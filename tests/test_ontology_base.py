"""ontology 第一批地基（docs/design/ontology.md 第 3、3.1、6.1、6.3 节）：知识类型、来源类型、决定、授权。只建结构，不改现有行为。"""

import tempfile
import unittest
from pathlib import Path

from ethan import decisions, lifebase
from ethan.memory import MemoryStore


class OntologyBaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_kinds_and_source_kinds(self):
        chat = lifebase.add_evidence(self.db, "chat", "chat:1", "2026-11-01", "我订之前都会查退改")
        grok = lifebase.add_evidence(self.db, "grok", "grok:1", "2026-11-01", "Grok 说她习惯拖延")
        f = lifebase.add_fact(self.db, "preferences", "订之前先查退改", [chat], kind="preference")
        row = self.db.conn.execute("SELECT kind, source_kind, root_evidence_ids FROM facts WHERE id = ?", (f,)).fetchone()
        self.assertEqual((row["kind"], row["source_kind"]), ("preference", "her_words"))
        with self.assertRaises(ValueError):  # 转述不能写成关于她的事实
            lifebase.add_fact(self.db, "thinking", "她习惯拖延", [grok], kind="fact", source_kind="agent_relay")
        h = lifebase.add_fact(self.db, "thinking", "她可能习惯拖到截止前", [grok], kind="hypothesis", source_kind="agent_relay")
        self.assertEqual(self.db.conn.execute("SELECT kind FROM facts WHERE id = ?", (h,)).fetchone()[0], "hypothesis")
        old = lifebase.add_fact(self.db, "rhythms", "老数据", [chat])  # 老的调用方照旧，按事实记、来源按证据补
        self.assertEqual(self.db.conn.execute("SELECT kind, source_kind FROM facts WHERE id = ?", (old,)).fetchone()[:], ("fact", "her_words"))

    def test_decisions_versions_and_scope(self):
        with self.assertRaises(ValueError):
            decisions.add(self.db, "rule", "全部", "什么都别做")
        a = decisions.add(self.db, "rule", "英语陪练", "找英语陪练渠道", roles=("planner", "prep"), quote="……")
        seen = decisions.versions(self.db, [a])
        b = decisions.add(self.db, "rule", "英语陪练", "就用 Grok，不再找渠道", roles=("planner", "prep"), supersedes=a, source="chat")
        self.assertEqual([d["id"] for d in decisions.active(self.db, role="planner", scope="英语陪练")], [b])
        self.assertEqual(decisions.changed_since(self.db, seen), [a])  # 13:00 读到的那条到 13:10 已经被取代
        self.assertEqual(decisions.versions(self.db, [b])[b][0], 2)
        g = decisions.add(self.db, "gate", "ontology 开发", "这一版先只做设计", roles=("maintainer",))
        self.assertEqual([d["id"] for d in decisions.active(self.db, role="planner")], [b])  # 管维修工的不给计划器
        self.assertIn(g, [d["id"] for d in decisions.active(self.db, role="maintainer")])

    def test_grants_expire_revoke_and_pay_always_confirms(self):
        gid = decisions.grant(self.db, "prep", "book", "餐厅", each_confirm=True, until="2026-11-08T23:59:59+00:00", quote="这周订餐厅前先给我看")
        self.assertTrue(decisions.allowed(self.db, "prep", "book", "餐厅", now="2026-11-05T12:00:00+00:00"))
        self.assertIsNone(decisions.allowed(self.db, "prep", "book", "餐厅", now="2026-11-09T12:00:00+00:00"))  # 到期
        self.assertTrue(decisions.revoke(self.db, gid))
        self.assertIsNone(decisions.allowed(self.db, "prep", "book", "餐厅", now="2026-11-05T12:00:00+00:00"))
        p = decisions.grant(self.db, "prep", "pay", "餐厅订金", each_confirm=False, quote="……")
        self.assertEqual(decisions.allowed(self.db, "prep", "pay", "餐厅订金")["each_confirm"], 1)  # 付款永远每次确认


if __name__ == "__main__":
    unittest.main()
