"""硬闸：在用付费额度（extra usage）或订阅额度用完时，后台大批整理一律不跑。"""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ethan import bgwork, codex_runner, interviews, relations, syscheck
from ethan.core import quota
from ethan.core.backends import BackendUnavailable, Reply, SubscriptionBackend, _parse_stream
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings

NOW = datetime(2026, 11, 5, 20, 0, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=2)


def info(**kw):
    base = {"status": "allowed", "resetsAt": int(RESET.timestamp()), "rateLimitType": "seven_day", "isUsingOverage": False}
    base.update(kw)
    return base


def stream(result_text="嗯", rl=None, error=False):
    lines = [{"type": "system", "subtype": "init"}]
    if rl is not None:
        lines.append({"type": "rate_limit_event", "rate_limit_info": rl})
    lines.append({"type": "result", "subtype": "error_during_execution" if error else "success", "is_error": error,
                  "result": result_text, "total_cost_usd": 0.01, "usage": {"input_tokens": 1, "output_tokens": 2}})
    return "\n".join(json.dumps(x) for x in lines) + "\n"


class Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()


class ParseTest(Tmp):
    def test_stream_json_command_and_rate_limit(self):
        seen = {}

        def run(cmd, **kw):
            seen["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout=stream(rl=info(isUsingOverage=True)), stderr="")
        r = SubscriptionBackend(runner=run).complete("s", [{"role": "user", "content": "嗨"}])
        self.assertIn("stream-json", seen["cmd"])
        self.assertIn("--verbose", seen["cmd"])
        self.assertEqual(r.text, "嗯")
        self.assertTrue(r.rate_limit["isUsingOverage"])
        self.assertEqual(r.usage()["output_tokens"], 2)

    def test_old_single_json_still_parses(self):
        res, rl = _parse_stream(json.dumps({"type": "result", "subtype": "success", "result": "x"}))
        self.assertEqual((res["result"], rl), ("x", None))

    def test_error_carries_rate_limit(self):
        run = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout=stream("You've hit your limit", info(status="rejected"), True), stderr="")
        with self.assertRaises(BackendUnavailable) as c:
            SubscriptionBackend(runner=run).complete("s", [{"role": "user", "content": "嗨"}])
        self.assertEqual(c.exception.rate_limit["status"], "rejected")
        self.assertIn("hit your limit", str(c.exception))


class Sub:
    name = "subscription"

    def __init__(self, rl=None, fail=None):
        self.rl, self.fail, self.calls = rl, fail, 0

    def complete(self, system, messages, images=()):
        self.calls += 1
        if self.fail:
            raise self.fail
        return Reply("好", "subscription", "sonnet", 0.01, rate_limit=self.rl)


class RouterTest(Tmp):
    def router(self, backend):
        return Router(backend, Ledger(self.root / "ledger"), Settings(self.root / "settings.json"))

    def test_overage_reply_blocks_background_but_chat_answers(self):
        r = self.router(Sub(info(isUsingOverage=True)))
        self.assertEqual(r.complete("s", [{"role": "user", "content": "嗨"}]).text, "好")  # 聊天照常
        b = quota.blocked(self.root, NOW)
        self.assertIn("付费额度", b["reason"])
        self.assertEqual(datetime.fromisoformat(b["until"]), RESET)
        self.assertFalse(quota.blocked(self.root, RESET + timedelta(minutes=1)))
        self.assertEqual(quota.state(self.root)["isUsingOverage"], True)

    def test_back_to_normal_clears(self):
        self.router(Sub(info(status="rejected"))).complete("s", [])
        self.assertTrue(quota.blocked(self.root))
        self.router(Sub(info())).complete("s", [])
        self.assertFalse(quota.blocked(self.root))

    def test_limit_error_without_info_blocks_until_utc9(self):
        r = self.router(Sub(fail=BackendUnavailable("claude 退出码 1：Claude AI usage limit reached")))
        with self.assertRaises(BackendUnavailable):
            r._call(r.primary, "s", [], "chat")
        until = datetime.fromisoformat(quota.blocked(self.root)["until"])
        self.assertEqual((until.hour, until.minute), (9, 0))
        self.assertEqual(quota.next_reset_guess(NOW), datetime(2026, 11, 6, 9, 0, tzinfo=timezone.utc))
        self.assertEqual(quota.next_reset_guess(NOW.replace(hour=8)), datetime(2026, 11, 5, 9, 0, tzinfo=timezone.utc))

    def test_openrouter_errors_do_not_block(self):
        class OR(Sub):
            name = "openrouter"
        r = self.router(OR(fail=BackendUnavailable("429 rate limit")))
        with self.assertRaises(BackendUnavailable):
            r._call(r.primary, "s", [], "chat")
        self.assertFalse(quota.blocked(self.root))

    def test_other_errors_do_not_block(self):
        r = self.router(Sub(fail=BackendUnavailable("claude 调用失败：timed out")))
        with self.assertRaises(BackendUnavailable):
            r._call(r.primary, "s", [], "chat")
        self.assertFalse(quota.blocked(self.root))


class RawStore:
    raw = SimpleNamespace(recent=lambda n: [])


class PatientTest(Tmp):
    def test_blocked_stops_at_once_no_waiting_no_cap(self):
        quota.block(self.root, NOW + timedelta(days=3), "在用付费额度", NOW)
        backend = Sub()
        r = Router(backend, Ledger(self.root / "ledger"), Settings(self.root / "settings.json"))
        sleeps = []
        p = bgwork.PatientRouter(r, RawStore(), sleep=sleeps.append, now=lambda: NOW)
        with self.assertRaises(bgwork.QuotaBlocked):
            p.complete("s", [])
        self.assertEqual((sleeps, backend.calls), ([], 0))
        # 3 天后（超过 MAX_YIELD 的 6 小时很多）还停着，直到额度恢复那一刻
        p.now = lambda: NOW + timedelta(days=2, hours=23)
        with self.assertRaises(bgwork.QuotaBlocked):
            p.complete("s", [])
        p.now = lambda: NOW + timedelta(days=3, minutes=1)
        self.assertEqual(p.complete("s", []).text, "好")

    def test_overage_seen_mid_run_stops_next_call(self):
        r = Router(Sub(info(isUsingOverage=True)), Ledger(self.root / "ledger"), Settings(self.root / "settings.json"))
        p = bgwork.PatientRouter(r, RawStore(), sleep=lambda s: None)
        self.assertEqual(p.complete("s", []).text, "好")  # 这一次已经花了，记下
        with self.assertRaises(bgwork.QuotaBlocked):
            p.complete("s", [])

    def test_limit_error_no_backoff_retry(self):
        backend = Sub(fail=BackendUnavailable("x", info(status="rejected")))
        r = Router(backend, Ledger(self.root / "ledger"), Settings(self.root / "settings.json"))
        sleeps = []
        p = bgwork.PatientRouter(r, RawStore(), sleep=sleeps.append)
        with self.assertRaises(bgwork.QuotaBlocked):
            p.complete("s", [])
        self.assertEqual((backend.calls, sleeps), (1, []))

    def test_utilization_yields(self):
        settings = Settings(self.root / "settings.json")
        quota.observe(self.root, info(unifiedWindows={"five_hour": {"utilization": 0.85, "resetsAt": int((NOW + timedelta(hours=2)).timestamp())}}), NOW)
        b = bgwork.budget(Ledger(self.root / "ledger"), settings, NOW)
        self.assertEqual(b["util"], {"five_hour": 0.85})
        self.assertTrue(b["yielding"])
        self.assertFalse(bgwork.budget(Ledger(self.root / "ledger"), settings, NOW + timedelta(hours=3))["yielding"])  # 窗口重置了
        settings.set(bgwork.UTIL_KEY, 0.9)
        self.assertFalse(bgwork.budget(Ledger(self.root / "ledger"), settings, NOW)["yielding"])


class EntryTest(Tmp):
    def setUp(self):
        super().setUp()
        quota.block(self.root, datetime.now(timezone.utc) + timedelta(days=1), "在用付费额度（extra usage），订阅额度恢复前后台不跑")

    def test_interviews_digest_all_exits_before_lock(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["x", "--data", str(self.root), "--digest-all"]), redirect_stdout(out), \
                mock.patch("ethan.cli.build_router", side_effect=AssertionError("不该开工")):
            interviews.main()
        self.assertIn("付费额度", out.getvalue())
        self.assertFalse((self.root / interviews.LOCK).exists())

    def test_relations_organize_all_exits(self):
        out = io.StringIO()
        with redirect_stdout(out), mock.patch("ethan.cli.build_router", side_effect=AssertionError("不该开工")):
            self.assertEqual(relations.main(["--data", str(self.root), "--organize-all"]), 0)
        self.assertFalse((self.root / relations.LOCK).exists())

    def test_codex_runner_exits(self):
        out = io.StringIO()
        with mock.patch.object(sys, "argv", ["x", "--data", str(self.root), "--repo", str(self.root)]), redirect_stdout(out), \
                mock.patch.object(codex_runner, "MemoryStore", side_effect=AssertionError("不该开工")):
            codex_runner.main()
        self.assertIn("[codex]", out.getvalue())

    def test_nightly_skips(self):
        store = SimpleNamespace(root=self.root)
        self.assertIn("付费额度", interviews.nightly(store, None)["skipped"])
        self.assertIn("付费额度", relations.nightly(store, None, NOW.date())["skipped"])

    def test_digest_stops_without_counting_a_try(self):
        done = []
        db = SimpleNamespace(conn=SimpleNamespace(rollback=lambda: None))
        store = SimpleNamespace(db=db, root=self.root)
        evs = [{"id": 1, "text": "访谈", "occurred_at": "2026-11-01"}, {"id": 2, "text": "访谈", "occurred_at": "2026-11-02"}]
        with mock.patch.object(interviews, "pending", return_value=evs), \
                mock.patch.object(interviews, "_digest_one", side_effect=bgwork.QuotaBlocked("付费")), \
                mock.patch.object(interviews, "_done", side_effect=lambda *a: done.append(a)):
            skip = set()
            got = interviews.digest(store, None, skip=skip)
        self.assertEqual((done, got["interviews"], skip), ([], 0, set()))

    def test_stop_or_blocked(self):
        self.assertTrue(bgwork.stop_or_blocked(self.root)())
        quota.clear(self.root)
        self.assertFalse(bgwork.stop_or_blocked(self.root)())
        self.assertTrue(bgwork.stop_or_blocked(self.root, lambda: True)())

    def test_syscheck_shows_block(self):
        q = syscheck.quota(self.root)
        self.assertTrue(q["yielding"])
        self.assertIn("付费额度", q["paid_block"]["reason"])
        (self.root / "ledger").mkdir()
        q = syscheck.quota(self.root)
        self.assertTrue(q["paid_block"]["until_local"])


if __name__ == "__main__":
    unittest.main()
