import io
import json
import tempfile
import unittest
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ethan import finance, territory
from ethan.inbox import Inbox
from tests.test_workbench import MONDAY, make_demo

NOW = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 12, tzinfo=timezone.utc)


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return None


class FinanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_apple_pay_shortcut(self):
        """Revolut 没接，最近的消费没记进来。快捷指令每次 Apple Pay 付款发过来；Monzo、招行卡已有来源，不重复记。"""
        inbox = Inbox(self.store, {"grok": "g", "cloud": "c"})
        post = lambda d: inbox.handle("/cloud/spend", {"Authorization": "Bearer c"}, json.dumps(d, ensure_ascii=False).encode(), NOW)[1]
        r = post({"kind": "applepay", "card": "Revolut Visa", "merchant": "Pret A Manger", "amount": "£4.85"})
        self.assertEqual((r["amount"], r["currency"], r["source"]), (-4.85, "GBP", "revolut"))
        self.assertTrue(post({"kind": "applepay", "card": "Revolut Visa", "merchant": "Pret A Manger", "amount": "£4.85"})["dup"])
        self.assertTrue(post({"card": "Monzo", "merchant": "Tesco", "amount": "£10"})["skipped"])
        self.assertTrue(post({"card": "招商银行信用卡", "merchant": "Tesco", "amount": "£10"})["skipped"])
        self.assertEqual(post({"card": "Revolut", "merchant": "Café", "amount": "12,30 €"})["currency"], "EUR")
        self.assertEqual(post({"card": "Amex", "merchant": "Uber", "amount": "23.10"})["source"], "applepay")
        self.assertFalse(post({"card": "Revolut", "merchant": "x", "amount": "免费"})["parsed"])
        n = self.db.conn.execute("SELECT COUNT(*) FROM transactions WHERE source IN ('revolut', 'applepay') AND amount < 0").fetchone()[0]
        self.assertEqual(n, 3)

    def test_apple_pay_and_email_count_once(self):
        """Apple Pay 记的和邮件记的去重。Apple Pay 先到、邮件后到：留邮件那条；邮件先到：Apple Pay 不记。Revolut 不动。"""
        ap = lambda d: finance.record_applepay(self.db, d, NOW)
        ap({"card": "Visa ••1234", "merchant": "Tesco", "amount": "£10.95"})
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'applepay'").fetchone()[0], 1)
        finance.add(self.db, "cmb", NOW + timedelta(hours=10), -10.95, "GBP", merchant="TESCO STORES MAI")  # 第二天的每日信用管家
        rows = self.db.conn.execute("SELECT source FROM transactions WHERE ABS(amount + 10.95) < 0.01").fetchall()
        self.assertEqual([r[0] for r in rows], ["cmb"])
        finance.add(self.db, "monzo", NOW, -4.2, "GBP", merchant="Pret")
        self.assertTrue(ap({"card": "Visa", "merchant": "Pret A Manger", "amount": "£4.20"})["dup"])
        ap({"card": "Revolut", "merchant": "Boots", "amount": "£7.00"})
        finance.add(self.db, "cmb", NOW, -7.0, "GBP", merchant="BOOTS")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM transactions WHERE source = 'revolut'").fetchone()[0], 1)

    def test_cmb_sms_recorded_once_and_feeds_health_line(self):
        inbox = Inbox(self.store, {"grok": "g", "cloud": "c"})
        sms = f"【招商银行】您账户1234于{MONDAY.month}月{MONDAY.day}日12:30在【瑞幸咖啡】快捷支付扣款人民币29.90元"
        code, body = inbox.handle("/cloud/spend", {"Authorization": "Bearer c"}, json.dumps({"text": sms}).encode(), NOW)
        self.assertEqual((code, body["amount"], body["merchant"]), (200, -29.9, "瑞幸咖啡"))
        inbox.handle("/cloud/spend", {"Authorization": "Bearer c"}, json.dumps({"text": sms}).encode(), NOW)  # 同一条再发
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
        self.assertEqual(inbox.handle("/cloud/spend", {}, b'{"text": "x"}', NOW)[0], 401)
        self.assertFalse(finance.record_cmb(self.db, "您的验证码是 1234", NOW)["parsed"])  # 认不出也留着
        # 同一笔在合并通知和单发通知里各出现一次（11-03 境外汇款手续费记了两次）：只记一次
        combo = "您账户6851于11月03日20:47境外汇款费用支出人民币291.91 您账户6851于11月03日20:47通过手机银行已提交境外汇款"
        n0 = self.db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        finance.record_cmb(self.db, combo, NOW, "95555")
        self.assertTrue(finance.record_cmb(self.db, "您账户6851于11月03日20:47境外汇款费用支出人民币291.91", NOW, "95555").get("dup"))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], n0 + 1)
        bocom = finance.record_cmb(self.db, f"【交通银行】您尾号5678的卡于{MONDAY.month}月{MONDAY.day}日18:02消费12.00元，余额100.00元", NOW, "95559")
        self.assertEqual((bocom["bank"], bocom["amount"]), ("bocom", -12.0))
        self.assertEqual(finance.record_cmb(self.db, "招商银行信用卡：您尾号0001消费人民币8.00元", NOW)["bank"], "cmb")
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 49.9})
        row = next(r for r in territory.health(self.db, "money", MONDAY, True) if r["name"] == "本周花销")
        self.assertIn("¥50", row["actual"])
        self.assertEqual(row["light"], "ok")  # 有数就亮，不再显示等数据
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        self.assertTrue(wb.act("money.budget", {"amount": "40"})["ok"])
        self.assertEqual(wb.territory_page("money")["budget"]["now"], 40.0)
        row = next(r for r in territory.health(self.db, "money", MONDAY, True) if r["name"] == "本周花销")
        self.assertEqual(row["light"], "bad")  # 超预算变红
        # 定了就锁住本月：再改只从下个月起生效
        self.assertIn("起生效", wb.act("money.budget", {"amount": "8000"})["msg"])
        plan = territory.budget_plan(self.db, datetime.now().date())
        self.assertEqual((plan["now"], plan["next"]), (40.0, 8000.0))
        # 改的时候选立即生效，这个月就按新的算，下个月那条也跟着改
        self.assertIn("这个月开始", wb.act("money.budget", {"amount": "9000", "when": "now"})["msg"])
        plan = territory.budget_plan(self.db, datetime.now().date())
        self.assertEqual((plan["now"], plan["next"]), (9000.0, None))
        self.assertFalse(wb.act("money.budget", {"amount": ""})["ok"])  # 不能清空
        # 英镑花销按汇率折成人民币一起算
        finance.add(self.db, "monzo", NOW, -10, "GBP", merchant="Pret", ext_id="monzo:t1")
        row = next(r for r in territory.health(self.db, "money", MONDAY, True) if r["name"] == "本周花销")
        self.assertIn(f"¥{49.9 + 10 * finance.FX_FALLBACK:,.0f}", row["actual"])
        rate = finance.refresh_fx(self.db, opener=lambda *a, **k: io.BytesIO(b'{"rates": {"CNY": 9.1}}'))
        self.assertEqual((rate, finance.fx_rate(self.db)), (9.1, 9.1))

    def test_monzo_time_any_fraction(self):
        for raw in ("2026-10-25T21:54:18.66Z", "2026-10-25T21:54:18.660Z", "2026-10-25T21:54:18.6612345+00:00"):
            self.assertEqual(finance.monzo_time(raw).replace(microsecond=0),
                             datetime(2026, 10, 25, 21, 54, 18, tzinfo=timezone.utc))
        self.assertEqual(finance.monzo_time("2026-10-25T21:54:18Z").second, 18)

    def test_monzo_oauth_and_sync(self):
        calls = []

        def opener(req, timeout):
            calls.append(req.full_url)
            if req.full_url.endswith("/oauth2/token"):
                return Resp(json.dumps({"access_token": "AT", "refresh_token": "RT", "expires_in": 21600}).encode())
            if "/accounts" in req.full_url:
                return Resp(json.dumps({"accounts": [{"id": "acc_1"}]}).encode())
            return Resp(json.dumps({"transactions": [
                {"id": "tx_1", "amount": -450, "currency": "GBP", "created": NOW.isoformat().replace("+00:00", "Z"),
                 "merchant": {"name": "Pret"}, "category": "eating_out"},
                {"id": "tx_2", "amount": -999, "currency": "GBP", "created": NOW.isoformat(), "decline_reason": "INSUFFICIENT_FUNDS"}]}).encode())
        with mock.patch.dict("os.environ", {"MONZO_CLIENT_ID": "oauth2client_x", "MONZO_CLIENT_SECRET": "mnzconfsecret"}):
            link = finance.auth_link(self.root)
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)["state"][0]
            with self.assertRaises(ValueError):
                finance.callback(self.root, "code", "wrong", opener)
            self.assertIn("批准", finance.callback(self.root, "code", state, opener))
            self.assertEqual(oct((self.root / finance.TOKEN_FILE).stat().st_mode)[-3:], "600")
            self.assertEqual(finance.sync_monzo(self.db, self.root, opener), 1)  # 被拒的不算
            self.assertEqual(finance.sync_monzo(self.db, self.root, opener), 0)  # 不重复
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"GBP": 4.5})


    def test_forwarded_wechat_notice_recorded_from_chat_marker(self):
        from ethan.core import agent
        reply = "记好啦 [记账 -56.8 CNY 瑞幸咖啡 招行信用卡]"
        self.assertEqual(finance.note_chat(self.db, reply, NOW), 1)
        self.assertEqual(finance.note_chat(self.db, reply, NOW), 0)  # 同一条不重复
        self.assertEqual(agent.parse_reply(reply)["bubbles"], ["记好啦"])
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 56.8})
        self.assertFalse(finance.has_source(self.db, "sms"))  # 聊天里记的一笔不算“银行卡接通了”
        row = next(r for r in territory.health(self.db, "money", MONDAY, True) if r["name"] == "本周花销")
        self.assertIn("¥57", row["actual"])  # 但花销照算

    def test_only_unparsed_does_not_count_as_connected(self):
        finance.record_cmb(self.db, "快捷指令输入", NOW)  # 的快捷指令把变量打成了字
        self.assertFalse(finance.has_source(self.db, "sms"))
        self.assertEqual(finance.last_unparsed(self.db)["raw"], "快捷指令输入")

    def test_real_cmb_debit_email(self):
        # 发来的招行一卡通邮件（卡号后四位已换）
        body = f"您账户0000于{MONDAY.month}月{MONDAY.day}日10:08在支付宝-某网络科技有限公司快捷支付123.45元"
        got = finance.record_cmb(self.db, body, NOW, "95555@message.cmbchina.com")
        self.assertEqual((got["bank"], got["amount"], got["merchant"]), ("cmb", -123.45, "支付宝-某网络科技有限公司"))
        repay = finance.record_cmb(self.db, f"您账户0000于{MONDAY.month}月{MONDAY.day}日11:00信用卡还款支出5000.00元", NOW, "95555@message.cmbchina.com")
        self.assertEqual(repay["amount"], -5000.0)
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 123.45})  # 还信用卡不算花销

    def test_cmb_daily_digest_email_split_into_rows(self):
        """招行信用卡“每日信用管家”一封列前一天好几笔，原来只认一封一笔，11 笔一笔没记上。外币照原币记。"""
        body = ("尊敬的客户，截至昨日最后一笔交易，您的额度和积分信息如下：可用额度&nbsp;￥51,694.10 积分余额 3,608 "
                "2026/10/30&nbsp;您的消费明细如下： 01:13:25 GBP 51.98 尾号**67 消费 Dishoom Carnaby Lon-Apple Pay:6790 "
                "03:16:41 EUR 72.00 尾号**67 消费 Europ Assistance PARIS FR 11:12:33 GBP 6.00 尾号**67 消费 TfL Travel Charge TFL-Apple Pay:6790")
        rows = finance.parse_cmb_digest(body)
        self.assertEqual([(r["currency"], r["amount"], r["merchant"]) for r in rows],
                         [("GBP", -51.98, "Dishoom Carnaby Lon"), ("EUR", -72.0, "Europ Assistance PARIS FR"), ("GBP", -6.0, "TfL Travel Charge TFL")])
        self.assertEqual(finance.record_cmb_digest(self.db, body, "招商银行信用卡 <c@message.cmbchina.com>"), 3)
        self.assertEqual(finance.record_cmb_digest(self.db, body, "招商银行信用卡 <c@message.cmbchina.com>"), 0)  # 同一封再来不重复
        self.assertEqual(self.db.conn.execute("SELECT day FROM transactions WHERE merchant LIKE 'Dishoom%'").fetchone()[0], "2026-10-29")  # 北京 01:13 = 伦敦前一天
        self.assertEqual(finance.parse_cmb_digest("您账户0000于10月28日12:30快捷支付29.90元"), [])

    def test_refund_same_subject_and_london_day(self):
        from zoneinfo import ZoneInfo
        evening = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 19, tzinfo=ZoneInfo("Europe/London"))
        bj = evening.astimezone(ZoneInfo("Asia/Shanghai"))  # 北京已经是第二天凌晨
        buy = finance.record_cmb(self.db, f"您账户0000于{bj.month}月{bj.day}日{bj.hour:02d}:{bj.minute:02d}在瑞幸咖啡快捷支付100.00元", evening, "95555@message.cmbchina.com")
        self.assertEqual(buy["amount"], -100.0)
        refund = finance.record_cmb(self.db, f"您账户0000于{bj.month}月{bj.day}日{bj.hour:02d}:{bj.minute:02d}收到瑞幸咖啡退款40.00元", evening, "95555@message.cmbchina.com")
        self.assertEqual((refund["amount"], refund["merchant"]), (40.0, "瑞幸咖啡"))  # 标题一样，看正文：退款是进账
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 60.0})  # 按伦敦今天算进来了，退款冲减
        self.assertEqual(finance.recent(self.db)[0]["day"], MONDAY.isoformat())
        back = finance.record_cmb(self.db, f"您账户0000于{bj.month}月{bj.day}日10:08在支付宝-盒马退款20.00元", evening, "95555@message.cmbchina.com")
        self.assertEqual((back["amount"], back["merchant"]), (20.0, "支付宝-盒马"))  # 商户不带“退款20.00元”

    def test_refund_counts_back_on_original_day(self):
        """退款冲回原消费那一周；对不上原消费的不算；同一笔退款两条通知只记一次；收入不算花销。"""
        from datetime import timedelta
        old = NOW - timedelta(days=20)
        finance.add(self.db, "cmb", old, -2155.2, "CNY", merchant="携程旅行网")
        finance.add(self.db, "cmb", NOW, 2155.2, "CNY", merchant="程支付-携程旅行网", category="refund")
        self.assertFalse(finance.add(self.db, "cmb", NOW, 2155.2, "CNY", merchant=None, category="refund", ext_id="mail:x"))  # 邮件又来一次
        finance.add(self.db, "cmb", NOW, 933.0, "CNY", merchant="程支付-携程旅行网", category="refund")  # 原消费不在记录里
        finance.add(self.db, "cmb", NOW, 5000.0, "CNY", merchant="工资")  # 收入
        today = NOW.date()
        self.assertEqual(finance.spend_between(self.db, today, today), {})  # 本周不被退款冲低
        old_day = finance.london_day(old)
        self.assertEqual(finance.spend_between(self.db, old_day, old_day).get("CNY", 0), 0)  # 原消费那天冲平
        r = next(x for x in finance.recent(self.db) if x["amount"] == 2155.2)
        self.assertEqual((r["count_day"], r["refund_merchant"]), (old_day.isoformat(), "携程旅行网"))
        self.assertIsNone(next(x for x in finance.recent(self.db) if x["amount"] == 933.0)["refund_of"])

    def test_mark_transaction_not_counted(self):
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        finance.record_cmb(self.db, f"您账户0000于{MONDAY.month}月{MONDAY.day}日10:00在瑞幸快捷支付30.00元", NOW)
        finance.record_cmb(self.db, f"您账户0000于{MONDAY.month}月{MONDAY.day}日11:00收到测试退款100.00元", NOW)
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 30.0})  # 11-07 起对不上原消费的退款不算花销
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        tid = next(x["id"] for x in finance.recent(self.db) if x["amount"] == 100)
        r = wb.act("money.ignore", {"id": tid, "on": 1})
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 30.0})
        self.assertTrue(wb.act(r["undo"]["action"], {k: v for k, v in r["undo"].items() if k != "action"})["ok"])
        self.assertEqual(finance.month_spend(self.db, MONDAY), {"CNY": 30.0})  # 算回来了也还是对不上原消费的退款，不算

if __name__ == "__main__":
    unittest.main()


class IgnoreRestoreTest(unittest.TestCase):
    def test_unignore_restores_monzo_category(self):
        with tempfile.TemporaryDirectory() as d:
            from ethan.memory import MemoryStore
            store = MemoryStore(Path(d))
            finance.add(store.db, "monzo", NOW, -200, "GBP", merchant="Savings pot", category="savings", ext_id="monzo:s1")
            tid = finance.recent(store.db)[0]["id"]
            finance.ignore(store.db, tid, True)
            finance.ignore(store.db, tid, False)
            self.assertEqual(finance.recent(store.db)[0]["category"], "savings")  # 不会变成花销
            store.close()


class MonzoPagingTest(unittest.TestCase):
    def test_full_pages_are_followed(self):
        with tempfile.TemporaryDirectory() as d:
            from ethan.memory import MemoryStore
            root = Path(d)
            store = MemoryStore(root)
            seen = []

            def opener(req, timeout):
                url = req.full_url
                if "/accounts" in url:
                    return Resp(json.dumps({"accounts": [{"id": "acc"}]}).encode())
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                seen.append(q["since"][0])
                start = 0 if not q["since"][0].startswith("tx_") else int(q["since"][0][3:]) + 1
                n = min(finance.PAGE, 230 - start)
                return Resp(json.dumps({"transactions": [{"id": f"tx_{i}", "amount": -100, "currency": "GBP",
                                                          "created": NOW.isoformat()} for i in range(start, start + n)]}).encode())
            with mock.patch.object(finance, "_token", lambda data, op: "AT"):
                self.assertEqual(finance.sync_monzo(store.db, root, opener), 230)
            self.assertEqual(len(seen), 3)
            store.close()
