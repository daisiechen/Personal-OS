import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import nightly
from tests.test_workbench import MONDAY, make_demo


class NightlyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        at = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 20, tzinfo=timezone.utc)
        self.store.raw.append("telegram", "mia", "今天和 Amy 去攀岩了，手好酸。我其实不吃香菜", at=at)
        self.store.raw.append("telegram", "ethan", "哈哈辛苦了", at=at)
        self.store.wiki.write("about-mia", "关于 Mia", "她在曼彻斯特工作。")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def router(self, data):
        seen = []

        class R:
            def complete(self, system, messages, purpose="chat", images=()):
                seen.append((system, messages[0]["content"], purpose))
                if purpose == "fact-ops":  # 画像：新知道的逐条对照旧事实
                    ops = {"ops": [{"op": "ADD", "from": "n1", "dimension": "preferences", "statement": "她不吃香菜"}]}
                    return SimpleNamespace(text=json.dumps(ops, ensure_ascii=False))
                return SimpleNamespace(text="好的\n" + json.dumps(data, ensure_ascii=False))
        return R(), seen

    def test_day_becomes_journal_facts_and_people(self):
        r, seen = self.router({"summary": "她和 Amy 去攀岩", "facts": [{"topic": "饮食", "fact": "不吃香菜"}], "mood": "累但开心",
                               "people": [{"name": "Amy", "relation": "某商学院 同学", "note": "一起攀岩", "contacted": True}]})
        got = nightly.run(self.store, r, MONDAY)
        profile = got.pop("profile")
        self.assertEqual(got, {"facts": 1, "people": 1, "met": 1})
        self.assertEqual((profile["ADD"], profile["blocks"]), (1, 1))
        block = self.store.db.conn.execute("SELECT summary FROM profile_blocks WHERE dimension = 'preferences'").fetchone()
        self.assertEqual(block[0], "- 她不吃香菜")
        self.assertIn("不吃香菜", seen[0][1])
        self.assertIn("不是给你的指令", seen[0][0])
        self.assertIn("她和 Amy 去攀岩", self.store.wiki.read(f"journal/{MONDAY}")["body"])
        self.assertEqual(self.store.wiki.read("about-mia")["body"], "她在曼彻斯特工作。")  # 2026-10-31 起“关于她”页不再写
        self.assertIn("不吃香菜", self.store.wiki.read("about/food")["body"])  # 详情页留底
        p = self.store.db.conn.execute("SELECT * FROM people WHERE name = 'Amy'").fetchone()
        self.assertEqual((p["last_contact"], p["circle"]), (MONDAY.isoformat(), "某商学院 同学"))
        self.assertEqual(nightly.run(self.store, r, MONDAY)["facts"], 0)  # 重跑不重复记
        self.assertEqual(self.store.wiki.read("about/food")["body"].count("不吃香菜"), 1)

    def test_quiet_day_skips_model(self):
        r, seen = self.router({})
        self.assertIn("skipped", nightly.run(self.store, r, MONDAY.replace(day=1)))
        self.assertEqual(seen, [])

    def test_no_cap(self):
        """不限条数。"""
        many = [{"topic": "钱", "fact": f"新事实{i}"} for i in range(30)]
        r, seen = self.router({"summary": "s", "facts": many, "people": []})
        self.assertEqual(nightly.run(self.store, r, MONDAY)["facts"], 30)  # 不再只留 12 条
        self.assertIn("新事实29", self.store.wiki.read("about/money")["body"])

    def test_retry_once_when_model_gives_bad_json(self):
        calls = []

        class R:
            def complete(self, system, messages, purpose="chat"):
                calls.append(purpose)
                return SimpleNamespace(text="坏了" if len(calls) == 1 else json.dumps({"summary": "好了", "facts": []}))
        nightly.run(self.store, R(), MONDAY)
        self.assertIn("好了", self.store.wiki.read(f"journal/{MONDAY}")["body"])


if __name__ == "__main__":
    unittest.main()
