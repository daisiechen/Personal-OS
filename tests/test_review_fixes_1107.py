"""Codex 整体代码评审（docs/research/2026-11-07-codex-code-review.md）第 1、7、8 条的回归测试。"""

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import desk, quests
from ethan.memory import MemoryStore
from ethan.workbench.server import log_path


class LogPathTest(unittest.TestCase):
    def test_device_token_not_logged(self):
        """第 1 条：设备登录链接 /d/<长期令牌> 不进日志。"""
        self.assertEqual(log_path("/d/abc.def.123?x=1"), "/d/…")
        self.assertEqual(log_path("/api/state?token=1"), "/api/state")


class FakeSMTP:
    sent = []
    fail = None

    def __init__(self, host, port, timeout=30):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        if FakeSMTP.fail:
            raise FakeSMTP.fail
        FakeSMTP.sent.append(msg["Message-ID"])


class SendOnceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        desk.ensure(self.db)
        cur = self.db.conn.execute("INSERT INTO outbox (to_addr, subject, draft, desk_state, created_at) VALUES ('amy@example.com', '报价', '你好', 'draft', '2026-11-07T10:00:00+00:00')")
        self.oid = cur.lastrowid
        self.db.conn.commit()
        host = next(iter(desk.SMTP))
        self.acc = [{"name": "工作", "host": host, "user": "me@example.com", "password": "x"}]
        FakeSMTP.sent, FakeSMTP.fail = [], None

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def send(self):
        return desk.send_new(self.db, self.oid, "你好", "工作", accounts=self.acc, smtp_factory=FakeSMTP)

    def test_second_click_does_not_send_again(self):
        """第 7 条：连点两次只发一封。"""
        self.assertTrue(self.send()["ok"])
        r = self.send()
        self.assertFalse(r["ok"])
        self.assertIn("已经发过", r["msg"])
        self.assertEqual(len(FakeSMTP.sent), 1)

    def test_failure_released_and_timeout_says_unknown(self):
        """第 7 条：确定没发出去的退回原状态能重发；超时提醒她先去已发送里看。"""
        import smtplib
        FakeSMTP.fail = smtplib.SMTPAuthenticationError(535, b"bad")
        self.assertIn("没发出去", self.send()["msg"])
        self.assertEqual(self.db.conn.execute("SELECT desk_state FROM outbox WHERE id = ?", (self.oid,)).fetchone()[0], "draft")
        FakeSMTP.fail = TimeoutError("timed out")
        self.assertIn("不确定发没发出去", self.send()["msg"])
        FakeSMTP.fail = None
        self.assertTrue(self.send()["ok"])


class RedeemTest(unittest.TestCase):
    def test_two_connections_cannot_overspend(self):
        """第 8 条：两个连接（工作台、Telegram）兑换，余额只够一次就只成一次。查余额和扣分现在在一个 BEGIN IMMEDIATE 写事务里，
        真正同时到的第二个会等第一个提交完再查余额；这里按先后顺序验结果。"""
        with tempfile.TemporaryDirectory() as d:
            a, b = MemoryStore(Path(d)), MemoryStore(Path(d))
            try:
                quests.seed(a.db)
                rid = a.db.conn.execute("SELECT id, cost FROM rewards WHERE active = 1 ORDER BY cost LIMIT 1").fetchone()
                need = rid["cost"] - quests.totals(a.db)["balance"]
                if need > 0:
                    a.db.conn.execute("INSERT INTO redemptions (reward_id, cost, status, at) VALUES (?, ?, '已兑现', '2026-01-01')", (rid["id"], -need))
                    a.db.conn.commit()
                self.assertEqual(quests.totals(a.db)["balance"], rid["cost"])
                ok1, _ = quests.redeem(a.db, rid["id"], date(2026, 11, 7))
                ok2, _ = quests.redeem(b.db, rid["id"], date(2026, 11, 7))
                self.assertEqual((ok1, ok2), (True, False))
                self.assertGreaterEqual(quests.totals(a.db)["balance"], 0)
            finally:
                a.close()
                b.close()


if __name__ == "__main__":
    unittest.main()


class MeetHabitTest(unittest.TestCase):
    def test_meet_logged_checks_weekly_habit_and_stops_asking(self):
        """明明这周见过朋友，小伴 周六还问“这周跟朋友见面那格还空着吧”。记了见面，“和朋友见面”自动打卡；这周见过了，“选本周和朋友见面的地方”不再问。"""
        from datetime import datetime
        from ethan import lifelog, nudges
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            try:
                db = store.db
                quests.seed(db)
                hid = quests.add_habit(db, "和朋友见面", "weekly") or quests.habit_by_title(db, "和朋友见面")["id"]
                db.conn.execute("UPDATE habits SET auto = ? WHERE id = ?", (lifelog.MEET_HABIT, hid))
                db.conn.commit()
                thu = date(2026, 11, 5)
                self.assertTrue(lifelog.add(db, "meet", thu, "周祥猷", "吃饭"))
                self.assertTrue(quests.checked(db, hid, thu))
                self.assertTrue(lifelog.met_since(db, date(2026, 11, 2)))
                self.assertFalse(lifelog.met_since(db, date(2026, 11, 9)))
            finally:
                store.close()


class CalendarBackoffTest(unittest.TestCase):
    def test_denied_once_then_waits(self):
        """改了 Apple ID 密码后工作台每几分钟撞一次日历，一天一千多次，换了新密码也被临时拦着。被拒一次就停 30 分钟。"""
        import urllib.error
        from ethan import icloud
        calls = []

        def opener(req, timeout=30):
            calls.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)
        icloud._DENIED.clear()
        c = icloud.ICloudCalendar("a@icloud.com", "old", opener=opener)
        with self.assertRaises(icloud.CalendarError):
            c.calendars()
        with self.assertRaises(icloud.CalendarError) as e:
            c.calendars()
        self.assertEqual(len(calls), 1)  # 第二次没去撞
        self.assertIn("分钟后再试", str(e.exception))
        c2 = icloud.ICloudCalendar("a@icloud.com", "new", opener=opener)  # 换了密码马上能试
        with self.assertRaises(icloud.CalendarError):
            c2.calendars()
        self.assertEqual(len(calls), 2)
        icloud._DENIED.clear()


class OldLoopsTest(unittest.TestCase):
    def test_loops_before_2026_closed(self):
        """"""
        import importlib.util
        import json
        from ethan import relations
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            try:
                relations.ensure(store.db)
                pid = relations.upsert(store.db, "阿杰")
                loops = [{"kind": "she_promised", "date": "2025-06-01", "text": "帮他看简历", "quote": "好我看看"},
                         {"kind": "they_asked", "date": "2026-10-20", "text": "问她去不去芬兰", "quote": "去芬兰吗"}]
                store.db.conn.execute("UPDATE people SET open_loops = ? WHERE id = ?", (json.dumps(loops, ensure_ascii=False), pid))
                store.db.conn.commit()
                spec = importlib.util.spec_from_file_location("m", Path(__file__).resolve().parent.parent / "migrations/20261107_old_loops_done.py")
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                self.assertIn("1 件", mod.run(store))
                left = json.loads(store.db.conn.execute("SELECT open_loops FROM people WHERE id = ?", (pid,)).fetchone()[0])
                self.assertEqual([x["text"] for x in left], ["问她去不去芬兰"])
            finally:
                store.close()


class MeetTickTest(unittest.TestCase):
    def test_home_tick_counts_in_social(self):
        """首页点了“和朋友见面”，社交领域和赛季分数没变。点了也记一笔见面；撤销就收回；同一天已经记过见面的不重复记。"""
        from ethan import lifelog
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            try:
                db = store.db
                quests.seed(db)
                hid = quests.add_habit(db, "和朋友见面", "weekly") or quests.habit_by_title(db, "和朋友见面")["id"]
                db.conn.execute("UPDATE habits SET auto = ? WHERE id = ?", (quests.MEET_AUTO, hid))
                db.conn.commit()
                sun = date(2026, 11, 8)
                quests.check_habit(db, hid, sun)
                self.assertEqual(lifelog.count(db, "meet", date(2026, 11, 2), sun), 1)
                quests.uncheck_habit(db, hid, sun)
                self.assertEqual(lifelog.count(db, "meet", date(2026, 11, 2), sun), 0)
                lifelog.add(db, "meet", sun, "周祥猷", "吃饭")  # 已经记过：自动打卡，不再多记一笔
                self.assertEqual(lifelog.count(db, "meet", date(2026, 11, 2), sun), 1)
                self.assertTrue(quests.checked(db, hid, sun))
            finally:
                store.close()


class LoopsLaterTest(unittest.TestCase):
    def test_conditional_deferred_and_date_done(self):
        """待履约里“回国后拜访”“BP 做好后发给他”不是现在要做的；生日办完了要能了结。"""
        import json
        from ethan import followups, relations
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            try:
                db = store.db
                relations.ensure(db)
                pid = relations.upsert(db, "闫会长")
                loops = [{"kind": "she_promised", "date": "2026-10-14", "text": "元旦前后回国后第一时间约闫会长补上聚餐", "quote": "x"},
                         {"kind": "she_promised", "date": "2026-08-25", "text": "BP做好后发给他，请他帮忙看", "quote": "y"},
                         {"kind": "they_asked", "date": "2026-11-01", "text": "问她下周能不能通个电话", "quote": "z"}]
                db.conn.execute("UPDATE people SET open_loops = ?, birthday = '11-10' WHERE id = ?", (json.dumps(loops, ensure_ascii=False), pid))
                db.conn.commit()
                today = date(2026, 11, 8)
                r = relations.defer_loop(db, pid, loops[1]["text"], today)
                self.assertTrue(r["ok"])
                self.assertIn("30 天后", r["msg"])
                self.assertEqual([x["wait"] for x in followups.later(db)], ["到 2026-12-08"])
                left = [x["text"] for x in json.loads(db.conn.execute("SELECT open_loops FROM people WHERE id = ?", (pid,)).fetchone()[0])]
                self.assertNotIn(loops[1]["text"], left)
                self.assertEqual([x["what"] for x in relations.due(db, today)["days"]], ["生日"])
                relations.date_done(db, pid, "2026-11-10", "生日")
                self.assertEqual(relations.due(db, today)["days"], [])
                self.assertEqual([x["what"] for x in relations.due(db, date(2027, 11, 8))["days"]], ["生日"])  # 明年照常
            finally:
                store.close()


class GateMigrationTest(unittest.TestCase):
    def test_only_filter(self):
        """闸门先跑改检验题的数据改动（文件名带 _eval），别的照旧等上线后再跑。"""
        from ethan import migrate
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            (repo / "migrations").mkdir(parents=True)
            for name in ("20990101_eval_fix", "20990101_other"):
                (repo / "migrations" / f"{name}.py").write_text('"""测试用的数据改动"""\n\ndef run(store):\n    return "ok"\n', encoding="utf-8")
            data = Path(d) / "data"
            store = MemoryStore(data)
            try:
                got = migrate.run_all(store, repo, data, only=lambda n: "_eval" in n)
                self.assertEqual([g[0] for g in got], ["20990101_eval_fix"])
                self.assertEqual([p.stem for p in migrate.pending(repo, data, store.db)], ["20990101_other"])
            finally:
                store.close()



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
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
