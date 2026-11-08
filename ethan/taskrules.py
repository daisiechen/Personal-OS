"""任务规矩：所有层级（领域 → 副本 → 里程碑 → 阶段任务 / 待办）共用的一套（docs/design/tasks-and-schedule.md 第 8 节）。

“要用模块化的方式去做……也要注意每一个地方在调用模块的时候有没有自己单独的规则”。

这里是唯一判断“一件事现在是什么状态、该出现在哪”的地方：
- state()：一件事现在是什么状态（按 STATES 的先后取第一个命中的）；“今天”只从 ethan/clock.py 来。
- VIEWS：每个地方（今日任务、其他、副本详情、计划面板、Telegram、小伴 看的单子、荒原、帮办、计划器、导出）收哪几种状态，
  还有它自己特殊的规则——每条都有名字和原因（RULES），页面和别的模块里不许再偷偷加条件。
- rows(db, 视图, day)：视图唯一的取数口，每行带 state 和给人看的标签（“在等”“11/12 起”）。
- 写入口：complete / uncomplete / delete / postpone / star / drop / restore / set_fields；查重 already()。
别的模块自己写“FROM tasks … done = 0”这类查询，tests/test_taskrules.py 的守卫不过。
"""

import re
from datetime import date, datetime, timedelta, timezone

from .clock import her_today

SOON_DAYS = 2      # 今日任务看截止在 2 天内的（原来 5 天）
MONTH_DAYS = 30    # “其他”和计划页看 30 天内的（约年度体检是明年的事；11-06 统一）
HELP_DAYS = 7      # 帮办看未来 7 天（quest-structure.md 第 6 节）
RECENT_DONE_DAYS = 7  # 查重时“最近做完的”也算同一件（原来只有邮件这样、45 天；统一 7 天，每月固定的事用日常周常管）
MAIN_BOSS = "减脂"  # 本赛季主副本，今日任务优先排它
AGENTS = {"claude", "codex", "grok"}  # 交给它们的事不进她的待办（第 6 条），在团队页看
GOAL_LATER = {"paused", "done", "not_started"}  # 这些副本下剩的事按暂停处理（第 5、8 条）
# 等别人结果的步骤；原来在 workbench/api.py
WAITING = re.compile(r"[（(]等[^）)]*[）)]|^等|等(结果|通知|回复|审批|审核|出签|消息|对方)")
# 标题写的是状态、不是要做的事（为什么排进今日任务、分数还这么高）：也算在等
STATUS_NOTE = re.compile(r"已取消|暂不|先不|定了再|再说|有了再|到时再|之后再")
# 到那天才发生的事（开会、见面、面试、饭局）：提前几天不进今日任务，只在“其他”里标着日期，当天才列（
# “我跟 小伴 说，我明天要跟合伙人开会，他为什么要把这个代办放到我今天的日历里呢？”）。要提前做的（准备、约、订、确认）照旧
APPOINTMENT = re.compile(r"开会|会议|见面|面试|聚餐|饭局|吃饭|约饭|电话会|视频会|meeting|call\b", re.I)
PREP_WORK = re.compile(r"准备|整理|写|发|约|订|确认|预约|安排|改|查|做|交")


def is_appointment(title: str) -> bool:
    return bool(APPOINTMENT.search(title or "")) and not PREP_WORK.search(title or "")


TODAY_ORDER = "CREATE TABLE IF NOT EXISTS today_order (day TEXT NOT NULL, task_id INTEGER NOT NULL, pos INTEGER NOT NULL, PRIMARY KEY (day, task_id))"

# 一件事“现在”的状态，按先后取第一个命中的
STATES = (
    "done",          # 做完了（反复做的阶段任务不会整件做完，看 today_done）
    "not_mine",      # 小伴 答应的 / 交给 Claude、Codex、Grok 的
    "dropped",       # 她在聊天里说了“算了 / 不做了”（抽取标成 drop_asked）
    "goal_dropped",  # 上层副本放弃了
    "agent_goal",    # 交给 agent、还没拆里程碑的副本的主线：是 agent 的活
    "stage_later",   # 不在当前里程碑（以后的阶段，或结束了的阶段剩下的）
    "habit_ref",     # 引用日常周常：在日常里打卡
    "goal_later",    # 上层副本暂停 / 达成 / 还没开始
    "blocked",       # 前置条件没做完
    "not_started",   # 还没到开始做的那天（start_day）
    "postponed",     # 今天推到明天了
    "waiting",       # 等别人结果
    "stage_rest",    # 阶段任务今天不用做（今天做过了、这周够了、复盘还没到时机）
    "open",          # 现在就能做
)
LABEL = {"waiting": "在等", "postponed": "推到明天", "blocked": "等前置", "goal_later": "副本暂停",
         "stage_rest": "今天不用做", "dropped": "你说算了", "not_mine": "小伴 答应的"}

# 每个调用的地方自己的特殊规则：有名字、有原因（tasks-and-schedule.md 第 8 节“规则表”）
RULES = {
    "no_territory": "待归属（代码名 waste，原叫荒原）= 领地视图里还没归到领域的那几件；导航红点、赛季页和大地图上“待归属”按钮的数、待归属页都只从这一个视图取（红点说荒原有，点进去又没有；同日改叫待归属，不当第八块领域）",
    "star_override": "她点了 ★ 的照样进今天：暂停副本的、在等的也算（11-06）",
    "hidden_today": "她今天把它移出今日任务（取消 ★）：今天不再进今日任务，别处照列",
    "keep_done_today": "今天做完的留在今日任务底下“已完成”里，第二天才走",
    "plus_soon": "截止在 2 天内的进今天",
    "plus_mail": "邮件、主页留言来的待处理事项进今天",
    "source_ignored": "来源邮件 / 留言她点了“不用管”：从它来的没做的待办按“她说算了”处理，能在“你说算了的”里恢复",
    "plus_stage": "当前里程碑里今天该做的阶段任务直接进今天，不等早上 6:30 的计划",
    "plus_planned": "早上计划里老结构副本的“下一步”和计划器当天新建的进今天（计划器的“下一步”另外只用来排时间块）",
    "side_only": "“其他”只列不在副本主线里的事；主线在副本详情里看",
    "minus_today": "已经排进今日任务的不在“其他”里重复列",
    "month_window": "截止在 30 天以后的先不列（11-06）",
    "label": "列出来的标上状态（在等、11/12 起、推到明天）",
    "help_window": "帮办只挑未来 7 天要做的和当前里程碑的一次性任务",
    "help_no_review": "复盘要她自己做，帮办不挑",
    "help_no_ops": "给开发会话的权限、放行、配置这类系统运维事务不挑（prep.is_ops）",
    "mail_money": "邮件：金额对得上也算同一件（账单每期标题不一样，11-01 交行账单早还了又建一条）",
    "agent_main_ok": "副本详情里交给 agent 的副本照样列它的主线（看进度）",
    "tg_manual_order": "Telegram 清单里她用 ⬆ 挪过的（tasks.sort）按她排的在前，其余今日任务在前、其他在后（能挪）",
    "skipped_paused": "计划面板“今天没排进”里，暂停、放弃副本下的和计划器写了“这个赛季不做”的不显示",
}

# 视图表：收哪些状态、★ 能破例哪些、从哪些来源挑、用了哪些特殊规则（第 8 节对照表的“应该”）
_HERS = {"open"}
VIEWS = {
    "today":        {"states": {"open"}, "star_ok": {"goal_later", "waiting"},
                     "rules": ("plus_soon", "plus_mail", "plus_stage", "plus_planned", "star_override", "hidden_today", "keep_done_today")},
    "other":        {"states": {"open"}, "rules": ("side_only", "month_window", "minus_today")},
    "later":        {"states": {"goal_later"}, "rules": ()},
    "dropped":      {"states": {"dropped"}, "rules": ()},
    "quest_detail": {"states": set(STATES) - {"dropped", "not_mine"}, "rules": ("label", "agent_main_ok")},
    "plan_panel":   {"union": ("today", "other"), "rules": ("skipped_paused",)},
    "tg_list":      {"union": ("today", "other"), "rules": ("tg_manual_order",)},
    "tg_buttons":   {"union": ("today",), "rules": ()},
    "ethan_ctx":    {"union": ("today", "other"), "states": {"waiting", "postponed", "not_started"},
                     "rules": ("month_window", "label")},
    "territory":    {"states": {"open", "stage_rest", "blocked", "not_started", "postponed", "waiting"}, "rules": ()},
    "waste":        {"union": ("territory",), "rules": ("no_territory",)},
    "helpscope":    {"states": {"open", "not_started"}, "rules": ("help_window", "help_no_review", "help_no_ops")},
    "planner_in":   {"states": {"open", "stage_rest", "blocked"}, "rules": ("month_window",)},
    "export":       {"states": set(STATES) - {"done", "dropped", "not_mine", "goal_dropped"}, "rules": ("label",)},
}


# ---------- 读 ----------
def ensure(db):
    """建好要用的列：阶段任务的列（stages）、谁做 / 状态 / 来源（lifebase 也会加）、推到明天那天（postpone_day，11-06 加）。"""
    from . import stages
    stages.ensure(db)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(tasks)")}
    for col in ("owner", "status", "source_type", "postpone_day", "help_for"):
        if col not in have:
            db.conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {'INTEGER' if col == 'help_for' else 'TEXT'}")
    db.conn.execute(TODAY_ORDER)


def who(t: dict) -> str:
    """谁做：mia / ethan / agent。owner 优先，没有再看 promised_by_ethan（两个字段并存期间都读）。"""
    owner = (t.get("owner") or "").lower()
    if owner in AGENTS:
        return "agent"
    if t.get("promised_by_ethan") or owner == "ethan":
        return "ethan"
    return "mia"


def when(t: dict, day: date) -> str:
    """截止在哪个窗口：overdue / today / soon（2 天内）/ month（30 天内）/ far / none。"""
    due = t.get("due")
    if not due:
        return "none"
    d = day.isoformat()
    if due < d:
        return "overdue"
    if due == d:
        return "today"
    if due <= (day + timedelta(days=SOON_DAYS)).isoformat() and not is_appointment(t.get("title")):
        return "soon"
    if due <= (day + timedelta(days=MONTH_DAYS)).isoformat():
        return "month"
    return "far"


def starred(t: dict, day: date) -> bool:
    """点了 ★、到今天还算：从点星那天起一直有效，直到做完或取消。"""
    return bool(t.get("star_day")) and t["star_day"] <= day.isoformat()


def state(t: dict, day: date) -> str:
    """一件事现在的状态。t 要是 facts() 出来的行（带 goal_status、current、blocked、due_now 这几列）。"""
    d = day.isoformat()
    if t.get("done"):
        return "done"
    if who(t) != "mia":
        return "not_mine"
    if (t.get("status") or "") == "drop_asked":
        return "dropped"
    gs = t.get("goal_status") or "active"
    if t.get("goal_id") and gs == "dropped":
        return "goal_dropped"
    if t.get("goal_handled_by") and t.get("quest") == "main" and not t.get("milestone_id") and not t.get("goal_staged"):
        return "agent_goal"
    if t.get("milestone_id") and not t.get("current"):
        return "stage_later"
    if t.get("habit_id"):
        return "habit_ref"
    if t.get("goal_id") and gs in GOAL_LATER:
        return "goal_later"
    if t.get("blocked"):
        return "blocked"
    if t.get("start_day") and t["start_day"] > d:
        return "not_started"
    if t.get("postpone_day") == d:
        return "postponed"
    if WAITING.search(t.get("title") or "") or STATUS_NOTE.search(t.get("title") or ""):
        return "waiting"
    if t.get("milestone_id") and not t.get("due_now"):
        return "stage_rest"
    return "open"


def label(t: dict) -> str:
    st = t.get("state")
    if st == "not_started":
        return f"{t['start_day'][5:].replace('-', '/')} 起"
    return LABEL.get(st, "")


def _cache_key(db, day: date):
    try:
        ver = db.conn.execute("PRAGMA data_version").fetchone()[0]
    except Exception:
        ver = None
    return (day.isoformat(), db.conn.total_changes, ver)


def facts(db, day: date = None) -> list:
    """一次读出所有待办，每行带上：副本状态、是不是当前里程碑、被挡着没有、今天该不该做、今天做了没有、谁做、
    状态（state）、截止窗口（when）、★、领域（territory）、标签。同一天数据没变时用上一次算好的（首页一次请求要用好几遍）。"""
    from . import stages, territory
    day = day or her_today()
    key = _cache_key(db, day)
    cached = getattr(db, "_taskrules_cache", None)
    if cached and cached[0] == key:
        return [dict(t) for t in cached[1]]
    ensure(db)
    d = day.isoformat()
    current = {r[0] for r in db.conn.execute(
        "SELECT m.id FROM milestones m WHERE m.status = 'open' AND m.seq = (SELECT MIN(seq) FROM milestones x "
        "WHERE x.goal_id = m.goal_id AND x.status = 'open')")}
    staged = {r[0] for r in db.conn.execute("SELECT DISTINCT goal_id FROM milestones")}
    paid = {r[0] for r in db.conn.execute("SELECT ref FROM points WHERE day = ? AND ref LIKE 'task:%'", (d,))}
    out = []
    for r in db.conn.execute(
            "SELECT t.*, g.title AS goal, g.status AS goal_status, g.domain AS goal_domain, g.handled_by AS goal_handled_by, "
            "g.sort AS goal_sort FROM tasks t LEFT JOIN goals g ON g.id = t.goal_id "
            "ORDER BY t.sort IS NULL, t.sort, t.due IS NULL, t.due, t.id"):
        t = dict(r)
        t["who"] = who(t)
        t["goal_staged"] = t.get("goal_id") in staged
        t["current"] = t.get("milestone_id") in current
        stage = bool(t.get("milestone_id"))
        repeat = stage and (t.get("cadence") or "once") != "once"
        if stage and not t["done"] and t["current"]:
            t["blocked"] = stages.blocked(db, t)
            t["due_now"] = stages.due_today(db, t, day)
        if repeat:
            t["today_done"] = bool(t["current"]) and stages.done_on(db, t, day)
        else:
            t["today_done"] = bool(t["done"]) and ((t.get("done_at") or "")[:10] >= d or f"task:{t['id']}" in paid)
        t["state"] = state(t, day)
        t["when"] = when(t, day)
        t["starred"] = starred(t, day)
        t["territory"] = territory.task_territory(t)
        t["label"] = label(t)
        out.append(t)
    db._taskrules_cache = (key, out)
    return [dict(t) for t in out]


def get(db, task_id: int, day: date = None):
    return next((t for t in facts(db, day) if t["id"] == int(task_id)), None)


def _planned(plan) -> set:
    """早上计划里算“今天”的：每个副本的下一步、计划器今天新建的（小伴 自己的不算）。"""
    return {i["task_id"] for i in (plan or {}).get("items", [])
            if i.get("kind") == "todo" and i.get("task_id") and i.get("priority") != "ethan"
            and ((i.get("priority") == "main" and i.get("how") == "下一步") or i.get("task_created"))}


DESK_SOURCES = ("email", "visitor")  # 邮件、主页留言记成的待办：首页“邮件和留言”管着，不进荒原等归类


def _today(ts: list, day: date, plan, db) -> list:
    planned = _planned(plan)
    out = []
    for t in ts:
        if t["who"] != "mia" or t["state"] in ("dropped", "goal_dropped", "agent_goal", "habit_ref", "stage_later"):
            continue
        picked = (t["starred"] or t["when"] in ("overdue", "today", "soon")
                  or (t.get("source_type") in ("email", "visitor")) or t["id"] in planned
                  or (t.get("milestone_id") and t["current"] and (t.get("goal_status") or "active") == "active"
                      and (t["state"] == "open" or t["today_done"])))  # plus_stage
        if not picked:
            continue
        if t.get("hide_day") == day.isoformat() and not t["starred"]:  # hidden_today
            continue
        ok = t["state"] == "open" or t["today_done"] or (t["starred"] and t["state"] in VIEWS["today"]["star_ok"])
        if t["state"] == "done" and not t["today_done"]:
            ok = False
        if ok:
            out.append(t)
    return order_today(db, out, day)


def order_today(db, ts: list, day: date) -> list:
    """没做完的在前；她拖过的按她排的，没拖过的：★ 的、主副本的主线、别的主线、其他。"""
    db.conn.execute(TODAY_ORDER)
    pos = {r[0]: r[1] for r in db.conn.execute("SELECT task_id, pos FROM today_order WHERE day = ?", (day.isoformat(),))}

    def rank(t):
        done = bool(t["today_done"] or t["state"] == "done")
        return (done, t["id"] not in pos, pos.get(t["id"], 0), not t["starred"],
                not (t.get("quest") == "main" and MAIN_BOSS in (t.get("goal") or "")), t.get("quest") != "main")
    return sorted(ts, key=rank)


def _in_month(t: dict) -> bool:
    return t["when"] != "far"


def rows(db, view: str, day: date = None, plan="auto") -> list:
    """视图唯一的取数口。plan：今日任务要用早上的计划（“下一步”），不传就读今天最新的一版。每行带 state、label、done_now（今天算做了）。"""
    day = day or her_today()
    if view not in VIEWS:
        raise ValueError(f"没有这个视图：{view}")
    if plan == "auto":
        plan = db.latest_plan(day.isoformat()) if hasattr(db, "latest_plan") else None
    ts = facts(db, day)
    for t in ts:
        t["done_now"] = bool(t["today_done"] or t["state"] == "done")
    spec = VIEWS[view]
    if view == "today":
        return _today(ts, day, plan, db)
    if view == "other":
        today = {t["id"] for t in _today(ts, day, plan, db)}
        return [t for t in ts if t["who"] == "mia" and t["state"] == "open" and t.get("quest") != "main"
                and _in_month(t) and t["id"] not in today]
    if view == "waste":  # no_territory；邮件、主页留言来的待办不算（它们在“邮件和留言”里管，邮件和留言的红点为什么会同时在赛季上面出现？）
        return [t for t in rows(db, "territory", day, plan) if not t["territory"] and t.get("source_type") not in DESK_SOURCES]
    if "union" in spec:
        seen, out = set(), []
        for part in spec["union"]:
            for t in rows(db, part, day, plan):
                if t["id"] not in seen:
                    seen.add(t["id"])
                    out.append(t)
        if "tg_manual_order" in spec["rules"]:
            out.sort(key=lambda t: (t.get("sort") is None, t.get("sort") or 0))
        if spec.get("states"):  # 小伴 看的单子：另外标出在等、推了的、还没到开始那天的（第 7 条）
            out += [t for t in ts if t["who"] == "mia" and t["state"] in spec["states"] and _in_month(t) and t["id"] not in seen]
        return out
    if view == "later":
        out = [t for t in ts if t["who"] == "mia" and t["state"] == "goal_later"]
        return sorted(out, key=lambda t: (t.get("goal_sort") is None, t.get("goal_sort") or 0, t.get("goal_id") or 0,
                                          t.get("seq") or 0, t["id"]))
    if view == "helpscope":
        end = (day + timedelta(days=HELP_DAYS)).isoformat()
        from . import prep
        out = []
        for t in ts:
            if t["who"] != "mia" or t["state"] not in spec["states"] or t.get("is_review") or prep.is_ops(t["title"]):
                continue
            if t["state"] == "not_started" and t["start_day"] > end:
                continue
            soon = t.get("due") and t["due"] <= end
            stage_now = t.get("milestone_id") and (t.get("cadence") or "once") == "once"
            if soon or stage_now:
                out.append(t)
        return out
    if view == "planner_in":
        return [t for t in ts if t["who"] == "mia" and t["state"] in spec["states"]
                and (t.get("quest") == "main" or _in_month(t))]
    if view == "quest_detail":
        return [t for t in ts if t["state"] in spec["states"]]
    return [t for t in ts if t["who"] == "mia" and t["state"] in spec["states"]]


def view_ids(db, view: str, day: date = None, plan="auto") -> list:
    return [t["id"] for t in rows(db, view, day, plan)]


def done_that_day(db, day: date) -> list:
    """那天她做完的（第 29 题“某天她做了什么”，11-06）：一次性的看做完在哪天（那天记了分，或完成时间按她的时区落在那天），
    反复做的阶段任务看那天记没记一笔。只算她的（小伴 答应的、交给 agent 的不算）。"""
    from . import stages
    from .clock import LOCAL
    d = day.isoformat()
    paid = {r[0] for r in db.conn.execute("SELECT ref FROM points WHERE day = ? AND ref LIKE 'task:%'", (d,))}

    def local_day(at) -> str:
        try:
            t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return str(at or "")[:10]
        return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(LOCAL).date().isoformat()
    out = []
    for t in facts(db, day):
        if t["who"] != "mia":
            continue
        if t.get("milestone_id") and (t.get("cadence") or "once") != "once":
            if stages.done_on(db, t, day):
                out.append(t)
        elif t["done"] and (f"task:{t['id']}" in paid or local_day(t.get("done_at")) == d):
            out.append(t)
    return out


def ethan_tasks(db, day: date = None) -> list:
    """小伴 自己答应的、还没做完的（给 Telegram 清单的“小伴 答应你的”、计划器的 小伴 一栏）。"""
    return [t for t in facts(db, day) if t["who"] == "ethan" and not t["done"]]


def quest_steps(db, day: date = None, with_agent_goals: bool = False) -> dict:
    """副本的主线：{目标编号: [步骤…]}，副本按她排的顺序，步骤按 seq，含做完的（显示进度用）。只算进行中的副本；
    新结构只看当前里程碑；with_agent_goals=False 时不含交给 agent 的副本、不含引用日常的步骤（计划和今天里不重复出现）。"""
    out = {}
    ts = [t for t in facts(db, day) if t.get("quest") == "main" and t.get("goal_id")
          and (t.get("goal_status") or "active") == "active" and t["state"] not in ("dropped", "not_mine")
          and (with_agent_goals or not t.get("goal_handled_by"))
          and not (t.get("milestone_id") and not t["current"])
          and (with_agent_goals or not t.get("habit_id"))]
    ts.sort(key=lambda t: (t.get("goal_sort") is None, t.get("goal_sort") or 0, t["goal_id"], t.get("seq") or 0, t["id"]))
    for t in ts:
        out.setdefault(t["goal_id"], []).append(t)
    return out


def skipped_shown(db, skipped: list, day: date = None) -> list:
    """计划面板“今天没排进”的那几行：暂停 / 放弃副本下的不显示（规矩 skipped_paused：修好之前排的计划里也可能有）。"""
    from .planner import PAUSED_WHY
    later = {t["title"] for t in facts(db, day) if t["state"] in ("goal_later", "goal_dropped")}
    return [x for x in skipped if x.get("title") not in later and not PAUSED_WHY.search(f"{x.get('title')} {x.get('why') or ''}")]


def counts(db, day: date = None) -> dict:
    """首页“其他”底下那行：说算了的有几件。"""
    return {"dropped": len(rows(db, "dropped", day, None))}


# ---------- 写 ----------
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _has(db, col: str) -> bool:
    return col in {r[1] for r in db.conn.execute("PRAGMA table_info(tasks)")}


def _status(db, task_id: int, value: str):
    if _has(db, "status"):
        db.conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (value, task_id))


def _row(db, task_id: int) -> dict:
    r = db.conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    if r is None:
        raise ValueError("这件事不在了")
    return dict(r)


def complete(db, task_id: int, day: date) -> int:
    """勾完成，唯一的入口（工作台、Telegram 早报按钮和 📋 清单、小伴 的 [结果 #… 完成] 都走这里，结果一样）：
    阶段任务走 stages.check（反复做的记当天一笔、按频次计分、复盘推进里程碑），普通待办走 quests.complete_task；
    一次性的同步 status = done。返回这次加的分；那件已经不在了（按钮过期）返回 0。
    11-06 先在 taskops.py 修过 Telegram / 小伴 绕开 stages.check 的 bug，统一时并进这里。"""
    from . import quests, stages
    ensure(db)
    r = db.conn.execute("SELECT milestone_id, cadence FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    if r is None:
        return 0
    if r["milestone_id"]:
        gain = stages.check(db, int(task_id), day)
    else:
        gain = quests.complete_task(db, int(task_id), day)
    if not r["milestone_id"] or (r["cadence"] or "once") == "once":
        _status(db, int(task_id), "done")
    db.conn.commit()
    return gain


def uncomplete(db, task_id: int, day: date) -> int:
    """撤销完成：阶段任务走 stages.uncheck，其余 quests.uncomplete_task。返回收回的分。"""
    from . import quests, stages
    ensure(db)
    t = _row(db, task_id)
    back = stages.uncheck(db, int(task_id), day) if t.get("milestone_id") else quests.uncomplete_task(db, int(task_id))
    if (t.get("status") or "") == "done":
        _status(db, int(task_id), "open")
    db.conn.commit()
    return back


def remove(db, task_id: int) -> dict:
    """删一件（db.delete_task 会记 deleted_tasks）、记状态变化；返回删之前那一行（撤销用），不在了返回 {}。
    不记锁：调用方负责（工作台的 act 在同一事务里记）。"""
    from . import transitions
    r = db.conn.execute("SELECT * FROM tasks WHERE id = ?", (int(task_id),)).fetchone()
    if r is None:
        return {}
    row = dict(r)
    db.delete_task(int(task_id))
    transitions.record(db, "todo", int(task_id), "done" if row.get("done") else "open", "deleted", {"title": row.get("title")})
    return row


def delete(db, task_id: int, day: date) -> dict:
    """她在工作台以外亲手删一件（Telegram 清单 ✕、“你说算了的”里删）：删和记“她删过”的锁一个事务，和工作台删除一样。"""
    from . import locks, tx
    with tx.atomic(db):
        row = remove(db, task_id)
        if row:
            locks.record_action(db, "task.delete", {"id": int(task_id)}, day, row)
    return row


def restore_row(db, row: dict) -> int:
    """撤销删除：按删之前那一行原样放回，删除的锁放开。"""
    from . import locks
    row = {k: v for k, v in dict(row).items()}
    cols = [c[1] for c in db.conn.execute("PRAGMA table_info(tasks)")]
    row = {k: row[k] for k in cols if k in row}
    db.conn.execute(f"INSERT INTO tasks ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
    if row.get("title"):
        locks.release_deleted(db, row["title"])
    db.conn.commit()
    return row.get("id")


def postpone(db, task_id: int, day: date) -> dict:
    """推到明天：今天哪儿都不列（第 3 条），明天回来。原来截止日比明天晚的不改截止日（11-06：11/8、11/9 的一推就被拉成明天截止）。
    返回撤销要用的旧值。"""
    from . import transitions
    ensure(db)
    old = _row(db, task_id)
    tomorrow = (day + timedelta(days=1)).isoformat()
    keep = old.get("due") and old["due"] > tomorrow
    db.conn.execute("UPDATE tasks SET due = ?, star_day = NULL, hide_day = NULL, postpone_day = ? WHERE id = ?",
                    (old["due"] if keep else tomorrow, day.isoformat(), int(task_id)))
    transitions.record(db, "todo", int(task_id), "open", "postponed", {"due_before": old.get("due")})
    db.conn.commit()
    return {"due": old.get("due"), "star_day": old.get("star_day"), "hide_day": old.get("hide_day"),
            "postpone_day": old.get("postpone_day")}


def star(db, task_id: int, day: date, on: bool):
    """点 ★ = 进今日任务、一直留到做完或取消；取消 = 今天移出今日任务（hidden_today），别处照列。"""
    ensure(db)
    d = day.isoformat()
    if on:
        db.conn.execute("UPDATE tasks SET star_day = ?, hide_day = NULL, postpone_day = NULL WHERE id = ?", (d, int(task_id)))
    else:
        db.conn.execute("UPDATE tasks SET star_day = NULL, hide_day = ? WHERE id = ?", (d, int(task_id)))


def set_fields(db, task_id: int, **fields):
    """撤销推迟、星标时把旧值写回去（只认这几列）。"""
    ensure(db)
    allowed = {k: v for k, v in fields.items() if k in ("due", "star_day", "hide_day", "postpone_day")}
    if allowed:
        db.conn.execute(f"UPDATE tasks SET {', '.join(k + ' = ?' for k in allowed)} WHERE id = ?", (*allowed.values(), int(task_id)))
    db.conn.commit()


def drop(db, task_id: int) -> bool:
    """她在聊天里说了“算了 / 不做了”：只标记不删（到处都不列，“其他”最底下能删或恢复）。"""
    from . import transitions
    ensure(db)
    n = db.conn.execute("UPDATE tasks SET status = 'drop_asked' WHERE id = ? AND done = 0", (int(task_id),)).rowcount
    if n:
        transitions.record(db, "todo", int(task_id), "open", "dropped", {})
    db.conn.commit()
    return n > 0


def drop_from_source(db, refs, task_ids=()) -> int:
    """来源（邮件 / 主页留言 / 新邮件）被她点了“不用管”：从它来的、还没做的待办都按“她说算了”处理（规则 source_ignored）。
    refs 是 source_ref 的几种写法，task_ids 是来源上直接记着的待办编号。返回收起了几件。"""
    ensure(db)
    refs = sorted({str(x) for x in refs if x})
    ids = {int(t) for t in task_ids if t}
    if refs:
        q = ",".join("?" * len(refs))
        ids |= {x[0] for x in db.conn.execute(f"SELECT id FROM tasks WHERE done = 0 AND source_ref IN ({q})", refs)}
    return sum(1 for t in sorted(ids) if drop(db, t))


def restore(db, task_id: int) -> bool:
    """“你说算了的”里点恢复：回到没做。"""
    from . import transitions
    ensure(db)
    n = db.conn.execute("UPDATE tasks SET status = 'open' WHERE id = ? AND status = 'drop_asked'", (int(task_id),)).rowcount
    if n:
        transitions.record(db, "todo", int(task_id), "dropped", "open", {})
    db.conn.commit()
    return n > 0


# ---------- 查重（所有建任务的入口共用） ----------
def already(db, title: str, user: str = "ethan", goal_id=None, money: bool = False):
    """要建的这件是不是已经有了：返回 {"why", "id"} 或 None。认四种（统一，原来 5 种判断各认一部分）：
    - open：和没做完的（含她说算了的、小伴 的）是同一件 → id 是那件
    - renamed：她把那件改了名，这是旧名字 → id 是改过名的那件
    - deleted：她删过同一件（锁，或 ontology 历史里删过）
    - done：最近 7 天做完了同一件
    user：哪个自动流程在问（memusers 登记过的名字，ontology 切换后按它读历史）。money：邮件账单的金额也比（RULES["mail_money"]）。"""
    from . import locks, onto_query
    from .core.agent import find_similar
    title = (title or "").strip()
    if not title:
        return None
    opened = {r["title"]: r["id"] for r in db.conn.execute("SELECT id, title FROM tasks WHERE done = 0 ORDER BY id")}
    same = opened.get(title) and title or find_similar(title, list(opened))
    if same:
        return {"why": "open", "id": opened[same]}
    tid = locks.renamed_from(db, title)
    if tid:
        return {"why": "renamed", "id": tid}
    gone = onto_query.she_removed(db, user, title) if onto_query.reading_new(db, user) else False
    if gone or locks.she_deleted(db, title, goal_id=goal_id):
        return {"why": "deleted", "id": None}
    since = (datetime.now(timezone.utc) - timedelta(days=RECENT_DONE_DAYS)).isoformat()
    done = [r[0] for r in db.conn.execute("SELECT title FROM tasks WHERE done = 1 AND done_at >= ?", (since,))]
    if find_similar(title, done):
        return {"why": "done", "id": None}
    if money:
        nums = {m.replace(",", "") for m in re.findall(r"\d[\d,]*\.\d{2}|\d{4,}", title)}
        if any(m in o.replace(",", "") for m in nums for o in list(opened) + done):
            return {"why": "open", "id": None}
    return None


# ---------- 定档（方案 A：任务按预计工作量定档，所有建任务的入口都在这里定） ----------
# 分数见 quests.SIZE_POINTS（S 40 / M 100 / L 200 / XL 400），节点 +300、硬骨头 ×1.5 照旧；每天 / 每周反复做的每次 +10 不看档。
SIZES = {"S": "一小时内", "M": "半天", "L": "一两天", "XL": "好几天"}
DEFAULT_SIZE = "S"  # 估不出来先按 S，她在工作台能改
NO_HINT = "看不出是大活"  # 估不出来时理由的开头
_QUICK = re.compile(r"^(回复|回一下|预约|约|买|订|打电话|打个电话|电话|发给|发消息|发邮件|问一下|问问|确认|付|缴|交钱|提醒|催|查一下|取|寄|领)")
_SIZE_WORDS = (  # 先看大的：标题里有就按这一档（以“回复 / 预约 / 买 / 打电话”这类开头的不看，见 _QUICK）
    ("XL", re.compile(r"上线|搭建|系统|迁移|重构|开发(?!会话)|改版|从零")),
    ("L", re.compile(r"验收|申请|准备.{0,6}材料|材料|方案|报告|论文|评估|调研|设计")),
    ("M", re.compile(r"写|研究|对比|比较|读完|填表|填写|准备|规划|整理|面试|学习|练习|复习|梳理")),
    ("S", re.compile(r"回复|预约|买|打电话|订|付款|缴费|确认|提醒|催|寄|取件|复盘")),
)


def estimate(title: str, context: str = "") -> tuple:
    """按标题估档，返回 (档, 理由)。context：所属副本 / 里程碑的名字，标题看不出时用来往上提一档到 M。"""
    title = (title or "").strip()
    m = _QUICK.match(title)
    if m:
        return "S", f"以“{m.group(1)}”开头的小事，{SIZES['S']}"
    for size, pat in _SIZE_WORDS:
        m = pat.search(title)
        if m:
            return size, f"标题里有“{m.group(0)}”，按{SIZES[size]}"
    for size, pat in _SIZE_WORDS[:2]:
        m = pat.search(context or "")
        if m:
            return "M", f"所属的「{context[:20]}」是大活（“{m.group(0)}”），这一步先按{SIZES['M']}"
    return DEFAULT_SIZE, f"{NO_HINT}，先按{SIZES[DEFAULT_SIZE]}"


def add(db, title: str, due=None, goal_id=None, promised_by_ethan: bool = False, size: str = None, context: str = "") -> int:
    """建一件待办，唯一的入口（工作台加任务、副本加步骤、阶段任务、计划器、小伴 的标记、邮件、聊天抽取、帮办……都走这里）：
    给了档（计划器、小伴 估的、她选的）就用，没给按标题估（estimate），估不出是 S。返回编号。"""
    from . import tx
    tid = db.add_task(title, due=due, goal_id=goal_id, promised_by_ethan=promised_by_ethan)
    db.conn.execute("UPDATE tasks SET size = ? WHERE id = ?", (size if size in SIZES else estimate(title, context)[0], tid))
    tx.commit(db)
    return tid


def set_size(db, task_id: int, size: str) -> dict:
    """她在工作台改一件的档（锁由工作台的 act 记成 task:编号 size，补估档这类自动流程以后不动）。返回 {"old", "size", "points"}。"""
    from . import quests
    if size not in SIZES:
        raise ValueError("档只能是 S、M、L、XL")
    old = _row(db, task_id)
    db.conn.execute("UPDATE tasks SET size = ? WHERE id = ?", (size, int(task_id)))
    _size_seen(db, [int(task_id)])
    db.conn.commit()
    return {"old": old.get("size"), "size": size, "points": quests.task_points({**old, "size": size})}


# 估档清单：补估档的迁移（migrations/20261106_effort_sizes.py）估过的，等她在工作台“日历 · 改计划”里过目
SIZE_REVIEW = ("CREATE TABLE IF NOT EXISTS size_review (task_id INTEGER PRIMARY KEY, size TEXT NOT NULL, old TEXT, why TEXT, "
               "at TEXT NOT NULL, seen_at TEXT)")


def size_review_add(db, task_id: int, size: str, old, why: str):
    db.conn.execute(SIZE_REVIEW)
    db.conn.execute("INSERT OR REPLACE INTO size_review (task_id, size, old, why, at) VALUES (?, ?, ?, ?, ?)",
                    (int(task_id), size, old, why, _now()))


def size_review(db) -> list:
    """还没过目、还没做完的估档：大的在前。每行带现在的档和分（她改过的显示改后的）。"""
    from . import quests
    db.conn.execute(SIZE_REVIEW)
    order = {k: n for n, k in enumerate(("XL", "L", "M", "S"))}
    out = []
    for r in db.conn.execute("SELECT r.why, r.old, t.* FROM size_review r JOIN tasks t ON t.id = r.task_id "
                             "WHERE r.seen_at IS NULL AND t.done = 0"):
        t = dict(r)
        out.append({"id": t["id"], "title": t["title"], "size": t.get("size") or DEFAULT_SIZE, "old": t["old"], "why": t["why"],
                    "points": quests.task_points(t)})
    return sorted(out, key=lambda x: (order.get(x["size"], 9), x["id"]))


def _size_seen(db, ids=None):
    db.conn.execute(SIZE_REVIEW)
    if ids is None:
        db.conn.execute("UPDATE size_review SET seen_at = ? WHERE seen_at IS NULL", (_now(),))
    else:
        for i in ids:
            db.conn.execute("UPDATE size_review SET seen_at = ? WHERE task_id = ? AND seen_at IS NULL", (_now(), int(i)))


def size_review_ok(db) -> int:
    """她点“都对”：清单收起，档不变。返回收起了几件。"""
    n = len(size_review(db))
    _size_seen(db)
    db.conn.commit()
    return n
