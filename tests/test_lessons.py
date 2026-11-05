import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import lessons
from ethan.memory import MemoryStore


class R:
    def __init__(self):
        self.seen = []

    def complete(self, system, msgs, purpose=""):
        self.seen.append(system)
        self.msgs = msgs
        n = len(self.seen)
        return type("X", (), {"text": json.dumps({"title": f"第{n}课", "gist": "美联储这周降息 25 个基点……", "usage": "利率往下，成长股和黄金通常受益",
                                                 "hook": "", "question": "这周美联储做了什么？"}, ensure_ascii=False)})()


class LessonTest(unittest.TestCase):
    """2026-11-01 她同意：小伴 按课程表主动教；讲了记下来，三天后复习，周报列出来。
    2026-11-04 改成“用得上”：投资类围绕当下市场、谈资类讲当下的，不再隔天固定讲金融历史。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_curriculum_teach_review_and_week(self):
        r, d = R(), date(2026, 11, 2)
        first = lessons.pick(self.db, r, d)
        self.assertEqual(first["topic"], "金融经济和市场")
        self.assertEqual(lessons.pick(self.db, r, d)["id"], first["id"])  # 一天一个
        second = lessons.pick(self.db, r, d + timedelta(days=1))
        self.assertEqual(second["topic"], "奢侈品")
        ctx = lessons.chat_context(self.db, d)
        self.assertIn(f"#{first['id']}", ctx)
        self.assertIn("这对你投资意味着什么：利率往下", ctx)
        self.assertEqual(lessons.note_chat(self.db, f"……就是这样 [教了 #{first['id']}]"), 1)
        self.assertNotIn("今天的知识点", lessons.chat_context(self.db, d))
        self.db.conn.execute("UPDATE lessons SET taught_at = ? WHERE id = ?",
                             ((datetime.now(timezone.utc) - timedelta(days=4)).isoformat(), first["id"]))
        self.assertIn("这周美联储做了什么", lessons.chat_context(self.db, d))
        lessons.note_chat(self.db, f"[复习 #{first['id']} 记得]")
        self.assertNotIn("这周美联储做了什么", lessons.chat_context(self.db, d))  # 刚复习过，过几天再说
        self.assertEqual(lessons.strip("嗯 [教了 #1][复习 #1 忘了]"), "嗯 ")
        w = lessons.week(self.db, date.today() - timedelta(days=7), date.today())
        self.assertEqual(w["教了"], ["（金融经济和市场）第1课"])
        self.assertIn("记得 1", w["复习"])

    def test_no_more_finance_history_every_other_day(self):
        r, d = R(), date(2026, 11, 5)
        topics = [lessons.pick(self.db, r, d + timedelta(days=i))["topic"] for i in range(len(lessons.TOPICS) + 1)]
        self.assertNotIn("金融历史", topics)
        self.assertEqual(len(set(topics[:len(lessons.TOPICS)])), len(lessons.TOPICS))  # 一轮里每个主题各一次
        self.assertEqual(topics[-1], topics[0])  # 讲完一轮回到最久没讲的
        names = {t for t, _, _ in lessons.TOPICS}
        self.assertTrue({"奢侈品", "服装和生活品牌", "享受生活", "各国社交礼仪", "新闻", "科技", "投资理财"} <= names)
        self.assertNotIn("历史", names)
        self.assertNotIn("政治", names)

    def test_prompt_says_what_it_is_for(self):
        r = R()
        lessons.pick(self.db, r, date(2026, 11, 5))  # 金融经济和市场
        lessons.pick(self.db, r, date(2026, 11, 6))  # 奢侈品
        self.assertIn("炒股、投资挣钱", r.seen[0])
        self.assertIn("这对你投资意味着什么", r.seen[0])
        self.assertIn("不讲古早", r.seen[0])
        self.assertIn("最近一个月", r.seen[0])
        self.assertIn("跟朋友聊天时有话聊", r.seen[1])
        self.assertIn("你可以这样跟朋友说", r.seen[1])

    def test_already_picked_today_untouched(self):
        lessons.ensure(self.db)
        self.db.conn.execute("INSERT INTO lessons (day, topic, title, gist) VALUES ('2026-11-04', '金融历史', '南海泡沫', '1720 年……')")
        self.db.conn.commit()
        got = lessons.pick(self.db, R(), date(2026, 11, 4))
        self.assertEqual(got["title"], "南海泡沫")
        self.assertIn("南海泡沫", lessons.chat_context(self.db, date(2026, 11, 4)))  # 旧的没有“用得上”那句也照常讲

    def test_usage_required(self):
        class Bad(R):
            def complete(self, system, msgs, purpose=""):
                return type("X", (), {"text": json.dumps({"title": "t", "gist": "g"})})()
        with self.assertRaises(ValueError):
            lessons.pick(self.db, Bad(), date(2026, 11, 5))

    def test_weekly_news_from_grok(self):
        sent = []
        grok = lambda task, channel, report_to=None: sent.append((task, report_to)) or True
        d = date(2026, 11, 4)
        self.assertTrue(lessons.ask_news(self.db, grok, d))
        self.assertFalse(lessons.ask_news(self.db, grok, d + timedelta(days=1)))  # 一周一次
        self.assertIn("【本周谈资 2026-11-02】", sent[0][0])
        self.assertIn("奢侈品", sent[0][0])
        self.assertFalse(lessons.ask_news(self.db, None, d + timedelta(days=7)))  # 没有 Grok 不派
        inbox = Path(self.tmp.name) / "inbox"
        inbox.mkdir(exist_ok=True)
        (inbox / "2026-11-04.jsonl").write_text(json.dumps({"path": "/grok/result", "at": "2026-11-04T10:00:00+00:00",
                                                            "text": "【本周谈资 2026-11-02】\n市场｜美联储降息｜11-03｜……"},
                                                           ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(lessons.collect_news(self.store), 1)
        self.assertEqual(lessons.collect_news(self.store), 0)
        r = R()
        lessons.pick(self.db, r, d)
        self.assertIn("美联储降息", r.msgs[0]["content"])  # 挑题时 Grok 查的本周新闻当资料

if __name__ == "__main__":
    unittest.main()
