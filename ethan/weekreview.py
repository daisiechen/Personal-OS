"""每周复盘。

她的原话：“复盘每周一天，每周日晚上的九点钟，伊森主动来找我做……就是每周日晚上九点有一个固定的日程，大概一个小时，
约在我的日历上，是我跟伊森做每周的复盘……他可以给我一个列表……然后每周最后一个周日晚上是，除了当日的复盘，还要做当月的复盘，
每季度的最后一个晚上还要加上这个季度的复盘。不过这个月度和季度的复盘不用单独约日程，就复用这个日程就好了。”
之前定的：“复盘只在聊天里聊吧，工作台上只看结果，就是汇总和草稿。”“季度”= 赛季，最后一个周日（她确认）；
原来周日 20:00 的周报并进来，周常“财务复盘”“周日复盘”复盘做完自动打卡。

怎么运作：
- 苹果日历“小伴 计划”里一个每周日 21:00–22:00 的重复日程「和 小伴 周复盘」（ensure_event，只写一次）。
  她在日历里把哪一次挪了时间，就按挪后的来；删了那一次，那周就不找她。
- 到点 小伴 主动发开场（ethan/proactive.py 调 opener）：这周（/ 这个月 / 这个赛季）的数，加一张 3–8 题的单子。
  她在聊天里答，一次答完或一题一题答都行；小伴 追问、列出要改的、她点头才改。
- 聊完 小伴 在回复末尾写 [复盘记录 小结 | 要点；要点 | 调整；调整]，记进 reviews 表，工作台赛季页“复盘记录”里看；
  周常“财务复盘”“周日复盘”自动打卡。
"""

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

TITLE = "和 小伴 周复盘"
UID = "ethan-weekly-review"
START, MINUTES = time(21, 0), 60
WINDOW = timedelta(minutes=90)  # 到点后这么久内都能开场（主动消息每 10 分钟查一次）
OPEN_FOR = timedelta(days=3)  # 开场后几天内她回的都算这次复盘
HABITS = ("财务复盘", "周日复盘")
LONDON = ZoneInfo("Europe/London")
SCHEMA = """CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY, day TEXT NOT NULL UNIQUE, layers TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    opened_at TEXT NOT NULL, summary TEXT, points TEXT, changes TEXT, done_at TEXT)"""
MARK = re.compile(r"\[复盘记录\s+([^\]]+)\]")
LAYER_NAME = {"week": "这周", "month": "这个月", "season": "这个赛季"}


def ensure(db):
    db.conn.execute(SCHEMA)


def anchor(day: date) -> date:
    """这次复盘算哪个周日的：挪到周一、周二的算前一个周日，挪到周五、周六的算后一个。"""
    off = (6 - day.weekday()) % 7
    return day + timedelta(days=off) if off <= 3 else day - timedelta(days=day.weekday() + 1)


def layers(day: date, season_end: date) -> list:
    """这个周日要复盘哪几层：每周都有；当月最后一个周日加月度；赛季最后一个周日再加赛季。"""
    sun = anchor(day)
    out = ["week"]
    if (sun + timedelta(days=7)).month != sun.month:
        out.append("month")
    if sun + timedelta(days=7) > season_end:
        out.append("season")
    return out


def event_ics(first: date) -> str:
    """每周日 21:00–22:00（本地时间，夏令时自动跟着走）的重复日程，开始前 10 分钟提醒。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    d = first.strftime("%Y%m%d")
    end = (datetime.combine(first, START) + timedelta(minutes=MINUTES)).strftime("%H%M%S")
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//小伴//Mias OS//ZH", "BEGIN:VEVENT", f"UID:{UID}", f"DTSTAMP:{stamp}",
        f"DTSTART;TZID=Europe/London:{d}T{START.strftime('%H%M%S')}", f"DTEND;TZID=Europe/London:{d}T{end}",
        "RRULE:FREQ=WEEKLY;BYDAY=SU", f"SUMMARY:{TITLE}",
        "DESCRIPTION:小伴 到点来找你。这周的数他先算好，再给你一张单子，在聊天里回就行。每月最后一个周日加月度复盘，赛季最后一个周日加赛季复盘。",
        "BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{TITLE}", "TRIGGER:-PT10M", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR"]) + "\r\n"


def ensure_event(calendar, settings, today: date) -> bool:
    """把重复日程写进“小伴 计划”日历，只写一次（settings 里记着）。写了返回 True。"""
    if calendar is None or settings.get("review_event") == UID:
        return False
    import urllib.parse
    cal = calendar.ethan_calendar()
    first = today + timedelta(days=(6 - today.weekday()) % 7)
    calendar.request("PUT", urllib.parse.urljoin(cal, f"{UID}.ics"), event_ics(first).encode("utf-8"), ctype="text/calendar")
    settings.set("review_event", UID)
    return True


def slot(day: date, calendar=None):
    """这天的复盘几点开始：(开始, 结束) HH:MM，这天没有返回 None。
    读得到日历就按日历上那一次（她挪过、删过都算）；读不到就按每周日 21:00。"""
    if calendar is not None and hasattr(calendar, "own_events"):
        try:
            for s, e, title in calendar.own_events(day):
                if title == TITLE:
                    return s, e
            return None
        except Exception as e:
            print(f"[复盘] 读日历没成，按周日 21:00 算：{type(e).__name__}", flush=True)
    if day.weekday() == 6:
        end = (datetime.combine(day, START) + timedelta(minutes=MINUTES)).time()
        return START.strftime("%H:%M"), end.strftime("%H:%M")
    return None


def busy_for(day: date, calendar=None) -> list:
    """给计划器和首页时间线：这天的复盘当成在家、定死的一段。"""
    got = slot(day, calendar)
    return [(got[0], got[1], TITLE, {"location": ""})] if got else []


def opened(db, day: date):
    ensure(db)
    r = db.conn.execute("SELECT * FROM reviews WHERE day = ?", (day.isoformat(),)).fetchone()
    return dict(r) if r else None


def due(db, now_local: datetime, season_end: date, calendar=None):
    """现在该不该开场：返回 {"day", "layers", "start"} 或 None。"""
    day = now_local.date()
    if opened(db, day):
        return None
    if day.weekday() != 6 and calendar is None:
        return None
    got = slot(day, calendar)
    if not got:
        return None
    h, m = map(int, got[0].split(":"))
    at = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    if not (at <= now_local < at + WINDOW):
        return None
    return {"day": day, "layers": layers(day, season_end), "start": got[0]}


def open_review(db, day: date, lays: list):
    ensure(db)
    cur = db.conn.execute("INSERT OR IGNORE INTO reviews (day, layers, opened_at) VALUES (?, ?, ?)",
                          (day.isoformat(), ",".join(lays), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    if cur.rowcount:
        from . import transitions
        transitions.record(db, "review", cur.lastrowid, None, "open", {"layers": lays})
    db.conn.commit()


def current(db, now: datetime = None):
    """正在进行的复盘（开场后 3 天内、还没记下结果），给 小伴 聊天时看。"""
    ensure(db)
    now = now or datetime.now(timezone.utc)
    r = db.conn.execute("SELECT * FROM reviews WHERE status = 'open' ORDER BY day DESC LIMIT 1").fetchone()
    if not r or now - datetime.fromisoformat(r["opened_at"]) > OPEN_FOR:
        return None
    return {**dict(r), "layers": r["layers"].split(",")}


def _split(part: str) -> list:
    return [x.strip() for x in re.split(r"[；]", part or "") if x.strip() and x.strip() not in ("无", "没有")]


def note(db, text: str, today: date) -> list:
    """从 小伴 的话里记下复盘结果；周常“财务复盘”“周日复盘”顺手打卡。返回记下的那次复盘（{"day", "layers", …}）。"""
    hits = MARK.findall(text)
    if not hits:
        return []
    cur = current(db)
    if not cur:
        return []
    parts = [p.strip() for p in re.split(r"[|｜]", hits[-1])]
    summary, points, changes = (parts + ["", "", ""])[:3]
    db.conn.execute("UPDATE reviews SET status = 'done', summary = ?, points = ?, changes = ?, done_at = ? WHERE id = ?",
                    (summary[:200], json.dumps(_split(points), ensure_ascii=False), json.dumps(_split(changes), ensure_ascii=False),
                     datetime.now(timezone.utc).isoformat(timespec="seconds"), cur["id"]))
    from . import transitions
    transitions.record(db, "review", cur["id"], "open", "done", {})
    db.conn.commit()
    try:  # ontology-plan 4.2：她在月度复盘里过的候选题（“候选题 #3 收”“候选题 #4 不要”）
        from . import upgrades
        for cid, verdict in re.findall(r"候选题\s*#(\d+)\s*(收|不要)", changes):
            upgrades.decide(db, int(cid), "accepted" if verdict == "收" else "rejected", "月度复盘")
    except Exception:
        pass
    from . import quests
    for title in HABITS:
        h = quests.habit_by_title(db, title)
        if h and not quests.checked(db, h["id"], today):
            quests.check_habit(db, h["id"], today)
    return [cur]


def strip(text: str) -> str:
    return MARK.sub("", text)


def recent(db, limit: int = 30) -> list:
    """工作台“复盘记录”：最新的在前。"""
    ensure(db)
    out = []
    for r in db.conn.execute("SELECT * FROM reviews ORDER BY day DESC LIMIT ?", (limit,)):
        out.append({"day": r["day"], "layers": [LAYER_NAME[x] for x in r["layers"].split(",") if x in LAYER_NAME],
                    "status": r["status"], "summary": r["summary"] or "",
                    "points": json.loads(r["points"] or "[]"), "changes": json.loads(r["changes"] or "[]")})
    return out


def chat_text(cur: dict) -> str:
    """小伴 聊天时的规矩（正在复盘时才放进去）。"""
    names = "、".join(LAYER_NAME[x] for x in cur["layers"])
    return (f"### 正在做的复盘（{cur['day']}，{names}）\n"
            "你已经把数和单子发给她了。她在聊天里一题一题答：每题接住她的话，说你的看法，不清楚的追问一两句，别一下子全问完。"
            "答完以后，把要改的列出来（挪哪个截止日、改哪项频次、加减哪件事），一件一句，等她点头才动手（用你平时的标记改待办）；"
            "下周最重要的三件定下来，加 [下周重点 定 第一件 | 第二件 | 第三件]。"
            "最后收尾那条回复的末尾加 [复盘记录 一句话小结 | 要点；要点；要点 | 定下的调整；调整]（没有调整写“无”），"
            "工作台的复盘记录就是这一条，周常“财务复盘”“周日复盘”会自动打卡。她说今天先到这儿，也照样收尾写上。")


# ---------- 开场 ----------
def _month_facts(db, day: date) -> dict:
    from . import finance, quests
    first = day.replace(day=1)
    prev_end = first - timedelta(days=1)
    prev_first = prev_end.replace(day=1)

    def pts(a, b):
        return db.conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points WHERE day >= ? AND day <= ? AND delta > 0",
                               (a.isoformat(), b.isoformat())).fetchone()[0]

    def train(a, b):
        h = quests.habit_by_title(db, "训练")
        return db.conn.execute("SELECT COUNT(DISTINCT day) FROM habit_log WHERE habit_id = ? AND day >= ? AND day <= ?",
                               (h["id"], a.isoformat(), b.isoformat())).fetchone()[0] if h else None
    ms = [dict(r) for r in db.conn.execute(
        "SELECT m.title, m.due, m.status, g.title AS goal FROM milestones m JOIN goals g ON g.id = m.goal_id "
        "WHERE m.due >= ? AND m.due <= ? ORDER BY m.due", (first.isoformat(), (first + timedelta(days=62)).isoformat()))] \
        if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'milestones'").fetchone() else []
    return {"这个月得分": pts(first, day), "上个月得分": pts(prev_first, prev_end),
            "这个月训练天数": train(first, day), "上个月训练天数": train(prev_first, prev_end),
            "这个月开销": finance.spend_between(db, first, day), "上个月开销": finance.spend_between(db, prev_first, prev_end),
            "这个月和下个月到期的里程碑": [f"{m['goal'].split('：')[0]} · {m['title']}（{m['due'][5:]}，{ {'open': '进行中', 'done': '达成', 'missed': '没达成'}[m['status']] }）" for m in ms]}


def _season_facts(db, settings, day: date) -> dict:
    from . import season
    s, start = season.current(settings)
    p = season.page(db, s, start, day)
    return {"赛季": s["name"], "小伴 对这季的结论": p.get("line"),
            "七块领域（开季 → 现在）": [f"{d['name']} {d.get('start')} → {d.get('now')}：{d.get('why') or ''}" for d in p.get("domains", [])],
            "下个赛季的建议": p.get("next"), "每个副本的结果和她的复盘": _season_results(db, start)}


def _season_results(db, start) -> list:
    """第 10 题走 onto_query.season_results（ontology-plan 3.1）：她没复盘过的只给结果。"""
    try:
        from . import onto_query
        got = onto_query.season_results(db, "review", since=start.isoformat())
    except Exception:
        return []
    return [{"副本": g["title"], "状态": g["status"], "她的复盘": [r["review"][:200] for r in g["her_review"]] or g["note"]} for g in got["items"]]


def opener_prompt(persona: str, store, settings, day: date, lays: list) -> str:
    from .core.agent import build_system_prompt
    from .report import collect_facts
    ctx = store.load_context(today=day, recent_messages=6)
    facts = {"这周": collect_facts(store, day)}
    try:  # 你起草好、她还没勾的里程碑复盘：顺着单子一起过（ethan/msreview.py）
        from . import msreview
        if msreview.waiting(store.db):
            facts["等她看的里程碑复盘草稿"] = msreview.waiting(store.db)
    except Exception:
        pass
    try:  # 这周系统新认识了你什么（ontology-plan 3.2）：和“我”页 metrics 同一套函数算（mepage.week_learned）
        from . import mepage
        facts["这周系统新认识了你什么"] = mepage.learned_text(mepage.week_learned(store.db, day))
    except Exception as e:
        facts["这周系统新认识了你什么"] = f"（没算出来：{type(e).__name__}）"
    try:  # ontology-plan 4.1、4.2：每周补满两周的升级数字、收一遍候选题（月度那次才给她过）
        from . import upgrades
        upgrades.fill(store.db, day)
        upgrades.collect(store.db, day)
    except Exception:
        pass
    asks = ["这周最满意的一件事", "卡住或没做成的一件，你觉得原因是什么", "下周最重要的三件事", "有没有想改的目标、频次或截止日"]
    if "month" in lays:
        try:
            facts["这个月"] = _month_facts(store.db, day)
        except Exception as e:
            facts["这个月"] = f"（没算出来：{type(e).__name__}）"
        try:  # ontology-plan 4.2：月度复盘列“候选题”让她过
            from . import upgrades
            facts["候选题"] = upgrades.candidates_text(store.db)
        except Exception as e:
            facts["候选题"] = f"（没算出来：{type(e).__name__}）"
        asks += ["这个月最大的变化是什么", "下个月几个里程碑的截止日要不要调",
                 "“候选题”里这几类老是查不到的，哪些值得以后专门能答（收 / 不要）"]
    if "season" in lays:
        try:
            facts["这个赛季"] = _season_facts(store.db, settings, day)
        except Exception as e:
            facts["这个赛季"] = f"（没算出来：{type(e).__name__}）"
        asks += ["这季每个副本：你自己怎么看，哪些想保留、哪些想停", "下个赛季最想新开的一件事", "下个赛季给自己定一条硬线"]
    names = "、".join(LAYER_NAME[x] for x in lays)
    return "\n\n".join([
        build_system_prompt(persona, ctx),
        f"## 现在是你们约好的周复盘（{names}复盘），你主动开场",
        "这是日历上约好的一小时，像开会也像约会：先一句轻松的开场，然后：\n"
        f"1. 把{names}的数说给她听：每块一两句，先说做得好的，再说差在哪、差多少（用数字），七块领域挨个过，没数据的合成一句带过；"
        + ("这个月的部分和上个月比；" if "month" in lays else "")
        + ("赛季部分说清每个副本达成没有、领域之间谁挤了谁，最后说你对下个赛季的初步想法；" if "season" in lays else "")
        + "\n   然后单独一段“这周系统新认识了你什么”：照“这周系统新认识了你什么”那份底稿说，新记下几条（按来源）、念 3–5 条原文、"
        "被纠正几次（记错 / 变了）举一例、哪几道题从答不上变成答得上（底稿说还没有对比数据就照说）；数字原样用，不改不估。"
        + ("\n   月度部分单独一段“候选题”：照“候选题”那份底稿念，每道说清是哪类查不到、几次，请她说收还是不要；"
           "她定了就在收尾的 [复盘记录 …] 调整里写“候选题 #编号 收”或“候选题 #编号 不要”。" if "month" in lays else "")
        + "\n2. 给她一张单子，编号列出要她回答的题，就用下面这几题（可以按这周的实际情况把题说具体）：\n"
        + "\n".join(f"   {i}. {q}" for i, q in enumerate(asks, 1))
        + "\n3. 告诉她直接在聊天里回就行，一次答完或一题一题答都可以，打字语音都行。",
        "只用下面的事实，没有的不要编。这次不用 [VOICE]、[REACT]、[不回]，也不写 [下周重点 …] 和 [复盘记录 …]（等她答完再写）。"
        f"不超过 {34 if 'season' in lays else 28} 行。",
        "### 事实",
        json.dumps(facts, ensure_ascii=False, indent=1, default=str),
    ])


def opener(ethan, day: date, lays: list) -> str:
    """写开场并记进原始对话（kind=review），返回要发的文字。"""
    from .report import MAX_TELEGRAM
    system = opener_prompt(ethan.persona, ethan.store, ethan.router.settings, day, lays)
    reply = ethan.router.complete(system, [{"role": "user", "content": "（到点了，现在开始这周的复盘）"}], purpose="week-review")
    text = strip(reply.text.strip())[:MAX_TELEGRAM]
    ethan.store.raw.append("telegram", "ethan", text, backend=reply.backend, model=reply.model, kind="review")
    open_review(ethan.store.db, day, lays)
    return text
