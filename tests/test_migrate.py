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

    def test_repo_migrations_are_well_formed(self):
        repo = Path(__file__).resolve().parent.parent
        for p in sorted((repo / "migrations").glob("[0-9]*.py")):
            text = p.read_text(encoding="utf-8")
            self.assertIn("def run(store)", text, p.name)
            self.assertIn("原话", text, p.name)  # 每个都写上她同意的原话


if __name__ == "__main__":
    unittest.main()
