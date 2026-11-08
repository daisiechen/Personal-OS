"""Codex 评审 11-07 第 2–6 条（docs/research/2026-11-07-codex-code-review.md）：遗忘、撤销、事件、竞态、只删这条的回归。"""

import json
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import lifebase, memsearch, mepage, onto_query, ontology, portrait
from ethan.memory import MemoryStore


class ForgetFixesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        mepage.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def q(self, sql, *a):
        return self.db.conn.execute(sql, a).fetchone()

    def test_undo_does_not_revive_forgotten_or_drop_other_confirmations(self):
        """第 2 条：确认矛盾题 → 删掉其中一份来源（停用）→ 撤销先前的确认，停用的不能恢复成有效；别的操作的确认记录不能被一起删。"""
        a = lifebase.add_evidence(self.db, "chat", "raw:u:1", "2026-11-01", "mia：私密的事 A")
        b = lifebase.add_evidence(self.db, "chat", "raw:u:2", "2026-11-01", "mia：普通的 B")
        mine = lifebase.add_fact(self.db, "rhythms", "每周练 5 次", [a, b], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (mine,))
        guess = lifebase.add_fact(self.db, "rhythms", "每周练 4 次", [b])
        other = lifebase.add_fact(self.db, "preferences", "爱喝美式", [b])
        lifebase.supersede(self.db, mine, guess)
        pid = mepage.pending_items(self.db)[0]["id"]
        self.assertTrue(mepage.resolve(self.db, pid, "pick", pick=0)["ok"])
        lifebase.forget(self.db, [a], root=self.root)
        self.assertEqual(self.q("SELECT status FROM facts WHERE id = ?", mine)[0], "suspended")
        mepage.verdict_fact(self.db, other, "yes")  # 之后另一项操作的确认
        later = self.q("SELECT MAX(id) FROM confirmations")[0]
        out = mepage.reopen(self.db, pid)
        self.assertTrue(out["ok"])
        self.assertEqual(self.q("SELECT status FROM facts WHERE id = ?", mine)[0], "suspended")  # 删除优先于撤销
        self.assertEqual(self.q("SELECT status FROM facts WHERE id = ?", guess)[0], "candidate")  # 没被后来改过的照常恢复
        self.assertIsNotNone(self.q("SELECT 1 FROM confirmations WHERE id = ?", later))  # 无关的确认还在
        self.assertIsNone(self.q("SELECT 1 FROM confirmations WHERE source = 'pending' AND fact_id = ?", mine))  # 这次自己的确认撤掉

    def test_forget_covers_native_situation(self):
        """第 3 条：原生情境（没有旧 facts）遗忘来源后，whereabouts_now 不再回答它，起止日也不留。"""
        ev = lifebase.add_evidence(self.db, "chat", "raw:s:1", "2026-11-01", "mia：我在做秘密项目")
        fid = lifebase.add_fact(self.db, "abilities", "在做秘密项目", [ev])
        sid = lifebase.add_situation(self.db, "秘密项目", "2026-11-01", "2026-12-31", fid=fid)
        got = onto_query.whereabouts_now(self.db, "ethan", today=date(2026, 11, 7))
        self.assertIn("秘密项目", json.dumps(got, ensure_ascii=False))
        lifebase.forget(self.db, [ev], root=self.root)
        got = onto_query.whereabouts_now(self.db, "ethan", today=date(2026, 11, 7))
        self.assertNotIn("秘密项目", json.dumps(got, ensure_ascii=False))
        self.assertNotIn("2026-12-31", json.dumps(got, ensure_ascii=False))
        left = self.db.conn.execute("SELECT value_text, review FROM statements WHERE subject_id = ?", (sid,)).fetchall()
        self.assertTrue(left and all(r["review"] == "forgotten" and not r["value_text"] for r in left))
        self.assertEqual(tuple(self.q("SELECT name, status FROM objects WHERE id = ?", sid)), ("", "forgotten"))

    def test_native_statement_with_forgotten_source_hidden_in_query(self):
        """第 3 条（查询端）：来源已删、但说法还没被遗忘处理到的（比如从备份恢复的中间态），查询也不给。"""
        ev = lifebase.add_evidence(self.db, "chat", "raw:s:2", "2026-11-01", "mia：在做另一个项目")
        fid = lifebase.add_fact(self.db, "abilities", "在做另一个项目", [ev])
        lifebase.add_situation(self.db, "另一个项目", "2026-11-01", None, fid=fid)
        self.db.conn.execute("UPDATE evidence SET text = NULL, deleted_at = '2026-11-07' WHERE id = ?", (ev,))
        got = onto_query.whereabouts_now(self.db, "ethan", today=date(2026, 11, 7))
        self.assertNotIn("另一个项目", json.dumps(got, ensure_ascii=False))

    def test_mixed_source_event_deactivated(self):
        """第 4 条：事件由私密来源 A 和普通来源 B 支撑，删掉 A 后标题、摘要不留，刷新索引后也检索不到。"""
        a = lifebase.add_evidence(self.db, "email", "mail:e:1", "2026-11-01", "私密：和前任吃饭")
        b = lifebase.add_evidence(self.db, "chat", "raw:e:1", "2026-11-01", "mia：晚上出去吃饭")
        eid = lifebase.add_event(self.db, "2026-11-01", "和前任吃饭", [a, b], "manual", summary="在老地方和前任吃饭")
        memsearch.refresh(self.db)
        self.assertTrue(memsearch.search(self.db, ["前任"], user="ethan"))
        rep = lifebase.forget(self.db, [a], root=self.root)
        self.assertIn(eid, rep["events_suspended"])
        row = self.q("SELECT title, summary, evidence_ids FROM events WHERE id = ?", eid)
        self.assertNotIn("前任", (row["title"] or "") + (row["summary"] or ""))
        self.assertEqual(json.loads(row["evidence_ids"]), [b])  # 剩下的依据留着，等重建
        memsearch.refresh(self.db)
        self.assertFalse(memsearch.search(self.db, ["前任"], user="ethan"))
        self.assertIsNone(self.q("SELECT 1 FROM mem_docs WHERE key = ?", f"event:{eid}"))

    def test_forget_between_read_and_write_does_not_regenerate(self):
        """第 5 条：每晚 fact_ops 读到旧事实以后、写之前插进来的遗忘，不能让它接出一条新的有效事实（检查和写入一个事务）。"""
        a = lifebase.add_evidence(self.db, "chat", "raw:r:1", "2026-11-01", "mia：私密，在吃某种药")
        old = lifebase.add_fact(self.db, "body", "在吃某种药", [a])
        j = lifebase.add_evidence(self.db, "chat", "journal:2026-11-07", "2026-11-07", "换了剂量")
        self.db.conn.commit()
        other = sqlite3.connect(str(self.root / "ethan.sqlite"), timeout=0.05, check_same_thread=False)
        other.row_factory = sqlite3.Row
        db2 = SimpleNamespace(conn=other)
        state = {"forgot": False}
        real = portrait._current

        def hooked(f):
            ok = real(f)
            if f and f["id"] == old and not state["forgot"]:
                try:  # 另一连接上的遗忘：修好后这时拿不到写锁，只能排在这次写入之后
                    lifebase.forget(db2, [a], root=self.root)
                    state["forgot"] = True
                except sqlite3.OperationalError:
                    other.rollback()
            return ok
        portrait._current = hooked
        try:
            portrait.apply_ops(self.db, {"ops": [{"op": "UPDATE", "from": "n1", "fact_id": old, "dimension": "body",
                                                  "statement": "在吃某种药，剂量换了"}]}, j, date(2026, 11, 7))
        finally:
            portrait._current = real
            other.close()
        new = self.db.conn.execute("SELECT id, status FROM facts WHERE id > ?", (old,)).fetchall()
        if state["forgot"]:  # 遗忘先落下：这次更新必须放弃
            self.assertFalse([r for r in new if r["status"] == "active"])

    def test_apply_ops_skips_when_basis_changed(self):
        """第 5 条：模型看到的旧事实和写入时的不一样（来源、状态、锁变了），这条放弃。"""
        a = lifebase.add_evidence(self.db, "chat", "raw:r:2", "2026-11-01", "mia：住在东区")
        b = lifebase.add_evidence(self.db, "chat", "raw:r:3", "2026-11-02", "mia：东区那套")
        old = lifebase.add_fact(self.db, "rhythms", "住在东区", [a, b])
        seen = {old: dict(self.q("SELECT * FROM facts WHERE id = ?", old))}
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (old,))  # 模型调用期间她锁上了
        j = lifebase.add_evidence(self.db, "chat", "journal:2026-11-08", "2026-11-08", "搬到西区")
        got = portrait.apply_ops(self.db, {"ops": [{"op": "UPDATE", "from": "n1", "fact_id": old, "dimension": "rhythms",
                                                    "statement": "住在西区"}]}, j, date(2026, 11, 8), seen=seen)
        self.assertEqual(got["UPDATE"], 0)
        self.assertIsNone(self.q("SELECT 1 FROM facts WHERE statement = '住在西区'"))

    def test_fact_only_deletion_blocks_same_claim_from_same_evidence(self):
        """第 6 条：只删这条记忆后，用同一份证据再抽出同一句（标点、空格不同也算），不再记回来；别的说法、别的证据照常。"""
        ev = lifebase.add_evidence(self.db, "chat", "raw:t:1", "2026-11-01", "mia：我怕狗，也爱喝咖啡")
        fid = lifebase.add_fact(self.db, "preferences", "她怕狗", [ev])
        out = mepage.verdict_fact(self.db, fid, "forget", root=self.root)
        self.assertTrue(out["ok"])
        with self.assertRaises(lifebase.Forgotten):
            lifebase.add_fact(self.db, "preferences", " 她怕狗。", [ev])
        self.assertTrue(lifebase.add_fact(self.db, "preferences", "爱喝咖啡", [ev]))  # 同一来源的别的说法照常
        ev2 = lifebase.add_evidence(self.db, "chat", "raw:t:2", "2026-11-05", "mia：我还是怕狗")
        both = lifebase.add_fact(self.db, "preferences", "她怕狗", [ev, ev2])  # 她又说了一遍：按新来源，旧来源不算依据
        self.assertEqual(json.loads(self.q("SELECT evidence_ids FROM facts WHERE id = ?", both)[0]), [ev2])
        self.assertNotIn("怕狗", (self.root / lifebase.FORGOTTEN_FILE).read_text(encoding="utf-8"))  # 墓碑不含原文

    def test_claim_tombstone_survives_restore(self):
        """第 6 条：从旧备份恢复（库里的墓碑没了）后，按库外的删除记录补回墓碑。"""
        ev = lifebase.add_evidence(self.db, "chat", "raw:t:3", "2026-11-01", "mia：不吃香菜")
        fid = lifebase.add_fact(self.db, "preferences", "不吃香菜", [ev])
        lifebase.forget(self.db, fact_ids=[fid], root=self.root)
        self.db.conn.execute("DELETE FROM forgotten_claims")
        self.db.conn.commit()
        lifebase.reapply_deletions(self.db, self.root)
        with self.assertRaises(lifebase.Forgotten):
            lifebase.add_fact(self.db, "preferences", "不吃香菜", [ev])

    def test_sweep_cleans_leftovers_of_old_deletions(self):
        """第 3、4 条的数据修复（migrations/20261107_forget_leftovers.py）：来源早就删了、当时没处理到的事件和原生情境补做。"""
        a = lifebase.add_evidence(self.db, "email", "mail:w:1", "2026-11-01", "私密：秘密项目和前任")
        b = lifebase.add_evidence(self.db, "chat", "raw:w:1", "2026-11-01", "mia：出去了")
        eid = lifebase.add_event(self.db, "2026-11-01", "和前任见面", [a, b], "manual")
        fid = lifebase.add_fact(self.db, "abilities", "在做秘密项目", [a])
        sid = lifebase.add_situation(self.db, "秘密项目", "2026-11-01", None, fid=fid)
        self.db.conn.execute("UPDATE evidence SET text = NULL, deleted_at = '2026-11-02' WHERE id = ?", (a,))  # 旧版删除留下的样子
        self.db.conn.commit()
        out = lifebase.sweep_forgotten(self.db)
        self.assertEqual((out["events"], out["objects_forgotten"]), (1, 1))
        self.assertEqual(self.q("SELECT title FROM events WHERE id = ?", eid)[0], lifebase.EVENT_GONE)
        self.assertEqual(self.q("SELECT status FROM objects WHERE id = ?", sid)[0], "forgotten")
        self.assertEqual(lifebase.sweep_forgotten(self.db)["objects_forgotten"], 0)  # 可重复跑


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
