"""人物卡唯一建卡入口（修盘点 bug）：她删掉的人，聊天抽取、每晚整理、小伴 的 [人物] 标记、工作台记一笔
都不能把卡建回来；她改过（锁住）的字段，这些自动入口也不动。守卫：除 relations.upsert、数据库层和初始化种子外，
ethan/ 里别处直接调 upsert_person 测试不过。"""

import re
import tempfile
import unittest
from pathlib import Path

from ethan import extract, lifelog, nightly, relations
from tests.test_workbench import MONDAY, make_demo

ROOT = Path(__file__).resolve().parent.parent
EMPTY = {"todos": [], "wishes": [], "facts": [], "events": [], "remember": [], "people": []}


class PersonUpsertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)
        pid = relations.upsert(self.db, "小林", source="test", circle="朋友")
        self.assertTrue(relations.delete(self.db, pid)["ok"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def card(self, name="小林"):
        return self.db.conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()

    def test_extract_does_not_rebuild(self):
        got = extract.apply(self.db, {**EMPTY, "people": [{"name": "小林", "relation": "同学", "note": "在搬家", "quote": "x"}]},
                            None, "ref", MONDAY)
        self.assertIsNone(self.card())
        self.assertEqual(got["person"], 0)

    def test_nightly_does_not_rebuild(self):
        nightly.apply(self.store, MONDAY, {"summary": "s", "facts": [],
                                           "people": [{"name": "小林", "relation": "同学", "contacted": True}]})
        self.assertIsNone(self.card())

    def test_lifelog_marker_does_not_rebuild(self):
        lifelog.note(self.db, "好 [人物 小林 见面 一起吃饭]", MONDAY)
        self.assertIsNone(self.card())

    def test_new_people_still_get_cards(self):
        lifelog.note(self.db, "好 [人物 Tom 新认识 讲座上认识]", MONDAY)
        self.assertEqual(self.card("Tom")["circle"], "新朋友")

    def test_locked_field_is_kept(self):
        pid = relations.upsert(self.db, "Amy", source="test", circle="同学")
        relations.set_field(self.db, pid, "circle", "挚友")
        relations.set_field(self.db, pid, "notes", "她写的")
        extract.apply(self.db, {**EMPTY, "people": [{"name": "Amy", "relation": "同事", "note": "换工作了", "quote": "x"}]},
                      None, "ref", MONDAY)
        p = self.card("Amy")
        self.assertEqual((p["circle"], p["notes"]), ("挚友", "她写的"))

    def test_guard_only_one_entry(self):
        allowed = {"ethan/relations.py", "ethan/memory/db.py", "ethan/seed.py"}
        bad = []
        for f in (ROOT / "ethan").rglob("*.py"):
            rel = f.relative_to(ROOT).as_posix()
            if rel not in allowed and re.search(r"\bupsert_person\(", f.read_text(encoding="utf-8")):
                bad.append(rel)
        self.assertEqual(bad, [], "建人物卡只走 relations.upsert（会查她删过的锁和改过的字段）")


if __name__ == "__main__":
    unittest.main()
