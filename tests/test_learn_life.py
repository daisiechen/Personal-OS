"""11-04 冷启动收尾：英语不排时间、给 Grok 的陪练规矩写全；学习、休闲标成完成（迁移）。"""

import importlib
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import domains, english
from ethan.memory import MemoryStore


class EnglishRulesTest(unittest.TestCase):
    def test_every_page_has_practice_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pages = english.vault_pages(root, date(2026, 11, 4))  # 没备课
            page = pages["英语课/今天.md"]
            for word in ("不排固定时间", "不要催", "中途不打断纠错", "结束", "练完回报", english.MARK):
                self.assertIn(word, page)
            folder = english.lesson_dir(root)
            folder.mkdir()
            (folder / "2026-11-03.md").write_text("# 英语课 2026-11-03\n\n场景……\n\n" + english.REPORT_FORMAT, encoding="utf-8")
            self.assertIn("2026-11-03.md", english.vault_pages(root, date(2026, 11, 4))["英语课/今天.md"])  # 先用最近一节
            (folder / "2026-11-04.md").write_text("# 英语课 2026-11-04\n\n场景……\n\n" + english.REPORT_FORMAT, encoding="utf-8")
            page = english.vault_pages(root, date(2026, 11, 4))["英语课/今天.md"]  # 11-04 前备的课：补上规矩，放在回报格式前
            self.assertLess(page.index("## 陪练规矩"), page.index("## 练完回报"))


class MigrationTest(unittest.TestCase):
    def test_marks_learning_and_leisure_done_not_social(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            try:
                m = importlib.import_module("migrations.20261104_cold_start_learn_life")
                src = Path(m.__file__).read_text(encoding="utf-8")
                self.assertIn("原话", src)
                self.assertIn("我有空了就去找grok聊就好了", src)
                self.assertIn("想试的运动就舞蹈吧", src)
                m.run(store)
                rows = {r["module"]: dict(r) for r in store.db.conn.execute("SELECT * FROM module_status")}
                self.assertEqual(rows["学习"]["cold_start"], "已完成")
                self.assertEqual(rows["休闲"]["cold_start"], "已完成")
                self.assertIn("不排固定时间", rows["学习"]["note"])
                self.assertIn("舞蹈", rows["休闲"]["note"])
                self.assertNotIn("社交", rows)
                lines = domains.domain_lines(store.db, date(2026, 11, 4))
                self.assertTrue(any(l.startswith("生活体验") and "每周五" in l for l in lines))
            finally:
                store.close()


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
