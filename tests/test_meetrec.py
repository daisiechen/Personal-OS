"""推荐见谁（ethan/meetrec.py）。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import meetrec, relations
from ethan.memory import MemoryStore

TODAY = date(2026, 11, 7)


class MeetRecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)
        self.lai = self.person("赖总", city="珠海", roles=["投资人"], importance=2, last="2026-06-01")
        self.zhou = self.person("周周", city="深圳", roles=["创业者"], importance=3, last="2026-11-01")
        self.amy = self.person("Amy", city="珠海", roles=[], importance=1, last="2026-10-01")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def person(self, name, city, roles, importance, last):
        pid = relations.upsert(self.db, name)
        self.db.conn.execute("UPDATE people SET city = ?, roles = ?, importance = ?, last_contact = ? WHERE id = ?",
                             (city, json.dumps(roles, ensure_ascii=False), importance, last, pid))
        self.db.conn.commit()
        return pid

    def test_trip_to_zhuhai_recommends_lai(self):
        """待办里写了要去珠海：一定推荐住在珠海的人，赖总排在前（更重要、更久没联系）。"""
        self.db.add_task("下周去珠海见客户", due="2026-11-12")
        got = meetrec.suggest(self.db, TODAY)
        self.assertIn("珠海", got[0]["why"])
        self.assertEqual([p["name"] for p in got[0]["people"]], ["赖总", "Amy"])

    def test_bp_done_recommends_investors(self):
        """做完 BP：按规则表找投资人、创业者。"""
        tid = self.db.add_task("写完 BP")
        self.db.conn.execute("UPDATE tasks SET done = 1, done_at = '2026-11-06T10:00:00+00:00' WHERE id = ?", (tid,))
        self.db.conn.commit()
        got = meetrec.suggest(self.db, TODAY)
        names = {p["name"] for s in got for p in s["people"]}
        self.assertEqual(names, {"赖总", "周周"})
        self.assertIn("投资人", got[0]["why"])

    def test_casual_loops_go_to_want_meet_and_boost(self):
        """随口约的“下次一起吃饭”不进待履约，记成想见，推荐时加分；具体答应的事照旧进待履约。"""
        row = self.db.conn.execute("SELECT * FROM people WHERE id = ?", (self.amy,)).fetchone()
        data = {"relation": "", "topics": [], "recent": "", "dates": [], "draft": "",
                "open_loops": [{"kind": "she_promised", "date": "2026-10-01", "text": "下次回珠海一起吃饭", "quote": "下次回珠海一起吃饭"},
                               {"kind": "she_promised", "date": "2026-10-02", "text": "把简历发给她", "quote": "我晚点发你简历"}]}
        relations._apply_card(self.db, row, data, TODAY)
        p = dict(self.db.conn.execute("SELECT open_loops, want_meet FROM people WHERE id = ?", (self.amy,)).fetchone())
        self.assertEqual([x["text"] for x in json.loads(p["open_loops"])], ["把简历发给她"])
        self.assertEqual([x["text"] for x in json.loads(p["want_meet"])], ["下次回珠海一起吃饭"])
        self.assertEqual([x["name"] for x in relations.due(self.db, TODAY)["loops"]], ["Amy"])  # 只剩具体的那件
        ranked = meetrec.pick(self.db, TODAY, city="珠海")
        self.assertIn("说过", ranked[1]["why"] if ranked[0]["name"] == "赖总" else ranked[0]["why"])

    def test_nothing_known_nothing_suggested(self):
        """城市、身份都没填、也没行程：不瞎推荐。"""
        self.db.conn.execute("UPDATE people SET city = NULL, roles = NULL")
        self.db.conn.commit()
        self.assertEqual(meetrec.suggest(self.db, TODAY), [])

    def test_city_and_roles_editable(self):
        self.assertTrue(relations.set_field(self.db, self.amy, "roles", "设计师，学姐")["ok"])
        self.assertEqual(json.loads(self.db.conn.execute("SELECT roles FROM people WHERE id = ?", (self.amy,)).fetchone()[0]), ["设计师", "学姐"])
        self.assertTrue(relations.set_field(self.db, self.amy, "city", "伦敦")["ok"])


if __name__ == "__main__":
    unittest.main()
