"""每周复盘（每周日 21:00 约在日历上，小伴 主动来找；月末加月度、赛季末加赛季；工作台只看结果）。"""

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ethan import quests, weekreview
from ethan.core.agent import Ethan, parse_reply
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.proactive import tick

SEASON_END = date(2026, 12, 31)


class Backend:
    name = "subscription"

    def __init__(self):
        self.seen = []

    def complete(self, system, messages, images=()):
        self.seen.append(system)
        return Reply("到点啦，我们开会～\n1. 这周最满意的一件事", self.name, "m", 0.0)


class FakeChannel:
    owner = 42

    def __init__(self):
        self.sent = []

    def deliver(self, chat_id, bubbles, voices=()):
        self.sent.append(list(bubbles))


class Cal:
    """“小伴 计划”日历：events 是 {日期: [(开始, 结束, 标题)]}。"""

    def __init__(self, events=None):
        self.events, self.put = events or {}, []

    def own_events(self, day):
        return self.events.get(day, [])

    def ethan_calendar(self):
        return "https://cal/ethan/"

    def request(self, method, url, body=None, **k):
        self.put.append((method, url, body.decode() if body else ""))


class WeekReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        quests.seed(self.db)
        self.backend = Backend()
        self.settings = Settings(root / "settings.json")
        self.ethan = Ethan(self.store, Router(self.backend, Ledger(root / "ledger"), self.settings), persona="人设")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_layers_month_and_season(self):
        self.assertEqual(weekreview.layers(date(2026, 11, 8), SEASON_END), ["week"])
        self.assertEqual(weekreview.layers(date(2026, 11, 29), SEASON_END), ["week", "month"])  # 11 月最后一个周日
        self.assertEqual(weekreview.layers(date(2026, 12, 27), SEASON_END), ["week", "month", "season"])  # 赛季最后一个周日
        self.assertEqual(weekreview.layers(date(2026, 11, 30), SEASON_END), ["week", "month"])  # 挪到周一的算前一个周日

    def test_event_is_weekly_sunday_9pm_london(self):
        cal = Cal()
        self.assertTrue(weekreview.ensure_event(cal, self.settings, date(2026, 11, 2)))
        method, url, ics = cal.put[0]
        self.assertEqual((method, url), ("PUT", "https://cal/ethan/ethan-weekly-review.ics"))
        self.assertIn("DTSTART;TZID=Europe/London:20261108T210000", ics)
        self.assertIn("DTEND;TZID=Europe/London:20261108T220000", ics)
        self.assertIn("RRULE:FREQ=WEEKLY;BYDAY=SU", ics)
        self.assertIn("SUMMARY:和 小伴 周复盘", ics)
        self.assertFalse(weekreview.ensure_event(cal, self.settings, date(2026, 11, 2)))  # 只写一次
        self.assertEqual(len(cal.put), 1)

    def test_due_follows_calendar(self):
        sun = date(2026, 11, 8)
        at = lambda d, h, m=0: datetime(d.year, d.month, d.day, h, m, tzinfo=weekreview.LONDON)
        self.assertIsNone(weekreview.due(self.db, at(sun, 20, 50), SEASON_END))
        self.assertEqual(weekreview.due(self.db, at(sun, 21, 5), SEASON_END)["layers"], ["week"])
        self.assertIsNone(weekreview.due(self.db, at(sun, 22, 40), SEASON_END))  # 过了一个半小时不再开场
        # 她在日历上把这次挪到周一 20:00：周日不找，周一 20:00 找
        moved = Cal({date(2026, 11, 9): [("20:00", "21:00", weekreview.TITLE)]})
        self.assertIsNone(weekreview.due(self.db, at(sun, 21, 5), SEASON_END, moved))
        self.assertEqual(weekreview.due(self.db, at(date(2026, 11, 9), 20, 10), SEASON_END, moved)["start"], "20:00")
        # 删了那一次：那周不找
        self.assertIsNone(weekreview.due(self.db, at(sun, 21, 5), SEASON_END, Cal()))
        self.assertEqual(weekreview.busy_for(sun), [("21:00", "22:00", weekreview.TITLE, {"location": ""})])
        self.assertEqual(weekreview.busy_for(date(2026, 11, 9)), [])

    def test_tick_opens_review_then_chat_records_it(self):
        now = datetime(2026, 11, 8, 21, 5, tzinfo=timezone.utc)  # 11 月伦敦是 UTC+0
        self.store.raw.append("telegram", "mia", "在呢")  # 刚聊过也照样开场
        ch = FakeChannel()
        out = tick(self.ethan, ch, now=now)
        self.assertIn("复盘开场", out)
        self.assertEqual(ch.sent[-1][0].splitlines()[0], "到点啦，我们开会～")
        self.assertIn("周复盘", self.backend.seen[-1])
        self.assertIn("这周最满意的一件事", self.backend.seen[-1])
        self.assertIn("这周的钱", self.backend.seen[-1])  # 财务复盘并进每周复盘
        self.assertNotIn("这个月最大的变化", self.backend.seen[-1])
        self.assertIn("复盘记录 一句话小结", weekreview.chat_text(weekreview.current(self.db)))
        self.assertNotIn("复盘开场", tick(self.ethan, ch, now=now.replace(minute=15)))  # 开过一次就不再开
        # 她答完，小伴 收尾
        fin = quests.habit_by_title(self.db, "每周复盘") or {"id": quests.add_habit(self.db, "每周复盘", "weekly")}
        text = "好，今天就到这儿～[复盘记录 训练稳住了，工作超时 | 训练 5 次；工作 52 小时 | 周三起 21 点收工；签证周四催]"
        self.assertEqual(parse_reply(text)["bubbles"], ["好，今天就到这儿～"])
        self.ethan.note_records(text)
        r = weekreview.recent(self.db)[0]
        self.assertEqual((r["day"], r["status"], r["layers"]), ("2026-11-08", "done", ["这周"]))
        self.assertEqual(r["points"], ["训练 5 次", "工作 52 小时"])
        self.assertEqual(r["changes"], ["周三起 21 点收工", "签证周四催"])
        self.assertTrue(quests.checked(self.db, fin["id"], self._today()))
        self.assertIsNone(weekreview.current(self.db))

    def _today(self):
        from ethan.clock import her_now
        return her_now(self.settings).date()

    def test_month_end_opener_asks_more(self):
        p = weekreview.opener_prompt("人设", self.store, self.settings, date(2026, 12, 27), ["week", "month", "season"])
        for q in ("这个月最大的变化", "下个赛季给自己定一条硬线", "这个赛季"):
            self.assertIn(q, p)


if __name__ == "__main__":
    unittest.main()
