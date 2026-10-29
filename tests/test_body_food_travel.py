import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import meals, training
from ethan.core.router import Settings
from ethan.memory import MemoryStore
from ethan.travel import Travel, depart_at

MONDAY = date(2026, 11, 2)
FORBIDDEN = ("葱", "胡萝卜", "生鱼", "刺身", "内脏", "鸭血")


class TrainingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_double_progression_rules(self):
        self.assertIsNone(training.next_weight([], 8, 12, 5)[0])
        full = [{"weight": 40, "reps": [12, 12, 12], "pain": 0}]
        self.assertEqual(training.next_weight(full, 8, 12, 5)[0], 45)
        partial = [{"weight": 40, "reps": [12, 11, 10], "pain": 0}]
        self.assertEqual(training.next_weight(partial, 8, 12, 5)[0], 40)
        twice_low = [{"weight": 40, "reps": [9, 7, 6], "pain": 0}, {"weight": 40, "reps": [8, 7, 7], "pain": 0}]
        self.assertEqual(training.next_weight(twice_low, 8, 12, 5)[0], 35)  # 降 10% 再取整到 5 kg
        self.assertEqual(training.next_weight([{"weight": 6, "reps": [15, 15, 15], "pain": 0}], 12, 15, 1)[0], 7)

    def test_prescribe_uses_template_history_and_pain_rule(self):
        rx = training.prescribe(self.db, MONDAY)
        self.assertEqual(rx["focus"], "下肢（臀为主）")
        self.assertEqual(rx["exercises"][0]["name"], "臀推机")
        self.assertIsNone(rx["exercises"][0]["weight"])
        self.assertIsNone(training.prescribe(self.db, date(2026, 11, 1)))
        failed = training.record(self.db, "[训练 臀推机 40x12/12/12 疼痛0] [训练 leg_press 60x10/9/9 疼痛5]",
                                 date(2026, 10, 26))
        self.assertEqual(failed, [])
        rx = training.prescribe(self.db, MONDAY)
        self.assertEqual(rx["exercises"][0]["weight"], 45)
        self.assertEqual(rx["exercises"][1]["name"], "臀桥")  # 腿举疼 5 分，换成更容易的
        self.assertIn("疼痛 5 分", rx["exercises"][1]["note"])
        self.assertIn("1. 臀推机：3 组 × 8–12 次，45 kg", training.describe(rx))

    def test_record_rejects_unknown_exercise(self):
        self.assertTrue(training.record(self.db, "[训练 深蹲架深蹲 50x5/5/5]", MONDAY))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM workout_sets").fetchone()[0], 0)

    def test_library_respects_gym_and_template_ids_exist(self):
        lib, tpl = training.load()
        self.assertFalse([k for k, v in lib.items() if "深蹲架" in v["equipment"] or "杠铃" in v["equipment"]])
        for day in tpl["days"].values():
            for item in day["exercises"]:
                self.assertIn(item["id"], lib)
        for v in lib.values():
            for alt in (v["easier"], v["harder"]):
                self.assertTrue(alt is None or alt in lib)
        self.assertTrue(all(s.startswith(("Schoenfeld", "American College")) for s in tpl["sources"]))


class MealsTest(unittest.TestCase):
    def test_hits_targets_with_simple_menu(self):
        menu = meals.pick(1570, 91, MONDAY)
        self.assertGreaterEqual(menu["protein"], 91)
        self.assertTrue(1420 <= menu["kcal"] <= 1670)
        self.assertEqual([r["meal"] for r in menu["meals"][:3]], ["早餐", "午餐", "晚餐"])
        self.assertLessEqual(len(menu["meals"]), 4)

    def test_avoids_yesterdays_lunch_and_dinner(self):
        first = meals.pick(1570, 91, MONDAY)
        avoid = [r["id"] for r in first["meals"]]
        second = meals.pick(1570, 91, MONDAY, avoid=avoid)
        main = lambda m: {r["id"] for r in m["meals"] if r["meal"] in ("午餐", "晚餐")}
        self.assertFalse(main(first) & main(second))

    def test_no_forbidden_food_and_all_air_fryer_simple(self):
        data = json.loads(meals.PLAYBOOK.read_text(encoding="utf-8"))
        for r in data["recipes"]:
            text = r["name"] + r["steps"] + "".join(r["items"])
            self.assertFalse([w for w in FORBIDDEN if w in text], r["name"])
            self.assertLessEqual(r["minutes"], 25)
            for k in r["items"]:
                self.assertIn(k, data["ingredients"])

    def test_describe_has_steps(self):
        text = meals.describe(meals.pick(1570, 91, MONDAY))
        self.assertIn("做法", text)
        self.assertIn("千卡", text)


class TravelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(Path(self.tmp.name) / "settings.json")
        self.settings.set("places", {"家": {"postcode": "AB1 2CD"}, "健身房": {"postcode": "EF3 4GH"}})

    def tearDown(self):
        self.tmp.cleanup()

    def test_tfl_fastest_route_cached_and_fallback(self):
        calls = []

        class Resp:
            def __init__(self, data):
                self.data = json.dumps(data).encode()

            def read(self):
                return self.data

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout):
            calls.append(req.full_url)
            return Resp({"journeys": [
                {"duration": 28, "legs": [{"instruction": {"summary": "46 bus"}}]},
                {"duration": 23, "legs": [{"instruction": {"summary": "Walk"}}, {"instruction": {"summary": "139 bus"}}]}]})

        travel = Travel(self.tmp.name, self.settings, opener=opener)
        r = travel.minutes("家", "健身房", MONDAY, "16:00")
        self.assertEqual((r["minutes"], r["route"], r["source"]), (23, "Walk → 139 bus", "tfl"))
        self.assertIn("AB12CD/to/EF34GH", calls[0])
        self.assertIn("timeIs=Arriving", calls[0])
        self.assertEqual(travel.minutes("家", "健身房", MONDAY, "16:00")["source"], "缓存")
        self.assertEqual(len(calls), 1)

        def broken(req, timeout):
            raise OSError("down")

        self.assertEqual(Travel(self.tmp.name + "/x", self.settings, opener=broken)
                         .minutes("家", "健身房", date(2026, 11, 3), "10:00")["minutes"], 30)
        self.assertEqual(Travel(self.tmp.name, self.settings).minutes("家", "没登记的地方", MONDAY, "10:00")["source"],
                         "默认")

    def test_depart_time(self):
        """出发 = 开始 − 路上 − 20 分钟（2026-10-29 她定），按 5 分钟往前取整。"""
        self.assertEqual(depart_at("16:00", 23), "15:15")
        self.assertEqual(depart_at("09:00", 30), "08:10")


if __name__ == "__main__":
    unittest.main()
