"""Codex 评审 P2（11-05，第 10、12 条）：后台整理的日志、锁、有限批和心跳；主动消息“发送中”标记防重发；提醒不被排前面的挡死。"""

import json
import os
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ethan import bgwork, interviews, jobs, lifebase, nudges, proactive, relations
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("ETHAN_NO_KICK", "1")


class BackgroundLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_kicks_write_logs_instead_of_devnull(self):
        for kick, name in ((interviews.kick_digest_all, "interviews"), (relations.kick_all, "relations")):
            seen = {}
            self.assertTrue(kick(self.root, spawn=lambda cmd, **kw: seen.update(kw)))
            self.assertEqual(seen["stderr"], subprocess.STDOUT)
            self.assertEqual(Path(seen["stdout"].name), self.root / "logs" / f"{name}.log")
            self.assertTrue(seen["stdout"].closed)  # 交给子进程后父进程这边关掉
            self.assertIn("启动", (self.root / "logs" / f"{name}.log").read_text(encoding="utf-8"))

    def test_log_is_size_capped(self):
        logs = self.root / "logs"
        logs.mkdir()
        (logs / "x.log").write_text("a" * (bgwork.LOG_MAX + 1), encoding="utf-8")
        bgwork.log_file(self.root, "x").close()
        self.assertTrue((logs / "x.log.1").exists())
        self.assertLess((logs / "x.log").stat().st_size, 200)

    def test_relations_nightly_shares_the_organize_lock(self):
        store = MemoryStore(self.root)
        try:
            held = relations.lock(self.root)
            self.assertIsNotNone(held)
            try:
                self.assertEqual(relations.nightly(store, None, date(2026, 11, 5)), {"skipped": "后台正在整理"})
                self.assertIsNone(relations.lock(self.root))
            finally:
                held.close()
            self.assertEqual(relations.nightly(store, None, date(2026, 11, 5))["done"], 0)
        finally:
            store.close()
        self.assertIn("relations.nightly(", (ROOT / "ethan" / "nightly.py").read_text(encoding="utf-8"))

    def test_digest_all_stops_after_budget_and_beats(self):
        store = MemoryStore(self.root)
        mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        try:
            for i in range(3):
                interviews.receive(store, {"session": f"s{i}", "scene": "x", "text": f"问：喜欢什么\n答：喜欢游泳{i}"},
                                   now=datetime(2026, 11, 4, tzinfo=timezone.utc))
            n = len(interviews.pending(store.db))
            self.assertEqual(n, 3)
            beats, done = [], []
            with mock.patch.object(interviews, "_digest_one", side_effect=lambda *a, **k: done.append(1) or {"ADD": 0}):
                got = interviews.digest_all(store, None, stop=lambda: len(done) >= 1, beat=beats.append)
            self.assertEqual((got["interviews"], got.get("more")), (1, True))
            self.assertEqual(len(interviews.pending(store.db)), 2)  # 下一轮接着整理
            self.assertTrue(beats)
        finally:
            lifebase.MIRROR_DIR = mirror
            store.close()

    def test_organize_all_stops_after_budget(self):
        rounds = [{"done": 20, "skipped": 0, "failed": 0, "names": ["a"]}, {"done": 3, "skipped": 0, "failed": 0, "names": ["b"]}]
        with mock.patch.object(relations, "organize", side_effect=rounds) as org:
            got = relations.organize_all(None, None, None, stop=lambda: org.call_count >= 1)
        self.assertEqual((got["done"], got.get("more")), (20, True))

    def test_backlog_service_runs_bounded_rounds(self):
        svc = (ROOT / "deploy" / "systemd" / "ethan2-backlog.service").read_text(encoding="utf-8")
        self.assertIn("--digest-all --max-minutes", svc)
        self.assertIn("--organize-all --max-minutes", svc)

    def test_heartbeat_shows_in_job_overview_and_survives_record(self):
        start = datetime.now(timezone.utc) - timedelta(minutes=30)
        jobs.mark_start(self.root, "backlog", start)
        bgwork.heartbeat(self.root, "访谈：整理了 3 份")
        row = next(r for r in jobs.overview(self.root) if r["key"] == "backlog")
        self.assertEqual(row["state"], "running")
        self.assertIn("最近一次进展", row["detail"])
        self.assertIn("访谈：整理了 3 份", row["detail"])
        jobs.record(self.root, "backlog", True, start, datetime.now(timezone.utc))
        self.assertIn("progress_at", jobs.load(self.root)["backlog"])


class Backend:
    name = "subscription"

    def complete(self, system, messages, images=()):
        from ethan.core.backends import Reply
        return Reply("在干嘛呀", self.name, "m", 0.0)


class Channel:
    owner = 42

    def __init__(self):
        self.sent, self.crash = [], False

    def deliver(self, chat_id, bubbles, voices=(), **kw):
        self.sent.append(list(bubbles))
        if self.crash:  # 发出去了，进程在记“已发”之前死掉
            raise RuntimeError("进程被杀")


def london(h, m=0):
    return datetime(2026, 10, 23, h - 1, m, tzinfo=timezone.utc)  # 伦敦夏令时


class SendingMarkTest(unittest.TestCase):
    def setUp(self):
        from ethan.core.agent import Ethan
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router, Settings
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.ethan = Ethan(self.store, Router(Backend(), Ledger(root / "ledger"), Settings(root / "settings.json")), persona="人设")
        self.channel = Channel()
        self.at = proactive.planned_time(date(2026, 10, 23), proactive.SLOTS[0])
        self.state_path = self.store.root / proactive.STATE_FILE

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def tick(self, h, m):
        return proactive.tick(self.ethan, self.channel, now=london(h, m)).split("；")[-1]

    def test_sent_but_crashed_before_record_is_not_resent(self):
        self.channel.crash = True
        with self.assertRaises(RuntimeError):
            self.tick(self.at.hour, self.at.minute)
        self.assertEqual(len(self.channel.sent), 1)
        self.channel.crash = False
        self.tick(self.at.hour, self.at.minute + 5 if self.at.minute < 50 else self.at.minute)
        self.assertEqual(len(self.channel.sent), 1)  # 不盲目重发
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["2026-10-23"]["morning"], proactive.UNKNOWN)

    def test_sending_mark_left_by_restart_becomes_unknown(self):
        self.state_path.write_text(json.dumps({"2026-10-23": {"perk:x:7": proactive.SENDING}}), encoding="utf-8")
        out = proactive.tick(self.ethan, self.channel, now=london(self.at.hour, self.at.minute))
        self.assertIn("结果未知", out)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["2026-10-23"]["perk:x:7"], proactive.UNKNOWN)
        self.assertTrue(nudges.told(self.store.db, "perk:x:7"))  # 只提一次的：结果未知也算提过，换一天也不重发

    def test_model_unavailable_clears_the_mark(self):
        from ethan.core.router import AllBackendsFailed
        with mock.patch.object(self.ethan, "reach_out", side_effect=AllBackendsFailed("x")):
            self.assertIn("模型不可用", self.tick(self.at.hour, self.at.minute))
        state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {}
        self.assertNotIn("morning", state.get("2026-10-23", {}))  # 没发出去：下次照常再试
        self.assertEqual(self.tick(self.at.hour, self.at.minute), "morning：已发送")

    def test_blocked_head_nudge_does_not_starve_the_tail(self):
        """排前面的“打卡提醒”被“刚聊过”挡住时，后面的“晚上收工”（刚聊过也照说）照样发得出去。"""
        self.store.raw.append("telegram", "mia", "我还在改论文", at=london(21, 0))
        none = lambda *a: None
        names = [c.__name__ for c in nudges.checks()]
        patches = {n: none for n in names}
        patches["_checkin"] = lambda db, now, done: None if "checkin-ask:x" in done else ("checkin-ask:x", "问打卡")
        patches["_evening"] = lambda db, now, done: None if "evening:2026-10-23:remind" in done else ("evening:2026-10-23:remind", "提醒收尾")
        with mock.patch.multiple(nudges, **patches):
            out = self.tick(21, 10)
        self.assertEqual(out, "evening:2026-10-23:remind：已发送")


if __name__ == "__main__":
    unittest.main()
