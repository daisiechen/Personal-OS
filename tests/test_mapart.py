import unittest

from ethan import mapart


class MapArtTest(unittest.TestCase):
    def test_prompt_keeps_layout_and_forbids_text(self):
        for theme in ("night", "day"):
            p = mapart.prompt(theme, 1)
            self.assertIn("exactly seven bordering provinces", p)  # 七块接壤的领地
            self.assertIn("wasteland", p)  # 荒原
            self.assertIn("no text", p)  # 字由工作台叠上去
            self.assertIn("not childish", p)

    def test_every_variant_is_colourful_and_three_dimensional(self):
        for variant in range(3):
            p = mapart.prompt("night", variant)
            self.assertIn("clearly different dominant colour", p)  # 相邻领地颜色拉开
            self.assertIn("three-dimensional relief", p)  # 立体
            self.assertNotIn("Flat top-down", p)


if __name__ == "__main__":
    unittest.main()
