import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import evals, lifebase, memeval, portrait
from ethan.core.agent import Ethan, build_system_prompt, parse_reply
from ethan.memory import MemoryStore

DAY = date(2026, 10, 30)


class Router:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append((purpose, messages[0]["content"]))
        a = self.answers.pop(0)
        return SimpleNamespace(text=a if isinstance(a, str) else json.dumps(a, ensure_ascii=False))


class PortraitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        portrait.ensure(self.db)
        self.src = lifebase.add_evidence(self.db, "chat", "t:0", "2026-06-01", "mia：测试")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fact(self, dim, text, **kw):
        return lifebase.add_fact(self.db, dim, text, [self.src], **kw)

    def test_nightly_ops_update_invalidate_noop_add(self):
        sleep = self.fact("rhythms", "她通常凌晨一点睡觉", valid_from="2026-06-01")
        knee = self.fact("body", "她膝盖有旧伤", valid_from="2026-03-01")
        cori = self.fact("preferences", "她不吃香菜")
        new = ["她现在十二点前睡觉", "她膝盖伤好了", "她一点香菜都不吃", "她喜欢克莱因蓝"]
        bad = {"ops": [{"op": "UPDATE", "from": "n1", "fact_id": 999, "dimension": "rhythms", "statement": "x"}]}
        good = {"ops": [
            {"op": "UPDATE", "from": "n1", "fact_id": sleep, "dimension": "rhythms", "statement": "她现在十二点前睡觉", "valid_from": "2026-10-01"},
            {"op": "INVALIDATE", "from": "n2", "fact_id": knee},
            {"op": "NOOP", "from": "n3", "fact_id": cori},
            {"op": "ADD", "from": "n4", "dimension": "preferences", "statement": "她喜欢克莱因蓝"}]}
        router = Router(bad, good)
        got = portrait.nightly(self.store, router, DAY, new, "今天聊了作息")
        self.assertEqual({k: got[k] for k in ("ADD", "UPDATE", "INVALIDATE", "NOOP")}, {"ADD": 1, "UPDATE": 1, "INVALIDATE": 1, "NOOP": 1})
        self.assertIn("她通常凌晨一点睡觉", router.calls[0][1])  # 旧事实由检索找出来给模型对照
        old = self.db.conn.execute("SELECT * FROM facts WHERE id = ?", (sleep,)).fetchone()
        self.assertEqual(old["valid_to"], "2026-10-01")
        self.assertIsNotNone(old["superseded_by"])
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (knee,)).fetchone()[0], "invalid")
        self.assertEqual(len(json.loads(self.db.conn.execute("SELECT evidence_ids FROM facts WHERE id = ?", (cori,)).fetchone()[0])), 2)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 5)  # 什么都不删
        text = portrait.chat_text(self.db, "ethan")
        self.assertIn("#### 时间与习惯\n- 她现在十二点前睡觉", text)
        self.assertNotIn("凌晨一点", text)
        self.assertNotIn("膝盖", text)

    def test_blocks_condense_with_check_and_fallback(self):
        for i in range(40):
            self.fact("preferences", f"她喜欢第 {i} 种很长很长很长的东西", importance=5 if i == 7 else 3)
        ids = [r[0] for r in self.db.conn.execute("SELECT id FROM facts ORDER BY id")]
        miss = {"summary": "- 她喜欢很多东西", "used_ids": ids[:3]}  # 漏了重要的
        ok = {"summary": "- 她喜欢很多东西，尤其第 7 种", "used_ids": ids}
        router = Router(miss, ok)
        self.assertEqual(portrait.rebuild_blocks(self.db, router), 1)
        self.assertIn("重要的事实没用上", router.calls[1][1])
        self.assertEqual(portrait.chat_text(self.db, "ethan"), "#### 偏好与审美\n- 她喜欢很多东西，尤其第 7 种")
        self.assertEqual(portrait.rebuild_blocks(self.db, Router()), 0)  # 没变化不重写
        portrait.rebuild_blocks(self.db, Router(miss, miss), force=True)  # 两次都不合格：照列，宁长不丢
        self.assertIn("第 39 种", portrait.chat_text(self.db, "ethan"))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM profile_history").fetchone()[0], 2)

    def test_weekly_review(self):
        coffee_old = self.fact("preferences", "她不喝咖啡", valid_from="2026-01-01")
        coffee_new = self.fact("preferences", "她每天早上喝拿铁", valid_from="2026-09-01")
        misplaced = self.fact("abilities", "她每天七点起床")
        stale = self.fact("values", "她想当老师")
        self.db.conn.execute("UPDATE facts SET recorded_at = '2025-01-01', last_confirmed_at = NULL WHERE id = ?", (stale,))
        data = {"contradictions": [{"old_id": coffee_new, "new_id": coffee_old, "reason": "矛盾"}],  # 模型把新旧说反了
                "reclassify": [{"fact_id": misplaced, "dimension": "rhythms"}],
                "questions": [{"fact_id": stale, "question": "你现在还想当老师吗？"}]}
        got = portrait.weekly(self.store, Router(data), DAY)
        self.assertEqual((got["decayed"], got["invalidated"], got["reclassified"], got["questions"]), (1, 1, 1, 1))
        self.assertIsNotNone(self.db.conn.execute("SELECT valid_to FROM facts WHERE id = ?", (coffee_old,)).fetchone()[0])  # 作废较早的
        self.assertIsNone(self.db.conn.execute("SELECT valid_to FROM facts WHERE id = ?", (coffee_new,)).fetchone()[0])
        self.assertEqual(self.db.conn.execute("SELECT dimension FROM facts WHERE id = ?", (misplaced,)).fetchone()[0], "rhythms")
        self.assertLess(self.db.conn.execute("SELECT confidence FROM facts WHERE id = ?", (stale,)).fetchone()[0], 0.6)
        from ethan import mepage
        q = mepage.pending_items(self.db)[0]  # 确认问题进“待你定”，和工作台同一份（2026-10-31）
        self.assertEqual((q["kind"], q["question"], q["options"][0]["fact_id"]), ("question", "你现在还想当老师吗？", stale))
        mepage.resolve(self.db, q["id"], "yes")
        self.assertEqual(self.db.conn.execute("SELECT locked FROM facts WHERE id = ?", (stale,)).fetchone()[0], 1)
        self.assertEqual(memeval.weekly_summary(self.db, DAY)["她确认过的"], 1)

    def test_agent_uses_profile_instead_of_nightly_section(self):
        self.store.wiki.write("about-mia", "关于 Mia", "她在伦敦读书。\n\n## 小伴 每晚整理\n\n### 饮食\n- 旧的整理")
        self.fact("preferences", "她不吃香菜")
        portrait.rebuild_blocks(self.db, None)
        portrait.ensure(self.db)
        self.db.conn.execute("INSERT INTO profile_questions (question, created_at) VALUES ('你还在学游泳吗？', '2026-10-30')")  # 旧线上的：挪进待你定
        from ethan import mepage
        pid = mepage.pending_items(self.db)[0]["id"]
        systems = []

        def complete(system, messages, purpose="chat", images=()):
            systems.append(system)
            return SimpleNamespace(text=f"对了你还在学游泳吗 [待定 #{pid} 问了]", backend="x", model="m", cost_usd=0)
        ethan = Ethan(self.store, SimpleNamespace(complete=complete, settings=None), persona="人设")
        out = ethan.reply("cli", "晚上吃什么好", today=DAY)
        self.assertIn("她在伦敦读书", systems[0])
        self.assertNotIn("旧的整理", systems[0])
        self.assertIn("- 她不吃香菜", systems[0])
        self.assertIn(f"#{pid} [长期] 你还在学游泳吗？", systems[0])
        self.assertEqual(out["bubbles"], ["对了你还在学游泳吗"])
        self.assertEqual(mepage.chat_items(self.db), [])  # 问过了，三天内不再问
        self.assertEqual(mepage.pending_count(self.db), 1)  # 她还没答，待你定里还在


    def test_agent_skips_about_page_once_head_migrated(self):
        """2026-10-31 收口：她手写的开头迁成事实后，小伴 只读画像，不再整页读“关于她”。"""
        from ethan import lifebase
        self.store.wiki.write("about-mia", "关于 Mia", "## 身体\n- 膝盖不太好\n\n## 小伴 每晚整理\n\n### 饮食\n- 旧的整理")
        lifebase.backfill(self.store)
        portrait.rebuild_blocks(self.db, None)
        systems = []

        def complete(system, messages, purpose="chat", images=()):
            systems.append(system)
            return SimpleNamespace(text="好", backend="x", model="m", cost_usd=0)
        Ethan(self.store, SimpleNamespace(complete=complete, settings=None), persona="人设").reply("cli", "晚上吃什么好", today=DAY)
        self.assertNotIn("### 关于 Mia", systems[0])
        self.assertIn("膝盖不太好", systems[0])  # 从画像里来


class FactOpsEvalTest(unittest.TestCase):
    def test_scorer_and_runner(self):
        cases = {c["id"]: c for c in evals.load("fact_ops")}
        self.assertGreaterEqual(len(cases), 30)
        score = evals._score_fact_ops
        self.assertTrue(score({"ops": [{"from": "n1", "op": "UPDATE", "fact_id": 1, "dimension": "rhythms"}]}, cases["update-sleep"]["expect"])[0])
        self.assertFalse(score({"ops": [{"from": "n1", "op": "ADD", "dimension": "rhythms"}]}, cases["update-sleep"]["expect"])[0])
        self.assertFalse(score({"ops": [{"from": "n1", "op": "UPDATE", "fact_id": 4, "dimension": "rhythms"}]}, cases["update-sleep"]["expect"])[0])
        self.assertTrue(score({"ops": []}, cases["transient"]["expect"])[0])
        self.assertFalse(score({"ops": [{"from": "n1", "op": "ADD", "dimension": "body"}]}, cases["transient"]["expect"])[0])
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            out = evals._fact_ops(Router({"ops": [{"from": "n1", "op": "NOOP", "fact_id": 3}]}), store.db, cases["noop-same"])
            store.close()
        self.assertTrue(score(out, cases["noop-same"]["expect"])[0])


if __name__ == "__main__":
    unittest.main()
