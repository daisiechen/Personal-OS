"""人物卡整表导出、填好导回（ethan/people_sheet.py）。"""

import tempfile
import unittest
from pathlib import Path

from ethan import people_sheet, relations
from ethan.memory import MemoryStore


class PeopleSheetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)
        self.a = relations.upsert(self.db, "小林", circle="朋友")
        self.b = relations.upsert(self.db, "Lili")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def row(self, pid):
        return dict(self.db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone())

    def test_only_changed_cells_written(self):
        got = {r["id"]: r for r in people_sheet.rows(self.db)}
        self.assertEqual((got[self.a]["importance"], got[self.a]["every"], got[self.a]["circle"]), ("☆☆☆", "默认", "朋友"))
        self.db.conn.execute("UPDATE people SET recent = '刚换了工作' WHERE id = ?", (self.a,))  # 导出以后 小伴 新整理出来的
        self.db.conn.commit()
        changed = [{"id": self.a, "key": "importance", "old": "☆☆☆", "new": "★★☆"},
                   {"id": self.a, "key": "every", "old": "默认", "new": "永不"},
                   {"id": self.a, "key": "recent", "old": "", "new": ""},  # 没改：不碰，不把新整理的盖掉
                   {"id": self.b, "key": "circle", "old": "", "new": "服务"},
                   {"id": self.b, "key": "how_met", "old": "", "new": "订机票找的代理"},
                   {"id": self.b, "key": "every", "old": "默认", "new": "45"},
                   {"id": self.b, "key": "n365", "old": "0", "new": "99"}]  # 灰色列：不收
        r = people_sheet.apply(self.db, changed)
        self.assertEqual((r["written"], r["people"], r["skipped"]), (5, 2, []))
        a, b = self.row(self.a), self.row(self.b)
        self.assertEqual((a["importance"], a["contact_every_days"], a["recent"]), (2, relations.NEVER, "刚换了工作"))
        self.assertEqual((b["circle"], b["how_met"], b["contact_every_days"]), ("服务", "订机票找的代理", 45))
        self.assertEqual(relations.her_edit(self.db, self.b, "how_met")[0], "订机票找的代理")  # 和她在卡上改一样记下来，整理时当参考

    def test_bad_values_reported_not_written(self):
        r = people_sheet.apply(self.db, [{"id": self.a, "name": "小林", "key": "importance", "old": "☆☆☆", "new": "很重要"},
                                         {"id": self.a, "name": "小林", "key": "circle", "old": "朋友", "new": "邻居"},
                                         {"id": 99999, "name": "没了", "key": "notes", "old": "", "new": "x"}])
        self.assertEqual(r["written"], 0)
        self.assertEqual(len(r["skipped"]), 3)
        self.assertEqual(self.row(self.a)["circle"], "朋友")

    def test_dry_run_writes_nothing(self):
        r = people_sheet.apply(self.db, [{"id": self.a, "key": "notes", "old": "", "new": "爱喝咖啡"}], dry=True)
        self.assertEqual(r["written"], 1)
        self.assertIsNone(self.row(self.a)["notes"])


if __name__ == "__main__":
    unittest.main()
