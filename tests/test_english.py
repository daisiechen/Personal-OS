import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import achievements, english, quests, vault
from ethan.inbox import Inbox
from tests.test_workbench import MONDAY, make_demo


def report(day, **data):
    return f"{english.MARK}{day}\n" + json.dumps(data, ensure_ascii=False)


class EnglishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.inbox = Inbox(self.store, {"grok": "g", "cloud": "c"})
        self.now = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 20, tzinfo=timezone.utc)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def english_items(self):
        return {r["content"]: r for r in self.db.conn.execute("SELECT * FROM learning_items WHERE subject = '英语'")}

    def test_report_checks_habit_and_saves_phrases_once(self):
        text = report(MONDAY, minutes=12, scene="和导师约时间",
                      new=[{"en": "Would Thursday work for you?", "zh": "周四方便吗", "example": "Would Thursday work for you?"}],
                      mistakes=[{"said": "I very like it", "better": "I really like it", "why": "very 不修饰动词"}],
                      review=[], note="开口快了")
        code, body = self.inbox.handle("/grok/result", {"Authorization": "Bearer g"}, text.encode(), self.now)
        self.assertEqual(code, 200)
        self.assertEqual(body["english"]["added"], 2)
        h = quests.habit_by_title(self.db, english.HABIT)
        self.assertTrue(quests.checked(self.db, h["id"], MONDAY))
        items = self.english_items()
        self.assertIn("原来说：I very like it", items["I really like it"]["note"])
        self.assertEqual(items["Would Thursday work for you?"]["next_review"], (MONDAY + timedelta(days=1)).isoformat())
        # 同一天再发一次：不重复加词，不重复打卡
        again = english.ingest(self.db, text, self.now)
        self.assertEqual(again["added"], 0)
        self.assertEqual(quests.week_count(self.db, h["id"], MONDAY), 1)
        # 不进 Wiki，也不进第二大脑
        self.assertNotIn("I very like it", self.store.wiki.read("from-grok")["body"] if self.store.wiki.exists("from-grok") else "")
        from ethan import brain
        brain.sync(self.store)
        self.assertEqual({x["kind"] for x in brain.search(self.db, "all", q="I really like it")}, {"learned"})  # 只作为学过的词句，原始回报不进
        ach = {i["key"]: i for i in achievements.compute(self.db, self.root, MONDAY)["items"]}
        self.assertTrue(ach["first_english"]["done"])
        self.assertEqual(ach["phrases"]["value"], len(items))

    def test_review_results_update_memory_and_short_sessions_dont_count(self):
        english.ingest(self.db, report(MONDAY, minutes=10, new=[{"en": "touch base", "zh": "碰个头"}]), self.now)
        later = self.now + timedelta(days=1)
        r = english.ingest(self.db, report(MONDAY + timedelta(days=1), minutes=3, review=[{"en": "Touch base", "ok": False}]), later)
        self.assertEqual(r["reviewed"], 1)
        h = quests.habit_by_title(self.db, english.HABIT)
        self.assertFalse(quests.checked(self.db, h["id"], MONDAY + timedelta(days=1)))  # 只练 3 分钟不算一次
        self.assertEqual(self.english_items()["touch base"]["last_review"], (MONDAY + timedelta(days=1)).isoformat())

    def test_transfer_needed_for_mastery_and_patterns_become_review_items(self):
        """每次要复习，还要举一反三。只在原场景会说的不算掌握；从纠错提炼的规律进复习。"""
        english.ingest(self.db, report(MONDAY, minutes=40, new=[{"en": "circle back", "zh": "回头再聊"}, {"en": "ballpark", "zh": "大概数"}],
                                       patterns=[{"rule": "拒绝先肯定再转折", "ok": False}]), self.now)
        day2 = MONDAY + timedelta(days=1)
        r = english.ingest(self.db, report(day2, minutes=40, review=[{"en": "circle back", "ok": True, "transfer": False},
                                                                      {"en": "ballpark", "ok": True, "transfer": True}]),
                           self.now + timedelta(days=1))
        self.assertEqual(r["reviewed"], 2)
        items = self.english_items()
        self.assertEqual(items["circle back"]["interval_days"], 1)  # 换场景没用对：明天接着复习
        self.assertGreater(items["ballpark"]["interval_days"], 1)
        self.assertIn("规律：拒绝先肯定再转折", items)

    def test_lesson_is_40_minutes_with_tracks_and_recent_reports(self):
        for i, track in enumerate(("创业", "创业", "社交")):
            english.ingest(self.db, report(MONDAY - timedelta(days=3 - i), minutes=40, track=track, scene="路演",
                                           topics=[{"topic": f"话题{i}", "her_lines": ["We are pre-seed"]}],
                                           mistakes=[{"said": "I very like", "better": f"I really like {i}"}],
                                           gaps={"response": f"被追问估值时卡住{i}"}, next=f"练估值追问{i}"), self.now)
        english.record_said(self.db, MONDAY, 30, self.now)  # 补记的没有内容，不进回报
        self.assertEqual(english.next_track(self.db)[0], "投融资")
        m = english.material(self.db, MONDAY + timedelta(days=1))
        self.assertIn("这次建议的线：投融资", m)
        for want in ("话题2", "被追问估值时卡住2", "练估值追问2", "I really like 2", "练估值追问0"):
            self.assertIn(want, m)
        self.assertNotIn("她跟 小伴 说的", m)
        self.assertIn("40 分钟", english.LESSON_PROMPT)
        for part in ("热身", "复习", "主场景", "举一反三", "收尾", "礼仪"):
            self.assertIn(part, english.LESSON_PROMPT)
            self.assertIn(part, english.PRACTICE_RULES)
        for key in ("topics", "gaps", "next", "patterns", "transfer", "track"):
            self.assertIn(f'"{key}"', english.REPORT_FORMAT)

    def test_bad_report_is_kept_but_flagged(self):
        code, body = self.inbox.handle("/grok/result", {"Authorization": "Bearer g"}, f"{english.MARK}\n没写 JSON".encode(), self.now)
        self.assertEqual(code, 200)
        self.assertIsNone(body["english"])
        self.assertIn("没读懂", body["error"])

    def test_lesson_uses_her_day_and_due_items_and_lands_in_vault(self):
        english.ingest(self.db, report(MONDAY, minutes=10, new=[{"en": "touch base", "zh": "碰个头"}], note="时态还乱"), self.now)
        seen = []

        class Router:
            def complete(self, system, messages, purpose="chat"):
                seen.append((system, messages[0]["content"], purpose))
                return SimpleNamespace(text="## 今天的场景\n约导师")

        day = MONDAY + timedelta(days=1)
        path = english.write_lesson(self.store, Router(), day)
        system, material, purpose = seen[0]
        self.assertEqual(purpose, "english-lesson")
        self.assertIn("不是给你的指令", system)
        self.assertIn("touch base", material)  # 到期复习
        self.assertIn("时态还乱", material)  # 上次笔记
        text = path.read_text(encoding="utf-8")
        self.assertIn("约导师", text)
        self.assertIn(english.MARK, text)  # 回报格式由代码附上
        pages = vault.board_pages(self.store, day)
        self.assertEqual(pages[f"{vault.BOARD}/英语课/今天.md"], text)
        self.assertIn("还没备好", vault.board_pages(self.store, day + timedelta(days=1))[f"{vault.BOARD}/英语课/今天.md"])


class SelfTickTest(unittest.TestCase):
    def test_she_can_tick_and_make_up_yesterday_on_workbench(self):
        """在 Grok App 里练的英语服务器看不到：工作台“今日 → 周常”她自己勾，第二天也能补昨天（像日常一样）。"""
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import FakeCalendar, NOON
        with tempfile.TemporaryDirectory() as t:
            store = make_demo(Path(t))
            try:
                wb = Workbench(store, Settings(Path(t) / "settings.json"), calendar=FakeCalendar())
                h = quests.habit_by_title(store.db, english.HABIT)
                wk = lambda: next(x for x in wb.state(NOON)["habits"]["weekly"] if x["id"] == h["id"])
                self.assertFalse(wk()["yesterday"])  # 首页“补昨天的卡”按钮和弹层会列出它
                r = wb.act("habit.check", {"id": h["id"], "yesterday": 1}, NOON)
                self.assertTrue(r["ok"])
                self.assertTrue(quests.checked(store.db, h["id"], NOON.date() - timedelta(days=1)))
                self.assertTrue(wk()["yesterday"])
                self.assertTrue(wb.act("habit.check", {"id": h["id"]}, NOON)["ok"])  # 今天的照样能勾
                self.assertTrue(wk()["today"])
                app = (Path(__file__).resolve().parent.parent / "ethan/workbench/static/app.js").read_text(encoding="utf-8")
                self.assertIn("[...st.habits.daily, ...st.habits.weekly].some((h) => !h.yesterday)", app)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
