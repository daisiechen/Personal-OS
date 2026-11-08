"""能力题做成的查询函数（迁移第 4 步，docs/design/capability-questions.md 第 5 节的合同）。

首批 5 题：第 1 题（在做什么、住哪、今天在哪）、第 4 题（偏好）、第 9 题（定死的安排）、第 12 题（准备时的要求）、
第 27 题（删过、改过、否掉过）。ontology-plan 3.1（11-04）补齐其余首版题（第 20、21 题除外），题号 → 函数见文末 QUESTIONS。
规则以 docs/design/ontology-contracts.md 为准。

共同约定：
- **只读**（只记一笔使用记录），读新结构的说法（statements），不读旧 facts；
- 每次调用都要说是谁（ethan/memusers.py 登记表），按 `memusers.may_read` 和可见级别把关，记忆暂停中只给她本人和系统维护；
- 返回统一格式：{"status": 有答案 answer / 有争议 disputed / 不知道 unknown / 来源不可用 unavailable / 没权限 forbidden,
  "items": [...], "note": 说明（对外的第 14、15 题还有 ask = 这一类要先问她）, "q": 题号, "basis": 依据的说法编号, "kinds": 来源类型, "at": 查的时间, "missing", "disputed"}。
  没权限时谁看到的都是 unknown，不暴露有没有（真实结果只记在 function_log）；
- 数据里有矛盾、如实并列返回，就是答对了（status = disputed）。

谁在用（11-05 按代码核对）：计划器、准备员、小伴 11-03 都已在服务器上按开关（reading_new，数据库里记的，默认没切；可单独切回旧路径）切到新结构，
读偏好、删改历史；准备员、计划器还按题读要求（第 12 题）和决定（第 22 题）；小伴 聊天里命中能力题的经 ethan/askroute.py 走函数；
每周复盘（review）、邮件起草（mail）、巡检（patrol）、正式评估（ethan/formaleval.py）、作息行为整理（lifebase.rhythm_items）直接走函数；
“我”页和升级记录读 function_log 算“按题答 vs 兜底”。切换前的四道关见 gates()。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

from . import lifebase, memusers, ontology
from . import clock

LOG = """CREATE TABLE IF NOT EXISTS function_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, fn TEXT NOT NULL,
  user TEXT NOT NULL, status TEXT NOT NULL, n INTEGER NOT NULL)"""
HER_OWN = ("her_words", "her_confirmation")  # 她说的、她确认的


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _out(db, fn, user, status, items=(), note=""):
    who = memusers.check(user)
    if not memusers.may_use(user, fn):
        status, items, note = "forbidden", (), ""  # 只登记了读某几项的（日记只读写作规范）
    shown = "unknown" if status == "forbidden" else status  # 没权限和没有一样回答（contracts 1.6 节），真实情况只记在使用记录里
    try:
        db.conn.execute(LOG)
        db.conn.execute("INSERT INTO function_log (at, fn, user, status, n) VALUES (?, ?, ?, ?, ?)", (_now(), fn, user, status, len(items)))
        db.conn.commit()
    except Exception:
        pass
    if shown != status:  # 没权限：和“没有”一样，不给条目也不给说明
        return {"status": shown, "items": [], "note": "", **_meta(fn, shown, [])}
    items = list(items)
    return {"status": status, "items": items, "note": note, **_meta(fn, status, items)}


def _meta(fn, status, items) -> dict:
    """每道题统一带上（ontology-plan 3.1）：题号、依据的说法编号、来源类型、什么时候查的、是不是缺失 / 有争议。
    没权限对调用方显示成缺失（contracts 1.6 节：不暴露有没有），真实情况只在 function_log。"""
    basis, kinds = set(), set()
    for i in items:
        if isinstance(i, dict):
            for k in ("statement", "before_statement"):
                if isinstance(i.get(k), int):
                    basis.add(i[k])
            k = i.get("said_by") or i.get("source") or i.get("from")
            if isinstance(k, str):
                kinds.add(k)
    return {"q": QUESTION_OF.get(fn), "basis": sorted(basis), "kinds": sorted(kinds), "at": _now(),
            "missing": status in ("unknown", "unavailable"), "disputed": status == "disputed"}


def _gate(db, user):
    """登记过才能用；记忆暂停中只给她本人和系统维护。返回 None 表示放行，否则返回拒绝的状态。"""
    who = memusers.check(user)
    if lifebase.on_hold(db) and not who.get("all"):
        return "unavailable"
    return None


SENSITIVE = {p[0]: p[6] for p in ontology.PROPERTIES if p[6]}


def _visible(user: str, row, db=None) -> bool:
    who = memusers.check(user)
    if db is not None and lifebase.fact_state(row, None, db) in ("suspended", "forgotten", "retracted"):  # 删后停用的（Codex 评审 11-05）
        return False
    if memusers.external(user):  # 对外两档：按对外开关和逐条例外（lifebase.disclose，ontology-plan 3.3），不按可见级别
        return db is not None and lifebase.disclose(db, user, row) == "allow"
    allowed = lifebase.LEVELS[lifebase.LEVELS.index(who["level"]):]
    if (row["visibility"] or "private") not in allowed:
        return False
    prop = row["property"] or ""
    # 细属性按登记的敏感标签算方面（1.11 把 dim:resources 的归到 application_status 后，对外那道双保险不能丢）
    dim = prop.split(":", 1)[1] if prop.startswith("dim:") else SENSITIVE.get(prop)
    return memusers.may_read(user, row["kind"], row["read_by"], dim)


def _match(text: str, words: str) -> bool:
    if not words:
        return True
    from .memsearch import tokens
    want = set(tokens(words, query=True))
    return bool(want & set(tokens(text or ""))) if want else True


def _sources(db, sid) -> list:
    return [{"evidence": r["evidence_id"], "unknown": bool(r["source_unknown"])}
            for r in db.conn.execute("SELECT evidence_id, source_unknown FROM statement_sources WHERE statement_id = ?", (sid,))]


def _item(db, r) -> dict:
    return {"statement": r["id"], "value": r["value_text"], "context": r["context"], "said_by": r["said_by"], "kind": r["kind"],
            "valid_from": r["valid_from"], "valid_to": r["valid_to"], "review": r["review"], "locked": bool(r["locked"]),
            "guess": r["kind"] == "hypothesis", "sources": _sources(db, r["id"])}


def _self_statements(db, where: str = "1 = 1", args=(), view: str = "topic", day=None) -> list:
    """关于她的说法里，这个场景收的（lifebase.MEM_VIEWS[view]，“算不算数”只在 lifebase 一处判）。"""
    lifebase.ensure(db)
    sid = ontology.self_id(db)
    cond, cargs = lifebase.mem_where(view, "statements", day)
    return db.conn.execute(f"SELECT * FROM statements WHERE subject_id = ? AND {cond} AND ({where})", (sid, *cargs, *args)).fetchall()


# ---------- 第 4 题：偏好 ----------
def preferences(db, user: str, topic: str = None, today: date = None) -> dict:
    """她在某类事上的偏好、什么条件下成立。只算她说过或确认过的；系统的猜测只给 小伴（标“猜测”）。候选只给 小伴 看。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "preferences", user, blocked)
    day = (today or lifebase_today()).isoformat()
    rows = _self_statements(db, "(property IN ('dim:preferences', 'preference') OR kind = 'preference') AND property != 'writing_style'", day=day)  # 写作规范走 writing_style
    who = memusers.check(user)
    items, hidden = [], 0
    for r in rows:
        if not _match(f"{r['value_text']} {r['context'] or ''}", topic):
            continue
        if not _visible(user, r, db):
            hidden += 1
            continue
        if r["review"] == "candidate" and user != "ethan" and not who.get("all"):
            continue
        if r["kind"] != "hypothesis" and r["said_by"] not in HER_OWN and r["review"] != "accepted":
            continue
        items.append(_item(db, r))
    if not items:
        return _out(db, "preferences", user, "forbidden" if hidden else "unknown", note="没找到她说过或确认过的偏好")
    status = "disputed" if any(i["review"] == "disputed" for i in items) else "answer"
    return _out(db, "preferences", user, status, items)


# ---------- 第 12 题：替她准备某件事时的要求和禁忌 ----------
def prep_requirements(db, user: str, task_title: str, today: date = None) -> dict:
    """只返回她说过、确认过的要求和禁忌（不返回能不能替她做，那是授权）；准备员不读系统的猜测。"""
    blocked = _gate(db, user)
    if not blocked and memusers.external(user):
        blocked = "forbidden"  # 替她办事的要求和禁忌只给自己人（正式评估 11-05 发现对外试探能拿到）
    if blocked:
        return _out(db, "prep_requirements", user, blocked)
    day = (today or lifebase_today()).isoformat()
    rows = _self_statements(db, "kind != 'hypothesis'", view="now", day=day)
    items = [_item(db, r) for r in rows if r["said_by"] in HER_OWN and _visible(user, r, db)
             and _match(f"{r['value_text']} {r['context'] or ''}", task_title)]
    past = history(db, user, task_title, _log=False)
    note = "" if items else "没找到已确认的要求，按默认规则做，并在结果里写明"
    if past["items"]:
        note = (note + "；" if note else "") + "这件事有删改记录，先看 history"
    if not items:
        return _out(db, "prep_requirements", user, "unknown", note=note)
    return _out(db, "prep_requirements", user, "disputed" if any(i["review"] == "disputed" for i in items) else "answer", items, note)


# ---------- 第 27 题：这件事她删过、改过、否掉过吗 ----------
def history(db, user: str, title: str, _log: bool = True) -> dict:
    """三种分开返回：删过（待办）、改过（改名、改时间）、否掉过（计划里她删掉的日程）。说法相近但不确定的标“可能是同一件”。"""
    blocked = _gate(db, user)
    if not blocked and memusers.check(user)["level"] == "public":
        blocked = "forbidden"  # 她删改过什么是她自己的事，不对外（3.1 补的没权限一档）
    if blocked:
        return _out(db, "history", user, blocked) if _log else {"status": "unknown" if blocked == "forbidden" else blocked, "items": [], "note": ""}
    from . import locks
    from .core.agent import find_similar
    locks.ensure(db)
    items = []
    for r in db.conn.execute("SELECT obj, field, value, title, at, day FROM manual_locks WHERE released_at IS NULL AND title IS NOT NULL"):
        if not r["title"]:
            continue
        same = r["title"] == title
        near = not same and find_similar(title, [r["title"]]) is not None
        if not (same or near):
            continue
        what = {"deleted": "删过", "title": "改过名", "time": "改过时间"}.get(r["field"], "改过")
        items.append({"what": what, "title": r["title"], "at": r["at"], "sure": same, "note": "" if same else "可能是同一件，问她"})
    try:
        for r in db.conn.execute("SELECT day, title, action, at FROM plan_overrides WHERE action = 'delete'"):
            same = r["title"] == title
            if same or find_similar(title, [r["title"]]) is not None:
                items.append({"what": "否掉过（计划里删了）", "title": r["title"], "at": r["at"], "day": r["day"], "sure": same,
                              "note": "" if same else "可能是同一件，问她"})
    except Exception:
        pass
    if not _log:
        return {"status": "answer" if items else "unknown", "items": items, "note": ""}
    if not items:
        return _out(db, "history", user, "unknown", note="没有记录")
    return _out(db, "history", user, "answer", items)


# ---------- 第 9 题：某天她有哪些定死的安排 ----------
def fixed_schedule(db, user: str, day: date, calendar=None) -> dict:
    """日历里的事 + 她在工作台改过、锁住的日程；计划器自己排的不算。日历读不到就是“来源不可用”，不当成没安排。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "fixed_schedule", user, blocked)
    if memusers.check(user)["level"] == "public":
        return _out(db, "fixed_schedule", user, "forbidden")
    if calendar is None:
        return _out(db, "fixed_schedule", user, "unavailable", note="日历没接上，读不到不等于没安排")
    try:
        busy = calendar.busy(day)
    except Exception:
        return _out(db, "fixed_schedule", user, "unavailable", note="日历这次读不到，不等于没安排")
    from . import locks
    locks.ensure(db)
    pinned = {r["obj"].split(":", 2)[2]: json.loads(r["value"] or "null")
              for r in db.conn.execute("SELECT obj, value FROM manual_locks WHERE obj LIKE ? AND field = 'time' AND released_at IS NULL",
                                       (f"event:{day.isoformat()}:%",))}
    items = [{"start": b[0], "end": b[1], "title": b[2], "from": "日历", "locked": b[2] in pinned} for b in busy]
    for title, value in pinned.items():
        if not any(i["title"] == title for i in items):
            items.append({"start": (value or {}).get("start"), "end": (value or {}).get("end"), "title": title,
                          "from": "她改过的日程", "locked": True})
    items.sort(key=lambda i: i["start"] or "")
    overlap = any(a["end"] and b["start"] and a["end"] > b["start"] for a, b in zip(items, items[1:]))
    return _out(db, "fixed_schedule", user, "answer" if items else "unknown", items, "有时间重叠，并列给出" if overlap else "")


# ---------- 第 1 题：在做什么、住哪、今天在哪 ----------
def whereabouts_now(db, user: str, today: date = None) -> dict:
    """三件事分开答，不互相推：在做什么（此刻成立的情境）、住在哪个城市、今天在哪（只看当天她说的）。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "whereabouts_now", user, blocked)
    if memusers.check(user)["level"] == "public":
        return _out(db, "whereabouts_now", user, "forbidden")
    day = (today or lifebase_today()).isoformat()
    ontology.ensure(db)
    me = ontology.self_id(db)
    now_sql, now_args = lifebase.mem_where("now", "statements", day)
    gone_sql, gone_args = lifebase.mem_where("situation_gone", "statements", day)
    doing = []
    for s in db.conn.execute("SELECT DISTINCT r.situation_id, o.name FROM situation_roles r JOIN objects o ON o.id = r.situation_id "
                             "WHERE r.object_id = ? AND o.status = 'active'", (me,)):
        if db.conn.execute(f"SELECT 1 FROM statements WHERE subject_id = ? AND {gone_sql}", (s["situation_id"], *gone_args)).fetchone():
            continue  # 起止有她删掉的来源：整个情境不答（Codex 评审 11-07 第 3 条）
        ends = {p: [x for x in db.conn.execute(f"SELECT * FROM statements WHERE subject_id = ? AND property = ? AND {now_sql}",
                                               (s["situation_id"], p, *now_args))] for p in ("situation_start", "situation_end")}
        start = [x["value_text"] for x in ends["situation_start"]]
        end = [x["value_text"] for x in ends["situation_end"]]
        if (not start or min(start) <= day) and (not end or max(end) >= day):
            doing.append({"what": s["name"], "start": start, "end": end, "disputed": len(set(end)) > 1})
    lives = [_item(db, r) for r in _self_statements(db, "property = 'lives_in'", view="now", day=day) if _visible(user, r, db)]
    from . import whereabouts
    here = whereabouts.current(db)
    today_city = here["city"] if here["source"] == "told" and (here["at"] or "")[:10] == day else None
    items = [{"part": "在做什么", "value": doing or None},
             {"part": "住在哪个城市", "value": lives or None, "default": None if lives else whereabouts.home_city()},
             {"part": "今天在哪", "value": today_city}]
    if not doing and not lives and not today_city:
        return _out(db, "whereabouts_now", user, "unknown", items, "三项都没有当前的记录（常驻地只是设置里的默认值）")
    disputed = any(d["disputed"] for d in doing) or len({str(x["value"]) for x in lives}) > 1
    return _out(db, "whereabouts_now", user, "disputed" if disputed else "answer", items,
                "" if today_city else "今天在哪没有当天的记录，不从住处推")


def lifebase_today() -> date:
    from .clock import her_today
    return her_today()


# ---------- 她在哪读过书、做过什么（ontology-plan 1.2 要改善的题） ----------
def background(db, user: str) -> dict:
    """经历：就读、任职、做过的项目、技能。都是过去的经历照样算（不按“现在成立”筛），出处一起给；
    来自简历这类她提供的资料的，标“她提供的资料”，不当成她的原话。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "background", user, blocked)
    marks = ",".join("?" * len(ontology.BACKGROUND))
    rows = _self_statements(db, f"property IN ({marks})", ontology.BACKGROUND, view="background")
    items, hidden = [], 0
    for r in rows:
        if not _visible(user, r, db):
            hidden += 1
            continue
        items.append({**_item(db, r), "property": r["property"]})
    if not items:
        return _out(db, "background", user, "forbidden" if hidden else "unknown", note="还没有经历方面的记录")
    order = {k: i for i, k in enumerate(ontology.BACKGROUND)}
    items.sort(key=lambda i: (order[i["property"]], i["valid_from"] or ""))
    return _out(db, "background", user, "disputed" if any(i["review"] == "disputed" for i in items) else "answer", items)


# ---------- 第 26 题：这周花了多少、和前几周比、哪几笔最大（ontology-plan 1.5） ----------
def weekly_spend(db, user: str, today: date = None, weeks: int = 4) -> dict:
    """从银行流水现算（行为记录，不存成说法）：本周（周一到今天）各币种、折成人民币的总数、记了几天、最大的三笔，
    前几周的总数一起给。记录不全的照样报，标清“这周记录不完整”（Codex 评审：不统一等满七天）。钱是敏感方面，对外的拿不到。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "weekly_spend", user, blocked)
    if memusers.check(user)["level"] == "public":
        return _out(db, "weekly_spend", user, "forbidden")
    from datetime import timedelta
    from . import finance
    day = today or lifebase_today()
    rate = finance.fx_rate(db)
    finance.ensure(db)

    def week(end: date) -> dict:
        start = clock.week_start(end)
        spent = finance.spend_between(db, start, end)
        days = db.conn.execute("SELECT COUNT(DISTINCT day) FROM transactions WHERE day >= ? AND day <= ? "
                               "AND COALESCE(category, '') NOT IN ('unparsed', 'ignored')", (start.isoformat(), end.isoformat())).fetchone()[0]
        return {"from": start.isoformat(), "to": end.isoformat(), "by_currency": spent, "cny": round(finance.convert(spent, "CNY", rate), 2),
                "days_recorded": days, "days": (end - start).days + 1}

    this = week(day)
    top = [dict(r) for r in db.conn.execute(
        "SELECT day, merchant, amount, currency FROM transactions WHERE day >= ? AND day <= ? AND amount < 0 "
        "AND COALESCE(category, '') NOT IN ('transfers', 'savings', 'unparsed', 'ignored') ORDER BY amount LIMIT 3",
        (this["from"], this["to"]))]
    past = [week(date.fromisoformat(this["from"]) - timedelta(days=1 + 7 * i)) for i in range(weeks - 1)]
    note = "" if this["days_recorded"] >= this["days"] else f"这周记录不完整：{this['days']} 天里有 {this['days_recorded']} 天有流水"
    if not this["by_currency"] and not this["days_recorded"]:
        return _out(db, "weekly_spend", user, "unknown", [{"this_week": this, "past": past}], "这周还没有流水记录（不等于没花钱）")
    return _out(db, "weekly_spend", user, "answer", [{"this_week": this, "top": top, "past": past, "fx": rate}], note)


# ---------- 作息的描述（行为记录，现算，不推性格；ontology-plan 1.5） ----------
RHYTHM_MIN_SAMPLES = 3  # 少于 3 天不写说法
DAY_START_MIN = clock.DAY_START_HOUR * 60  # 05:00 以前的不算“开始”，0–5 点算前一晚（决定 9：和工作时长一样按 5 点分天；以前 4 点）
IDLE_GAP_MIN = 4 * 60  # 前面空了 4 小时以上才算新的一段


def mac_sessions(db, start: date, day: date):
    """只看 Mac 来源的格（不算工作台、Claude 会话、手机），按“空 4 小时以上”切成一段一段。
    返回 (工作日每天的开始分钟数列表, 每晚最后一格的分钟数列表——相对那晚的 0 点，过了午夜的大于 1440)。
    开始 = 当天 05:00 以后、前面空了 4 小时以上的第一格；跨午夜连着的不算开始。
    熬夜 = 一段的最后一格，往前推 5 小时算哪一晚（04:30 算前一晚；clock.DAY_START_HOUR），每晚取最后的一段；后面空了 4 小时还没确认的不算。"""
    from datetime import datetime, timedelta
    try:
        rows = [r[0] for r in db.conn.execute(
            "SELECT slot FROM work_slots WHERE source = 'mac' AND substr(slot, 1, 10) >= ? AND substr(slot, 1, 10) < ? ORDER BY slot",
            ((start - timedelta(days=1)).isoformat(), day.isoformat()))]
    except Exception:
        return [], []
    times = []
    for s in rows:
        try:
            times.append(datetime.strptime(s[:16], "%Y-%m-%dT%H:%M"))
        except ValueError:
            continue
    gap = timedelta(minutes=IDLE_GAP_MIN)
    end_of_data = datetime.combine(day, datetime.min.time())
    starts, ends = {}, {}
    for i, t in enumerate(times):
        if i == 0 or t - times[i - 1] >= gap:  # 一段的开头（窗口前一天也查了，第一格前面确实空着）
            d = t.date()
            if start <= d and d.weekday() < 5 and t.hour * 60 + t.minute >= DAY_START_MIN and d not in starts:
                starts[d] = t.hour * 60 + t.minute
        nxt = times[i + 1] if i + 1 < len(times) else None
        if (nxt and nxt - t >= gap) or (nxt is None and end_of_data - t >= gap):  # 一段的结尾，后面确实空了 4 小时
            night = (t - timedelta(minutes=DAY_START_MIN)).date()
            if start <= night:
                ends[night] = int((t - datetime.combine(night, datetime.min.time())).total_seconds() // 60)
    return list(starts.values()), list(ends.values())


def rhythm_items(db, day: date, weeks: int = 4) -> list:
    """rhythm 的计算本身（不查角色）：给每周整理成说法的 ethan/behavior.py 共用。"""
    from datetime import timedelta
    start = day - timedelta(days=7 * weeks)
    items = []
    starts, nights = mac_sessions(db, start, day)
    if len(starts) >= RHYTHM_MIN_SAMPLES:  # 样本不够不报（服务器 11-04：5 个工作日里 3 天的“开始”其实是熬夜过了午夜）
        mins = sorted(starts)
        mid = mins[len(mins) // 2]
        items.append({"key": "work_start", "what": "工作日开始用 Mac 的时间（中位数）", "value": f"{mid // 60:02d}:{mid % 60:02d}", "samples": len(mins),
                      "note": "Mac 有键鼠动静、前面空了 4 小时以上的第一个 5 分钟（05:00 以后，跨午夜连着的不算），不等于开始工作"})
    if len(nights) >= RHYTHM_MIN_SAMPLES:
        mins = sorted(nights)
        mid = mins[len(mins) // 2] % (24 * 60)
        items.append({"key": "sleep_late", "what": "晚上最后用 Mac 的时间（中位数）", "value": f"{mid // 60:02d}:{mid % 60:02d}", "samples": len(mins),
                      "note": "每晚 Mac 最后一个有键鼠动静的 5 分钟（过了午夜的算前一晚），不等于睡觉时间"})
    try:
        outs = db.conn.execute("SELECT COUNT(DISTINCT substr(at, 1, 10)) FROM place_events WHERE event = 'leave' AND substr(at, 1, 10) >= ? "
                               "AND substr(at, 1, 10) < ?", (start.isoformat(), day.isoformat())).fetchone()[0]
    except Exception:
        outs = 0
    if outs:
        items.append({"key": "outings_per_week", "what": "每周出门的天数（平均）", "value": round(outs / weeks, 1), "samples": outs,
                      "note": "按 iPhone 报的“离开家”算，出门不等于去做了什么"})
    try:  # 日历只算“安排”（S13）：去没去要有别的证据
        plans = db.conn.execute("SELECT COUNT(*) FROM future_events WHERE day >= ? AND day < ?", (start.isoformat(), day.isoformat())).fetchone()[0]
    except Exception:
        plans = 0
    if plans:
        items.append({"key": "calendar_per_week", "what": "日历上每周的安排（平均）", "value": round(plans / weeks, 1), "samples": plans,
                      "note": "日历上的安排，不等于真的去了"})
    edits = 0  # 她在工作台上亲手动过的（S16）：锁住、改计划、删待办
    for sql in ("SELECT COUNT(*) FROM manual_locks WHERE substr(at, 1, 10) >= ? AND substr(at, 1, 10) < ?",
                "SELECT COUNT(*) FROM plan_overrides WHERE substr(at, 1, 10) >= ? AND substr(at, 1, 10) < ?",
                "SELECT COUNT(*) FROM deleted_tasks WHERE substr(at, 1, 10) >= ? AND substr(at, 1, 10) < ?"):
        try:
            edits += db.conn.execute(sql, (start.isoformat(), day.isoformat())).fetchone()[0]
        except Exception:
            pass
    if edits:
        items.append({"key": "workbench_edits", "what": "在工作台上亲手改过的次数", "value": edits, "samples": edits,
                      "note": "只是次数，改了什么、为什么改另看改动记录"})
    return items


def rhythm(db, user: str, today: date = None, weeks: int = 4) -> dict:
    """最近几周：工作日一般几点开始用 Mac（空了 4 小时以上之后的第一格）、晚上一般用到几点、每周出门几天。带样本数；没装的来源说没有。
    Mac 有动静不等于开始工作，出门不等于去做了什么——只描述，不下结论。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "rhythm", user, blocked)
    if memusers.check(user)["level"] == "public":
        return _out(db, "rhythm", user, "forbidden")
    items = rhythm_items(db, today or lifebase_today(), weeks)
    if not items:
        return _out(db, "rhythm", user, "unknown", note="最近几周没有 Mac 和手机定位的记录")
    return _out(db, "rhythm", user, "answer", items, f"最近 {weeks} 周")


# ---------- 系统上次做了什么、她怎么反应的（ontology-plan 1.6，运行记录；不是关于她的证据） ----------
def system_did(db, user: str, kind: str = None, limit: int = 10) -> dict:
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "system_did", user, blocked)
    if memusers.check(user)["level"] == "public":
        return _out(db, "system_did", user, "forbidden")
    from . import runlog
    rows = runlog.recent(db, kind, limit)
    if not rows:
        return _out(db, "system_did", user, "unknown", note="还没有运行记录")
    items = [{"at": r["at"][:16], "what": runlog.KINDS.get(r["kind"], r["kind"]), "title": r["title"],
              "reaction": runlog.REACTIONS.get(r["reaction"] or "", "还没反应"), "note": r["note"] or ""} for r in rows]
    return _out(db, "system_did", user, "answer", items, "系统自己的输出，不是关于她的证据")


# =====================================================================================================
# ontology-plan 3.1（11-04）：其余首版题（第 20、21 题除外）。返回格式和上面一样，_out 统一补上题号、依据的说法编号、来源类型、时间。
# =====================================================================================================
HER_SOURCES = ("chat", "interview", "wechat", "recording", "workbench", "sent", "social", "correction")  # 她自己说的、写的
RELAY = ("grok", "agent")
# 决定表里的角色（ethan/decisions.py ROLES）↔ 登记表里的使用者
ROLE = {"planner": "planner", "prep": "prep", "ethan": "ethan", "mail": "ethan", "twin": "dais", "site": "dais",
        "patrol": "patrol", "review": "maintainer", "system": "maintainer", "me_page": "maintainer"}


def _hit(text: str, words: str, need: float = 0.5) -> bool:
    """查询词的片段至少有一半出现在这段话里（_match 是“有一个就算”，查原话时太松）。"""
    if not words:
        return True
    from .memsearch import tokens
    want = set(tokens(words, query=True))
    if not want:
        return (words or "") in (text or "")
    have = set(tokens(text or ""))
    return len(want & have) >= max(1, round(len(want) * need))


def _private_only(db, fn, user):
    """对外的（Days、主页）一律没权限；放行返回 None。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, fn, user, blocked)
    if memusers.check(user)["level"] == "public" or memusers.external(user):
        return _out(db, fn, user, "forbidden")
    return None


def _her_only(db, fn, user, also=()):
    """只给她本人看的（“我”页、系统维护），also 里的使用者也行。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, fn, user, blocked)
    if not memusers.check(user).get("all") and user not in also:
        return _out(db, fn, user, "forbidden")
    return None


def _rows(db, sql, args=()):
    try:
        return [dict(r) for r in db.conn.execute(sql, args)]
    except Exception:
        return []  # 表还没建（那个模块没用过）：当成没有记录


_monday = clock.week_start  # 本周一只在 clock 算（11-06）


# ---------- 第 2 题：她上次说到某件事是什么时候、原话、当时什么场合 ----------
def _her_lines(source: str, text: str):
    """这条原始记录里她说的话，最近的在前。整段对话（一行一句标【她】/【某某】，微信、Grok、Codex 的交接，一份常有上万字）
    只取她的那几轮逐轮比：原来整份一起比，长对话几乎什么话题都“命中”，最近 3 条全被它占满、又只截前 300 字，
    真正说到这件事的那句排不进来（11-07 正式评估“知识更新”F 4/15 对 R 13/15，服务器只读诊断查到）；别人说的那几行也不算她的原话。
    按说话轮次切：不以【某某】开头的行是上一轮的续行（导出时保留了消息里的换行，Codex 评审 11-07）。
    普通聊天（source = chat，一条一句“mia：…”）照旧整条比，里面引用了带【她】的文字也不当整段对话。"""
    if source != "chat" and re.search(r"^【她】", text, re.M) and not re.search(r"^答：", text, re.M):
        turns, who = [], None
        for line in text.splitlines():
            m = re.match(r"^【([^】]+)】(.*)$", line)
            if m:
                who = m.group(1)
                turns.append([who, m.group(2).strip()])
            elif re.match(r"^〔[^〕]*〕\s*$", line):
                who = None  # 新的一天
            elif who is not None and line.strip():
                turns[-1][1] += "\n" + line.strip()
        for who, said in reversed(turns):
            if who == "她" and said:
                yield said
        return
    yield text.split("：", 1)[1] if source == "chat" and "：" in text[:10] else text


def last_said(db, user: str, topic: str, limit: int = 3) -> dict:
    """只看她自己说的、写的原始记录（删掉的不算），最近的在前；自述整理成说法时记了语境（玩笑、气话、场合）的一起给。"""
    no = _private_only(db, "last_said", user)
    if no:
        return no
    lifebase.ensure(db)
    marks = ",".join("?" * len(HER_SOURCES))
    rows = _rows(db, f"SELECT id, source, occurred_at, text, visibility FROM evidence WHERE source IN ({marks}) AND deleted_at IS NULL "
                     "AND text IS NOT NULL ORDER BY occurred_at DESC, id DESC LIMIT 3000", HER_SOURCES)
    items = []
    for r in rows:
        text = r["text"] or ""
        if r["source"] == "chat" and text.startswith("ethan："):
            continue  # 聊天里 小伴 说的不算她说的
        quote = next((q for q in _her_lines(r["source"], text) if _hit(q, topic)), None)
        if quote is None:
            continue
        items.append({"evidence": r["id"], "quote": quote[:300], "at": r["occurred_at"], "source": r["source"], "context": None})
        if len(items) >= limit:
            break
    day = lifebase_today().isoformat()
    selfs = [x for x in _self_statements(db, "kind = 'self_report'", view="now", day=day)
             if _visible(user, x, db) and _hit(f"{x['value_text']} {x['context'] or ''}", topic)]
    for x in selfs:  # 自述的语境：按出处对回原话
        for s in _sources(db, x["id"]):
            for i in items:
                if i["evidence"] == s["evidence"] and x["context"]:
                    i["context"], i["statement"] = x["context"], x["id"]
    if not items:
        return _out(db, "last_said", user, "unknown", note="没找到她说过这件事（删掉的不算）")
    disputed = any(x["review"] == "disputed" for x in selfs)
    return _out(db, "last_said", user, "disputed" if disputed else "answer", items,
                "最近的在前；有语境标注的要一起说" + ("；整理出的说法有两种，没定" if disputed else ""))


# ---------- 第 3 题：某人是谁、和她什么关系、最近一件相关的事 ----------
def person(db, user: str, name: str) -> dict:
    """人物卡 + 关于这个人的说法（关系）+ 最近一次见面 / 联系。同名的人分不开时并列，标“先问她是哪位”。别人的事不对外。"""
    no = _private_only(db, "person", user)
    if no:
        return no
    name = (name or "").strip()
    if not name:
        return _out(db, "person", user, "unknown", note="没说是谁")
    cards = _find_people(db, name)
    ontology.ensure(db)
    day = lifebase_today().isoformat()
    items = []
    for c in cards[:5]:
        ref = db.conn.execute("SELECT object_id FROM object_refs WHERE ref = ?", (f"person:{c['id']}",)).fetchone()
        says = []
        if ref:
            now_sql, now_args = lifebase.mem_where("now", "statements", day)
            for s in db.conn.execute(f"SELECT * FROM statements WHERE subject_id = ? AND {now_sql}", (ref[0], *now_args)):
                if _visible(user, s, db):
                    says.append({**_item(db, s), "property": s["property"]})
        last = _rows(db, "SELECT kind, day, text FROM life_log WHERE who = ? ORDER BY day DESC, id DESC LIMIT 1", (c["name"],))
        card = _card_extra(c)  # 每晚整理出的近况、在聊的话题、没了结的事、重要日子（见面前提醒要用，借鉴 10）
        items.append({"person": c["id"], "name": c["name"], "circle": c.get("circle"), "how_met": c.get("how_met"),
                      "notes": (c.get("notes") or "")[:300], "last_contact": c.get("last_contact"), **card,
                      "relation": [x for x in says if x["property"] == "relation_to_her"],
                      "statements": [x for x in says if x["property"] != "relation_to_her"][:10],
                      "mentions": _mentions(db, user, day, {name, _short_name(c["name"])}),
                      "latest": last[0] if last else None})
    if not items:  # 没有人物卡、但她说起过的人（王宇航：“她拉老同事王宇航一起做 KingSail”，11-07 评估试跑）
        said = _mentions(db, user, day, {name})
        if said:
            return _out(db, "person", user, "answer", [{"person": None, "name": name, "mentions": said}], "没有人物卡，下面是她说起过的")
        return _out(db, "person", user, "unknown", note="人物卡里没有这个人")
    if len(items) > 1:
        return _out(db, "person", user, "disputed", items, "同名（或名字相近）的有好几位，先问她是哪位")
    disputed = any(x["review"] == "disputed" for x in items[0]["relation"] + items[0]["statements"])
    return _out(db, "person", user, "disputed" if disputed else "answer", items)


_NICK = re.compile(r"(?:叫他|叫她|称他|称她|称呼他|称呼她|称呼|喊他|喊她|备注)[“\"「]?([\u4e00-\u9fffA-Za-z]{1,8})[”\"」]?")


def _short_name(n: str) -> str:
    """“科比💤 1123” → “科比”：去掉表情、数字和后面的备注，留最前面那段名字。"""
    m = re.match(r"[\u4e00-\u9fffA-Za-z·.]+", (n or "").strip())
    return m.group(0) if m else (n or "")


def _find_people(db, name: str) -> list:
    """按她叫的名字找人物卡（11-07 评估试跑：“鹏哥”“科比”认不出）：卡名、微信名一样的最先；
    再看去掉表情和数字的短名、关系 / 备注里写的小名（“叫他鹏哥”）、合并时记下的别名；都没有才按名字包含找。"""
    rows = _rows(db, "SELECT * FROM people")
    exact = [c for c in rows if name in (c["name"], c.get("wx_name"))]
    if exact:
        return exact
    nick = [c for c in rows if _short_name(c["name"]) == name
            or name in _NICK.findall(f"{c.get('relation') or ''} {c.get('notes') or ''}")]
    try:  # 合并卡时旧名字记成了对象的别名（relations.merge → lifebase.repoint_person）
        ids = {int(r[0].split(":", 1)[1]) for r in db.conn.execute(
            "SELECT r.ref FROM aliases a JOIN object_refs r ON r.object_id = a.object_id WHERE a.alias = ? AND r.ref LIKE 'person:%'", (name,))}
        nick += [c for c in rows if c["id"] in ids and c not in nick]
    except Exception:
        pass
    if nick:
        return nick
    return [c for c in rows if len(name) >= 2 and name in c["name"]]


def _mentions(db, user: str, day: str, names: set) -> list:
    """她说过的话里提到这个人的（关于她自己的说法，比如“她与科比分工调研”），最多 8 条；按这个使用者能看的筛。"""
    names = {n for n in names if n and len(n) >= 2}
    if not names:
        return []
    now_sql, now_args = lifebase.mem_where("now", "statements", day)
    self_obj = db.conn.execute("SELECT object_id FROM object_refs WHERE ref = 'self'").fetchone()
    if not self_obj:
        return []
    like = " OR ".join("value_text LIKE ?" for _ in names)
    out = []
    for s in db.conn.execute(f"SELECT * FROM statements WHERE subject_id = ? AND ({like}) AND {now_sql} ORDER BY recorded_at DESC LIMIT 30",
                             (self_obj[0], *[f"%{n}%" for n in names], *now_args)):
        if _visible(user, s, db):
            out.append(_item(db, s))
        if len(out) >= 8:
            break
    return out


def _card_extra(c: dict) -> dict:
    """人物卡上每晚整理出来的几样（ethan/relations.py）：近况、话题、没了结的事（她了结过的不给）、重要日子。没整理过的是空。"""
    def j(v, default):
        try:
            return json.loads(v) if v else default
        except (TypeError, ValueError):
            return default
    from .relations import _done_keys, _is_done
    done = _done_keys({"loops_done": c.get("loops_done")})
    loops = [x for x in j(c.get("open_loops"), []) if isinstance(x, dict) and not _is_done(x, done)]
    return {"relation_text": c.get("relation") or "", "recent": (c.get("recent") or "")[:300], "topics": j(c.get("topics"), [])[:5],
            "open_loops": loops[:5], "dates": j(c.get("dates"), [])[:5], "birthday": c.get("birthday") or ""}


# ---------- 第 5 题：她定过哪些长期规矩 ----------
def _decisions_for(db, user, kinds=None, topic=None):
    from . import decisions
    who = memusers.check(user)
    role = ROLE.get(user)
    out = []
    for d in decisions.active(db):
        if kinds and d["kind"] not in kinds:
            continue
        if d["roles"] and not who.get("all") and role not in d["roles"]:
            continue  # 管的是别的角色
        if topic and not (_hit(f"{d['scope']} {d['statement']} {d['context'] or ''}", topic) or d["scope"] in topic):
            continue
        out.append(d)
    return out


def _clashes(decs) -> list:
    """同一个对象上两条生效的规矩说法不同、又不是互为例外：可能打架（并列，不挑一条）。"""
    out = []
    for i, a in enumerate(decs):
        for b in decs[i + 1:]:
            if a["scope"] == b["scope"] and a["kind"] == b["kind"] == "rule" and a["statement"] != b["statement"] \
                    and a["exception_of"] != b["id"] and b["exception_of"] != a["id"]:
                out.append((a["id"], b["id"]))
    return out


def _decision_item(d) -> dict:
    return {"decision": d["id"], "kind": d["kind"], "scope": d["scope"], "value": d["statement"], "quote": d["quote"],
            "since": d["starts_at"], "until": d["ends_at"], "version": d["version"], "said_by": "her_words" if d["quote"] else "unknown"}


def standing_rules(db, user: str, topic: str = None) -> dict:
    """生效的规矩和约束（决定表）+ 她说过、确认过的“决定”类说法。只有系统推的不算规矩。"""
    no = _private_only(db, "standing_rules", user)
    if no:
        return no
    decs = _decisions_for(db, user, ("rule", "constraint", "exception"), topic)
    items = [_decision_item(d) for d in decs]
    day = lifebase_today().isoformat()
    for r in _self_statements(db, "kind = 'decision'", view="now", day=day):
        if r["said_by"] in HER_OWN and _visible(user, r, db) and _match(f"{r['value_text']} {r['context'] or ''}", topic):
            items.append(_item(db, r))
    if not items:
        return _out(db, "standing_rules", user, "unknown", note="没找到她定过的规矩")
    clash = _clashes(decs)
    disputed = clash or any(i.get("review") == "disputed" for i in items)
    return _out(db, "standing_rules", user, "disputed" if disputed else "answer", items,
                ("这几条可能打架：" + "、".join(f"#{a} 和 #{b}" for a, b in clash)) if clash else "")


# ---------- 第 6 题：这周计划了什么、完成了什么、没完成的已知原因 ----------
def week_plan(db, user: str, today: date = None) -> dict:
    """每天只看最新一版计划；结果和原因只用记下的（她说的或打卡），原因没人说过就写“原因未知”，不推断。"""
    no = _private_only(db, "week_plan", user)
    if no:
        return no
    from datetime import timedelta
    day = today or lifebase_today()
    start = _monday(day)
    planned, odd = [], []
    d = start
    while d <= day:
        plan = _rows(db, "SELECT id, version FROM daily_plans WHERE day = ? ORDER BY version DESC LIMIT 1", (d.isoformat(),))
        if plan:
            for it in _rows(db, "SELECT i.id, i.kind, i.title, i.task_id, (SELECT done FROM tasks WHERE id = i.task_id) AS task_done "
                                "FROM plan_items i WHERE plan_id = ? AND kind IN ('todo', 'event') ORDER BY start, id", (plan[0]["id"],)):
                res = _rows(db, "SELECT result, reason, at FROM item_results WHERE item_id = ? ORDER BY id", (it["id"],))
                kinds = {r["result"] for r in res}
                last = res[-1] if res else None
                result = last["result"] if last else ("完成" if it["task_done"] else "没记录")
                if len(kinds) > 1:
                    odd.append(it["title"])
                reason = (last or {}).get("reason") or ("" if result == "完成" else "原因未知")
                planned.append({"day": d.isoformat(), "title": it["title"], "kind": it["kind"], "result": result, "reason": reason,
                                "plan_version": plan[0]["version"], "results": [r["result"] for r in res]})
        d += timedelta(days=1)
    done = _rows(db, "SELECT id, title, done_at FROM tasks WHERE done = 1 AND done_at >= ? ORDER BY done_at", (start.isoformat(),))
    if not planned and not done:
        return _out(db, "week_plan", user, "unknown", [], "这周还没有计划和完成记录")
    item = {"from": start.isoformat(), "to": day.isoformat(), "planned": planned, "done_tasks": done,
            "not_done": [p for p in planned if p["result"] != "完成"]}
    return _out(db, "week_plan", user, "disputed" if odd else "answer", [item],
                ("这几件前后记了不同的结果：" + "、".join(odd)) if odd else "")


# ---------- 第 7 题：这周哪些天、哪些来源没有记录 ----------
DAILY_SOURCES = (("S1", "和 小伴 聊天", "SELECT DISTINCT substr(occurred_at, 1, 10) FROM evidence WHERE source = 'chat' AND source_ref LIKE 'raw:%' "
                  "AND substr(occurred_at, 1, 10) BETWEEN ? AND ?"),
                 ("S5", "银行流水", "SELECT DISTINCT day FROM transactions WHERE day BETWEEN ? AND ?"),
                 ("S14", "iPhone 到家离家", "SELECT DISTINCT substr(at, 1, 10) FROM place_events WHERE substr(at, 1, 10) BETWEEN ? AND ?"),
                 ("S15", "Mac 工作时长", "SELECT DISTINCT substr(slot, 1, 10) FROM work_slots WHERE substr(slot, 1, 10) BETWEEN ? AND ?"))


def record_gaps(db, user: str, today: date = None) -> dict:
    """按天的来源（聊天、流水、定位、Mac）逐天看哪天没有；其余来源看断档（lifebase.source_health）。没接过的来源说“没接”。"""
    no = _private_only(db, "record_gaps", user)
    if no:
        return no
    from datetime import timedelta
    day = today or lifebase_today()
    start = _monday(day)
    days = [(start + timedelta(days=i)).isoformat() for i in range((day - start).days + 1)]
    by_source, covered = [], set()
    for code, name, sql in DAILY_SOURCES:
        try:
            have = {r[0] for r in db.conn.execute(sql, (days[0], days[-1]))}
            ever = True
        except Exception:
            have, ever = set(), False
        covered |= have
        by_source.append({"code": code, "name": name, "missing_days": [d for d in days if d not in have] if ever else days,
                          "connected": ever})
    now = datetime.combine(day, datetime.min.time(), timezone.utc)
    health = lifebase.source_health(db, now)
    stale = [{"code": h["code"], "name": h["name"], "state": h["state"], "days": h["days"]} for h in health if h["state"] != "ok"]
    if not covered and all(h["state"] == "not_connected" for h in health):
        return _out(db, "record_gaps", user, "unknown", note="一个来源都还没有记录")
    item = {"from": days[0], "to": days[-1], "days_without_any": [d for d in days if d not in covered], "by_source": by_source,
            "stale_sources": stale}
    return _out(db, "record_gaps", user, "answer", [item])


# ---------- 第 8 题：过去同类里程碑按时完成了几成、参照了哪几件 ----------
REASONS = (("外部阻塞", r"等|卡在|对方|签证|审批|没回|别人"), ("范围变了", r"范围|目标改|改了目标|加了|砍了|拆"),
           ("优先级调整", r"优先|先做|挪|推迟|放一放|顾不上"))


def milestone_ontime(db, user: str, topic: str = None) -> dict:
    """同时给三个数：全部可比的按时率、按原因分组、原因缺失的比例；参照集列清楚。可比的少于 3 件不给结论。"""
    no = _private_only(db, "milestone_ontime", user)
    if no:
        return no
    import re
    rows = _rows(db, "SELECT m.id, m.title, m.due, m.status, m.closed_at, m.review, g.title AS goal FROM milestones m "
                     "LEFT JOIN goals g ON g.id = m.goal_id WHERE m.status IN ('done', 'missed') AND m.due IS NOT NULL ORDER BY m.due")
    refs = []
    for r in rows:
        if topic and not _match(f"{r['title']} {r['goal'] or ''}", topic):
            continue
        if not r["closed_at"]:
            continue  # 关了却没记哪天关的：比不了
        on = r["status"] == "done" and r["closed_at"][:10] <= r["due"][:10]
        why = next((k for k, pat in REASONS if re.search(pat, r["review"] or "")), None) if not on else None
        refs.append({"milestone": r["id"], "title": r["title"], "goal": r["goal"], "due": r["due"], "closed": r["closed_at"][:10],
                     "on_time": on, "reason": why or ("" if on else "原因未知")})
    if len(refs) < 3:
        return _out(db, "milestone_ontime", user, "unknown", refs, f"可比的只有 {len(refs)} 件（少于 3 件），不给结论")
    late = [r for r in refs if not r["on_time"]]
    groups = {}
    for r in late:
        groups[r["reason"]] = groups.get(r["reason"], 0) + 1
    unknown = groups.get("原因未知", 0)
    item = {"on_time_rate": round(sum(r["on_time"] for r in refs) / len(refs), 2), "by_reason": groups,
            "reason_missing_rate": round(unknown / len(late), 2) if late else 0.0, "refs": refs}
    return _out(db, "milestone_ontime", user, "answer", [item], "原因多半未知，结论不稳" if late and unknown * 2 > len(late) else "")


# ---------- 第 10 题：这季每个副本的结果、她自己复盘时怎么说 ----------
def season_results(db, user: str, since: str = None) -> dict:
    """副本（今年的目标）现在的状态 + 每个里程碑的结果和复盘原话；她没复盘过的只给结果。"""
    no = _private_only(db, "season_results", user)
    if no:
        return no
    goals = _rows(db, "SELECT id, title, status, due, updated_at FROM goals WHERE level = 'year' AND (? IS NULL OR updated_at >= ? "
                      "OR status IN ('active', 'paused')) ORDER BY id", (since, since))
    items = []
    for g in goals:
        ms = _rows(db, "SELECT id, title, status, due, closed_at, review FROM milestones WHERE goal_id = ? ORDER BY seq, id", (g["id"],))
        reviews = [{"milestone": m["id"], "review": m["review"]} for m in ms if (m["review"] or "").strip()]
        items.append({"goal": g["id"], "title": g["title"], "status": g["status"], "due": g["due"],
                      "milestones": [{k: m[k] for k in ("id", "title", "status", "due", "closed_at")} for m in ms],
                      "her_review": reviews, "note": "" if reviews else "她还没复盘过，只给结果"})
    if not items:
        return _out(db, "season_results", user, "unknown", note="这季还没有副本")
    return _out(db, "season_results", user, "answer", items)


# ---------- 第 11 题：系统猜她某个特点，支持、反对的依据和别的解释 ----------
OBSERVE_MIN = 3  # 独立观察少于这个数只显示“观察中”（ontology.md 第 2 节第 11 题，首版暂定）


def hypothesis_case(db, user: str, topic: str = None) -> dict:
    """只给她本人（和要拿去跟她讨论的 小伴）。转述来的不算独立依据。"""
    no = _her_only(db, "hypothesis_case", user, also=("ethan",))
    if no:
        return no
    day = lifebase_today().isoformat()
    items = []
    relay = {r["id"] for r in _rows(db, "SELECT id FROM evidence WHERE source IN ('grok', 'agent')")}
    for r in _self_statements(db, "kind = 'hypothesis'", day=day):
        if not _match(f"{r['value_text']} {r['context'] or ''}", topic):
            continue
        src = _rows(db, "SELECT evidence_id, from_statement, role, source_unknown FROM statement_sources WHERE statement_id = ?", (r["id"],))
        sup = [s for s in src if s["role"] in ("supports", "quotes", "derived")]
        con = [s for s in src if s["role"] == "contradicts"]
        independent = {s["evidence_id"] for s in sup if s["evidence_id"] and s["evidence_id"] not in relay}
        items.append({**_item(db, r), "supports": [s["evidence_id"] or s["from_statement"] for s in sup],
                      "contradicts": [s["evidence_id"] or s["from_statement"] for s in con], "independent": len(independent),
                      "alternatives": [], "observing": len(independent) < OBSERVE_MIN})
    if not items:
        return _out(db, "hypothesis_case", user, "unknown", note="没有相关的猜测")
    disputed = any(i["contradicts"] or i["review"] == "disputed" for i in items)
    return _out(db, "hypothesis_case", user, "disputed" if disputed else "answer", items,
                "别的解释还没有地方记；独立依据不够 3 条的只算“观察中”")


# ---------- 第 13 题：某角色做某动作被授权到哪一步 ----------
def authorization(db, user: str, action: str, scope: str, grantee: str = None) -> dict:
    """只认她明确给的授权（ethan/decisions.py grants）。没有 = 只能起草，等她确认。角色只能问自己的，她本人和系统维护能问所有的。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "authorization", user, blocked)
    who = memusers.check(user)
    mine = ROLE.get(user, user)
    grantee = grantee or mine
    if grantee != mine and not who.get("all"):
        return _out(db, "authorization", user, "forbidden")
    from . import decisions
    decisions.ensure(db)
    now = _now()
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM grants WHERE grantee = ? AND action = ? AND revoked_at IS NULL AND (until IS NULL OR until > ?) "
        "AND NOT (one_off = 1 AND used_at IS NOT NULL) ORDER BY id DESC", (grantee, action, now)) if r["scope"] in scope or scope in r["scope"]]
    if not rows:
        return _out(db, "authorization", user, "unknown", [{"allowed": "draft_only"}], "没有授权记录：只能起草，等她确认")
    items = [{"grant": r["id"], "action": r["action"], "scope": r["scope"], "each_confirm": bool(r["each_confirm"]), "until": r["until"],
              "one_off": bool(r["one_off"]), "quote": r["quote"], "said_by": "her_words", "at": r["created_at"]} for r in rows]
    items.sort(key=lambda i: not i["each_confirm"])  # 最严的在前
    mixed = len({i["each_confirm"] for i in items}) > 1
    return _out(db, "authorization", user, "disputed" if mixed else "answer", items,
                "两条授权宽严不一，按最严的（每次确认）来" if mixed else "")


# ---------- 第 14、15、16 题：对外（ontology-plan 3.3：按三档受众 × 类别开关，lifebase.disclose） ----------
REFUSE = "这个我不方便说"
ASK_REPLY = "这个我问问她，回头告诉你"
SENSITIVE_TOPICS = (("address", r"住址|住哪|住在哪|地址|门牌|哪个区|几号楼|邮编|电话|手机号|邮箱|微信号|联系方式", None),
                    ("body", r"身体|体重|病|健康|医院|生理|怀孕|吃药", "body"),
                    ("money", r"钱|工资|收入|存款|花了多少|多少钱|负债|房贷|开销", "resources"),
                    ("love", r"感情|男朋友|女朋友|恋爱|结婚|分手|单身|对象", "relationships"),
                    ("others", r"她妈|她爸|家人|朋友的事|别人的事|同事的事", "relationships"))
TOPIC_CATEGORY = {"address": "address", "body": "body", "money": "money", "love": "family", "others": "family"}
# 第 14 题里访客问到哪一类（问到“问我”的类别时，没有现成能说的就“我问问她”）
CATEGORY_WORDS = (("whereabouts", r"在哪|哪个城市|人在|住在|在伦敦吗|在国内吗"), ("recent", r"最近|在忙|忙什么|在做什么|手头"),
                  ("career", r"工作|职业|实习|公司|任职|上班"), ("education", r"学校|读书|学历|专业|毕业|读的"),
                  ("skills", r"会什么|擅长|技能"), ("projects", r"项目|作品"), ("hobbies", r"爱好|喜欢做|兴趣"),
                  ("rules", r"规矩|原则"), ("prefs", r"喜欢吃|偏好|口味"), ("rhythms", r"几点|作息|日程|有空"),
                  ("temperament", r"性格|脾气|心情|情绪"))
OFFER_WORDS = r"合作|邀约|邀请|约稿|演讲|采访|接不接|商务|广告|推广"


def _public_gate(db, fn, user):
    blocked = _gate(db, user)
    if blocked:
        return _out(db, fn, user, blocked)
    who = memusers.check(user)
    if not memusers.external(user) and not who.get("all"):
        return _out(db, fn, user, "forbidden")  # 对外口径是给对外的使用者（合作方、公开）的；她本人在“我”页看
    return None


def _said_ok(db, user, recipient=None) -> list:
    """她批准的原句（逐条放行）。挡住的句子不在里面。"""
    return [d for d in lifebase.disclosures(db) if (memusers.check(user).get("all") and not d["revoked_at"])
            or lifebase.disclosure_ok(db, d["id"], user, recipient=recipient)]


def _outside(db, user, recipient=None):
    """关于她、现在成立的说法，按对外开关分成能说的和要先问她的。返回 (allow, ask)，每条带 category。"""
    day = lifebase_today().isoformat()
    allow, ask = [], []
    if memusers.check(user).get("all"):
        return allow, ask
    for r in _self_statements(db, day=day):
        if r["value_text"] is None:
            continue
        mode = lifebase.disclose(db, user, r, recipient=recipient)
        if mode == "deny":
            continue
        cat = lifebase.category_of(None, r["property"], r["kind"], r["value_text"])
        item = {"statement": r["id"], "value": r["value_text"], "category": cat, "said_by": r["said_by"], "at": r["recorded_at"]}
        (allow if mode == "allow" else ask).append(item)
    return allow, ask


def _ask_item(cat, draft=None, statement=None) -> dict:
    return {"reply": ASK_REPLY, "ask": True, "category": cat, "category_name": lifebase.CATEGORY_NAMES.get(cat, cat or ""),
            "draft": draft or "", "statement": statement}


def public_say(db, user: str, topic: str = None, recipient: str = None) -> dict:
    """第 14 题：访客问她在做什么、做过什么——按这一档的对外开关：“可以说”的类别给她的原话整理成的说法，加上她逐条放行的原句；
    问到“问我”的类别：回答“我问问她”，带上拟答（调用方 public_answer 在待你定里生成一条）；“不说”的一律“这个我不方便说”。
    别人说的、系统推的、转述的，删掉的、她说不对的，类别开着也不说（lifebase.disclose）。"""
    import re
    no = _public_gate(db, "public_say", user)
    if no:
        return no
    tier = memusers.tier_of(user)
    said = [d for d in _said_ok(db, user, recipient) if _match(d["text"], topic) and not re.search(OFFER_WORDS, d["text"])]
    allow, ask = _outside(db, user, recipient)
    asked_cats = [c for c, pat in CATEGORY_WORDS if topic and re.search(pat, topic)]
    items = [{"disclosure": d["id"], "value": d["text"], "quote": d["quote"], "at": d["created_at"], "said_by": "her_confirmation",
              "once": bool(d.get("once"))} for d in said]
    texts = {i["value"] for i in items}
    items += [a for a in allow if a["value"] not in texts and (_match(a["value"], topic) or a["category"] in asked_cats)]
    if not items:
        pend = [a for a in ask if _match(a["value"], topic) or a["category"] in asked_cats]
        if pend:
            return _out(db, "public_say", user, "ask", [_ask_item(pend[0]["category"], pend[0]["value"], pend[0]["statement"])],
                        "这一类要先问她")
        ask_cats = [c for c in asked_cats if lifebase.policy_mode(db, tier, c) == "ask"]
        if ask_cats:
            return _out(db, "public_say", user, "ask", [_ask_item(ask_cats[0])], "这一类要先问她（还没有现成的说法）")
        return _out(db, "public_say", user, "unknown", [{"reply": REFUSE}], "没有能对外说的")
    return _out(db, "public_say", user, "disputed" if topic and len(items) > 1 else "answer", items,
                "能说的有好几句，只照原文挑一句说，不合并、不往下推" if topic and len(items) > 1 else "")


def public_sensitive(db, user: str, topic: str, recipient: str = None) -> dict:
    """第 15 题：住址、身体、钱、感情、别人的事——按这一档的开关（默认都不说），不确认也不否认。
    住址联系方式永远不说（锁死）；某类“不说”但她逐条放行过的那一句可以说；某类她打开了“可以说”就给那一类能说的；“问我”就问她。"""
    import re
    no = _public_gate(db, "public_sensitive", user)
    if no:
        return no
    hits = [(k, dim) for k, pat, dim in SENSITIVE_TOPICS if re.search(pat, topic or "")]
    if not hits:
        return _out(db, "public_sensitive", user, "unknown", note="不是敏感话题，按第 14 题（public_say）答")
    if any(k == "address" for k, _ in hits):
        return _out(db, "public_sensitive", user, "answer", [{"reply": REFUSE, "refuse": True}], "住址联系方式永远不说")
    dims = {d for _, d in hits}
    ok = [d for d in _said_ok(db, user, recipient) if d["dimension"] in dims]
    if ok:
        return _out(db, "public_sensitive", user, "answer", [{"reply": d["text"], "disclosure": d["id"], "refuse": False,
                                                             "said_by": "her_confirmation", "at": d["created_at"]} for d in ok],
                    "她逐条批准过的只有这一句，别的照样不说")
    cats = [TOPIC_CATEGORY[k] for k, _ in hits]
    modes = {lifebase.policy_mode(db, memusers.tier_of(user), c) for c in cats}
    if "deny" in modes:
        return _out(db, "public_sensitive", user, "answer", [{"reply": REFUSE, "refuse": True}], "不确认也不否认")
    allow, ask = _outside(db, user, recipient)
    if "ask" in modes:
        pend = [a for a in ask if a["category"] in cats]
        return _out(db, "public_sensitive", user, "ask", [_ask_item(pend[0]["category"] if pend else cats[0],
                                                                     pend[0]["value"] if pend else None,
                                                                     pend[0]["statement"] if pend else None)], "这一类要先问她")
    got = [a for a in allow if a["category"] in cats]
    if got:
        return _out(db, "public_sensitive", user, "answer", [{**a, "reply": a["value"], "refuse": False} for a in got],
                    "这一类她打开了“可以说”")
    return _out(db, "public_sensitive", user, "answer", [{"reply": REFUSE, "refuse": True}], "不确认也不否认")


def public_offer(db, user: str, topic: str = None, recipient: str = None) -> dict:
    """第 16 题：访客问她接不接合作 / 邀约——只说她批准过的对外口径；没有就不预测、不代答，记下来转给她（待你定里一条）。"""
    import re
    no = _public_gate(db, "public_offer", user)
    if no:
        return no
    rows = [d for d in _said_ok(db, user, recipient) if re.search(OFFER_WORDS, d["text"]) and _match(d["text"], topic)]
    if not rows:
        return _out(db, "public_offer", user, "unknown", [{"reply": "这个我得先问问她，我记下来转给她", "forward": True, "ask": True,
                                                          "category": None, "category_name": "合作邀约", "draft": ""}],
                    "没有她批准过的对外口径：不预测、不代答")
    return _out(db, "public_offer", user, "answer", [{"reply": d["text"], "disclosure": d["id"], "said_by": "her_confirmation",
                                                      "at": d["created_at"], "forward": False} for d in rows])


def public_answer(db, user: str, question: str, recipient: str = None) -> dict:
    """对外使用者（Days、主页、合作方 agent）回答访客的统一入口：按问题分到第 16 / 15 / 14 题；
    要先问她的，在“待你定”里生成一条（谁问的、问了什么、拟答），回答“我问问她”。返回题的结果 + reply（能直接说的话）+ pending。"""
    import re
    q = (question or "").strip()[:200]
    if day_question(q):  # 她哪天做了什么、去了哪（第 29 题）：行程一律不对外，回“不方便说”，不生成“问我”
        got = day_summary(db, user, day_question(q)[2])
        return {**got, "items": [], "reply": REFUSE, "pending": None}
    if re.search(OFFER_WORDS, q):
        got = public_offer(db, user, q, recipient)
    elif any(re.search(pat, q) for _, pat, _ in SENSITIVE_TOPICS):
        got = public_sensitive(db, user, q, recipient)
    else:
        got = public_say(db, user, q, recipient)
    first = (got.get("items") or [{}])[0]
    reply = first.get("reply") or first.get("value") or REFUSE
    pending = None
    if first.get("ask"):
        from . import mepage
        pending = mepage.add_disclosure_ask(db, user, q, first.get("category"), first.get("draft"), recipient, first.get("statement"))
    return {**got, "reply": reply, "pending": pending}


# ---------- 第 17 题：这条说法是谁说的、经过几次转述、有几条独立依据 ----------
def provenance(db, user: str, statement_id: int) -> dict:
    """顺着出处往回找：原始记录（哪个来源、什么时候）、派生自哪条说法、转述（Grok、agent）不算独立依据。"""
    no = _her_only(db, "provenance", user, also=("ethan",))
    if no:
        return no
    ontology.ensure(db)
    r = db.conn.execute("SELECT * FROM statements WHERE id = ?", (statement_id,)).fetchone()
    if not r or r["review"] == "forgotten" or not _visible(user, r, db):
        return _out(db, "provenance", user, "unknown", note="没有这条说法（或已删除）")
    roots, hops, contra, unknown, seen = [], 0, [], False, set()
    todo = [(statement_id, 0)]
    while todo:
        sid, depth = todo.pop()
        if sid in seen:
            continue
        seen.add(sid)
        for s in db.conn.execute("SELECT * FROM statement_sources WHERE statement_id = ?", (sid,)):
            if s["role"] == "contradicts":
                contra.append(s["evidence_id"] or s["from_statement"])
                continue
            if s["source_unknown"]:
                unknown = True
            if s["from_statement"]:
                hops = max(hops, depth + 1)
                todo.append((s["from_statement"], depth + 1))
            if s["evidence_id"]:
                e = db.conn.execute("SELECT id, source, occurred_at, deleted_at FROM evidence WHERE id = ?", (s["evidence_id"],)).fetchone()
                if e and not e["deleted_at"]:
                    roots.append({"evidence": e["id"], "source": e["source"], "at": e["occurred_at"], "relay": e["source"] in RELAY})
    independent = len({x["evidence"] for x in roots if not x["relay"]})
    relayed = sum(1 for x in roots if x["relay"])
    item = {**_item(db, r), "roots": roots, "independent": independent, "relayed": relayed + hops, "superseded_by": r["superseded_by"],
            "contradicts": contra, "source_unknown": unknown}
    note = "；".join(x for x in ("已被 #%s 取代" % r["superseded_by"] if r["superseded_by"] else "",
                                 "有出处缺失" if unknown else "", "有反对的依据" if contra else "") if x)
    return _out(db, "provenance", user, "disputed" if contra or r["review"] == "disputed" else "answer", [item], note)


# ---------- 第 18 题：她在某件事上，上个月和现在有什么变化 ----------
def changes(db, user: str, topic: str, since: str = None) -> dict:
    """新旧两条 + 变化时间（旧的到哪天为止）。只有一条时说“没看到变化记录”。作废的（记错了）不算变化。"""
    no = _private_only(db, "changes", user)
    if no:
        return no
    from datetime import timedelta
    since = since or (lifebase_today() - timedelta(days=31)).isoformat()
    rows = [r for r in _self_statements(db) if _visible(user, r, db) and _match(f"{r['value_text']} {r['context'] or ''}", topic)]
    olds = _self_statements(db, "superseded_by IS NOT NULL", view="changes")
    by_id = {r["id"]: r for r in rows}
    pairs = []
    for o in olds:
        new = by_id.get(o["superseded_by"]) or db.conn.execute("SELECT * FROM statements WHERE id = ?", (o["superseded_by"],)).fetchone()
        if not new or not (_match(o["value_text"], topic) or _match(new["value_text"], topic)) or not _visible(user, o, db):
            continue
        when = o["valid_to"] or new["valid_from"] or new["recorded_at"]
        if when and when[:10] < since:
            continue
        pairs.append({"before": o["value_text"], "before_statement": o["id"], "after": new["value_text"], "statement": new["id"],
                      "changed_at": (when or "")[:10], "said_by": new["said_by"], "review": new["review"]})
    if not pairs:
        return _out(db, "changes", user, "unknown", [_item(db, r) for r in rows[:3]], "没看到变化记录")
    disputed = any(p["review"] == "disputed" for p in pairs)
    return _out(db, "changes", user, "disputed" if disputed else "answer", sorted(pairs, key=lambda p: p["changed_at"]))


# ---------- 第 19 题：删掉某件事后，系统里还剩哪里有它 ----------
def still_there(db, user: str, words: str) -> dict:
    """逐处查：原始记录、说法、检索索引、她自己写的日记，再加备份这类我们管不到的地方。只给她本人看。"""
    no = _her_only(db, "still_there", user)
    if no:
        return no
    lifebase.ensure(db)
    ev = [r["id"] for r in _rows(db, "SELECT id, text FROM evidence WHERE deleted_at IS NULL AND text IS NOT NULL") if _hit(r["text"], words)]
    sts_sql, sts_args = lifebase.mem_where("deletable", "statements")
    sts = [r["id"] for r in _rows(db, f"SELECT id, value_text FROM statements WHERE {sts_sql}", sts_args) if _hit(r["value_text"], words)]
    from . import memsearch
    idx = [r["key"] for r in memsearch.search(db, [words], "chat", log=False, user="me_page", via="q19")]
    diary = [r["day"] for r in _rows(db, "SELECT day, body FROM diary") if _hit(r["body"], words)]
    places = [{"where": "原始记录", "ids": ev}, {"where": "说法", "ids": sts}, {"where": "检索索引", "ids": idx},
              {"where": "她自己写的日记（不自动改，请她自己看）", "ids": diary}]
    left = [p for p in places if p["ids"]]
    always = ["加密备份（按保留期自然过期）", "已经发给模型服务商的请求（不在我们控制里）"]
    return _out(db, "still_there", user, "answer", left + [{"where": w, "ids": []} for w in always],
                "能查到的地方都没有了" if not left else f"还有 {len(left)} 处有它")


# ---------- 第 22 题：做某件事前，她有哪些生效的决定和它有关 ----------
def decisions_for(db, user: str, topic: str = None) -> dict:
    """管到这个角色的决定（规矩、暂停、约束、门禁、例外）和给它的授权。没有就照默认规则，并写明“没找到相关决定”。"""
    no = _private_only(db, "decisions_for", user)
    if no:
        return no
    decs = _decisions_for(db, user, None, topic)
    items = [_decision_item(d) for d in decs]
    from . import decisions
    decisions.ensure(db)
    for g in db.conn.execute("SELECT * FROM grants WHERE grantee = ? AND revoked_at IS NULL AND (until IS NULL OR until > ?)",
                             (ROLE.get(user, user), _now())):
        if not topic or _match(g["scope"], topic) or g["scope"] in topic:
            items.append({"grant": g["id"], "kind": "grant", "scope": g["scope"], "value": f"可以{g['action']}", "quote": g["quote"],
                          "since": g["created_at"], "until": g["until"], "said_by": "her_words"})
    if not items:
        return _out(db, "decisions_for", user, "unknown", note="没找到相关决定，照默认规则做，并在结果里写明")
    clash = _clashes(decs)
    return _out(db, "decisions_for", user, "disputed" if clash else "answer", items,
                ("这几条可能打架：" + "、".join(f"#{a} 和 #{b}" for a, b in clash)) if clash else "")


# ---------- 第 23 题：某条待办 / 日程 / 副本上次是谁、什么时候、为什么改的 ----------
def change_log(db, user: str, title: str) -> dict:
    """三处合成一份：她在工作台上的改动（锁）、状态转换、计划里删掉的日程、删掉的待办。没有就说“没有改动记录”，不猜。"""
    no = _private_only(db, "change_log", user)
    if no:
        return no
    from .core.agent import find_similar
    same = lambda t: bool(t) and (t == title or find_similar(title, [t]) is not None)
    items = []
    for r in _rows(db, "SELECT obj, field, value, title, note, at, released_at FROM manual_locks"):
        if same(r["title"]):
            items.append({"who": "她", "at": r["at"], "what": {"deleted": "删了", "title": "改了名", "time": "改了时间"}.get(r["field"], f"改了{r['field']}"),
                          "title": r["title"], "why": r["note"] or "", "still_locked": not r["released_at"]})
    tasks = {r["id"]: r["title"] for r in _rows(db, "SELECT id, title FROM tasks")}
    goals = {r["id"]: r["title"] for r in _rows(db, "SELECT id, title FROM goals")}
    for r in _rows(db, "SELECT kind, ref, from_state, to_state, at, effects, undone_at FROM transitions WHERE kind IN ('todo', 'goal', 'prep')"):
        name = (tasks if r["kind"] == "todo" else goals if r["kind"] == "goal" else {}).get(r["ref"])
        if same(name):
            items.append({"who": "系统记录", "at": r["at"], "what": f"{r['from_state'] or '新建'} → {r['to_state']}", "title": name,
                          "why": r["effects"] if r["effects"] not in (None, "{}") else "", "undone": bool(r["undone_at"])})
    for r in _rows(db, "SELECT day, title, action, at FROM plan_overrides"):
        if same(r["title"]):
            items.append({"who": "她", "at": r["at"], "what": f"计划里{ {'delete': '删了', 'move': '挪了'}.get(r['action'], r['action'])}（{r['day']}）",
                          "title": r["title"], "why": ""})
    for r in _rows(db, "SELECT title, at FROM deleted_tasks"):
        if same(r["title"]):
            items.append({"who": "工作台", "at": r["at"], "what": "待办删了", "title": r["title"], "why": ""})
    if not items:
        return _out(db, "change_log", user, "unknown", note="没有改动记录")
    items.sort(key=lambda i: i["at"] or "", reverse=True)
    return _out(db, "change_log", user, "answer", items, "最近的在前")


# ---------- 第 24 题：现在有没有互相矛盾的地方 ----------
PROMISE = r"我来|我帮你|交给我|我去(查|问|订|找|弄|办)|我记下了|我会(提醒|帮)"


def contradictions(db, user: str, today: date = None) -> dict:
    """三种：计划里有她否掉 / 删掉的事；小伴 答应了的不在待办里；两条生效的决定打架。只报出来，不改她的数据。"""
    no = _private_only(db, "contradictions", user)
    if no:
        return no
    import re
    from datetime import timedelta
    from .core.agent import find_similar
    day = today or lifebase_today()
    items = []
    gone = [r["title"] for r in _rows(db, "SELECT title FROM manual_locks WHERE field = 'deleted' AND released_at IS NULL AND title IS NOT NULL")]
    for d in (day, day + timedelta(days=1)):
        plan = _rows(db, "SELECT id FROM daily_plans WHERE day = ? ORDER BY version DESC LIMIT 1", (d.isoformat(),))
        if not plan:
            continue
        nos = {r["title"] for r in _rows(db, "SELECT title FROM plan_overrides WHERE day = ? AND action = 'delete'", (d.isoformat(),))}
        for it in _rows(db, "SELECT title FROM plan_items WHERE plan_id = ?", (plan[0]["id"],)):
            hit = it["title"] in nos or find_similar(it["title"], gone)
            if hit:
                items.append({"kind": "plan_has_removed", "day": d.isoformat(), "title": it["title"], "detail": "计划里又排了她删掉 / 否掉的事"})
    since = (day - timedelta(days=7)).isoformat()
    open_titles = [r["title"] for r in _rows(db, "SELECT title FROM tasks WHERE created_at >= ? OR done = 0", (since,))]
    for e in _rows(db, "SELECT id, occurred_at, text FROM evidence WHERE source = 'chat' AND deleted_at IS NULL AND text LIKE 'ethan：%' "
                       "AND substr(occurred_at, 1, 10) >= ? ORDER BY occurred_at DESC LIMIT 300", (since,)):
        said = e["text"].split("：", 1)[1]
        m = re.search(PROMISE, said)
        if m and not find_similar(said[m.start():m.start() + 30], open_titles):
            items.append({"kind": "promise_not_todo", "evidence": e["id"], "at": e["occurred_at"], "title": said[m.start():m.start() + 40],
                          "detail": "小伴 答应了，待办里没有"})
    decs = _decisions_for(db, "system")
    for a, b in _clashes(decs):
        items.append({"kind": "decisions_clash", "decisions": [a, b], "detail": "两条生效的决定打架"})
    return _out(db, "contradictions", user, "answer", items, "" if items else "没查到互相矛盾的地方")


# ---------- 第 25 题：维修工接手某个问题 ----------
def issue_brief(db, user: str, keyword: str) -> dict:
    """现象（任务报错）、她的原话（纠错单，只截到修问题需要的长度）、相关决定、最近改动、代码在哪（模块说明里提到的）。"""
    no = _her_only(db, "issue_brief", user, also=("patrol",))
    if no:
        return no
    from pathlib import Path
    errors = [{"job": r["job"], "at": r["finished_at"], "error": str(r["error"])[:300]}
              for r in _rows(db, "SELECT job, finished_at, error FROM job_runs WHERE error IS NOT NULL AND error != '' ORDER BY id DESC LIMIT 200")
              if _hit(f"{r['job']} {r['error']}", keyword)][:5]
    quotes = [{"ticket": r["id"], "at": r["at"], "quote": (r["quote"] or r["right"] or "")[:120], "layer": r["layer"]}
              for r in _rows(db, "SELECT id, at, quote, wrong, right, layer FROM corrections ORDER BY id DESC LIMIT 200")
              if _hit(f"{r['quote'] or ''} {r['wrong'] or ''} {r['right'] or ''}", keyword)][:5]
    decs = [_decision_item(d) for d in _decisions_for(db, "system", None, keyword)][:5]
    edits = change_log(db, "system", keyword)["items"][:5]
    code = []
    for f in sorted(Path(__file__).resolve().parent.glob("*.py")):
        try:
            head = f.read_text(encoding="utf-8")[:600]
        except OSError:
            continue
        if _hit(head, keyword, need=0.6):
            code.append(f"ethan/{f.name}")
    item = {"symptoms": errors, "her_words": quotes, "decisions": decs, "recent_changes": edits, "code": code[:8]}
    if not any(item.values()):
        return _out(db, "issue_brief", user, "unknown", note="没有相关的报错、原话、决定和改动（还没有问题单）")
    return _out(db, "issue_brief", user, "answer", [item], "她的原话只截到修问题需要的长度")


# ---------- 第 28 题：她最近一周说过要做、但还没进待办的事 ----------
INTENT = r"我(要|得|想|打算|准备|计划|回头|明天|周末|下周|今晚)(去|做|写|买|订|约|报名|整理|联系|看|学|改|交|发|找|查)"


def said_not_todo(db, user: str, today: date = None, days: int = 7) -> dict:
    """只列出来，不自动建待办。和现有待办（含做完的、删掉的）说的是同一件就不列。"""
    no = _private_only(db, "said_not_todo", user)
    if no:
        return no
    import re
    from datetime import timedelta
    from .core.agent import find_similar
    day = today or lifebase_today()
    since = (day - timedelta(days=days)).isoformat()
    titles = [r["title"] for r in _rows(db, "SELECT title FROM tasks")] + [r["title"] for r in _rows(db, "SELECT title FROM deleted_tasks")]
    items, seen = [], []
    for e in _rows(db, "SELECT id, occurred_at, text FROM evidence WHERE source IN ('chat', 'interview') AND deleted_at IS NULL "
                       "AND substr(occurred_at, 1, 10) >= ? ORDER BY occurred_at DESC LIMIT 1000", (since,)):
        text = e["text"] or ""
        if text.startswith("ethan："):
            continue
        said = text.split("：", 1)[1] if text.startswith("mia：") else text
        m = re.search(INTENT, said)
        if not m:
            continue
        what = re.split(r"[，。！？,.!?\n]", said[m.start():])[0][:40]
        if find_similar(what.replace("我", ""), titles) or find_similar(what, seen):
            continue
        seen.append(what)
        items.append({"evidence": e["id"], "at": e["occurred_at"], "said": what, "quote": said[:200], "said_by": "her_words"})
    if not items:
        return _out(db, "said_not_todo", user, "unknown", note=f"最近 {days} 天没找到说过要做、还没进待办的")
    return _out(db, "said_not_todo", user, "answer", items, "只列出来，不自动建待办")


# ---------- 第 29 题：某天 / 某段时间她做了什么（原候选 D；一问“我昨天干嘛去了”，小伴 答“我不知道”） ----------
# 问法里要有“哪天”（clock.day_range 认得出）和“做了什么 / 去哪”这类，才算这道题（ethan/askroute.py 分流、对外的一律“不方便说”）
DAY_ASK = r"干嘛|干啥|干什么|干了(什么|啥|些什么)|做了(什么|啥|些什么|哪些)|做什么|忙(了)?(什么|啥)|去(了)?哪|在哪|怎么过的|过得怎么样|都干了|干嘛去了|去干嘛"
DAY_MAX = 14  # 一次最多汇总几天（“这个月”只给最近 14 天，写明）


def day_question(text: str, today: date = None):
    """这句是不是在问“某天她做了什么 / 在哪”：是就返回 (开始, 结束, 原话里说日子的那几个字)，不是返回 None。"""
    import re
    if not text or not re.search(DAY_ASK, text):
        return None
    return clock.day_range(text, today or lifebase_today())


def _local_day(at) -> str:
    """存的时间（UTC 或带时区的 ISO）落在她那边的哪一天。"""
    try:
        t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return str(at or "")[:10]
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(clock.LOCAL).date().isoformat()


def _diary_part(db, d: str):
    """那天的日记：她写的 / 她改过的为准；系统写的标“小伴 记的”。没有返回 None。"""
    from . import diary
    try:
        r = diary.get(db, d)
    except Exception:
        return None
    if not r or not (r.get("body") or "").strip():
        return None
    who = diary.who_wrote(r)
    return {"part": "日记", "who": who, "said_by": "her_words" if r.get("source") in diary.HER else "system",
            "text": " ".join(r["body"].split())}


def _day_parts(db, day: date, full: bool = True) -> list:
    """一天的各项记录（行为记录、日程、日记），每项一个 {"part", "value", ...}；没有的不放。full=False 时每项写短一点（汇总好几天用）。"""
    from datetime import timedelta
    d = day.isoformat()
    parts = []
    dia = _diary_part(db, d)
    if dia:
        text = dia["text"] if full else dia["text"][:120] + ("…" if len(dia["text"]) > 120 else "")
        parts.append({"part": "日记", "value": f"日记（{dia['who']}）：{text}", "said_by": dia["said_by"], "from": "diary"})
    try:
        from . import places_log
        trips = places_log.trips(db, day, day)
        if trips:
            parts.append({"part": "出门", "from": "iPhone 定位", "value": "出门和到家：" + "；".join(
                f"{a:%H:%M} 出门" + (f"，{b:%H:%M} 到家" + ("（第二天）" if b.date() > day else "") if b else "，没记到回家") for a, b in trips)})
    except Exception:
        pass
    try:
        from . import workaholic
        s = workaholic.day_stats(db, day)
        if s["hours"]:
            parts.append({"part": "工作时长", "from": "电脑记录", "value": f"电脑上工作了约 {s['hours']} 小时"
                          + (f"，晚上 9 点后 {s['late']} 小时" if s["late"] else "") + (f"，最长一段 {s['stretch']} 小时" if full and s["stretch"] else "")})
    except Exception:
        pass
    evs = []
    try:
        plan = db.latest_plan(d) if hasattr(db, "latest_plan") else None
        evs = [f"{i['start']}{'–' + i['end'] if i.get('end') else ''} {i['title']}" + (f"（{i['place']}）" if i.get("place") else "")
               for i in (plan or {}).get("items", []) if i.get("kind") == "event" and i.get("start")]
    except Exception:
        pass
    try:
        from . import events as fev
        evs += [f"{b[0]}–{b[1]} {b[2]}" + (f"（{b[3]['location']}）" if b[3].get("location") else "") for b in fev.for_day(db, day)]
    except Exception:
        pass
    if evs:
        parts.append({"part": "日程", "from": "日历和计划", "value": "日程（排了的不一定去了，以出门记录和她说的为准）：" + "；".join(dict.fromkeys(evs))})
    try:
        from . import taskrules
        done = [t["title"] for t in taskrules.done_that_day(db, day)]
        if done:
            parts.append({"part": "做完的待办", "from": "待办", "value": "做完的待办：" + "、".join(done[:12 if full else 5])
                          + (f" 等 {len(done)} 件" if len(done) > (12 if full else 5) else "")})
    except Exception:
        pass
    checks = _rows(db, "SELECT reason FROM points WHERE day = ? AND delta > 0 AND COALESCE(ref, '') NOT LIKE 'task:%' ORDER BY at", (d,))
    if checks:
        parts.append({"part": "打卡", "from": "打卡记录", "value": "打卡：" + "、".join(r["reason"] for r in checks[:10])})
    try:
        from . import finance
        spent = finance.spend_between(db, day, day)
        if spent:
            top = _rows(db, "SELECT merchant, amount, currency FROM transactions WHERE day = ? AND amount < 0 "
                            "AND COALESCE(category, '') NOT IN ('transfers', 'savings', 'unparsed', 'ignored') ORDER BY amount LIMIT 3", (d,))
            parts.append({"part": "花销", "from": "银行流水", "value": "花销：" + "、".join(f"{v:g} {c}" for c, v in spent.items())
                          + ("（最大的：" + "、".join(f"{r['merchant'] or '某笔'} {-r['amount']:g} {r['currency']}" for r in top) + "）" if full and top else "")})
    except Exception:
        pass
    met = _rows(db, "SELECT kind, who FROM life_log WHERE day = ? AND kind IN ('meet', 'new_friend', 'contact')", (d,))
    if met:
        parts.append({"part": "见了谁", "from": "社交记录", "value": "见了谁 / 联系了谁：" + "、".join(
            f"{r['who']}（{ {'meet': '见面', 'new_friend': '新认识', 'contact': '联系'}.get(r['kind'], r['kind'])}）" for r in met)})
    if full:  # 她那天在聊天里说的（只取她说的，删掉的不算）；时间按她的时区算哪天
        lo, hi = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=2)).isoformat()
        said = [r for r in _rows(db, "SELECT id, occurred_at, text FROM evidence WHERE source = 'chat' AND deleted_at IS NULL "
                                     "AND occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at", (lo, hi))
                if (r["text"] or "").startswith("mia：") and _local_day(r["occurred_at"]) == d]
        if said:
            lines = []
            for r in said[-12:]:
                at = str(r["occurred_at"])
                hm = ""
                try:
                    t = datetime.fromisoformat(at.replace("Z", "+00:00"))
                    hm = (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(clock.LOCAL).strftime("%H:%M") + " "
                except ValueError:
                    pass
                lines.append(hm + " ".join(r["text"][len("mia："):].split())[:120])
            parts.append({"part": "聊天里说的", "from": "聊天", "said_by": "her_words", "value": "她那天在聊天里说的：" + " / ".join(lines)})
    return parts


def day_her_words(db, user: str, day: date, limit: int = 300) -> dict:
    """某天她在聊天里说的原话（evidence 里 source = 'chat'、以“mia：”开头的；小伴 说的、删掉的不算），按时间先后。
    哪天按她的时区算。昨日卡（cards）用它挑有画面感的几句给导演（晚同意“加”）。对外的一律没权限。"""
    no = _private_only(db, "day_her_words", user)
    if no:
        return no
    d = day.isoformat()
    lo, hi = (day - timedelta(days=1)).isoformat(), (day + timedelta(days=2)).isoformat()
    rows = [r for r in _rows(db, "SELECT id, occurred_at, text FROM evidence WHERE source = 'chat' AND deleted_at IS NULL "
                                 "AND occurred_at >= ? AND occurred_at < ? ORDER BY occurred_at, id", (lo, hi))
            if (r["text"] or "").startswith("mia：") and _local_day(r["occurred_at"]) == d]
    items = [{"evidence": r["id"], "at": str(r["occurred_at"]), "value": " ".join(r["text"][len("mia："):].split()),
              "said_by": "her_words"} for r in rows[-limit:]]
    return _out(db, "day_her_words", user, "answer" if items else "unknown", items)


def day_summary(db, user: str, when: str = None, today: date = None) -> dict:
    """某天 / 某段时间她做了什么：日记（她写的、她改过的为准；系统写的标“小伴 记的”）、出门和到家、电脑上的工作时长、日程、
    做完的待办（ethan/taskrules.py）、打卡、花销、见了谁、那天她在聊天里说的。“哪天”按她的时区解析（clock.day_range）。
    行程是私事：对外的（Days、主页、合作方）一律没权限（调用方看到和“没有”一样，回“不方便说”）。
    日记没写的照样用行为记录答，写明“那天的日记还没写”；什么都没有就是不知道（不等于那天没做事）。"""
    no = _private_only(db, "day_summary", user)
    if no:
        return no
    from datetime import timedelta
    now = today or lifebase_today()
    got = clock.day_range(when or "", now) if when else (now - timedelta(days=1), now - timedelta(days=1), "昨天")
    if not got:
        return _out(db, "day_summary", user, "unknown", note=f"没认出“{when}”是哪天")
    start, end, said = got
    if start > now:
        return _out(db, "day_summary", user, "unknown", note=f"{said}（{start.isoformat()}）还没到")
    end = min(end, now)
    days = (end - start).days + 1
    cut = ""
    if days > DAY_MAX:
        start, cut = end - timedelta(days=DAY_MAX - 1), f"只汇总了最近 {DAY_MAX} 天"
    one = start == end
    items, no_diary = [], []
    d = end
    while d >= start:  # 新的在前
        parts = _day_parts(db, d, full=one)
        wd = "一二三四五六日"[d.weekday()]
        if not any(p["part"] == "日记" for p in parts) and d < now:
            no_diary.append(d)
        for p in parts:
            items.append({**p, "day": d.isoformat(), "value": (p["value"] if one else f"{d.month}/{d.day} 周{wd} " + p["value"])})
        d -= timedelta(days=1)
    span = start.isoformat() if one else f"{start.isoformat()} 到 {end.isoformat()}"
    notes = [f"问的是{said}（{span}）"]
    if no_diary:
        notes.append(("那天" if one else "、".join(f"{x.month}/{x.day}" for x in no_diary) + " ") + "的日记还没写，按行为记录答")
    if any(p.get("said_by") == "system" for p in items):
        notes.append("标“小伴 记的”日记是系统写的初稿，她没改过，不当她的原话")
    if one and start == now:
        notes.append("今天还没过完")
    if cut:
        notes.append(cut)
    if not items:
        return _out(db, "day_summary", user, "unknown", note="；".join(notes) + "；这段时间没有任何记录（不等于没做事）")
    return _out(db, "day_summary", user, "answer", items, "；".join(notes))


# ---------- 写作规范（ontology.md 3.2，“提前做”；docs/design/style-and-skills.md） ----------
STYLE_CHAT_PER_DAY = 6  # 聊天原话抽样：每天最多几句、一共最多几句（不全量）
STYLE_CHAT_MIN = 6  # 太短的（“好”“嗯嗯”）不要
FRESH_YEARS = 2  # 写作规范和 skill 只用最近两年的材料


def fresh_cutoff(today: date = None) -> str:
    d = today or lifebase_today()
    return d.replace(year=d.year - FRESH_YEARS).isoformat() if not (d.month == 2 and d.day == 29) else d.replace(year=d.year - FRESH_YEARS, day=28).isoformat()


def recent_only(db, got: dict, today: date = None) -> dict:
    """把一题的结果里“出处全是两年前的原始记录”的条目去掉（没有出处日期的、她当面确认过的照留）。给 skill 和写作规范用，不改原题。"""
    cut = fresh_cutoff(today)
    keep = []
    for i in got.get("items", []):
        ids = [s["evidence"] for s in i.get("sources") or [] if s.get("evidence")]
        if ids and i.get("said_by") != "her_confirmation":
            q = ",".join("?" * len(ids))
            dates = [r[0] for r in db.conn.execute(f"SELECT occurred_at FROM evidence WHERE id IN ({q})", ids) if r[0]]
            if dates and max(dates)[:10] < cut:
                continue
        keep.append(i)
    return {**got, "items": keep}


def style_materials(db, user: str, chat_limit: int = 80) -> dict:
    """写作规范的原料：**只取她自己写的**——
    - mail：她发出去的邮件（Gmail“已发送”，引用的别人的话收的时候就去掉了）；
    - diary：她从空白自己写的日记（source='her'）整篇；
    - pair：她在系统草稿上改的句子，改前 → 改后（最强信号；新加的句子 kind='added'，删掉的系统句子 kind='deleted'）；
    - liked：日记里她用荧光笔标出的句子（系统写的，她喜欢）；disliked：她批注“写得不好 / 不像我”的句子（系统写的，她不喜欢）；
    - chat：聊天里她的原话，抽样（每天最多 STYLE_CHAT_PER_DAY 句，一共 chat_limit 句，最近的先）。
    系统写的、她没改的日记一句都不进。只给“写作规范”和系统维护。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "style_materials", user, blocked)
    if user != "style" and not memusers.check(user).get("all"):
        return _out(db, "style_materials", user, "forbidden")
    import hashlib
    from . import diary
    h = lambda t: hashlib.sha1(t.encode("utf-8")).hexdigest()[:12]
    lifebase.ensure(db)
    items = []
    from . import diary, desk
    desk.ensure(db)
    cut = fresh_cutoff()
    # 小伴 起草、她照发的邮件不算她写的：
    # 发出的邮件里含 小伴 草稿原文的整封不收；她改过的只收改前→改后（和日记一样）
    norm = lambda t: re.sub(r"\s+", "", t or "")
    ethan_sent = []
    for r in _rows(db, "SELECT msg_key, draft, ethan_draft, COALESCE(sent_at, created_at) AS at FROM mail_items "
                       "WHERE COALESCE(ethan_draft, '') != '' AND COALESCE(draft, '') != '' AND desk_state = 'sent'"):
        ethan_sent.append(norm(r["draft"]))
        if norm(r["draft"]) != norm(r["ethan_draft"]) and (r["at"] or "")[:10] >= cut:
            for before, after in diary.changes(r["ethan_draft"], r["draft"]):
                kind = "pair" if before and after else "added" if after else "deleted"
                items.append({"ref": f"mailpair:{r['msg_key']}:{h(before + '→' + after)}", "kind": kind, "before": before,
                              "text": after or before, "at": r["at"], "evidence": None})
    for r in _rows(db, "SELECT id, occurred_at, text FROM evidence WHERE source = 'sent' AND deleted_at IS NULL AND text IS NOT NULL "
                       "AND SUBSTR(COALESCE(occurred_at, ''), 1, 10) >= ? ORDER BY occurred_at DESC, id DESC LIMIT 60", (cut,)):
        head, _, body = (r["text"] or "").partition("\n")
        body = body.strip() if head.startswith("主题：") else (r["text"] or "").strip()
        if body and any(len(d) >= 4 and d in norm(body) for d in ethan_sent):
            continue
        if body:
            items.append({"ref": f"sent:{r['id']}", "kind": "mail", "text": body[:1500], "subject": head[3:80] if head.startswith("主题：") else "",
                          "at": r["occurred_at"], "evidence": r["id"]})
    diary.ensure(db)
    ev_of = {}
    for r in _rows(db, "SELECT id, source_ref FROM evidence WHERE source = 'workbench' AND source_ref LIKE 'diary:%' AND deleted_at IS NULL"):
        ev_of.setdefault(r["source_ref"][6:16], []).append(r["id"])
    for r in diary.her_rows(db, 60):  # “哪些是她写的”只在 diary 定义
        ev = (ev_of.get(r["day"]) or [None])[-1]
        if r["source"] == "her":
            items.append({"ref": f"diary:{r['day']}:{h(r['body'])}", "kind": "diary", "text": r["body"][:2000], "at": r["day"], "evidence": ev})
            continue
        for before, after in diary.changes(r.get("draft_text") or "", r["body"]):
            kind = "pair" if before and after else "added" if after else "deleted"
            items.append({"ref": f"pair:{r['day']}:{h(before + '→' + after)}", "kind": kind, "before": before, "text": after or before,
                          "at": r["day"], "evidence": ev if after else None})
    for r in _rows(db, "SELECT id, day, text FROM diary_marks ORDER BY id DESC LIMIT 60"):
        items.append({"ref": f"liked:{r['id']}", "kind": "liked", "text": r["text"], "at": r["day"], "evidence": None})
    for r in _rows(db, "SELECT id, day, text, note FROM diary_notes ORDER BY id DESC LIMIT 100"):
        if STYLE_BAD.search(r["note"] or ""):
            items.append({"ref": f"disliked:{r['id']}", "kind": "disliked", "text": r["text"], "note": r["note"][:200], "at": r["day"], "evidence": None})
    per_day, n = {}, 0
    for r in _rows(db, "SELECT id, occurred_at, text FROM evidence WHERE source = 'chat' AND source_ref LIKE 'raw:%' AND deleted_at IS NULL "
                       "AND text LIKE 'mia：%' AND SUBSTR(COALESCE(occurred_at, ''), 1, 10) >= ? ORDER BY id DESC LIMIT 3000", (cut,)):
        words = " ".join((r["text"] or "")[len("mia："):].split())
        day = (r["occurred_at"] or "")[:10]
        if len(words) < STYLE_CHAT_MIN or per_day.get(day, 0) >= STYLE_CHAT_PER_DAY or n >= chat_limit:
            continue
        per_day[day] = per_day.get(day, 0) + 1
        n += 1
        items.append({"ref": f"chat:{r['id']}", "kind": "chat", "text": words[:300], "at": r["occurred_at"], "evidence": r["id"]})
    if not items:
        return _out(db, "style_materials", user, "unknown", note="还没有她自己写的文字")
    return _out(db, "style_materials", user, "answer", items)


STYLE_BAD = re.compile(r"写得不好|不像我|不是我的(语气|口吻|说法)|我不会这么(说|写)|别这么写|不要这么写|太(啰嗦|肉麻|鸡汤|书面|正式|官方)|生硬|别扭|矫情")


def writing_style(db, user: str, audience: str = None, scene: str = None) -> dict:
    """她**确认过**的写作规范（说法 property = writing_style，review = accepted）；草稿、她说不对的都不给。
    audience（friend 朋友 / elder 长辈 / work 工作）：只给通用的和这一类的。
    对外的（Days）只给规则本身，不给出处原句（原句里是她写过的内容），也不按对外开关挡——规范只管怎么说，不涉及内容权限。"""
    blocked = _gate(db, user)
    if blocked:
        return _out(db, "writing_style", user, blocked)
    outside = memusers.external(user)
    day = lifebase_today().isoformat()
    rows = _self_statements(db, "property = 'writing_style'", view="style", day=day)
    meta = {r["statement_id"]: r for r in _rows(db, "SELECT * FROM style_rules WHERE status = 'confirmed' AND statement_id IS NOT NULL")}
    items = []
    for r in rows:
        m = meta.get(r["id"])
        if m is None and r["said_by"] not in HER_OWN:
            continue
        aud = (m or {}).get("audience") or "all"
        if audience and aud not in ("all", audience):
            continue
        sc = (m or {}).get("scene") or "all"  # 用在哪：只给通用的和这个场景的
        if scene and sc not in ("all", scene):
            continue
        if outside and lifebase.CONTACT.search(r["value_text"] or ""):
            continue
        item = {"statement": r["id"], "value": r["value_text"], "category": (m or {}).get("category") or "other", "audience": aud,
                "words": json.loads((m or {}).get("words") or "[]"), "said_by": r["said_by"]}
        if not outside:
            item["quotes"] = [q.get("text") for q in json.loads((m or {}).get("quotes") or "[]")][:3]
        items.append(item)
    if not items:
        return _out(db, "writing_style", user, "unknown", note="还没有她确认过的写作规范")
    return _out(db, "writing_style", user, "answer", items)


# ---------- 题号 → 函数（能力题清单 capability-questions.md；第 20、21 题首版不做） ----------
QUESTIONS = {1: "whereabouts_now", 2: "last_said", 3: "person", 4: "preferences", 5: "standing_rules", 6: "week_plan",
             7: "record_gaps", 8: "milestone_ontime", 9: "fixed_schedule", 10: "season_results", 11: "hypothesis_case",
             12: "prep_requirements", 13: "authorization", 14: "public_say", 15: "public_sensitive", 16: "public_offer",
             17: "provenance", 18: "changes", 19: "still_there", 22: "decisions_for", 23: "change_log", 24: "contradictions",
             25: "issue_brief", 26: "weekly_spend", 27: "history", 28: "said_not_todo",
             29: "day_summary"}  # 第 29 题：原候选 D，“我昨天干嘛去了”答不上后收
FIRST_VERSION = tuple(q for q in QUESTIONS if q <= 28)  # 首版要求的题（1–19、22–28）
QUESTION_OF = {fn: q for q, fn in QUESTIONS.items()}


def lines(got: dict, key: str = "value", limit: int = 8) -> str:
    """给提示词用：一题的结果压成几行。没有就空；有争议、猜测都标出来。"""
    out = []
    for i in got.get("items", [])[:limit]:
        v = i.get(key) or i.get("value") or i.get("title") or i.get("said") or i.get("quote") or ""
        if not v:
            continue
        out.append(f"- {v}" + (f"（{i['context']}）" if i.get("context") else "") + ("（猜测）" if i.get("guess") else ""))
    if out and got.get("status") == "disputed":
        out.append(f"（有两种说法，没定：{got.get('note') or '并列给出'}）")
    return "\n".join(out)


# ---------- 切换开关：哪个角色已经改从新结构读 ----------
SWITCH = "CREATE TABLE IF NOT EXISTS onto_switch (user TEXT PRIMARY KEY, on_at TEXT, off_at TEXT, note TEXT)"


def reading_new(db, user: str) -> bool:
    """这个角色是不是已经切到新结构（默认没切）。读不到开关当没切。"""
    try:
        r = db.conn.execute("SELECT on_at, off_at FROM onto_switch WHERE user = ?", (user,)).fetchone()
    except Exception:
        return False
    return bool(r and r["on_at"] and not r["off_at"])


def switch(db, user: str, on: bool, note: str = "") -> None:
    """切过去 / 切回旧的。切回是回退：不丢东西（函数只读，旧 facts 一直双写）。"""
    memusers.check(user)
    db.conn.execute(SWITCH)
    if on:
        db.conn.execute("INSERT OR REPLACE INTO onto_switch (user, on_at, off_at, note) VALUES (?, ?, NULL, ?)", (user, _now(), note))
    else:
        db.conn.execute("UPDATE onto_switch SET off_at = ?, note = ? WHERE user = ?", (_now(), note, user))
    db.conn.commit()


# ---------- 到期了的临时状态（借鉴 6） ----------
EXPIRED_DAYS = 14  # 到期两周内的还拿出来提一句；更早的就只是以前的事了


def expired_states(db, user: str, today: date = None, days: int = EXPIRED_DAYS, _log_empty: bool = True) -> dict:
    """“这周在生病”“这几天在出差”这类带了预计到哪天的说法，过了那天就不再当“现在”（lifebase.fact_state 判成 expired），转成“以前的状态”：
    这里列出最近到期、还没被新说法取代的，给 小伴 当“上次说的是……，现在还这样吗”的话头。不删、不改，她说还算数由“我”页改日期。
    不是能力题（没有题号），只给自己人。"""
    no = _private_only(db, "expired_states", user)
    if no:
        return no
    day = today or lifebase_today()
    lo = (day - timedelta(days=days)).isoformat()
    rows = _self_statements(db, "substr(until, 1, 10) >= ?", (lo,), view="expired", day=day)
    items = [{**_item(db, r), "until": r["until"][:10]} for r in rows if _visible(user, r, db)]
    items.sort(key=lambda i: i["until"], reverse=True)
    if not items and not _log_empty:  # 每轮聊天都带一下：没有到期的就不记使用记录（免得每轮一条“没有”）
        return {"status": "unknown", "items": [], "note": ""}
    return _out(db, "expired_states", user, "answer" if items else "unknown", items[:8])


def expired_lines(db, user: str, today: date = None) -> str:
    """给提示词用：到期了的临时状态，一条一行（带原定到哪天）。没有就空。"""
    got = expired_states(db, user, today=today, _log_empty=False)
    return "\n".join(f"- {i['value']}（原定到 {i['until'][5:]}）" for i in got["items"] if not i["guess"])


def preference_lines(db, user: str, today: date = None) -> str:
    """给提示词用：偏好按新结构列出来（带条件）。没有就空。"""
    got = preferences(db, user, today=today)
    lines = []
    for i in got["items"]:
        if i["guess"]:
            continue
        lines.append(f"- {i['value']}" + (f"（{i['context']}）" if i["context"] else "") + ("（有两种说法，没定）" if i["review"] == "disputed" else ""))
    return "\n".join(lines)


def she_removed(db, user: str, title: str) -> bool:
    """第 27 题给自动流程用：她删过这件（同名或说法相近的）就是 True——自动流程宁可不建。"""
    got = history(db, user, title)
    return any(i["what"] == "删过" for i in got["items"])


# ---------- 切换前的四道关 ----------
def gates(db) -> dict:
    """切换读取前要过的四道关里，能在服务器上自动查的两道：完整对账、还没对照的旧记录。
    故障注入、回退演练、5 题验收在测试里（tests/test_onto_query.py、tests/test_dual_write.py、tests/test_tx.py）。"""
    lifebase.ensure(db)
    diff = lifebase.reconcile_statements(db)
    unmapped = lifebase.unmapped_facts(db)
    return {"reconcile_diffs": len(diff), "unmapped_facts": unmapped, "pass": not diff and not unmapped}


def main():
    import argparse
    from pathlib import Path
    from .memory import MemoryStore
    parser = argparse.ArgumentParser(description="ontology 查询函数：切换前检查")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--gates", action="store_true")
    parser.add_argument("--switch", nargs=2, metavar=("角色", "on/off"), help="切到新结构 / 切回旧的（回退）")
    parser.add_argument("--retire-check", action="store_true", help="旧结构下线体检（ontology-plan 3.4，只读）")
    parser.add_argument("--fallback", choices=("on", "off"), help="3.4 改走新结构的读路径切回旧表（on）/ 切回新结构（off）")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    try:
        if args.retire_check:
            from . import retire
            print(json.dumps(retire.check(store.db, args.data), ensure_ascii=False, indent=1), flush=True)
        elif args.fallback:
            from . import retire
            retire.set_fallback(store.db, args.fallback == "on", "命令行")
            print(json.dumps({"fallback": retire.fallback(store.db)}), flush=True)
        elif args.switch:
            switch(store.db, args.switch[0], args.switch[1] == "on", "命令行")
            print(json.dumps({"user": args.switch[0], "reading_new": reading_new(store.db, args.switch[0])}), flush=True)
        else:
            print(json.dumps(gates(store.db), ensure_ascii=False), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
