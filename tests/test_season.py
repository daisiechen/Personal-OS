"""赛季页（样稿 B）：七块领域这个赛季怎么样、是什么造成的、彼此怎么此消彼长。"""

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


class SeasonBase(unittest.TestCase):
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


class SeasonTest(SeasonBase):
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
        self.assertTrue(all("calendar" not in d for d in p["domains"]))  # 11-05 第二版：大卡和 21 天小日历撤掉了
        self.assertIsNone(p["next"])  # 没到三分之二，小伴 的正式建议不出
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


class SeasonPageV2Test(SeasonBase):
    """的第二版：A 的通行证 + 小伴 总评和下赛季建议（一直显示）+ C 的七块领地分数柱（比开季、比上季）。"""

    def test_suggestion_always_shown_rule_based_before_ethan(self):
        p = season.page(self.db, self.s, START, TODAY)
        sg = p["suggest"]
        self.assertEqual(sg["by"], "rule")
        self.assertEqual(sg["rule"], f"工作每周不超过 {season.WORK_CAP} 小时")  # 第 2 周干了 60 小时
        self.assertTrue(any("事业少加码" in x and "健康" in x for x in sg["pause"]))  # 工作挤掉训练
        self.assertTrue(all(x.startswith("把") for x in sg["start"]))

    def test_ethan_suggestion_wins_when_late(self):
        late = date(2026, 12, 20)
        r = Router(json.dumps({"line": "收官前把训练补回来。", "next": {"keep": ["减脂"], "pause": [], "start": ["每周见一次朋友"], "rule": "工作不超过 45 小时"}},
                              ensure_ascii=False))
        season.write_note(self.db, r, self.s, START, late)
        sg = season.page(self.db, self.s, START, late)["suggest"]
        self.assertEqual(sg["by"], "小伴")
        self.assertEqual(sg["keep"], ["减脂"])

    def test_compare_with_last_season(self):
        past = [{"name": "上一季", "domains": [{"name": "健康", "now": 90}, {"name": "事业", "now": None}]}]
        doms = {d["key"]: d for d in season.page(self.db, self.s, START, TODAY, past)["domains"]}
        if doms["body"]["now"] is not None:
            self.assertEqual(doms["body"]["prev"], 90)
            self.assertEqual(doms["body"]["prev_delta"], doms["body"]["now"] - 90)
        self.assertIsNone(doms["career"]["prev_delta"])
        self.assertIsNone(season.page(self.db, self.s, START, TODAY)["domains"][0]["prev"])  # 第一季没有上季


class SeasonApiTest(unittest.TestCase):
    def test_season_page_tells_what_to_adjust(self):
        """“赛季这一页，你只需要告诉我，我现在状态的整体评价是什么，结论是什么，下个赛季的建议是什么，
        区块领地各自的状态是什么？就是我看到这一页，我知道我接下来怎么做调整就可以。”主副本路线不再给。"""
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import FakeCalendar, NOON, make_demo
        with tempfile.TemporaryDirectory() as t:
            store = make_demo(Path(t))
            try:
                wb = Workbench(store, Settings(Path(t) / "settings.json"), calendar=FakeCalendar())
                p = wb.season_page(NOON)
                self.assertNotIn("route", p)
                for k in ("line", "conclusion", "suggest", "domains", "waste"):
                    self.assertIn(k, p)
                self.assertEqual(len(p["domains"]), 7)
                for d in p["domains"]:
                    self.assertIn(d["status"], season.STATUS)
                    self.assertTrue(d["adjust"], d["name"])
                bad = [d for d in p["domains"] if d["status"] in ("floor", "down")]
                if bad:
                    self.assertIn("接下来先管", p["conclusion"])
            finally:
                store.close()

    def test_waste_badge_equals_waste_page(self):
        """        导航红点、赛季页荒原柱、地图、荒原页都从 taskrules 的 waste 视图取，件数一样；归好一件，几处一起少一件。"""
        from ethan import invariants
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import FakeCalendar, MONDAY, NOON, make_demo
        with tempfile.TemporaryDirectory() as t:
            store = make_demo(Path(t))
            try:
                wb = Workbench(store, Settings(Path(t) / "settings.json"), calendar=FakeCalendar())
                counts = lambda: (wb.badges(MONDAY).get("map", 0), len(wb.territory_page("waste", NOON)["tasks"]),
                                  wb.season_page(NOON)["waste"], wb.map_page(NOON)["waste"])
                n = counts()
                self.assertGreater(n[0], 0)
                self.assertEqual(len(set(n)), 1, n)
                tid = wb.territory_page("waste", NOON)["tasks"][0]["id"]
                self.assertTrue(wb.act("task.domain", {"id": tid, "key": "career"}, NOON)["ok"])
                self.assertEqual(counts(), (n[0] - 1,) * 4)
                self.assertEqual(invariants.badges_have_targets(wb, MONDAY), [])
            finally:
                store.close()

    def test_mail_todo_not_in_waste(self):
        """邮件记成的待办没有领域，又进了荒原等归类，两处红点算同一件。"""
        from ethan import taskrules
        from tests.test_workbench import MONDAY, make_demo
        with tempfile.TemporaryDirectory() as t:
            store = make_demo(Path(t))
            try:
                before = len(taskrules.rows(store.db, "waste", MONDAY))
                tid = taskrules.add(store.db, "回复 Amy：报价")
                from ethan import lifebase
                lifebase.ensure(store.db)
                store.db.conn.execute("UPDATE tasks SET owner = 'mia', source_type = 'email', source_ref = 'mail:1', status = 'open' WHERE id = ?", (tid,))
                store.db.conn.commit()
                self.assertEqual(len(taskrules.rows(store.db, "waste", MONDAY)), before)
            finally:
                store.close()

    def test_life_log_shows_on_season_page_right_away(self):
        """        记一笔后不用等每晚的快照：赛季页的社交分数柱、领域页的分数和走势最后一点都马上变，而且是同一个数。"""
        from ethan import attributes
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import FakeCalendar, NOON, make_demo
        with tempfile.TemporaryDirectory() as t:
            store = make_demo(Path(t))
            try:
                wb = Workbench(store, Settings(Path(t) / "settings.json"), calendar=FakeCalendar())
                attributes.snapshot(store.db, NOON.date())  # 当天的快照已经存过（旧的），也不能挡住现算
                social = lambda: next(d for d in wb.season_page(NOON)["domains"] if d["key"] == "social")["now"]
                self.assertIsNone(social())
                self.assertTrue(wb.act("life.log", {"kind": "meet", "who": "小林"}, NOON)["ok"])
                met = social()
                self.assertIsNotNone(met)
                self.assertTrue(wb.act("life.log", {"kind": "new_friend", "who": "新同学"}, NOON)["ok"])
                self.assertGreater(social(), met)
                sc = wb.territory_page("social", NOON)["score"]
                self.assertEqual(sc["now"], social())  # 领域页和赛季页同一个数
                self.assertEqual(sc["history"][-1], {"day": NOON.date().isoformat(), "v": social()})  # 走势最后一点是今天现算的
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()


class DomainStatsTest(SeasonBase):
    """七个领域页都应该是这个领域的分析——分数（开季→现在、组成、走势）和关键指标的变化图。"""

    def test_score_block_and_charts(self):
        from ethan import domain_stats
        sc = domain_stats.score_block(self.db, "body", START, TODAY)
        self.assertEqual(sc["start"], 80)
        self.assertTrue(any(i["name"] == "训练" for i in sc["items"]))
        # 健康页训练、0 点前睡不要图（只留健康线和体重）；学习页不要认知拓展图
        self.assertEqual(domain_stats.charts(self.db, "body", TODAY), [])
        self.assertEqual([c["title"] for c in domain_stats.charts(self.db, "study", TODAY)], ["每周和 Grok 练英语"])
        work = domain_stats.charts(self.db, "career", TODAY)[0]
        self.assertEqual(len(work["points"]), 14)
        for key in ("study", "social", "academic", "life"):
            self.assertTrue(domain_stats.charts(self.db, key, TODAY))

    def test_english_minutes_weekly(self):
        """没写时长的打卡按 0 分钟、照样算一次。"""
        from ethan import domain_stats, english, quests
        english.ensure(self.db)
        last_mon = TODAY - timedelta(days=7)
        for d, m in ((last_mon, 12), (last_mon + timedelta(days=1), 20)):
            self.db.conn.execute("INSERT INTO english_sessions (day, minutes, data, at) VALUES (?, ?, '{}', 'x')", (d.isoformat(), m))
        self.db.conn.execute("INSERT INTO habits (title, cadence, points, active) VALUES (?, 'daily', 10, 1)", (english.HABIT,)) \
            if not quests.habit_by_title(self.db, english.HABIT) else None
        h = quests.habit_by_title(self.db, english.HABIT)
        self.db.conn.execute("INSERT INTO habit_log (habit_id, day, at, easy) VALUES (?, ?, 'x', 0)", (h["id"], (last_mon + timedelta(days=3)).isoformat()))
        self.db.conn.commit()
        c = domain_stats.charts(self.db, "study", TODAY)[0]
        self.assertEqual(c["unit"], "分钟")
        self.assertEqual(len(c["points"]), domain_stats.WEEKS)
        self.assertEqual((c["points"][-2]["v"], c["points"][-2]["n"]), (32, 3))  # 12 + 20 分钟；两天有回报 + 一天只打卡
        self.assertFalse(c["empty"])

    def test_english_health_row_matches_chart(self):
        """11-06：练习时长有、没打卡的周，健康线“英语陪练”和学习页的图别对不上——两边都按 english.practice_days（记录 ∪ 打卡）。"""
        from ethan import domain_stats, english, territory
        english.ensure(self.db)
        for k in (0,):
            self.db.conn.execute("INSERT INTO english_sessions (day, minutes, data, at) VALUES (?, 30, '{}', 'x')", (TODAY.isoformat(),))
        self.db.conn.commit()
        row = next(r for r in territory.health(self.db, "study", TODAY, False) if r["name"] == english.HABIT)
        n = domain_stats.charts(self.db, "study", TODAY)[0]["points"][-1]["n"]
        self.assertEqual(row["actual"], f"{n} 次")
        self.assertGreaterEqual(n, 1)

    def test_daily_spend_range(self):
        from ethan import domain_stats, finance
        finance.ensure(self.db)
        sp = domain_stats.daily_spend(self.db, TODAY - timedelta(days=6), TODAY)
        self.assertEqual(len(sp["points"]), 7)
        self.assertEqual(sp["total"], 0)
        swapped = domain_stats.daily_spend(self.db, TODAY, TODAY - timedelta(days=2))
        self.assertEqual(len(swapped["points"]), 3)


class SeasonLineTest(SeasonBase):
    def test_fallback_says_state_and_impact_and_kicks_rewrite(self):
        """最上面那句不能只是数字，要说现在的状态和影响；小伴 没写或写旧了就后台重写。"""
        p = season.page(self.db, self.s, START, TODAY)
        self.assertIn("挤占", p["line"])
        self.assertTrue(p["stale"])
        started = []
        self.assertTrue(season.kick(self.tmp.name, spawn=lambda *a, **k: started.append(a)))
        self.assertFalse(season.kick(self.tmp.name, spawn=lambda *a, **k: started.append(a)))  # 15 分钟内不重复
        self.assertEqual(len(started), 1)


class SeasonNoteOutdatedTest(unittest.TestCase):
    def test_backfill_after_ethan_wrote_hides_old_numbers_and_rewrites(self):
        """小伴 11-05 01:04 写的总评是“这两周见朋友 0 次”，
        18:10 补记了 11-05 见朋友。之后：带数字的旧句子不再摆出来（换成现算的），标“X 月 X 日写的，之后有新记录”，后台重写；
        因为新记录重写每天有上限，15 分钟锁挡下的不算一次。"""
        from datetime import datetime, timezone
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import FakeCalendar, NOON, make_demo
        with tempfile.TemporaryDirectory() as t:
            store = make_demo(Path(t))
            try:
                wb = Workbench(store, Settings(Path(t) / "settings.json"), calendar=FakeCalendar())
                kicks = []
                wb.season_kick = lambda: kicks.append(1) or True
                s, _ = season.current(wb.settings)
                season.ensure(store.db)
                store.db.conn.execute("UPDATE body_measures SET at = '2026-01-01T00:00:00+00:00'")  # 示意数据是刚记的，挪到 小伴 写之前
                written = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
                body = {"line": "这两周见朋友 0 次，认识新朋友 0 个，社交在往下掉。", "why": {"social": "这两周见朋友 0 次", "body": "训练节奏稳住了"}, "next": None}
                store.db.conn.execute("INSERT INTO season_notes (week, season, body, created_at) VALUES (?, ?, ?, ?)",
                                      (clock_week(NOON), s["name"], json.dumps(body, ensure_ascii=False), written))
                store.db.conn.commit()
                p = wb.season_page(NOON)
                self.assertFalse(p["outdated"])
                self.assertIn("见朋友 0 次", p["line"])  # 写完以后没新记录：照旧显示 小伴 的
                self.assertEqual(p["conclusion"], "")  # 小伴 写了总评就不再拼代码的结论（两句各说各的）
                self.assertEqual(kicks, [])
                self.assertTrue(wb.act("life.log", {"kind": "meet", "who": "小林", "day": (NOON.date() - timedelta(days=1)).isoformat()}, NOON)["ok"])
                p = wb.season_page(NOON)
                self.assertTrue(p["outdated"] and p["hidden"] and p["rewriting"])
                self.assertNotIn("见朋友 0 次", json.dumps(p, ensure_ascii=False))  # 补记后不再出现和现算矛盾的数字
                soc = next(d for d in p["domains"] if d["key"] == "social")
                self.assertEqual(soc["why_by"], "")  # 带数字的原因换成现算的
                self.assertTrue(soc["fact"])  # 现算的那一项（离标准最远的）
                body_d = next(d for d in p["domains"] if d["key"] == "body")
                self.assertEqual((body_d["why"], body_d["why_by"]), ("训练节奏稳住了", "小伴"))  # 只写判断的照旧
                self.assertRegex(p["note_at"], r"^\d+ 月 \d+ 日$")
                self.assertEqual(len(kicks), 1)
                for _ in range(season.REWRITE_CAP + 2):
                    wb.season_page(NOON)
                self.assertEqual(len(kicks), season.REWRITE_CAP)  # 每天最多重写几次
                capped = wb.season_page(NOON)
                self.assertFalse(capped["rewriting"])
                self.assertTrue(capped["capped"])
                wb.season_kick = lambda: False  # 15 分钟锁挡下：不算次数
                (Path(t) / season.REWRITES_FILE).unlink()
                wb.season_page(NOON)
                self.assertTrue(season.may_rewrite(t, NOON.date()))
                self.assertEqual(season._rewrites(t, NOON.date()), 0)
            finally:
                store.close()

    def test_writer_asks_for_judgement_not_numbers(self):
        self.assertIn("不写数字", season.WRITER)


def clock_week(now):
    from ethan import clock
    return clock.week_start(now.date()).isoformat()
