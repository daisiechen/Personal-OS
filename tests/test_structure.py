"""规矩、在做的事、住处从普通说法整理进待你定，她点对才写进结构（ethan/structure.py）。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import decisions, lifebase, mepage, onto_query, ontology, structure
from ethan.memory import MemoryStore


class FakeRouter:
    def __init__(self, items):
        self.items, self.calls = items, 0

    def complete(self, system, messages, purpose="chat"):
        self.calls += 1
        return SimpleNamespace(text=json.dumps(self.items(messages[0]["content"]), ensure_ascii=False))


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        ontology.ensure(self.db)
        ev = lifebase.add_evidence(self.db, "chat", "raw:s:1", "2026-11-01", "mia：随便聊聊")
        self.rule = lifebase.add_fact(self.db, "rhythms", "她每周英语陪练的目标是 5 次。", [ev])
        self.stay = lifebase.add_fact(self.db, "rhythms", "她在商学院交换，待到 2027 年 1 月。", [ev])
        self.home = lifebase.add_fact(self.db, "rhythms", "她现在住在伦敦。", [ev])
        self.other = lifebase.add_fact(self.db, "preferences", "她喜欢罗马。", [ev])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def items(self, listing):
        out = []
        if f"#{self.rule} " in listing:
            out.append({"id": self.rule, "type": "rule", "text": "每周英语陪练 5 次", "scope": "英语"})
        if f"#{self.stay} " in listing:
            out.append({"id": self.stay, "type": "situation", "text": "伦敦商学院交换", "start": "2026-08-01", "end": "2027-01-15"})
        if f"#{self.home} " in listing:
            out.append({"id": self.home, "type": "lives_in", "text": "住在伦敦", "city": "伦敦"})
        out.append({"id": 99999, "type": "rule", "text": "编出来的"})  # 不在这批里的编号不收
        return out

    def test_propose_then_confirm_writes_structures(self):
        router = FakeRouter(self.items)
        got = structure.propose(self.store, router)
        self.assertEqual(got["proposed"], 3)
        self.assertNotIn(self.other, [r["id"] for r in lifebase.structure_candidates(self.db)])  # 关键词没挑上的不送去判
        items = {json.loads(r["payload"])["type"]: r["id"] for r in self.db.conn.execute("SELECT id, payload FROM pending WHERE kind = 'structure'")}
        self.assertEqual(structure.propose(self.store, router)["proposed"], 0)  # 提议过的不重复
        self.assertTrue(mepage.resolve(self.db, items["rule"], "yes")["ok"])
        self.assertEqual([d["statement"] for d in decisions.active(self.db)], ["每周英语陪练 5 次"])
        rules = onto_query.standing_rules(self.db, "ethan", "英语")  # 第 5 题现在答得出
        self.assertEqual(rules["status"], "answer")
        self.assertIn("每周英语陪练 5 次", json.dumps(rules["items"], ensure_ascii=False))
        self.assertTrue(mepage.resolve(self.db, items["situation"], "yes")["ok"])
        self.assertTrue(mepage.resolve(self.db, items["lives_in"], "no")["ok"])  # 不对：不写
        w = onto_query.whereabouts_now(self.db, "ethan")
        doing = next(i for i in w["items"] if i["part"] == "在做什么")["value"]
        self.assertEqual(doing[0]["what"], "伦敦商学院交换")
        self.assertIsNone(next(i for i in w["items"] if i["part"] == "住在哪个城市")["value"])
        self.assertFalse(mepage.resolve(self.db, items["rule"], "yes")["ok"])  # 定过的不能再定

    def test_same_sentence_asked_once(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:s:2", "2026-11-02", "mia：又说了一遍")
        again = lifebase.add_fact(self.db, "rhythms", "她现在住在伦敦，住得挺好。", [ev])
        both = lambda listing: [{"id": i, "type": "lives_in", "text": "住在伦敦", "city": "伦敦"} for i in (self.home, again) if f"#{i} " in listing]
        self.assertEqual(structure.propose(self.store, FakeRouter(both))["proposed"], 1)

    def test_lives_in_confirmed(self):
        structure.propose(self.store, FakeRouter(self.items))
        pid = next(r["id"] for r in self.db.conn.execute("SELECT id, payload FROM pending WHERE kind = 'structure'")
                   if json.loads(r["payload"])["type"] == "lives_in")
        self.assertTrue(mepage.resolve(self.db, pid, "yes")["ok"])
        w = onto_query.whereabouts_now(self.db, "ethan")
        lives = next(i for i in w["items"] if i["part"] == "住在哪个城市")["value"]
        self.assertIn("伦敦", lives[0]["value"])


if __name__ == "__main__":
    unittest.main()
