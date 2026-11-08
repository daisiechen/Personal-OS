"""任务按预计工作量定档、效率补漏算的分、首页英语次数统一口径（晚定，原话“要”）。

docs/design/tasks-and-schedule.md 第 5 节、docs/design/territories.md “事业的效率和任务分”。
"""

import importlib.util
import re
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from ethan import attributes, english, locks, quests, stages, taskrules, territory, workaholic, worktime
from ethan.clock import LOCAL
from ethan.core.router import Settings
from ethan.memory import MemoryStore
from ethan.workbench.api import Workbench

ROOT = Path(__file__).resolve().parent.parent
D = date(2026, 11, 6)  # 周五


def goal(db, title, domain="事业", status="active"):
    return db.conn.execute("INSERT INTO goals (title, level, status, updated_at, domain) VALUES (?, 'year', ?, 'x', ?)",
                           (title, status, domain)).lastrowid


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        taskrules.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class SizeTest(Base):
    def test_estimate_rules(self):
        for title, size in (("DemoApp 工作台上线", "XL"), ("任务和记忆系统验收", "XL"), ("搭建个人主页", "XL"),
                            ("申请英国人才签", "L"), ("准备签证材料", "L"), ("写个人主页文案", "M"),
                            ("回复房东邮件", "S"), ("预约牙医", "S"), ("买蛋白粉", "S"), ("给银行打电话", "S"),
                            ("回复系统通知邮件", "S"), ("随便一件小事", "S")):
            self.assertEqual(taskrules.estimate(title)[0], size, title)
        size, why = taskrules.estimate("看一下数据", "DemoApp 上线")
        self.assertEqual(size, "M")
        self.assertIn("DemoApp 上线", why)
        self.assertTrue(taskrules.estimate("随便一件小事")[1].startswith(taskrules.NO_HINT))

    def test_points_by_size_and_bonuses(self):
        P = quests.SIZE_POINTS
        self.assertEqual(P, {"S": 40, "M": 100, "L": 200, "XL": 400})
        for s, p in P.items():
            self.assertEqual(quests.task_points({"size": s}), p)  # 其他（支线）定了档也按档
            self.assertEqual(quests.task_points({"size": s, "quest": "main"}), p)
        self.assertEqual(quests.task_points({"size": "XL", "quest": "main", "milestone": 1}), 700)  # 节点 +300 照旧
        self.assertEqual(quests.task_points({"size": "L", "hard": 1}), 300)                         # 硬骨头 ×1.5 照旧
        self.assertEqual(quests.task_points({"size": "XL", "source_type": "email"}), 0)            # 邮件来的照旧不计分
        self.assertEqual(quests.task_points({}), quests.SIDE_POINTS)                                # 没定档的老待办照旧
        stage = {"milestone_id": 1, "cadence": "once", "size": "XL"}
        self.assertEqual(stages.points_for(stage), 400)                                             # 阶段任务一次性的按档
        self.assertEqual(quests.task_points(stage), 400)
        self.assertEqual(stages.points_for({**stage, "cadence": "weekly"}), 10)                     # 反复做的每次 +10 不变
        self.assertEqual(stages.points_for({**stage, "habit_id": 3}), 0)

    def test_add_estimates_or_takes_given_size(self):
        a = taskrules.add(self.db, "DemoApp 工作台上线")
        b = taskrules.add(self.db, "回复房东", size="L")
        c = taskrules.add(self.db, "看一下", size="乱写")
        size = lambda t: self.db.conn.execute("SELECT size FROM tasks WHERE id = ?", (t,)).fetchone()[0]
        self.assertEqual((size(a), size(b), size(c)), ("XL", "L", "S"))

    def test_stage_add_and_breakdown(self):
        g = goal(self.db, "DemoApp")
        m = stages.add_milestone(self.db, g, "工作台上线")
        t1 = stages.add_task(self.db, m, "任务和记忆系统验收")
        t2 = stages.add_task(self.db, m, "看一眼数据")          # 标题看不出，里程碑是大活 → M
        t3 = stages.add_task(self.db, m, "每周复盘", "weekly", 1)
        t4 = stages.add_task(self.db, m, "发消息", size="M")
        sz = {r[0]: r[1] for r in self.db.conn.execute("SELECT id, size FROM tasks")}
        self.assertEqual((sz[t1], sz[t2], sz[t4]), ("XL", "M", "M"))
        self.assertEqual(stages.points_for(dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (t3,)).fetchone())), 10)
        ids = quests.add_breakdown(self.db, g, [{"title": "迁移数据库", "size": "XL"}, {"title": "上线主页"}, {"title": "买域名", "size": "S"}])
        sz = {r[0]: r[1] for r in self.db.conn.execute("SELECT id, size FROM tasks")}
        self.assertEqual([sz[i] for i in ids], ["XL", "XL", "S"])

    def test_every_entry_goes_through_taskrules_add(self):
        """建任务只走 taskrules.add（定档在那里）；直接 db.add_task 的只剩一次性脚本。"""
        allowed = {"taskrules.py", "goals_import.py", "season1.py", "season1_fix.py"}  # 一次性导入脚本，不再建新任务
        bad = []
        for f in (ROOT / "ethan").rglob("*.py"):
            if f.name in allowed or f.match("memory/db.py"):
                continue
            src = f.read_text(encoding="utf-8")
            for m in re.finditer(r"(?<!stages)(?<!taskrules)\.add_task\(", src):
                bad.append(f"{f.relative_to(ROOT)}:{src[:m.start()].count(chr(10)) + 1}")
        self.assertEqual(bad, [])

    def test_js_size_table_matches(self):
        app = (ROOT / "ethan/workbench/static/app.js").read_text(encoding="utf-8")
        table = re.search(r"const SIZES = \[(.*?)\];", app).group(1)
        got = {k: int(p) for k, _, p in re.findall(r'\["(\w+)", "([^"]+)", (\d+)\]', table)}
        self.assertEqual(got, quests.SIZE_POINTS)
        self.assertEqual([x for x in re.findall(r'\["\w+", "([^"]+)"', table)], list(taskrules.SIZES.values()))


class WorkbenchSizeTest(Base):
    def setUp(self):
        super().setUp()
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))
        self.now = datetime(2026, 11, 6, 12, 0)

    def test_add_task_and_change_size_locks(self):
        r = self.wb.act("task.add", {"title": "DemoApp 工作台上线", "when": "week"}, self.now)
        self.assertTrue(r["ok"], r)
        tid = self.db.conn.execute("SELECT id FROM tasks WHERE title = 'DemoApp 工作台上线'").fetchone()[0]
        row = lambda: dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        self.assertEqual(row()["size"], "XL")
        panel = self.wb.plan_panel(D, None)
        todo = next(t for t in panel["todos"] if t["id"] == tid)
        self.assertEqual((todo["size"], todo["points"]), ("XL", 400))
        r = self.wb.act("task.size", {"id": tid, "size": "M"}, self.now)
        self.assertTrue(r["ok"], r)
        self.assertIn("+100", r["msg"])
        self.assertEqual(row()["size"], "M")
        self.assertTrue(locks.locked(self.db, "task", tid, "size"))
        self.assertFalse(self.wb.act("task.size", {"id": tid, "size": "XXL"}, self.now)["ok"])
        self.wb.act(r["undo"]["action"], {k: v for k, v in r["undo"].items() if k != "action"}, self.now)
        self.assertEqual(row()["size"], "XL")

    def test_step_and_stage_add_estimate(self):
        g = goal(self.db, "DemoApp")
        m = stages.add_milestone(self.db, g, "M1")
        self.wb.act("step.add", {"goal_id": g, "title": "迁移数据库"}, self.now)
        self.wb.act("stage.add", {"milestone_id": m, "title": "预约律师"}, self.now)
        self.wb.act("stage.add", {"milestone_id": m, "title": "写方案", "size": "XL"}, self.now)
        sz = {r[0]: r[1] for r in self.db.conn.execute("SELECT title, size FROM tasks")}
        self.assertEqual((sz["迁移数据库"], sz["预约律师"], sz["写方案"]), ("XL", "S", "XL"))


class MigrationTest(Base):
    def load(self):
        spec = importlib.util.spec_from_file_location("m", ROOT / "migrations/20261106_effort_sizes.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_backfill_only_what_it_should(self):
        db = self.db
        g = goal(db, "DemoApp")
        m = stages.add_milestone(db, g, "工作台上线")

        def raw(title, **kw):
            tid = db.add_task(title)
            if kw:
                db.conn.execute(f"UPDATE tasks SET {', '.join(k + ' = ?' for k in kw)} WHERE id = ?", (*kw.values(), tid))
            return tid
        T = {
            "stage": raw("DemoApp 工作台上线", goal_id=g, milestone_id=m, cadence="once", size="S", quest="main"),
            "stage_ctx": raw("看一下数据", goal_id=g, milestone_id=m, cadence="once", size="S", quest="main"),
            "side": raw("申请英国人才签"),
            "small": raw("预约牙医"),
            "done": raw("搭建系统", done=1, done_at="2026-11-01T00:00:00+00:00", size="S"),
            "locked": raw("迁移数据", size="S"),
            "weekly": raw("每周写周报", goal_id=g, milestone_id=m, cadence="weekly", size="S", quest="main"),
            "ethan": raw("搭建行程表", promised_by_ethan=1),
            "email": raw("准备材料给银行", source_type="email"),
            "dropped": raw("搭建店铺", status="drop_asked"),
            "planner": raw("见客户", goal_id=g, quest="main", size="L"),
        }
        locks.lock(db, "task", T["locked"], "size", "S")
        db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (40, 'x', ?, '2026-11-01', 'x')", (f"task:{T['done']}",))
        db.conn.commit()
        note = self.load().run(self.store)
        sz = {r[0]: r[1] for r in db.conn.execute("SELECT id, size FROM tasks")}
        self.assertEqual(sz[T["stage"]], "XL")
        self.assertEqual(sz[T["stage_ctx"]], "M")
        self.assertEqual(sz[T["side"]], "L")
        self.assertEqual(sz[T["small"]], "S")
        self.assertEqual(sz[T["planner"]], "L")  # 计划器的档，规则看不出 → 保留
        for k in ("done", "locked", "weekly"):
            self.assertEqual(sz[T[k]], "S", k)
        for k in ("ethan", "email", "dropped"):
            self.assertIsNone(sz[T[k]], k)
        self.assertEqual(db.conn.execute("SELECT delta FROM points WHERE ref = ?", (f"task:{T['done']}",)).fetchone()[0], 40)
        self.assertIn("改了 4 件", note)
        review = taskrules.size_review(db)
        self.assertEqual({x["id"] for x in review}, {T[k] for k in ("stage", "stage_ctx", "side", "small", "planner")})
        self.assertEqual(review[0]["size"], "XL")  # 大的在前
        self.assertTrue(all(x["why"] for x in review))
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        self.assertEqual(len(wb.plan_panel(D, None)["size_review"]), 5)
        wb.act("task.size", {"id": T["side"], "size": "XL"}, datetime(2026, 11, 6, 12))
        self.assertEqual(len(taskrules.size_review(db)), 4)  # 改过的那件算过目了
        self.assertTrue(wb.act("size.ok", {}, datetime(2026, 11, 6, 12))["ok"])
        self.assertEqual(taskrules.size_review(db), [])


class EfficiencyTest(Base):
    def work(self, day, hours):
        slots, t = [], datetime(day.year, day.month, day.day, 9, 0, tzinfo=LOCAL)
        for _ in range(int(hours * 12)):
            slots.append(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(minutes=5)
        worktime.record(self.db, slots, "mac")

    def pts(self, ref, n, day):
        self.db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (?, 'x', ?, ?, 'x')", (n, ref, day.isoformat()))

    def test_output_counts_milestones_and_repeats_and_avg4(self):
        db = self.db
        workaholic.ensure(db)
        g, life = goal(db, "DemoApp", "事业"), goal(db, "攀岩", "生活体验")
        m, ml = stages.add_milestone(db, g, "M1"), stages.add_milestone(db, life, "L1")
        once = taskrules.add(db, "上线", goal_id=g)
        rep = stages.add_task(db, m, "每天写日志", "daily")
        keyed = taskrules.add(db, "写论文")
        db.conn.execute("UPDATE tasks SET domain = 'academic' WHERE id = ?", (keyed,))  # 库里记的是领域的键
        other = taskrules.add(db, "去攀岩", goal_id=life)
        self.pts(f"task:{once}", 400, D)
        self.pts(f"stage:{rep}:{D.isoformat()}", 10, D)
        self.pts(f"milestone:{m}", 300, D)
        self.pts(f"task:{keyed}", 100, D)
        self.pts(f"task:{other}", 40, D)
        self.pts(f"milestone:{ml}", 300, D)
        old = D - timedelta(days=20)
        self.pts(f"task:{once}", 200, old)
        self.work(D, 4)
        self.work(old, 6)
        out = workaholic.output(db, D, D + timedelta(days=1))
        self.assertEqual(out["points"], 810)
        self.assertEqual(out["parts"], {"task": 500, "stage": 10, "milestone": 300})
        e = workaholic.efficiency(db, D)
        self.assertEqual((e["week_points"], e["week_hours"], e["week"]), (810, 4.0, 202.5))
        self.assertEqual((e["avg4_points"], e["avg4_hours"], e["avg4"]), (1010, 10.0, 101.0))
        self.assertEqual(workaholic.week(db, D)["efficiency"], e["week"])
        # 领域页和周报同一个函数、两个数一致
        rows = territory.health(db, "career", D, True)
        row = next(r for r in rows if r["name"].startswith("效率"))
        self.assertIn("本周 202.5", row["actual"])
        self.assertIn("最近 4 周平均 101", row["actual"])
        from ethan import report
        self.assertEqual(report._work_line(db, D)["效率"], {"本周": 202.5, "最近4周平均": 101.0})


class EnglishSameCountTest(Base):
    def test_home_territory_attributes_agree(self):
        db = self.db
        quests.seed(db)
        h = db.conn.execute("SELECT id FROM habits WHERE title = ?", (english.HABIT,)).fetchone()[0]
        monday = D - timedelta(days=D.weekday())
        english.record_said(db, monday, 20)                          # 只有练习记录、没打卡
        quests.check_habit(db, h, monday + timedelta(days=1))        # 只打卡
        english.record_said(db, monday + timedelta(days=2), 15)
        quests.check_habit(db, h, monday + timedelta(days=2))        # 两样都有，算一天
        want = len(english.practice_days(db, monday, D))
        self.assertEqual(want, 3)
        home = next(x for x in quests.today_summary(db, D)["weekly"] if x["title"] == english.HABIT)
        self.assertEqual(home["count"], want)
        row = next(r for r in territory.health(db, "study", D, True) if r["name"] == english.HABIT)
        self.assertEqual(row["actual"], f"{want} 次")
        item = next(x for x in attributes.study(db, monday, D) if x["name"] == "英语陪练")
        self.assertEqual(item["actual"], f"{want} 次")
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        g = quests.today_summary(db, D)
        self.assertEqual(next(x for x in wb._weekly(g, D) if x["title"] == english.HABIT)["count"], want)


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
