import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import desk, mail, publicsite
from ethan.memory import MemoryStore
from ethan.workbench.api import Workbench
from ethan.core.router import Settings


def router(answer):
    def complete(system, messages, purpose="chat", images=()):
        if purpose == "translate":
            return SimpleNamespace(text="（中文）" + messages[0]["content"][:30])
        return SimpleNamespace(text=answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False))
    return SimpleNamespace(complete=complete)


ATT = {"category": "work", "attention": True, "reason": "等你回复", "todo": None, "event": None}


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append(msg)


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add_mail(self, i, sender="Mazen <m@london.edu>"):
        mail.receive(self.db, {"items": [{"account": "Gmail", "id": f"<m{i}@x>", "sender": sender, "subject": f"Re: 报告 {i}",
                                          "received_at": f"2026-10-30T1{i}:00:00", "body": "能周五前发我你的部分吗？"}]})
        mail.process(self.store, router(ATT))

    def test_list_todo_ignore_and_learn_sender(self):
        self.add_mail(1)
        self.add_mail(2)
        publicsite.receive(self.db, {"visitor_id": "v", "at": "t", "name": "Lena", "intent": "合作", "message": "想合作", "contact": "l@x.com"},
                           datetime.now(timezone.utc))
        publicsite.triage(self.db, router({"items": [{"id": 1, "tier": "reply", "priority": 4, "summary": "Lena 想合作"}]}), "2026-10-30")
        self.assertEqual([x["kind"] for x in desk.items(self.db)], ["mail", "mail"])  # Dais的草稿没好，留言先不给她看（2026-10-31 她定）
        publicsite.reply_draft(self.db, {"request_id": "v1-1", "channel": "manual", "body": "谢谢，我们聊聊"})
        items = desk.items(self.db)
        v = next(x for x in items if x["kind"] == "visitor")
        self.assertEqual((v["state"], v["draft"]), ("draft", "谢谢，我们聊聊"))  # 留言和草稿一起出现
        self.assertEqual(desk.count(self.db), 3)
        desk.todo(self.db, "visitor", v["id"], "下周回")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type = 'visitor'").fetchone()[0], 1)  # 分档时已建的不重复
        m1, m2 = [x for x in items if x["kind"] == "mail"]
        desk.ignore(self.db, "mail", m1["id"])
        self.assertFalse(desk.quiet_sender(self.db, "m@london.edu"))
        desk.ignore(self.db, "mail", m2["id"])
        self.assertTrue(desk.quiet_sender(self.db, "Mazen <m@london.edu>"))
        self.add_mail(3)  # 她连着两次不用管：以后不算要她看
        self.assertEqual(desk.items(self.db), [])

    def test_draft_and_send_only_on_click(self):
        self.add_mail(1)
        rid = desk.items(self.db)[0]["id"]
        kicked = []
        desk.ask_draft(self.db, "mail", rid, "说周五前发", kick=kicked.append)
        self.assertEqual((kicked, desk.items(self.db)[0]["state"]), ([rid], "drafting"))
        text = desk.write_draft(self.store, router("Hi Mazen, I'll send it by Friday.\nMia"), rid)
        self.assertIn("Friday", text)
        x = desk.items(self.db)[0]
        self.assertEqual(x["state"], "draft")
        self.assertTrue(x["draft_zh"].startswith("（中文）Hi Mazen"))  # 草稿也一起译好，她默认看中文
        self.assertTrue(x["is_zh"])  # 中文来信不出“看原文/看翻译”
        acc = [{"name": "Gmail", "user": "d@gmail.com", "host": "imap.gmail.com", "password": "x"}]
        FakeSMTP.sent = []
        out = desk.send(self.db, rid, "Hi Mazen, by Friday. Mia", accounts=acc, smtp_factory=FakeSMTP)
        self.assertTrue(out["ok"])
        msg = FakeSMTP.sent[0]
        self.assertEqual((msg["To"], msg["Subject"], msg["In-Reply-To"]), ("Mazen <m@london.edu>", "Re: 报告 1", "<m1@x>"))
        # 发了还留到今天结束，能再记个待办跟进（这条照样在“已发送”），点“收起”才走
        x = desk.items(self.db)[0]
        self.assertEqual((x["state"], x["draft"]), ("sent", "Hi Mazen, by Friday. Mia"))
        desk.todo(self.db, "mail", rid)
        self.assertEqual(desk.items(self.db)[0]["state"], "sent")
        self.assertTrue(self.db.conn.execute("SELECT title FROM tasks").fetchone()[0].startswith("跟进 Mazen"))
        self.db.conn.execute("UPDATE mail_items SET sent_at = '2026-01-01T10:00:00'")  # 过了当天就不再显示
        self.assertEqual(desk.items(self.db), [])
        self.assertFalse(desk.send(self.db, rid, "x", accounts=[], smtp_factory=FakeSMTP)["ok"])

    def test_visitor_reply_through_site_relay(self):
        from ethan.inbox import Inbox
        site = "s" * 40
        inbox = Inbox(self.store, {"cloud": "c" * 40, "site": site})
        h = {"Authorization": f"Bearer {site}"}
        now = datetime.now(timezone.utc)
        for vid, extra in (("v1", {"email": "amy@x.com", "message_id": "m1", "source": "home"}), ("v2", {"contact": "微信 abc"}), ("v3", {})):
            inbox.handle("/cloud/visitor", h, json.dumps({"visitor_id": vid, "at": "t", "name": vid, "intent": "其他", "message": "Love the site!",
                                                          **extra}).encode(), now)
        publicsite.triage(self.db, router({"items": [{"id": i, "tier": "fyi", "summary": f"夸主页 {i}"} for i in (1, 2, 3)]}), "2026-10-30")
        self.assertFalse(desk.ask_draft(self.db, "visitor", 3)["ok"])  # 没留联系方式，回不了
        got = inbox.handle_get("/cloud/site/reply-requests?status=pending", h)[1]["items"]  # 分档后能回的自动排队起草
        self.assertEqual([(x["request_id"], x["email"], x["instruction"]) for x in got], [("v1-1", "amy@x.com", ""), ("v2-1", "", "")])
        self.assertEqual(got[0]["message_id"], "m1")
        self.assertEqual(inbox.handle_get("/cloud/site/reply-requests?status=pending", {})[0], 401)
        desk.ask_draft(self.db, "visitor", 1, "再热情一点")  # 重写：换一个 request_id，旧的草稿不认
        inbox.handle("/cloud/site/reply-drafts", h, json.dumps({"request_id": "v1-1", "channel": "email", "subject": "Hi", "body": "old"}).encode())
        self.assertEqual(self.db.conn.execute("SELECT desk_state FROM visitor_messages WHERE id = 1").fetchone()[0], "handed")
        for rid, ch in (("v1-2", "email"), ("v2-1", "email")):  # 没邮箱的就算中转说 email 也按手动
            self.assertEqual(inbox.handle("/cloud/site/reply-drafts", h, json.dumps({"request_id": rid, "channel": ch, "subject": "Hi",
                                                                                     "body": "Thanks so much!"}).encode())[0], 200)
        x1, x2 = sorted([x for x in desk.items(self.db) if x["kind"] == "visitor" and x["id"] in (1, 2)], key=lambda x: x["id"])
        self.assertEqual((x1["state"], x1["channel"], x2["channel"]), ("draft", "email", "manual"))
        self.assertEqual(inbox.handle_get("/cloud/site/reply-requests?status=approved", h)[1]["items"], [])  # 她没点发送，什么都不发
        self.assertFalse(desk.send(self.db, 2, "x", kind="visitor")["ok"])  # 手动的不经中转
        self.assertTrue(desk.send(self.db, 1, "Thanks so much, Amy!", kind="visitor")["ok"])
        got = inbox.handle_get("/cloud/site/reply-requests?status=approved", h)[1]["items"]
        self.assertEqual(got, [{"request_id": "v1-2", "message_id": "m1", "email": "amy@x.com", "subject": "Hi", "body": "Thanks so much, Amy!"}])
        inbox.handle("/cloud/site/reply-status", h, json.dumps({"request_id": "v1-2", "status": "failed", "error": "bounce"}).encode())
        x1 = next(x for x in desk.items(self.db) if x["kind"] == "visitor" and x["id"] == 1)
        self.assertEqual((x1["state"], x1["error"]), ("draft", "bounce"))  # 回到草稿，能再发
        desk.send(self.db, 1, "Thanks so much, Amy!", kind="visitor")
        inbox.handle("/cloud/site/reply-status", h, json.dumps({"request_id": "v1-2", "status": "sent"}).encode())
        desk.replied(self.db, "visitor", 2)
        states = {x["id"]: x["state"] for x in desk.items(self.db) if x["kind"] == "visitor"}
        self.assertEqual((states[1], states[2]), ("sent", "sent"))

    def test_workbench_wiring(self):
        self.add_mail(1)
        wb = Workbench(self.store, Settings(Path(self.tmp.name) / "s.json"))
        self.assertEqual(len(wb.desk_page()["items"]), 1)
        self.assertEqual(wb.badges(datetime(2026, 10, 30).date())["desk"], 1)
        rid = wb.desk_page()["items"][0]["id"]
        self.assertTrue(wb.act("desk.todo", {"kind": "mail", "id": rid, "note": "周五前"})["ok"])
        self.assertIn("（周五前）", self.db.conn.execute("SELECT title FROM tasks").fetchone()[0])
        self.assertFalse(wb.act("desk.todo", {"kind": "nope", "id": 1})["ok"])


class TranslateTest(unittest.TestCase):
    def test_translate_caches_body_and_draft_and_skips_chinese(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            db = store.db
            mail.receive(db, {"items": [{"account": "G", "id": "<z@x>", "sender": "A <a@x>", "subject": "Hi", "received_at": "", "body": "Could you send it by Friday?"}]})
            mail.process(store, router(ATT))
            x = desk.items(db)[0]
            self.assertEqual((x["is_zh"], x["zh"]), (False, "（中文）Could you send it by Friday?"))  # 收信时就译好
            calls = []

            def complete(system, messages, purpose="chat", images=()):
                calls.append(purpose)
                return SimpleNamespace(text="好的，周五前发你。")
            r = SimpleNamespace(complete=complete)
            self.assertEqual(desk.translate(db, r, "mail", 1, "body")["zh"], x["zh"])
            self.assertEqual(calls, [])  # 原文只译一次
            db.conn.execute("UPDATE mail_items SET draft = 'Sure, by Friday.', draft_zh = '好，周五前。' WHERE id = 1")
            self.assertEqual(desk.translate(db, r, "mail", 1, "draft", "Sure, by Friday.")["zh"], "好，周五前。")
            self.assertEqual(desk.translate(db, r, "mail", 1, "draft", "Sure, by Friday!! Mia")["zh"], "好的，周五前发你。")  # 她改过就重译
            self.assertEqual(calls, ["translate"])
            self.assertEqual(desk.translate(db, r, "mail", 1, "draft", "好的，周五前发你")["zh"], "好的，周五前发你")  # 已经是中文不译
            self.assertFalse(desk.translate(db, None, "mail", 1, "draft", "Something new")["ok"])
            store.close()

    def test_unreadable_mail_waits_for_her(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            mail.receive(store.db, {"items": [{"account": "G", "id": "G-uid-1-9", "sender": mail.UNREADABLE, "subject": "一封解析不了的邮件（ValueError）",
                                               "received_at": "", "body": ""}]})
            calls = []
            mail.process(store, SimpleNamespace(complete=lambda *a, **k: calls.append(1)))
            self.assertEqual(calls, [])  # 不调模型去猜
            x = desk.items(store.db)[0]
            self.assertIn("邮件 App", x["why"])
            self.assertEqual(mail.action(store.db.conn.execute("SELECT * FROM mail_items").fetchone())["keep"], True)  # 留在收件箱不动
            store.close()


if __name__ == "__main__":
    unittest.main()


class OutboxTest(unittest.TestCase):
    """小伴 起草的新邮件：她在工作台选邮箱点发送才发（2026-10-31 她：“邮件用新流程，从工作台发”）。"""

    def test_ethan_writes_she_sends(self):
        from ethan.core.agent import parse_reply
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            db = store.db
            reply = ("写好了，你看一下\n[写邮件 收件人：bookings@ltmuseum.co.uk | 主题：Goodwill rebooking]\nHello,\n\nOrder [待填：订单号]\n\nMia Chen[/写邮件]")
            self.assertEqual(parse_reply(reply)["bubbles"], ["写好了，你看一下"])
            oid = desk.note_outbox(db, reply)[0]
            x = next(i for i in desk.items(db) if i["kind"] == "out")
            self.assertEqual((x["id"], x["to"], x["title"], x["state"]), (oid, "bookings@ltmuseum.co.uk", "Goodwill rebooking", "draft"))
            acc = [{"name": "Gmail", "user": "d@gmail.com", "host": "imap.gmail.com", "password": "x"}]
            FakeSMTP.sent = []
            self.assertIn("待填", desk.send_new(db, oid, x["draft"], "Gmail", accounts=acc, smtp_factory=FakeSMTP)["msg"])
            self.assertFalse(desk.send_new(db, oid, "Hello", "Gmail", to="not-an-email", accounts=acc, smtp_factory=FakeSMTP)["ok"])
            self.assertFalse(desk.send_new(db, oid, "Hello", "Nope", accounts=acc, smtp_factory=FakeSMTP)["ok"])
            self.assertEqual(FakeSMTP.sent, [])  # 她没点、或者没填好，什么都不发
            out = desk.send_new(db, oid, "Hello, order 123. Mia Chen", "Gmail", accounts=acc, smtp_factory=FakeSMTP)
            self.assertTrue(out["ok"])
            msg = FakeSMTP.sent[0]
            self.assertEqual((msg["To"], msg["Subject"], msg["In-Reply-To"]), ("bookings@ltmuseum.co.uk", "Goodwill rebooking", None))
            self.assertEqual(next(i for i in desk.items(db) if i["kind"] == "out")["state"], "sent")
            kicked = []  # 她写交代、点“按交代重写”：后台按交代改（2026-10-31 她要）
            oid2 = desk.note_outbox(db, "[写邮件 收件人：a@b.com | 主题：Hi]Hello there[/写邮件]")[0]
            desk.ask_draft(db, "out", oid2, "语气软一点", kick=kicked.append)
            self.assertEqual((kicked, next(i for i in desk.items(db) if i["id"] == oid2 and i["kind"] == "out")["state"]), ([-oid2], "drafting"))
            desk.write_draft(store, router("Hi, hope you're well."), -oid2)
            x2 = next(i for i in desk.items(db) if i["id"] == oid2 and i["kind"] == "out")
            self.assertEqual((x2["state"], x2["draft"]), ("draft", "Hi, hope you're well."))
            desk.todo(db, "out", oid)
            self.assertIn("发邮件给 bookings@ltmuseum.co.uk", db.conn.execute("SELECT title FROM tasks").fetchone()[0])
            store.close()


class MailSearchTest(unittest.TestCase):
    def test_search_reads_only_and_ethan_looks_up(self):
        from ethan import imapmail
        raw = (b"Message-ID: <o1@x>\r\nFrom: London Transport Museum <bookings@ltmuseum.co.uk>\r\nSubject: Your order 778899\r\n"
               b"Date: Mon, 20 Oct 2026 10:00:00 +0000\r\n\r\nOrder number 778899 for Hidden London Aldwych.\r\n")

        class FakeIMAP:
            selected = []

            def __init__(self, host, port):
                pass

            def login(self, u, p):
                pass

            def select(self, folder, readonly=False):
                FakeIMAP.selected.append((folder, readonly))
                return "OK", [b"1"]

            def uid(self, *args):
                if args[0] == "SEARCH":
                    return "OK", [b"7"]
                return "OK", [(b"7 (BODY[] {10}", raw)]

            def logout(self):
                pass

        acc = [{"name": "Gmail", "user": "d@gmail.com", "host": "imap.gmail.com", "password": "x"}]
        hits = imapmail.search(["ltmuseum"], accounts=acc, factory=FakeIMAP)
        self.assertEqual((hits[0]["subject"], hits[0]["folder"]), ("Your order 778899", "[Gmail]/All Mail"))
        self.assertTrue(all(ro for _, ro in FakeIMAP.selected))  # 只读打开，不改已读
        self.assertIn("778899", imapmail.lines(hits))
        self.assertEqual(imapmail.search([], accounts=acc, factory=FakeIMAP), [])
