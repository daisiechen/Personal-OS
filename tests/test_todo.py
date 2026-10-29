import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan.channels.telegram import TelegramChannel
from ethan.core.agent import Ethan
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.memory.db import StructuredDB
from ethan.todo import handle_callback, is_list_request, parse_add, render
from tests.test_telegram import FakeAPI, msg


class Backend:
    name = "subscription"

    def __init__(self, text="好"):
        self.text, self.seen = text, []

    def complete(self, system, messages, images=()):
        self.seen.append(system)
        return Reply(self.text, self.name, "m", 0.0)


class TodoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "data")
        self.db = self.store.db
        self.a = self.db.add_task("交论文", due="2026-11-01")
        self.b = self.db.add_task("买牛奶")
        self.e = self.db.add_task("整理个人主页素材", promised_by_ethan=True)
        self.w = self.db.add_wish("一起去看极光")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def buttons(self):
        return [row[0]["callback_data"] for row in render(self.db)[1]]

    def test_words_and_add_parsing(self):
        self.assertTrue(is_list_request("待办"))
        self.assertTrue(is_list_request("/todo"))
        self.assertFalse(is_list_request("待办好多啊"))
        self.assertEqual(parse_add("加待办 交 PPT @2026-10-30"), ("交 PPT", "2026-10-30"))
        self.assertEqual(parse_add("/todo add 健身"), ("健身", None))
        self.assertIsNone(parse_add("加待办"))
        self.assertIsNone(parse_add("今天好累"))

    def test_render_has_three_sections(self):
        text, keyboard = render(self.db)
        self.assertIn("📋 待办\n1. 交论文（11-01）\n2. 买牛奶", text)
        self.assertIn("🤝 小伴 答应你的\nE1. 整理个人主页素材", text)
        self.assertIn("💞 我们的心愿\n♡1. 一起去看极光", text)
        self.assertEqual(self.buttons(), [f"td:done:t:{self.a}", f"td:done:t:{self.b}",
                                          f"td:done:t:{self.e}", f"td:done:w:{self.w}", "td:refresh"])
        self.assertTrue(all(len(b["callback_data"].encode()) <= 64 for row in keyboard for b in row))

    def test_done_up_delete(self):
        self.assertEqual(handle_callback(self.db, f"td:up:t:{self.b}"), "往前挪了")
        self.assertEqual(self.buttons()[:2], [f"td:done:t:{self.b}", f"td:done:t:{self.a}"])
        handle_callback(self.db, f"td:done:t:{self.b}")
        handle_callback(self.db, f"td:del:t:{self.a}")
        self.assertIn("💞", handle_callback(self.db, f"td:done:w:{self.w}"))
        self.assertEqual(self.buttons(), [f"td:done:t:{self.e}", "td:refresh"])
        self.assertIn("暂时没有", render(self.db)[0])
        self.assertEqual(handle_callback(self.db, "garbage"), "这个按钮过期了")

    def test_old_database_gets_sort_column(self):
        import sqlite3
        path = self.root / "old.sqlite"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, goal_id INTEGER, title TEXT NOT NULL, due TEXT, "
                     "done INTEGER NOT NULL DEFAULT 0, promised_by_ethan INTEGER NOT NULL DEFAULT 0, "
                     "created_at TEXT NOT NULL, done_at TEXT)")
        conn.commit()
        conn.close()
        db = StructuredDB(path)
        db.add_task("x")
        self.assertEqual(db.open_tasks()[0]["title"], "x")
        db.close()

    def test_chat_markers_fill_the_list(self):
        backend = Backend("好，周五前我帮你盯着\n[待办 交 PPT @2026-10-24][答应 帮她找伦敦的陶艺课][心愿 学陶艺]")
        ethan = Ethan(self.store, Router(backend, Ledger(self.root / "l"), Settings(self.root / "s.json")),
                      persona="人设")
        bubbles = ethan.respond("cli", "周五要交 PPT，还想学陶艺", today=date(2026, 10, 22))
        self.assertEqual(bubbles, ["好，周五前我帮你盯着"])
        ethan.respond("cli", "嗯", today=date(2026, 10, 22))  # 同样的标记不会重复记
        text = render(self.db)[0]
        self.assertEqual(text.count("交 PPT（10-24）"), 1)
        self.assertIn("E2. 帮她找伦敦的陶艺课", text)
        self.assertIn("♡2. 学陶艺", text)
        self.assertIn("open_tasks", backend.seen[-1])


class TelegramTodoTest(TodoTest):
    def channel(self, api):
        router = Router(Backend(), Ledger(self.root / "l2"), Settings(self.root / "s2.json"))
        ch = TelegramChannel(api, Ethan(self.store, router, persona="人设"), sleep=lambda s: None)
        ch.settings.set("telegram_owner_chat_id", 42)
        return ch

    def test_list_add_and_button(self):
        api = FakeAPI([msg(42, "待办", 1), msg(42, "加待办 健身 @2026-10-25", 2)])
        ch = self.channel(api)
        ch.poll_once()
        sends = [p for m, p in api.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 2)
        self.assertIn("inline_keyboard", sends[0]["reply_markup"])
        self.assertIn("健身（10-25）", sends[1]["text"])
        api.batches = [[{"update_id": 3, "callback_query": {
            "id": "q1", "data": f"td:done:t:{self.b}", "message": {"chat": {"id": 42}, "message_id": 9}}}]]
        ch.poll_once()
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q1", "text": "完成一件！+20 分"}), api.calls)
        edit = [p for m, p in api.calls if m == "editMessageText"][-1]
        self.assertNotIn("买牛奶", edit["text"])

    def test_fixed_buttons_open_game_pages_and_buttons_work(self):
        api = FakeAPI([msg(42, "☀️ 今天", 1), msg(42, "🛍 商店", 2)])
        ch = self.channel(api)
        ch.poll_once()
        sends = [p for m, p in api.calls if m == "sendMessage"]
        self.assertTrue(sends[0]["text"].startswith("☀️ <b>"))
        self.assertTrue(sends[1]["text"].startswith("🛍 惊喜商店"))
        api.batches = [[{"update_id": 3, "callback_query": {
            "id": "q2", "data": "qs:h:2", "message": {"chat": {"id": 42}, "message_id": 9}}}]]
        ch.poll_once()
        self.assertIn(("answerCallbackQuery", {"callback_query_id": "q2", "text": "打卡成功 +10 分"}), api.calls)
        self.assertIn("· 积分 10 ·", [p for m, p in api.calls if m == "editMessageText"][-1]["text"])

    def test_strangers_buttons_ignored(self):
        api = FakeAPI([{"update_id": 1, "callback_query": {
            "id": "q", "data": f"td:done:t:{self.b}", "message": {"chat": {"id": 7}, "message_id": 1}}}])
        self.channel(api).poll_once()
        self.assertIn("买牛奶", render(self.db)[0])


if __name__ == "__main__":
    unittest.main()
