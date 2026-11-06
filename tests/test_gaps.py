"""小伴 主动补缺口（ontology-plan 1.7）：先查再问、空着的方面会被挑到、三天内不重复、她说“今天别问”就停、答了算她的原话。"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import lifebase, memsearch, mepage, onto_query
from ethan.memory import MemoryStore

DAY = date(2026, 11, 5)


class GapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        mepage.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def gap_items(self):
        return [p for p in mepage.chat_items(self.db, n=10)
                if self.db.conn.execute("SELECT origin FROM pending WHERE id = ?", (p["id"],)).fetchone()[0] == "gap"]

    def test_thin_dimensions_all_asked_once_a_day(self):
        got = mepage.pick_gaps(self.db, DAY)
        self.assertGreaterEqual(len(got["asked"]), 10)  # 不设上限：空着的方面都挑
        self.assertTrue(any("最不能让步" in q for q in got["asked"]))  # 价值观带一串接着问的
        self.assertEqual(mepage.pick_gaps(self.db, DAY)["skipped"], "今天挑过了")
        self.assertEqual(len(mepage.pick_gaps(self.db, DAY + timedelta(days=1))["asked"]), 0)  # 一周内挑过的不再挑
        self.assertEqual(len(mepage.pick_gaps(self.db, DAY + timedelta(days=2), n=2)["asked"]), 0)
        self.assertEqual(mepage.pick_gaps(self.db, DAY + timedelta(days=3), n=2)["skipped"], "上次的题她还没答完")

    def test_unanswerable_capability_question_comes_first(self):
        onto_query.whereabouts_now(self.db, "ethan", today=DAY)  # 答不上，记一笔 unknown
        got = mepage.pick_gaps(self.db, DAY)
        self.assertIn("住在哪个城市", got["asked"][0])

    def test_look_before_asking(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:1", "2026-11-01T10:00:00+00:00",
                                   text="mia：我做大决定之前一般会纠结很久，最后还是自己选")
        memsearch.refresh(self.db, self.store.root)
        got = mepage.pick_gaps(self.db, DAY, n=10)
        self.assertIn("dim:thinking", got["has_records"])  # 聊天里提过：记一笔
        self.assertTrue(any("做大决定" in q for q in got["asked"]))  # 但这方面整理出来的还很薄，照样问（11-04 第一次跑一题都没问）

    def test_asked_not_repeated_within_three_days_and_stop_word(self):
        mepage.pick_gaps(self.db, DAY)
        pid = self.gap_items()[0]["id"]
        mepage.note_chat(self.db, f"随口问一句 [待定 #{pid} 问了]")
        self.assertNotIn(pid, [p["id"] for p in self.gap_items()])
        self.assertTrue(mepage.gap_note_her(self.db, "今天别问我问题了，好累", DAY))
        self.assertTrue(mepage.gap_paused(self.db, DAY))
        self.assertEqual(mepage.pick_gaps(self.db, DAY)["skipped"], "她说今天别问")
        self.assertFalse(mepage.gap_note_her(self.db, "今天好累", DAY))

    def test_answer_is_her_words_with_question_as_context(self):
        mepage.pick_gaps(self.db, DAY)
        item = self.gap_items()[0]
        mepage.note_chat(self.db, f"[待定 #{item['id']} 写 我一般七点起，周末睡到九点]")
        f = self.db.conn.execute("SELECT * FROM facts WHERE statement LIKE '%七点起%'").fetchone()
        self.assertEqual(f["source_kind"], "her_words")
        self.assertIn(item["question"], f["context"])
        week = mepage.gap_week(self.db)
        self.assertGreaterEqual(week["answered"], 1)


if __name__ == "__main__":
    unittest.main()


class PendingCleanTest(unittest.TestCase):
    """"""

    def test_one_off_and_duplicates_not_asked_and_gaps_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            db = store.db
            lifebase.ensure(db)
            mepage.ensure(db)
            gr = lifebase.add_evidence(db, "grok", "g:1", "2026-10-31", text="x")
            a = lifebase.add_fact(db, "rhythms", "2026-10-30 早上约 8:43 才睡醒", [gr], kind="hypothesis", source_kind="agent_relay")
            b = lifebase.add_fact(db, "rhythms", "工作日一般 9 点前起床", [gr], kind="hypothesis", source_kind="agent_relay")
            c = lifebase.add_fact(db, "rhythms", "工作日一般九点之前起床", [gr], kind="hypothesis", source_kind="agent_relay")
            from unittest import mock
            with mock.patch.object(mepage, "_uses", lambda db, fid, days=30: (5, ["ethan"])):
                mepage.refresh(db, DAY)
            asked = [json.loads(r[0])[0] for r in db.conn.execute("SELECT fact_ids FROM pending WHERE kind = 'confirm'")]
            self.assertNotIn(a, asked)  # 某一天的事不当长期去确认
            self.assertEqual(len([x for x in asked if x in (b, c)]), 1)  # 同一件事只问一次
            mepage.pick_gaps(db, DAY)
            self.assertEqual(mepage.pending_count(db), 1)  # 补缺口的问题不算“待你定”
            store.close()
