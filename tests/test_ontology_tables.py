"""ontology 迁移第 1 步（docs/design/ontology-structure.md 9.1 节）：只建表和登记元数据，约束要真的挡得住，不碰旧数据。"""

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ethan import lifebase, ontology
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent


class OntologyTablesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def stmt(self, **kw):
        base = {"subject_id": ontology.self_id(self.db), "property": "wake_time", "kind": "preference", "said_by": "her_words",
                "recorded_at": "2026-11-03"}
        base.update(kw)
        cols = ",".join(base)
        return self.db.conn.execute(f"INSERT INTO statements ({cols}) VALUES ({','.join('?' * len(base))})", list(base.values()))

    def test_ensure_is_idempotent_and_registers_metadata(self):
        again = ontology.ensure(self.db)  # lifebase.ensure 已经建过一次
        self.assertEqual(again, {"properties": 0, "self": False})
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM properties").fetchone()[0],
                         len(ontology.PROPERTIES) + len(lifebase.DIMENSIONS))  # 首版属性 + 每个方面一个“总的”
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM object_types WHERE type = 'self'").fetchone()[0], 1)
        self.assertEqual(len(ontology.OBJECT_TYPES), 10)

    def test_exactly_one_value_or_no_value(self):
        ontology.ensure(self.db)
        self.stmt(value_text="工作日 9 点", context="工作日")
        self.stmt(value_text="周末 11 点", context="周末")  # 同一属性、不同情境，可以同时成立
        self.stmt(no_value="unknown")
        with self.assertRaises(sqlite3.IntegrityError):
            self.stmt()  # 什么值都没有
        with self.assertRaises(sqlite3.IntegrityError):
            self.stmt(value_text="9 点", no_value="unknown")  # 又有值又说“不知道”
        with self.assertRaises(sqlite3.IntegrityError):
            self.stmt(value_text="9 点", review="首选")  # 审核状态只能是那五种

    def test_object_can_have_several_types_and_sources_need_origin(self):
        ontology.ensure(self.db)
        lbs = self.db.conn.execute("INSERT INTO objects (name, created_at) VALUES ('某商学院', 'x')").lastrowid
        self.db.conn.execute("INSERT INTO object_types VALUES (?, 'org')", (lbs,))
        self.db.conn.execute("INSERT INTO object_types VALUES (?, 'place')", (lbs,))
        s = self.stmt(value_text="9 点").lastrowid
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("INSERT INTO statement_sources (statement_id, role) VALUES (?, 'supports')", (s,))  # 没出处又没标未知
        self.db.conn.execute("INSERT INTO statement_sources (statement_id, role, source_unknown) VALUES (?, 'supports', 1)", (s,))
        self.db.conn.execute("INSERT INTO action_log (kind, target, basis, action_key, actor, at, result) "
                             "VALUES ('改日程', '训练', 'v3', '改日程|训练|v3', 'mia', 'x', 'done')")
        with self.assertRaises(sqlite3.IntegrityError):  # 同一编号不能提交两次
            self.db.conn.execute("INSERT INTO action_log (kind, target, basis, action_key, actor, at, result) "
                                 "VALUES ('改日程', '训练', 'v3', '改日程|训练|v3', 'planner', 'x', 'done')")

    def test_migration_does_not_touch_old_facts(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：周末睡懒觉")
        lifebase.add_fact(self.db, "rhythms", "周末睡懒觉", [ev])
        self.db.conn.commit()
        before = [tuple(r) for r in self.db.conn.execute("SELECT * FROM facts")]
        stmts = self.db.conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0]  # 第 2 步双写已经同步了新建的这条
        spec = importlib.util.spec_from_file_location("m", ROOT / "migrations" / "20261103_ontology_tables.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn("新表已建", mod.run(self.store))
        self.assertEqual([tuple(r) for r in self.db.conn.execute("SELECT * FROM facts")], before)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM statements").fetchone()[0], stmts)  # 迁移脚本本身不迁旧记录


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
