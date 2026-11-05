"""“聊聊”：下午傍晚 小伴 主动讲今天的知识点、顺带一个补缺口的问题（她 11-04：“小伴今天还是没来跟我聊认知提升的事情啊，
新知识点也没有”）。以前两样都只在她来聊天时“找个自然时机”，服务器上 11-01 到 11-04 一次都没讲、补缺口一题都没问。"""

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ethan import lessons, mepage, nudge_registry, nudges
from ethan.core.agent import Ethan
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.proactive import tick
from tests.test_proactive import Backend, FakeChannel

LDN = ZoneInfo("Europe/London")
DAY = date(2026, 11, 4)


def at(h, m=0):
    return datetime(DAY.year, DAY.month, DAY.day, h, m, tzinfo=LDN)


class LessonChatTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        lessons.ensure(self.db)
        cur = self.db.conn.execute("INSERT INTO lessons (day, topic, title, gist, hook, question) VALUES (?, ?, ?, ?, ?, ?)",
                                   (DAY.isoformat(), "金融历史", "南海泡沫", "1720 年英国南海公司股价暴涨暴跌……", "", "哪年？"))
        self.lid = cur.lastrowid
        mepage.ensure(self.db)
        self.db.conn.executescript(mepage.GAP_SCHEMA)
        self.gids = []
        for q in ("有什么事是你特别看重、怎么都不愿意妥协的？", "你做大决定的时候，一般是怎么想的？"):
            pid = mepage.add_question(self.db, q)
            self.db.conn.execute("UPDATE pending SET origin = 'gap', dim = 'values' WHERE id = ?", (pid,))
            self.db.conn.execute("INSERT INTO gap_log (day, key, question, outcome, pending_id, at) VALUES (?, 'dim:values', ?, 'asked', ?, ?)",
                                 (DAY.isoformat(), q, pid, "2026-11-04T01:00:00+00:00"))
            self.gids.append(pid)
        self.db.conn.commit()
        self.backend = Backend("今天读到南海泡沫，想到你\n对了，你最看重什么？")
        self.settings = Settings(root / "settings.json")
        self.ethan = Ethan(self.store, Router(self.backend, Ledger(root / "ledger"), self.settings), persona="人设")
        self.channel = FakeChannel()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def set_asked(self, pid, when):
        self.db.conn.execute("UPDATE pending SET asked_at = ? WHERE id = ?", (when, pid))
        self.db.conn.commit()

    def lesson(self):
        return lessons.today(self.db, DAY)

    def asked(self, pid):
        return self.db.conn.execute("SELECT asked_at FROM pending WHERE id = ?", (pid,)).fetchone()[0]

    def test_registered(self):
        self.assertTrue(nudge_registry.registered(f"lesson-chat:{DAY}:L1:G2"))

    def test_in_window_brings_lesson_and_top_gap(self):
        key, why = nudges._lesson_chat(self.db, at(16), {})
        self.assertEqual(key, f"lesson-chat:{DAY}:L{self.lid}:G{self.gids[0]}")  # 优先级最高的那题
        self.assertIn("南海泡沫", why)
        self.assertIn("特别看重", why)

    def test_outside_window(self):
        self.assertIsNone(nudges._lesson_chat(self.db, at(14, 50), {}))
        self.assertIsNone(nudges._lesson_chat(self.db, at(20, 30), {}))

    def test_taught_already_only_question(self):
        lessons.note_chat(self.db, f"[教了 #{self.lid}]")
        key, why = nudges._lesson_chat(self.db, at(16), {})
        self.assertTrue(key.endswith(f":L0:G{self.gids[0]}"))
        self.assertNotIn("南海泡沫", why)

    def test_both_done_sends_nothing(self):
        lessons.note_chat(self.db, f"[教了 #{self.lid}]")
        self.set_asked(self.gids[1], "2026-11-04T12:00:00+00:00")  # 今天已经问过一个
        self.assertIsNone(nudges._lesson_chat(self.db, at(16), {}))

    def test_paused_only_lesson(self):
        mepage.gap_note_her(self.db, "今天别问了", DAY)
        key, why = nudges._lesson_chat(self.db, at(16), {})
        self.assertTrue(key.endswith(":G0"))
        self.assertNotIn("特别看重", why)
        lessons.note_chat(self.db, f"[教了 #{self.lid}]")
        self.assertIsNone(nudges._lesson_chat(self.db, at(16), {}))

    def test_asked_in_three_days_skipped(self):
        mepage.mark_asked(self.db, self.gids[0])
        self.db.conn.execute("UPDATE pending SET asked_at = ? WHERE id = ?",
                             ((datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds"), self.gids[0]))
        self.db.conn.commit()
        self.assertEqual(mepage.top_gap(self.db, DAY)["id"], self.gids[1])

    def test_busy_in_plan_skips(self):
        import json
        pid = self.db.conn.execute("INSERT INTO daily_plans (day, version, origin, facts, brief, fingerprint, created_at) VALUES (?, 1, 'fallback', '{}', ?, 'x', ?)",
                                   (DAY.isoformat(), json.dumps({"busy": ["15:30–17:00 组会"]}, ensure_ascii=False),
                                    "2026-11-04T07:00:00")).lastrowid
        self.assertTrue(pid)
        self.assertIsNone(nudges._lesson_chat(self.db, at(16), {}))
        self.assertIsNotNone(nudges._lesson_chat(self.db, at(17, 10), {}))

    def run_at(self, h, m=0):
        return tick(self.ethan, self.channel, now=at(h, m)).split("；")[-1]

    def test_tick_sends_once_and_records(self):
        got = self.run_at(16)
        self.assertTrue(got.startswith("lesson-chat:") and got.endswith("已发送"), got)
        self.assertEqual(len(self.channel.sent), 1)
        self.assertIn("南海泡沫", self.backend.seen[-1][0])
        self.assertIsNotNone(self.lesson()["taught_at"])  # 等同 [教了 #id]
        self.assertIsNotNone(self.asked(self.gids[0]))  # 等同 [待定 #id 问了]
        self.assertIsNone(self.asked(self.gids[1]))
        self.store.raw.append("telegram", "mia", "哈哈", at=at(16, 5).astimezone(timezone.utc))
        self.store.raw.append("telegram", "ethan", "嗯", at=at(16, 6).astimezone(timezone.utc))
        self.run_at(19, 30)
        self.assertEqual(len(self.channel.sent), 1)  # 一天只发一次

    def test_tick_skips_when_chatting_recently(self):
        self.store.raw.append("telegram", "mia", "在吗", at=at(14, 30).astimezone(timezone.utc))
        self.store.raw.append("telegram", "ethan", "在呀", at=at(14, 31).astimezone(timezone.utc))
        self.assertFalse(self.run_at(16).startswith("lesson-chat:"))
        self.assertEqual(self.channel.sent, [])
        self.assertIsNone(self.lesson()["taught_at"])
        self.assertTrue(self.run_at(16, 40).endswith("已发送"))

    def test_chat_prompt_more_urgent(self):
        ctx = lessons.chat_context(self.db, DAY)
        self.assertIn("这一轮或下一轮就讲", ctx)
        self.assertNotIn("找个自然的时机", ctx)
        self.assertFalse(mepage.gap_asked_today(self.db, DAY))
        self.set_asked(self.gids[0], "2026-11-04T12:00:00+00:00")
        self.assertTrue(mepage.gap_asked_today(self.db, DAY))
        self.assertFalse(mepage.gap_asked_today(self.db, DAY + timedelta(days=1)))


if __name__ == "__main__":
    unittest.main()
