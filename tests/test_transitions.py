"""状态转换记录（docs/design/system-rules.md 第 2 节，她 2026-11-02 认可）：做了再撤回回到原样；重复点不重复。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import quests, stages
from ethan.memory import MemoryStore

DAY = date(2026, 11, 14)


class TransitionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = db = self.store.db
        quests.seed(db)
        db.conn.execute("INSERT INTO goals (id, level, title, status, updated_at) VALUES (20, 'year', '减脂', 'active', 'x')")
        db.conn.execute("INSERT INTO goals (id, level, title, status, handled_by, updated_at) VALUES (24, 'year', 'DemoApp', 'active', 'agent', 'x')")
        stages.ensure(db)
        self.m1 = stages.add_milestone(db, 20, "M1", "2026-11-15")
        self.m2 = stages.add_milestone(db, 20, "M2", "2026-11-29")
        self.once = stages.add_task(db, self.m1, "测体脂")
        self.rep = stages.add_task(db, self.m1, "按清单买菜", "weekly", 1)
        self.review = stages.add_task(db, self.m1, "M1 复盘", review=True)
        self.a1 = stages.add_milestone(db, 24, "M2 内测", "2026-11-30")
        self.a2 = stages.add_milestone(db, 24, "M3 上线", "2026-12-15")
        self.a_task = stages.add_task(db, self.a1, "找 5 个内测用户")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def snapshot(self):
        db = self.db
        return (quests.totals(db)["earned"],
                sorted((r["id"], r["title"], r["done"], r["milestone_id"]) for r in db.conn.execute("SELECT * FROM tasks")),
                sorted((r["id"], r["status"]) for r in db.conn.execute("SELECT id, status FROM milestones")))

    def test_review_check_then_undo_restores_everything(self):
        before = self.snapshot()
        stages.check(self.db, self.review, DAY)
        self.assertEqual(stages.current(self.db, 20)["id"], self.m2)
        self.assertIn("按清单买菜", [t["title"] for t in stages.tasks_of(self.db, self.m2)])  # 反复做的带过去
        stages.uncheck(self.db, self.review, DAY)
        self.assertEqual(self.snapshot(), before)  # 分数、任务、里程碑状态都回到原样
        kinds = [h["to_state"] for h in stages.history(self.db, "milestone", self.m1)]
        self.assertEqual(kinds[-2:], ["missed", "open"])

    def test_repeat_clicks_do_not_stack(self):
        stages.check(self.db, self.review, DAY)
        once = self.snapshot()
        for _ in range(3):  # 她 11-01 连点 4 次
            stages.check(self.db, self.review, DAY)
        self.assertEqual(self.snapshot(), once)
        stages.uncheck(self.db, self.review, DAY)
        after_undo = self.snapshot()
        stages.uncheck(self.db, self.review, DAY)
        self.assertEqual(self.snapshot(), after_undo)

    def test_reopen_keeps_carried_task_she_already_used(self):
        stages.check(self.db, self.review, DAY)
        carried = next(t for t in stages.tasks_of(self.db, self.m2) if t["title"] == "按清单买菜")
        stages.check(self.db, carried["id"], date(2026, 11, 16))  # 她已经在 M2 打过卡
        stages.uncheck(self.db, self.review, DAY)
        self.assertIn(carried["id"], [t["id"] for t in stages.tasks_of(self.db, self.m2)])  # 动过的不删

    def test_undo_last_task_reopens_agent_milestone(self):
        """交给 agent 的副本没有复盘：最后一件做完自动结束里程碑；撤销这一件，里程碑也要重新打开（以前不会）。"""
        before = self.snapshot()
        stages.check(self.db, self.a_task, DAY)
        self.assertEqual(stages.current(self.db, 24)["id"], self.a2)
        stages.uncheck(self.db, self.a_task, DAY)
        self.assertEqual(stages.current(self.db, 24)["id"], self.a1)
        self.assertEqual(self.snapshot(), before)


    def test_other_kinds_recorded_and_unknown_states_flagged(self):
        """副本、待办、准备员的事、每周复盘也记转换；状态图之外的状态，检查报出来。"""
        from ethan import prep, transitions, weekreview
        tid = self.db.add_task("买菜")
        quests.complete_task(self.db, tid, DAY)
        quests.uncomplete_task(self.db, tid)
        self.assertEqual([h["to_state"] for h in transitions.history(self.db, "todo", tid)], ["done", "open"])
        pid = self.db.add_task("整理芬兰装备", promised_by_ethan=True)
        prep.ensure(self.db)
        prep._set(self.db, pid, state="todo")
        prep._set(self.db, pid, state="ready", result="好了")
        prep._set(self.db, pid, result="改了一下")  # 状态没变不记
        self.assertEqual([(h["from_state"], h["to_state"]) for h in transitions.history(self.db, "prep", pid)],
                         [(None, "todo"), ("todo", "ready")])
        weekreview.open_review(self.db, DAY, ["week"])
        rid = self.db.conn.execute("SELECT id FROM reviews").fetchone()[0]
        self.assertEqual([h["to_state"] for h in transitions.history(self.db, "review", rid)], ["open"])
        self.assertEqual(transitions.unknown_states(self.db), [])
        transitions.record(self.db, "prep", pid, "ready", "飞走了")
        self.assertTrue(transitions.unknown_states(self.db))

if __name__ == "__main__":
    unittest.main()
