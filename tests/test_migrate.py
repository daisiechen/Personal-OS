import tempfile
import unittest
from pathlib import Path

from ethan import migrate
from ethan.memory import MemoryStore


class MigrateTest(unittest.TestCase):
    def test_runs_each_once_and_rolls_back_failures(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repo" / "migrations").mkdir(parents=True)
            (root / "repo" / "migrations" / "20261029_a.py").write_text(
                "def run(store):\n    store.db.add_task('新的一条')\n    return '加了一条'\n", encoding="utf-8")
            (root / "repo" / "migrations" / "20261029_b.py").write_text(
                "def run(store):\n    store.db.conn.execute(\"UPDATE tasks SET title = '改坏了'\")\n    raise RuntimeError('出错')\n", encoding="utf-8")
            store = MemoryStore(root / "data")
            out = migrate.run_all(store, root / "repo", root / "data")
            self.assertEqual([(n, ok) for n, ok, _ in out], [("20261029_a", True), ("20261029_b", False)])
            self.assertEqual([r["title"] for r in store.db.conn.execute("SELECT title FROM tasks")], ["新的一条"])  # b 回滚了
            self.assertEqual(migrate.run_all(store, root / "repo", root / "data"), [])  # 跑过的不再跑
            store.close()

    def test_each_recorded_right_away_so_an_interrupt_does_not_rerun(self):
        """跑完一个记一个：后面的把进程打断了，前面跑过的下次不再跑（部署超时）。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repo" / "migrations").mkdir(parents=True)
            (root / "repo" / "migrations" / "20261029_a.py").write_text("def run(store):\n    return 'a'\n", encoding="utf-8")
            (root / "repo" / "migrations" / "20261029_b.py").write_text("def run(store):\n    raise KeyboardInterrupt\n", encoding="utf-8")
            store = MemoryStore(root / "data")
            with self.assertRaises(KeyboardInterrupt):
                migrate.run_all(store, root / "repo", root / "data")
            self.assertEqual([p.stem for p in migrate.pending(root / "repo", root / "data")], ["20261029_b"])
            store.close()

    def test_killed_after_script_commit_does_not_rerun(self):
        """Codex 评审 11-05：脚本自己 commit 了数据、还没记“跑过”就被杀掉，下次不能再跑一遍。"""
        import os
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repo" / "migrations").mkdir(parents=True)
            (root / "repo" / "migrations" / "20261029_a.py").write_text(
                "import os\ndef run(store):\n    store.db.add_task('只加一次')\n    store.db.conn.commit()\n    if os.environ.get('KILL_ME'):\n        os._exit(9)\n",
                encoding="utf-8")
            MemoryStore(root / "data").close()
            code = ("import sys; from pathlib import Path; from ethan import migrate; from ethan.memory import MemoryStore; "
                    "r = Path(sys.argv[1]); migrate.run_all(MemoryStore(r / 'data'), r / 'repo', r / 'data')")
            repo = Path(__file__).resolve().parent.parent
            self.assertEqual(subprocess.run([sys.executable, "-c", code, str(root)], cwd=str(repo), env={**os.environ, "KILL_ME": "1"}).returncode, 9)
            store = MemoryStore(root / "data")
            self.assertEqual(migrate.run_all(store, root / "repo", root / "data"), [])  # 不再跑
            self.assertEqual(store.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE title = '只加一次'").fetchone()[0], 1)
            self.assertFalse(migrate.load(root / "data", store.db)["20261029_a"]["ok"])  # 记成“跑到一半”
            store.close()

    def test_cli_exits_nonzero_when_a_migration_fails(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repo" / "migrations").mkdir(parents=True)
            (root / "repo" / "migrations" / "20261029_a.py").write_text("def run(store):\n    raise KeyError('x')\n", encoding="utf-8")
            repo = Path(__file__).resolve().parent.parent
            p = subprocess.run([sys.executable, "-m", "ethan.migrate", "--data", str(root / "data"), "--repo", str(root / "repo")],
                               cwd=str(repo), capture_output=True, text=True)
            self.assertEqual(p.returncode, 1)
            self.assertIn("20261029_a：失败", p.stdout)
            self.assertIn("出错的：20261029_a", p.stderr)

    def test_old_json_records_imported(self):
        import json
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "repo" / "migrations").mkdir(parents=True)
            (root / "repo" / "migrations" / "20261029_a.py").write_text("def run(store):\n    raise RuntimeError('不该再跑')\n", encoding="utf-8")
            (root / "repo" / "migrations" / "20261029_b.py").write_text("def run(store):\n    return 'b'\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "migrations.json").write_text(json.dumps({"20261029_a": {"at": "2026-10-29T00:00:00+00:00", "ok": True, "note": "老的"}}),
                                                         encoding="utf-8")
            store = MemoryStore(root / "data")
            self.assertEqual([(n, ok) for n, ok, _ in migrate.run_all(store, root / "repo", root / "data")], [("20261029_b", True)])
            rows = dict(store.db.conn.execute("SELECT name, note FROM migrations_done").fetchall())
            self.assertEqual(rows, {"20261029_a": "老的", "20261029_b": "b"})
            (root / "data" / "migrations.json").unlink()  # 副本丢了也不重跑：以表为准
            self.assertEqual(migrate.pending(root / "repo", root / "data"), [])
            store.close()

    def test_deploy_step_timeout_is_a_failed_step(self):
        import subprocess
        from unittest import mock
        from ethan import autodeploy
        with mock.patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 900)):
            with self.assertRaises(autodeploy.StepFailed):
                autodeploy.shell(["python3", "-m", "ethan.migrate"])

    def test_repo_migrations_are_well_formed(self):
        repo = Path(__file__).resolve().parent.parent
        for p in sorted((repo / "migrations").glob("[0-9]*.py")):
            text = p.read_text(encoding="utf-8")
            self.assertIn("def run(store)", text, p.name)
            self.assertIn("原话", text, p.name)  # 每个都写上她同意的原话


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
