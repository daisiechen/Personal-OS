import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import checkup, jobs
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

NOW = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
CLOUD = "c" * 40
UP = lambda cmd, **k: SimpleNamespace(stdout="active\n")


class CheckupTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.store = MemoryStore(self.root)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_all_fine_is_empty(self):
        (self.root / "deploy-status.json").write_text(json.dumps({"result": "已上线", "to": "abc1234"}))
        for key in jobs.JOBS:
            jobs.record(self.root, key, True, NOW - timedelta(minutes=5), NOW - timedelta(minutes=4))
        mem = self.root / "meminfo"
        mem.write_text("MemTotal:        1990000 kB\nMemAvailable:     800000 kB\n")
        self.assertEqual(checkup.problems(self.root, self.store.db, NOW, UP, meminfo=mem), [])

    def test_reports_what_is_wrong(self):
        """系统出问题主动找她，不等她发现。"""
        (self.root / "deploy-status.json").write_text(json.dumps(
            {"result": "失败，已退回原版本", "to": "abc1234", "step": "跑测试", "detail": "FAILED (failures=1)"}))
        jobs.record(self.root, "proactive", False, NOW - timedelta(minutes=5), NOW - timedelta(minutes=4), "Traceback\nValueError: x")
        db = self.store.db
        run = db.start_job("planner")
        db.finish_job(run, error="写日历失败：401")
        down = lambda cmd, **k: SimpleNamespace(stdout="failed\n" if cmd[-1] == "ethan2-weixin" else "active\n")
        found = {p["what"]: p["detail"] for p in checkup.problems(self.root, db, datetime.now(timezone.utc), down)}
        self.assertIn("跑测试", found["自动上线"])
        self.assertIn("ValueError", found["主动消息"])
        self.assertIn("写日历失败", found["任务 planner"])
        self.assertEqual(found["服务 ethan2-weixin"], "没在跑（failed）")
        self.assertNotIn("周报", found)  # 还没跑过的（周报第一次在周日）不算出错

    def test_endpoint_needs_token(self):
        inbox = Inbox(self.store, {"cloud": CLOUD})
        self.assertEqual(inbox.handle_get("/cloud/health", {})[0], 401)
        code, body = inbox.handle_get("/cloud/health", {"Authorization": f"Bearer {CLOUD}"})
        self.assertEqual(code, 200)
        self.assertIn("problems", body)

    def test_ethan_knows(self):
        from ethan.core.agent import build_system_prompt
        prompt = build_system_prompt("人设", {"today": "2026-11-02", "system_issues": [{"what": "主动消息", "detail": "失败"}]})
        self.assertIn("主动消息：失败", prompt)


if __name__ == "__main__":
    unittest.main()
