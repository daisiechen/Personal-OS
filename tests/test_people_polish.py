"""人物卡“关系”“怎么认识的”过一遍话术（ethan/people_polish.py）。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import people_polish, relations
from ethan.memory import MemoryStore


class Fake:
    def __init__(self, out):
        self.out = out

    def complete(self, system, messages, purpose="chat"):
        assert purpose == "relations-polish"
        return SimpleNamespace(text=json.dumps(self.out, ensure_ascii=False))


class PolishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)
        self.a = relations.upsert(self.db, "杨骏")
        self.b = relations.upsert(self.db, "小林")
        self.db.conn.execute("UPDATE people SET relation = '中介,', how_met = '她发了好友验证请求，2026-05-11 他通过' WHERE id = ?", (self.a,))
        self.db.conn.execute("UPDATE people SET relation = '大学室友', how_met = NULL WHERE id = ?", (self.b,))
        self.db.conn.commit()
        relations.set_field(self.db, self.b, "relation", "大学室友")  # 她改过：不动

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_rewrite_dry_then_write(self):
        out = [{"id": self.a, "relation": "帮她办续签合同的中介", "how_met": ""},
               {"id": self.b, "relation": "大学室友，现在在上海", "how_met": "大学同宿舍"}]
        got = people_polish.polish(self.db, Fake(out), dry=True)
        self.assertEqual({(c["name"], c["field"]) for c in got["changes"]}, {("杨骏", "relation"), ("杨骏", "how_met")})
        row = lambda i: self.db.conn.execute("SELECT relation, how_met FROM people WHERE id = ?", (i,)).fetchone()
        self.assertEqual(row(self.a)["relation"], "中介,")  # dry 不写
        people_polish.polish(self.db, Fake(out))
        self.assertEqual(tuple(row(self.a)), ("帮她办续签合同的中介", None))
        self.assertEqual(tuple(row(self.b)), ("大学室友", None))  # 她改过的、原来空的都不动


if __name__ == "__main__":
    unittest.main()
