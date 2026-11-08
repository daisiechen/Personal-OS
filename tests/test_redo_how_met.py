"""“怎么认识的”写成加好友系统提示的卡，按新规则重新整理。"""

import importlib.util
import tempfile
import unittest
from pathlib import Path

from ethan import relations
from ethan.llm import load_prompt
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent


class RedoHowMetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_verify_only_cards_redone_hers_kept(self):
        ids = {}
        for name, how in (("杨骏", "她发了好友验证请求，2026-05-11 他通过"), ("小海", "为办西班牙签证找的中介"), ("自己改的", "通过好友验证")):
            ids[name] = relations.upsert(self.db, name)
            self.db.conn.execute("UPDATE people SET how_met = ?, organized_at = '2026-11-05' WHERE id = ?", (how, ids[name]))
        self.db.conn.commit()
        relations.set_field(self.db, ids["自己改的"], "how_met", "通过好友验证")
        spec = importlib.util.spec_from_file_location("m", ROOT / "migrations/20261107_redo_how_met.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn("1 张", mod.run(self.store))
        got = {n: self.db.conn.execute("SELECT organized_at FROM people WHERE id = ?", (i,)).fetchone()[0] for n, i in ids.items()}
        self.assertIsNone(got["杨骏"])
        self.assertIsNotNone(got["小海"])
        self.assertIsNotNone(got["自己改的"])

    def test_circle_renamed_to_service(self):
        """→“可以，改成服务吧”。"""
        pid = relations.upsert(self.db, "杨骏")
        self.db.conn.execute("UPDATE people SET circle = '中介' WHERE id = ?", (pid,))
        self.db.conn.commit()
        from ethan import locks
        locks.lock(self.db, "person", pid, "circle", "中介")
        spec = importlib.util.spec_from_file_location("m2", ROOT / "migrations/20261107_circle_service.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.run(self.store)
        self.assertEqual(self.db.conn.execute("SELECT circle FROM people WHERE id = ?", (pid,)).fetchone()[0], "服务")
        self.assertEqual(relations.her_edit(self.db, pid, "circle")[0], "服务")

    def test_prompt_says_not_the_friend_request(self):
        _, text = load_prompt("person_card")
        self.assertIn("加好友的手续", text)


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
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
