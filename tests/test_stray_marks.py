"""小伴 自己编了个系统不认的记号，原样发给了她。兜底：长得像记号的一律不发。"""

import unittest

from ethan.core.agent import parse_reply


class StrayMarkTest(unittest.TestCase):
    def test_unknown_marker_not_sent(self):
        got = parse_reply("明后天再没消息，你点一下发就行，[准备 #240 问了]\n我这边十一点多了")["bubbles"]
        self.assertFalse(any("#240" in b for b in got))
        self.assertIn("我这边十一点多了", got)

    def test_plain_text_with_hash_kept(self):
        self.assertEqual(parse_reply("想看 #3 号方案吗")["bubbles"], ["想看 #3 号方案吗"])


if __name__ == "__main__":
    unittest.main()
