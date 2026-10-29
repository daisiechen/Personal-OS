import json
import tempfile
import unittest
from pathlib import Path

from ethan.memory import MemoryStore
from ethan.seed import import_seed


class SeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.seed = root / "seed"
        (self.seed / "wiki" / "modules").mkdir(parents=True)
        (self.seed / "wiki" / "about-mia.md").write_text("# 关于 Mia\n\n处女座，INFJ\n", encoding="utf-8")
        (self.seed / "wiki" / "modules" / "health.md").write_text("# 健康总览\n\n先矫正后增肌\n", encoding="utf-8")
        (self.seed / "seed.json").write_text(json.dumps({
            "goals": [
                {"key": "v", "title": "这辈子干一件大事", "level": "vision", "status": "active"},
                {"key": "q", "title": "创业项目跑出订单", "level": "stage", "parent": "v",
                 "status": "active", "due": "2026-12-31", "domain": "career"},
            ],
            "people": [{"name": "小雨", "circle": "挚友", "notes": "帮忙照顾两只猫"}],
            "wishes": [{"quote": "想学网球", "context": "因为阿明常打"}],
            "learning": [{"subject": "english", "content": "right up my alley"}],
        }, ensure_ascii=False), encoding="utf-8")
        self.store = MemoryStore(root / "data")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_import_and_idempotent(self):
        counts = import_seed(self.seed, self.store)
        self.assertEqual(counts, {"wiki": 2, "goals": 2, "people": 1, "wishes": 1, "learning": 1})
        self.assertEqual(self.store.wiki.read("modules/health")["title"], "健康总览")
        self.assertEqual(self.store.wiki.read("about-mia")["body"], "处女座，INFJ")
        quarter = [g for g in self.store.db.active_goals() if g["title"] == "创业项目跑出订单"][0]
        self.assertIsNotNone(self.store.db.get_goal(quarter["id"])["parent_id"])
        again = import_seed(self.seed, self.store)
        self.assertEqual((again["goals"], again["wishes"], again["learning"]), (0, 0, 0))
        self.assertEqual(len(self.store.db.active_goals()), 2)


if __name__ == "__main__":
    unittest.main()
