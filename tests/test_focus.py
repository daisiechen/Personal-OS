import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import focus
from ethan.core.agent import parse_reply
from ethan.memory import MemoryStore

SUNDAY = date(2026, 11, 1)


class FocusTest(unittest.TestCase):
    def test_propose_then_confirm_and_planner_sees_it(self):
        """2026-10-29 她选的 A：周报提 3 件下周重点，她点头或改，下周计划优先照顾。"""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            db = store.db
            focus.note(db, "下周加油 [下周重点 1. 连练 5 次 | 2. 订芬兰机票 | 3. 找 3 个内测用户]", SUNDAY)
            monday = SUNDAY + timedelta(days=1)
            self.assertEqual(focus.waiting(db, SUNDAY)["items"], ["连练 5 次", "订芬兰机票", "找 3 个内测用户"])
            self.assertFalse(focus.current(db, monday)["confirmed"])  # 没回也照提议的来
            focus.note(db, "好 [下周重点 定 连练 5 次 | 订芬兰机票和住宿 | 找 3 个内测用户]", monday)  # 她周一才改
            got = focus.current(db, monday)
            self.assertTrue(got["confirmed"])
            self.assertEqual(got["items"][1], "订芬兰机票和住宿")
            self.assertIsNone(focus.waiting(db, monday))
            self.assertEqual(parse_reply("好 [下周重点 定 a | b | c]")["bubbles"], ["好"])
            store.close()
