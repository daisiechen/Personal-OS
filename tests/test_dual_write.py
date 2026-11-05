"""迁移第 2 步：新旧双写（docs/design/ontology-structure.md 9.2 节）。不管哪条路径写 facts，同一个事务里同步一条说法；对账逐条比。"""

import json
import tempfile
import unittest
from pathlib import Path

from ethan import lifebase, tx
from ethan.memory import MemoryStore


class DualWriteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：周末睡懒觉")

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def stmt(self, fid):
        return self.db.conn.execute("SELECT s.* FROM statements s JOIN fact_map m ON m.statement_id = s.id WHERE m.fact_id = ?",
                                    (fid,)).fetchone()

    def test_new_fact_mirrored_with_sources(self):
        f = lifebase.add_fact(self.db, "rhythms", "周末睡懒觉", [self.ev], kind="preference")
        s = self.stmt(f)
        self.assertEqual((s["property"], s["value_text"], s["review"], s["kind"], s["said_by"]),
                         ("dim:rhythms", "周末睡懒觉", "accepted", "preference", "her_words"))
        src = [r[0] for r in self.db.conn.execute("SELECT evidence_id FROM statement_sources WHERE statement_id = ?", (s["id"],))]
        self.assertEqual(src, [self.ev])
        self.assertEqual(lifebase.reconcile_statements(self.db), [])

    def test_changes_from_any_module_follow(self):
        f = lifebase.add_fact(self.db, "rhythms", "周末睡懒觉", [self.ev])
        self.db.conn.execute("UPDATE facts SET status = 'invalid', valid_to = '2026-11-03' WHERE id = ?", (f,))  # 像 mepage 那样直接改
        s = self.stmt(f)
        self.assertEqual((s["review"], s["valid_to"]), ("retracted", "2026-11-03"))
        self.assertIsNotNone(s["retracted_at"])
        changes = [r["field"] for r in self.db.conn.execute("SELECT field FROM statement_changes WHERE statement_id = ?", (s["id"],))]
        self.assertEqual(sorted(changes), ["review", "valid_to"])
        self.assertEqual(lifebase.reconcile_statements(self.db), [])

    def test_permissions_follow(self):
        f = lifebase.add_fact(self.db, "preferences", "看牙医前要吃甜的", [self.ev])
        self.db.conn.execute("UPDATE facts SET read_by = ?, locked = 1 WHERE id = ?", (json.dumps([{"user": "ethan"}]), f))
        s = self.stmt(f)
        self.assertEqual((json.loads(s["read_by"]), s["locked"], s["visibility"]), ([{"user": "ethan"}], 1, self.db.conn.execute("SELECT visibility FROM facts WHERE id = ?", (f,)).fetchone()[0]))
        self.assertEqual(lifebase.reconcile_statements(self.db), [])

    def test_supersede_links_statements(self):
        old = lifebase.add_fact(self.db, "rhythms", "周末 11 点起", [self.ev])
        new = lifebase.add_fact(self.db, "rhythms", "周末 9 点起", [self.ev])
        lifebase.supersede(self.db, old, new, when="2026-11-02")
        self.assertEqual(self.stmt(old)["superseded_by"], self.stmt(new)["id"])

    def test_forget_erases_statement_text_and_log_has_no_content(self):
        f = lifebase.add_fact(self.db, "preferences", "在看心理医生", [self.ev])
        lifebase.forget(self.db, [self.ev], root=self.root)
        s = self.stmt(f)
        self.assertEqual((s["value_text"], s["review"]), ("", "forgotten"))
        logged = json.dumps([tuple(r) for r in self.db.conn.execute("SELECT * FROM statement_changes")], ensure_ascii=False)
        self.assertNotIn("心理医生", logged)

    def test_rolls_back_together(self):
        before = self.db.conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]
        with self.assertRaises(RuntimeError):
            with tx.action(self.db, "记说法", "睡眠", "v1", "test"):
                lifebase.add_fact(self.db, "rhythms", "周末睡懒觉", [self.ev])
                raise RuntimeError("中途出错")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0], before)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)

    def test_other_people_mirrored_to_person_object(self):
        self.db.conn.execute("INSERT INTO people (id, name, updated_at) VALUES (3, 'Leo', 'x')")
        f = lifebase.add_fact(self.db, "relationships", "Leo 是导师", [self.ev], subject="person:3")
        s = self.stmt(f)
        name = self.db.conn.execute("SELECT o.name FROM objects o JOIN object_types t ON t.object_id = o.id "
                                    "WHERE o.id = ? AND t.type = 'person'", (s["subject_id"],)).fetchone()[0]
        self.assertEqual(name, "Leo")
        f2 = lifebase.add_fact(self.db, "relationships", "Leo 在伦敦", [self.ev], subject="person:3")
        self.assertEqual(self.stmt(f2)["subject_id"], s["subject_id"])  # 同一张人物卡同一个对象

    def test_migrate_old_facts(self):
        # 第 2 步之前的老数据：没有对照
        self.db.conn.execute("DROP TRIGGER facts_mirror_ins_v4")
        a = lifebase.add_fact(self.db, "rhythms", "周末 11 点起", [self.ev])
        b = lifebase.add_fact(self.db, "rhythms", "周末 9 点起", [self.ev])
        lifebase.supersede(self.db, a, b, when="2026-11-02")
        c = lifebase.add_fact(self.db, "values", "看重自由", [self.ev])
        self.db.conn.execute("UPDATE facts SET evidence_ids = '[]', locked = 1 WHERE id = ?", (c,))
        self.db.conn.execute("INSERT INTO people (id, name, updated_at) VALUES (5, 'Mia', 'x')")
        self.db.conn.execute("INSERT INTO facts (dimension, subject, statement, recorded_at, evidence_ids, status, visibility) "
                             "VALUES ('relationships', 'person:5', 'Mia 是室友', '2026-10-01', '[]', 'active', 'private')")
        self.db.conn.commit()
        lifebase.ensure(self.db)  # 每次都会跑的老数据补来源类型，不算迁移改的
        self.db.conn.commit()
        before = [tuple(r) for r in self.db.conn.execute("SELECT * FROM facts")]
        got = lifebase.migrate_old_facts(self.db)
        self.assertEqual((got["migrated"], got["source_unknown"], got["people"]), (4, 2, 1))
        self.assertEqual([tuple(r) for r in self.db.conn.execute("SELECT * FROM facts")], before)  # 旧的一点没动
        self.assertEqual(self.stmt(a)["superseded_by"], self.stmt(b)["id"])
        self.assertEqual(self.db.conn.execute("SELECT source_unknown FROM statement_sources WHERE statement_id = ?",
                                              (self.stmt(c)["id"],)).fetchone()[0], 1)  # 找不到出处标未知，不补
        self.assertEqual(lifebase.reconcile_statements(self.db), [])
        self.assertEqual(lifebase.migrate_old_facts(self.db)["migrated"], 0)  # 重复跑不重复迁

    def test_reconcile_reports_drift(self):
        f = lifebase.add_fact(self.db, "rhythms", "周末睡懒觉", [self.ev])
        self.db.conn.execute("UPDATE statements SET value_text = '被改了' WHERE id = ?", (self.stmt(f)["id"],))
        problems = lifebase.reconcile_statements(self.db)
        self.assertEqual(len(problems), 1)
        self.assertIn("内容", problems[0])


if __name__ == "__main__":
    unittest.main()
