"""“她改过”的锁（docs/design/system-rules.md 第 1 节，她 2026-11-02 认可、先试行）：她亲手改过、删过的，自动流程不改回去、不建回来。"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from ethan import locks, planner, prep, stages
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import NOON, FakeCalendar, make_demo


class LockTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"), calendar=FakeCalendar())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def act(self, action, **p):
        return self.wb.act(action, p, now=NOON)

    def test_deleted_todo_not_recreated_by_planner_ethan_or_chat(self):
        tid = self.act("task.add", title="查英语陪练的渠道", when="later")["id"]
        self.act("task.delete", id=tid)
        self.assertTrue(locks.she_deleted(self.db, "查英语陪练的渠道"))
        self.assertTrue(locks.she_deleted(self.db, "查英语陪练渠道"))  # 说法略有不同的同一件（意思一样、字面差很多的要等向量，见 ontology 10.1）
        items = [{"kind": "todo", "title": "查英语陪练的渠道", "priority": "side"}, {"kind": "todo", "title": "买菜", "priority": "side"}]
        planner.link_tasks(self.db, items, NOON.date())
        self.assertEqual([i["title"] for i in items], ["买菜"])  # 计划器不再建回来
        # 她恢复了：锁放开，计划器又能排
        undo = {"action": "task.restore", "row": {"id": tid, "title": "查英语陪练的渠道", "created_at": "x"}}
        self.act(undo.pop("action"), **undo)
        self.assertFalse(locks.she_deleted(self.db, "查英语陪练的渠道"))

    def test_renamed_title_not_changed_back_by_ethan(self):
        from ethan.core.agent import Ethan
        from ethan.core.backends import Reply
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router

        class B:
            name = "subscription"

            def complete(self, system, messages, images=()):
                return Reply("嗯", self.name, "m", 0)
        tid = self.act("task.add", title="发邮件", when="later")["id"]
        self.act("task.rename", id=tid, title="给导师发进度邮件")
        e = Ethan(self.store, Router(B(), Ledger(self.root / "ledger"), Settings(self.root / "settings.json")), persona="人设")
        e.note_records(f"[待办改 #{tid} 发邮件]")
        self.assertEqual(self.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0], "给导师发进度邮件")

    def test_day_lock_expires_next_day_long_lock_stays(self):
        locks.lock(self.db, "event", "2026-11-02:训练", "time", {"start": "17:10"}, "day", NOON.date())
        self.assertTrue(locks.locked(self.db, "event", "2026-11-02:训练", "time", NOON.date()))
        self.assertFalse(locks.locked(self.db, "event", "2026-11-02:训练", "time", NOON.date() + timedelta(days=1)))
        locks.lock(self.db, "task", 5, "title", "新名字")
        self.assertTrue(locks.locked(self.db, "task", 5, "title"))
        self.assertEqual(locks.release(self.db, "task", 5), 1)  # 她说“按系统的来”
        self.assertFalse(locks.locked(self.db, "task", 5, "title"))

    def test_carried_repeat_task_she_deleted_stays_deleted(self):
        self.db.conn.execute("INSERT INTO goals (id, level, title, status, updated_at) VALUES (90, 'year', '减脂', 'active', 'x')")
        stages.ensure(self.db)
        m1 = stages.add_milestone(self.db, 90, "M1", "2026-11-15")
        m2 = stages.add_milestone(self.db, 90, "M2", "2026-11-29")
        stages.add_task(self.db, m1, "按清单买菜", "weekly", 1)
        rep = stages.add_task(self.db, m1, "吃够蛋白质", "daily")
        locks.lock(self.db, "task", rep, "deleted", title="吃够蛋白质", note="goal:90")
        stages.advance(self.db, m1, NOON.date())
        self.assertEqual([t["title"] for t in stages.tasks_of(self.db, m2)], ["按清单买菜"])

    def test_prep_review_leaves_what_she_starred(self):
        import json
        from types import SimpleNamespace
        tid = self.db.add_task("整理芬兰装备清单", promised_by_ethan=True)
        prep.ensure(self.db)
        prep._set(self.db, tid, state="todo")
        self.act("prep.star", id=tid, on=True)

        class R:
            def complete(self, system, messages, purpose="chat"):
                return SimpleNamespace(text=json.dumps({"order": [], "stale": [{"id": tid}], "delivered": [], "add": []}))
        prep.review(self.store, R())
        self.assertIn(tid, [x["id"] for x in prep.queue(self.db)])  # 她点了星的，回头看时不替她删


    def test_release_by_ethan_tag_and_workbench(self):
        """她说“按系统的来”：小伴 写 [按系统的来 #编号] / [按系统的来 日程 名字]，或在计划面板点按钮，锁就放开。"""
        from ethan.core.agent import Ethan, parse_reply
        from ethan.core.backends import Reply
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router
        from ethan.planner import note_override

        class B:
            name = "subscription"

            def complete(self, system, messages, images=()):
                return Reply("嗯", self.name, "m", 0)
        tid = self.act("task.add", title="发邮件", when="later")["id"]
        self.act("task.rename", id=tid, title="给导师发进度邮件")
        e = Ethan(self.store, Router(B(), Ledger(self.root / "ledger"), Settings(self.root / "settings.json")), persona="人设")
        text = f"好，撤销你的改动～[撤销改动 #{tid}]"
        self.assertEqual(parse_reply(text)["bubbles"], ["好，撤销你的改动～"])
        e.note_records(text)
        self.assertFalse(locks.locked(self.db, "task", tid, "title"))
        today = NOON.date()
        note_override(self.db, today, "训练", "time", "17:10", "18:25")
        locks.lock(self.db, "event", f"{today}:训练", "time", None, "day", today)
        out = self.act("event.release", title="训练")
        self.assertTrue(out["ok"])
        self.assertFalse(locks.locked(self.db, "event", f"{today}:训练", "time", today))
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM plan_overrides WHERE day = ? AND title = '训练'", (today.isoformat(),)).fetchone())

if __name__ == "__main__":
    unittest.main()
