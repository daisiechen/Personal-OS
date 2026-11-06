import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from ethan import nudges, places_log, worktime
from tests.test_workbench import MONDAY, make_demo

LDN = ZoneInfo("Europe/London")


def at(h, m=0, day=MONDAY):
    return datetime(day.year, day.month, day.day, h, m, tzinfo=LDN)


class NudgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_follow_up_after_planned_start(self):
        item = next(i for i in self.db.latest_plan(MONDAY.isoformat())["items"] if i.get("start") and i["kind"] != "travel")
        h, m = map(int, item["start"].split(":"))
        start = at(h, m)
        self.assertIsNone(nudges._follow(self.db, start + timedelta(minutes=5), {}))  # 还早
        key, why = nudges._follow(self.db, start + timedelta(minutes=25), {})
        self.assertEqual(key, f"follow:{item['start']}:{item['title']}")  # 按时间和事认，重排后不重问
        self.assertIn(item["title"], why)
        self.assertIsNone(nudges._follow(self.db, start + timedelta(minutes=30), {key: "已发送"}))  # 一件只问一次

    def test_sitting_water_and_outing(self):
        worktime.ensure(self.db)
        now = at(14, 32)
        slots = [worktime.slot_of(now - timedelta(minutes=5 * k)) for k in range(1, 20)]
        worktime.record(self.db, slots, "mac")
        self.assertEqual(nudges._sitting(self.db, now, {})[0], "sit")  # 两次隔 2 小时由 proactive.gate 管（tests/test_nudge_gate.py）
        self.assertIsNone(nudges._water(self.db, now, {"water-am": 1}))
        self.assertEqual(nudges._water(self.db, at(15, 5), {})[0], "water-pm")
        self.assertIsNone(nudges._water(self.db, at(15, 5), {"water-pm": "已发送"}))
        self.assertIsNone(nudges._outing(self.db, at(17, 45), {}))  # 没接定位就不催
        places_log.record(self.db, "arrive", now=at(9))  # 接上了定位，今天没出门
        key, why = nudges._outing(self.db, at(17, 45), {})
        self.assertEqual(key, "outing")
        self.assertIn("吸引", why)  # 不光催，要用具体好玩的去处勾她出门
        from ethan import brain
        brain._put(self.db, "saved", "saved:x", "运河边的二手书店", tags=["地点"], status="kept",
                   extra={"place": {"name": "Word on the Water", "area": "King's Cross"}})
        self.assertIn("Word on the Water", nudges._outing(self.db, at(17, 45), {})[1])
        places_log.record(self.db, "leave", now=at(17, 50))
        self.assertIsNone(nudges._outing(self.db, at(18, 0), {}))


if __name__ == "__main__":
    unittest.main()


class DepartNudgeTest(NudgeTest):
    def test_calendar_trip_reminds_once_with_route(self):
        """她日历里的递签证没有出发提醒；现在提前 1 小时说清去哪、路上多久、几点出门。"""
        import json
        plan = self.db.latest_plan(MONDAY.isoformat())
        brief = json.loads(plan["brief"])
        trip = {"title": "签证办理", "place": "签证中心", "start": "11:30", "leave": "10:30", "remind": "09:30",
                "trip_min": 40, "route": "Victoria line"}
        brief["timeline"] = [{"at": "09:30", "icon": "⏰", "text": "准备出发去签证办理", "trip": trip}]
        self.db.conn.execute("UPDATE daily_plans SET brief = ? WHERE id = ?", (json.dumps(brief, ensure_ascii=False), plan["id"]))
        self.db.conn.commit()
        self.assertIsNone(nudges._depart(self.db, at(9, 0), {}))
        key, why = nudges._depart(self.db, at(9, 40), {})
        self.assertIn("10:30", why)
        self.assertIn("路上约 40 分钟", why)
        self.assertIsNone(nudges._depart(self.db, at(9, 45), {key: "已发送"}))


class SurpriseOnceTest(NudgeTest):
    def test_reveal_only_once_across_days(self):
        """拆了惊喜，小伴 主动揭晓一次；第二天不会再“刚拆开”一遍（自查）。"""
        from ethan import quests
        quests.seed(self.db)
        quests.award(self.db, 2500, "测试", MONDAY)
        small = self.db.conn.execute("SELECT id FROM rewards WHERE tier = '小'").fetchone()["id"]
        quests.redeem(self.db, small, MONDAY)
        key, _ = nudges.due(self.db, at(10), {})
        self.assertTrue(key.startswith("redeem:"))
        nudges.mark_told(self.db, [key])
        got = nudges.due(self.db, at(10, day=MONDAY + timedelta(days=1)), {})
        self.assertFalse(got and got[0].startswith("redeem:"))


class SurpriseInChatTest(NudgeTest):
    def test_ethan_checks_back_and_confirms_surprise(self):
        """别让她点“兑现了”：揭晓两天后 小伴 问一句玩了没有，由他记兑现。"""
        from datetime import datetime
        from ethan import quests
        quests.seed(self.db)
        quests.award(self.db, 2500, "测试", MONDAY)
        small = self.db.conn.execute("SELECT id FROM rewards WHERE tier = '小'").fetchone()["id"]
        quests.redeem(self.db, small, MONDAY)
        r = quests.pending_redemptions(self.db)[0]
        noon = datetime(2026, 11, 5, 12, 0)
        self.assertIsNone(nudges._surprise_check(self.db, noon, {}))  # 还没揭晓过
        nudges.mark_told(self.db, [nudges.redeem_key(r)])
        self.db.conn.execute("UPDATE nudge_once SET at = '2026-11-02 10:00:00'")
        key, why = nudges._surprise_check(self.db, noon, {})
        self.assertIn("已兑现", why)
        nudges.mark_told(self.db, [key])
        self.assertIsNone(nudges._surprise_check(self.db, noon, {}))  # 同一轮只问一次
        quests.deliver(self.db, r["id"], "玩得很开心")
        self.assertIsNone(nudges._surprise_check(self.db, datetime(2026, 11, 9, 12, 0), {}))

    def test_asks_about_every_prep_item_spaced_out(self):
        """小伴 记下要替她查体态评估，没来问她。她说“所有都问，但别同一时间问”：一次一件，隔 2 小时。"""
        from datetime import datetime
        t1 = self.db.add_task("查伦敦能做 physio 体态评估的诊所", promised_by_ethan=True)
        t2 = self.db.add_task("比较芬兰租车", promised_by_ethan=True)
        noon = datetime(2026, 11, 2, 12, 0)
        self.db.conn.execute("UPDATE tasks SET done = 1 WHERE promised_by_ethan = 1 AND id NOT IN (?, ?)", (t1, t2))
        key, why = nudges._prep_ask(self.db, noon, {})
        self.assertIn(key, (f"prep-ask:{t1}", f"prep-ask:{t2}"))
        self.assertIn("准备 #", why)
        nudges.mark_told(self.db, [key])
        key2, _ = nudges._prep_ask(self.db, noon, {})  # 问过的不再问，轮到下一件；隔 2 小时由 proactive.gate 管（tests/test_nudge_gate.py）
        self.assertNotEqual(key2, key)
        self.assertIsNone(nudges._prep_ask(self.db, datetime(2026, 11, 2, 22, 0), {}))  # 晚上不问

    def test_evening_asks_what_is_left_to_check_in(self):
        """她常忘了打卡。9 点后 小伴 问一句还有哪几项，问过之后她回“做了”就能替她打卡。"""
        from datetime import datetime
        from ethan import quests
        quests.seed(self.db)
        evening = datetime(2026, 11, 2, 21, 30)
        self.assertIsNone(nudges._checkin(self.db, datetime(2026, 11, 2, 15, 0), {}))  # 白天不问
        key, why = nudges._checkin(self.db, evening, {})
        self.assertIn("[打卡", why)
        self.assertFalse(nudges.checkin_asked_recently(self.db))
        nudges.mark_told(self.db, [key])
        self.assertTrue(nudges.checkin_asked_recently(self.db))
        self.assertIsNone(nudges._checkin(self.db, evening, {}))  # 一晚只问一次

    def test_chatting_ethan_brings_up_untold_surprise(self):
        """她正聊着天时主动消息会被“刚聊过”挡住，所以聊天里 小伴 要主动提没说过的惊喜（线上）。"""
        from ethan import quests
        from ethan.core.agent import build_system_prompt
        quests.seed(self.db)
        quests.award(self.db, 2500, "测试", MONDAY)
        small = self.db.conn.execute("SELECT id FROM rewards WHERE tier = '小'").fetchone()["id"]
        quests.redeem(self.db, small, MONDAY)
        r = quests.pending_redemptions(self.db)[0]
        game = {**quests.today_summary(self.db, MONDAY), "pending": [{**r, "told": False}]}
        self.assertIn("这次回复里就主动提", build_system_prompt("人设", {"today": MONDAY.isoformat(), "game": game}))
        game["pending"] = [{**r, "told": True}]
        self.assertNotIn("这次回复里就主动提", build_system_prompt("人设", {"today": MONDAY.isoformat(), "game": game}))
