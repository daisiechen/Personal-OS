import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ethan import evals, mail, mailbridge, nudges
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

HOOK = "h" * 40


def item(i, subject="Your payment was declined", sender="Netflix <info@netflix.com>", body="Please update by 5 November."):
    return {"account": "Google", "id": f"<m{i}@x>", "sender": sender, "subject": subject, "received_at": "2026-10-30T20:00:00", "body": body}


class Router:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    def complete(self, system, messages, purpose="chat", images=()):
        if purpose == "translate":  # 要她看的邮件先译成中文（ethan/desk.py）
            self.translated = getattr(self, "translated", 0) + 1
            return SimpleNamespace(text="（中文）" + messages[0]["content"][:20])
        self.calls += 1
        return SimpleNamespace(text=json.dumps(self.answers.pop(0), ensure_ascii=False))


class MailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_queue_classify_and_actions(self):
        got = mail.receive(self.db, {"items": [item(1), item(2, "晓君, 4 折优惠今晚截止！", "EF <ef@x.com>", "unsubscribe"),
                                               item(3, "Hidden London tour", "LTM <b@ltmuseum.co.uk>", "Saturday 31 October 11:00")]})
        self.assertEqual((got["new"], len(got["pending"]), got["actions"]), (3, 3, []))
        self.assertEqual(mail.receive(self.db, {"items": [item(1)]})["new"], 0)  # 重送不重复
        router = Router(
            {"category": "bill", "attention": True, "reason": "付款失败", "todo": {"title": "更新 Netflix 付款方式", "due": "2026-11-05"}, "event": None},
            {"category": "booking", "attention": False, "reason": "导览订单", "todo": None,
             "event": {"day": "2026-10-31", "start": "11:00", "end": None, "title": "Hidden London 导览", "place": "Euston"}})
        res = mail.process(self.store, router)
        self.assertEqual((res["done"], router.calls), (3, 2))  # 广告按规则判，不调模型
        acts = {a["key"]: a for a in mail.receive(self.db, {"ask": ["msgid:m1@x", "msgid:m2@x", "msgid:m3@x"]})["actions"]}
        self.assertTrue(acts["msgid:m1@x"]["keep"])
        self.assertEqual(acts["msgid:m2@x"], {"key": "msgid:m2@x", "folder": "小伴整理/推广", "mark_read": True})
        self.assertEqual(acts["msgid:m3@x"]["folder"], "小伴整理/订单行程")
        t = self.db.conn.execute("SELECT title, due, source_type FROM tasks").fetchone()
        self.assertEqual(tuple(t), ("更新 Netflix 付款方式", "2026-11-05", "email"))
        e = self.db.conn.execute("SELECT day, start, end, title FROM future_events").fetchone()
        self.assertEqual(tuple(e), ("2026-10-31", "11:00", "13:00", "Hidden London 导览"))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM evidence WHERE source = 'email'").fetchone()[0], 3)

    def test_visa_mail_never_archived_as_junk(self):
        """她 11-04：“这个为什么是垃圾？”——芬兰签证中心经 london.edu 转发的补材料通知被判成垃圾。"""
        mail.receive(self.db, {"items": [item(9, "关于您的申根签证申请", "Visa London <visa.london@utn.is>", "我们需要您提供有效的英国居留许可。")]})
        router = Router({"category": "junk", "attention": False, "reason": "像冒充的钓鱼", "todo": None, "event": None})
        mail.process(self.store, router)
        r = self.db.conn.execute("SELECT category, attention FROM mail_items").fetchone()
        self.assertEqual((r["category"], r["attention"]), ("membership", 1))
        self.assertTrue(mail.receive(self.db, {"ask": ["msgid:m9@x"]})["actions"][0]["keep"])  # 留在收件箱，不归档

    def test_fyi_shows_on_desk_but_is_not_counted(self):
        """她 11-04 同意：“不用你做什么”的进行中大事放进工作台，但不算待处理。"""
        from ethan import desk
        mail.receive(self.db, {"items": [item(5, "Check your contact details are up to date", "MPS <automation.noreply@met.police.uk>",
                                              "If nothing has changed, you do not need to take any action.")]})
        mail.process(self.store, Router({"category": "membership", "attention": False, "fyi": True, "reason": "案件联系方式核对", "todo": None, "event": None}))
        got = [x for x in desk.items(self.db) if x.get("fyi")]
        self.assertEqual(len(got), 1)
        self.assertTrue(got[0]["why"].startswith("知道一下"))
        self.assertEqual(desk.count(self.db), 0)

    def test_bill_already_paid_does_not_come_back_as_todo(self):
        """2026-11-01：交行 16692.91 她 10-29 分三笔还清了，10-31 旧账单邮件又建了“还交通银行信用卡账单16692.91元”。"""
        tid = self.db.add_task("还信用卡 9976：1692.91（最后一笔，还清 16692.91）")
        self.db.complete_task(tid)
        mail.receive(self.db, {"items": [item(5, "交通银行信用卡电子账单", "交通银行 <b@bocom.com>", "本期应还 16,692.91")]})
        router = Router({"category": "bill", "attention": True, "reason": "账单", "todo": {"title": "还交通银行信用卡账单16692.91元", "due": "2026-11-13"}, "event": None})
        mail.process(self.store, router)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE done = 0").fetchone()[0], 0)

    def test_cmb_spend_email_goes_into_the_books(self):
        """2026-11-01：126 里有招行的消费邮件，但 10-30 后一笔都没进账（邮件被整理走，Mac 上的规则不再触发）。"""
        from ethan import finance
        mail.receive(self.db, {"items": [item(7, "招商银行一卡通账户变动通知", "招商银行 <95555@message.cmbchina.com>",
                                                "尊敬的客户：您账户0000于11月01日10:08在支付宝-瑞幸咖啡快捷支付29.90元，余额100.00")]})
        mail.process(self.store, Router({"category": "bill", "attention": False, "reason": "消费通知", "todo": None, "event": None}))
        rows = self.db.conn.execute("SELECT amount, merchant, source FROM transactions").fetchall()
        self.assertEqual([tuple(r) for r in rows], [(-29.9, "支付宝-瑞幸咖啡", "cmb")])
        import importlib
        m = importlib.import_module("migrations.20261101_cmb_emails_backfill")
        self.assertIn("0 笔", m.run(self.store))  # 已经记过的不重复
        mail.receive(self.db, {"items": [item(8, "每日信用管家", "招商银行信用卡 <c@message.cmbchina.com>",
                                                "2026/10/31 您的消费明细如下： 02:06:20 GBP 2.45 尾号**67 消费 ALDI LON-Apple Pay:6790 03:36:37 GBP 28.32 尾号**67 消费 UBER * EATS PENDING Lon-Apple Pay:6790")]})
        self.db.conn.execute("INSERT INTO transactions (source, ext_id, at, day, amount, currency, category, raw) VALUES "
                             "('cmb', 'x', '2026-11-01T06:42:34', '2026-11-01', 0, 'CNY', 'unparsed', '尊敬的客户，截至昨日最后一笔交易，…')")
        d = importlib.import_module("migrations.20261101_cmb_digest_backfill")
        self.assertEqual(d.run(self.store), "补记招行每日信用管家 2 笔；删掉 0 元没认出的 1 条")
        self.assertEqual(d.run(self.store), "补记招行每日信用管家 0 笔；删掉 0 元没认出的 0 条")

    def test_unclassifiable_stays_in_inbox(self):
        mail.receive(self.db, {"items": [item(1)]})
        mail.process(self.store, Router("不是 JSON", "还不是"))
        row = self.db.conn.execute("SELECT category, attention FROM mail_items").fetchone()
        self.assertEqual(tuple(row), ("unsure", 1))

    def test_inbox_endpoint_and_digest(self):
        inbox = Inbox(self.store, {"grok": "g" * 40, "hook": HOOK})
        kicked = []
        inbox.mail_kick = lambda root: kicked.append(root)
        body = json.dumps({"items": [item(1)]}).encode()
        self.assertEqual(inbox.handle("/cloud/mail", {}, body)[0], 401)
        code, got = inbox.handle("/cloud/mail", {"Authorization": f"Bearer {HOOK}"}, body)
        self.assertEqual((code, got["new"], len(kicked)), (200, 1, 1))
        mail.process(self.store, Router({"category": "bill", "attention": True, "reason": "付款失败", "todo": None, "event": None}))
        self.assertIsNone(nudges._mail(self.db, datetime(2026, 10, 30, 12, 0), {}))  # 还没到傍晚
        key, text = nudges._mail(self.db, datetime(2026, 10, 30, 19, 0), {})
        self.assertIn("Your payment was declined", text)
        nudges.mark_told(self.db, [key])
        self.assertIsNone(nudges._mail(self.db, datetime(2026, 10, 30, 19, 10), {}))
        self.assertEqual(mail.to_digest(self.db), [])


    def test_digest_skips_what_she_already_handled(self):
        """2026-10-31：交行账单她在工作台勾完了，傍晚 小伴 又说了一遍。"""
        mail.receive(self.db, {"items": [item(1), item(2)]})
        bill = {"category": "bill", "attention": True, "reason": "要还款", "todo": "还交行账单", "event": None}
        mail.process(self.store, Router(*[bill] * 8))
        rows = [r["id"] for r in mail.to_digest(self.db)]
        self.assertEqual(len(rows), 2)
        tid = self.db.conn.execute("SELECT task_id FROM mail_items WHERE id = ?", (rows[0],)).fetchone()[0]
        if tid:
            self.db.conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (tid,))
        else:
            self.db.conn.execute("UPDATE mail_items SET desk_state = 'ignored' WHERE id = ?", (rows[0],))
        self.db.conn.execute("UPDATE mail_items SET desk_state = 'sent' WHERE id = ?", (rows[1],))
        self.assertEqual(mail.to_digest(self.db), [])


class FakeMac:
    """假的 osascript：收件箱里两封邮件，记下移动。"""
    def __init__(self):
        self.moves = []

    def __call__(self, cmd, input, capture_output, text, timeout):
        args = cmd[2:]
        if "messages of inbox whose date received" in input:
            recs = [f"<m{i}@x>\x1fGoogle\x1fA <a@x>\x1f主题{i}\x1f2026-10-30T20:00:00\x1ffalse\x1f正文{i}" for i in (1, 2)]
            return SimpleNamespace(returncode=0, stdout="\x1e".join(recs) + "\x1e", stderr="")
        if "move m to target" in input:
            self.moves.append(args)
            return SimpleNamespace(returncode=0, stdout="INBOX|false\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class BridgeTest(unittest.TestCase):
    def test_once_moves_only_what_server_says(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(mailbridge, "STATE", Path(tmp) / "s.json"), \
                mock.patch.object(mailbridge, "LOG", Path(tmp) / "log.csv"), mock.patch.object(mailbridge, "token", lambda: HOOK):
            sent = []

            def opener(req, timeout):
                sent.append(json.loads(req.data))
                reply = {"actions": [{"key": "msgid:m1@x", "folder": "小伴整理/推广", "mark_read": True},
                                     {"key": "msgid:m2@x", "keep": True}], "pending": []}
                return mock.MagicMock(__enter__=lambda s: SimpleNamespace(read=lambda: json.dumps(reply).encode()), __exit__=lambda *a: None)
            mac = FakeMac()
            out = mailbridge.once(dry_run=True, run=mac, opener=opener)
            self.assertIn("演练", out)
            self.assertEqual(mac.moves, [])
            out = mailbridge.once(run=mac, opener=opener)
            self.assertEqual(mac.moves, [["<m1@x>", "Google", "小伴整理/推广", "1"]])
            self.assertEqual(sent[0]["items"][0]["body"], "正文1")
            self.assertNotIn("was_read", sent[0]["items"][0])
            state = json.loads((Path(tmp) / "s.json").read_text())
            self.assertEqual(sorted(state["done"]), ["msgid:m1@x", "msgid:m2@x"])
            self.assertIn("INBOX", (Path(tmp) / "log.csv").read_text())


class AboutHerTest(unittest.TestCase):
    """收到的邮件里关于她的有效信息记成说法（ontology-plan 1.6，11-04）：第三方来源、语境写清发件人，广告不记，重复不记。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def facts(self):
        return [dict(r) for r in self.db.conn.execute(
            "SELECT statement, dimension, kind, source_kind, context, prop, evidence_ids, valid_from FROM facts ORDER BY id")]

    def run_one(self, i, answer, subject="Your visa application has been submitted", sender="TLScontact <noreply@tlscontact.com>",
                body="Submitted on 9 November 2026."):
        mail.receive(self.db, {"items": [item(i, subject, sender, body)]})
        return mail.process(self.store, Router(answer))

    def test_visa_and_offer_become_third_party_statements(self):
        tasks_before = self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        self.run_one(1, {"category": "membership", "attention": False, "fyi": True, "reason": "签证已递交", "todo": None, "event": None,
                         "about_her": [{"statement": "她 2026 年 11 月 9 日在 TLScontact 伦敦递交了法国签证申请", "type": "visa"}]})
        self.run_one(2, {"category": "school", "attention": True, "reason": "录取，要确认", "todo": None, "event": None,
                         "about_her": [{"statement": "她被 Example University 行为科学硕士录取", "type": "application"}]},
                     "Offer of admission", "Graduate Admissions <admissions@example-university.ac.uk>", "We are delighted to offer you a place.")
        got = self.facts()
        self.assertEqual(len(got), 2)
        visa, offer = got
        self.assertEqual((visa["kind"], visa["source_kind"], visa["dimension"], visa["prop"]), ("fact", "other_person", "resources", "application_status"))
        self.assertEqual(visa["context"], "TLScontact 的邮件里写的（2026-10-30）")
        self.assertEqual(offer["context"], "Graduate Admissions 的邮件里写的（2026-10-30）")
        self.assertNotIn("@", offer["context"])  # 不留邮箱地址
        ev = self.db.conn.execute("SELECT source, source_ref FROM evidence WHERE id = ?", (json.loads(visa["evidence_ids"])[0],)).fetchone()
        self.assertEqual(tuple(ev), ("email", "mail:msgid:m1@x"))  # 证据就是那封邮件
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], tasks_before)  # 不建待办

    def test_promo_and_empty_make_nothing(self):
        # 广告按规则判，不调模型、不记说法
        mail.receive(self.db, {"items": [item(3, "限时大促 5 折", "Shop <s@shop.com>", "unsubscribe")]})
        mail.process(self.store, Router())
        # 模型判成群发，就算给了说法也不记
        self.run_one(4, {"category": "newsletter", "attention": False, "reason": "俱乐部群发", "todo": None, "event": None,
                         "about_her": [{"statement": "她报名了俱乐部讲座", "type": "booking"}]}, "WiB November", "WiB <w@club.edu>", "events")
        self.run_one(5, {"category": "booking", "attention": False, "reason": "订单", "todo": None, "event": None, "about_her": []},
                     "Your order", "Shop <o@shop.com>", "thanks")
        self.assertEqual(self.facts(), [])

    def test_same_news_not_twice_but_progress_is_new(self):
        say = lambda s: {"category": "membership", "attention": False, "reason": "签证", "todo": None, "event": None,
                         "about_her": [{"statement": s, "type": "visa"}]}
        self.run_one(6, say("她 2026 年 11 月 9 日在 TLScontact 递交了法国签证申请"))
        self.run_one(7, say("她 11 月 9 日在 TLScontact 递交了法国签证申请"))  # 提醒邮件又说一遍：不重复
        self.run_one(8, say("她在 TLScontact 递交的法国签证申请已获批"))  # 状态变了：新进展
        self.run_one(9, {"category": "booking", "attention": False, "reason": "改期", "todo": None, "event": None,
                         "about_her": [{"statement": "她的 TLScontact 取护照预约在 11 月 20 日", "type": "booking"}]})
        self.run_one(10, {"category": "booking", "attention": False, "reason": "改期", "todo": None, "event": None,
                          "about_her": [{"statement": "她的 TLScontact 取护照预约在 11 月 24 日", "type": "booking"}]})  # 改期：日期变了算新的
        got = [f["statement"] for f in self.facts()]
        self.assertEqual(len(got), 4, got)
        self.assertEqual(self.facts()[-1]["dimension"], "rhythms")

    def test_forgotten_mail_and_rejected_statement_stay_out(self):
        from ethan import lifebase
        lifebase.ensure(self.db)
        say = {"category": "membership", "attention": False, "reason": "签证", "todo": None, "event": None,
               "about_her": [{"statement": "她 11 月 9 日递交了法国签证申请", "type": "visa"}]}
        self.run_one(11, say)
        fid = self.db.conn.execute("SELECT id FROM facts").fetchone()[0]
        lifebase.invalidate(self.db, fid, force=True)  # 她说“不对”
        self.db.conn.commit()
        self.run_one(12, say)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)
        # 她删过的那封邮件：证据记不进来，说法也不记
        self.db.conn.execute("INSERT INTO forgotten (fp, source, at) VALUES (?, 'email', 'x')", (lifebase.fingerprint("email", "mail:msgid:m13@x"),))
        self.db.conn.commit()
        self.run_one(13, {**say, "about_her": [{"statement": "她被 X 大学录取", "type": "application"}]})
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 1)

    def test_sender_name(self):
        from ethan import lifebase
        self.assertEqual(lifebase.mail_sender_name('"Visa London" <visa.london@utn.is>'), "Visa London")
        self.assertEqual(lifebase.mail_sender_name("noreply@tlscontact.com"), "tlscontact.com")
        self.assertEqual(lifebase.mail_sender_name("<a@b.com>"), "b.com")


class MailEvalTest(unittest.TestCase):
    def test_scorer(self):
        cases = {c["id"]: c for c in evals.load("mail_classify")}
        self.assertGreaterEqual(len(cases), 20)
        s = evals._score_mail_classify
        self.assertTrue(s({"category": "bill", "attention": True}, cases["bill-fail"]["expect"])[0])
        self.assertFalse(s({"category": "bill", "attention": False}, cases["bill-fail"]["expect"])[0])
        self.assertTrue(s({"category": "school", "attention": False}, cases["school-club"]["expect"])[0])
        # 关于她的信息只在写了 about_her 的题里判
        visa = cases["visa-submitted"]["expect"]
        self.assertTrue(s({"category": "membership", "attention": False, "about_her": [{"statement": "她递交了法国签证申请"}]}, visa)[0])
        self.assertFalse(s({"category": "membership", "attention": False, "about_her": []}, visa)[0])
        self.assertFalse(s({"category": "newsletter", "attention": False, "about_her": [{"statement": "她报名了讲座"}]},
                           cases["club-newsletter-nothing"]["expect"])[0])


class HeldTest(unittest.TestCase):
    def test_quarantine_notice_needs_her(self):
        got = mail.rules("postmaster@london.edu", "You have new held messages (Quarantined Emails)", "Release Permit Block unsubscribe")
        self.assertEqual((got["attention"], got["category"]), (True, "membership"))
        self.assertIsNone(mail.rules("a@b.com", "Your order is confirmed", "thanks"))


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
