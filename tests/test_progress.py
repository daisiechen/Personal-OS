import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import goals2026
from ethan.memory import MemoryStore
from ethan.progress import goal_progress, year_progress


def seed_parents(db):
    vision = db.add_goal("这辈子干一件大事", "vision", status="active")
    for title, domain in (("常年保持健康体态", "健康"), ("拥有 2–3 个能长期坚持的爱好", "休闲"),
                          ("去世界各地看看", "旅行"), ("做出自己的小事业", "事业"),
                          ("拥有几段长久的好友情", "感情")):
        db.add_goal(title, "long", parent_id=vision, status="active", domain=domain)
    ids = {r["title"]: r["id"] for r in db.conn.execute("SELECT id, title FROM goals")}
    db.add_goal("建立每周训练和饮食习惯", "stage", parent_id=ids["常年保持健康体态"])
    db.add_goal("认识更多新朋友", "stage", parent_id=ids["拥有几段长久的好友情"],
                status="active")


class ProgressTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        seed_parents(self.db)
        goals2026.apply(self.store)
        self.ids = {r["title"]: r["id"] for r in self.db.conn.execute("SELECT id, title FROM goals")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def goal(self, title):
        return dict(self.db.conn.execute("SELECT * FROM goals WHERE id = ?", (self.ids[title],)).fetchone())

    def test_goals2026_is_idempotent_and_adjusts_old_goals(self):
        before = self.db.conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0]
        goals2026.apply(self.store)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM goals").fetchone()[0], before)
        self.assertEqual(self.goal("认识更多新朋友")["status"], "dropped")
        body = self.goal("减脂塑形：年底体脂 27%–28%、体重约 56.5 kg")
        self.assertEqual((body["level"], body["due"]), ("year", "2026-12-31"))
        self.assertEqual(self.goal("建立每周训练和饮食习惯")["parent_id"], body["id"])
        self.assertEqual(len(self.db.body_measures("2026-10-01")), 1)

    def test_weight_compares_with_straight_line_plan(self):
        gid = self.ids["减脂塑形：年底体脂 27%–28%、体重约 56.5 kg"]
        for day, w in (("2026-11-10", 59.6), ("2026-11-12", 59.2), ("2026-11-14", 59.0)):
            self.db.add_body_measure(day, weight=w)
        p = goal_progress(self.db, self.goal("减脂塑形：年底体脂 27%–28%、体重约 56.5 kg"), date(2026, 11, 15))
        self.assertEqual(p["current"], 59.3)
        self.assertEqual(p["expected"], 58.9)  # 60 → 56.5，66 天走了 20 天
        self.assertEqual(p["status"], "落后")
        self.assertEqual(goal_progress(self.db, self.goal("减脂塑形：年底体脂 27%–28%、体重约 56.5 kg"),
                                       date(2026, 11, 30))["status"], "还没有数据")
        self.assertTrue(gid)

    def test_weekly_count_and_paused_travel(self):
        gid = self.ids["每周至少 1 次新体验"]
        today = date.today()
        self.assertIn(goal_progress(self.db, self.goal("每周至少 1 次新体验"), today)["status"], ("正常", "落后"))
        self.db.log_metric(gid, note="Tate Modern")
        self.assertEqual(goal_progress(self.db, self.goal("每周至少 1 次新体验"), today)["status"], "达成")
        self.assertEqual(goal_progress(self.db, self.goal("年底前去 3 个新地方"), today)["status"], "暂停")

    def test_funnel_reports_where_it_is_stuck(self):
        gid = self.ids["创业拿到第一个订单"]
        for _ in range(30):
            self.db.log_metric(gid, step="列出潜在客户")
        for _ in range(4):
            self.db.log_metric(gid, step="联系")
        p = goal_progress(self.db, self.goal("创业拿到第一个订单"), date(2026, 11, 1))
        self.assertEqual(p["stuck_at"], "联系")
        self.assertIn("联系 4/20", p["text"])
        self.db.log_metric(gid, step="成交")
        self.assertEqual(goal_progress(self.db, self.goal("创业拿到第一个订单"), date(2026, 11, 1))["status"], "达成")

    def test_year_progress_lists_only_year_goals(self):
        titles = [p["title"] for p in year_progress(self.db, date(2026, 11, 1))]
        self.assertIn("创业拿到第一个订单", titles)
        self.assertNotIn("认识更多新朋友", titles)
        launch = [p for p in year_progress(self.db, date(2026, 11, 1)) if p["title"] == "DemoApp 上线"][0]
        self.assertIn("计划 2026-12-31 上线", launch["text"])


if __name__ == "__main__":
    unittest.main()


class PausedWithoutMetricTest(unittest.TestCase):
    def test_paused_goal_without_metric_is_paused(self):
        """2026-10-29：毕业论文这种没定指标的暂停副本，以前判成“没有数据”，还显示在副本进度里。"""
        import tempfile
        from pathlib import Path
        from datetime import date
        from ethan.memory import MemoryStore
        from ethan import progress
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            p = progress.goal_progress(store.db, {"id": 999, "title": "毕业论文", "status": "paused"}, date(2026, 11, 2))
            self.assertEqual(p["status"], progress.STATUS_PAUSED)
            store.close()
