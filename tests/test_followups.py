"""等着办（ethan/followups.py）。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import followups, lifebase, mepage, stages
from ethan.memory import MemoryStore


class FollowupsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_hers_becomes_task_under_milestone_when_task_done(self):
        """她的：前一件做完，第二天变成她的待办，挂在里程碑下（进副本）；还没到时在待你定“以后再办”里。"""
        stages.ensure(self.db)
        gid = self.db.add_goal("KingSail 首单", "stage")
        mid = stages.add_milestone(self.db, gid, "首轮融资")
        first = self.db.add_task("写完 BP")
        fid = followups.add(self.db, "约赖总聊 BP", f"task_done:{first}", for_her=True, milestone_id=mid, owner="她")
        self.assertEqual([x["what"] for x in followups.later(self.db)], ["约赖总聊 BP"])
        self.assertEqual(followups.daily(self.db, date(2026, 11, 8))["triggered"], 0)
        self.db.conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (first,))
        self.db.conn.commit()
        self.assertEqual(followups.daily(self.db, date(2026, 11, 9)), {"triggered": 1, "her": 1})
        t = self.db.conn.execute("SELECT title, milestone_id FROM tasks WHERE title = '约赖总聊 BP'").fetchone()
        self.assertEqual(tuple(t), ("约赖总聊 BP", mid))
        self.assertEqual(followups.later(self.db), [])
        self.assertEqual(followups.daily(self.db, date(2026, 11, 10))["triggered"], 0)  # 只做一次
        self.assertEqual(followups.add(self.db, "约赖总聊 BP", f"task_done:{first}", for_her=True), fid + 1)  # 办完的可以再登记新的

    def test_system_item_never_in_her_lists_and_shows_in_checkup(self):
        """系统自己的：不进她的待办和待你定；到了在体检里提示负责的人，收掉就没了；等太久也提示。"""
        fid = followups.add(self.db, "核对 Apple Pay 去重", "check:applepay_seen", owner="Mia OS 开发会话")
        self.assertEqual(followups.later(self.db), [])
        self.assertEqual(followups.problems(self.db, date(2026, 11, 8)), [])
        self.assertIn("等太久", followups.problems(self.db, date(2026, 12, 1))[0]["what"])
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, source TEXT)")
        self.db.conn.execute("INSERT INTO transactions (source) VALUES ('applepay')")
        self.db.conn.commit()
        followups.daily(self.db, date(2026, 11, 8))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertIn("到了", followups.problems(self.db, date(2026, 11, 8))[0]["what"])
        self.assertTrue(followups.close(self.db, fid))
        self.assertEqual(followups.problems(self.db, date(2026, 12, 1)), [])

    def test_date_and_ask(self):
        followups.add(self.db, "下赛季要不要继续英语陪练", "date:2026-12-01", for_her=True, then="ask")
        followups.daily(self.db, date(2026, 11, 30))
        self.assertEqual(mepage.pending_count(self.db), 0)
        followups.daily(self.db, date(2026, 12, 1))
        self.assertEqual(mepage.pending_count(self.db), 1)

    def test_bad_input_rejected(self):
        for wait in ("whenever", "date:明天", "check:不存在", "task_done:abc"):
            with self.assertRaises(ValueError):
                followups.add(self.db, "x", wait)
        with self.assertRaises(ValueError):
            followups.add(self.db, "x", "date:2026-12-01", for_her=False, then="task")  # 系统的不能变成她的待办


if __name__ == "__main__":
    unittest.main()
