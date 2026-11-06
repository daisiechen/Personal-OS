import json
import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import imapmail, mail
from ethan.memory import MemoryStore

NOW = datetime(2026, 10, 30, 20, tzinfo=timezone.utc)


def raw(i, subject, sender="A <a@x.com>", body="hello"):
    return (f"Message-ID: <m{i}@x>\r\nFrom: {sender}\r\nSubject: {subject}\r\nDate: Fri, 30 Oct 2026 19:00:00 +0000\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n{body}\r\n").encode()


class FakeIMAP:
    """够用的假 IMAP：一个收件箱，记下标记和移动。"""
    def __init__(self, msgs):
        self.box = {"INBOX": dict(msgs)}  # uid → raw
        self.seen, self.created, self.moved, self.logged_in = set(), [], [], False
        self.capabilities = ("IMAP4REV1", "MOVE")
        self.current = "INBOX"

    def __call__(self, host, port):
        return self

    def login(self, user, pw):
        self.logged_in = True

    def _simple_command(self, *a):
        return "OK", [b""]

    def select(self, name="INBOX", readonly=False):
        self.current = name.strip('"')
        return "OK", [b"1"]

    def response(self, code):
        return code, [b"42"]

    def list(self, *a):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"']

    def create(self, name):
        self.created.append(name)
        return "OK", [b""]

    def expunge(self):
        return "OK", [b""]

    def logout(self):
        pass

    def uid(self, cmd, *args):
        box = self.box.setdefault(self.current, {})
        if cmd == "SEARCH":
            q = " ".join(a for a in args if a)
            if q.startswith("UID"):
                lo = int(re.search(r"UID (\d+):", q).group(1))
                uids = [u for u in box if u >= lo] or ([max(box)] if box else [])
            elif q == "UNSEEN":
                uids = [u for u in box if u not in self.seen]
            elif "Message-ID" in q:
                mid = args[-1].strip('"')
                uids = [u for u, r in box.items() if f"<{mid}>".encode() in r]
            else:
                uids = list(box)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        uid = int(args[0])
        if cmd == "FETCH" and "FLAGS" in args[1]:
            flag = "\\Seen" if uid in self.seen else ""
            return "OK", [f"1 (UID {uid} FLAGS ({flag}))".encode()]
        if cmd == "FETCH":
            return "OK", [(b"1 (BODY[] {10}", box[uid]), b")"]
        if cmd == "STORE":
            if "Seen" in args[2]:
                (self.seen.add if args[1].startswith("+") else self.seen.discard)(uid)
            return "OK", [b""]
        if cmd == "MOVE":
            self.moved.append((uid, args[1]))
            self.box.setdefault(args[1], {})[uid] = box.pop(uid)
            return "OK", [b""]
        return "OK", [b""]


class Router:
    def complete(self, system, messages, purpose="chat", images=()):
        return SimpleNamespace(text=json.dumps({"category": "bill", "attention": True, "reason": "付款失败", "todo": None, "event": None}))


class ImapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.acc = [{"name": "Gmail", "user": "d@gmail.com", "host": "imap.gmail.com", "password": "x"}]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_search_checks_words_itself_and_finds_chinese_all_mail(self):
        """QQ 不认搜索条件、整箱都给回来；Gmail 中文界面的“所有邮件”不叫 All Mail。"""
        qq = FakeIMAP({1: raw(1, "QQ邮箱APP"), 2: raw(2, "Your Hidden London booking", sender="LTM <b@ltmuseum.co.uk>", body="Order 778899")})
        acc = [{"name": "qq", "user": "d@qq.com", "host": "imap.qq.com", "password": "x"}]
        hits = imapmail.search(["ltmuseum"], accounts=acc, factory=qq)
        self.assertEqual([h["subject"] for h in hits], ["Your Hidden London booking"])

        class G(FakeIMAP):
            def list(self, *a):
                return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/&YkBnCZCuTvY-"', b'(\\HasNoChildren) "/" "INBOX"']
        self.assertEqual(imapmail.all_mail(G({})), "[Gmail]/所有邮件")
        self.assertEqual(imapmail.all_mail(FakeIMAP({})), "[Gmail]/All Mail")

    def test_search_does_not_wait_for_a_slow_mailbox(self):
        import time

        class Slow(FakeIMAP):
            def login(self, user, pw):
                time.sleep(2)
        fast = FakeIMAP({1: raw(1, "ltmuseum order")})
        accs = [{"name": "slow", "user": "a", "host": "imap.slow.com", "password": "x", "f": Slow({})},
                {"name": "fast", "user": "b", "host": "imap.fast.com", "password": "x", "f": fast}]
        started = time.time()
        hits = imapmail.search(["ltmuseum"], accounts=accs, factory=lambda host, port: next(a["f"] for a in accs if a["host"] == host), budget=0.5)
        self.assertLess(time.time() - started, 1.5)
        self.assertEqual([h["account"] for h in hits], ["fast"])

    def test_utf7(self):
        self.assertEqual(imapmail.utf7("小伴整理/账单"), "小伴&ZXR0Bg-/&jSZTVQ-")
        self.assertEqual(imapmail.utf7("A&B"), "A&-B")

    def test_run_moves_only_not_needed_and_continues(self):
        fake = FakeIMAP({1: raw(1, "Payment declined"), 2: raw(2, "晓君, 4 折优惠今晚截止！", body="buy now unsubscribe")})
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual((got["Gmail"]["new"], got["moved"]), (2, 1))
        self.assertEqual(fake.moved, [(2, '"' + imapmail.utf7("小伴整理/推广") + '"')])
        self.assertIn(2, fake.seen)
        self.assertNotIn(1, fake.seen)  # 要她看的：留在收件箱、不改已读
        self.assertEqual(self.store.db.conn.execute("SELECT COUNT(*) FROM mail_moves").fetchone()[0], 1)
        # 下一轮：只读新来的
        fake.box["INBOX"][3] = raw(3, "Payment declined again")
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual(got["Gmail"]["new"], 1)
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual(got["Gmail"]["new"], 0)

    def test_spam_folder_watched_and_not_junk_put_back(self):
        """垃圾箱里像签证 / 银行 / 学校的只提醒不移动；她说“不是垃圾”的放回收件箱。"""
        class WithSpam(FakeIMAP):
            def list(self, *a):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

            def uid(self, cmd, *args):
                if cmd == "MOVE":  # 假邮箱：文件夹按去掉引号的名字存，和 select 一致
                    args = (args[0], args[1].strip('"'))
                if cmd == "COPY":
                    self.box.setdefault(args[1], {})[int(args[0])] = self.box[self.current][int(args[0])]
                    return "OK", [b""]
                return super().uid(cmd, *args)
        fake = WithSpam({2: raw(2, "晓君, 4 折优惠今晚截止！", body="buy now unsubscribe")})
        fake.box["[Gmail]/Spam"] = {7: raw(7, "关于您的申根签证申请", body="需要您提供居留许可"), 8: raw(8, "Win a prize", body="click")}
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual(got["Gmail"]["spam_watch"], 1)  # 只挑签证那封，抽奖的不理
        db = self.store.db
        r = db.conn.execute("SELECT attention, spam, applied FROM mail_items WHERE body LIKE '%居留%'").fetchone()
        self.assertEqual(tuple(r), (1, 1, 1))
        self.assertIn(7, fake.box["[Gmail]/Spam"])  # 没移动
        promo = db.conn.execute("SELECT id FROM mail_items WHERE body LIKE '%unsubscribe%'").fetchone()[0]
        self.assertTrue(any(x["id"] == promo for x in mail.archived(db)))
        mail.not_junk(db, promo)
        self.assertFalse(any(x["id"] == promo for x in mail.archived(db)))
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual(got["Gmail"]["put_back"], 1)
        self.assertEqual(db.conn.execute("SELECT restore FROM mail_items WHERE id = ?", (promo,)).fetchone()[0], 2)

    def test_sent_mail_becomes_her_words_without_quotes(self):
        """“后面的继续做吧”（1.6）：已发送里她写的算她的原话，引用的别人的话去掉，只读不动。"""
        from ethan import lifebase

        class WithSent(FakeIMAP):
            def list(self, *a):
                return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Sent) "/" "[Gmail]/Sent Mail"']
        body = "Hi Amy, I will be in Paris from Nov 20, looking forward to the workshop.\n\nOn Mon, 3 Nov 2026 Amy wrote:\n> Are you coming?"
        fake = WithSent({})
        fake.box["[Gmail]/Sent Mail"] = {3: raw(3, "Re: workshop", body=body), 4: raw(4, "ok", body="好的")}
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual(got["Gmail"]["sent"], 1)  # “好的”太短不收
        ev = self.store.db.conn.execute("SELECT text, source FROM evidence WHERE source = 'sent'").fetchone()
        self.assertIn("Paris", ev["text"])
        self.assertNotIn("Are you coming", ev["text"])
        self.assertEqual(lifebase.source_kind_for(self.store.db, [self.store.db.conn.execute("SELECT id FROM evidence WHERE source = 'sent'").fetchone()[0]]), "her_words")
        self.assertIn(3, fake.box["[Gmail]/Sent Mail"])  # 没动
        self.assertEqual(imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)["Gmail"].get("sent"), None)  # 不重复

    def test_own_words_strips_chinese_quote(self):
        self.assertEqual(imapmail.own_words("好的，周五见。\n\n在 2026年11月3日 周一，Amy 写道：\n> 周五有空吗"), "好的，周五见。")

    def test_backlog_old_unread_once(self):
        """接进来之前就在收件箱里的旧未读，补整理一次，补完不再看。"""
        imapmail.ensure(self.store.db)
        self.store.db.conn.execute("INSERT INTO imap_state (account, uidvalidity, last_uid) VALUES ('Gmail', '42', 10)")
        fake = FakeIMAP({3: raw(3, "限时大促 4 折优惠", body="unsubscribe"), 4: raw(4, "Payment declined")})
        fake.seen.add(4)  # 已读的不管
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertEqual((got["Gmail"].get("backlog"), got["moved"]), (1, 1))
        got = imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)
        self.assertNotIn("backlog", got["Gmail"])
        self.assertEqual(self.store.db.conn.execute("SELECT backlog_done FROM imap_state").fetchone()[0], 1)

    def test_login_failures_back_off_and_show_in_checkup(self):
        """126 授权码作废后每 10 分钟登一次、失败几十次。连续 3 次后改成 2 小时一次，体检里写清。"""
        from datetime import timedelta
        from ethan import checkup

        class Bad(FakeIMAP):
            def login(self, user, pw):
                raise imapmail.imaplib.IMAP4.error("LOGIN Login error or password error")
        bad = Bad({})
        acc = [{"name": "126", "user": "x@126.com", "host": "imap.126.com", "password": "x"}]
        real, imapmail.CONFIG = imapmail.CONFIG, Path(self.tmp.name) / "no-mail-accounts.json"  # 服务器上有真账户文件，刚改过会“马上重试”
        self.addCleanup(setattr, imapmail, "CONFIG", real)
        for i in range(3):
            got = imapmail.run(self.store, Router(), acc, factory=bad, now=NOW + timedelta(minutes=10 * i))
            self.assertIn("error", got["126"])
        got = imapmail.run(self.store, Router(), acc, factory=bad, now=NOW + timedelta(minutes=40))
        self.assertIn("skipped", got["126"])
        got = imapmail.run(self.store, Router(), acc, factory=bad, now=NOW + timedelta(hours=3))
        self.assertIn("error", got["126"])  # 过了 2 小时再试一次
        found = [p for p in checkup.problems(self.tmp.name, self.store.db, NOW, services=False) if p["what"] == "邮箱 126"]
        self.assertIn("password error", found[0]["detail"])
        good = FakeIMAP({1: raw(1, "Payment declined")})
        self.assertTrue(imapmail.should_try(self.store.db, "126", NOW + timedelta(hours=3, minutes=10), changed_at=(NOW + timedelta(hours=3, minutes=5)).timestamp()))
        got = imapmail.run(self.store, Router(), [dict(acc[0])], factory=good, now=NOW + timedelta(hours=6))
        self.assertNotIn("error", got["126"])
        self.assertEqual(imapmail.failing(self.store.db), [])  # 登上了就清零

    def test_netease_flat_folder_and_give_up(self):
        """126 建不了子文件夹（TRYCREATE Folder not exist），每封每轮都移不走、报错越积越多。
        改成一层的“小伴整理-推广”；还是不行就试三轮后留在收件箱，不再重试。"""
        class NoSub(FakeIMAP):
            def uid(self, cmd, *args):
                if cmd == "MOVE" and "/" in imapmail.from_utf7(args[1].strip('"')):
                    return "NO", [b"[TRYCREATE] Folder not exist"]
                return super().uid(cmd, *args)
        fake = NoSub({2: raw(2, "限时大促 4 折优惠", body="unsubscribe")})
        acc = [{"name": "126", "user": "x@126.com", "host": "imap.126.com", "password": "x"}]
        got = imapmail.run(self.store, Router(), acc, factory=fake, now=NOW)
        self.assertEqual(got["moved"], 1)
        self.assertEqual(imapmail.from_utf7(fake.moved[0][1].strip('"')), "小伴整理-推广")
        self.assertEqual(self.store.db.conn.execute("SELECT flat FROM imap_state WHERE account = '126'").fetchone()[0], 1)

        class Never(FakeIMAP):
            def uid(self, cmd, *args):
                return ("NO", [b"nope"]) if cmd == "MOVE" else super().uid(cmd, *args)
        never = Never({5: raw(5, "限时大促 5 折", body="unsubscribe")})
        acc2 = [{"name": "qq", "user": "x@qq.com", "host": "imap.qq.com", "password": "x"}]
        for i in range(4):
            imapmail.run(self.store, Router(), acc2, factory=never, now=NOW)
        row = self.store.db.conn.execute("SELECT applied, move_tries FROM mail_items WHERE account = 'qq'").fetchone()
        self.assertEqual(tuple(row), (2, 3))  # 试三轮就停

    def test_dry_run_and_bad_account(self):
        fake = FakeIMAP({2: raw(2, "限时大促 4 折优惠", body="unsubscribe")})
        got = imapmail.run(self.store, Router(), self.acc, dry_run=True, factory=fake, now=NOW)
        self.assertEqual(fake.moved, [])
        self.assertEqual(len(got["would_move"]), 1)
        imapmail.run(self.store, Router(), self.acc, factory=fake, now=NOW)  # 演练过的，下一轮照样办
        self.assertEqual(len(fake.moved), 1)

        def broken(host, port):
            raise OSError("login failed")
        got = imapmail.run(self.store, Router(), self.acc, factory=broken, now=NOW)
        self.assertIn("error", got["Gmail"])

    def test_add_account_keeps_secret_file_private(self):
        path = Path(self.tmp.name) / "acc.json"
        answers = iter(["d@qq.com", "", "QQ"])
        msg = imapmail.add_account(path, ask=lambda q: next(answers), secret=lambda q: "code")
        self.assertIn("已保存 QQ", msg)
        self.assertEqual(imapmail.load_accounts(path)[0]["host"], "imap.qq.com")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")


if __name__ == "__main__":
    unittest.main()


class ParseTest(unittest.TestCase):
    def test_folded_and_encoded_headers(self):
        raw = (b"Message-ID: <x1@y>\r\nFrom: =?utf-8?B?5byg5LiJ?=\r\n <zhang@x.com>\r\nTo: a@b.com,\r\n\tc@d.com\r\n"
               b"Subject: =?utf-8?B?5rWL6K+V?=\r\nDate: Fri, 30 Oct 2026 19:00:00 +0000\r\n"
               b"Content-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>hi <b>there</b></p>\r\n--b--\r\n")
        got = imapmail.parse(raw)
        self.assertEqual((got["sender"], got["subject"], got["body"]), ("张三 <zhang@x.com>", "测试", "hi there"))
        self.assertEqual(got["received_at"], "2026-10-30T19:00:00+00:00")
        self.assertIsNone(got["via"])

    def test_homepage_mail_and_paragraphs(self):
        raw = (b"Message-ID: <x2@y>\r\nFrom: a@b.com\r\nTo: hi@example.com\r\nSubject: hello\r\n\r\nHi   Mia,\r\n\r\n\r\nLove it.\r\nAmy\r\n")
        got = imapmail.parse(raw)
        self.assertEqual((got["via"], got["body"]), ("example.com", "Hi Mia,\n\nLove it.\nAmy"))



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
