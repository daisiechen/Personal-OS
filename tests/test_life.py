import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ethan import life
from ethan.clock import home_now, time_brief
from ethan.core.agent import Ethan, build_system_prompt, parse_reply, to_messages
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.proactive import tick

SCRIPT = """## 时间线
- 05:30–06:30 深圳湾晨跑，被大爷超车
- 09:00–12:00 组会

## 细节
团子把咖啡豆打翻了

## 想跟她分享的
被大爷超车

## 惦记她的事
无

## 找她的时刻
- 16:20 分享早上被大爷超车的事
- 05:40 去晨跑前提醒她放下手机拉伸
"""


def utc(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class Backend:
    """按请求内容回不同的东西：写计划/剧本时回 plan_text，其余回 chat_text。"""
    name = "subscription"

    def __init__(self, plan_text="计划", chat_text="在呢"):
        self.plan_text, self.chat_text, self.seen = plan_text, chat_text, []

    def complete(self, system, messages, images=()):
        self.seen.append((system, messages))
        text = self.plan_text if system.startswith("你是 小伴 本人，在给自己写") else self.chat_text
        return Reply(text, self.name, "m", 0.0)


class FakeChannel:
    owner = 42

    def __init__(self):
        self.sent = []

    def deliver(self, chat_id, bubbles, voices=()):
        self.sent.append((chat_id, list(bubbles), list(voices)))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.backend = Backend()
        self.settings = Settings(root / "settings.json")
        self.router = Router(self.backend, Ledger(root / "ledger"), self.settings)
        self.ethan = Ethan(self.store, self.router, persona="人设")
        self.wiki = self.store.wiki

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class ClockTest(unittest.TestCase):
    def test_time_brief_shows_both_cities_weekday_and_gap(self):
        now = utc(2026, 10, 23, 18, 40)  # 伦敦夏令时 19:40，深圳 02:40
        records = [{"role": "mia", "at": "2026-10-23T13:40:00+00:00", "text": "嗯"}]
        brief = time_brief(records, now=now)
        self.assertIn("伦敦）现在：2026-10-23 周五 19:40", brief)
        self.assertIn("深圳）现在：2026-10-24 周六 02:40", brief)
        self.assertIn("她上一条消息：5 小时前", brief)

    def test_winter_time_shift(self):
        self.assertEqual(home_now(utc(2026, 10, 26, 12)).hour, 20)  # 冬令时后差 8 小时

    def test_home_day_starts_at_0430(self):
        self.assertEqual(life.home_day(utc(2026, 10, 23, 20, 0)), date(2026, 10, 23))  # 深圳 04:00
        self.assertEqual(life.home_day(utc(2026, 10, 23, 20, 40)), date(2026, 10, 24))  # 深圳 04:40


class LifeTest(Case):
    def test_moments_parsed_only_from_their_section(self):
        self.assertEqual(life.moments(SCRIPT), [("16:20", "分享早上被大爷超车的事"),
                                                ("05:40", "去晨跑前提醒她放下手机拉伸")])

    def test_ensure_plans_writes_each_layer_once(self):
        now = utc(2026, 10, 23, 23)  # 深圳 10-24 07:00
        written = life.ensure_plans(self.store, self.router, "人设", self.settings, now)
        self.assertEqual(written, ["ethan-life/year", "ethan-life/month-2026-10", "ethan-life/week-2026-w43",
                                   "ethan-life/day-2026-10-24"])
        prompt = self.backend.seen[-1][1][0]["content"]
        self.assertIn("找她的时刻", prompt)
        self.assertIn("深圳 05:00 = 伦敦 22:00", prompt)
        self.assertIn("剧本写作指南", self.backend.seen[-1][0])
        self.assertNotIn("参考来源", self.backend.seen[-1][0])
        self.assertEqual(life.ensure_plans(self.store, self.router, "人设", self.settings, now), [])

    def test_markers_change_script_and_remember_her_things(self):
        now = utc(2026, 10, 23, 23)
        self.wiki.write("ethan-life/day-2026-10-24", "小伴 的一天", SCRIPT)
        text = "好呀\n[剧本 答应今晚陪她看电影，推掉网球][惦记 2026-10-24 14:00 阿明约她吃饭]"
        self.assertEqual(life.note_changes(self.wiki, text, now), 2)
        body = self.wiki.read("ethan-life/day-2026-10-24")["body"]
        self.assertIn("## 今天临时变化\n- 07:00 答应今晚陪她看电影，推掉网球", body)
        self.assertEqual(life.her_things(self.wiki)[0]["what"], "阿明约她吃饭")
        self.assertEqual(parse_reply(text)["bubbles"], ["好呀"])

    def test_her_thing_due_after_it_happens_and_only_once(self):
        life.add_her_thing(self.wiki, "2026-10-24", "14:00", "面试")
        london = utc(2026, 10, 24, 13, 30).astimezone(life.her_now().tzinfo)
        self.assertIsNone(life.due_her_thing(self.wiki, london))
        later = utc(2026, 10, 24, 14, 10).astimezone(london.tzinfo)
        thing = life.due_her_thing(self.wiki, later)
        self.assertEqual(thing["what"], "面试")
        life.mark_asked(self.wiki, thing)
        self.assertIsNone(life.due_her_thing(self.wiki, later))


class AgentTimeTest(Case):
    def test_prompt_has_time_and_today_script_but_hides_life_pages_from_index(self):
        self.wiki.write("about-mia", "关于", "在伦敦")
        self.wiki.write(life.day_slug(life.home_day()), "一天", SCRIPT)
        self.ethan.reply("telegram", "在干嘛")
        system, messages = self.backend.seen[-1]
        self.assertIn("她那边（伦敦）现在", system)
        self.assertIn("你今天的剧本", system)
        self.assertIn("被大爷超车", system)
        self.assertNotIn("ethan-life/day", system)
        self.assertNotIn("她上一条消息", system)  # 她刚发的这条不算“上一条”
        self.assertRegex(messages[-1]["content"], r"^【\d{2}-\d{2} \d{2}:\d{2}】在干嘛$")

    def test_today_follows_london_not_server(self):
        ctx = self.ethan.context(now=utc(2026, 10, 23, 22, 30))  # 伦敦 23:30，旧服务器已是 24 号
        self.assertEqual(ctx["today"], "2026-10-23")

    def test_to_messages_without_tz_unchanged(self):
        self.assertEqual(to_messages([{"role": "mia", "at": "2026-10-23T10:00:00+00:00", "text": "早"}]),
                         [{"role": "user", "content": "早"}])

    def test_system_prompt_tells_him_he_can_lead(self):
        prompt = build_system_prompt("人设", {"today": "2026-10-23"})
        self.assertIn("不用每句都以问她结尾", prompt)
        self.assertIn("[惦记 YYYY-MM-DD HH:MM 事情]", prompt)


class ScriptDrivenProactiveTest(Case):
    def setUp(self):
        super().setUp()
        self.channel = FakeChannel()
        for slug in ("ethan-life/year", "ethan-life/month-2026-10", "ethan-life/week-2026-w43"):
            self.wiki.write(slug, "计划", "计划")
        self.wiki.write("ethan-life/day-2026-10-23", "一天", SCRIPT)

    def run_at(self, now):
        return tick(self.ethan, self.channel, now=now)

    def test_moment_sent_once_with_its_reason(self):
        at = utc(2026, 10, 23, 8, 25)  # 深圳 16:25，伦敦 09:25
        self.assertEqual(self.run_at(at), "m-16:20：已发送")
        self.assertIn("分享早上被大爷超车的事", self.backend.seen[-1][0])
        self.assertEqual(self.channel.sent, [(42, ["在呢"], [])])
        self.assertEqual(self.run_at(utc(2026, 10, 23, 8, 35)), "没有到点的时刻")
        self.assertEqual(len(self.channel.sent), 1)

    def test_early_morning_moment_is_her_bedtime_and_quiet_hours_win(self):
        # 深圳 10-23 05:40 = 伦敦 10-22 22:40，正好是她睡前
        self.assertEqual(self.run_at(utc(2026, 10, 22, 21, 45)), "m-05:40：已发送")
        self.assertIn("拉伸", self.backend.seen[-1][0])
        self.settings.set("quiet_hours", "08:00-10:00")
        self.assertEqual(self.run_at(utc(2026, 10, 23, 8, 25)), "m-16:20：免打扰")
        self.assertEqual(len(self.channel.sent), 1)

    def test_no_bombing_when_she_does_not_reply(self):
        self.store.raw.append("telegram", "ethan", "a", at=utc(2026, 10, 23, 6), kind="proactive")
        self.run_at(utc(2026, 10, 23, 8, 25))
        self.assertIn("小抱怨", self.backend.seen[-1][0])
        self.store.raw.append("telegram", "ethan", "b", at=utc(2026, 10, 23, 8, 26), kind="proactive")
        self.wiki.write("ethan-life/day-2026-10-23", "一天", SCRIPT.replace("16:20", "17:20"))
        self.assertEqual(self.run_at(utc(2026, 10, 23, 9, 25)), "m-17:20：还没回")

    def test_follow_up_on_her_thing(self):
        life.add_her_thing(self.wiki, "2026-10-23", "08:00", "面试")
        self.assertEqual(self.run_at(utc(2026, 10, 23, 8, 5)), "跟进 面试：已发送")
        self.assertIn("面试", self.backend.seen[-1][0])
        self.assertTrue(life.her_things(self.wiki)[0]["asked"])

    def test_script_is_written_when_missing(self):
        self.wiki.delete("ethan-life/day-2026-10-23")
        self.backend.plan_text = SCRIPT
        result = self.run_at(utc(2026, 10, 23, 8, 25))
        self.assertTrue(result.startswith("写了 ethan-life/day-2026-10-23"), result)
        self.assertTrue(result.endswith("m-16:20：已发送"), result)


if __name__ == "__main__":
    unittest.main()
