"""首批 5 道能力题的验收（docs/design/capability-questions.md 第 5 节的合同）：正常、变化、缺失、冲突、没权限。"""

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import lifebase, locks, onto_query, ontology, planner, whereabouts
from ethan.memory import MemoryStore

DAY = date(2026, 11, 3)


class Cal:
    def __init__(self, busy=(), broken=False):
        self._busy, self.broken = list(busy), broken

    def busy(self, day):
        if self.broken:
            raise OSError("iCloud 连不上")
        return self._busy


class OntoQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：随便聊聊")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def pref(self, text, **kw):
        return lifebase.add_fact(self.db, "preferences", text, [self.ev], kind="preference", **kw)

    def sid(self, fid):
        return self.db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (fid,)).fetchone()[0]

    # ---- 第 4 题 偏好 ----
    def test_preferences_normal_condition_change(self):
        self.pref("下雨天不出门跑步", context="下雨天")
        old = self.pref("喜欢靠窗的位子")
        new = self.pref("喜欢靠过道的位子")
        lifebase.supersede(self.db, old, new, when="2026-11-02")
        got = onto_query.preferences(self.db, "planner", "位子", today=DAY)
        self.assertEqual([i["value"] for i in got["items"]], ["喜欢靠过道的位子"])  # 旧的已被取代，不当现在
        rain = onto_query.preferences(self.db, "prep", "跑步", today=DAY)
        self.assertEqual(rain["items"][0]["context"], "下雨天")  # 条件一起给

    def test_preferences_guess_only_for_ethan_and_disputed(self):
        a = lifebase.add_evidence(self.db, "grok", "grok:1", "2026-11-01", "Grok 觉得她喜欢热闹")
        lifebase.add_fact(self.db, "preferences", "可能喜欢热闹的地方", [a], kind="hypothesis", source_kind="agent_relay")
        self.assertEqual(onto_query.preferences(self.db, "planner", "热闹", today=DAY)["status"], "unknown")
        mine = onto_query.preferences(self.db, "ethan", "热闹", today=DAY)
        self.assertTrue(mine["items"][0]["guess"])  # 给 小伴 时标明是猜测
        f = self.pref("咖啡要燕麦奶")
        self.db.conn.execute("UPDATE statements SET review = 'disputed' WHERE id = ?", (self.sid(f),))
        self.assertEqual(onto_query.preferences(self.db, "prep", "燕麦奶", today=DAY)["status"], "disputed")

    def test_forbidden_looks_like_nothing_for_public_users(self):
        f = self.pref("看牙医前要吃甜的")
        self.db.conn.execute("UPDATE facts SET read_by = ? WHERE id = ?", (json.dumps([{"user": "ethan"}]), f))
        self.assertEqual(onto_query.preferences(self.db, "planner", "牙医", today=DAY)["status"], "unknown")
        self.assertEqual(self.db.conn.execute("SELECT status FROM function_log ORDER BY id DESC").fetchone()[0], "forbidden")
        twin = onto_query.preferences(self.db, "twin", "牙医", today=DAY)
        self.assertEqual((twin["status"], twin["items"], twin["note"]), ("unknown", [], ""))  # 不暴露有没有

    def test_memory_hold_blocks_everyone_but_her_page(self):
        self.pref("喜欢靠窗的位子")
        lifebase.hold(self.db, "测试")
        self.assertEqual(onto_query.preferences(self.db, "ethan", "位子", today=DAY)["status"], "unavailable")
        self.assertEqual(onto_query.preferences(self.db, "me_page", "位子", today=DAY)["status"], "answer")

    # ---- 第 12 题 准备时的要求 ----
    def test_prep_requirements_only_confirmed_and_flags_history(self):
        self.pref("订餐厅之前先查退改")
        a = lifebase.add_evidence(self.db, "grok", "grok:2", "2026-11-01", "Grok：她可能讨厌排队")
        lifebase.add_fact(self.db, "preferences", "订餐厅可能讨厌排队", [a], kind="hypothesis", source_kind="agent_relay")
        got = onto_query.prep_requirements(self.db, "prep", "订餐厅", today=DAY)
        self.assertEqual([i["value"] for i in got["items"]], ["订餐厅之前先查退改"])  # 猜测不给准备员
        locks.lock(self.db, "task", 9, "deleted", title="订周五的餐厅")
        got = onto_query.prep_requirements(self.db, "prep", "订周五的餐厅", today=DAY)
        self.assertIn("删改记录", got["note"])
        none = onto_query.prep_requirements(self.db, "prep", "买滑雪装备", today=DAY)
        self.assertEqual(none["status"], "unknown")
        self.assertIn("默认规则", none["note"])

    # ---- 第 27 题 删过、改过、否掉过 ----
    def test_history_three_kinds_and_maybe_same(self):
        locks.lock(self.db, "task", 4, "deleted", title="查英语陪练的渠道")
        locks.lock(self.db, "task", 5, "title", "给导师发进度邮件", title="给导师发进度邮件")
        planner.note_override(self.db, DAY, "晚上跑步", "delete")
        got = onto_query.history(self.db, "planner", "查英语陪练的渠道")
        self.assertEqual([(i["what"], i["sure"]) for i in got["items"]], [("删过", True)])
        near = onto_query.history(self.db, "planner", "查英语陪练渠道")
        self.assertFalse(near["items"][0]["sure"])  # 说法相近：标“可能是同一件”
        self.assertEqual(onto_query.history(self.db, "planner", "晚上跑步")["items"][0]["what"], "否掉过（计划里删了）")
        locks.release_deleted(self.db, "查英语陪练的渠道")  # 她说“按系统的来”
        self.assertEqual(onto_query.history(self.db, "planner", "查英语陪练的渠道")["status"], "unknown")
        self.assertEqual(onto_query.history(self.db, "planner", "完全不相关的事")["note"], "没有记录")

    # ---- 第 9 题 定死的安排 ----
    def test_fixed_schedule_calendar_locks_and_unavailable(self):
        cal = Cal([("09:00", "10:00", "上课", {}), ("09:30", "11:00", "组会", {})])
        locks.lock(self.db, "event", f"{DAY}:训练", "time", {"start": "18:25", "end": "19:25"}, "day", DAY)
        got = onto_query.fixed_schedule(self.db, "planner", DAY, cal)
        self.assertEqual([i["title"] for i in got["items"]], ["上课", "组会", "训练"])
        self.assertIn("重叠", got["note"])
        self.assertEqual(onto_query.fixed_schedule(self.db, "planner", DAY, Cal(broken=True))["status"], "unavailable")
        self.assertEqual(onto_query.fixed_schedule(self.db, "planner", DAY, None)["status"], "unavailable")  # 读不到不当没安排
        self.assertEqual(onto_query.fixed_schedule(self.db, "twin", DAY, cal)["status"], "unknown")

    # ---- 第 1 题 在做什么、住哪、今天在哪 ----
    def test_whereabouts_three_parts_not_inferred(self):
        ontology.ensure(self.db)
        me = ontology.self_id(self.db)
        sit = self.db.conn.execute("INSERT INTO objects (name, created_at) VALUES ('在 某商学院 交换', 'x')").lastrowid
        lbs = self.db.conn.execute("INSERT INTO objects (name, created_at) VALUES ('某商学院', 'x')").lastrowid
        self.db.conn.execute("INSERT INTO object_types VALUES (?, 'situation')", (sit,))
        self.db.conn.execute("INSERT INTO situation_roles VALUES (?, ?, '交换生')", (sit, me))
        self.db.conn.execute("INSERT INTO situation_roles VALUES (?, ?, '学校')", (sit, lbs))
        for prop, value in (("situation_start", "2026-09-01"), ("situation_end", "2027-01-31")):
            self.db.conn.execute("INSERT INTO statements (subject_id, property, value_text, kind, said_by, recorded_at, review) "
                                 "VALUES (?, ?, ?, 'fact', 'her_words', 'x', 'accepted')", (sit, prop, value))
        got = onto_query.whereabouts_now(self.db, "ethan", today=DAY)
        parts = {i["part"]: i for i in got["items"]}
        self.assertEqual(parts["在做什么"]["value"][0]["what"], "在 某商学院 交换")
        self.assertIsNone(parts["今天在哪"]["value"])  # 交换在伦敦 ≠ 今天在伦敦
        self.assertIn("不从住处推", got["note"])
        whereabouts.note(self.db, "[在 巴黎]", now=datetime(2026, 11, 3, 10, tzinfo=timezone.utc))
        got = onto_query.whereabouts_now(self.db, "ethan", today=DAY)
        self.assertEqual({i["part"]: i for i in got["items"]}["今天在哪"]["value"], "巴黎")
        self.assertEqual(onto_query.whereabouts_now(self.db, "twin", today=DAY)["status"], "unknown")

    def test_planner_switch_and_rollback(self):
        self.pref("下雨天不出门跑步", context="下雨天")
        locks.lock(self.db, "task", 4, "deleted", title="查英语陪练的渠道")
        self.assertFalse(onto_query.reading_new(self.db, "planner"))  # 默认没切
        old = planner._profile(self.db)
        items = [{"kind": "todo", "title": "查英语陪练渠道", "priority": "side"}, {"kind": "todo", "title": "买菜", "priority": "side"}]
        planner.link_tasks(self.db, items, DAY)
        before = [i["title"] for i in items]
        onto_query.switch(self.db, "planner", True)
        self.assertIn("下雨天不出门跑步（下雨天）", planner._profile(self.db))
        items = [{"kind": "todo", "title": "查英语陪练渠道", "priority": "side"}, {"kind": "todo", "title": "买菜2", "priority": "side"}]
        planner.link_tasks(self.db, items, DAY)
        self.assertEqual([i["title"] for i in items], ["买菜2"])  # 切过去以后照样不建回她删过的
        self.assertEqual(before, ["买菜"])
        onto_query.switch(self.db, "planner", False)  # 回退
        self.assertFalse(onto_query.reading_new(self.db, "planner"))
        self.assertEqual(planner._profile(self.db), old)

    def test_prep_and_ethan_switch(self):
        from ethan import helpscope  # noqa: F401  确认导入不坏
        self.pref("订餐厅之前先查退改")
        locks.lock(self.db, "task", 4, "deleted", title="查英语陪练的渠道")
        for user in ("prep", "ethan"):
            onto_query.switch(self.db, user, True)
            self.assertTrue(onto_query.reading_new(self.db, user))
            self.assertIn("订餐厅之前先查退改", onto_query.preference_lines(self.db, user))
            self.assertTrue(onto_query.she_removed(self.db, user, "查英语陪练渠道"))
            onto_query.switch(self.db, user, False)
            self.assertFalse(onto_query.reading_new(self.db, user))

    def test_weekly_spend_and_incomplete_week(self):
        from ethan import finance
        finance.ensure(self.db)
        rows = [("2026-11-02", -12.5, "GBP", "Tesco"), ("2026-11-03", -40.0, "GBP", "Dishoom"), ("2026-11-03", -3.2, "GBP", "TfL"),
                ("2026-10-28", -20.0, "GBP", "Pret"), ("2026-11-03", -100.0, "GBP", "存钱")]
        for i, (d, amt, cur, m) in enumerate(rows):
            self.db.conn.execute("INSERT INTO transactions (source, ext_id, at, day, amount, currency, merchant, category) VALUES "
                                 "('monzo', ?, ?, ?, ?, ?, ?, ?)", (f"t{i}", d + "T12:00:00", d, amt, cur, m, "savings" if m == "存钱" else None))
        got = onto_query.weekly_spend(self.db, "ethan", today=DAY)  # 11-03 是周二
        w = got["items"][0]
        self.assertEqual(w["this_week"]["by_currency"], {"GBP": 55.7})  # 存钱不算花销
        self.assertEqual([t["merchant"] for t in w["top"]], ["Dishoom", "Tesco", "TfL"])
        self.assertEqual(w["past"][0]["by_currency"], {"GBP": 20.0})
        self.assertEqual(got["note"], "")  # 周一、周二都有流水
        self.assertEqual(onto_query.weekly_spend(self.db, "twin", today=DAY)["status"], "unknown")  # 钱不对外
        later = onto_query.weekly_spend(self.db, "ethan", today=date(2026, 11, 5))
        self.assertIn("不完整", later["note"])  # 周三、周四没流水，照样报，标不完整

    def test_rhythm_describes_with_samples(self):
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS work_slots (slot TEXT PRIMARY KEY, source TEXT NOT NULL)")
        for d, t in (("2026-10-27", "09:40"), ("2026-10-28", "10:20"), ("2026-10-29", "10:05"), ("2026-11-01", "13:00")):  # 11-01 是周日，不算
            self.db.conn.execute("INSERT INTO work_slots VALUES (?, 'mac')", (f"{d}T{t}",))
        got = onto_query.rhythm(self.db, "ethan", today=DAY)
        item = got["items"][0]
        self.assertEqual((item["value"], item["samples"]), ("10:05", 3))
        self.assertIn("不等于开始工作", item["note"])

    def _slots(self, rows, source="mac"):
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS work_slots (slot TEXT PRIMARY KEY, source TEXT NOT NULL)")
        for s in rows:
            self.db.conn.execute("INSERT OR IGNORE INTO work_slots VALUES (?, ?)", (s, source))

    def test_work_start_skips_past_midnight_run(self):
        # 服务器 11-04 查实：前一晚 23:55 一直连到第二天 00:00–01:10，是熬夜没停，不是 00:00 开始
        for d in ("2026-10-27", "2026-10-28", "2026-10-29"):  # 周二到周四
            nxt = date.fromisoformat(d) + timedelta(days=1)
            self._slots([f"{d}T09:30", f"{d}T09:35", f"{d}T23:50", f"{d}T23:55"]
                        + [f"{nxt.isoformat()}T{h:02d}:{m:02d}" for h in (0, 1) for m in range(0, 60, 5) if h * 60 + m <= 70])
        self._slots(["2026-10-30T10:00"])  # 周五：前一晚 01:10 收工，10:00 才是开始
        items = {i["key"]: i for i in onto_query.rhythm_items(self.db, DAY)}
        self.assertEqual(items["work_start"]["value"], "09:30")  # 10-28、10-29 的 00:00 不算，三天 09:30 + 周五 10:00
        self.assertEqual(items["work_start"]["samples"], 4)
        self.assertEqual(items["sleep_late"]["value"], "01:10")  # 过了午夜算前一晚
        self.assertEqual(items["sleep_late"]["samples"], 4)  # 10-27、28、29 晚到 01:10，10-30 晚 10:00 后空着
        self.assertIn("前一晚", items["sleep_late"]["note"])

    def test_work_start_only_counts_mac(self):
        self._slots(["2026-10-27T09:00", "2026-10-28T09:00", "2026-10-29T09:00"])
        self._slots(["2026-10-27T07:00", "2026-10-28T07:00", "2026-10-29T07:00"], source="workbench")
        self._slots(["2026-10-27T06:00", "2026-10-28T06:00"], source="claude")
        items = {i["key"]: i for i in onto_query.rhythm_items(self.db, DAY)}
        self.assertEqual((items["work_start"]["value"], items["work_start"]["samples"]), ("09:00", 3))

    def test_work_start_needs_three_days(self):
        self._slots(["2026-10-27T09:00", "2026-10-28T09:00"])
        self._slots(["2026-10-29T08:00", "2026-10-30T08:00"], source="claude")
        keys = [i["key"] for i in onto_query.rhythm_items(self.db, DAY)]
        self.assertNotIn("work_start", keys)
        self.assertNotIn("sleep_late", keys)
        self.assertEqual(lifebase.write_behavior(self.db, DAY)["written"], 0)

    def test_work_start_not_before_5am(self):
        # 02:00 起来干活（前面空了 4 小时以上）不算开始；决定 9（11-06）：和工作时长一样按凌晨 5 点分天，04:30 的也不算（以前 4 点），05:30 的算
        for d in ("2026-10-27", "2026-10-28", "2026-10-29"):
            self._slots([f"{d}T02:00", f"{d}T02:05", f"{d}T09:00"])
        self._slots(["2026-10-30T04:30"])
        items = {i["key"]: i for i in onto_query.rhythm_items(self.db, DAY)}
        self.assertEqual((items["work_start"]["value"], items["work_start"]["samples"]), ("09:00", 3))
        self.assertNotIn("02:", items["work_start"]["value"])
        self._slots(["2026-10-31T05:30"])  # 周六不算工作日；换成下周一前的周五已经用过，直接查 mac_sessions
        starts, nights = onto_query.mac_sessions(self.db, date(2026, 10, 27), date(2026, 11, 2))
        self.assertNotIn(4 * 60 + 30, starts)
        self.assertIn(28 * 60 + 30, nights)  # 10-30 04:30 收工算 10-29 那一晚（相对那晚 0 点 28:30）

    def test_write_behavior_replaces_same_week_when_algorithm_changes(self):
        self._slots(["2026-10-27T09:40", "2026-10-28T10:20", "2026-10-29T10:05"])
        ev = lifebase.add_evidence(self.db, "computer", "behavior:work_start:2026-11-02", DAY.isoformat(), text="旧算法：00:00。x")
        old = lifebase.add_fact(self.db, "rhythms", "最近 4 周工作日一般 00:00 开始用 Mac（样本 5 天）", [ev], kind="fact",
                                source_kind="behavior", prop="work_start", valid_from=DAY.isoformat())
        got = lifebase.write_behavior(self.db, DAY)
        self.assertEqual((got["written"], got["superseded"]), (2, 1))  # work_start 取代旧的 + 新的 sleep_late
        live = self.db.conn.execute("SELECT statement FROM facts WHERE prop = 'work_start' AND valid_to IS NULL").fetchall()
        self.assertEqual([r[0] for r in live], ["最近 4 周工作日一般 10:05 开始用 Mac（样本 3 天）"])
        self.assertIsNotNone(self.db.conn.execute("SELECT valid_to FROM facts WHERE id = ?", (old,)).fetchone()[0])
        self.assertEqual(lifebase.write_behavior(self.db, DAY)["written"], 0)  # 再跑不重复

    def test_migration_drops_old_midnight_work_start(self):
        import importlib.util
        self._slots(["2026-10-27T09:00", "2026-10-28T09:00"])  # 新算法样本不够，不写新的
        ev = lifebase.add_evidence(self.db, "computer", "behavior:work_start:2026-11-02", DAY.isoformat(), text="旧算法")
        old = lifebase.add_fact(self.db, "rhythms", "最近 4 周工作日一般 00:00 开始用 Mac（样本 5 天）", [ev], kind="fact",
                                source_kind="behavior", prop="work_start", valid_from=DAY.isoformat())
        path = Path(__file__).resolve().parent.parent / "migrations" / "20261104_fix_work_start.py"
        spec = importlib.util.spec_from_file_location("m_fix_work_start", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn("作废 1 条", mod.run(self.store))
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (old,)).fetchone()[0], "invalid")

    def test_write_behavior_weekly_supersedes_and_is_idempotent(self):
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS work_slots (slot TEXT PRIMARY KEY, source TEXT NOT NULL)")
        for d, t in (("2026-10-27", "09:40"), ("2026-10-28", "10:20"), ("2026-10-29", "10:05")):
            self.db.conn.execute("INSERT INTO work_slots VALUES (?, 'mac')", (f"{d}T{t}",))
        got = lifebase.write_behavior(self.db, DAY)
        self.assertEqual(got["written"], 2)  # work_start + sleep_late
        self.assertEqual(lifebase.write_behavior(self.db, DAY)["written"], 0)  # 同一周重跑不重复记
        f = self.db.conn.execute("SELECT * FROM facts WHERE prop = 'work_start'").fetchone()
        self.assertEqual((f["source_kind"], f["dimension"]), ("behavior", "rhythms"))
        self.assertIn("10:05", f["statement"])
        self.assertIn("样本 3 天", f["statement"])
        st = self.db.conn.execute("SELECT * FROM statements WHERE property = 'work_start'").fetchone()
        self.assertIsNotNone(st)
        self.db.conn.execute("INSERT INTO work_slots VALUES ('2026-11-03T08:00', 'mac')")
        got = lifebase.write_behavior(self.db, DAY + timedelta(days=7))
        self.assertEqual((got["written"], got["superseded"]), (2, 2))
        live = self.db.conn.execute("SELECT COUNT(*) FROM facts WHERE prop = 'work_start' AND valid_to IS NULL").fetchone()[0]
        self.assertEqual(live, 1)

    def test_rhythm_counts_calendar_and_workbench_edits(self):
        from ethan import locks
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS future_events (id INTEGER PRIMARY KEY, day TEXT NOT NULL, start TEXT NOT NULL, "
                             "end TEXT NOT NULL, title TEXT NOT NULL, place TEXT, note TEXT, uid TEXT, created_at TEXT NOT NULL)")
        for d in ("2026-10-20", "2026-10-28", "2026-11-01", "2026-11-10"):  # 11-10 在以后，不算
            self.db.conn.execute("INSERT INTO future_events (day, start, end, title, created_at) VALUES (?, '10:00', '11:00', '会', 'x')", (d,))
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS deleted_tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, goal_id INTEGER, at TEXT NOT NULL)")
        self.db.conn.execute("INSERT INTO deleted_tasks (title, at) VALUES ('x', '2026-10-30T09:00:00')")
        items = {i["key"]: i for i in onto_query.rhythm_items(self.db, DAY)}
        self.assertEqual(items["calendar_per_week"]["samples"], 3)
        self.assertIn("不等于真的去了", items["calendar_per_week"]["note"])
        self.assertGreaterEqual(items["workbench_edits"]["value"], 1)
        got = lifebase.write_behavior(self.db, DAY)
        self.assertGreaterEqual(got["written"], 2)

    def test_usage_logged_and_gates(self):
        onto_query.preferences(self.db, "planner", "位子", today=DAY)
        self.assertEqual(self.db.conn.execute("SELECT fn, user FROM function_log").fetchone()[:], ("preferences", "planner"))
        self.assertTrue(onto_query.gates(self.db)["pass"])


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
