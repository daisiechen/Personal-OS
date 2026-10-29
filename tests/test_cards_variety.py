import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import cards
from tests.test_workbench import make_demo


class CardVarietyTest(unittest.TestCase):
    """2026-10-28 她说昨天和今天的卡长得太像：地点、穿搭、镜头一周内不重样，同类天气也换场景。"""

    def test_week_without_repeats_and_prompt_avoids_yesterday(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "cards").mkdir()
            day = date(2026, 11, 2)
            seen = {"place": [], "outfit": [], "camera": [], "pose": []}  # 脸的角度也一周不重样（2026-10-29）
            for i in range(7):
                v = cards.variety(root, day + timedelta(days=i))
                for k in seen:
                    self.assertNotIn(v[k], seen[k])
                    seen[k].append(v[k])
                (root / "cards" / f"{(day + timedelta(days=i)).isoformat()}.json").write_text(
                    json.dumps({"code": "W4", "variety": v}))
            card = {"code": "W4", "weather": "", "variety": cards.variety(root, day + timedelta(days=7)),
                    "avoid": "x, y, z"}
            text = cards.prompt(card)
            self.assertIn("Setting:", text)
            self.assertIn("different from yesterday", text)
            self.assertIn("do not copy their head angle", text)

    def test_same_cloudy_scene_not_two_days_running(self):
        w = {"code": 3, "wind": 10, "min": 8, "max": 13}
        monday = date(2026, 11, 2)
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            first = cards.choose(store.db, monday, None, w, [])["code"]
            second = cards.choose(store.db, monday + timedelta(days=1), None, w, [{"code": first}])["code"]
            store.close()
        self.assertEqual(first, "W4")
        self.assertNotEqual(second, first)


if __name__ == "__main__":
    unittest.main()


class CardFeedbackTest(unittest.TestCase):
    def test_her_card_feedback_goes_into_prompt(self):
        """2026-10-29 她问提的意见有没有被参考：聊天里记下的反馈，画卡时带上（只挑说卡的）。"""
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            store.db.add_feedback("她说这两天的今日卡长得一样，不喜欢")
            store.db.add_feedback("她说训练计划太满")
            day = date.today()
            notes = cards.her_notes(store.db, day)
            self.assertEqual(notes, ["她说这两天的今日卡长得一样，不喜欢"])
            text = cards.prompt({"code": "W4", "weather": "", "notes": notes})
            self.assertIn("长得一样", text)
            store.close()
