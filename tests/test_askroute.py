"""小伴 聊天按题分流（ontology-plan 3.1）：几种问法命中正确的题号、没命中走兜底、没权限照样当“没有”、提示词里有按题结果。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import askroute, lifebase, memusers
from ethan.core.agent import Ethan
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore


class Backend:
    name = "subscription"

    def __init__(self):
        self.calls = []

    def complete(self, system, messages, images=()):
        self.calls.append(system)
        return Reply("嗯", self.name, "m", 0.0)


class MatchTest(unittest.TestCase):
    def test_phrasings_hit_the_right_question(self):
        cases = {"我现在住哪？": 1, "我在哪": 1, "我最近在忙什么？": 1,
                 "我上次说过搬家的事吗？": 2, "我上次说到签证是什么时候": 2,
                 "小王是谁？": 3, "你知道小王是谁吗": 3, "Amy和我什么关系": 3,
                 "我喜欢吃什么？": 4, "我不吃什么来着？": 4,
                 "我定过哪些规矩？": 5, "你为什么觉得我很焦虑？": 11, "我的签证和上个月比有什么变化？": 18,
                 "我做过什么决定？": 22, "跑步计划是谁改的？": 23, "这周花了多少钱？": 26,
                 "我删过跑步那个待办吗？": 27, "我说过要做但还没做的有哪些？": 28}
        for text, q in cases.items():
            got = askroute.match(text)
            self.assertIsNotNone(got, text)
            self.assertEqual(got[0], q, text)
        self.assertEqual(askroute.match("小王是谁？")[1], "小王")
        self.assertEqual(askroute.match("我上次说过搬家的事吗？")[1], "搬家")

    def test_unsure_is_not_routed(self):
        for text in ("今天好累啊", "我喜欢猫", "我昨天去了公园，好开心", "你是谁？", "他是谁啊", "我在哪里买的那个包？",
                     "我不知道为什么", "", "我上次说" + "很长的一段话" * 20 + "吗？"):
            self.assertIsNone(askroute.match(text), text)

    def test_only_questions_the_user_registered(self):
        for q, *_ in askroute.RULES:
            self.assertIn(q, memusers.USERS["ethan"]["questions"])
        self.assertIsNone(askroute.match("我现在住哪？", user="twin"))  # 主页助手 没登记第 1 题


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.backend = Backend()
        self.ethan = Ethan(self.store, Router(self.backend, Ledger(root / "ledger"), Settings(root / "settings.json")), persona="人设")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def log(self):
        return [tuple(r) for r in self.db.conn.execute("SELECT fn, user, status FROM function_log ORDER BY id")]

    def last_via(self):
        return self.db.conn.execute("SELECT via FROM retrieval_log ORDER BY id DESC LIMIT 1").fetchone()[0]

    def test_routed_turn_puts_the_answer_in_the_prompt_and_logs(self):
        lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:1", "2026-11-01T09:00:00", "mia：我下个月要搬家，搬去 Camden 那边")
        self.ethan.respond("cli", "我上次说过搬家的事吗？", today=date(2026, 11, 4))
        system = self.backend.calls[0]
        self.assertIn("### 按题查到的", system)
        self.assertIn("第 2 题", system)
        self.assertIn("我下个月要搬家", system)
        self.assertIn(("last_said", "ethan", "answer"), self.log())  # 命中的题记 function_log
        self.assertEqual(self.last_via(), "q2")  # 兜底检索照做，记成按题的

    def test_unrouted_turn_is_plain_fallback(self):
        self.ethan.respond("cli", "今天好累啊", today=date(2026, 11, 4))
        self.assertNotIn("按题查到的", self.backend.calls[0])
        self.assertEqual([r for r in self.log() if r[1] == "ethan"], [])
        self.assertIsNone(self.last_via())

    def test_missing_is_said_as_missing(self):
        self.ethan.respond("cli", "小王是谁？", today=date(2026, 11, 4))
        system = self.backend.calls[0]
        self.assertIn("第 3 题", system)
        self.assertIn("记忆里没有", system)
        self.assertIn(("person", "ethan", "unknown"), self.log())

    def test_forbidden_looks_like_missing(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:2", "2026-11-01T09:00:00", "mia：我不吃香菜")
        lifebase.add_fact(self.db, "preferences", "不吃香菜", [ev], kind="preference", source_kind="her_words")
        self.db.conn.execute("UPDATE statements SET read_by = '[\"planner\"]'")  # 只给计划器看
        self.db.conn.commit()
        hit = askroute.route(self.db, "我不吃什么来着？")
        self.assertEqual(hit["q"], 4)
        self.assertEqual(hit["got"]["status"], "unknown")
        self.assertNotIn("香菜", askroute.prompt_text(hit))
        self.assertIn("记忆里没有", askroute.prompt_text(hit))
        self.assertEqual(self.log()[-1], ("preferences", "ethan", "forbidden"))  # 真实情况只在使用记录里
        twin = askroute.ask(self.db, 1, None, user="twin")  # 对外的问第 1 题：没权限，和没有一样
        self.assertEqual((twin["status"], twin["items"]), ("unknown", []))

    def test_answer_carries_basis_and_source(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:3", "2026-11-01T09:00:00", "mia：我喜欢靠窗的座位")
        lifebase.add_fact(self.db, "preferences", "喜欢靠窗的座位", [ev], kind="preference", source_kind="her_words")
        hit = askroute.route(self.db, "我喜欢什么样的座位？")
        text = askroute.prompt_text(hit)
        self.assertIn("喜欢靠窗的座位", text)
        self.assertIn("依据的说法编号", text)
        self.assertIn("来源：her_words", text)


if __name__ == "__main__":
    unittest.main()
