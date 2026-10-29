import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import planner
from tests.test_workbench import MONDAY, make_demo


class PlannerMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_deleted_task_is_remembered_for_planner(self):
        tid = self.db.add_task("去看那个不想去的展")
        self.db.delete_task(tid)
        facts = planner.gather_facts(self.db, MONDAY)
        self.assertIn("去看那个不想去的展", facts["deleted_recently"])
        self.assertIn("deleted_recently", planner.PLANNER_PROMPT)

    def weigh(self, start, kg):
        for i in range(7):
            self.db.conn.execute("INSERT INTO body_measures (day, weight, at) VALUES (?, ?, 'x')", ((start + timedelta(days=i)).isoformat(), kg))
        self.db.conn.commit()

    def test_weekly_calorie_adjustment(self):
        self.assertEqual(planner.weekly_adjust(self.db, MONDAY)["offset"], 0)  # 体重记录不够两周
        self.weigh(MONDAY, 60.0)
        self.weigh(MONDAY + timedelta(days=7), 60.1)  # 没降
        nxt = MONDAY + timedelta(days=14)
        adj = planner.weekly_adjust(self.db, nxt)
        self.assertEqual(adj["offset"], -100)
        self.assertIn("少吃 100", adj["reason"])
        self.assertEqual(planner.weekly_adjust(self.db, nxt + timedelta(days=3))["offset"], -100)  # 一周只算一次
        base = planner.nutrition(60.0, nxt)["kcal"]
        self.assertEqual(planner.nutrition(60.0, nxt, -100)["kcal"], base - 100)
        self.weigh(nxt, 59.0)  # 掉了 1.9%
        self.assertEqual(planner.weekly_adjust(self.db, nxt + timedelta(days=7))["offset"], 0)


    def test_review_and_skipped_kept_and_goal_task_edits(self):
        data = planner.parse_plan('{"breakdowns": [], "events": [], "ethan": [], "review": {"9": "落后 0.3 kg，今天先把训练打了"}, '
                                  '"skipped": [{"title": "去 V&A", "why": "今天下雨"}]}')
        self.assertEqual(data["review"], {"9": "落后 0.3 kg，今天先把训练打了"})
        self.assertEqual(data["skipped"][0]["why"], "今天下雨")
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        wb = Workbench(self.store, Settings(Path(self.tmp.name) / "settings.json"))
        gid = wb.bosses(MONDAY)[0]["goal_id"]
        self.assertTrue(wb.act("goal.set", {"id": gid, "title": "减脂塑形（新）", "due": "2026-12-20", "target": "53"})["ok"])
        b = next(x for x in wb.bosses(MONDAY) if x["goal_id"] == gid)
        self.assertEqual(b["due"], "2026-12-20")
        self.assertFalse(wb.act("goal.set", {"id": gid, "due": "明天"})["ok"])  # 日期不对就不改
        tid = self.db.add_task("旧名字")
        self.assertTrue(wb.act("task.rename", {"id": tid, "title": "新名字"})["ok"])
        self.assertEqual(self.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0], "新名字")
        self.assertFalse(wb.act("plan.replan", {})["ok"])  # 测试里没有重排进程
        calls = []
        wb.replan_runner = type("R", (), {"__call__": lambda s: calls.append(1) or True, "busy": lambda s: False})()
        self.assertTrue(wb.act("plan.replan", {})["ok"])
        self.assertEqual(calls, [1])

if __name__ == "__main__":
    unittest.main()
