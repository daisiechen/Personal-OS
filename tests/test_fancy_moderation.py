import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ethan import relay
from ethan.fancy_moderation import LOG, message, telegram_notifier, validate
from ethan.inbox import Inbox, recent
from ethan.memory import MemoryStore

FANCY = "f" * 43
AUTH = {"Authorization": f"Bearer {FANCY}"}
GROK = "g" * 43
CLOUD = "c" * 43
NOW = datetime(2026, 10, 29, 8, 0, tzinfo=timezone.utc)
URL = "https://admin.demoapp.example/moderation"


def alert(**over):
    body = {"kind": "morning", "sentAt": "2026-10-29T08:00:00Z", "open": 3, "autoLimited": 1, "overdue": 0,
            "dueWithin6h": 0, "oldestHoursLeft": 5.2, "urgentOpen": 0, "reason": None, "count": 0, "adminUrl": URL}
    body.update(over)
    return body


class FakeTelegram:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def __call__(self, text):
        self.sent.append(text)
        return self.ok


class DemoAppModerationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "data")
        self.tg = FakeTelegram()
        self.inbox = Inbox(self.store, {"grok": GROK, "cloud": CLOUD, "fancy": FANCY}, notify=self.tg)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, body=None, headers=AUTH, now=NOW, raw=None):
        data = raw if raw is not None else json.dumps(alert() if body is None else body).encode()
        return self.inbox.handle("/fancy/moderation", headers, data, now=now)

    def log(self):
        path = Path(self.store.root) / LOG
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    # 鉴权
    def test_only_fancy_token_is_accepted(self):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": f"Bearer {GROK}"},
                        {"Authorization": f"Bearer {CLOUD}"}, {"Authorization": FANCY}):
            self.assertEqual(self.post(headers=headers)[0], 401)
        no_key = Inbox(self.store, {"grok": GROK}, notify=self.tg)
        self.assertEqual(no_key.handle("/fancy/moderation", AUTH, json.dumps(alert()).encode(), now=NOW)[0], 401)
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.log(), [])
        # DemoApp 的钥匙也进不了别的口
        self.assertEqual(self.inbox.handle("/grok/result", AUTH, b"hi", now=NOW)[0], 401)
        self.assertEqual(self.inbox.handle("/cloud/grok", AUTH, b'{"task": "x"}', now=NOW)[0], 401)
        self.assertEqual(self.inbox.handle_get("/cloud/grok/results", AUTH)[0], 401)

    # 各 kind 的说法
    def test_morning_message(self):
        status, reply = self.post()
        self.assertEqual((status, reply["sent"]), (200, True))
        self.assertEqual(self.tg.sent, [f"DemoApp 有 3 条举报待看（1 条已自动限制），最早一条还剩 5 小时。\n→ {URL}"])

    def test_morning_variants(self):
        self.assertEqual(message(validate(alert(autoLimited=0, oldestHoursLeft=None))), f"DemoApp 有 3 条举报待看。\n→ {URL}")
        self.assertIn("最早一条还剩 不到 1 小时", message(validate(alert(oldestHoursLeft=0.4))))
        self.assertIn("2 条已经超过 24 小时", message(validate(alert(overdue=2, oldestHoursLeft=-3))))
        self.assertEqual(message(validate(alert(open=0))), "")  # 没有待看的不打扰她

    def test_evening_message(self):
        self.post(alert(kind="evening", dueWithin6h=2, overdue=1))
        self.assertEqual(self.tg.sent, [f"DemoApp 还有 2 条举报快到 24 小时（1 条已超时）。\n→ {URL}"])
        self.assertEqual(message(validate(alert(kind="evening", dueWithin6h=2, overdue=0))),
                         f"DemoApp 还有 2 条举报快到 24 小时。\n→ {URL}")
        self.assertIn("1 条举报已经超过 24 小时", message(validate(alert(kind="evening", dueWithin6h=0, overdue=1))))
        status, reply = self.post(alert(kind="evening", dueWithin6h=0, overdue=0), now=NOW + timedelta(hours=1))
        self.assertEqual((status, reply["sent"]), (200, False))
        self.assertEqual(len(self.tg.sent), 1)

    def test_urgent_message(self):
        self.post(alert(kind="urgent", reason="safety", count=1, urgentOpen=1))
        self.assertEqual(self.tg.sent, [f"DemoApp 收到一条紧急举报（人身安全），请尽快看。\n→ {URL}"])
        self.assertEqual(message(validate(alert(kind="urgent", reason="harassment", count=3))),
                         f"DemoApp 收到 3 条紧急举报（骚扰或仇恨），请尽快看。\n→ {URL}")
        self.assertIn("（疑似未满 18 岁）", message(validate(alert(kind="urgent", reason="under_18", count=1))))
        self.assertIn("收到一条紧急举报，请尽快看", message(validate(alert(kind="urgent", count=1))))

    # 校验
    def test_rejects_bad_bodies(self):
        bad = [alert(kind="noon"), alert(kind=None), alert(sentAt="yesterday"), alert(sentAt=123),
               alert(open="3"), alert(open=-1), alert(open=True), alert(open=1.5), alert(count=10 ** 9),
               alert(oldestHoursLeft="5"), alert(oldestHoursLeft=True), alert(oldestHoursLeft=float("inf")),
               alert(reason="spam"), alert(reason=["safety"]), alert(adminUrl="http://demoapp.example/a"),
               alert(adminUrl="https://evil.com/demoapp.example"), alert(adminUrl="https://demoapp.example.evil.com/"),
               alert(adminUrl="https://evildemoapp.example/"), alert(adminUrl="https://user@demoapp.example/"),
               alert(adminUrl="https://demoapp.example/" + "a" * 3000), alert(adminUrl="https://demoapp.example/a b"),
               alert(adminUrl="javascript:alert(1)"), alert(adminUrl=None), [alert()], "hi"]
        for missing in ("kind", "sentAt", "open", "oldestHoursLeft", "reason", "adminUrl"):
            body = alert()
            del body[missing]
            bad.append(body)
        for body in bad:
            self.assertEqual(self.post(body)[0], 400, body)
        for raw in (b"", b"not json", b"\xff\xfe"):
            self.assertEqual(self.post(raw=raw)[0], 400)
        self.assertEqual(self.post(raw=b"x" * (64 * 1024 + 1))[0], 413)
        self.assertEqual(self.tg.sent, [])

    def test_accepts_subdomain_unknown_fields_and_float_hours(self):
        body = alert(adminUrl="https://demoapp.example/admin?tab=reports", extra={"note": "ignored"}, oldestHoursLeft=7)
        self.assertEqual(self.post(body)[0], 200)
        self.assertEqual(validate(alert(adminUrl="https://a.b.demoapp.example/x"))["adminUrl"], "https://a.b.demoapp.example/x")
        self.assertNotIn("extra", validate(body))

    # 去重
    def test_same_kind_within_10_minutes_sent_once(self):
        self.assertTrue(self.post()[1]["sent"])
        self.assertFalse(self.post(now=NOW + timedelta(minutes=9))[1]["sent"])
        self.assertTrue(self.post(alert(kind="evening", dueWithin6h=1), now=NOW + timedelta(minutes=9))[1]["sent"])
        self.assertTrue(self.post(now=NOW + timedelta(minutes=11))[1]["sent"])
        self.assertEqual(len(self.tg.sent), 3)

    def test_urgent_merged_by_count(self):
        self.post(alert(kind="urgent", reason="safety", count=1))
        self.assertFalse(self.post(alert(kind="urgent", reason="safety", count=1), now=NOW + timedelta(minutes=2))[1]["sent"])
        self.assertTrue(self.post(alert(kind="urgent", reason="harassment", count=2), now=NOW + timedelta(minutes=4))[1]["sent"])
        self.assertIn("2 条紧急举报", self.tg.sent[-1])
        self.assertFalse(self.post(alert(kind="urgent", count=2), now=NOW + timedelta(minutes=9))[1]["sent"])
        # 窗口从第一条算起：第 11 分钟再来就是新的一批
        self.assertTrue(self.post(alert(kind="urgent", count=1), now=NOW + timedelta(minutes=11))[1]["sent"])
        self.assertEqual(len(self.tg.sent), 3)

    def test_failed_send_is_not_deduped(self):
        self.tg.ok = False
        self.assertEqual(self.post()[0], 502)
        self.tg.ok = True
        self.assertTrue(self.post(now=NOW + timedelta(minutes=1))[1]["sent"])  # DemoApp 重试能送到
        offline = Inbox(self.store, {"fancy": FANCY})
        self.assertEqual(offline.handle("/fancy/moderation", AUTH, json.dumps(alert(kind="evening", dueWithin6h=1)).encode(),
                                        now=NOW)[0], 503)

    # 不进记忆
    def test_not_in_memory_wiki_inbox_or_chat(self):
        self.post(alert(kind="urgent", reason="under_18", count=1))
        self.post()
        self.assertEqual(recent(self.store.root, now=NOW), [])  # 收件箱没有：聊天上下文、relay、brain 都看不到
        self.assertFalse((Path(self.store.root) / "inbox").exists())
        self.assertEqual(relay.pending(self.store.root, now=NOW), [])
        self.assertEqual(self.store.wiki.pages(), [])  # Wiki 没有，ethan-vault 也就没有
        self.assertEqual(self.store.raw.recent(50), [])  # 聊天原文没有
        rows = self.log()
        self.assertEqual([r["kind"] for r in rows], ["urgent", "morning"])
        self.assertEqual(set(rows[0]), {"at", "kind", "sentAt", "open", "autoLimited", "overdue", "dueWithin6h",
                                        "urgentOpen", "count", "oldestHoursLeft", "result"})
        self.assertNotIn("demoapp", (Path(self.store.root) / LOG).read_text())  # 日志不记链接

    def test_telegram_notifier_reads_owner_and_skips_without_binding(self):
        calls = []

        class FakeAPI:
            def call(self, method, timeout=70, **params):
                calls.append((method, params))
                return {}

        root = Path(self.store.root)
        self.assertIsNone(telegram_notifier(root, token=""))
        send = telegram_notifier(root, token="123:abc", api=FakeAPI())
        self.assertFalse(send("hi"))  # 还没绑定 Telegram
        (root / "settings.json").write_text(json.dumps({"telegram_owner_chat_id": 42}))
        self.assertTrue(send("DemoApp 有 1 条举报待看。"))
        self.assertEqual(calls, [("sendMessage", {"chat_id": 42, "text": "DemoApp 有 1 条举报待看。",
                                                  "disable_web_page_preview": True})])

        class BrokenAPI:
            def call(self, *a, **k):
                raise RuntimeError("Telegram sendMessage 失败")

        self.assertFalse(telegram_notifier(root, token="123:abc", api=BrokenAPI())("hi"))

if __name__ == "__main__":
    unittest.main()
