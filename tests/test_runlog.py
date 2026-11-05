"""系统输出和她的反应（ontology-plan 1.6）：汇进一张表能查；攒够次数才写成行为说法；收藏 + 聊过才是“可能感兴趣”的猜测。"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, onto_query, prep, runlog
from ethan.memory import MemoryStore

DAY = date(2026, 11, 5)


class RunLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        prep.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_collect_query_and_behavior_after_three(self):
        ids = [self.db.add_task(f"替她查 {i}", promised_by_ethan=True) for i in range(3)]
        prep._set(self.db, ids[0], state="approved")
        prep._set(self.db, ids[1], state="revising", feedback="要更短")
        self.db.conn.execute("UPDATE prep_work SET updated_at = '2026-11-04T10:00:00+00:00'")
        runlog.collect(self.db)
        got = onto_query.system_did(self.db, "ethan")
        self.assertEqual(got["status"], "answer")
        self.assertIn("要改", [x["reaction"] for x in got["items"]])
        self.assertEqual(runlog.behavior_items(self.db, DAY), [])  # 才两次：不写成习惯
        prep._set(self.db, ids[2], state="approved")
        self.db.conn.execute("UPDATE prep_work SET updated_at = '2026-11-04T11:00:00+00:00'")
        runlog.collect(self.db)
        items = runlog.behavior_items(self.db, DAY)
        self.assertEqual(items[0]["key"], "reaction_prep")
        self.assertIn("通过 / 用了 2 次", items[0]["value"])
        self.assertEqual(lifebase.write_behavior(self.db, DAY)["written"], 1)
        f = self.db.conn.execute("SELECT source_kind, statement FROM facts WHERE prop = 'reaction_prep'").fetchone()
        self.assertEqual(f["source_kind"], "behavior")

    def test_interest_needs_saves_and_chat(self):
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS brain_items (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL UNIQUE, "
                             "title TEXT NOT NULL, url TEXT, source TEXT, summary TEXT, note TEXT, tags TEXT NOT NULL DEFAULT '[]', territory TEXT, "
                             "status TEXT NOT NULL DEFAULT 'new', extra TEXT NOT NULL DEFAULT '{}', happened_at TEXT, created_at TEXT NOT NULL)")
        for i in range(3):
            self.db.conn.execute("INSERT INTO brain_items (kind, ref, title, tags, created_at) VALUES ('saved', ?, 't', ?, '2026-11-01')",
                                 (f"r{i}", json.dumps(["咖啡", "露营"] if i else ["咖啡"], ensure_ascii=False)))
        self.assertEqual(lifebase.write_interests(self.db, DAY)["written"], 0)  # 只收藏、没聊过：不算
        lifebase.add_evidence(self.db, "chat", "raw:x:1", "2026-11-02T09:00:00+00:00", text="mia：今天又去试了一家咖啡")
        self.assertEqual(lifebase.write_interests(self.db, DAY)["written"], 1)
        f = self.db.conn.execute("SELECT kind, source_kind, statement FROM facts WHERE prop = 'interest'").fetchone()
        self.assertEqual((f["kind"], f["source_kind"]), ("hypothesis", "system_inference"))
        self.assertIn("「咖啡」", f["statement"])
        self.assertEqual(lifebase.write_interests(self.db, DAY)["written"], 0)  # 不重复记


if __name__ == "__main__":
    unittest.main()
