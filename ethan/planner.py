"""每日计划器（设计第 5 节）：

- 只有要去固定地点的事进日历（训练、预约、课、见人、活动）。事件带地点和苹果日历的“通行时间”（按 TfL 算路程再留
  20 分钟余量），出发前 1 小时提醒（不再单独加“路上”日程）；其他事都是待办，分“今天必须做”和“有空就做”，进她的待办清单（Telegram 发“待办”可看、可点完成）。
- 训练写到具体动作、组数、次数和重量（ethan/training.py），三餐写到具体菜谱（ethan/meals.py），都由代码算。
- 各方面每天一行状态（ethan/domains.py）；习惯模块冷启动每天排一个“必须做”。

流程：代码算事实 → 模型挑今天的待办（和少量要出门的事）→ 代码校验，不过带着错误重试 2 次，仍失败用保底计划
→ 代码加训练、冷启动、路上时间 → 保存、写待办和日历 → 早报素材。
ethan2-planner.timer 每天本地 06:30 运行：python3 -m ethan.planner --data <数据目录>
"""

import argparse
import hashlib
import json
import re
from datetime import date, timedelta
from pathlib import Path

from . import brain, domains, focus, meals, quests, training
from .clock import her_now
from .progress import year_progress
from .travel import BUFFER_MIN, DEFAULT_MIN, depart_at, places, where
from .clock import her_today

REMIND_BEFORE_DEPART = 60  # 出发前多久提醒
OVERRIDES = """CREATE TABLE IF NOT EXISTS plan_overrides (day TEXT NOT NULL, title TEXT NOT NULL, action TEXT NOT NULL,
    start TEXT, end TEXT, place TEXT, at TEXT, PRIMARY KEY (day, title))"""


def _hm2m(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _m2hm(n: int) -> str:
    n = max(0, min(n, 23 * 60 + 59))
    return f"{n // 60:02d}:{n % 60:02d}"


def note_override(db, day: date, title: str, action: str, start: str = None, end: str = None, place: str = None):
    """记下她手动改的日程：time（改时间）/ delete（删掉）/ add（自己加的）。同一天同一条以最后一次为准。"""
    db.conn.execute(OVERRIDES)
    db.conn.execute("INSERT OR REPLACE INTO plan_overrides (day, title, action, start, end, place, at) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
                    (day.isoformat(), title, action, start, end, place))
    db.conn.commit()


def shift_trip(item: dict, start: str):
    """日程挪了时间：出发、提醒、到家跟着挪。"""
    delta = _hm2m(start) - _hm2m(item["start"])
    item["start"] = start
    for k in ("end", "leave", "back", "remind"):
        if item.get(k):
            item[k] = _m2hm(_hm2m(item[k]) + delta)
    return item


def apply_overrides(db, day: date, items: list) -> list:
    """重排时保留她手动改的：改过时间的照她的时间（出发、到家跟着挪），删掉的不再排，她自己加的留着。"""
    db.conn.execute(OVERRIDES)
    rows = {r["title"]: dict(r) for r in db.conn.execute("SELECT * FROM plan_overrides WHERE day = ?", (day.isoformat(),))}
    if not rows:
        return items
    out = []
    for it in items:
        o = rows.get(it.get("title")) if it.get("kind") == "event" else None
        if o and o["action"] == "delete":
            continue
        if o and o["action"] in ("time", "add") and o["start"]:
            shift_trip(it, o["start"])
            if o.get("end"):
                it["end"] = o["end"]
        out.append(it)
    have = {it.get("title") for it in out if it.get("kind") == "event"}
    for o in rows.values():
        if o["action"] == "add" and o["title"] not in have and o["start"]:
            out.append({"kind": "event", "start": o["start"], "end": o["end"] or o["start"], "title": o["title"], "place": o["place"] or "",
                        "priority": "main"})
    return out

TRAINING_START, TRAINING_END, TRAINING_PLACE = "16:00", "17:15", "健身房"
DAY_START, DAY_END = "07:00", "23:30"
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
MAX_EVENTS = 3
MIN_STEPS, MAX_STEPS = 6, 20
MAX_RETRIES = 2

# 营养计算（设计第 3 节）：Mifflin-St Jeor，女性；活动系数 1.55（每周 5 练）；每天少吃约 400 千卡；蛋白质 1.6 g/kg
BIRTH, HEIGHT_CM, ACTIVITY, DEFICIT, PROTEIN_PER_KG = date(1996, 3, 15), 165, 1.55, 400, 1.6
TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# 挑选由 小伴 直接拍板，不把“选哪个”丢回给她
ASK_HER_TO_PICK = re.compile(r"供她(挑|选)|给她(挑|选)|让她(挑|选)|从 ?小伴 ?(给|列|找|查)的.{0,6}(候选|选项|名单)?里(挑|选)")


def nutrition(weight_kg: float, today: date, adjust: int = 0) -> dict:
    age = today.year - BIRTH.year - ((today.month, today.day) < (BIRTH.month, BIRTH.day))
    bmr = 10 * weight_kg + 6.25 * HEIGHT_CM - 5 * age - 161
    kcal = round((bmr * ACTIVITY - DEFICIT + adjust) / 10) * 10
    return {"kcal": kcal, "protein_g": round(weight_kg * PROTEIN_PER_KG), "based_on_weight": weight_kg, "weekly_adjust": adjust}


ADJUST_STEP, ADJUST_MIN, ADJUST_MAX = 100, -300, 200


def weekly_adjust(db, today: date) -> dict:
    """每周一按体重趋势调一次热量（mias-os §3）：上周 7 天平均没比再上周降 → 少吃 100 千卡；降超过 1% → 加回 100。
    一周算一次，存进 nutrition_adjust；体重记录不够两周就不动。"""
    db.conn.execute("CREATE TABLE IF NOT EXISTS nutrition_adjust (week TEXT PRIMARY KEY, offset INTEGER NOT NULL, reason TEXT NOT NULL)")
    monday = today - timedelta(days=today.weekday())
    row = db.conn.execute("SELECT * FROM nutrition_adjust WHERE week = ?", (monday.isoformat(),)).fetchone()
    if row:
        return {"offset": row["offset"], "reason": row["reason"]}
    prev = db.conn.execute("SELECT offset FROM nutrition_adjust WHERE week < ? ORDER BY week DESC LIMIT 1", (monday.isoformat(),)).fetchone()
    offset = prev["offset"] if prev else 0

    def avg(start, end):
        ws = [r["weight"] for r in db.conn.execute("SELECT weight FROM body_measures WHERE weight IS NOT NULL AND day >= ? AND day < ?",
                                                   (start.isoformat(), end.isoformat()))]
        return sum(ws) / len(ws) if len(ws) >= 3 else None
    last, before = avg(monday - timedelta(days=7), monday), avg(monday - timedelta(days=14), monday - timedelta(days=7))
    if last is None or before is None:
        reason = "体重记录不够两周，热量先不动"
    elif last >= before:
        offset, reason = max(ADJUST_MIN, offset - ADJUST_STEP), f"上周平均 {last:.1f} kg 没比前一周（{before:.1f}）降，少吃 100 千卡"
    elif (before - last) / before > 0.01:
        offset, reason = min(ADJUST_MAX, offset + ADJUST_STEP), f"上周掉了 {100 * (before - last) / before:.1f}%，掉太快，加回 100 千卡"
    else:
        reason = f"上周平均 {last:.1f} kg，比前一周降了一点，热量照旧"
    db.conn.execute("INSERT INTO nutrition_adjust (week, offset, reason) VALUES (?, ?, ?)", (monday.isoformat(), offset, reason))
    db.conn.commit()
    return {"offset": offset, "reason": reason}


def _minutes(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def _hhmm(minutes: int) -> str:
    minutes = max(0, min(minutes, 23 * 60 + 59))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def busy_with_travel(busy, today: date, travel=None) -> list:
    """她日历里已有的安排，加上来回路上的时间（日历里只写事件本身，排时间时要算路上）。
    她在苹果日历给事件设了通行时间就用她的；没设但有地点，用 TfL 算；没有地点（线上、在家）不算路上。回程按同样时长估。"""
    out = []
    for b in busy:
        s, e, title = b[0], b[1], b[2]
        loc = b[3] if len(b) > 3 and b[3] else {}
        item = {"start": s, "end": e, "title": title, "leave": s, "back": e}
        trip_min, route = loc.get("travel_min"), "她在日历里设的通行时间"
        if trip_min is None and loc.get("location"):
            dest = where(loc)
            if travel is not None and dest:
                trip = travel.minutes("家", dest, today, s)
                trip_min, route = trip["minutes"], trip["route"]
            else:
                trip_min, route = DEFAULT_MIN, f"地点查不到路线，按 {DEFAULT_MIN} 分钟估"
        if trip_min:  # 和训练一样写清：去哪、路上多久、几点出发、几点提醒（出发 = 开始 − 路上 − 20 分钟）
            leave = depart_at(s, trip_min)
            minutes = _minutes(s) - _minutes(leave)
            item.update(place=loc.get("location", ""), travel_min=minutes, trip_min=trip_min, route=route,
                        travel_how=f"路上约 {trip_min} 分钟（{route}），加 {BUFFER_MIN} 分钟余量",
                        leave=leave, back=_hhmm(_minutes(e) + trip_min), remind=_hhmm(_minutes(leave) - REMIND_BEFORE_DEPART))
        out.append(item)
    return out


def quest_facts(db, day: date = None) -> dict:
    """任务系统的现状：每个今年目标的主线拆到哪了、哪些目标还没拆、支线、小伴 答应的事。
    哪些算数都按 ethan/taskrules.py（统一）：说算了的、在等的、推到明天的、暂停副本的、交给 Claude / Codex / Grok 的都不给模型。"""
    from . import stages, taskrules
    day = day or her_today()
    mains = quests.main_quests(db, with_agent_goals=True, day=day)
    years = [dict(r) for r in db.conn.execute("SELECT id, title, status, due, handled_by FROM goals WHERE level = 'year' "
                                              "AND status = 'active' ORDER BY sort IS NULL, sort, due IS NULL, due, id")]
    # 交给 agent 的副本：拆了里程碑的才写进来（里面是要她本人做的那几步）
    years = [g for g in years if not g["handled_by"] or stages.has_structure(db, g["id"])]
    rows = taskrules.rows(db, "planner_in", day, None)
    now = {t["id"] for t in rows if t["state"] == "open"}
    main = {}
    for g in years:
        qs = mains.get(g["id"], [])
        if qs:
            qs = [q for q in qs if not q.get("habit_id")]
            main[g["id"]] = {"goal": g["title"], "done": sum(q["done"] for q in qs), "total": len(qs),
                             "draft": any(q["draft"] for q in qs),
                             "open": [q["title"] for q in qs if not q["done"] and q["state"] not in ("waiting", "postponed", "not_started")]}
            m = stages.current(db, g["id"]) if stages.has_structure(db, g["id"]) else None
            if m:  # 新结构：写当前里程碑和今天该做的阶段任务
                main[g["id"]].update(milestone=m["title"], milestone_due=m["due"], open=[q["title"] for q in qs if q["id"] in now])
    return {"main": main,
            "needs_breakdown": [{"goal_id": g["id"], "title": g["title"], "due": g["due"]}
                                for g in years if g["id"] not in mains],
            "side": [t["title"] for t in rows if t.get("quest") != "main"],
            "ethan": [t["title"] for t in taskrules.ethan_tasks(db, day)]}


def gather_facts(db, today: date, busy=(), settings=None, cold_start_from: date = None, travel=None) -> dict:
    """第一步：全部由代码算出来。busy：她日历里已有安排的 [(开始, 结束, 标题[, 地点信息])]，HH:MM。"""
    weights = [r["weight"] for r in db.body_measures((today - timedelta(days=6)).isoformat()) if r["weight"]]
    latest = db.conn.execute("SELECT weight FROM body_measures WHERE weight IS NOT NULL "
                             "ORDER BY day DESC, id DESC LIMIT 1").fetchone()
    weight = round(sum(weights) / len(weights), 1) if weights else (latest["weight"] if latest else 60.0)
    yesterday = db.completion((today - timedelta(days=1)).isoformat(), today.isoformat())
    week = db.completion((today - timedelta(days=7)).isoformat(), today.isoformat())
    chain = [g for g in db.goal_chain() if g["level"] in ("year", "stage")]
    rx = training.prescribe(db, today)
    adj = weekly_adjust(db, today)
    food = {**nutrition(weight, today, adj["offset"]), "adjust_reason": adj["reason"]}
    cold = domains.todays_cold_start(db, today, cold_start_from)
    return {
        "today": today.isoformat(),
        "weekday": WEEKDAYS[today.weekday()],
        "progress": year_progress(db, today),
        "domains": domains.domain_lines(db, today, cold_start_from),
        "goals": [{"id": g["id"], "level": g["level"], "title": g["title"], "status": g["status"]} for g in chain],
        "yesterday": yesterday,
        "last_7_days": {"planned": week["planned"], "done": week["done"]},
        "feedback": [f["note"] for f in db.unused_feedback()],
        "quests": quest_facts(db, today),
        "training": ({"start": TRAINING_START, "end": TRAINING_END, "place": TRAINING_PLACE, "focus": rx["focus"]}
                     if rx else None),
        "cold_start": {"module": cold[0], "how": cold[1]} if cold else None,
        "nutrition": food,
        "places": sorted(places(settings)),
        "saved_ideas": brain.ideas(db),
        "deleted_recently": db.recently_deleted(),
        "busy": busy_with_travel(busy, today, travel),
        "day_window": [DAY_START, DAY_END],
        "week_focus": focus.current(db, today),
        "her_profile": _profile(db),
        "her_decisions": _decisions(db),
    }


def _decisions(db) -> list:
    """第 22 题：排计划前，管到计划器的生效决定（暂停、规矩、约束），走 onto_query（ontology-plan 3.1）；没切到新结构时不给。"""
    try:
        from . import onto_query
        if not onto_query.reading_new(db, "planner"):
            return []
        got = onto_query.decisions_for(db, "planner", None)
        return [i["value"] for i in got["items"]][:15]
    except Exception:
        return []


def _profile(db) -> str:
    """她的画像（作息、偏好这些排计划用得上），走记忆入口、报上“计划器”（ethan/memusers.py）。"""
    try:
        from . import onto_query, portrait
        if onto_query.reading_new(db, "planner"):  # ontology 第 4 步：偏好改从新结构读（第 4 题），其余方面照旧读画像
            prefs = onto_query.preference_lines(db, "planner")
            rest = portrait.chat_text(db, skip=("preferences",), user="planner")
            return ((f"#### 偏好\n{prefs}\n\n" if prefs else "") + rest)[:1500]
        return portrait.chat_text(db, "planner")[:1500]
    except Exception:
        return ""


PLANNER_PROMPT = """你是 Mia 的人生管理系统里的计划器。下面的“事实”全部由代码算好，你不能改其中的数字。
她的待办按游戏任务系统管理：主线 = 每个目标完整拆成的步骤；日常 = 每天坚持的事（系统已有，不用你管）；
支线 = 其他零散待办。训练、冷启动、三餐也由代码安排。你只负责三件事：

只输出一个 JSON 对象，不要输出别的文字：
{"breakdowns": [{"goal_id": 编号, "steps": [{"title": "具体要做的一步", "size": "S|M|L", "milestone": true|false, "hard": true|false}]}],
 "ethan": [{"title": "小伴 要替她准备好、再交给她审批的东西", "goal_id": 目标编号或 null}],
 "events": [{"start": "HH:MM", "end": "HH:MM", "place": "地点名", "title": "去做什么", "goal_id": 目标编号或 null}],
 "review": {"目标编号": "今天的进展点评，一句话"},
 "skipped": [{"title": "今天故意没排的事", "why": "一句人话原因"}]}

规则：
1. breakdowns：只给 quests.needs_breakdown 里的目标拆，其他目标已经拆过了不要再拆。每个目标拆 6–20 步，
   从现在一直拆到在截止日前完成这个目标，按先后顺序，足够全面；每步具体到她能直接开始。
   size：S 一小时内、M 半天左右、L 要好几天。关键的阶段节点 milestone 设 true（每个目标 1–4 个）。
   hard 设 true 的是“硬骨头”：她最可能拖、最难开始的那一步（比如第一次给陌生客户发消息），每个目标 1–3 个，完成多给 50% 积分。
   步骤只放她本人亲自要做的事；漏斗类目标按漏斗各步拆（列名单、联系、聊、报价、成交）。
   交给对应 agent 管的项目副本不会出现在 needs_breakdown 里，也不要替它们排任务。
2. ethan：小伴 只管日程和生活上的事（订餐厅门票交通、查场地和价格、比较选项、列生活清单）；项目工作（DemoApp、个人主页、开发、
   创业、自媒体、各副本里的具体工作内容）她直接找对应的 agent，不要写进 ethan，也不要拆成她的步骤。
   有前提没满足的（比如签证没下来就做行程）先不写。quests.ethan 里已有的不要重复写。新写的 小伴 会先问她要不要再做。
   日常周常里已经定了怎么做的不要再去找（比如英语陪练就是 Grok，别写“查英语陪练的渠道 / 老师 / 课程”）。
   和本赛季副本（goals 里 status 为 active 的）相关的先列、先做，其他的排在后面，但也要做，不能丢。
   要挑选的（去哪、上哪节课、选哪家）由 小伴 直接拍板，告诉她选了什么、为什么，不要列选项让她挑；
   她的步骤里也不要写“从 小伴 的候选里选”。只有要花钱或付款时，小伴 先跟她确认再订。
3. events：只有必须到某个地点才能做的事才放（预约、上课、见人、活动），最多 3 件，大多数日子是空的。
   能选时间的尽量排在早上：起床后先出门，再开始工作。
   saved_ideas 是她自己收藏的地方和玩法：小伴 替她选新体验、出门小计划、旅行地点时优先从里面挑（收藏真正用起来）。
   place 用 places 里的名字；不在里面就写具体地点名称。不能和训练、busy（leave 到 back 是她连同来回路上都不在的时间）冲突。
   要出门的事按一条路线排：上一站结束到下一站开始要留出路上时间；离得近、中间空不出一小时就从上一站直接过去，
   别排成刚出门又回家、或者上一站还没结束就得出发去下一站。训练前后有饭局，训练挪到饭局结束加路上之后。
4. goal_id 只能用 goals 里出现过的编号。参考 yesterday、feedback：总拖的事换个做法。
5. deleted_recently 是她最近 30 天亲手删掉的事：不要再排回来，换个说法的同一件事也不要。
   her_decisions 是她定过、现在生效的决定（暂停、规矩、约束）：照着排，和别的事实冲突时以它为准（ontology-plan 3.1，第 22 题）。
6. review：给 progress 里每个目标写一句今天的进展点评，像军师：领先还是落后、差在哪、今天打哪一下最值（数字只用 progress 里的）。
   skipped：今天本来该做、有意不排的事（比如时间不够、等别人回复、天气不合适），一句人话说清为什么，没有就空数组。
   暂停的副本、以后才做的事本来就不在今天的范围，不要写进 skipped（这个赛季不看的目标列出来是多余的）。
7. week_focus 是这周的 3 件重点（周日周报里定的）：今天的 events、ethan 和点评优先围着它们转，
   能往前推一步的就推；skipped 里不要出现它们，除非真的做不了并写清原因。confirmed 为 false 是她还没点头，照样优先。
"""


def parse_plan(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("没有找到 JSON 对象")
    data = json.loads(text[start:end + 1])
    for key in ("breakdowns", "events", "ethan"):
        if not isinstance(data.get(key, []), list):
            raise ValueError(f"{key} 必须是列表")
    data["ethan"] = [x for x in data.get("ethan") or [] if not DECIDED.search(str(x.get("title") if isinstance(x, dict) else x))]
    review = data.get("review") if isinstance(data.get("review"), dict) else {}
    skipped = [x for x in data.get("skipped") or [] if isinstance(x, dict) and x.get("title")] if isinstance(data.get("skipped"), list) else []
    skipped = [x for x in skipped if not PAUSED_WHY.search(f"{x.get('title')} {x.get('why') or ''}")]  # 代码兜底：暂停副本的不列
    return {"breakdowns": data.get("breakdowns", []), "events": data.get("events", []), "ethan": data.get("ethan", []),
            "review": {str(k): str(v)[:120] for k, v in review.items() if v}, "skipped": skipped[:8]}


def validate(plan: dict, facts: dict, goal_ids: set) -> list:
    """第三步：返回错误列表，空表示通过。"""
    errors = []
    need = {g["goal_id"] for g in facts["quests"]["needs_breakdown"]}
    got = set()
    for b in plan.get("breakdowns", []):
        gid = b.get("goal_id") if isinstance(b, dict) else None
        if gid not in need:
            errors.append(f"目标 {gid} 不需要拆解（只拆 needs_breakdown 里的）")
            continue
        got.add(gid)
        steps = b.get("steps") or []
        if not MIN_STEPS <= len(steps) <= MAX_STEPS:
            errors.append(f"目标 {gid} 拆了 {len(steps)} 步，要 {MIN_STEPS}–{MAX_STEPS} 步")
        for n, s in enumerate(steps, 1):
            if not isinstance(s, dict) or not s.get("title"):
                errors.append(f"目标 {gid} 第 {n} 步缺少 title")
            elif s.get("size") not in quests.SIZE_POINTS:
                errors.append(f"目标 {gid} 第 {n} 步 size 只能是 S、M、L")
            elif ASK_HER_TO_PICK.search(s["title"]):
                errors.append(f"目标 {gid} 第 {n} 步让她挑选，挑选由 小伴 直接拍板，这步不要放给她")
        if not any(isinstance(s, dict) and s.get("milestone") for s in steps):
            errors.append(f"目标 {gid} 没有标阶段节点（milestone）")
    for gid in need - got:
        errors.append(f"目标 {gid} 还没拆解，请拆")
    for n, t in enumerate(plan.get("ethan", []), 1):
        if not isinstance(t, dict) or not t.get("title"):
            errors.append(f"第 {n} 件 小伴 准备的事缺少 title")
        elif t.get("goal_id") is not None and t["goal_id"] not in goal_ids:
            errors.append(f"第 {n} 件 小伴 准备的事 goal_id {t['goal_id']} 不存在")
        elif ASK_HER_TO_PICK.search(t["title"]):
            errors.append(f"第 {n} 件 小伴 准备的事让她挑选了，挑选由 小伴 直接拍板")
    events = plan.get("events", [])
    if len(events) > MAX_EVENTS:
        errors.append(f"出门的事 {len(events)} 件，最多 {MAX_EVENTS} 件")
    lo, hi = (_minutes(t) for t in facts["day_window"])
    taken = [(_minutes(b["leave"]), _minutes(b["back"]), b["title"] + ("（含来回路上）" if b.get("travel_min") else ""))
             for b in facts["busy"]]
    if facts.get("training"):
        t = facts["training"]
        taken.append((_minutes(t["start"]) - DEFAULT_MIN - 10, _minutes(t["end"]), "训练（含路上）"))
    for n, e in enumerate(events, 1):
        if not isinstance(e, dict) or not e.get("title") or not e.get("place"):
            errors.append(f"第 {n} 件出门的事缺少 title 或 place")
            continue
        if not (TIME.match(str(e.get("start", ""))) and TIME.match(str(e.get("end", "")))):
            errors.append(f"第 {n} 件出门的事时间格式不对：{e.get('start')}–{e.get('end')}")
            continue
        s, en = _minutes(e["start"]), _minutes(e["end"])
        if s >= en or s < lo or en > hi:
            errors.append(f"第 {n} 件出门的事时间不合理：{e['start']}–{e['end']}")
        if e.get("goal_id") is not None and e["goal_id"] not in goal_ids:
            errors.append(f"第 {n} 件出门的事 goal_id {e['goal_id']} 不存在")
        for a, b, title in taken:
            if s - DEFAULT_MIN - 10 < b and a < en:
                errors.append(f"第 {n} 件出门的事（含路上时间）和“{title}”冲突")
        taken.append((s - DEFAULT_MIN - 10, en, e["title"]))
    return errors


def fallback_plan(facts: dict) -> dict:
    """保底：不拆新目标、不加出门的事，只列已有的任务。"""
    return {"breakdowns": [], "events": [], "ethan": []}


def assemble(plan: dict, facts: dict, db, today: date, travel=None) -> list:
    """第三步之后：代码加上训练、冷启动和路上时间，得到要保存的计划条目。"""
    items = []
    events = list(plan["events"])
    rx = training.prescribe(db, today)
    if facts.get("training") and rx:
        t = facts["training"]
        events.append({"start": t["start"], "end": t["end"], "place": t["place"], "title": f"训练：{t['focus']}",
                       "goal_id": None, "detail": training.describe(rx),
                       "how": "到出发时间先换衣服出门，到了先做 10 分钟体态热身", "smaller": "只做热身和前两个动作"})
    for e in sorted(events, key=lambda e: e["start"]):
        travel_min, detail, leave, remind, trip = None, e.get("detail"), None, None, {}
        if travel is not None and e.get("place"):
            trip = travel.minutes("家", e["place"], today, e["start"])
            leave = depart_at(e["start"], trip["minutes"])
            travel_min = _minutes(e["start"]) - _minutes(leave)  # 含 20 分钟余量，日历按这个留通行时间
            remind = _hhmm(_minutes(leave) - REMIND_BEFORE_DEPART)
            route = f"路上约 {trip['minutes']} 分钟（{trip['route']}），{leave} 前出发，{remind} 提醒"
            detail = route + ("\n\n" + detail if detail else "")
        items.append({"kind": "event", "start": e["start"], "end": e["end"], "place": e.get("place"),
                      "title": e["title"], "goal_id": e.get("goal_id"), "how": e.get("how"),
                      "smaller": e.get("smaller"), "detail": detail, "travel_min": travel_min,
                      "leave": leave, "remind": remind,
                      "trip_min": trip["minutes"] if leave else None, "route": trip["route"] if leave else None})
    cold = facts.get("cold_start")
    if cold:
        items.append({"kind": "todo", "priority": "side", "title": f"冷启动：{cold['module']}（小伴 带你做，10–15 分钟）",
                      "how": "找 小伴 说“开始建档”，他一步步带你", "smaller": "先做其中一小步", "detail": cold["how"]})
    for t in plan.get("ethan", []):
        items.append({"kind": "todo", "priority": "ethan", "title": t["title"], "goal_id": t.get("goal_id"),
                      "how": "小伴 做好后发给她审批；需要上网、查地图、预订的交给 Grok"})
    return items


def link_tasks(db, items: list, today: date):
    """待办进她的待办清单：同名的没做完待办直接挂上，没有就新建，截止今天。
    当天重排时，上一版自动建的、新版不要了、她也还没做的待办删掉，免得清单越堆越多。
    小伴 准备的事不删：那是他答应要做的，重排不该让它消失（重排丢过 9 件）。"""
    from . import naming, taskrules
    for it in list(items):
        if it["kind"] != "todo":
            continue
        # 统一查重（ethan/taskrules.py already）：同名或换了说法的没做完的、她改过名字的那件 → 挂上去；
        # 她删过的、最近做完的 → 不再排（以前只靠提示词叮嘱模型，docs/design/system-rules.md 第 1 节）。小伴 自己的活只挂不删
        hit = taskrules.already(db, it["title"], "planner")
        if hit and hit["id"]:
            it["task_id"] = hit["id"]
            continue
        if hit and it.get("priority") != "ethan":
            print(f"[planner] {'她删过的' if hit['why'] == 'deleted' else '最近做完了'}，不再排：{it['title']}", flush=True)
            items.remove(it)
            continue
        it["title"] = naming.title(it["title"])  # 新建的待办名字写成标题式（docs/design/ui-spec.md 第 5 节）
        it["task_id"] = db.add_task(it["title"], goal_id=it.get("goal_id"),  # 多数事没有硬截止，不默认截止今天
                                    promised_by_ethan=it.get("priority") == "ethan" and not _is_ops(it["title"]))
        it["task_created"] = True
    keep = {it["task_id"] for it in items if it.get("task_id")}
    for r in db.conn.execute(
            "SELECT DISTINCT i.task_id FROM plan_items i JOIN daily_plans p ON p.id = i.plan_id "
            "WHERE p.day = ? AND i.task_created = 1 AND i.priority != 'ethan'", (today.isoformat(),)).fetchall():
        done = db.conn.execute("SELECT done FROM tasks WHERE id = ?", (r["task_id"],)).fetchone()
        if r["task_id"] not in keep and done is not None and not done["done"]:
            db.conn.execute("UPDATE plan_items SET task_id = NULL WHERE task_id = ?", (r["task_id"],))
            db.conn.execute("DELETE FROM tasks WHERE id = ?", (r["task_id"],))
    db.conn.commit()


def rest_items(db, items: list, day: date = None) -> list:
    """计划里列出没做完的事：主线（每个进行中的副本，从下一步开始）、其他、小伴 答应的事。哪些能列按 ethan/taskrules.py 的
    planner_in 视图（统一）：暂停 / 达成 / 没开始副本的、说算了的、在等的、推到明天的、没到开始日的都不列（以前暂停的排最后照列）。
    拆了里程碑的副本：当前里程碑里今天该做的阶段任务标“下一步”（进今天），其余照样列着（quest-structure.md 第 5 节）；
    交给 agent 的副本：里程碑里要她本人做的照样排，没拆里程碑的是 agent 的活、不排。"""
    from . import stages, taskrules
    day = day or her_today()
    chosen = {it.get("task_id") for it in items}
    rows = [t for t in taskrules.rows(db, "planner_in", day, None) if t["id"] not in chosen]
    order = list(quests.main_quests(db, with_agent_goals=True, day=day))
    rest, first = [], set()
    for gid in order:
        qs = [t for t in rows if t.get("quest") == "main" and t.get("goal_id") == gid]
        staged = stages.has_structure(db, gid)
        for n, q in enumerate(qs):
            nxt = q["state"] == "open" if staged else (n == 0 and q["state"] == "open")
            rest.append({"kind": "todo", "priority": "main", "title": q["title"], "goal_id": gid, "task_id": q["id"],
                         "how": ("下一步" if nxt else None), "detail": json.dumps(
                             {"size": q["size"], "milestone": bool(q["milestone"]), "draft": bool(q["draft"]),
                              "points": quests.task_points(q)}, ensure_ascii=False)})
            first.add(q["id"])
    for t in rows:
        if t["id"] in first or t.get("milestone_id") or t.get("quest") == "main":
            continue
        rest.append({"kind": "todo", "priority": "side", "title": t["title"], "goal_id": t.get("goal_id"), "task_id": t["id"],
                     "how": f"截止 {t['due']}" if t.get("due") else None})
    for t in taskrules.ethan_tasks(db, day):
        if t["id"] not in chosen:
            rest.append({"kind": "todo", "priority": "ethan", "title": t["title"], "goal_id": t.get("goal_id"), "task_id": t["id"],
                         "how": f"截止 {t['due']}" if t.get("due") else None})
    return rest


def main_brief(items: list, facts: dict) -> list:
    """每个目标一行：完成几步、下一步是什么、拆解是不是还在等她过目。"""
    out = []
    for gid, info in facts["quests"]["main"].items():
        nxt = next((i["title"] for i in items if i.get("goal_id") == gid and i.get("priority") == "main"
                    and i.get("how") == "下一步"), None)
        out.append({"goal": info["goal"], "done": info["done"], "total": info["total"], "next": nxt,
                    "draft": info["draft"]})
    return out


def timeline(items: list, busy: list) -> list:
    """早报卡片的时间线：她日历上已有的安排（连同出门、到家）和计划里要出门的事，按时间排好。"""
    rows = []
    def trip(x, start, title, place):  # 点开能看的：去哪、路上多久、几点出发
        return {"title": title, "place": place or "", "start": start, "leave": x.get("leave"), "remind": x.get("remind"),
                "trip_min": x.get("trip_min"), "route": x.get("route") or ""}
    for b in busy:
        if b["leave"] != b["start"]:
            info = trip(b, b["start"], b["title"], b.get("place"))
            if b.get("remind"):
                rows.append((b["remind"], "⏰", f"准备出发去{b['title']}", info))
            rows += [(b["leave"], "🚶", f"出门 → {b['start']} {b['title']}", info), (b["back"], "🏠", "到家", None)]
        else:
            rows.append((b["start"], "📌", f"{b['title']}（到 {b['end']}）", None))
    for i in items:
        if i["kind"] != "event":
            continue
        if i.get("leave"):
            info = trip(i, i["start"], i["title"], i.get("place"))
            rows += [(i["remind"], "⏰", "准备出发", info), (i["leave"], "🚶", f"出门 → {i['start']} {i['title']}（到 {i['end']}）", info)]
        else:
            rows.append((i["start"], "📌", f"{i['title']}（到 {i['end']}）", None))
    return [{"at": at, "icon": icon, "text": text, **({"trip": info} if info else {})}
            for at, icon, text, info in sorted(rows, key=lambda r: r[:3])]


STATUS_MARK = {"领先": "🔥 领先", "落后": "⚠️ 落后", "达成": "✅ 达成", "正常": "正常"}


DECIDED = __import__("re").compile(r"英语陪练.*(渠道|老师|课程|平台)|(找|查).*英语陪练")  # 已经定了用 Grok
PAUSED_WHY = __import__("re").compile(r"暂停|这个赛季不|下个赛季|以后的副本")


def goal_card(p: dict) -> dict:
    """早报卡片里每个目标一行：短名字、一句进度，体重类带进度条（frac 是从基线到目标走了多少）。"""
    line, frac = p["text"], None
    if p.get("kind") is None:
        line = "还没有指标"
    elif p.get("current") is not None and p.get("baseline") is not None and p["kind"] == "weight_avg7":
        line = f"{p['current']:g} → {p['target']:g} kg"
        span = p["baseline"] - p["target"]
        frac = round(min(max((p["baseline"] - p["current"]) / span, 0), 1), 2) if span else None
    elif p.get("kind") == "funnel" and p["status"] != "达成" and p.get("stuck_at"):
        part = next(x for x in p["steps"] if x.startswith(p["stuck_at"]))
        line = f"卡在「{p['stuck_at']}」{part[len(p['stuck_at']):].strip()}"
    elif p.get("kind") == "launch" and p.get("days_left") is not None and p["status"] != "达成":
        line = f"还有 {p['days_left']} 天上线"
    elif p.get("kind") == "count_week":
        line = p["text"].replace("本周还剩", "还剩").replace(" 次，", "，")
    return {"name": p["title"].split("：")[0], "line": line, "frac": frac, "status": STATUS_MARK.get(p["status"], "")}


def make_brief(facts: dict, items: list, origin: str, menu: dict, rx: dict) -> dict:
    """第四步：早报素材。"""
    active = [p for p in facts["progress"] if p["status"] != "暂停"]
    todos = [i for i in items if i["kind"] == "todo"]
    return {
        "goals": [p["title"] for p in active],
        "progress": [f"{p['title']}：{p['status']}，{p['text']}" for p in facts["progress"]],
        "domains": facts["domains"],
        "out": [f"{i['start']}–{i['end']} {i['title']}" + (f"（{i['detail'].splitlines()[0]}）" if i.get("travel_min") else "")
                for i in items if i["kind"] == "event"],
        "main": main_brief(items, facts),
        "side": [i["title"] + (f"（{i['how']}）" if str(i.get("how") or "").startswith("截止") else "")
                 for i in todos if i["priority"] == "side"],
        "ethan": [i["title"] for i in todos if i["priority"] == "ethan"],
        "busy": [f"{b['start']}–{b['end']} {b['title']}"
                 + (f"（路上约 {b.get('trip_min') or b['travel_min']} 分钟：{b['leave']} 出门，约 {b['back']} 回来）" if b.get("travel_min") else "")
                 for b in facts["busy"]],
        "training": ({"focus": rx["focus"], "exercises": [
            f"{e['name']} {e['sets']}×{e['reps'][0]}–{e['reps'][1]}{e['unit'] if e['unit'] != '次' else ''}"
            + (f" {e['weight']:g}kg" if e.get("weight") else "") for e in rx["exercises"]]} if rx else None),
        "meals": meals.summary(menu),
        "cold_start": facts["cold_start"]["module"] if facts.get("cold_start") else None,
        "fallback": origin == "fallback",
        "timeline": timeline(items, facts["busy"]),
        "goal_cards": [goal_card(p) for p in active],
    }


def plan_day(db, router, today: date, busy=(), calendar=None, travel=None, settings=None,
             cold_start_from: date = None) -> dict:
    run = db.start_job("planner")
    quests.seed(db)
    facts = gather_facts(db, today, busy, settings, cold_start_from, travel)
    goal_ids = {g["id"] for g in facts["goals"]}
    facts_json = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    messages = [{"role": "user", "content": "事实：\n" + facts_json}]
    origin, plan, errors = "fallback", None, []
    for attempt in range(MAX_RETRIES + 1):
        try:
            reply = router.complete(PLANNER_PROMPT, messages, purpose="planner")
            candidate = parse_plan(reply.text)
            errors = validate(candidate, facts, goal_ids)
        except Exception as e:  # 模型不可用或输出不是 JSON
            reply, errors = None, [f"{type(e).__name__}: {str(e)[:200]}"]
        if not errors:
            origin, plan = "model", candidate
            break
        if reply is not None:
            messages += [{"role": "assistant", "content": reply.text},
                         {"role": "user", "content": "校验没通过，请修正后重新输出完整 JSON：\n- " + "\n- ".join(errors)}]
    if plan is None:
        plan = fallback_plan(facts)
    for b in plan.get("breakdowns", []):  # 主线拆解先当草稿存下，小伴 发她过目
        quests.add_breakdown(db, b["goal_id"], b["steps"], draft=True)
    if plan.get("breakdowns"):
        facts["quests"] = quest_facts(db, today)
    items = assemble(plan, facts, db, today, travel)
    items = apply_overrides(db, today, items)  # 她在工作台手动改过的日程，重排时照她改的
    link_tasks(db, items, today)
    items += rest_items(db, items, today)
    menu = meals.pick(facts["nutrition"]["kcal"], facts["nutrition"]["protein_g"], today,
                      avoid=meals.yesterday_ids(db, today))
    rx = training.prescribe(db, today)
    brief = make_brief(facts, items, origin, menu, rx)
    try:  # 时间线按一条路线串（ethan/route.py）：上一站结束直接去下一站还是先回家、赶不赶得上
        from . import route
        from .travel import places as _places
        raw = [(b["start"], b["end"], b["title"], {"location": b.get("place") or "", "travel_min": b.get("trip_min")})
               for b in facts["busy"]]
        r = route.timeline(raw, items, _places(settings), travel, today)
        brief["timeline"], brief["route_conflicts"] = r["rows"], r["conflicts"]
    except Exception as e:
        print(f"[planner] 路线没串起来：{type(e).__name__} {e}", flush=True)
    brief["meals_text"] = meals.describe(menu)
    brief["game"] = quests.today_summary(db, today)
    brief["review"] = {k: v for k, v in (plan.get("review") or {}).items() if k.isdigit() and int(k) in goal_ids}  # 每个副本今天的进展点评
    brief["skipped"] = [{"title": str(x["title"])[:80], "why": str(x.get("why") or "")[:120]} for x in plan.get("skipped") or []]
    fingerprint = hashlib.sha256(facts_json.encode()).hexdigest()[:16]
    plan_id = db.save_plan(today.isoformat(), origin, facts_json, json.dumps(brief, ensure_ascii=False),
                           fingerprint, items)
    if facts["feedback"]:
        db.mark_feedback_used([f["id"] for f in db.unused_feedback() if f["note"] in facts["feedback"]])  # 排计划期间新来的留给下次
    synced = None
    if calendar is not None:
        saved = db.latest_plan(today.isoformat())
        stale = [r["calendar_uid"] for r in db.conn.execute(
            "SELECT i.calendar_uid FROM plan_items i JOIN daily_plans p ON p.id = i.plan_id "
            "WHERE p.day = ? AND p.id != ? AND i.calendar_uid IS NOT NULL", (today.isoformat(), saved["id"]))]
        try:
            synced = calendar.sync_day(today, [i for i in saved["items"] if i["kind"] != "todo"], stale_uids=stale,
                                       places=places(settings), remind_before=REMIND_BEFORE_DEPART)
            for item_id, uid in synced.items():
                db.set_calendar_uid(item_id, uid)
        except Exception as e:  # 日历失败不影响计划和早报，但要留痕
            errors.append(f"写日历失败：{e}")
    db.finish_job(run, processed=len(items), error="; ".join(errors) if origin == "fallback" or errors else None)
    return {"plan_id": plan_id, "origin": origin, "items": items, "brief": brief, "errors": errors,
            "calendar": synced}


INSPIRATION_TASK = (
    "【惊喜灵感调研】你是 小伴 的调研员。请读或查 2–3 部近期口碑好的言情小说、影视，或真实的异地恋故事，"
    "提炼 5 个能用在“伦敦（她）—深圳（他）异国恋”里的惊喜点子。每个写清：形式、要准备什么、大概花费、为什么动人、"
    "怎么变得只属于她。不要大餐和名牌，要细节和创意。办完把结果 POST 到回传地址，正文以【惊喜灵感】开头。")
INSPIRATION_EVERY_DAYS = 7


def ask_for_inspiration(settings, today: date, grok) -> bool:
    """每周派 Grok 调研员找一次惊喜灵感（她兑换惊喜时 小伴 要用）。"""
    if grok is None or settings is None:
        return False
    last = settings.get("grok_inspiration_last")
    if last and (today - date.fromisoformat(last)).days < INSPIRATION_EVERY_DAYS:
        return False
    if grok(INSPIRATION_TASK, "planner"):
        settings.set("grok_inspiration_last", today.isoformat())
        return True
    return False


def main():
    from .cli import build_router
    from .grok import build_grok
    from .icloud import build_calendar
    from .memory import MemoryStore
    from .travel import Travel

    parser = argparse.ArgumentParser(description="生成今天的计划")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--day", help="YYYY-MM-DD，默认伦敦今天")
    parser.add_argument("--force", action="store_true", help="当天已有计划也重排")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    router = build_router(args.data, args.budget)
    settings = router.settings
    today = date.fromisoformat(args.day) if args.day else her_now(settings).date()
    if store.db.latest_plan(today.isoformat()) and not args.force:
        print(f"{today} 已经有计划，不重排（要重排加 --force）")
        return
    calendar = build_calendar()
    busy = calendar.busy(today) if calendar is not None else []
    from . import events  # 提前登记的日程（订位等）也算她已有的安排
    from . import weekreview  # 每周日 21:00 的复盘：在家、定死的一小时
    busy = sorted(list(busy) + events.for_day(store.db, today) + weekreview.busy_for(today, calendar))
    try:
        events.sync(store.db, calendar)
    except Exception as e:
        print(f"[planner] 提前登记的日程没写进日历：{e}", flush=True)
    start = settings.get("cold_start_from")
    result = plan_day(store.db, router, today, busy=busy, calendar=calendar, travel=Travel(args.data, settings),
                      settings=settings, cold_start_from=date.fromisoformat(start) if start else None)
    try:  # 今天排进复盘的里程碑：小伴 先起草复盘（ethan/msreview.py）
        from . import msreview
        wrote = msreview.write_due(store.db, router, today)
        if wrote:
            print(f"[planner] 起草了里程碑复盘：{'、'.join(wrote)}", flush=True)
    except Exception as e:
        print(f"[planner] 里程碑复盘草稿没写成：{type(e).__name__} {e}", flush=True)
    ask_for_inspiration(settings, today, build_grok(args.data))
    try:  # 昨日卡：排完计划就画（ethan/cards.py），失败不影响计划
        import os
        from .cards import make_card
        from .photos import OpenRouter
        key = os.environ.get("OPENROUTER_API_KEY")
        make_card(args.data, store.db, today, store.db.latest_plan(today.isoformat()),
                  maker=OpenRouter(key) if key else None, ledger=router.ledger, router=router,  # router：按今天的事编镜头
                  store=store)  # 昨天的日记漏写了先补写再出卡
    except Exception as e:
        print(f"[planner] 昨日卡没生成：{e}", flush=True)
    print(json.dumps({k: result[k] for k in ("plan_id", "origin", "errors")}, ensure_ascii=False))
    for i in result["items"]:
        when = f"{i['start']}–{i['end']}" if i.get("start") else i.get("priority", "")
        print(f"  [{i['kind']}] {when} {i['title']}")


if __name__ == "__main__":
    main()


def _is_ops(title: str) -> bool:
    """给 Claude / 开发会话的放行、权限、配置：不记成 小伴 的准备任务（prep.is_ops）。"""
    from . import prep
    return prep.is_ops(title)
