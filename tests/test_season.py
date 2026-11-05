"""赛季页（她 2026-11-02 选了样稿 B）：七块领域这个赛季怎么样、是什么造成的、彼此怎么此消彼长。"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import attributes, lifelog, quests, season, worktime
from ethan.memory import MemoryStore

START = date(2026, 10, 12)  # 周一
TODAY = date(2026, 11, 2)   # 周一，第 4 周第一天


class Router:
    def __init__(self, text):
        self.text = text

    def complete(self, system, messages, purpose=""):
        assert "七块领域" in system
        return type("R", (), {"text": self.text})()


class SeasonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        db = self.db = self.store.db
        quests.seed(db)
        worktime.ensure(db)
        lifelog.ensure(db)
        train = quests.habit_by_title(db, "训练")
        d = START
        while d < TODAY:  # 前两周工作少、练得多、见朋友；第三周工作 60 小时、只练 1 次、没见朋友
            wk = (d - START).days // 7
            hrs = 12 if wk == 2 and d.weekday() < 5 else (6 if d.weekday() < 5 else 0)
            per = 60 // worktime.SLOT_MIN
            for n in range(hrs * per):
                m = 8 * 60 + n * worktime.SLOT_MIN
                db.conn.execute("INSERT OR IGNORE INTO work_slots (slot, source) VALUES (?, 'mac')",
                                (f"{d.isoformat()}T{m // 60:02d}:{m % 60:02d}",))
            if train and d.weekday() < 5 and (wk < 2 or d.weekday() == 0):
                db.conn.execute("INSERT INTO habit_log (habit_id, day, at) VALUES (?, ?, 'x')", (train["id"], d.isoformat()))
            if wk < 2 and d.weekday() == 4:
                lifelog.add(db, "meet", d, who="Mia")
            d += timedelta(days=1)
        db.conn.execute(attributes.SNAP_SQL)
        db.conn.execute("INSERT INTO attribute_daily (day, key, score, items) VALUES (?, 'body', 80, '[]')", (START.isoformat(),))
        db.conn.commit()
        self.s = {"name": "测试赛季", "start": START.isoformat(), "end": "2026-12-31"}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ledger_and_relations_come_from_data(self):
        led = season.ledger(self.db, START, TODAY)
        self.assertEqual([w["work"] for w in led["weeks"]], [30.0, 60.0, 0.0])
        self.assertEqual([w["train"] for w in led["weeks"]], [5, 1, 0])
        work = next(r for r in led["rows"] if r["name"] == "工作")
        self.assertEqual([v["bad"] for v in work["values"]], [False, True, False])  # 本周还没过完，不标
        rels = season.relations(self.db, START, TODAY)
        self.assertIn("工作挤掉训练", [r["label"] for r in rels])
        self.assertIn("加班推掉见朋友", [r["label"] for r in rels])

    def test_page_has_no_quest_progress_and_shows_focus_domains(self):
        p = season.page(self.db, self.s, START, TODAY)
        self.assertNotIn("bosses", p)
        self.assertEqual(len(p["domains"]), 7)
        body = next(d for d in p["domains"] if d["key"] == "body")
        self.assertEqual(body["start"], 80)
        self.assertIn("工作挤掉训练", body["crowded_by"])
        focus = [d for d in p["domains"] if d["focus"]]
        self.assertLessEqual(len(focus), 2)
        self.assertTrue(all(len(d["calendar"]) == 21 for d in focus))
        self.assertIsNone(p["next"])  # 没到三分之二，不出下季建议
        self.assertTrue(p["line"])  # 小伴 没写时有代码拼的一句

    def test_weekly_note_written_by_ethan(self):
        r = Router(json.dumps({"line": "这三周事业冲得猛，训练少了 4 次。", "why": {"body": "周三周四开会挤掉训练", "nope": "x"},
                               "next": {"keep": ["减脂"]}}, ensure_ascii=False))
        body = season.write_note(self.db, r, self.s, START, TODAY)
        self.assertIsNone(body["next"])  # 还没到三分之二，下季建议不收
        p = season.page(self.db, self.s, START, TODAY)
        self.assertEqual(p["line"], "这三周事业冲得猛，训练少了 4 次。")
        self.assertEqual(next(d for d in p["domains"] if d["key"] == "body")["why"], "周三周四开会挤掉训练")
        late = season.info(self.s, START, date(2026, 12, 20))
        self.assertTrue(late["late"])


if __name__ == "__main__":
    unittest.main()


class DomainStatsTest(SeasonTest):
    """她 2026-11-02：七个领域页都应该是这个领域的分析——分数（开季→现在、组成、走势）和关键指标的变化图。"""

    def test_score_block_and_charts(self):
        from ethan import domain_stats
        sc = domain_stats.score_block(self.db, "body", START, TODAY)
        self.assertEqual(sc["start"], 80)
        self.assertTrue(any(i["name"] == "训练" for i in sc["items"]))
        ch = domain_stats.charts(self.db, "body", TODAY)
        train = next(c for c in ch if c["title"] == "每周训练")
        self.assertEqual(len(train["points"]), domain_stats.WEEKS)
        self.assertEqual([p["v"] for p in train["points"]][-4:], [5, 5, 1, 0])
        work = domain_stats.charts(self.db, "career", TODAY)[0]
        self.assertEqual(len(work["points"]), 14)
        for key in ("study", "social", "academic", "life"):
            self.assertTrue(domain_stats.charts(self.db, key, TODAY))

    def test_daily_spend_range(self):
        from ethan import domain_stats, finance
        finance.ensure(self.db)
        sp = domain_stats.daily_spend(self.db, TODAY - timedelta(days=6), TODAY)
        self.assertEqual(len(sp["points"]), 7)
        self.assertEqual(sp["total"], 0)
        swapped = domain_stats.daily_spend(self.db, TODAY, TODAY - timedelta(days=2))
        self.assertEqual(len(swapped["points"]), 3)


class SeasonLineTest(SeasonTest):
    def test_fallback_says_state_and_impact_and_kicks_rewrite(self):
        """她 2026-11-02：最上面那句不能只是数字，要说现在的状态和影响；小伴 没写或写旧了就后台重写。"""
        p = season.page(self.db, self.s, START, TODAY)
        self.assertIn("挤占", p["line"])
        self.assertTrue(p["stale"])
        started = []
        self.assertTrue(season.kick(self.tmp.name, spawn=lambda *a, **k: started.append(a)))
        self.assertFalse(season.kick(self.tmp.name, spawn=lambda *a, **k: started.append(a)))  # 15 分钟内不重复
        self.assertEqual(len(started), 1)
