import json
import tempfile
import unittest
from pathlib import Path

from ethan import lifebase
from ethan.memory import MemoryStore


class LifebaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ev(self, ref="raw:2026-10-30:0"):
        return lifebase.add_evidence(self.db, "chat", ref, "2026-10-30T10:00:00+00:00", "mia：我开始跑步了")

    def test_ensure_adds_columns_rules_and_backfills_owner(self):
        self.db.conn.execute("INSERT INTO tasks (title, done, promised_by_ethan, created_at) VALUES ('查资料', 0, 1, '2026-10-30')")
        self.db.conn.execute("INSERT INTO tasks (title, done, promised_by_ethan, created_at) VALUES ('交表', 1, 0, '2026-10-30')")
        lifebase.ensure(self.db)
        lifebase.ensure(self.db)  # 可重复
        rows = {r["title"]: r for r in self.db.conn.execute("SELECT * FROM tasks")}
        self.assertEqual((rows["查资料"]["owner"], rows["查资料"]["status"]), ("ethan", "open"))
        self.assertEqual((rows["交表"]["owner"], rows["交表"]["status"]), ("mia", "done"))
        cols = {r[1] for r in self.db.conn.execute("PRAGMA table_info(wishes)")}
        self.assertTrue({"category", "mention_count", "quotes", "realized_event_id"} <= cols)
        n = self.db.conn.execute("SELECT COUNT(*) n FROM visibility_rules").fetchone()["n"]
        self.assertEqual(n, len(lifebase.DEFAULT_RULES))

    def test_levels_never_public_for_private_dims_and_sources(self):
        self.assertEqual(lifebase.level_for(self.db, "body", tags=["cv"]), "private")
        self.assertEqual(lifebase.level_for(self.db, "resources", tags=["work"]), "private")
        self.assertEqual(lifebase.level_for(self.db, "values", "bank", ["cv"]), "private")
        self.assertEqual(lifebase.level_for(self.db, "abilities"), "work")
        self.assertEqual(lifebase.level_for(self.db, "abilities", tags=["cv"]), "public")
        self.assertEqual(lifebase.level_for(self.db, "values"), "internal")

    def test_evidence_dedup_and_validation(self):
        a, b = self.ev(), self.ev()
        self.assertEqual(a, b)
        with self.assertRaises(ValueError):
            lifebase.add_evidence(self.db, "nope", "x", "2026-10-30")

    def test_fact_validation(self):
        e = self.ev()
        with self.assertRaises(ValueError):
            lifebase.add_fact(self.db, "luck", "运气好", [e])
        with self.assertRaises(ValueError):
            lifebase.add_fact(self.db, "rhythms", "开始跑步", [])
        with self.assertRaises(ValueError):
            lifebase.add_fact(self.db, "rhythms", "开始跑步", [e + 99])
        fid = lifebase.add_fact(self.db, "abilities", "会写 Python", [e], also_dims=["thinking", "abilities", "luck"],
                                subject="person:3", tags=["cv"])
        row = self.db.conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
        self.assertEqual(json.loads(row["also_dims"]), ["thinking"])
        self.assertEqual(row["visibility"], "private")  # 别人的事永远私密

    def test_event_validation(self):
        e = self.ev()
        with self.assertRaises(ValueError):
            lifebase.add_event(self.db, "2026-10-30", "跑步", [e], "realtime", emotion="meh")
        eid = lifebase.add_event(self.db, "2026-10-30", "第一次跑 5 公里", [e], "realtime", dims=["body", "rhythms"],
                                 emotion="proud", territory="body", importance=9)
        row = self.db.conn.execute("SELECT * FROM events WHERE id = ?", (eid,)).fetchone()
        self.assertEqual(row["importance"], 5)

    def test_supersede_keeps_old_and_current_filters_level(self):
        e = self.ev()
        old = lifebase.add_fact(self.db, "rhythms", "每天 1 点睡", [e])
        new = lifebase.add_fact(self.db, "rhythms", "每天 12 点睡", [e])
        lifebase.supersede(self.db, old, new)
        cur = [f["id"] for f in lifebase.current_facts(self.db, "rhythms")]
        self.assertEqual(cur, [new])
        self.assertEqual(self.db.conn.execute("SELECT superseded_by FROM facts WHERE id = ?", (old,)).fetchone()[0], new)
        pub = lifebase.add_fact(self.db, "abilities", "做过 小伴 项目", [e], tags=["work"])
        self.assertEqual([f["id"] for f in lifebase.current_facts(self.db, max_level="public")], [pub])
        lifebase.invalidate(self.db, pub)
        self.assertEqual(lifebase.current_facts(self.db, max_level="public"), [])

    def test_backfill_idempotent(self):
        root = Path(self.tmp.name)
        self.store.raw.append("telegram", "mia", "今天去跑步了")
        self.store.raw.append("telegram", "ethan", "好棒")
        self.store.raw.append("telegram", "system", "忽略")
        (root / "inbox").mkdir()
        (root / "inbox" / "2026-10-30.jsonl").write_text(
            json.dumps({"at": "2026-10-30T09:00:00+00:00", "path": "/grok", "label": "grok", "text": "资料"}, ensure_ascii=False) + "\n",
            encoding="utf-8")
        self.store.wiki.write("about/body", "身体与健康", "- （2026-10-29）膝盖不太好\n- （2026-10-30）开始跑步\n")
        self.store.wiki.write("about/other", "其他", "- （2026-10-30）喜欢蓝色\n")
        got = lifebase.backfill(self.store)
        self.assertEqual((got["chat"], got["grok"], got["facts"]), (2, 1, 3))
        again = lifebase.backfill(self.store)
        self.assertEqual((again["facts"], again["new_evidence"]), (0, 0))
        rows = {r["statement"]: r for r in self.db.conn.execute("SELECT * FROM facts")}
        self.assertEqual(rows["膝盖不太好"]["dimension"], "body")
        self.assertEqual(rows["膝盖不太好"]["valid_from"], "2026-10-29")
        self.assertEqual(rows["喜欢蓝色"]["status"], "candidate")

    def test_backfill_reads_journal_and_about_section(self):
        self.store.wiki.write("journal/2026-10-28", "记忆", "# 2026-10-28 的记忆\n\n她去攀岩。\n\n新知道的：\n- 【饮食】不吃香菜\n- 【作息与习惯】十二点前睡\n\n聊到的人：\n- Amy：一起攀岩")
        self.store.wiki.write("about-mia", "关于 Mia", "她在伦敦读书。\n\n## 小伴 每晚整理\n\n### 饮食\n- （2026-10-28）不吃香菜\n- 爱喝冰美式\n\n### 其他\n- 喜欢克莱因蓝")
        got = lifebase.backfill(self.store)
        rows = {r["statement"]: dict(r) for r in self.db.conn.execute("SELECT * FROM facts")}
        self.assertEqual(got["facts"], 5)  # 香菜两处只记一次；“聊到的人”不算事实；开头她写的那句也迁
        self.assertEqual((rows["不吃香菜"]["dimension"], rows["不吃香菜"]["valid_from"]), ("preferences", "2026-10-28"))
        self.assertEqual(rows["十二点前睡"]["dimension"], "rhythms")
        self.assertIsNone(rows["爱喝冰美式"]["valid_from"])
        self.assertEqual(rows["喜欢克莱因蓝"]["status"], "candidate")
        self.assertEqual((rows["她在伦敦读书。"]["confidence"], rows["她在伦敦读书。"]["locked"]), (1.0, 0))
        self.assertEqual(lifebase.backfill(self.store)["facts"], 0)
        self.assertTrue(lifebase.head_migrated(self.db))

    def test_head_lines_guess_dims_and_split(self):
        body = ("# 关于 Mia\n\n## 身体\n- 膝盖不太好\n\n## 性格\n" + "她做事很快，" * 10 + "但讨厌被催。也怕冷。"
                "\n\n### 杂\n见 [[relationship|关系]]\n\n## 小伴 每晚整理\n\n- 不该迁")
        got = lifebase.head_lines(body)
        self.assertEqual(got[0], ("body", "膝盖不太好"))
        self.assertEqual([d for d, _ in got[1:3]], ["temperament", "temperament"])  # 长段落按句号拆开
        self.assertIn(("preferences", "见 relationship"), got)
        self.assertNotIn("不该迁", " ".join(t for _, t in got))


if __name__ == "__main__":
    unittest.main()
