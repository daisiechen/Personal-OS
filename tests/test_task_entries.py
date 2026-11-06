"""完成和删除的入口要一致（盘点 docs/research/2026-11-task-system-audit.md 第 3 节第 8、9 条，2026-11-06 修）：
Telegram 早报按钮、📋清单、小伴 的 [结果 #… 完成] 和工作台走同一个完成函数；Telegram 清单 ✕ 和工作台删除一样记“她删过”。"""

import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from ethan import game_view, locks, quests, stages, taskrules
from ethan.core.agent import Ethan
from ethan.memory import MemoryStore
from ethan.todo import handle_callback

D = date(2026, 11, 3)


class TaskEntriesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        self.gid = self.db.conn.execute(
            "INSERT INTO goals (title, level, status, updated_at) VALUES ('减脂塑形', 'year', 'active', 'x')").lastrowid
        self.m1 = stages.add_milestone(self.db, self.gid, "M1 先把节奏建起来", "2026-11-15")
        self.m2 = stages.add_milestone(self.db, self.gid, "M2 稳住", "2026-11-30")
        self.weigh = stages.add_task(self.db, self.m1, "训练前称重", "daily")
        self.review = stages.add_task(self.db, self.m1, "M1 复盘", review=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def row(self, tid):
        return dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def logged(self, tid):
        return [r["day"] for r in self.db.conn.execute("SELECT day FROM stage_log WHERE task_id = ? ORDER BY day", (tid,))]

    def assert_daily_like_workbench(self, tick):
        """反复做的阶段任务：每天记一笔 +10，不整件勾死，第二天还能再做。"""
        self.assertEqual(tick(self.weigh, D), stages.REPEAT_POINTS)
        self.assertEqual(self.row(self.weigh)["done"], 0)
        self.assertEqual(tick(self.weigh, D), 0)  # 同一天再点不重复给分
        self.assertEqual(tick(self.weigh, D + timedelta(days=1)), stages.REPEAT_POINTS)
        self.assertEqual(self.logged(self.weigh), [D.isoformat(), (D + timedelta(days=1)).isoformat()])
        self.assertEqual(self.row(self.weigh)["done"], 0)

    def assert_review_advances(self, tick):
        tick(self.review, D)
        self.assertEqual(stages.current(self.db, self.gid)["id"], self.m2)  # 复盘勾了，进下一个里程碑

    def test_telegram_todo_list_done(self):
        def gain(tid, day):
            start = quests.totals(self.db)["earned"]
            handle_callback(self.db, f"td:done:t:{tid}", day)
            return quests.totals(self.db)["earned"] - start
        self.assert_daily_like_workbench(gain)
        self.assert_review_advances(gain)

    def test_morning_card_button(self):
        def gain(tid, day):
            start = quests.totals(self.db)["earned"]
            game_view.handle(self.db, f"qs:t:{tid}", day)
            return quests.totals(self.db)["earned"] - start
        self.assert_daily_like_workbench(gain)
        self.assert_review_advances(gain)

    def test_ethan_result_done(self):
        ethan = SimpleNamespace(store=self.store)

        def gain(tid, day):
            self.db.save_plan(day.isoformat(), "model", "", "", "fp", [{"kind": "todo", "title": "x", "task_id": tid}])
            item = self.db.conn.execute("SELECT MAX(id) FROM plan_items").fetchone()[0]
            start = quests.totals(self.db)["earned"]
            self.assertEqual(Ethan.note_plan(ethan, f"[结果 #{item} 完成]", day, asked=True), [])
            return quests.totals(self.db)["earned"] - start
        self.assert_daily_like_workbench(gain)
        self.assert_review_advances(gain)

    def test_plain_task_unchanged(self):
        tid = self.db.add_task("买牛奶")
        self.assertEqual(taskrules.complete(self.db, tid, D), quests.task_points(self.row(tid)))  # 普通待办照旧按 quests 算
        self.assertEqual(self.row(tid)["done"], 1)
        self.assertEqual(taskrules.complete(self.db, tid, D), 0)
        self.assertEqual(taskrules.complete(self.db, 99999, D), 0)  # 按钮过期、那件已经不在了

    def test_telegram_delete_records_her_lock(self):
        tid = self.db.add_task("查英语陪练的渠道")
        self.assertEqual(handle_callback(self.db, f"td:del:t:{tid}", D), "删掉了")
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone())
        self.assertTrue(locks.she_deleted(self.db, "查英语陪练的渠道"))
        self.assertEqual(self.db.conn.execute(
            "SELECT to_state FROM transitions WHERE kind = 'todo' AND ref = ? ORDER BY id DESC", (tid,)).fetchone()[0], "deleted")
        self.assertEqual(handle_callback(self.db, f"td:del:t:{tid}", D), "这件已经不在了")

    def test_stage_task_delete_keeps_goal_scope(self):
        handle_callback(self.db, f"td:del:t:{self.weigh}", D)
        self.assertTrue(locks.she_deleted(self.db, "训练前称重", goal_id=self.gid))


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
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
