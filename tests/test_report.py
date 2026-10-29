import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan.core.agent import Ethan
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.report import collect_facts, is_overview_request, write_report

TODAY = date(2026, 10, 25)  # 周日


class Backend:
    name = "subscription"

    def __init__(self, text="小宝，这周的总览来啦"):
        self.text, self.seen = text, []

    def complete(self, system, messages, images=()):
        self.seen.append((system, messages))
        return Reply(self.text, self.name, "m", 0.0)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "data")
        db = self.store.db
        long_id = db.add_goal("创业", "long", status="active")
        stage = db.add_goal("跑出第一笔订单", "stage", parent_id=long_id, status="active", next_step="约客户")
        db.update_goal(stage, note="见了两个客户")
        db.add_task("帮她整理个人主页素材", due="2026-11-01", promised_by_ethan=True)
        db.add_learning_item("日语", "お疲れ様", today=TODAY)
        db.upsert_person("小美", birthday="10-28")
        db.add_wish("想要一个个人主页")
        self.backend = Backend()
        router = Router(self.backend, Ledger(self.root / "ledger"), Settings(self.root / "s.json"))
        self.ethan = Ethan(self.store, router, persona="人设")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_overview_words(self):
        for text in ("看看总览", "总览", "/overview", "看看总览～"):
            self.assertTrue(is_overview_request(text), text)
        self.assertFalse(is_overview_request("今天好累"))
        self.assertFalse(is_overview_request("总览这个词怎么说"))

    def test_facts_follow_dashboard(self):
        facts = collect_facts(self.store, TODAY)
        self.assertEqual(facts["本周从"], "2026-10-19")
        self.assertEqual([g["title"] for g in facts["目标"]], ["跑出第一笔订单"])
        self.assertEqual(facts["目标"][0]["本周进展"], ["见了两个客户"])
        self.assertEqual(facts["学习"]["本周新增"][0]["content"], "お疲れ様")
        self.assertEqual(facts["社交"], [{"name": "小美", "reasons": ["birthday_soon"]}])
        self.assertEqual(facts["小伴答应还没做的"][0]["title"], "帮她整理个人主页素材")
        self.assertEqual(facts["待实现的心愿"], ["想要一个个人主页"])
        self.assertEqual(len(facts["区域"]), 7)  # 七块领地都在，和战报同一套算法
        self.assertIn("财务", facts["还没有数据来源的区域"])
        self.assertIn("英语陪练", facts)
        json.dumps(facts, ensure_ascii=False)

    def test_overview_records_both_sides(self):
        text = write_report(self.ethan, "telegram", today=TODAY, asked="看看总览")
        self.assertEqual(text, "小宝，这周的总览来啦")
        system, messages = self.backend.seen[0]
        self.assertIn("跑出第一笔订单", system)
        self.assertIn("不要编", system)
        self.assertEqual(messages, [{"role": "user", "content": "看看总览"}])
        log = self.store.raw.recent(10)
        self.assertEqual([(r["role"], r.get("kind")) for r in log], [("mia", None), ("ethan", "overview")])

    def test_weekly_is_proactive(self):
        write_report(self.ethan, "telegram", today=TODAY, weekly=True)
        system, _ = self.backend.seen[0]
        self.assertIn("周报", system)
        self.assertEqual([r["role"] for r in self.store.raw.recent(10)], ["ethan"])

    def test_long_report_is_cut_for_telegram(self):
        self.backend.text = "很" * 5000
        self.assertLessEqual(len(write_report(self.ethan, "telegram", today=TODAY, weekly=True)), 4000)


if __name__ == "__main__":
    unittest.main()
