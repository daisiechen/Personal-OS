"""Codex 代码评审（“你们俩沟通一致之后直接改”）最高优先级三条，核实后修：
1. 删后停用的知识，小伴 按题（onto_query）还能读到；
2. 人物卡的读取不看对外档和记忆暂停；
3. 她改的值和“她改过”的锁分两次提交，自动整理插在中间会覆盖她的改动。"""

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ethan import lifebase, locks, memsearch, mepage, onto_query, ontology, portrait, relations
from ethan.memory import MemoryStore

TODAY = date(2026, 11, 5)


class ForgottenNotReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        ontology.ensure(self.db)
        a = lifebase.add_evidence(self.db, "chat", "raw:1", "2026-11-01", "mia：前任说我晚上不该喝咖啡")
        b = lifebase.add_evidence(self.db, "chat", "raw:2", "2026-11-01", "mia：嗯")
        self.pref = lifebase.add_fact(self.db, "preferences", "晚上不喝咖啡", [a, b], kind="preference", source_kind="her_words")
        self.hyp = lifebase.add_fact(self.db, "temperament", "可能怕冲突", [a, b], kind="hypothesis", source_kind="system_inference")
        self.db.conn.execute("INSERT OR REPLACE INTO profile_blocks (dimension, summary, fact_ids, version, updated_at) "
                             "VALUES ('preferences', '晚上不喝咖啡', ?, 1, 'x')", (json.dumps([self.pref]),))
        self.db.conn.commit()
        self.a = a

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def values(self, got):
        return [i.get("value") for i in got.get("items") or []]

    def test_suspended_after_forget_not_read_anywhere(self):
        self.assertIn("晚上不喝咖啡", self.values(onto_query.preferences(self.db, "ethan", "咖啡", today=TODAY)))
        rep = lifebase.forget(self.db, [self.a], root=self.root)
        self.assertEqual(sorted(rep["facts_suspended"]), sorted([self.pref, self.hyp]))
        # 以前：说法上停用只是 candidate，小伴 照样能按题读到（偏好、猜测）
        self.assertEqual(self.values(onto_query.preferences(self.db, "ethan", "咖啡", today=TODAY)), [])
        self.assertEqual(self.values(onto_query.hypothesis_case(self.db, "ethan")), [])
        sid = self.db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (self.pref,)).fetchone()[0]
        self.assertEqual(onto_query.provenance(self.db, "ethan", sid)["status"], "unknown")
        st = self.db.conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(lifebase.disclose(self.db, "twin", st), "deny")
        memsearch.refresh(self.db)
        self.assertNotIn(f"fact:{self.pref}", [r["key"] for r in memsearch.search(self.db, ["咖啡"], user="ethan")])
        self.assertNotIn("咖啡", portrait.chat_text(self.db, "ethan"))
        # 她看过说“留着”：恢复后又能读到
        self.assertTrue(mepage.keep_suspended(self.db, self.pref)["ok"])
        self.assertIn("晚上不喝咖啡", self.values(onto_query.preferences(self.db, "ethan", "咖啡", today=TODAY)))


class RelationsGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)
        self.pid = self.db._insert("people", name="小林", importance=3, last_contact="2026-09-01", birthday="11-07",
                                   updated_at="x")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_outside_users_get_nothing(self):
        self.assertTrue(relations.due(self.db, TODAY, user="ethan")["contact"])
        for user in ("twin", "site"):
            d = relations.due(self.db, TODAY, user=user)
            self.assertEqual((d["contact"], d["days"], d["reply"]), ([], [], []), user)
            self.assertEqual(relations.page(self.db, TODAY, user=user)["people"], [])
        self.assertEqual(onto_query.person(self.db, "twin", "小林")["status"], "unknown")  # 对外看到的都是“不知道”

    def test_memory_hold_stops_automatic_readers_not_her(self):
        lifebase.hold(self.db, "测试：删除记录核不清")
        self.assertEqual(relations.chat_hint(self.db, TODAY), "")
        self.assertEqual(relations.week_line(self.db, TODAY)["这周该联系谁"], [])
        self.assertEqual(relations.organize(self.db, None, TODAY)["done"], 0)
        page = relations.page(self.db, TODAY)  # 她本人看自己的“关系”页不受影响
        self.assertEqual([p["name"] for p in page["people"]], ["小林"])
        self.assertTrue(page["due"]["contact"])
        lifebase.release_hold(self.db)
        self.assertIn("小林", relations.chat_hint(self.db, TODAY))


class SameTransactionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        relations.ensure(self.db)
        self.pid = self.db._insert("people", name="小林", relation="同学", updated_at="x")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def card(self):
        return {"relation": "整理出来的关系", "topics": [], "recent": "", "dates": [], "open_loops": []}

    def test_organize_in_between_cannot_overwrite_her_edit(self):
        """以前：她的值先提交、锁后提交；另一个进程的整理插在中间看不到锁，把她的改动盖掉。"""
        other = MemoryStore(self.root)
        other.db.conn.execute("PRAGMA busy_timeout = 0")
        seen = {}
        real = locks.lock

        def organize_meanwhile(db, *a, **kw):
            row = other.db.conn.execute("SELECT * FROM people WHERE id = ?", (self.pid,)).fetchone()
            try:
                relations._apply_card(other.db, row, self.card(), TODAY)
                seen["overwrote"] = True
            except sqlite3.OperationalError:  # 她的改动没提交完，整理拿不到写锁：等她改完
                seen["waited"] = True
            return real(db, *a, **kw)
        try:
            with mock.patch.object(relations.locks, "lock", organize_meanwhile):
                self.assertTrue(relations.set_field(self.db, self.pid, "relation", "她写的关系")["ok"])
            self.assertEqual(seen, {"waited": True})
            row = other.db.conn.execute("SELECT * FROM people WHERE id = ?", (self.pid,)).fetchone()
            relations._apply_card(other.db, row, self.card(), TODAY)  # 她改完以后再整理：看到锁，不动
        finally:
            other.close()
        self.assertEqual(self.db.conn.execute("SELECT relation FROM people WHERE id = ?", (self.pid,)).fetchone()[0], "她写的关系")
        self.assertTrue(locks.locked(self.db, "person", self.pid, "relation"))

    def test_lock_failure_rolls_back_her_value(self):
        with mock.patch.object(relations.locks, "lock", side_effect=RuntimeError("锁表坏了")):
            with self.assertRaises(RuntimeError):
                relations.set_field(self.db, self.pid, "relation", "她写的关系")
        self.assertEqual(self.db.conn.execute("SELECT relation FROM people WHERE id = ?", (self.pid,)).fetchone()[0], "同学")

    def test_me_page_fix_writes_fact_and_lock_together(self):
        """“我”页改说法：对的那句和“她锁定”以前分开提交，中间出错留下一条没锁的新说法。"""
        from ethan import memusers
        lifebase.ensure(self.db)
        ev = lifebase.add_evidence(self.db, "chat", "raw:9", "2026-11-01", "mia：我住在伦敦")
        lifebase.add_fact(self.db, "resources", "住在曼彻斯特", [ev])
        n = self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        with mock.patch.object(memusers.lifebase, "supersede", side_effect=RuntimeError("中途断了")):
            with self.assertRaises(RuntimeError):
                memusers.open_ticket(self.db, "住在伦敦", "住在曼彻斯特", entry="me_page")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], n)
        got = memusers.open_ticket(self.db, "住在伦敦", "住在曼彻斯特", entry="me_page")
        self.assertEqual(self.db.conn.execute("SELECT locked FROM facts WHERE id = ?", (got["fact_id"],)).fetchone()[0], 1)

    def test_workbench_edit_and_lock_one_transaction(self):
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import NOON, FakeCalendar, make_demo
        tmp = tempfile.TemporaryDirectory()
        store = make_demo(Path(tmp.name))
        try:
            wb = Workbench(store, Settings(Path(tmp.name) / "settings.json"), calendar=FakeCalendar())
            tid = wb.act("task.add", {"title": "发邮件", "when": "later"}, now=NOON)["id"]
            with mock.patch.object(locks, "record_action", side_effect=RuntimeError("锁表坏了")):
                got = wb.act("task.rename", {"id": tid, "title": "给导师发进度邮件"}, now=NOON)
            self.assertFalse(got["ok"])  # 以前：名字改了、锁没记上，只打一行日志，计划器以后会改回去
            self.assertEqual(store.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0], "发邮件")
            self.assertTrue(wb.act("task.rename", {"id": tid, "title": "给导师发进度邮件"}, now=NOON)["ok"])
            self.assertTrue(locks.locked(store.db, "task", tid, "title"))
            self.assertFalse(store.db.conn.in_transaction)
        finally:
            store.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
