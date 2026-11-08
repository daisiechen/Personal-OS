import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import attributes, lifelog
from ethan.memory import MemoryStore

DAY = date(2026, 12, 31)


class AttributesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifelog.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def habit(self, title, days):
        hid = self.db.conn.execute("INSERT INTO habits (title, cadence, points, active) VALUES (?, 'daily', 10, 1)", (title,)).lastrowid
        for d in days:
            self.db.conn.execute("INSERT INTO habit_log (habit_id, day, at) VALUES (?, ?, ?)", (hid, d.isoformat(), d.isoformat()))
        self.db.conn.commit()

    def by_key(self, rows):
        return {r["key"]: r for r in rows}

    def test_scores_weights_missing_and_labels(self):
        self.habit("训练", [DAY - timedelta(days=i) for i in range(0, 60, 2)])  # 隔天练：30 天里 15 天
        self.habit("0 点前睡", [DAY - timedelta(days=i) for i in range(60)])
        rows = self.by_key(attributes.compute(self.db, DAY))
        body = rows["body"]
        items = {i["name"]: i for i in body["items"]}
        self.assertAlmostEqual(items["训练"]["score"], 70.0, places=1)  # 15 / (目标每周 5 天 × 30/7)；决定 8 前按 17 天算是 88.2
        self.assertEqual(items["睡眠"]["score"], 100.0)
        self.assertIsNone(items["饮食"]["score"])  # 没有这个坚持，不算
        self.assertAlmostEqual(body["score"], round((70.0 * 30 + 100 * 25) / 55, 1), places=0)
        self.assertEqual(body["label"], "良好")
        self.assertEqual(rows["academic"]["label"], "未解锁")
        self.assertEqual(body["delta"], 0.0)  # 前一个 30 天也是这样

    def test_short_history_scales_standard(self):
        self.habit("训练", [DAY - timedelta(days=i) for i in range(0, 10, 2)])  # 系统才记了 9 天，练了 5 天
        items = {i["name"]: i for i in self.by_key(attributes.compute(self.db, DAY))["body"]["items"]}
        self.assertEqual(items["训练"]["score"], 77.8)  # 标准折成 目标 5×9/7≈6.4 天（决定 8 前按 17×9/30 是 98.0）
        self.assertIsNone(self.by_key(attributes.compute(self.db, DAY))["body"]["delta"])  # 不满 60 天不给涨跌

    def test_social_and_snapshot(self):
        lifelog.add(self.db, "meet", DAY - timedelta(days=59), who="老朋友")  # 有满 30 天的记录，标准不折算
        for i in range(3):
            lifelog.add(self.db, "meet", DAY - timedelta(days=i * 5), who=f"朋友{i}")
        self.db.conn.commit()
        rows = self.by_key(attributes.compute(self.db, DAY))
        self.assertEqual({i["name"]: i["score"] for i in rows["social"]["items"]}["和朋友见面"], 70.0)  # 3 / (每周 1 次 × 30/7)；决定 8 前按 30 天 4 次是 75
        self.assertEqual(attributes.snapshot(self.db, DAY), 7)
        attributes.snapshot(self.db, DAY)  # 重跑覆盖
        hist = attributes.history(self.db, "social")
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["day"], DAY.isoformat())



class CareerMilestoneTest(unittest.TestCase):
    def test_goal_without_metric_scored_by_milestones_on_time(self):
        """事业副本没有指标时，至少按里程碑算——到今天该完成的里程碑完成了几个。"""
        from ethan import stages
        with tempfile.TemporaryDirectory() as t:
            store = MemoryStore(Path(t))
            db = store.db
            stages.ensure(db)
            g = db.add_goal("个人管理系统上线", "year", status="active", domain="事业")
            m1 = stages.add_milestone(db, g, "M1 工作台能每天用", None)
            stages.add_milestone(db, g, "M2 数据流程改版", "2026-11-22")
            db.conn.execute("UPDATE milestones SET status = 'done' WHERE id = ?", (m1,))
            db.conn.commit()
            day = date(2026, 11, 7)
            item = attributes.career(db, day - timedelta(days=29), day)[0]
            self.assertEqual(item["score"], 100.0)  # 还没有到期的：按期
            self.assertIn("11-22 到期", item["actual"])
            late = date(2026, 11, 25)
            item = attributes.career(db, late - timedelta(days=29), late)[0]
            self.assertEqual(item["score"], 0.0)  # M2 过期没做完
            self.assertIn("过期没做完：M2", item["actual"])
            store.close()


if __name__ == "__main__":
    unittest.main()
