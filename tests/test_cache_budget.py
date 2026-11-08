"""提示词“固定的在前、变的在后”和订阅额度预算（docs/research/borrow-2026-11.md 第 3、4 条）。"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import bgwork, syscheck
from ethan.core.agent import build_system_prompt
from ethan.core.backends import OpenRouterBackend, Reply, SubscriptionBackend, SystemPrompt
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings

NOW = datetime(2026, 11, 5, 15, 0, tzinfo=timezone.utc)


def mem(text):
    return [{"label": "聊天", "at": "2026-11-01", "title": "", "text": text}]


def ctx_turn(n: int) -> dict:
    """同一天的两轮：时间、简报、按题查到的、检索、补查都不同；画像、目标这类一天才变的相同。"""
    return {"today": f"2026-11-0{n}", "time": f"伦敦 1{n}:0{n}", "open_tasks": [f"待办{n}"],
            "profile": "她的画像", "life": "小伴 的剧本",
            "routed": {"q": n, "name": "题", "got": {"status": "unknown", "items": []}},
            "memory": mem(f"第{n}轮查到的"), "lookups": [{"asked": f"词{n}", "found": mem("补查")}],
            "deeds_note": f"重写提醒{n}"}


class FixedFirstTest(unittest.TestCase):
    def test_fixed_prefix_identical_and_first(self):
        a, b = build_system_prompt("人设原文", ctx_turn(1)), build_system_prompt("人设原文", ctx_turn(2))
        self.assertIsInstance(a, SystemPrompt)
        self.assertTrue(a.startswith("人设原文"))
        self.assertGreater(a.fixed, 1000)
        self.assertEqual(a[:a.fixed].encode("utf-8"), b[:b.fixed].encode("utf-8"))  # 逐轮字节相同
        self.assertNotEqual(a, b)
        fixed = a[:a.fixed]
        for rule in ("陪伴模式下用短句回复", "[纠错 她 维度", "想用语音说的话放进 [VOICE]", "## 运行时信息"):
            self.assertIn(rule, fixed)

    def test_changing_parts_all_after_fixed(self):
        p = build_system_prompt("人设原文", ctx_turn(1))
        for mark in ("今天是 2026-11-01", "### 现在几点", "### 今日简报", "### 按题查到的", "### 记忆里和她这句话相关的",
                     "### 你刚才要查的", "重写提醒1", "### 她的画像", "## 你自己的生活"):
            self.assertGreater(p.index(mark), p.fixed, mark)
        # 一天才变的在前，每轮都变的在后
        self.assertLess(p.index("### 她的画像"), p.index("今天是 2026-11-01"))
        for mark in ("### 现在几点", "### 今日简报", "### 按题查到的", "### 记忆里和她这句话相关的"):
            self.assertLess(p.index("今天是 2026-11-01"), p.index(mark))

    def test_switches_do_not_leak_into_dynamic(self):
        p = build_system_prompt("人设", ctx_turn(1), can_delegate=True, can_photo=True)
        self.assertIn("[交给Grok]", p[:p.fixed])
        self.assertIn("[照片 画面描述]", p[:p.fixed])

    def test_concat_drops_marker(self):
        p = build_system_prompt("人设", ctx_turn(1))
        self.assertEqual(getattr(p + "x", "fixed", 0), 0)


class Resp:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.data).encode()


class BackendUsageTest(unittest.TestCase):
    def _openrouter(self, model, usage=None):
        sent = {}

        def opener(req, timeout=None):
            sent["body"] = json.loads(req.data)
            return Resp({"choices": [{"message": {"content": "好"}}], "usage": usage or {}})
        return OpenRouterBackend("k", model=model, opener=opener), sent

    def test_cache_control_on_fixed_for_claude_on_openrouter(self):
        p = build_system_prompt("人设", ctx_turn(1))
        be, sent = self._openrouter("anthropic/claude-sonnet-4.5")
        be.complete(p, [{"role": "user", "content": "嗨"}])
        content = sent["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(content[0]["text"] + content[1]["text"], str(p))
        self.assertNotIn("cache_control", content[1])

    def test_other_models_get_plain_string(self):
        p = build_system_prompt("人设", ctx_turn(1))
        be, sent = self._openrouter("x-ai/grok-4.7")
        be.complete(p, [{"role": "user", "content": "嗨"}])
        self.assertEqual(sent["body"]["messages"][0]["content"], str(p))
        be.complete("普通字符串", [{"role": "user", "content": "嗨"}])
        self.assertEqual(sent["body"]["messages"][0]["content"], "普通字符串")

    def test_openrouter_usage(self):
        be, _ = self._openrouter("x-ai/grok-4.7", {"prompt_tokens": 1000, "completion_tokens": 50, "cost": 0.01,
                                                   "prompt_tokens_details": {"cached_tokens": 800}})
        r = be.complete("s", [{"role": "user", "content": "嗨"}])
        self.assertEqual(r.usage(), {"input_tokens": 200, "output_tokens": 50, "cache_read_tokens": 800, "cache_write_tokens": None})
        be, _ = self._openrouter("x-ai/grok-4.7")
        self.assertEqual(be.complete("s", [{"role": "user", "content": "嗨"}]).usage(),
                         {"input_tokens": None, "output_tokens": None, "cache_read_tokens": None, "cache_write_tokens": None})

    def test_subscription_usage(self):
        out = {"type": "result", "subtype": "success", "is_error": False, "result": " 嗯 ", "total_cost_usd": 0.05,
               "usage": {"input_tokens": 12, "output_tokens": 30, "cache_read_input_tokens": 9000, "cache_creation_input_tokens": 400}}
        run = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(out), stderr="")
        r = SubscriptionBackend(runner=run).complete("s", [{"role": "user", "content": "嗨"}])
        self.assertEqual((r.text, r.cost_usd), ("嗯", 0.05))
        self.assertEqual(r.usage(), {"input_tokens": 12, "output_tokens": 30, "cache_read_tokens": 9000, "cache_write_tokens": 400})


class LedgerWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "ledger")

    def tearDown(self):
        self.tmp.cleanup()

    def test_records_tokens_or_nulls(self):
        self.ledger.record("subscription", "sonnet", 0.1, "chat", at=NOW,
                           tokens={"input_tokens": 5, "output_tokens": 6, "cache_read_tokens": 7, "cache_write_tokens": None})
        self.ledger.record("subscription", "sonnet", 0.1, "photo", at=NOW)  # 老调用方不给 token
        rows = [json.loads(x) for x in (self.root / "ledger" / "2026-11.jsonl").read_text().splitlines()]
        self.assertEqual(rows[0]["cache_read_tokens"], 7)
        self.assertIsNone(rows[0]["cache_write_tokens"])
        self.assertNotIn("input_tokens", rows[1])

    def test_router_records_usage(self):
        class B:
            name = "subscription"

            def complete(self, system, messages, images=()):
                return Reply("好", "subscription", "sonnet", 0.02, 1, 2, 3, 4)
        Router(B(), self.ledger, Settings(self.root / "settings.json")).complete("s", [])
        w = self.ledger.window()
        self.assertEqual(w["calls"], 1)
        self.assertEqual(w["tokens"], {"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 3, "cache_write_tokens": 4})

    def test_window_sliding_rate_and_projection(self):
        rec = lambda h, usd, backend="subscription": self.ledger.record(backend, "m", usd, "bg", at=NOW - timedelta(hours=h))
        rec(6, 100)        # 窗口外
        rec(4.5, 2.0)      # 窗口内，但再过 1 小时就滑出去
        rec(2, 1.0)
        rec(0.5, 0.5)      # 最近一小时
        rec(0.2, 9, "openrouter")  # 不算订阅
        w = self.ledger.window(now=NOW)
        self.assertEqual(w["calls"], 3)
        self.assertAlmostEqual(w["used_usd"], 3.5)
        self.assertAlmostEqual(w["rate_per_hour"], 0.5)
        self.assertAlmostEqual(w["projected_usd"], 1.5 + 0.5)

    def test_window_crosses_month(self):
        start = datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc)
        self.ledger.record("subscription", "m", 1.0, at=start - timedelta(hours=2))  # 11 月的文件
        self.ledger.record("subscription", "m", 1.0, at=start - timedelta(minutes=10))
        self.assertAlmostEqual(self.ledger.window(now=start)["used_usd"], 2.0)


class RawStore:
    def __init__(self, minutes_ago=None):
        recs = [] if minutes_ago is None else [{"role": "mia", "at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}]
        self.raw = SimpleNamespace(recent=lambda n: recs[-n:])


class Backend:
    name = "subscription"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, images=()):
        self.calls += 1
        return Reply("好", "subscription", "sonnet", 0.01)


class BudgetYieldTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.ledger = Ledger(self.root / "ledger")
        self.settings = Settings(self.root / "settings.json")
        self.router = Router(Backend(), self.ledger, self.settings)

    def tearDown(self):
        self.tmp.cleanup()

    def spend(self, usd, hours_ago=0.5):
        self.ledger.record("subscription", "sonnet", usd, "chat", at=NOW - timedelta(hours=hours_ago))

    def test_default_limit_is_conservative(self):
        self.assertEqual(bgwork.budget_limit(self.settings), bgwork.BUDGET_USD)
        self.settings.set(bgwork.BUDGET_KEY, 12)
        self.assertEqual(bgwork.budget_limit(self.settings), 12.0)

    def test_background_yields_near_limit_then_resumes(self):
        self.spend(bgwork.BUDGET_USD, hours_ago=4.5)  # 快到门槛：这笔 30 分钟后滑出窗口
        clock, sleeps = [NOW], []

        def sleep(s):
            sleeps.append(s)
            clock[0] += timedelta(seconds=s)
        p = bgwork.PatientRouter(self.router, RawStore(), sleep=sleep, now=lambda: clock[0])
        self.assertTrue(bgwork.budget(self.ledger, self.settings, NOW)["yielding"])
        self.assertEqual(p.complete("s", [], purpose="bg").text, "好")
        self.assertTrue(sleeps and all(s == bgwork.BACKOFF * 60 for s in sleeps))
        self.assertGreaterEqual(p.budget_waits, 1)
        self.assertEqual(self.router.primary.calls, 1)

    def test_reported_utilization_wins_over_usd_estimate(self):
        """11-07：美元估算到了 $5，但 Claude 报的用量只有两三成——后台不该一直让路（从 11-05 起空转了两天）。"""
        from ethan.core import quota
        root = Path(self.settings.path).parent
        self.spend(bgwork.BUDGET_USD + 1, hours_ago=1)
        reset = int((NOW + timedelta(hours=3)).timestamp())
        quota.observe(root, {"status": "allowed_warning", "unifiedWindows": {"five_hour": {"utilization": 0.2, "resetsAt": reset, "status": "allowed"},
                                                                            "seven_day": {"utilization": 0.27, "resetsAt": reset + 86400, "status": "allowed"}}}, NOW)
        self.assertFalse(bgwork.budget(self.ledger, self.settings, NOW)["yielding"])
        quota.observe(root, {"status": "allowed", "unifiedWindows": {"five_hour": {"utilization": 0.85, "resetsAt": reset, "status": "allowed"}}}, NOW)
        self.assertTrue(bgwork.budget(self.ledger, self.settings, NOW)["yielding"])  # 真用到八成照样让路

    def test_projection_yields_before_hitting_limit(self):
        # 窗口里才用了 3 美元（没到 5），但最近一小时烧了 3：预计再过一小时到 6，先让路
        self.spend(3.0, hours_ago=0.5)
        b = bgwork.budget(self.ledger, self.settings, NOW)
        self.assertLess(b["used_usd"], b["limit_usd"])
        self.assertTrue(b["yielding"])

    def test_under_limit_no_wait_and_zero_disables(self):
        self.spend(1.0, hours_ago=3)
        sleeps = []
        p = bgwork.PatientRouter(self.router, RawStore(), sleep=sleeps.append, now=lambda: NOW)
        p.complete("s", [])
        self.assertEqual(sleeps, [])
        self.spend(50)
        self.settings.set(bgwork.BUDGET_KEY, 0)
        p.complete("s", [])
        self.assertEqual(sleeps, [])

    def test_gives_up_waiting_after_max_yield(self):
        self.spend(100, hours_ago=0.1)
        sleeps = []
        p = bgwork.PatientRouter(self.router, RawStore(), sleep=sleeps.append, now=lambda: NOW)  # 时钟不走：窗口一直满
        self.assertEqual(p.complete("s", []).text, "好")
        self.assertEqual(sum(sleeps), bgwork.MAX_YIELD)

    def test_chat_not_affected(self):
        self.spend(100)
        self.assertTrue(bgwork.budget(self.ledger, self.settings, NOW)["yielding"])
        self.assertEqual(self.router.complete("s", [{"role": "user", "content": "嗨"}]).text, "好")  # 聊天直接走 Router，不等

    def test_syscheck_shows_quota(self):
        q = syscheck.quota(self.root, NOW)
        self.assertEqual((q["calls"], q["yielding"]), (0, False))
        self.spend(100)
        q = syscheck.quota(self.root, NOW)
        self.assertTrue(q["yielding"])
        self.assertEqual(q["limit_usd"], bgwork.BUDGET_USD)
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertFalse(syscheck.quota(empty, NOW)["yielding"])
        self.assertFalse((empty / "ledger").exists())  # 体检只读，不建目录


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
