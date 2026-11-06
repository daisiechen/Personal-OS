"""portrait._current 核实（盘点第 1 节）：以前只看 valid_to 和 status != invalid，删后停用（suspended）、删掉（deleted）
的也算“仍成立”。每晚 fact_ops 先检索旧事实、再等模型回、再执行：中间她删了证据（forget 把那条停用），执行时 NOOP 会往停用的
那条上追加证据、UPDATE 会从它 supersede 出一条新的有效说法——删了又被接回去。改成用 lifebase.GONE 统一判断。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, portrait
from ethan.memory import MemoryStore

DAY = date(2026, 11, 6)


class PortraitCurrentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        portrait.ensure(self.db)
        a = lifebase.add_evidence(self.db, "chat", "chat:1", "2026-11-01", "我在减脂")
        b = lifebase.add_evidence(self.db, "chat", "chat:2", "2026-11-02", "最近在减脂")
        self.fid = lifebase.add_fact(self.db, "body", "她在减脂", [a, b], confidence=1.0)
        self.a, self.b = a, b
        self.new_ev = lifebase.add_evidence(self.db, "chat", "chat:3", "2026-11-06", "还在减脂")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def status(self):
        return self.db.conn.execute("SELECT status, evidence_ids FROM facts WHERE id = ?", (self.fid,)).fetchone()

    def test_suspended_is_not_current(self):
        lifebase.forget(self.db, evidence_ids=[self.a], root=Path(self.tmp.name))
        self.assertEqual(self.status()["status"], "suspended")
        self.assertFalse(portrait._current(portrait._fact(self.db, self.fid)))

    def test_noop_after_forget_does_not_touch_suspended(self):
        """检索时还有效，等模型回的时候她删了证据：NOOP 不往停用的那条上追加证据。"""
        lifebase.forget(self.db, evidence_ids=[self.a], root=Path(self.tmp.name))
        before = self.status()["evidence_ids"]
        got = portrait.apply_ops(self.db, {"ops": [{"op": "NOOP", "from": "n1", "fact_id": self.fid}]}, self.new_ev, DAY)
        self.assertEqual(got["NOOP"], 0)
        self.assertEqual(self.status()["evidence_ids"], before)

    def test_update_after_forget_does_not_revive(self):
        lifebase.forget(self.db, evidence_ids=[self.a], root=Path(self.tmp.name))
        got = portrait.apply_ops(self.db, {"ops": [{"op": "UPDATE", "from": "n1", "fact_id": self.fid, "dimension": "body",
                                                    "statement": "她还在减脂"}]}, self.new_ev, DAY)
        self.assertEqual(got["UPDATE"], 0)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts WHERE statement = '她还在减脂'").fetchone()[0], 0)

    def test_deleted_is_not_current(self):
        lifebase.forget(self.db, evidence_ids=[self.a, self.b], root=Path(self.tmp.name))
        self.assertEqual(self.status()["status"], "deleted")
        self.assertFalse(portrait._current(portrait._fact(self.db, self.fid)))

    def test_candidate_still_matches(self):
        """候选（还没审的）照样能被今天的新说法证实或更新（盘点里 fact_ops 匹配的口径：当前 + 候选，不含停用）。"""
        self.db.conn.execute("UPDATE facts SET status = 'candidate' WHERE id = ?", (self.fid,))
        self.assertTrue(portrait._current(portrait._fact(self.db, self.fid)))


if __name__ == "__main__":
    unittest.main()
