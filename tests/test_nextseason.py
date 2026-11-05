"""下个赛季：拆副本草稿、开季、结算（她 2026-11-02：“1-5 先做吧”第 1 件；做法见 docs/design/tasks-and-schedule.md 第 7 节）。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import nextseason, quests, season, stages, weekreview
from ethan.core.backends import Reply
from ethan.core.router import Settings
from ethan.memory import MemoryStore

DAY = date(2026, 12, 27)
DRAFT = {"name": "2027 春季 · 城市篇", "start": "2027-01-01", "end": "2027-03-31", "summary": "少开副本，先守住训练和睡觉。",
         "rule": "每周训练不少于 4 次",
         "goals": [{"title": "减脂塑形", "domain": "身体", "due": "2027-03-31", "scope": "训练、饮食、体重", "from_goal": 20,
                    "milestones": [{"title": "M1 稳住 55", "due": "2027-01-31",
                                    "tasks": [{"title": "训练 4 次 / 周", "cadence": "weekly", "per_week": 4, "habit": "训练"},
                                              {"title": "测体脂", "cadence": "per_milestone"}]}]},
                   {"title": "英语口语", "domain": "学习", "due": "2027-03-31", "scope": "陪练、口语", "from_goal": None,
                    "milestones": [{"title": "M1 每周 3 次陪练", "due": "2027-02-15", "tasks": [{"title": "陪练", "cadence": "bogus"}]}]},
                   {"title": "", "milestones": []}],
         "pause": [22]}


class Router:
    def __init__(self, settings, body=DRAFT):
        self.settings, self.body, self.seen = settings, body, []

    def complete(self, system, messages, purpose="chat"):
        self.seen.append((system, messages))
        return Reply("好：" + json.dumps(self.body, ensure_ascii=False), "x", "m", 0)


class NextSeasonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        quests.seed(self.db)
        self.settings = Settings(root / "settings.json")
        for gid, title, dom in ((20, "减脂塑形", "身体"), (22, "年底前去 3 个新国家", "生活体验")):
            self.db.conn.execute("INSERT INTO goals (id, level, title, domain, status, due, updated_at) VALUES (?, 'year', ?, ?, 'active', '2026-12-31', 'x')",
                                 (gid, title, dom))
        stages.ensure(self.db)
        self.m_old = stages.add_milestone(self.db, 20, "M4 收官", "2026-12-31")
        if not quests.habit_by_title(self.db, "训练"):
            quests.add_habit(self.db, "训练", "weekly")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_draft_cleaned_and_rewritten_with_feedback(self):
        r = Router(self.settings)
        body = nextseason.write(self.store, r, DAY)
        self.assertEqual([g["title"] for g in body["goals"]], ["减脂塑形", "英语口语"])  # 没名字的去掉
        eng = body["goals"][1]["milestones"][0]["tasks"]
        self.assertEqual(eng[0]["cadence"], "once")  # 不认识的频次改成一次
        self.assertTrue(eng[-1]["review"])  # 没写复盘的补一件
        self.assertIn("七块领域", r.seen[0][0])
        nextseason.write(self.store, r, DAY, "英语那个不要了")
        self.assertIn("她的意见：英语那个不要了", r.seen[1][1][0]["content"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM season_drafts").fetchone()[0], 1)  # 改的是同一份

    def test_open_next_settles_builds_and_switches(self):
        nextseason.write(self.store, Router(self.settings), DAY)
        got = nextseason.open_next(self.db, self.settings, DAY)
        self.assertEqual(got["season"], "2027 春季 · 城市篇")
        self.assertEqual(got["paused"], [22])  # 没接着打的暂停，不删
        self.assertEqual(self.db.conn.execute("SELECT status FROM goals WHERE id = 22").fetchone()[0], "paused")
        self.assertEqual(self.db.conn.execute("SELECT status FROM milestones WHERE id = ?", (self.m_old,)).fetchone()[0], "missed")
        cur = stages.current(self.db, 20)
        self.assertEqual(cur["title"], "M1 稳住 55")
        ts = {t["title"]: t for t in stages.tasks_of(self.db, cur["id"])}
        self.assertEqual((ts["训练 4 次 / 周"]["per_week"], bool(ts["训练 4 次 / 周"]["habit_id"])), (4, True))
        new = [g for g in got["made"] if g != 20][0]
        self.assertEqual(self.db.conn.execute("SELECT title, scope FROM goals WHERE id = ?", (new,)).fetchone()[:], ("英语口语", "陪练、口语"))
        s, start = season.current(self.settings)
        self.assertEqual((s["name"], start), ("2027 春季 · 城市篇", date(2027, 1, 1)))
        past = nextseason.archive(self.db)
        self.assertEqual(past[0]["name"], season.SEASON["name"])
        self.assertIn("减脂塑形", [b["title"] for b in past[0]["bosses"]])
        self.assertIsNone(nextseason.draft(self.db))
        with self.assertRaises(ValueError):
            nextseason.open_next(self.db, self.settings, DAY)

    def test_season_review_kicks_draft_and_chat_feedback(self):
        from ethan.core.agent import Ethan, parse_reply
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router as RealRouter
        kicked = []

        class B:
            name = "subscription"

            def complete(self, system, messages, images=()):
                return Reply("嗯", self.name, "m", 0)
        e = Ethan(self.store, RealRouter(B(), Ledger(Path(self.tmp.name) / "ledger"), self.settings), persona="人设")
        e.season_kick = lambda root, fb=None: kicked.append(fb)
        weekreview.open_review(self.db, DAY, ["week", "month", "season"])
        e.note_records("收尾～[复盘记录 这季训练稳了 | 训练 | 下季少开副本]")
        self.assertEqual(kicked, [None])
        text = "好，我改～[下季草稿 改 英语副本不要了]"
        self.assertEqual(parse_reply(text)["bubbles"], ["好，我改～"])
        e.note_records(text)
        self.assertEqual(kicked, [None, "英语副本不要了"])


if __name__ == "__main__":
    unittest.main()
