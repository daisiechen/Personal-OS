import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import events
from ethan.core.agent import parse_reply
from ethan.memory import MemoryStore


class FakeCal:
    def __init__(self):
        self.put = []

    def ethan_calendar(self):
        return "https://cal/ethan/"

    def request(self, method, url, body=None, ctype=None):
        self.put.append((method, url, body.decode()))
        return 201, ""


class EventsTest(unittest.TestCase):
    def test_add_from_chat_goes_to_calendar_and_planner_busy(self):
        """2026-10-30 她发来周六午餐订位要进日历：以后的日程先登记，马上写日历，当天排计划时避开。"""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            text = "好，记上了 [日程 2099-10-31 14:45-16:45 午餐（4 人） @ Dishoom]"
            self.assertEqual(parse_reply(text)["bubbles"], ["好，记上了"])
            self.assertEqual(len(events.note_chat(store.db, text)), 1)
            self.assertEqual(len(events.note_chat(store.db, text)), 1)  # 说两遍不重复
            cal = FakeCal()
            self.assertEqual(events.sync(store.db, cal), 1)
            self.assertIn("SUMMARY:午餐（4 人）", cal.put[0][2])
            self.assertEqual(events.sync(store.db, cal), 0)  # 写过的不再写
            self.assertEqual(events.for_day(store.db, date(2099, 10, 31)), [("14:45", "16:45", "午餐（4 人）", {"location": "Dishoom"})])
            with self.assertRaises(ValueError):
                events.add(store.db, "2099-10-31", "16:00", "15:00", "反了")
            store.close()


if __name__ == "__main__":
    unittest.main()


class SameEventTest(unittest.TestCase):
    """她 11-04：“我的日历里怎么还有这么多重复事项？”——同一个约叫法不同也要认出来。"""

    def test_same_event_variants(self):
        from ethan.events import same_event as S
        self.assertTrue(S(("06:55", "07:55", "日出", "Primrose Hill Road"), ("06:55", "08:25", "票券：THE SUNRISE CLUB", "Primrose Hill")))
        self.assertTrue(S(("11:00", "13:00", "某商学院 交换生送别活动", "Garden Room"), ("11:00", "13:00", "IEPAUT26 Farewell Event", "Garden Room")))
        self.assertTrue(S(("10:30", "11:30", "递法签材料", ""), ("10:30", "12:30", "TLScontact 法国签证递签", "TLScontact 签证中心")))
        self.assertFalse(S(("19:00", "21:00", "和 Leo 吃饭", "Dishoom"), ("19:00", "21:00", "统计课", "某商学院")))  # 真撞了照样问她
        self.assertFalse(S(("10:30", "11:30", "递法签材料", ""), ("14:00", "15:00", "递法签材料", "")))

    def test_add_and_sync_skip_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            db = store.db
            a = events.add(db, "2099-11-09", "10:30", "12:30", "TLScontact 递交签证申请", "TLScontact")
            b = events.add(db, "2099-11-09", "10:30", "11:30", "递法签材料")
            self.assertEqual(a, b)  # 换了叫法还是同一个约

            class Cal:
                put = []

                def busy(self, day):
                    return [("11:00", "13:00", "IEPAUT26 Farewell Event", {"location": "Garden Room"})]

                def ethan_calendar(self):
                    return "https://cal/ethan/"

                def request(self, *a, **kw):
                    Cal.put.append(a)
            c = events.add(db, "2099-11-12", "11:00", "13:00", "某商学院 交换生送别活动", "Garden Room")
            events.sync(db, Cal())
            row = db.conn.execute("SELECT uid, conflict, note FROM future_events WHERE id = ?", (c,)).fetchone()
            self.assertTrue(row["uid"].startswith("dup:"))
            self.assertIsNone(row["conflict"])
            self.assertIn("IEPAUT26", row["note"])
            store.close()

    def test_dupes_report_file(self):
        import json
        from datetime import date

        class Cal:
            def all_events(self, day):
                return [("11:00", "13:00", "某商学院 交换生送别活动", {"location": "Garden Room", "calendar": "小伴 计划"}),
                        ("11:00", "13:00", "IEPAUT26 Farewell Event", {"location": "Garden Room", "calendar": "学校"})]
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(events.write_dupes_report(Cal(), tmp, date(2099, 11, 12), days=1), 1)
            got = json.loads((Path(tmp) / "calendar-dupes.json").read_text(encoding="utf-8"))
            self.assertIn("学校", got["pairs"][0]["b"])
            self.assertEqual(events.write_dupes_report(Cal(), tmp, date(2099, 11, 12), days=1), -1)  # 一小时内不重写
