"""“我”页后台指标（ontology-plan 2.1）：厚 / 薄 / 旧 / 空只算她说的和确认的；被问得多又薄的给升级建议；转述不算独立证据。"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, mepage, onto_query
from ethan.memory import MemoryStore

DAY = date(2026, 11, 5)


class MetricsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        mepage.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def dim(self, m, name):
        return next(d for d in m["dims"] if d["dim"] == name)

    def test_states_count_only_her_words(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-04:1", "2026-11-04T09:00:00+00:00", text="mia：我喜欢靠窗、喜欢安静、不吃香菜")
        file_ev = lifebase.add_evidence(self.db, "file", "file:cv", "2026-11-04", text="简历")
        for s in ("喜欢靠窗", "喜欢安静", "不吃香菜"):
            lifebase.add_fact(self.db, "preferences", s, [ev])
        lifebase.add_fact(self.db, "preferences", "喜欢日料", [file_ev], source_kind="her_provided", kind="fact")
        grok = lifebase.add_evidence(self.db, "grok", "grok:1", "2026-11-04", text="她可能喜欢爵士")
        lifebase.add_fact(self.db, "values", "可能看重自由", [grok], kind="hypothesis", source_kind="agent_relay")
        self.db.conn.execute("UPDATE facts SET recorded_at = '2026-11-04T10:00:00+00:00'")
        m = mepage.metrics(self.db, DAY)
        p = self.dim(m, "preferences")
        self.assertEqual((p["n"], p["hers"], p["state"]), (4, 4, "thick"))
        self.assertEqual(p["evidence"], 2)
        v = self.dim(m, "values")
        self.assertEqual((v["hers"], v["state"], v["evidence"]), (0, "empty", 0))  # 转述撑着的不算她说的、也不算独立证据
        self.assertEqual(self.dim(m, "body")["state"], "empty")
        self.assertGreater(m["said"]["hers"], m["said"]["system"])

    def test_asked_but_thin_becomes_a_suggestion(self):
        for _ in range(3):
            onto_query.whereabouts_now(self.db, "ethan", today=DAY)  # 答不上，记 unknown
        self.db.conn.execute("UPDATE function_log SET at = '2026-11-04T12:00:00+00:00'")
        m = mepage.metrics(self.db, DAY)
        self.assertEqual(self.dim(m, "resources")["missed"], 3)
        self.assertTrue(any(s["who"] == "你" and "资源与处境" in s["what"] for s in m["suggestions"]))
        json.dumps(m, ensure_ascii=False, default=str)  # 命令行能打印


if __name__ == "__main__":
    unittest.main()


class VerdictTest(unittest.TestCase):
    """全部记忆里的按钮（2.2）：对 / 删掉；猜测是像我 / 不像我 / 说不清；星图往下钻按细项筛。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        mepage.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        ev = lifebase.add_evidence(self.db, "file", "file:cv", "2026-11-03", text="简历")
        self.school = lifebase.add_fact(self.db, "abilities", "在某理工大学读本科", [ev], prop="studied_at")
        self.skill = lifebase.add_fact(self.db, "abilities", "会 Python", [ev], prop="skill")
        gr = lifebase.add_evidence(self.db, "grok", "grok:1", "2026-11-03", text="她可能喜欢爵士")
        self.guess = lifebase.add_fact(self.db, "preferences", "可能喜欢爵士", [gr], kind="hypothesis", source_kind="agent_relay")

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def test_buttons(self):
        self.assertTrue(mepage.verdict_fact(self.db, self.school, "yes")["ok"])
        self.assertEqual(self.db.conn.execute("SELECT locked FROM facts WHERE id = ?", (self.school,)).fetchone()[0], 1)
        mepage.verdict_fact(self.db, self.guess, "unsure")
        self.assertIsNone(self.db.conn.execute("SELECT valid_to FROM facts WHERE id = ?", (self.guess,)).fetchone()[0])  # 说不清：不改
        mepage.verdict_fact(self.db, self.guess, "unlike")
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (self.guess,)).fetchone()[0], "invalid")
        mepage.verdict_fact(self.db, self.skill, "forget", root=self.root)
        texts = [x["text"] for x in mepage.all_memory(self.db)["items"]]
        self.assertNotIn("会 Python", texts)

    def test_drill_down_by_property(self):
        d = next(x for x in mepage.metrics(self.db, DAY)["dims"] if x["dim"] == "abilities")
        self.assertEqual({p["key"] for p in d["props"]}, {"studied_at", "skill"})
        got = mepage.all_memory(self.db, dim="abilities", prop="studied_at")["items"]
        self.assertEqual([x["text"] for x in got], ["在某理工大学读本科"])


class ManageTest(unittest.TestCase):
    """删除管理和对外表达（2.2 第二层）：删过的只给日期、方面、条数；能放开重新记；停用的能留；对外的一句能放行、撤回。"""

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

    def test_deletions_reimport_suspended_and_disclosure(self):
        a = lifebase.add_evidence(self.db, "chat", "raw:1:1", "2026-11-03", text="我不吃香菜")
        b = lifebase.add_evidence(self.db, "chat", "raw:1:2", "2026-11-03", text="香菜真的不行")
        only = lifebase.add_fact(self.db, "preferences", "不吃香菜", [a])
        both = lifebase.add_fact(self.db, "preferences", "很讨厌香菜", [a, b])
        lifebase.forget(self.db, evidence_ids=[a], root=self.root)
        g = mepage.manage(self.db)
        d = g["deletions"][0]
        self.assertEqual((d["facts"], d["sources"], d["dims"], d["reimport"]), (1, 1, ["偏好与审美"], False))
        self.assertNotIn("香菜", json.dumps(g["deletions"], ensure_ascii=False))  # 删过的不露内容
        self.assertEqual([x["id"] for x in g["suspended"]], [both])
        mepage.allow_reimport_request(self.db, d["id"], root=self.root)
        self.assertTrue(mepage.manage(self.db)["deletions"][0]["reimport"])
        mepage.keep_suspended(self.db, both)
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (both,)).fetchone()[0], "active")
        did = lifebase.approve_disclosure(self.db, "我在 某商学院 做交换生", "我在 某商学院 做交换生")
        self.assertEqual(mepage.manage(self.db)["disclosures"][0]["state"], "有效")
        lifebase.revoke_disclosure(self.db, did)
        self.assertEqual(mepage.manage(self.db)["disclosures"][0]["state"], "撤回了")
        self.assertIsNotNone(only)
