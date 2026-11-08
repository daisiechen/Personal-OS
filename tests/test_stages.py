import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import stages
from ethan.memory import MemoryStore

D = date(2026, 11, 3)  # 周二


class StagesTest(unittest.TestCase):
    """docs/design/quest-structure.md：副本 → 里程碑 → 阶段任务（四种频次、前置、引用日常）→ 每天的待办。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        self.gid = self.db.conn.execute("INSERT INTO goals (title, level, status, updated_at) VALUES ('减脂塑形', 'year', 'active', 'x')").lastrowid
        self.m1 = stages.add_milestone(self.db, self.gid, "7 天平均 ≤ 56.5 kg", "2026-11-15", 56.5)
        self.m2 = stages.add_milestone(self.db, self.gid, "7 天平均 ≤ 55.5 kg", "2026-11-30", 55.5)
        self.db.conn.execute("UPDATE milestones SET created_at = '2026-11-01T00:00:00+00:00'")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_milestones_drive_goal_progress(self):
        self.assertEqual(stages.current(self.db, self.gid)["id"], self.m1)
        self.assertEqual(stages.goal_progress(self.db, self.gid)["frac"], 0)
        stages.close_milestone(self.db, self.m1, reached=False, review="出差两周，节奏放慢")
        g = stages.goal_progress(self.db, self.gid)
        self.assertEqual((g["passed"], g["total"], g["current"]["id"]), (1, 2, self.m2))  # 没达成但复盘过也算走过
        with self.assertRaises(ValueError):
            stages.add_milestone(self.db, self.gid, " ")

    def test_done_tasks_in_current_milestone_count(self):
        """两个里程碑、M1 里 4 件做完 2 件 → 25%。"""
        ids = [stages.add_task(self.db, self.m1, t) for t in ("体态评估", "约复查", "买泡沫轴", "列动作清单")]
        stages.add_task(self.db, self.m1, "训练前称重", "daily")  # 反复做的不算
        for i in ids[:2]:
            self.db.conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (i,))
        self.db.conn.commit()
        g = stages.goal_progress(self.db, self.gid)
        self.assertEqual((g["frac"], g["cur_done"], g["cur_total"]), (0.25, 2, 4))
        p = stages.boss_progress(None, g)
        self.assertEqual(p["frac"], 0.25)
        self.assertIn("当前里程碑任务 2/4", p["label"])

    def test_four_cadences_become_todays_todos(self):
        weigh = stages.add_task(self.db, self.m1, "训练前称重", "daily")
        shop = stages.add_task(self.db, self.m1, "按清单买菜", "weekly", per_week=1)
        fat = stages.add_task(self.db, self.m1, "测体脂", "per_milestone")
        posture = stages.add_task(self.db, self.m1, "体态评估")
        book = stages.add_task(self.db, self.m1, "约 physio 复查", depends_on=posture)
        review = stages.add_task(self.db, self.m1, "M1 复盘", review=True)
        t = lambda i: dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (i,)).fetchone())
        due = lambda i, d=D: stages.due_today(self.db, t(i), d)
        self.assertTrue(all(due(i) for i in (weigh, shop, fat, posture)))
        self.assertFalse(due(book))  # 前置没做完不排
        self.assertTrue(stages.log(self.db, weigh, D))
        self.assertFalse(stages.log(self.db, weigh, D))  # 一天只记一次
        self.assertFalse(due(weigh))
        self.assertTrue(due(weigh, D + timedelta(days=1)))
        self.assertEqual(stages.progress(self.db, t(weigh), D)["text"], "1 / 3 天")  # 11-01 到 11-03
        stages.log(self.db, shop, D)
        self.assertFalse(due(shop, D + timedelta(days=1)))  # 这周够了
        self.assertTrue(due(shop, D + timedelta(days=6)))  # 下周一又要
        stages.log(self.db, fat, D)
        self.assertFalse(due(fat, D + timedelta(days=5)))  # 这个阶段做过了
        stages.log(self.db, posture, D)
        self.assertTrue(t(posture)["done"])
        self.assertTrue(due(book))  # 前置做完了就排
        self.assertEqual([x["id"] for x in stages.tasks_of(self.db, self.m1)][-1], review)  # 复盘排在最后

    def test_task_referencing_a_habit_counts_from_check_ins(self):
        from ethan import quests
        quests.seed(self.db)
        h = next(x for x in quests.habits(self.db) if x["cadence"] == "weekly")
        tid = stages.add_task(self.db, self.m1, "每周训练 5 次", "weekly", per_week=5, habit_id=h["id"])
        quests.check_habit(self.db, h["id"], D)
        row = dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        self.assertTrue(stages.progress(self.db, row, D)["text"].startswith("本周 1 / 5"))  # 后面还写这阶段几周达标



class BossProgressTest(unittest.TestCase):
    """进度条按什么画只在 stages.boss_progress 一处定，
    旁边写明依据；有指标按指标，没有指标按里程碑或步骤并写“按里程碑 1/4”，什么依据都没有就不画。"""

    def test_order_and_labels(self):
        from ethan.stages import boss_progress
        self.assertEqual(boss_progress(0.4, {"passed": 1, "total": 4}, 2, 5)["by"], "metric")
        ms = boss_progress(None, {"passed": 1, "total": 4}, 7, 8)
        self.assertEqual((ms["by"], ms["frac"], ms["label"]), ("milestones", 0.25, "按里程碑 1/4"))
        st = boss_progress(None, None, 3, 8)
        self.assertEqual((st["by"], st["label"], st["short"]), ("steps", "按步骤 3/8", "3/8 步"))
        none = boss_progress(None, {"passed": 0, "total": 0}, 0, 0)
        self.assertIsNone(none["frac"])
        self.assertEqual(none["label"], "")


if __name__ == "__main__":
    unittest.main()
