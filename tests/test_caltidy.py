"""日历整理（导入的事件换成中文、时间地点填进去、要点进描述）：原文保留、提醒不丢、改过的不再动、能恢复。"""
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import caltidy
from ethan.memory import MemoryStore

ICS = ("BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\nUID:abc@x\r\nDTSTART;VALUE=DATE:20991110\r\nDTEND;VALUE=DATE:20991111\r\n"
       "SUMMARY:IEPAUT26 Careers Workshop\r\nDESCRIPTION:Join us in Room LG.01\\, Sammy Ofer Centre\\, from 18:30 to 20:00. "
       "Please bring your CV. Dress code: smart casual." + "x" * 150 + "\r\n"
       "BEGIN:VALARM\r\nACTION:DISPLAY\r\nDESCRIPTION:Reminder\r\nTRIGGER:-PT30M\r\nEND:VALARM\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
GOT = {"changed": True, "title": "交换生职业工作坊", "location": "Sammy Ofer Centre LG.01", "start": "18:30", "end": "20:00",
       "notes": ["带简历", "着装：商务休闲"]}


class Router:
    def complete(self, system, messages, purpose="chat", images=()):
        return SimpleNamespace(text=json.dumps(GOT, ensure_ascii=False))


class Cal:
    def __init__(self):
        self.store = {"https://cal/home/abc.ics": ICS}

    def calendars(self):
        return {"家": "https://cal/home/", "小伴 计划": "https://cal/ethan/"}

    def request(self, method, url, body=None, depth=None, ctype=None):
        if method == "PUT":
            self.store[url] = body.decode()
            return 201, ""
        if method == "REPORT" and url == "https://cal/home/":
            resp = "".join(f"<d:response><d:href>{h}</d:href><d:propstat><d:prop><c:calendar-data>{i.replace('&', '&amp;').replace('<', '&lt;')}"
                           f"</c:calendar-data></d:prop></d:propstat></d:response>" for h, i in self.store.items())
            return 207, f'<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{resp}</d:multistatus>'
        return 207, ""


class CalTidyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_preview_does_not_write(self):
        cal = Cal()
        got = caltidy.run(self.store.db, cal, Router(), start=date(2099, 11, 10), days=1)
        self.assertEqual(got[0]["after"]["title"], "交换生职业工作坊")
        self.assertEqual(cal.store["https://cal/home/abc.ics"], ICS)
        GOT2 = dict(GOT, title="别的叫法")

        class Other(Router):
            def complete(self, *a, **kw):
                return SimpleNamespace(text=json.dumps(GOT2, ensure_ascii=False))
        caltidy.run(self.store.db, cal, Other(), start=date(2099, 11, 10), days=1, preview=False)  # 照预览过的那份写
        self.assertEqual(caltidy.master(caltidy.unfold(cal.store["https://cal/home/abc.ics"]))["title"], "交换生职业工作坊")

    def test_apply_keeps_original_alarm_and_marks(self):
        cal = Cal()
        caltidy.run(self.store.db, cal, Router(), start=date(2099, 11, 10), days=1, preview=False)
        new = cal.store["https://cal/home/abc.ics"]
        ev = caltidy.master(caltidy.unfold(new))
        self.assertEqual((ev["title"], ev["location"]), ("交换生职业工作坊", "Sammy Ofer Centre LG.01"))
        self.assertIn("· 带简历", ev["description"])
        self.assertIn("原标题：IEPAUT26 Careers Workshop", ev["description"])
        self.assertIn("Please bring your CV", ev["description"])  # 原文一字不改留着
        self.assertIn("DTSTART;TZID=Europe/London:20991110T183000", new)  # 全天事件换成描述里写的时间
        self.assertIn("DESCRIPTION:Reminder", new)  # 提醒还在
        self.assertTrue(ev["tidied"])
        self.assertEqual(caltidy.run(self.store.db, cal, Router(), start=date(2099, 11, 10), days=1, preview=False), [])  # 整理过的不再动
        self.assertEqual(caltidy.undo(self.store.db, cal), 1)
        self.assertEqual(cal.store["https://cal/home/abc.ics"].replace("\r\n", "\n"), ICS.replace("\r\n", "\n"))  # 恢复成原样

    def test_recurring_keeps_time_and_chinese_left_alone(self):
        rec = ICS.replace("DTEND;VALUE=DATE:20991111\r\n", "DTEND;VALUE=DATE:20991111\r\nRRULE:FREQ=WEEKLY\r\n")
        ev = caltidy.master(caltidy.unfold(rec))
        self.assertTrue(ev["recurring"])
        self.assertNotIn("T183000", caltidy.rewrite(rec, ev, GOT))
        zh = ICS.replace("SUMMARY:IEPAUT26 Careers Workshop", "SUMMARY:职业工作坊").replace("Room LG.01", "x")
        self.assertTrue(caltidy.needs_tidy(caltidy.master(caltidy.unfold(zh))))  # 描述长、没填地点：还是要整理
        done = zh.replace("END:VEVENT", "LOCATION:LG.01\r\nEND:VEVENT", 1)
        self.assertFalse(caltidy.needs_tidy(caltidy.master(caltidy.unfold(done))))
        self.assertFalse(caltidy.is_on(self.store.db))  # 默认关着，她看过预览再开


if __name__ == "__main__":
    unittest.main()
