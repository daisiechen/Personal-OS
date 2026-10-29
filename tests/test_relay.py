import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import relay
from ethan.channels.telegram import TelegramChannel
from ethan.core.agent import Ethan, build_system_prompt
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from tests.test_stage1 import Backend
from tests.test_telegram import FakeAPI

NOW = datetime(2026, 10, 27, 12, 20, tzinfo=timezone.utc)


def write(root, tasks, results):
    with (root / "grok-tasks.jsonl").open("w") as f:
        for at, channel, task in tasks:
            f.write(json.dumps({"at": at, "channel": channel, "task": task, "ok": True}) + "\n")
    (root / "inbox").mkdir(exist_ok=True)
    with (root / "inbox" / "2026-10-27.jsonl").open("w") as f:
        for at, text in results:
            f.write(json.dumps({"at": at, "path": "/grok/result", "label": "Grok Bot 办完", "text": text},
                               ensure_ascii=False) + "\n")


class RelayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_results_of_chat_tasks_and_no_surprise_spoilers(self):
        write(self.root,
              [("2026-10-27T00:04:53+00:00", "planner", "惊喜调研"), ("2026-10-27T12:11:58+00:00", "telegram", "翻小红书评论")],
              [("2026-10-27T00:37:05+00:00", "【惊喜灵感调研·完整版】不能剧透"),
               ("2026-10-27T12:13:31+00:00", "【小红书评论整理｜部分完成】175 条评论"),
               ("2026-10-27T12:15:00+00:00", "【惊喜灵感】也不能转告")])
        got = relay.pending(self.root, NOW)
        self.assertEqual([(r["at"], r["task"]) for r in got], [("2026-10-27T12:13:31+00:00", "翻小红书评论")])
        relay.mark(self.root, got[-1]["at"])
        self.assertEqual(relay.pending(self.root, NOW), [])
        text = relay.occasion(got)
        self.assertIn("不用提 Grok 怎么办的", text)
        self.assertIn("175 条评论", text)

    def test_dev_session_reports_are_never_relayed_to_her(self):
        write(self.root, [("2026-10-27T12:11:58+00:00", "telegram", "翻小红书评论")],
              [("2026-10-27T12:15:00+00:00", "【Claude 会话】\n我的虚拟形象的来历")])
        self.assertEqual(relay.pending(self.root, NOW), [])

    def test_result_long_after_the_task_is_not_relayed(self):
        write(self.root, [("2026-10-26T20:00:00+00:00", "telegram", "订餐厅")],
              [("2026-10-27T12:13:31+00:00", "【回报】和那件事无关")])
        self.assertEqual(relay.pending(self.root, NOW), [])

    def test_prompt_says_results_come_back_here(self):
        prompt = build_system_prompt("人设", {"today": "2026-10-27"}, can_delegate=True)
        self.assertIn("你再在这里用自己的话告诉她", prompt)
        self.assertIn("非必要不跟她提", prompt)
        self.assertNotIn("在 Grok 那边告诉她", prompt)

    def test_telegram_tells_her_in_ethans_words(self):
        store = MemoryStore(self.root / "data")
        write(store.root, [("2026-10-27T12:11:58+00:00", "telegram", "翻小红书评论")],
              [("2026-10-27T12:13:31+00:00", "【小红书评论整理】大多在说是 AI 造谣")])
        backend = Backend("翻完啦\n大家都在吵这是不是 AI 造谣")
        router = Router(backend, Ledger(self.root / "l"), Settings(self.root / "s.json"))
        ch = TelegramChannel(FakeAPI([]), Ethan(store, router, persona="人设"), sleep=lambda s: None)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.relay_grok(now=NOW)
        self.assertEqual(ch.api.sent_texts(), ["翻完啦", "大家都在吵这是不是 AI 造谣"])
        ch.relay_grok(now=NOW)  # 不会重复转告
        self.assertEqual(len(ch.api.sent_texts()), 2)
        store.close()


if __name__ == "__main__":
    unittest.main()
