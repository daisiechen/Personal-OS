"""第 29 题“某天 / 某段时间她做了什么”（在“我”页问一问“我昨天干嘛去了”，小伴 答“我不知道”）：
日期说法、按题分流、日记 + 行为记录、日记没写、对外拿不到、兜底检索不再拿“昨天”两个字去字面匹配。"""

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import askroute, clock, diary, events, lifebase, memsearch, memusers, mepage, onto_query, places_log, taskrules
from ethan.memory import MemoryStore

TODAY = date(2026, 11, 6)  # 周五
DAY = date(2026, 11, 5)


class DayRangeTest(unittest.TestCase):
    def test_phrasings(self):
        one = lambda d: (d, d)
        cases = {"我昨天干嘛去了": one(date(2026, 11, 5)), "前天呢": one(date(2026, 11, 4)), "大前天": one(date(2026, 11, 3)),
                 "上周五我做了啥": one(date(2026, 10, 30)), "上周三我在哪": one(date(2026, 10, 28)),
                 "周三我在哪": one(date(2026, 11, 4)), "这周五": one(date(2026, 11, 6)), "上星期天": one(date(2026, 11, 1)),
                 "11月3号做了什么": one(date(2026, 11, 3)), "十一月三号": one(date(2026, 11, 3)), "11 月 3 日": one(date(2026, 11, 3)),
                 "3号我干嘛了": one(date(2026, 11, 3)), "二十号": one(date(2026, 10, 20)), "11/3": one(date(2026, 11, 3)),
                 "2026-11-03": one(date(2026, 11, 3)), "12月25号": one(date(2025, 12, 25)), "三天前": one(date(2026, 11, 3)),
                 "这周做了什么": (date(2026, 11, 2), date(2026, 11, 6)), "上周": (date(2026, 10, 26), date(2026, 11, 1)),
                 "这个月": (date(2026, 11, 1), date(2026, 11, 6)), "上个月": (date(2026, 10, 1), date(2026, 10, 31)),
                 "这几天": (date(2026, 11, 3), date(2026, 11, 6)), "今天": one(TODAY)}
        for text, want in cases.items():
            got = clock.day_range(text, TODAY)
            self.assertIsNotNone(got, text)
            self.assertEqual(got[:2], want, text)
        for text in ("我在哪", "写日记", "我最近在忙什么", "日程", "3.5 小时", ""):
            self.assertIsNone(clock.day_range(text, TODAY), text)


class RouteMatchTest(unittest.TestCase):
    def test_day_questions_go_to_29(self):
        for text, said in {"我昨天干嘛去了": "昨天", "上周三我在哪": "上周三", "这周做了什么？": "这周", "我前天干了啥": "前天",
                           "11月3号我去哪了": "11月3号", "你知道我昨天干嘛去了吗": "昨天"}.items():
            self.assertEqual(askroute.match(text), (29, said), text)

    def test_not_day_questions(self):
        self.assertEqual(askroute.match("我在哪")[0], 1)  # 没说哪天：还是第 1 题
        self.assertEqual(askroute.match("这周花了多少钱？")[0], 26)
        self.assertEqual(askroute.match("我最近在忙什么？")[0], 1)
        for text in ("我昨天去了公园，好开心", "昨天好累", "我在哪里买的那个包？"):
            self.assertIsNone(askroute.match(text), text)

    def test_registered(self):
        self.assertIn(29, memusers.USERS["ethan"]["questions"])
        self.assertIn(29, memusers.USERS["me_page"]["questions"])
        for u in ("twin", "site"):
            self.assertNotIn(29, memusers.USERS[u]["questions"])
            self.assertIsNone(askroute.match("我昨天干嘛去了", user=u))
        self.assertEqual(onto_query.QUESTIONS[29], "day_summary")
        self.assertNotIn(29, onto_query.FIRST_VERSION)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def behave(self, day: date):
        """那天的行为记录：出门 / 到家、电脑上 6.8 小时、日程、做完一件待办、一笔花销、她在聊天里说的一句。"""
        d = day.isoformat()
        places_log.record(self.db, "leave", now=datetime(day.year, day.month, day.day, 14, 58, tzinfo=timezone.utc))
        places_log.record(self.db, "arrive", now=datetime(day.year, day.month, day.day, 16, 30, tzinfo=timezone.utc))
        places_log.record(self.db, "leave", now=datetime(day.year, day.month, day.day, 19, 24, tzinfo=timezone.utc))
        nxt = day + timedelta(days=1)
        places_log.record(self.db, "arrive", now=datetime(nxt.year, nxt.month, nxt.day, 1, 31, tzinfo=timezone.utc))
        start = datetime(day.year, day.month, day.day, 9, 0)
        for i in range(82):  # 82 × 5 分钟 ≈ 6.8 小时
            self.db.conn.execute("INSERT OR IGNORE INTO work_slots (slot, source) VALUES (?, 'mac')",
                                 ((start + timedelta(minutes=5 * i)).strftime("%Y-%m-%dT%H:%M"),))
        events.add(self.db, d, "19:30", "21:30", "和 Amy 吃饭", place="Three Sheets")
        tid = self.db.add_task("交房租")
        taskrules.complete(self.db, tid, day)
        self.db.conn.execute("INSERT INTO transactions (source, ext_id, at, day, amount, currency, merchant, category) "
                             "VALUES ('monzo', 'x' || ?, ?, ?, -42.5, 'GBP', 'Three Sheets', 'eating_out')", (d, f"{d}T21:00:00+00:00", d))
        lifebase.add_evidence(self.db, "chat", f"raw:{d}:1", f"{d}T12:00:00+00:00", "mia：下午去健身房练下肢")
        lifebase.add_evidence(self.db, "chat", f"raw:{d}:2", f"{d}T12:01:00+00:00", "ethan：好的")
        self.db.conn.commit()

    def text(self, got):
        return "\n".join(i["value"] for i in got["items"]) + "\n" + got["note"]


class DaySummaryTest(Base):
    def setUp(self):
        super().setUp()
        from ethan import workaholic, finance
        workaholic.ensure(self.db)
        finance.ensure(self.db)

    def test_diary_and_behaviour(self):
        self.behave(DAY)
        diary.save(self.db, DAY.isoformat(), "下午去练了下肢，晚上和 Amy 在 Three Sheets 吃饭，聊到很晚才回家。", source="ethan")
        got = onto_query.day_summary(self.db, "ethan", "昨天", today=TODAY)
        self.assertEqual((got["status"], got["q"]), ("answer", 29))
        text = self.text(got)
        for want in ("Three Sheets 吃饭", "小伴 记的", "14:58 出门", "19:24 出门", "01:31 到家（第二天）", "6.8 小时", "交房租",
                     "42.5 GBP", "下午去健身房练下肢", "2026-11-05"):
            self.assertIn(want, text)
        self.assertNotIn("好的", text)  # 小伴 说的不算
        self.assertNotIn("日记还没写", text)
        self.assertEqual(got["items"][0]["part"], "日记")  # 日记在最前

    def test_her_diary_wins_label(self):
        diary.save(self.db, DAY.isoformat(), "系统写的", source="ethan")
        diary.save(self.db, DAY.isoformat(), "我自己改的：今天练了腿。")
        text = self.text(onto_query.day_summary(self.db, "ethan", "11月5号", today=TODAY))
        self.assertIn("她改过", text)
        self.assertIn("今天练了腿", text)
        self.assertNotIn("小伴 记的，她还没改过", text)

    def test_no_diary_uses_behaviour(self):
        self.behave(DAY)
        got = onto_query.day_summary(self.db, "ethan", "昨天", today=TODAY)
        self.assertEqual(got["status"], "answer")
        self.assertIn("那天的日记还没写", got["note"])
        self.assertIn("6.8 小时", self.text(got))

    def test_nothing_is_unknown(self):
        got = onto_query.day_summary(self.db, "ethan", "上周三", today=TODAY)
        self.assertEqual(got["status"], "unknown")
        self.assertIn("2026-10-28", got["note"])
        self.assertEqual(onto_query.day_summary(self.db, "ethan", "随便", today=TODAY)["status"], "unknown")

    def test_range(self):
        self.behave(DAY)
        self.behave(date(2026, 11, 3))
        got = onto_query.day_summary(self.db, "ethan", "这周", today=TODAY)
        text = self.text(got)
        self.assertIn("11/5 周四", text)
        self.assertIn("11/3 周二", text)
        self.assertLess(text.index("11/5"), text.index("11/3"))  # 新的在前

    def test_external_gets_nothing(self):
        self.behave(DAY)
        diary.save(self.db, DAY.isoformat(), "晚上在 Three Sheets 吃饭。")
        for user in ("twin", "site"):
            got = onto_query.day_summary(self.db, user, "昨天", today=TODAY)
            self.assertEqual((got["status"], got["items"], got["note"]), ("unknown", [], ""))
        self.assertEqual(self.db.conn.execute("SELECT status FROM function_log ORDER BY id DESC LIMIT 1").fetchone()[0], "forbidden")
        pub = onto_query.public_answer(self.db, "twin", "她昨天干嘛去了？")
        self.assertEqual(pub["reply"], onto_query.REFUSE)
        self.assertIsNone(pub["pending"])
        self.assertNotIn("Three Sheets", str(pub))


class Router:
    def __init__(self):
        self.systems = []

    def complete(self, system, messages, purpose="chat", images=()):
        self.systems.append(system)
        return SimpleNamespace(text="你昨天下午去练了下肢，晚上在 Three Sheets 吃饭")


class AskPageTest(Base):
    """“我”页问一问：她截图里的问法。用真实的“今天”（问一问不传日期）。"""

    def test_ask_yesterday(self):
        from ethan import workaholic, finance
        workaholic.ensure(self.db)
        finance.ensure(self.db)
        yday = clock.her_today() - timedelta(days=1)
        self.behave(yday)
        diary.save(self.db, yday.isoformat(), "下午 14:58 出门练下肢，晚上 19:24 去 Three Sheets 吃饭。", source="ethan")
        mepage.ensure(self.db)
        router = Router()
        got = mepage.ask(self.db, router, "我昨天干嘛去了")
        ethan, twin = got["answers"]
        self.assertEqual(len(router.systems), 1)  # Days 不问模型
        system = router.systems[0]
        for want in ("按题查到的", "第 29 题", "Three Sheets", "6.8 小时", "14:58 出门"):
            self.assertIn(want, system)
        self.assertTrue(ethan["sources"][0].startswith("按题查"))
        self.assertEqual(twin["answer"], onto_query.REFUSE)
        self.assertEqual(twin["sources"], [])
        logs = [tuple(r) for r in self.db.conn.execute("SELECT fn, user, status FROM function_log")]
        self.assertIn(("day_summary", "ethan", "answer"), logs)
        self.assertIn(("day_summary", "twin", "forbidden"), logs)
        via = self.db.conn.execute("SELECT via FROM retrieval_log WHERE user = 'ethan' ORDER BY id DESC LIMIT 1").fetchone()[0]
        self.assertEqual(via, "q29")


class FallbackSearchTest(Base):
    """兜底检索：“昨天”是日期不是内容。以前它把几个月前写着“昨天”的访谈原话排在最前。"""

    def test_date_words_not_matched_literally(self):
        today = clock.her_today()
        yday = (today - timedelta(days=1)).isoformat()
        for i in range(60):  # 撑大索引，让片段有区分度
            lifebase.add_evidence(self.db, "chat", f"raw:2026-08-01:{i}", "2026-08-01T10:00:00", f"mia：随便说说第{i}件事情")
        lifebase.add_evidence(self.db, "wechat", "wx:old", "2026-09-28T10:00:00", "昨天我和朋友去干嘛了来着，忘了")
        lifebase.add_evidence(self.db, "chat", f"raw:{yday}:9", f"{yday}T19:00:00+00:00", "mia：晚上在 Three Sheets 吃饭")
        memsearch.refresh(self.db)
        found = memsearch.search(self.db, ["我昨天干嘛去了"], user="ethan", log=False)
        self.assertTrue(found)
        self.assertIn("Three Sheets", found[0]["text"])
        plain = memsearch.search(self.db, ["昨天好累"], user="ethan", log=False)  # 不是在问哪天：照旧字面匹配
        self.assertTrue(any("忘了" in f["text"] for f in plain))


if __name__ == "__main__":
    unittest.main()
