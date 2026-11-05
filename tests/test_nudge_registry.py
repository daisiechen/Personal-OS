"""打扰登记表（ontology.md 6.4 节，system-rules.md 第 7 节第 7 条）：所有主动消息和红点都登记过，没登记的不发。"""

import re
import unittest
from pathlib import Path

from ethan import nudge_registry, nudges

ROOT = Path(__file__).resolve().parent.parent / "ethan"


class NudgeRegistryTest(unittest.TestCase):
    def test_every_nudge_key_in_code_is_registered(self):
        src = (ROOT / "nudges.py").read_text(encoding="utf-8") + (ROOT / "weekly.py").read_text(encoding="utf-8") \
            + (ROOT / "perks.py").read_text(encoding="utf-8") + (ROOT / "weekend.py").read_text(encoding="utf-8")
        prefixes = set(re.findall(r'f"([a-z-]+:)\{', src)) | set(re.findall(r'return "([a-z-]+)", \(', src))
        prefixes |= set(nudges.WATER) | {nudges.CHECKIN_ASK}
        from ethan import weekend
        prefixes.add(weekend.PREFIX)
        missing = sorted(p for p in prefixes if not nudge_registry.registered(p + "x"))
        self.assertEqual(missing, [])

    def test_every_badge_is_registered_with_a_target(self):
        src = (ROOT / "workbench/api.py").read_text(encoding="utf-8")
        body = src[src.index("    def badges("):src.index("    def brain_page(")]
        keys = set(re.findall(r'out\["([a-z]+)"\]', body))
        self.assertEqual(sorted(keys - set(nudge_registry.BADGES)), [])

    def test_lookup_prefers_longest_prefix(self):
        self.assertEqual(nudge_registry.lookup("redeem-check:3:x:1")[0], "redeem-check:")
        self.assertEqual(nudge_registry.lookup("redeem:3:x")[0], "redeem:")
        self.assertIsNone(nudge_registry.lookup("新花样:1"))

    def test_unregistered_nudge_not_sent(self):
        from datetime import datetime, timezone
        from tests.test_proactive import Backend, FakeChannel, ProactiveTest
        from ethan import proactive
        t = ProactiveTest("test_quiet_hours")
        t.setUp()
        try:
            orig = proactive.nudges.candidates  # proactive 按优先顺序逐个看候选（11-05 起），没登记的跳过、不发
            proactive.nudges.candidates = lambda db, now_local, done: iter([("新花样:1", "随便说点什么")])
            ch = FakeChannel()
            out = proactive.tick(t.ethan, ch, now=datetime(2026, 10, 23, 13, 0, tzinfo=timezone.utc))
            self.assertIn("没登记，不发", out)
            self.assertEqual(ch.sent, [])
        finally:
            proactive.nudges.candidates = orig
            t.tearDown()


if __name__ == "__main__":
    unittest.main()
