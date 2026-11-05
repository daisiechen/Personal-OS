"""她 2026-11-02：邮件里读出 The Devonshire 15:00–17:00，和她日历上自己约的 Sunday Roast 撞了，没人找她确认。"""

import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from ethan import events, nudges
from ethan.memory import MemoryStore


class Cal:
    def __init__(self):
        self.puts = []

    def busy(self, day):
        return [("15:00", "17:00", "伦敦最火Sunday Roast", {})]

    def ethan_calendar(self):
        return "https://cal/ethan/"

    def request(self, method, url, body=None, ctype=None):
        self.puts.append(url)


class ClashTest(unittest.TestCase):
    def test_clash_is_held_and_asked(self):
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            db = store.db
            day = (date.today() + timedelta(days=1)).isoformat()
            eid = events.add(db, day, "15:00", "17:00", "The Devonshire 午餐", "Denman St", note="来自邮件")
            ok = events.add(db, day, "19:00", "20:00", "晚饭")
            cal = Cal()
            self.assertEqual(events.sync(db, cal), 1)  # 撞的那条先不写
            self.assertEqual(len(cal.puts), 1)
            self.assertEqual(events.conflicts(db)[0]["id"], eid)
            key, why = nudges._clash(db, datetime.now(), {})
            self.assertIn("Sunday Roast", why)
            self.assertIn(f"[日程确认 #{eid}]", why)
            events.note_chat(db, f"好，那就去 Devonshire [日程确认 #{eid}]")
            self.assertEqual(events.conflicts(db), [])
            self.assertEqual(events.sync(db, cal), 1)  # 她确认了才写
            self.assertEqual(events.strip(f"好 [日程取消 #{ok}]"), "好 ")
            store.close()


    def test_training_clash_with_new_calendar_event_is_asked(self):
        """她 2026-11-02：Sunday Roast 和训练撞了，为什么没先问要不要把训练往后推。"""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            db = store.db
            day = date(2026, 11, 2)
            db.save_plan(day.isoformat(), "model", "{}", "{}", "fp",
                         [{"kind": "event", "start": "15:30", "end": "16:30", "title": "训练：上肢", "priority": "main"}])
            iid = db.latest_plan(day.isoformat())["items"][0]["id"]
            busy = [("15:00", "17:00", "伦敦最火Sunday Roast", {})]
            self.assertEqual(len(events.find_plan_clashes(db, day, busy, "12:00")), 1)
            self.assertEqual(events.find_plan_clashes(db, day, busy, "12:00"), [])  # 同一个只记一次
            key, why = nudges._plan_clash(db, datetime(2026, 11, 2, 13, 0), {})
            self.assertIn(f"[挪 #{iid}", why)
            self.assertEqual(events.move_items(db, f"好，挪到晚上 [挪 #{iid} 18:00]"), [(iid, day.isoformat())])
            it = db.latest_plan(day.isoformat())["items"][0]
            self.assertEqual((it["start"], it["end"]), ("18:00", "19:00"))
            self.assertEqual(events.open_plan_clashes(db, day), [])  # 挪开了就不再问
            self.assertEqual(events.strip(f"好 [挪 #{iid} 18:00]"), "好 ")
            store.close()


if __name__ == "__main__":
    unittest.main()
