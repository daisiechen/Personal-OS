import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ethan import quests, review
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import MONDAY, make_demo


class ReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_weeks_freeze_after_they_end(self):
        h = quests.habit_by_title(self.db, "训练")
        start = MONDAY - timedelta(days=14)
        for i in range(14):
            quests.check_habit(self.db, h["id"], start + timedelta(days=i))
        season = {"name": "测试赛季", "start": start.isoformat(), "end": "2026-12-31"}
        page = review.page(self.db, self.root, MONDAY, season)
        self.assertEqual(len(page["weeks"]), 3)
        self.assertTrue(page["weeks"][0]["live"])
        self.assertFalse(page["weeks"][1]["live"])
        frozen = self.db.conn.execute("SELECT COUNT(*) FROM war_weeks").fetchone()[0]
        self.assertEqual(frozen, 2)  # 过完的两周存下来
        quests.check_habit(self.db, quests.habit_by_title(self.db, "吃够蛋白质")["id"], start)
        again = review.page(self.db, self.root, MONDAY, season)
        self.assertEqual(again["weeks"][2], {**page["weeks"][2]})  # 存下来的不再变
        body = next(t for t in page["weeks"][1]["territories"] if t["key"] == "body")
        self.assertEqual(next(r for r in body["rows"] if r["name"] == "训练")["actual"], "7 / 7 天")
        self.assertEqual(page["season"]["weeks"], 2)

    def test_season_not_started_yet_still_has_this_week(self):
        season = {"name": "测试赛季", "start": (MONDAY + timedelta(days=10)).isoformat(), "end": "2026-12-31"}
        page = review.page(self.db, self.root, MONDAY, season)
        self.assertEqual(len(page["weeks"]), 1)  # 之前是空列表，战报页直接报错
        self.assertTrue(page["weeks"][0]["live"])

    def test_season_points_include_this_week(self):
        season = {"name": "测试赛季", "start": MONDAY.isoformat(), "end": "2026-12-31"}
        quests.award(self.db, 110, "测试", MONDAY)
        page = review.page(self.db, self.root, MONDAY, season)
        self.assertEqual(page["season"]["weeks"], 0)  # 第一周还没过完
        self.assertEqual(page["season"]["points"], 110)  # 但积分算上本周，和首页对得上

    def test_ethan_weekly_text_and_feedback(self):
        self.store.raw.append("telegram", "ethan", "这周你超棒", at=datetime.combine(MONDAY + timedelta(days=6), datetime.min.time()).replace(hour=20), kind="weekly")
        page = self.wb.review_page(datetime.combine(MONDAY + timedelta(days=6), datetime.min.time()).replace(hour=21))
        self.assertTrue(any(w["ethan"] == "这周你超棒" for w in page["weeks"]))
        self.assertTrue(self.wb.act("review.feedback", {"note": "训练太满"})["ok"])
        self.assertIn("战报反馈：训练太满", [f["note"] for f in self.db.unused_feedback()])
        self.assertFalse(self.wb.act("review.feedback", {"note": " "})["ok"])

    def test_settings_page_and_toggles(self):
        page = self.wb.settings_page()
        names = [c["name"] for c in page["connections"]]
        self.assertIn("Monzo", names)
        self.assertTrue(page["prefs"]["proactive"])
        self.assertTrue(self.wb.act("settings.set", {"proactive": False})["ok"])
        self.assertFalse(self.wb.settings_page()["prefs"]["proactive"])
        self.assertTrue(self.wb.act("settings.set", {"quiet": "23:30-08:00"})["ok"])
        self.assertFalse(self.wb.act("settings.set", {"quiet": "不对"})["ok"])
        self.assertEqual(self.wb.settings_page()["prefs"]["quiet"], "23:30-08:00")


if __name__ == "__main__":
    unittest.main()
