"""日记是谁写的（修）：系统写的不能记成“她的原话”，她在系统草稿上改的只有改动的句子算她的。"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import diary, interviews, lifebase, runlog
from ethan.memory import MemoryStore

os.environ.setdefault("ETHAN_NO_KICK", "1")

DRAFT = "早上去游泳了。下午在图书馆写报告。晚上早早睡了。"


class Weaver:
    def __init__(self, text):
        self.text, self.seen = text, []

    def complete(self, system, messages, purpose="chat", images=()):
        self.seen.append(messages[-1]["content"])
        return SimpleNamespace(text=self.text)


class DiarySourceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def diary_evidence(self):
        return [dict(r) for r in self.db.conn.execute(
            "SELECT id, source_ref, text FROM evidence WHERE source = 'workbench' AND source_ref LIKE 'diary:%' ORDER BY id")]

    def test_system_draft_is_not_her_words(self):
        d = diary.save(self.db, "2026-11-03", DRAFT, source="ethan")
        self.assertEqual((d["source"], d["draft_text"]), ("ethan", DRAFT))
        self.assertEqual(diary.her_sentences(d), [])
        lifebase.backfill_handwritten(self.db)
        self.assertEqual(self.diary_evidence(), [])

    def test_weave_is_system_and_her_light_edit_only_counts_changed_sentences(self):
        diary.save(self.db, "2026-11-03", DRAFT, source="ethan")
        woven = DRAFT + "傍晚还喝了杯咖啡。"
        got = diary.weave(self.store, Weaver(woven), "2026-11-03", "傍晚喝了咖啡")
        self.assertEqual((got["source"], got["draft_text"]), ("ethan", woven))
        # 她只改了一句、加了一句
        mine = woven.replace("下午在图书馆写报告。", "下午在图书馆把报告磨完了，比想的顺。") + "\n其实我挺喜欢一个人待着。"
        got = diary.save(self.db, "2026-11-03", mine)
        self.assertEqual((got["source"], got["draft_text"]), ("her_edited", woven))  # 原稿留着
        self.assertEqual(diary.her_sentences(got), ["下午在图书馆把报告磨完了，比想的顺。", "其实我挺喜欢一个人待着。"])
        self.assertEqual(diary.write(self.store, None, __import__("datetime").date(2026, 11, 3), force=True), "她自己改过，不动")
        lifebase.backfill_handwritten(self.db)
        ev = self.diary_evidence()
        self.assertEqual(len(ev), 1)
        self.assertIn(":edit-", ev[0]["source_ref"])
        self.assertNotIn("游泳", ev[0]["text"])  # 没改的句子是系统写的，不算
        self.assertIn("比想的顺", ev[0]["text"])
        # 再存一次原样：她的句子还是那两句，不重复登记
        diary.save(self.db, "2026-11-03", mine)
        lifebase.backfill_handwritten(self.db)
        self.assertEqual(len(self.diary_evidence()), 1)
        # 交给 digest 时写清是“系统草稿上改动的句子”
        seen = []

        class R:
            def complete(self, system, messages, purpose="chat", images=()):
                seen.append(messages[-1]["content"])
                return SimpleNamespace(text=json.dumps({"facts": []}))
        interviews.digest(self.store, R())
        self.assertTrue(any("系统草稿上改动的句子" in m and "比想的顺" in m and "游泳" not in m for m in seen))
        self.assertIn("改系统草稿时", interviews._context("", ev[0]["source_ref"]))

    def test_saving_draft_unchanged_stays_system(self):
        diary.save(self.db, "2026-11-03", DRAFT, source="ethan")
        self.assertEqual(diary.save(self.db, "2026-11-03", DRAFT)["source"], "ethan")

    def test_written_from_blank_is_her(self):
        d = diary.save(self.db, "2026-11-04", "今天我自己写。下雨了。")
        self.assertEqual((d["source"], d["draft_text"]), ("her", None))
        self.assertEqual(diary.save(self.db, "2026-11-04", "今天我自己写。雨停了。")["source"], "her")
        # 存空 = 重写；之后她从空白再写，还是她的
        diary.save(self.db, "2026-11-04", "")
        self.assertEqual(diary.save(self.db, "2026-11-04", "重新写一篇。")["source"], "her")
        # 还没日记时语音“帮我写进去”：她说的就是开头，算她的
        got = diary.weave(self.store, Weaver("不会用到"), "2026-11-05", "今天去了海边")
        self.assertEqual(got["source"], "her")
        lifebase.backfill_handwritten(self.db)
        refs = [e["source_ref"] for e in self.diary_evidence()]
        self.assertEqual(len(refs), 2)
        self.assertFalse(any(":edit-" in r for r in refs))

    def test_weave_keeps_her_own_sentences_hers(self):
        diary.save(self.db, "2026-11-04", "我今天去了海边。风很大。")
        got = diary.weave(self.store, Weaver("我今天去了海边。风很大。回来路上买了花，心情变好了。"), "2026-11-04", "回来买了花")
        self.assertEqual(got["source"], "her_edited")
        self.assertEqual(diary.her_sentences(got), ["我今天去了海边。", "风很大。"])  # 模型加的那句不算她的

    def test_legacy_auto_row_edit_uses_old_body_as_draft(self):
        diary.ensure(self.db)
        self.db.conn.execute("INSERT INTO diary (day, body, source, updated_at) VALUES ('2026-10-30', ?, 'auto', 'x')", (DRAFT,))
        got = diary.save(self.db, "2026-10-30", DRAFT.replace("晚上早早睡了。", "晚上熬夜了。"))
        self.assertEqual((got["source"], got["draft_text"]), ("her_edited", DRAFT))
        self.assertEqual(diary.her_sentences(got), ["晚上熬夜了。"])

    def test_runlog_note_summarises_edits(self):
        diary.save(self.db, "2026-11-03", DRAFT, source="ethan")
        runlog.collect(self.db)
        self.assertIsNone(runlog.recent(self.db, "diary")[0]["reaction"])
        diary.save(self.db, "2026-11-03", DRAFT.replace("晚上早早睡了。", "晚上看了一部很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长很长的电影。"))
        runlog.collect(self.db)
        r = runlog.recent(self.db, "diary")[0]
        self.assertEqual(r["reaction"], "edited")
        self.assertTrue(r["note"].startswith("改了 1 句："))
        self.assertIn("「晚上早早睡了。」→「晚上看了", r["note"])
        self.assertIn("…", r["note"])  # 截短
        diary.save(self.db, "2026-11-03", DRAFT + "又加一句。")
        runlog.collect(self.db)
        self.assertTrue(runlog.recent(self.db, "diary")[0]["note"].startswith("改了 1 句：「（新加）」→「又加一句。」"))

    def test_audit_lists_old_misregistered_days_read_only(self):
        diary.ensure(self.db)
        # 以前的数据：weave 后她存、批注改过、系统初稿她改了再存，全记成 her 并登记成了原话
        for day, body in (("2026-11-01", "被融过的一篇。"), ("2026-11-02", "批注改过的一篇。"), ("2026-11-03", "她自己写的一篇。")):
            self.db.conn.execute("INSERT INTO diary (day, body, source, updated_at) VALUES (?, ?, 'her', 'x')", (day, body))
        self.db.conn.execute(diary.NOTES)
        self.db.conn.execute("INSERT INTO diary_notes (day, text, note, status, at) VALUES ('2026-11-02', 'a', 'b', 'done', 'x')")
        self.db.conn.commit()
        lifebase.backfill_handwritten(self.db)
        self.db.conn.commit()
        with (self.root / "workbench-log.jsonl").open("w", encoding="utf-8") as f:
            for t in ("补日记：2026-11-01", "改日记：2026-11-01", "改日记：2026-11-03"):
                f.write(json.dumps({"at": "x", "text": t}, ensure_ascii=False) + "\n")
        before = self.db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0]
        items = {x["day"]: x for x in diary.audit_source(self.root)}
        self.assertEqual(set(items), {"2026-11-01", "2026-11-02", "2026-11-03"})
        self.assertTrue(items["2026-11-01"]["suspect"])
        self.assertIn("weave", items["2026-11-01"]["why"][0])
        self.assertTrue(items["2026-11-02"]["suspect"])
        self.assertFalse(items["2026-11-03"]["suspect"])
        self.assertEqual(len(items["2026-11-01"]["evidence"]), 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], before)  # 只读
        self.assertEqual(self.db.conn.execute("SELECT source FROM diary WHERE day = '2026-11-01'").fetchone()[0], "her")
        # runlog 记着“小伴 初稿、她改了”的也算疑似
        runlog.collect(self.db)
        self.assertTrue({x["day"]: x for x in diary.audit_source(self.root)}["2026-11-03"]["suspect"])


if __name__ == "__main__":
    unittest.main()
