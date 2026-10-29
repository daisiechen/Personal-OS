import json
import tempfile
import unittest
from pathlib import Path

from ethan.core.agent import Ethan, build_system_prompt, parse_reply
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.grok import GrokWebhook, build_grok
from ethan.memory import MemoryStore

TASK_REPLY = "好，我让 Grok 去订\n[交给Grok]周六 14:00 两人，订 Dishoom Covent Garden，预算 60 镑[/交给Grok]"


class Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Backend:
    name = "subscription"

    def __init__(self, text):
        self.text, self.systems = text, []

    def complete(self, system, messages, images=()):
        self.systems.append(system)
        return Reply(self.text, self.name, "m", 0.0)


class GrokWebhookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_posts_task_with_secret_and_logs(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            return Resp()

        grok = GrokWebhook("https://grok.example/hook", "s3cret", log_dir=self.root, opener=opener)
        self.assertTrue(grok("订餐厅", "telegram"))
        req = seen[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer s3cret")
        self.assertEqual(json.loads(req.data)["task"], "订餐厅")
        log = [json.loads(l) for l in (self.root / "grok-tasks.jsonl").read_text().splitlines()]
        self.assertEqual((log[0]["ok"], log[0]["channel"]), (True, "telegram"))
        self.assertNotIn("s3cret", (self.root / "grok-tasks.jsonl").read_text())

    def test_custom_header_and_failure_does_not_raise(self):
        seen = []

        def opener(req, timeout):
            seen.append(req)
            raise OSError("connection refused")

        grok = GrokWebhook("https://grok.example/hook", "s3cret", header="X-Webhook-Secret",
                           log_dir=self.root, opener=opener)
        self.assertFalse(grok("订餐厅"))
        self.assertEqual(seen[0].get_header("X-webhook-secret"), "s3cret")
        self.assertIn("connection refused", (self.root / "grok-tasks.jsonl").read_text())

    def test_build_needs_url_and_secret(self):
        self.assertIsNone(build_grok(env={}))
        self.assertIsNone(build_grok(env={"GROK_WEBHOOK_URL": "https://x", "GROK_WEBHOOK_SECRET": ""}))
        self.assertIsNotNone(build_grok(env={"GROK_WEBHOOK_URL": "https://x", "GROK_WEBHOOK_SECRET": "s"}))


class DelegationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.backend = Backend(TASK_REPLY)
        self.router = Router(self.backend, Ledger(root / "ledger"), Settings(root / "settings.json"))
        self.sent = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ethan(self, delegate):
        return Ethan(self.store, self.router, persona="人设", delegate=delegate)

    def test_marker_is_sent_to_grok_and_hidden_from_her(self):
        reply = self.ethan(lambda task, channel: self.sent.append((task, channel)) or True).reply("telegram", "帮我订个餐厅")
        self.assertEqual(self.sent, [("周六 14:00 两人，订 Dishoom Covent Garden，预算 60 镑", "telegram")])
        self.assertEqual(reply["bubbles"], ["好，我让 Grok 去订"])
        self.assertIn("交给Grok", self.backend.systems[0])

    def test_failed_send_is_told_honestly(self):
        reply = self.ethan(lambda task, channel: False).reply("weixin", "帮我订个餐厅")
        self.assertEqual(len(reply["bubbles"]), 2)
        self.assertIn("没发出去", reply["bubbles"][-1])
        self.assertIn("Dishoom", reply["bubbles"][-1])

    def test_without_grok_prompt_does_not_mention_it(self):
        self.assertNotIn("交给Grok", build_system_prompt("人设", {"today": "2026-10-26"}))
        self.assertIn("交给Grok", build_system_prompt("人设", {"today": "2026-10-26"}, can_delegate=True))
        reply = self.ethan(None).reply("telegram", "帮我订个餐厅")
        self.assertIn("没发出去", reply["bubbles"][-1])

    def test_parse_reply_strips_marker(self):
        result = parse_reply("好\n[交给Grok]查一下[/交给Grok]")
        self.assertEqual((result["bubbles"], result["delegated"]), (["好"], ["查一下"]))


if __name__ == "__main__":
    unittest.main()
