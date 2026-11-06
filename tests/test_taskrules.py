"""任务规矩（ethan/taskrules.py，的统一方案，docs/design/tasks-and-schedule.md 第 8 节）。

对照表变成可执行的：每种状态各造一件，断言每个视图收哪几件；再查视图之间的关系和写入口。
"""

import re
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import locks, stages, taskrules
from ethan.memory import MemoryStore

D = date(2026, 11, 6)  # 周五
ROOT = Path(__file__).resolve().parent.parent / "ethan"


def iso(n: int) -> str:
    return (D + timedelta(days=n)).isoformat()


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        db = self.db = self.store.db
        taskrules.ensure(db)

        def goal(title, status, handled=None):
            return db.conn.execute("INSERT INTO goals (title, level, status, updated_at, handled_by) VALUES (?, 'year', ?, 'x', ?)",
                                   (title, status, handled)).lastrowid
        self.g_active, self.g_paused = goal("减脂", "active"), goal("进修", "paused")
        self.g_done, self.g_not = goal("搬家", "done"), goal("学法语", "not_started")
        self.g_drop, self.g_agent = goal("开店", "dropped"), goal("DemoApp", "active", "codex")
        self.m1 = stages.add_milestone(db, self.g_active, "M1", iso(9))
        self.m2 = stages.add_milestone(db, self.g_active, "M2", iso(30))
        db.conn.execute("UPDATE milestones SET created_at = '2026-11-01T00:00:00+00:00'")
        habit = db.conn.execute("INSERT INTO habits (title, cadence, target, points) VALUES ('训练', 'daily', 1, 10)").lastrowid

        def side(title, **kw):
            tid = db.add_task(title, due=kw.pop("due", None), goal_id=kw.pop("goal_id", None),
                              promised_by_ethan=kw.pop("ethan", False))
            if kw:
                db.conn.execute(f"UPDATE tasks SET {', '.join(k + ' = ?' for k in kw)} WHERE id = ?", (*kw.values(), tid))
            return tid

        T = self.T = {}
        T["open"] = side("买运动袜", due=iso(1))                 # 2 天内：今日
        T["other"] = side("约牙医", due=iso(10))                 # 30 天内：其他
        T["far"] = side("约年度体检", due=iso(60))               # 30 天以后：不列
        T["nodue"] = side("整理照片")                            # 没截止：其他
        T["done"] = side("交房租", due=iso(0), done=1, done_at="2026-10-01T10:00:00+00:00")
        T["ethan"] = side("起草邀请消息", due=iso(0), ethan=True)
        T["agent"] = side("改落地页", due=iso(0), owner="codex")
        T["dropped"] = side("办 NI 号", due=iso(0), status="drop_asked")
        T["goal_dropped"] = side("看店面", due=iso(0), goal_id=self.g_drop)
        T["goal_paused"] = side("联系导师", due=iso(0), goal_id=self.g_paused)
        T["goal_done"] = side("退押金", goal_id=self.g_done)
        T["goal_not"] = side("报法语课", goal_id=self.g_not)
        T["not_started"] = side("11/12 前没出签就催问", due=iso(6), start_day=iso(6))
        T["postponed"] = side("回导师邮件", due=iso(1), postpone_day=D.isoformat())
        T["waiting"] = side("拿到芬兰签证（等结果）", due=iso(0))
        T["mail"] = side("确认快递地址", source_type="email")
        T["agent_main"] = side("写 PRD", goal_id=self.g_agent, quest="main")
        T["stage"] = stages.add_task(db, self.m1, "买体脂秤")                 # 当前阶段一次性：今天该做
        T["stage_later"] = stages.add_task(db, self.m2, "测体脂", "per_milestone")
        T["habit"] = stages.add_task(db, self.m1, "训练 5 次", "weekly", per_week=5, habit_id=habit)
        T["blocked"] = stages.add_task(db, self.m1, "照着秤称", depends_on=T["stage"])
        T["review"] = stages.add_task(db, self.m1, "M1 复盘", review=True)  # 截止前 2 天才排
        db.conn.commit()
        self.plan = {"items": [{"kind": "todo", "task_id": T["stage"], "priority": "main", "how": "下一步"},
                               {"kind": "todo", "task_id": T["review"], "priority": "main", "how": "下一步"}]}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ids(self, view, plan=None):
        return set(taskrules.view_ids(self.db, view, D, plan if plan is not None else self.plan))

    def names(self, ids):
        back = {v: k for k, v in self.T.items()}
        return {back[i] for i in ids if i in back}


class StateTest(Fixture):
    def test_every_state(self):
        want = {"open": "open", "other": "open", "far": "open", "nodue": "open", "done": "done", "ethan": "not_mine",
                "agent": "not_mine", "dropped": "dropped", "goal_dropped": "goal_dropped", "goal_paused": "goal_later",
                "goal_done": "goal_later", "goal_not": "goal_later", "not_started": "not_started", "postponed": "postponed",
                "waiting": "waiting", "mail": "open", "agent_main": "agent_goal", "stage": "open", "stage_later": "stage_later",
                "habit": "habit_ref", "blocked": "blocked", "review": "stage_rest"}
        got = {k: taskrules.get(self.db, v, D)["state"] for k, v in self.T.items()}
        self.assertEqual(got, want)
        self.assertEqual(set(want.values()), set(taskrules.STATES) - set())  # 每种状态都造了一件
        self.assertEqual(taskrules.get(self.db, self.T["not_started"], D)["label"], "11/12 起")
        self.assertEqual(taskrules.get(self.db, self.T["waiting"], D)["label"], "在等")

    def test_when_windows(self):
        w = {k: taskrules.get(self.db, self.T[k], D)["when"] for k in ("open", "other", "far", "nodue")}
        self.assertEqual(w, {"open": "soon", "other": "month", "far": "far", "nodue": "none"})


class ViewMatrixTest(Fixture):
    def test_matrix(self):
        """第 8 节对照表：每个视图收哪几件。"""
        want = {
            "today": {"open", "mail", "stage"},
            "other": {"other", "nodue"},
            "later": {"goal_paused", "goal_done", "goal_not"},
            "dropped": {"dropped"},
            "plan_panel": {"open", "mail", "stage", "other", "nodue"},
            "tg_list": {"open", "mail", "stage", "other", "nodue"},
            "tg_buttons": {"open", "mail", "stage"},
            "ethan_ctx": {"open", "mail", "stage", "other", "nodue", "waiting", "postponed", "not_started"},
            "territory": {"open", "other", "far", "nodue", "not_started", "postponed", "waiting", "mail", "stage", "blocked", "review"},
            "helpscope": {"open", "not_started", "stage"},
            "planner_in": {"open", "other", "nodue", "mail", "stage", "blocked", "review"},
        }
        for view, names in want.items():
            self.assertEqual(self.names(self.ids(view)), names, view)

    def test_quest_detail_lists_waiting_with_label_not_dropped(self):
        got = self.names(self.ids("quest_detail"))
        self.assertIn("waiting", got)
        self.assertIn("agent_main", got)  # 交给 agent 的副本详情照样列主线
        self.assertNotIn("dropped", got)
        self.assertNotIn("ethan", got)

    def test_stage_tasks_in_today_without_plan(self):
        """阶段任务直接按当前里程碑算进今日任务，不等早上 6:30 的计划排出“下一步”。"""
        got = self.names(self.ids("today", {}))
        self.assertIn("stage", got)
        self.assertFalse({"blocked", "review", "stage_later", "habit"} & got)  # 被挡着的、复盘还没到时候的、以后阶段的、引用日常的照样不进
        weigh = stages.add_task(self.db, self.m1, "训练前称重", "daily")
        self.assertIn(weigh, self.ids("today", {}))
        taskrules.complete(self.db, weigh, D)
        rows = {t["id"]: t for t in taskrules.rows(self.db, "today", D, {})}
        self.assertTrue(rows[weigh]["done_now"])  # 今天做过的留在“已完成”里

    def test_stage_view_marks_waiting_hides_dropped(self):
        """副本里程碑详情（stages.view）以前不看“说算了”、在等的也不标（审计 ③b）。"""
        wait = stages.add_task(self.db, self.m1, "等出签结果")
        gone = stages.add_task(self.db, self.m1, "办 NI 号")
        taskrules.drop(self.db, gone)
        ts = {t["id"]: t for m in stages.view(self.db, self.g_active, D) for t in m["tasks"]}
        self.assertEqual(ts[wait]["label"], "在等")
        self.assertNotIn(gone, ts)

    def test_star_overrides_only_paused_and_waiting(self):
        for k in ("goal_paused", "waiting", "not_started", "dropped"):
            taskrules.star(self.db, self.T[k], D, True)
        got = self.names(self.ids("today"))
        self.assertTrue({"goal_paused", "waiting"} <= got)
        self.assertFalse({"not_started", "dropped"} & got)

    def test_unstar_hides_today_only(self):
        taskrules.star(self.db, self.T["open"], D, False)
        self.assertNotIn(self.T["open"], self.ids("today"))
        self.assertIn(self.T["open"], self.ids("other"))  # 移出今日任务，别处照列（不是推到明天）

    def test_views_agree(self):
        today, other = self.ids("today"), self.ids("other")
        self.assertFalse(today & other)
        self.assertEqual(today | other, self.ids("plan_panel"))
        self.assertEqual(self.ids("plan_panel"), self.ids("tg_list"))
        for view in taskrules.VIEWS:
            if view != "dropped":
                self.assertNotIn(self.T["dropped"], self.ids(view), view)  # 说了算了的只在“你说算了的”里
            self.assertNotIn(self.T["agent"], self.ids(view), view)  # 交给 Codex 的不进她的任何列表

    def test_done_today_stays_in_today(self):
        taskrules.complete(self.db, self.T["open"], D)
        rows = {t["id"]: t for t in taskrules.rows(self.db, "today", D, self.plan)}
        self.assertTrue(rows[self.T["open"]]["done_now"])
        self.assertNotIn(self.T["open"], self.ids("other"))
        self.assertNotIn(self.T["open"], taskrules.view_ids(self.db, "today", D + timedelta(days=1), {}))

    def test_tomorrow_postponed_comes_back(self):
        self.assertIn(self.T["postponed"], taskrules.view_ids(self.db, "today", D + timedelta(days=1), {}))

    def test_quest_steps(self):
        steps = taskrules.quest_steps(self.db, D)
        self.assertNotIn(self.g_paused, steps)
        self.assertNotIn(self.g_agent, steps)
        self.assertIn(self.g_agent, taskrules.quest_steps(self.db, D, with_agent_goals=True))
        mine = [t["id"] for t in steps[self.g_active]]
        self.assertNotIn(self.T["stage_later"], mine)
        self.assertNotIn(self.T["habit"], mine)


class WriteTest(Fixture):
    def test_repeating_stage_task_logged_not_closed(self):
        """Telegram 勾“训练前称重”这类每天做的，以前整件勾死、按 40 分算（审计 2.2）；统一入口按阶段任务记一笔、10 分。"""
        weigh = stages.add_task(self.db, self.m1, "训练前称重", "daily")
        gain = taskrules.complete(self.db, weigh, D)
        self.assertEqual(gain, stages.REPEAT_POINTS)
        self.assertEqual(self.db.conn.execute("SELECT done FROM tasks WHERE id = ?", (weigh,)).fetchone()[0], 0)
        self.assertIn(weigh, taskrules.view_ids(self.db, "today", D, {"items": [{"kind": "todo", "task_id": weigh, "priority": "main", "how": "下一步"}]}))
        self.assertEqual(taskrules.uncomplete(self.db, weigh, D), stages.REPEAT_POINTS)

    def test_complete_syncs_status(self):
        taskrules.complete(self.db, self.T["open"], D)
        self.assertEqual(self.db.conn.execute("SELECT status FROM tasks WHERE id = ?", (self.T["open"],)).fetchone()[0], "done")
        taskrules.uncomplete(self.db, self.T["open"], D)
        self.assertEqual(self.db.conn.execute("SELECT status FROM tasks WHERE id = ?", (self.T["open"],)).fetchone()[0], "open")

    def test_delete_locks_and_restore_releases(self):
        row = taskrules.delete(self.db, self.T["other"], D)
        self.assertTrue(locks.she_deleted(self.db, "约牙医"))
        self.assertEqual(taskrules.already(self.db, "约牙医")["why"], "deleted")
        taskrules.restore_row(self.db, row)
        self.assertFalse(locks.she_deleted(self.db, "约牙医"))

    def test_postpone_and_undo(self):
        old = taskrules.postpone(self.db, self.T["open"], D)
        self.assertNotIn(self.T["open"], self.ids("today") | self.ids("other") | self.ids("plan_panel"))
        taskrules.set_fields(self.db, self.T["open"], **old)
        self.assertIn(self.T["open"], self.ids("today"))

    def test_drop_and_restore(self):
        taskrules.drop(self.db, self.T["other"])
        self.assertIn(self.T["other"], self.ids("dropped"))
        self.assertNotIn(self.T["other"], self.ids("other"))
        taskrules.restore(self.db, self.T["other"])
        self.assertIn(self.T["other"], self.ids("other"))

    def test_recent_done_is_seven_days(self):
        """查重“最近做完的”统一 7 天（每月固定的事用日常周常管）。"""
        old = self.db.add_task("交房租")
        self.db.conn.execute("UPDATE tasks SET done = 1, done_at = ? WHERE id = ?",
                             ((date.today() - timedelta(days=8)).isoformat() + "T10:00:00+00:00", old))
        self.db.conn.execute("DELETE FROM tasks WHERE id = ?", (self.T["done"],))  # 夹具里那件“交房租”是 10-01 做完的
        self.assertIsNone(taskrules.already(self.db, "交房租"))
        self.db.conn.execute("UPDATE tasks SET done_at = ? WHERE id = ?", ((date.today() - timedelta(days=3)).isoformat() + "T10:00:00+00:00", old))
        self.assertEqual(taskrules.already(self.db, "交房租")["why"], "done")

    def test_already_knows_open_renamed_done(self):
        self.assertEqual(taskrules.already(self.db, "约牙医")["id"], self.T["other"])
        locks.lock(self.db, "task", self.T["nodue"], "title", {"title": "整理旅行照片"}, title="清理手机相册")
        self.assertEqual(taskrules.already(self.db, "清理手机相册"), {"why": "renamed", "id": self.T["nodue"]})
        taskrules.complete(self.db, self.T["open"], D)
        self.assertEqual(taskrules.already(self.db, "买运动袜")["why"], "done")
        self.assertIsNone(taskrules.already(self.db, "学做蛋糕"))
        self.assertIsNone(taskrules.already(self.db, "交行信用卡 1234.56"))
        self.db.add_task("交行账单 1,234.56 元")
        self.assertIsNotNone(taskrules.already(self.db, "交行信用卡 1234.56", money=True))


class EntryPointsTest(Fixture):
    """第 3 步：Telegram 清单和早报按钮、抽取、小伴 的标记都走 taskrules。"""

    def test_telegram_list_same_as_home(self):
        from ethan import todo
        from unittest import mock
        with mock.patch("ethan.taskrules.her_today", return_value=D):
            text, kb = todo.render(self.db)
        ids = {int(b["callback_data"].split(":")[3]) for row in kb for b in row if b["callback_data"].startswith("td:done:t:")}
        mine = ids - {self.T["ethan"]}
        self.assertEqual(mine, set(taskrules.view_ids(self.db, "tg_list", D, None)))
        self.assertIn(self.T["ethan"], ids)  # 小伴 答应你的照样一栏
        for k in ("waiting", "postponed", "dropped", "agent", "goal_paused", "far"):
            self.assertNotIn(self.T[k], ids, k)

    def test_brief_buttons_are_today(self):
        from ethan import brief_card
        taps = {int(b["callback_data"].split(":")[2]) for row in brief_card.keyboard(self.db, D) for b in row
                if b["callback_data"].startswith("qs:t:")}
        self.assertEqual(taps, set(taskrules.view_ids(self.db, "tg_buttons", D)))

    def test_extract_drop_goes_through_rules(self):
        from ethan import extract, transitions
        extract.apply(self.db, {"todos": [{"op": "drop", "match_id": self.T["other"]}]}, 0, "chat:x", D)
        self.assertEqual(taskrules.get(self.db, self.T["other"], D)["state"], "dropped")
        self.assertEqual(transitions.history(self.db, "todo", self.T["other"])[-1]["to_state"], "dropped")

    def test_extract_skips_recently_done(self):
        from ethan import extract
        taskrules.complete(self.db, self.T["open"], D)
        got = extract.apply(self.db, {"todos": [{"op": "add", "title": "买运动袜"}]}, 0, "chat:x", D)
        self.assertEqual(got["todo_add"], 0)


class ConsumersTest(Fixture):
    """第 4 步：计划器、帮办、小伴 提示词、导出都按同一张表。"""

    def test_ethan_context_same_as_home_with_labels(self):
        ctx = self.store._tasks(D)
        got = {t["id"]: t.get("state") for t in ctx["open_tasks"]}
        self.assertEqual(got[self.T["waiting"]], "在等")
        self.assertEqual(got[self.T["postponed"]], "推到明天")
        self.assertEqual(got[self.T["ethan"]], "你答应的")
        for k in ("dropped", "agent", "goal_paused", "stage_later", "habit"):
            self.assertNotIn(self.T[k], got, k)
        self.assertTrue(all(t["due"] <= D.isoformat() for t in ctx["due_tasks"]))

    def test_helpscope_and_planner(self):
        from ethan import helpscope, planner
        self.assertEqual({t["id"] for t in helpscope.candidates(self.db, D)}, set(taskrules.view_ids(self.db, "helpscope", D)))
        rest = planner.rest_items(self.db, [], D)
        ids = {r["task_id"] for r in rest}
        for k in ("waiting", "dropped", "postponed", "goal_paused", "agent", "far", "not_started"):
            self.assertNotIn(self.T[k], ids, k)
        self.assertIn(self.T["ethan"], ids)
        nxt = {r["task_id"] for r in rest if r.get("how") == "下一步"}
        self.assertEqual(nxt, {self.T["stage"]})
        facts = planner.quest_facts(self.db, D)
        self.assertNotIn("办 NI 号", facts["side"])


class BackfillTest(Fixture):
    def test_migration_aligns_status_owner_and_old_postpone(self):
        import importlib
        from unittest import mock
        from ethan import transitions
        m = importlib.import_module("migrations.20261106_taskrules_backfill")
        self.db.conn.execute("UPDATE tasks SET status = 'open' WHERE id = ?", (self.T["done"],))
        self.db.conn.execute("UPDATE tasks SET owner = NULL WHERE id = ?", (self.T["ethan"],))
        old = self.db.add_task("回导师邮件 2")
        self.db.conn.execute("UPDATE tasks SET hide_day = ? WHERE id = ?", (D.isoformat(), old))
        transitions.record(self.db, "todo", old, "open", "postponed", {})
        self.db.conn.commit()
        with mock.patch.object(m, "her_today", return_value=D):
            msg = m.run(self.store)
            self.assertIn("今天推到明天的补标 1 件", msg)
        row = lambda i: self.db.conn.execute("SELECT status, owner, postpone_day FROM tasks WHERE id = ?", (i,)).fetchone()
        self.assertEqual(row(self.T["done"])["status"], "done")
        self.assertEqual(row(self.T["ethan"])["owner"], "ethan")
        self.assertEqual(row(self.T["dropped"])["status"], "drop_asked")  # 说算了的不动
        self.assertEqual(row(old)["postpone_day"], D.isoformat())
        self.assertIn("补标 0 件", m.run(self.store))  # 再跑不重复


class RulesTableTest(unittest.TestCase):
    def test_every_rule_used_by_a_view_has_a_reason(self):
        used = {r for v in taskrules.VIEWS.values() for r in v.get("rules", ())}
        self.assertEqual(used - set(taskrules.RULES), set())

    def test_no_sqlite_now(self):
        src = (ROOT / "taskrules.py").read_text(encoding="utf-8")
        self.assertNotIn("date('now'", src)  # 日期只从 clock 取（system-rules 第 5 节）


# ---------- 守卫：别的模块不许自己查“没做完的待办”、自己改完成 / 状态 / 推迟 / 星标（仿 test_memusers） ----------
READ = re.compile(r"\b(?:FROM|JOIN)\s+tasks\b(?:(?!\bdef\b|\n\n).){0,300}?\b(?:t\.)?done\s*=\s*0", re.S)
WRITE = re.compile(r"UPDATE\s+tasks\s+SET\s+((?:(?!WHERE)[^\"])*?)\b(done|status|hide_day|star_day|postpone_day)\s*=")  # 只看 SET 那段
ONE = re.compile(r"\bid\s*=\s*\?|title\s*(?:=|LIKE)\s*\?")  # 按编号 / 名字查一件在不在，不是列清单
OWNERS = {"taskrules.py", "memory/db.py"}  # 规矩模块和数据库层
# 不走 taskrules 的，每条写原因：(列清单的查询数, 改状态的写入数, 原因)。只减不增；新加的要么改走 taskrules，要么在这里写原因
ALLOWED = {
    # 建任务时写来源和初始状态（status = 'open'），不是改她的待办状态
    "desk.py": (0, 1, "邮件台“记成待办”：她亲手点的，新建时写来源"),
    "extract.py": (0, 1, "聊天抽取新建待办时写 owner / 来源 / 初始状态"),
    "grokexport.py": (0, 1, "Grok 导出提醒新建时写来源"),
    "mail.py": (0, 1, "邮件新建待办时写来源"),
    "publicsite.py": (0, 1, "主页留言新建待办时写来源"),
    "lifebase.py": (0, 1, "新列补默认值（status 跟 done 对齐），不改已有值"),
    # 完成 / 撤销的底层：只被 taskrules.complete / uncomplete 调
    "quests.py": (0, 1, "uncomplete_task：普通待办撤销完成的底层，taskrules.uncomplete 调"),
    "stages.py": (0, 1, "uncheck：阶段任务撤销的底层，taskrules.uncomplete 调"),
    # 小伴 自己的活（promised_by_ethan = 1）：准备队列、过期进回收站、周报里“小伴 答应还没做的”、首页长期工作
    "prep.py": (2, 0, "小伴 自己的准备队列和过期回收，不是她的列表"),
    "helpscope.py": (1, 0, "数 小伴 同时在准备几件（help_for），不是她的列表"),
    "report.py": (1, 0, "周报里“小伴 答应还没做的”"),
    "workbench/api.py": (1, 0, "首页“小伴 在准备”里的长期工作（promised_by_ethan = 1）"),
    # 检查、导入、历史
    "invariants.py": (2, 0, "巡检：她删过 / 改过名的有没有被建回来，要看全部没做完的"),
    "onto_query.py": (1, 0, "ontology：小伴 说过要做的有没有落成待办，按标题对，要看全部"),
    "autodone.py": (1, 0, "自动勾完成（ENABLED=False，只撤回）：按标题找候选，不是给人看的列表"),
    "goals_import.py": (1, 0, "导入目标草稿时按同名去重"),
    "season1.py": (3, 2, "赛季 1 开季的一次性脚本（10-29），已执行完"),
    "season1_fix.py": (1, 0, "赛季 1 修数据的一次性脚本（11-01），已执行完"),
}


def scan() -> dict:
    out = {}
    for p in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        name = str(p.relative_to(ROOT))
        text = p.read_text(encoding="utf-8")
        r = sum(1 for m in READ.finditer(text) if not ONE.search(m.group(0)))
        w = len(WRITE.findall(text))
        if (r or w) and name not in OWNERS:
            out[name] = (r, w)
    return out


class GuardTest(unittest.TestCase):
    def test_only_owners_read_open_tasks(self):
        over = {f: n for f, n in scan().items() if f not in ALLOWED or n[0] > ALLOWED[f][0] or n[1] > ALLOWED[f][1]}
        self.assertEqual(over, {}, "这些模块自己查没做完的待办或改状态：改走 ethan/taskrules.py 的 rows() / 写入口")

    def test_allowed_only_shrinks(self):
        now = scan()
        here = {f for f in ALLOWED if any(ROOT.rglob(f))}  # 公开版里不导出的文件（比如 season1_fix.py）不算
        stale = {f: (now.get(f, (0, 0)), a[:2]) for f, a in ALLOWED.items() if f in here and now.get(f, (0, 0)) != a[:2]}
        self.assertTrue(all(a[2] and a[2] != "TODO" for a in ALLOWED.values()), "白名单每条都要写原因")
        self.assertEqual(stale, {}, "改走 taskrules 以后把白名单里的数也改小（只减不增）")


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
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
