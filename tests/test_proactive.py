import tempfile
import unittest
from datetime import date, datetime, time, timezone
from pathlib import Path

from ethan.core.agent import Ethan
from ethan.core.backends import BackendUnavailable, Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.proactive import SLOTS, in_quiet, parse_quiet, planned_time, set_proactive, tick

DAY = date(2026, 10, 23)  # 伦敦夏令时，UTC+1


def london(h, m=0):
    return datetime(2026, 10, 23, h - 1, m, tzinfo=timezone.utc)


class Backend:
    name = "subscription"

    def __init__(self, text="早呀小宝\n今天先把 PPT 标题页写了"):
        self.text, self.seen, self.fail = text, [], False

    def complete(self, system, messages, images=()):
        if self.fail:
            raise BackendUnavailable("rate_limit")
        self.seen.append((system, messages))
        return Reply(self.text, self.name, "m", 0.0)


class FakeChannel:
    owner = 42

    def __init__(self):
        self.sent = []

    def deliver(self, chat_id, bubbles, voices=()):
        self.sent.append((chat_id, list(bubbles), list(voices)))


class ProactiveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.backend = Backend()
        self.settings = Settings(root / "settings.json")
        self.ethan = Ethan(self.store, Router(self.backend, Ledger(root / "ledger"), self.settings),
                           persona="人设")
        self.channel = FakeChannel()
        self.morning = SLOTS[0]
        self.at = planned_time(DAY, self.morning)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_at(self, h, m=0):
        """只看主动消息部分；前面可能有“写了计划”的日志。这里模型回的不是剧本格式，所以走固定时段。"""
        return tick(self.ethan, self.channel, now=london(h, m)).split("；")[-1]

    def test_quiet_hours(self):
        quiet = parse_quiet("23:30-08:00")
        self.assertTrue(in_quiet(time(0, 10), quiet))
        self.assertTrue(in_quiet(time(23, 45), quiet))
        self.assertFalse(in_quiet(time(9, 0), quiet))
        with self.assertRaises(ValueError):
            parse_quiet("晚上")

    def test_planned_time_is_inside_window_and_stable(self):
        for slot in SLOTS:
            t = planned_time(DAY, slot)
            self.assertTrue(slot.start <= t < slot.end, slot.name)
            self.assertEqual(t, planned_time(DAY, slot))

    def test_morning_sent_once_and_logged(self):
        self.assertEqual(self.run_at(self.at.hour, self.at.minute), "morning：已发送")
        self.assertEqual(self.channel.sent, [(42, ["早呀小宝", "今天先把 PPT 标题页写了"], [])])
        self.assertIn("早上第一次找她", self.backend.seen[-1][0])
        self.assertEqual(self.store.raw.recent(5)[-1]["kind"], "proactive")
        self.assertEqual(self.run_at(9, 25), "没有到点的时段")
        self.assertEqual(len(self.channel.sent), 1)

    def test_nothing_before_planned_time_or_outside_slots(self):
        if self.at > time(8, 0):
            self.assertEqual(self.run_at(8, 0), "没有到点的时段")
        self.assertEqual(self.run_at(11, 0), "没有到点的时段")
        self.assertEqual(self.channel.sent, [])

    def test_skips_when_she_just_talked(self):
        self.store.raw.append("telegram", "mia", "早", at=london(self.at.hour, self.at.minute) .replace(minute=0))
        self.assertEqual(self.run_at(self.at.hour, self.at.minute), "morning：刚聊过")
        self.assertEqual(self.channel.sent, [])

    def test_quiet_hours_win(self):
        self.settings.set("quiet_hours", "00:00-10:00")
        self.assertEqual(self.run_at(self.at.hour, self.at.minute), "morning：免打扰")
        self.assertEqual(self.channel.sent, [])

    def test_optional_slot_skipped_if_last_one_unanswered(self):
        self.run_at(self.at.hour, self.at.minute)
        afternoon = planned_time(DAY, SLOTS[1])
        self.assertEqual(self.run_at(afternoon.hour, afternoon.minute), "afternoon：上一条还没回")
        self.assertEqual(len(self.channel.sent), 1)

    def test_model_can_choose_silence_and_failure_retries(self):
        self.backend.fail = True
        self.assertIn("模型不可用", self.run_at(self.at.hour, self.at.minute))
        self.backend.fail, self.backend.text = False, "[不回]"
        self.assertEqual(self.run_at(self.at.hour, self.at.minute), "morning：模型选择不打扰")
        self.assertEqual(self.channel.sent, [])
        self.assertEqual(self.store.raw.recent(5), [])

    def test_off_switch_and_commands(self):
        self.assertIn("先不主动找你", set_proactive(self.settings, "/proactive off"))
        self.assertEqual(self.run_at(self.at.hour, self.at.minute), "主动消息已暂停")
        set_proactive(self.settings, "/proactive on")
        self.assertIn("23:30-08:00", set_proactive(self.settings, "/quiet 23:30-08:00"))
        self.assertEqual(self.settings.get("quiet_hours"), "23:30-08:00")
        self.assertIn("没改成", set_proactive(self.settings, "/quiet 半夜"))
        self.assertIn("23:30-08:00", set_proactive(self.settings, "/quiet"))

    def test_unbound_does_nothing(self):
        self.channel.owner = None
        self.assertEqual(self.run_at(self.at.hour, self.at.minute), "未绑定")


if __name__ == "__main__":
    unittest.main()
