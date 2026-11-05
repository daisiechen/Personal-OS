"""一次动作一个事务（docs/design/ontology-contracts.md 1.7 节）：中途出错全部回滚；同一编号不做两次；合法的第二次修改照做。"""

import tempfile
import unittest
from pathlib import Path

from ethan import decisions, lifebase, locks, tx
from ethan.memory import MemoryStore


class TxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def count(self, table):
        return self.db.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_error_midway_rolls_back_everything(self):
        d = lifebase.approve_disclosure(self.db, "在找实习", quote="这次可以跟 Leo 说", once=True, to_whom="Leo")
        ev = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：周末睡懒觉")
        decisions.ensure(self.db)
        locks.ensure(self.db)
        self.db.conn.commit()
        facts, decs, lks = self.count("facts"), self.count("decisions"), self.count("manual_locks")
        with self.assertRaises(ValueError):
            with tx.action(self.db, "测试", "回滚", "v1", "test"):
                lifebase.add_fact(self.db, "rhythms", "周末睡懒觉", [ev])
                decisions.add(self.db, "rule", "作息", "周末不排早上的事")
                locks.lock(self.db, "task", 5, "title", "新名字")
                self.assertIsNotNone(lifebase.claim_disclosure(self.db, d, "twin", recipient="Leo"))
                raise ValueError("中途出错")
        self.assertEqual((self.count("facts"), self.count("decisions"), self.count("manual_locks")), (facts, decs, lks))
        self.assertIsNotNone(lifebase.claim_disclosure(self.db, d, "twin", recipient="Leo"))  # 领取也回滚了，还能领
        self.assertEqual(self.db.conn.execute("SELECT result FROM action_log WHERE action_key = '测试|回滚|v1'").fetchone()[0], "failed")

    def test_same_key_not_done_twice_but_new_basis_is(self):
        with tx.action(self.db, "改日程", "11-03 训练", "v3", "mia"):
            locks.lock(self.db, "event", "2026-11-03:训练", "time", {"start": "18:25"})
        with self.assertRaises(tx.AlreadyDone):
            with tx.action(self.db, "改日程", "11-03 训练", "v3", "mia"):
                pass
        with tx.action(self.db, "改日程", "11-03 训练", "v4", "mia"):  # 同一日程合法地再改一次
            pass
        self.assertEqual(self.count("action_log"), 2)

    def test_failed_action_can_be_retried(self):
        with self.assertRaises(RuntimeError):
            with tx.action(self.db, "建待办", "买菜", "v1", "planner"):
                raise RuntimeError("外部服务没回来")
        with tx.action(self.db, "建待办", "买菜", "v1", "planner"):
            pass
        rows = self.db.conn.execute("SELECT result FROM action_log WHERE action_key = '建待办|买菜|v1'").fetchall()
        self.assertEqual([r[0] for r in rows], ["done"])

    def test_outside_action_helpers_still_commit(self):
        locks.lock(self.db, "task", 7, "title", "新名字")
        self.db.conn.rollback()  # 动作外面照旧马上提交，回滚不掉
        self.assertEqual(self.count("manual_locks"), 1)

    def test_no_nested_actions(self):
        with tx.action(self.db, "外", "a", "v1", "test"):
            with self.assertRaises(RuntimeError):
                with tx.action(self.db, "里", "b", "v1", "test"):
                    pass


if __name__ == "__main__":
    unittest.main()
