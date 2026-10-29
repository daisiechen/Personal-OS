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


if __name__ == "__main__":
    unittest.main()
