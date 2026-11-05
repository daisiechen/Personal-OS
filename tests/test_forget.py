"""删除防复活（docs/design/ontology.md 7.3 节，2026-11-03）：她删掉的来源，同步、整理、从备份恢复后都不再长回来。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, memsearch, portrait
from ethan.memory import MemoryStore


class ForgetTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"  # 数据目录外那份，测试里放临时目录

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql, *a):
        return self.db.conn.execute(sql, a).fetchone()

    def test_forget_clears_derived_and_blocks_reimport(self):
        a = lifebase.add_evidence(self.db, "email", "mail:x1", "2026-11-01", "前任的邮件：周末见")
        b = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:3", "2026-11-01", "mia：周末去爬山")
        only = lifebase.add_fact(self.db, "relationships", "周末和前任见面", [a])
        both = lifebase.add_fact(self.db, "rhythms", "周末爱出门", [a, b])
        rep = lifebase.forget(self.db, [a], root=self.root)
        self.assertEqual(rep["facts_deleted"], [only])
        self.assertEqual(rep["facts_suspended"], [both])  # 还有别的依据的先停用，不直接留着原话
        self.assertEqual(self.q("SELECT status FROM facts WHERE id = ?", both)[0], "suspended")
        self.assertEqual(rep["log_saved"], 2)
        self.assertIsNone(self.q("SELECT text FROM evidence WHERE id = ?", a)[0])
        self.assertEqual(self.q("SELECT statement FROM facts WHERE id = ?", only)[0], "")
        self.assertEqual(json.loads(self.q("SELECT evidence_ids FROM facts WHERE id = ?", both)[0]), [b])
        self.assertNotIn("前任", json.dumps(rep, ensure_ascii=False))  # 报告不带原文
        with self.assertRaises(lifebase.Forgotten):
            lifebase.add_evidence(self.db, "email", "mail:x1", "2026-11-01", "前任的邮件：周末见")
        self.assertIsNone(lifebase.try_evidence(self.db, "email", "mail:x1", "2026-11-01", "再同步一次"))
        with self.assertRaises(lifebase.Forgotten):  # 只剩删掉的证据，不能再拿来生成条目
            lifebase.add_fact(self.db, "relationships", "又是前任", [a])
        self.assertTrue(lifebase.allow_reimport(self.db, "email", "mail:x1", self.root))  # 她明确选“重新导入”
        again = lifebase.try_evidence(self.db, "email", "mail:x1", "2026-11-01", "前任的邮件：周末见")
        self.assertEqual(again, a)
        row = self.q("SELECT text, deleted_at FROM evidence WHERE id = ?", a)
        self.assertEqual((row[0], row[1]), ("前任的邮件：周末见", None))  # 重新导入真的把内容放回来了

    def test_index_drops_deleted(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：我很怕牙医")
        lifebase.add_fact(self.db, "preferences", "很怕看牙医", [a])
        memsearch.refresh(self.db)
        self.assertTrue(memsearch.search(self.db, ["牙医"], user="ethan"))
        lifebase.forget(self.db, [a], root=self.root)
        memsearch.refresh(self.db)
        self.assertFalse(memsearch.search(self.db, ["牙医"], user="ethan"))

    def test_restore_from_backup_reapplies(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:d:9", "2026-11-01", "mia：别记这个")
        self.db.conn.commit()
        saved = self.db.conn.execute("SELECT * FROM evidence WHERE id = ?", (a,)).fetchone()  # 删除前的备份
        lifebase.forget(self.db, [a], root=self.root)
        self.db.conn.execute("DELETE FROM forgotten")  # 模拟从旧备份恢复：库里的删除记录也回到删除前
        self.db.conn.execute("UPDATE evidence SET text = ?, deleted_at = NULL WHERE id = ?", (saved["text"], a))
        self.assertEqual(self.q("SELECT text FROM evidence WHERE source_ref = 'raw:d:9'")[0], "mia：别记这个")  # 备份里还在
        self.assertEqual(lifebase.reapply_deletions(self.db, self.root)["redone"], 1)
        self.assertIsNone(self.q("SELECT text FROM evidence WHERE source_ref = 'raw:d:9'")[0])
        self.assertIsNone(lifebase.try_evidence(self.db, "chat", "raw:d:9", "2026-11-01", "别记这个"))

    def test_whole_data_dir_rollback_still_reapplies_from_outside_copy(self):
        a = lifebase.add_evidence(self.db, "email", "mail:z9", "2026-11-01", "要忘掉的邮件")
        self.db.conn.commit()
        saved = self.q("SELECT text FROM evidence WHERE id = ?", a)[0]
        lifebase.forget(self.db, [a], root=self.root)
        # 整个数据目录退回到删除之前：库里的记录和目录里的指纹文件都回到删除前
        (self.root / lifebase.FORGOTTEN_FILE).unlink()
        self.db.conn.execute("DELETE FROM forgotten")
        self.db.conn.execute("UPDATE evidence SET text = ?, deleted_at = NULL WHERE id = ?", (saved, a))
        self.assertEqual(lifebase.reapply_deletions(self.db, self.root)["redone"], 1)
        self.assertIsNone(self.q("SELECT text FROM evidence WHERE id = ?", a)[0])

    def test_mixed_source_private_text_leaves_profile_and_history(self):
        secret = lifebase.add_evidence(self.db, "email", "mail:s1", "2026-11-01", "私密：在看心理医生")
        plain = lifebase.add_evidence(self.db, "chat", "raw:d:2", "2026-11-01", "mia：最近睡得不好")
        f = lifebase.add_fact(self.db, "preferences", "在看心理医生，最近睡得不好", [secret, plain])
        self.db.conn.execute("INSERT OR REPLACE INTO profile_blocks (dimension, summary, fact_ids, version, updated_at) "
                             "VALUES ('preferences', '在看心理医生', ?, 1, 'x')", (json.dumps([f]),))
        self.db.conn.execute("INSERT INTO profile_history (dimension, version, summary, at) VALUES ('preferences', 1, '在看心理医生', 'x')")
        rep = lifebase.forget(self.db, [secret], root=self.root)
        self.assertNotIn("心理医生", portrait.chat_text(self.db, "ethan"))
        self.assertIsNone(self.q("SELECT 1 FROM profile_history WHERE summary LIKE '%心理医生%'"))
        self.assertEqual(rep["history_removed"], 1)
        self.assertFalse([x for x in lifebase.current_facts(self.db) if x["id"] == f])

    def test_derived_facts_suspended_too(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:d:3", "2026-11-01", "mia：我在吃抗过敏药")
        base = lifebase.add_fact(self.db, "preferences", "在吃抗过敏药", [a])
        b = lifebase.add_evidence(self.db, "chat", "raw:d:4", "2026-11-02", "mia：春天少出门")
        derived = lifebase.add_fact(self.db, "rhythms", "春天少出门，可能和过敏有关", [b])
        self.db.conn.execute("UPDATE facts SET derived_from = ? WHERE id = ?", (json.dumps([base]), derived))
        rep = lifebase.forget(self.db, [a], root=self.root)
        self.assertIn(derived, rep["facts_suspended"])

    def test_log_failure_reported_not_silent(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:d:5", "2026-11-01", "mia：别记")
        orig = lifebase._log_paths
        lifebase._log_paths = lambda root: [Path("/proc/nope/a.jsonl"), Path("/proc/nope/b.jsonl")]
        try:
            rep = lifebase.forget(self.db, [a], root=self.root)
        finally:
            lifebase._log_paths = orig
        self.assertEqual(rep["log_saved"], 0)
        self.assertTrue(any("没写到库外" in p for p in lifebase.integrity_problems(self.db)))

    def test_fact_only_deletion_is_logged_and_redone(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:d:6", "2026-11-01", "mia：随便说说")
        f = lifebase.add_fact(self.db, "preferences", "喜欢深夜写东西", [a])
        self.db.conn.commit()
        lifebase.forget(self.db, [], [f], root=self.root)
        self.db.conn.execute("UPDATE facts SET status = 'active', statement = '喜欢深夜写东西' WHERE id = ?", (f,))  # 退回旧备份
        out = lifebase.reapply_deletions(self.db, self.root)
        self.assertEqual(out["facts_redone"], 1)
        self.assertEqual(self.q("SELECT status FROM facts WHERE id = ?", f)[0], "deleted")

    def test_unverifiable_restore_puts_memory_on_hold(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:d:7", "2026-11-01", "mia：我很怕狗")
        lifebase.add_fact(self.db, "preferences", "很怕狗", [a])
        lifebase.forget(self.db, [lifebase.add_evidence(self.db, "chat", "raw:d:8", "2026-11-01", "mia：别记这个")], root=self.root)
        for p in lifebase._log_paths(self.root)[1:]:
            p.unlink()  # 换了新服务器：数据目录外那份不在
        out = lifebase.reapply_deletions(self.db, self.root)
        self.assertFalse(out["verified"])
        memsearch.refresh(self.db)
        self.assertEqual(memsearch.search(self.db, ["怕狗"], user="ethan"), [])  # 核清之前谁都查不到
        self.assertTrue(memsearch.search(self.db, ["怕狗"], user="system"))
        lifebase.release_hold(self.db)
        self.assertTrue(memsearch.search(self.db, ["怕狗"], user="ethan"))

    def test_fresh_install_without_deletions_is_verified(self):
        self.assertTrue(lifebase.reapply_deletions(self.db, self.root)["verified"])
        self.assertIsNone(lifebase.on_hold(self.db))

    def test_nightly_skips_forgotten_journal_day(self):
        day = date(2026, 11, 1)
        ev = lifebase.add_evidence(self.db, "chat", f"journal:{day}", str(day), "她说了件私事")
        lifebase.forget(self.db, [ev], root=self.root)

        class R:
            def complete(self, *a, **k):
                raise AssertionError("删掉的那天不该再送去整理")
        got = portrait.nightly(self.store, R(), day, ["她说了件私事"])
        self.assertIn("skipped", got)

    def test_her_originals_untouched(self):
        diary = self.root / "wiki" / "journal"
        diary.mkdir(parents=True)
        (diary / "2026-11-01.md").write_text("她自己写的", encoding="utf-8")
        a = lifebase.add_evidence(self.db, "chat", "wiki:journal/2026-11-01", "2026-11-01", "她自己写的", path="wiki/journal/2026-11-01.md")
        rep = lifebase.forget(self.db, [a], root=self.root)
        self.assertEqual((diary / "2026-11-01.md").read_text(encoding="utf-8"), "她自己写的")
        self.assertTrue(rep["still_may_remain"])


if __name__ == "__main__":
    unittest.main()
