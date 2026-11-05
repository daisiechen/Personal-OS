"""文案规范：系统自动起的名字写成标题式（docs/design/ui-spec.md 第 5 节，她 2026-11-02 同意）。"""

import unittest

from ethan import naming


class NamingTest(unittest.TestCase):
    def test_title_style(self):
        self.assertEqual(naming.title("帮我查一下英语陪练的渠道吧～"), "查英语陪练的渠道")
        self.assertEqual(naming.title("记得给导师回邮件！"), "给导师回邮件")
        self.assertEqual(naming.title("递交芬兰签证材料"), "递交芬兰签证材料")  # 本来就好的不动
        long = "查一下伦敦冬天室内能去的运动课，最好是离家近、价格不贵、周末也开的那种，顺便看看有没有体验课"
        self.assertLessEqual(len(naming.title(long)), naming.MAX)
        self.assertEqual(naming.title("查伦敦冬天室内能去的运动课，最好离家近"), "查伦敦冬天室内能去的运动课，最好离家近")


if __name__ == "__main__":
    unittest.main()
