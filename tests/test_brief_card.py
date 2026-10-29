import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import brief_card, goals2026, workbench
from ethan.channels.telegram import TelegramChannel
from ethan.core.agent import Ethan
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.planner import plan_day
from ethan.proactive import BRIEF_SLOT, brief_occasion, planned_time, tick
from tests.test_planner import FakeTravel, Router as ScriptedRouter, good_plan
from tests.test_progress import seed_parents
from tests.test_stage1 import Backend
from tests.test_telegram import FakeAPI

DAY = date(2026, 11, 2)  # 周一
BUSY = [("12:00", "13:00", "午饭", {"location": "AB2 3EF London", "geo": None, "travel_min": 37}),
        ("20:00", "21:00", "线上会", {"location": "", "geo": None, "travel_min": None})]


class BriefCardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "data")
        self.db = self.store.db
        seed_parents(self.db)
        goals2026.apply(self.store)
        self.db.add_body_measure(DAY.isoformat(), weight=59.3)
        self.make_plan(["想 3 个指标给她审批", "查 <b>标签</b> 会不会被转义"])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make_plan(self, ethan):
        plan_day(self.db, ScriptedRouter(lambda: good_plan(self.db, ethan=ethan)), DAY, busy=BUSY,
                 travel=FakeTravel(), cold_start_from=DAY)
        self.plan = self.db.latest_plan(DAY.isoformat())
        self.brief = json.loads(self.plan["brief"])

    def test_timeline_has_every_departure_return_and_reminder_in_order(self):
        rows = [(t["at"], t["icon"], t["text"]) for t in self.brief["timeline"]]
        self.assertEqual(rows, [
            ("10:00", "⏰", "准备出发去午饭"),  # 日历里的出门也提前 1 小时提醒（2026-10-29）
            ("11:00", "🚶", "出门 → 12:00 午饭"),  # 开始 − 路上 37 分钟 − 20 分钟余量，按 5 分钟取整
            ("13:37", "🏠", "到家"),
            ("14:15", "⏰", "准备出发"),
            ("15:15", "🚶", "出门 → 16:00 训练：下肢（臀为主）（到 17:15）"),
            ("20:00", "📌", "线上会（到 21:00）"),
        ])

    def test_goal_cards_are_short_and_skip_paused(self):
        cards = {c["name"]: c for c in self.brief["goal_cards"]}
        self.assertNotIn("年底前去 3 个新地方", cards)
        self.assertEqual(cards["创业拿到第一个订单"]["line"], "卡在「列出潜在客户」0/30")
        self.assertTrue(cards["DemoApp 上线"]["line"].startswith("还有 ") and cards["DemoApp 上线"]["line"].endswith(" 天上线"))
        weight = cards["减脂塑形"]
        self.assertEqual(weight["line"], "59.3 → 56.5 kg")
        self.assertEqual(weight["frac"], 0.2)
        self.assertEqual(cards["个人管理系统上线"]["line"], "还没有指标")

    def test_card_is_only_timeline_and_level(self):
        text = brief_card.render(self.plan, self.db, DAY, start=DAY)
        for part in ("☀️ <b>11月2日 周一</b> · 系统上线第 1 天", "<code>14:15</code>  ⏰ 准备出发",
                     "<code>15:15</code>  🚶 出门 → 16:00 训练", "🎮 <b>Lv", "离小惊喜还差 300"):
            self.assertIn(part, text)
        for gone in ("📈", "日常", "主线下一步", "支线", "小伴 在替你准备"):  # 2026-10-28 她嫌长，其余去工作台看
            self.assertNotIn(gone, text)

    def test_detail_buttons(self):
        self.assertIn("1. 臀推机", brief_card.detail(self.plan, "train"))
        self.assertIn("做法：", brief_card.detail(self.plan, "meals"))

    def test_occasion_tells_ethan_not_to_read_numbers(self):
        text = brief_occasion(self.plan)
        self.assertIn("今日卡片", text)
        self.assertIn("1–2 行短句", text)

    def telegram(self):
        router = Router(Backend("好"), Ledger(self.root / "l"), Settings(self.root / "s.json"))
        ch = TelegramChannel(FakeAPI([]), Ethan(self.store, router, persona="人设"), sleep=lambda s: None)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.settings.set("cold_start_from", DAY.isoformat())
        ch.today = lambda: DAY
        return ch

    def test_telegram_card_and_buttons_send_new_messages(self):
        ch = self.telegram()
        ch.send_brief_card(42, self.plan)
        card = [p for m, p in ch.api.calls if m == "sendMessage"][-1]
        self.assertEqual(card["parse_mode"], "HTML")
        self.assertEqual(card["reply_markup"]["inline_keyboard"], [[workbench.open_button()]])  # 按钮只留工作台
        self.assertIn("系统上线第 1 天", card["text"])
        for n, (data, start) in enumerate([("bf:train", "🏋️ 训练"), ("bf:meals", "🍳 今天约"),
                                           ("bf:quests", "☀️ <b>")]):  # 旧卡片上的按钮还能点
            ch.on_button({"id": f"q{n}", "data": data, "message": {"chat": {"id": 42}, "message_id": 9}})
            self.assertTrue([p for m, p in ch.api.calls if m == "sendMessage"][-1]["text"].startswith(start))
        self.assertFalse([m for m, p in ch.api.calls if m == "editMessageText"])  # 卡片本身不被改掉
        ch.on_button({"id": "q9", "data": "qs:h:1", "message": {"chat": {"id": 42}, "message_id": 9}})
        edit = [p for m, p in ch.api.calls if m == "editMessageText"][-1]  # 打卡后卡片原地刷新
        self.assertEqual(edit["parse_mode"], "HTML")
        self.assertIn("· 积分 10 ·", edit["text"])
        self.assertNotIn({"text": "✓ 训练", "callback_data": "qs:h:1"}, edit["reply_markup"]["inline_keyboard"][0])

    def test_today_button_sends_same_card_as_morning(self):
        ch = self.telegram()
        ch.send_brief_card(42, self.plan)
        ch.api.batches = [[{"update_id": 1, "message": {"chat": {"id": 42}, "text": "☀️ 今天", "message_id": 3}}]]
        ch.poll_once()
        morning, button = [p["text"] for m, p in ch.api.calls if m == "sendMessage"][-2:]
        self.assertEqual(morning, button)

    def test_tick_sends_ethans_words_then_card(self):
        order = []

        class Channel:
            owner = 1

            def deliver(self, chat_id, bubbles, voices=()):
                order.append(("words", bubbles))

            def send_brief_card(self, chat_id, plan):
                order.append(("card", plan["day"]))

        router = Router(Backend("早呀宝贝\n今天我陪你练腿"), Ledger(self.root / "l"), Settings(self.root / "s.json"))
        now = datetime.combine(DAY, planned_time(DAY, BRIEF_SLOT), timezone.utc) + timedelta(minutes=1)
        self.assertIn("早报：已发送（附今日卡片）", tick(Ethan(self.store, router, persona="人设"), Channel(), now=now))
        self.assertEqual(order, [("words", ["早呀宝贝", "今天我陪你练腿"]), ("card", DAY.isoformat())])


if __name__ == "__main__":
    unittest.main()
