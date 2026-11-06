"""领域标准表（她的决定 8）：领地灯和属性分按同一张表算；两边原来不一样的，松的当保底、严的当目标。"""

import re
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import standards

ROOT = Path(__file__).resolve().parent.parent


class StandardsTest(unittest.TestCase):
    def test_table(self):
        for k, s in standards.STANDARDS.items():
            self.assertLessEqual(s.floor, s.target, k)
            self.assertIn(s.per, ("week", "month"))
            self.assertTrue(s.was and s.why, k)

    def test_decision_8_numbers(self):
        """松的当保底、严的当目标（数字列在 docs/design/territories.md 给她过目）。"""
        want = {"training": (4, 5), "protein": (5, 7), "sleep": (5, 7), "outside": (6, 7), "english": (5, 5),
                "cognitive": (5, 7), "new_exp": (1, 1), "outing": (1, 1), "new_friend": (3, 4)}
        for k, (floor, target) in want.items():
            s = standards.get(k)
            self.assertEqual((s.floor, s.target), (floor, target), k)

    def test_full_and_labels(self):
        d = date(2026, 11, 6)
        self.assertAlmostEqual(standards.full("training", d - timedelta(days=29), d), 5 * 30 / 7)
        self.assertAlmostEqual(standards.full("new_friend", d - timedelta(days=29), d), 4)
        self.assertEqual(standards.week_label("sleep", "target"), "每天")
        self.assertEqual(standards.week_label("training", "floor"), "每周 4 天")
        self.assertEqual(standards.week_label("research"), "每周 2 小时")

    def test_territory_and_attributes_have_no_own_numbers(self):
        """领地灯、属性分里不再自己写这些标准的数字。"""
        terr = (ROOT / "ethan/territory.py").read_text(encoding="utf-8")
        attr = (ROOT / "ethan/attributes.py").read_text(encoding="utf-8")
        self.assertNotRegex(terr, r"_paced\(n, \d")
        self.assertNotRegex(terr, r'\("训练", "训练", \d')
        self.assertIsNone(re.search(r"_ratio\(\w+, \d+ \* _k", attr))  # 社交两项 11-06 第一批合进来后也接上了表
        self.assertNotIn("每月 3–4 个", terr)
        self.assertNotIn('"每周 1 次", f"{met} 次"', terr)


if __name__ == "__main__":
    unittest.main()
