"""赛季页：这个赛季她在七块领域上过得怎么样、彼此怎么此消彼长、下个赛季该怎么调（docs/design/season-page.md）。

的第二版：小伴 的一句话总评 + 下赛季建议（一直显示），下面是样稿 C 的“七块领地分数柱”（现在的分数、比开季 / 上季的变化），
最后只留主副本一条关卡路线。最上面原来的“赛季通行证”大卡（等级、本季积分轨道、下一级 / 成就 / 奖励）和首页重复，删了。目的是一眼看出哪些领域好、哪些不好、各有什么变化。
数字都由代码算；总评、每块的原因、下季建议由 小伴 每周日写一次（season_notes），没写就用代码规则兜底（不额外调模型）。
11-06：小伴 只写判断不写数字；他写完以后她又记了东西（newer_records），带数字的旧句子先收起来换成现算的，后台重写（每天最多 REWRITE_CAP 次）。
"""

import json
from datetime import date, datetime, timedelta, timezone

from . import attributes, lifelog, quests, workaholic, worktime
from . import clock

SEASON = {"name": "2026 冬季 · 城市篇", "start": "2026-10-13", "end": "2026-12-31"}


def current(settings) -> tuple:
    """(赛季, 真正开始那天)：第一季她说从冷启动那天（10-28）开始算；之后的赛季由她在工作台点“按这个开下个赛季”写进 settings（ethan/nextseason.py）。"""
    got = settings.get("season") if settings else None
    if isinstance(got, dict) and got.get("name") and got.get("start") and got.get("end"):
        return {"name": got["name"], "start": got["start"], "end": got["end"]}, date.fromisoformat(got["start"])
    start = max(SEASON["start"], (settings.get("cold_start_from") if settings else None) or SEASON["start"])
    return SEASON, date.fromisoformat(start)


NOTES = """CREATE TABLE IF NOT EXISTS season_notes (
    week TEXT PRIMARY KEY, season TEXT NOT NULL, body TEXT NOT NULL, created_at TEXT NOT NULL)"""
WORK_CAP = 45          # 一周工作上限（小时），超过算“冲得猛”
TRAIN_STD = 5          # 每周训练标准（这个赛季 5 次）
SLEEP_STD = 6          # 每周 0 点前睡标准
ICON_KEY = {"body": "健康", "career": "事业", "money": "财务", "social": "社交", "study": "学习", "academic": "学术", "life": "生活体验"}


def ensure(db):
    db.conn.execute(NOTES)
    attributes.lifelog.ensure(db)


def info(season: dict, start: date, day: date) -> dict:
    end = date.fromisoformat(season["end"])
    total = (end - start).days + 1
    gone = (day - start).days + 1
    two_thirds = start + timedelta(days=round(total * 2 / 3) - 1)
    week = clock.season_week(day, start)  # 按自然周，和依据里的周数一致
    return {"name": season["name"], "start": start.isoformat(), "end": season["end"], "week": week,
            "days_left": max(0, (end - day).days), "two_thirds": two_thirds.isoformat(), "late": day >= two_thirds,
            "ended": day > end}


# ---------- 三周小账 ----------
def _habit_count(db, title: str, a: date, b: date) -> int:
    h = quests.habit_by_title(db, title)
    if not h:
        return 0
    return db.conn.execute("SELECT COUNT(DISTINCT day) FROM habit_log WHERE habit_id = ? AND day >= ? AND day <= ?",
                           (h["id"], a.isoformat(), b.isoformat())).fetchone()[0]


def ledger(db, start: date, day: date, weeks: int = 3) -> dict:
    """最近几个自然周（含本周）的四个数。只算赛季开始以后。"""
    monday = clock.week_start(day)
    out = []
    for i in range(weeks - 1, -1, -1):
        a = monday - timedelta(weeks=i)
        b = min(a + timedelta(days=6), day)
        if b < start:
            continue
        a = max(a, start)
        out.append({"from": a.isoformat(), "to": b.isoformat(), "n": clock.season_week(a, start),  # 按自然周数（看到“第 1、1 周”）
                    "work": round(sum(worktime.hours(db, a + timedelta(days=n)) for n in range((b - a).days + 1)), 1),
                    "train": _habit_count(db, "训练", a, b), "friends": lifelog.count(db, "meet", a, b, distinct_days=True),
                    "sleep": _habit_count(db, "0 点前睡", a, b)})
    rows = [("工作", "work", "小时", f"不超过 {WORK_CAP}", lambda v: v > WORK_CAP),
            ("训练", "train", "次", f"标准 {TRAIN_STD}", lambda v: v < TRAIN_STD),
            ("见朋友", "friends", "次", "保底 1", lambda v: v < 1),
            ("0 点前睡", "sleep", "天", f"标准 {SLEEP_STD}", lambda v: v < SLEEP_STD)]
    last = len(out) - 1
    return {"weeks": out, "rows": [{"name": n, "unit": u, "std": s,
                                    "values": [{"v": w[k], "bad": bad(w[k]) and i != last} for i, w in enumerate(out)]}
                                   for n, k, u, s, bad in rows]}


# ---------- 此消彼长 ----------
def relations(db, start: date, day: date) -> list:
    """从每天的数据算领域之间的影响；样本不够（少于 2 周或 5 天）就不说。
    [{"from", "to", "kind": crowd/help, "label", "text", "n"}]"""
    out = []
    led = ledger(db, start, day, weeks=12)["weeks"]
    full = [w for w in led if (date.fromisoformat(w["to"]) - date.fromisoformat(w["from"])).days >= 6]
    hi = [w for w in full if w["work"] > WORK_CAP]
    lo = [w for w in full if w["work"] <= WORK_CAP]
    if hi and lo:
        a = sum(w["train"] for w in hi) / len(hi)
        b = sum(w["train"] for w in lo) / len(lo)
        if b - a >= 1:
            out.append({"from": "career", "to": "body", "kind": "crowd", "label": "工作挤掉训练", "n": len(full),
                        "text": f"工作超过 {WORK_CAP} 小时的周，训练平均 {a:.0f} 次；其他周 {b:.0f} 次"})
        fa = sum(w["friends"] for w in hi) / len(hi)
        fb = sum(w["friends"] for w in lo) / len(lo)
        if fb - fa >= 0.5:
            out.append({"from": "career", "to": "social", "kind": "crowd", "label": "加班推掉见朋友", "n": len(full),
                        "text": f"工作超过 {WORK_CAP} 小时的周，见朋友平均 {fa:.1f} 次；其他周 {fb:.1f} 次"})
    # 前一天工作到很晚 → 第二天该练没练
    h = quests.habit_by_title(db, "训练")
    late_days, missed_after = 0, 0
    d = start
    while d < day:
        if workaholic.day_stats(db, d)["late"] >= 1:
            nxt = d + timedelta(days=1)
            if h and quests.due_today(h, nxt):
                late_days += 1
                missed_after += not quests.checked(db, h["id"], nxt)
        d += timedelta(days=1)
    if late_days >= 5 and missed_after / late_days >= 0.5:
        out.append({"from": "career", "to": "body", "kind": "crowd", "label": "熬夜工作挤掉训练", "n": late_days,
                    "text": f"工作到很晚的第二天，排好的 {late_days} 次训练没练 {missed_after} 次"})
    # 出门的日子睡得早
    out_h, sleep_h = quests.habit_by_title(db, "每天出门"), quests.habit_by_title(db, "0 点前睡")
    if out_h and sleep_h:
        days = [start + timedelta(days=n) for n in range((day - start).days)]
        went = [x for x in days if quests.checked(db, out_h["id"], x)]
        stay = [x for x in days if x not in went]
        if len(went) >= 5 and len(stay) >= 5:
            r1 = sum(quests.checked(db, sleep_h["id"], x) for x in went) / len(went)
            r2 = sum(quests.checked(db, sleep_h["id"], x) for x in stay) / len(stay)
            if r1 - r2 >= 0.25:
                out.append({"from": "life", "to": "body", "kind": "help", "label": "出门帮早睡", "n": len(days),
                            "text": f"出门的 {len(went)} 天里 {round(r1 * 100)}% 0 点前睡；没出门的只有 {round(r2 * 100)}%"})
    return out


# ---------- 各领域 ----------
def domains(db, start: date, day: date, notes: dict, rels: list, past: list = None) -> list:
    """七块领域的分数。past 是往季结算（nextseason.archive），有上一季就带上上季收官的分数（和上赛季比）。"""
    prev = {d.get("name"): d.get("now") for d in ((past or [{}])[0].get("domains") or [])} if past else {}
    now = {r["key"]: r for r in attributes.compute(db, day, trend=False)}
    work = round(sum(worktime.hours(db, start + timedelta(days=n)) for n in range((day - start).days + 1)), 1)
    out = []
    for key in attributes.ORDER:
        r = now[key]
        score = None if r["score"] is None else round(r["score"])  # 现在的分数现算（她记一笔，刷新就变）
        tr = attributes.trail(db, key, start, day, r["score"])  # 开季那天的分数：和领域页同一处取（attributes.trail）
        before = attributes.first_of(tr, day)
        delta = None if score is None or before is None else score - before
        facts = [{"name": i["name"], "actual": i["actual"] if i.get("score") is not None else "还没有数据", "standard": i["standard"]}
                 for i in r["items"]][:3]  # 没数据的也列出来写“还没有数据”（学习卡片里怎么没有认知拓展）
        out.append({
            "key": key, "name": r["name"], "start": before, "now": score, "delta": delta, "label": r["label"],
            "prev": prev.get(r["name"]), "prev_delta": None if score is None or prev.get(r["name"]) is None else score - prev[r["name"]],
            "floor": score is not None and score < 40,  # 跌破保底（40 以下受损）
            "hours": work if key == "career" else None, "facts": facts, "weak": _weakest(r),
            "why": (notes.get("why") or {}).get(key) or _why(r), "why_by": "小伴" if (notes.get("why") or {}).get(key) else "",
            "fact": _why(r),  # 现算的那一项数字，摆在 小伴 的判断旁边（他只写判断，11-06）
            "crowded_by": [x["label"] for x in rels if x["to"] == key and x["kind"] == "crowd"],
            "crowds": [ICON_KEY[x["to"]] for x in rels if x["from"] == key and x["kind"] == "crowd"],
            "helped_by": [x["label"] for x in rels if x["to"] == key and x["kind"] == "help"],
        })
        out[-1]["status"] = status(out[-1])
        out[-1]["adjust"] = adjust(out[-1], r)
    # 失衡最严重的两块做大卡：先看跌破保底，再看跌得多
    ranked = sorted([d for d in out if d["now"] is not None], key=lambda d: (not d["floor"], d["delta"] if d["delta"] is not None else 0, d["now"]))
    focus = [d["key"] for d in ranked if d["floor"] or (d["delta"] or 0) < 0][:2]
    for d in out:
        d["focus"] = d["key"] in focus
        d["source"] = d["key"] in {x["from"] for x in rels if x["kind"] == "crowd"}
    return sorted(out, key=lambda d: (not d["focus"], not d["source"], attributes.ORDER.index(d["key"])))


STATUS = {"floor": "跌破保底", "down": "在往下掉", "good": "状态好", "mid": "一般", "none": "还没有数据"}


def status(d: dict) -> str:
    """每块领地一个状态（看完赛季页就知道接下来怎么调整）：floor / down / good / mid / none。"""
    if d["now"] is None:
        return "none"
    if d["floor"]:
        return "floor"
    if (d["delta"] or 0) < 0:
        return "down"
    return "good" if d["now"] >= 70 else "mid"


def adjust(d: dict, r: dict) -> str:
    """这块接下来怎么调，一句（代码规则，不调模型）：挤了别人的少加码；往下掉的先补离标准最远那项；一般的把那项拉到标准；好的保持。"""
    items = [i for i in r["items"] if i.get("score") is not None]
    if not items:
        first = next((i["name"] for i in r["items"]), "")
        return f"先开始记录{first}，有数据才看得出好坏" if first else "先接上数据来源"
    worst = min(items, key=lambda i: i["score"])
    std = str(worst["standard"]).split("（")[0].split("；")[0]  # 只留“每周 5 天”这一截，30 天折算和保底放领域页
    gap = f"{worst['name']}（现在 {worst['actual']}，标准{'' if std.startswith('每') else ' '}{std}）"
    if d["crowds"]:
        return f"少加码：在挤{'、'.join(d['crowds'])}"
    if d["status"] in ("floor", "down"):
        return f"先补{gap}" + (f"；{d['crowded_by'][0]}，先把那边缓一缓" if d["crowded_by"] else "")
    if d["status"] == "good":
        return "照现在的节奏保持" + (f"，{worst['name']}再稳一点" if worst["score"] < 70 else "")
    return f"把{gap}拉到标准"


def conclusion(doms: list) -> str:
    """结论：接下来先做哪一件（代码按分数拼，小伴 的总评在上面）。只挑重点，不再列哪几块好、哪几块在掉——下面的表逐个列了
"""
    bad = [d for d in doms if d["status"] in ("floor", "down")]
    if bad:
        first = min(bad, key=lambda d: (d["status"] != "floor", d["delta"] or 0, d["now"]))
        return f"接下来先管{first['name']}：{first['adjust']}。"
    if any(d["status"] == "good" for d in doms):
        return "接下来照现在的节奏，别往上加码。"
    return "各个领域都还在中间，数据再攒一攒就看得出该往哪边调。" if any(d["now"] is not None for d in doms) else "还没有数据，记上几天再看。"


def _weakest(r: dict):
    items = [i for i in r["items"] if i.get("score") is not None]
    return min(items, key=lambda i: i["score"])["name"] if items else None


def _why(r: dict) -> str:
    """小伴 还没写的时候：拿离标准最远的那一项说一句。"""
    items = [i for i in r["items"] if i.get("score") is not None]
    if not items:
        return "还没有数据"
    worst = min(items, key=lambda i: i["score"])
    std = str(worst["standard"]).split("（")[0].split("；")[0]
    return f"{worst['name']}：{worst['actual']}（标准{'' if std.startswith('每') else ' '}{std}）"


# ---------- 小伴 每周写的结论 ----------
def latest_note(db, season_name: str) -> dict:
    ensure(db)
    r = db.conn.execute("SELECT week, body, created_at FROM season_notes WHERE season = ? ORDER BY week DESC LIMIT 1", (season_name,)).fetchone()
    if not r:
        return {}
    try:
        return {**json.loads(r["body"]), "week": r["week"], "at": r["created_at"]}
    except ValueError:
        return {}


def fallback_line(led: dict, rels: list = None) -> str:
    """小伴 还没写的时候：用小账和此消彼长拼一句“现在的状态和影响”，不只是数字。"""
    if not led["weeks"]:
        return "赛季刚开始，数据攒一周再说。"
    w = led["weeks"][-2] if len(led["weeks"]) >= 2 else led["weeks"][-1]
    lost = []
    if w["train"] < TRAIN_STD:
        lost.append(f"训练只练了 {w['train']} 次")
    if w["friends"] < 1:
        lost.append("朋友一次没见")
    if w["sleep"] < SLEEP_STD:
        lost.append(f"0 点前睡只有 {w['sleep']} 天")
    if w["work"] > WORK_CAP and lost:
        return f"上周工作 {w['work']:g} 小时，超过上限：事业在挤占健康和社交，{'、'.join(lost)}。"
    if w["work"] > WORK_CAP:
        return f"上周工作 {w['work']:g} 小时，超过上限，其他几块还撑得住，但别再往上加了。"
    if lost:
        return f"上周工作量正常，但{'、'.join(lost)}，生活这几块在往下掉。"
    return "上周各块都在标准里，保持这个节奏。"


WRITER = """你是 小伴，在给 Mia 写赛季页的周总结（她的人生被分成七块领域：健康、事业、财务、社交、学习、学术、生活体验）。
下面是这个赛季到现在的数据（都是代码算的，不要编）。页面上的数字（次数、小时、分数）由代码打开页面时现算、摆在你的话旁边，
所以你只写判断，不写数字：她之后补记一笔，你写的数字就和页面对不上了（社交补记了两笔，总评还写着见朋友 0 次）。请只输出一个 JSON：
{"line": "一句结论，30–60 字，只挑最要紧的一两件说（最该管的那块、为什么、具体少了什么；说事，不写数字），不要把七个领域挨个点评一遍——下面的表已经逐个列了，口吻是你本人，温柔但直说",
 "why": {"领域 key": "这块变好或变差最主要的原因，一句，20–40 字，尽量点到具体的人、日子、事，不写数字"},
 "next": null 或 {"keep": ["保留的目标"], "pause": ["暂停的"], "start": ["新开的"], "rule": "一条硬线，比如“工作每周不超过 45 小时”"}}
next 只有在数据里 late 为 true（赛季过了三分之二）时才写，否则写 null。不提副本进度。不说教，不喊口号。"""


def write_note(db, router, season: dict, start: date, day: date) -> dict:
    """每周日由 weekly 调一次；也可以手动跑。返回写进去的内容。"""
    ensure(db)
    led = ledger(db, start, day)
    rels = relations(db, start, day)
    doms = domains(db, start, day, {}, rels)
    facts = {"season": info(season, start, day), "ledger": led, "relations": rels,
             "domains": [{k: d[k] for k in ("key", "name", "start", "now", "delta", "facts", "crowded_by", "helped_by")} for d in doms]}
    text = router.complete(WRITER, [{"role": "user", "content": json.dumps(facts, ensure_ascii=False)}], purpose="season-note").text
    data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    body = {"line": str(data.get("line") or "").strip()[:120],
            "why": {k: str(v)[:80] for k, v in (data.get("why") or {}).items() if k in attributes.ORDER},
            "next": data.get("next") if facts["season"]["late"] else None,
            "evidence": led}
    if not body["line"]:
        raise ValueError("没写出结论")
    week = clock.week_start(day).isoformat()
    db.conn.execute("INSERT OR REPLACE INTO season_notes (week, season, body, created_at) VALUES (?, ?, ?, ?)",
                    (week, season["name"], json.dumps(body, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.conn.commit()
    return body


def page(db, season: dict, start: date, day: date, past: list = None) -> dict:
    ensure(db)
    note = latest_note(db, season["name"])
    led = ledger(db, start, day)
    rels = relations(db, start, day)
    inf = info(season, start, day)
    newer = newer_records(db, note)
    shown = usable_note(note, newer)  # 写完以后她又记了东西：带数字的旧句子不再摆出来（会和现算的数对不上）
    doms = domains(db, start, day, shown, rels, past)
    nxt = note.get("next") if inf["late"] else None
    # 小伴 写了总评就不再拼代码的结论：两句各挑各的重点会打架
    return {"season": inf, "line": shown.get("line") or fallback_line(led, rels), "conclusion": "" if shown.get("line") else conclusion(doms),
            "stale": stale(note, day) or bool(newer), "outdated": bool(newer), "line_by": "小伴" if shown.get("line") else "",
            "line_at": (note.get("at") or "")[:10], "note_at": note_label(note), "hidden": bool(note.get("line")) and not shown.get("line"),
            "ledger": led, "relations": rels, "domains": doms, "next": nxt,
            # 下赛季建议一直显示：小伴 过了三分之二才写正式的，之前用代码按这季的数字先拟
            "suggest": {**nxt, "by": "小伴"} if nxt else suggest_next(doms, led, rels)}


# ---------- 下赛季建议的兜底（代码规则，不调模型） ----------
def suggest_next(doms: list, led: dict, rels: list) -> dict:
    """小伴 还没写正式建议时，按这季的分数和此消彼长先拟：好的保持、挤人的收一收、掉下去的补上，加一条硬线。"""
    scored = [d for d in doms if d["now"] is not None]
    keep = [f"{d['name']}照现在的节奏（{d['now']} 分）" for d in sorted(scored, key=lambda d: -d["now"])
            if d["now"] >= 70 and not d["floor"] and (d["delta"] or 0) >= 0][:2]
    pause = [f"{ICON_KEY[k]}少加码：挤了{'、'.join(sorted({ICON_KEY[x['to']] for x in rels if x['from'] == k and x['kind'] == 'crowd'}))}"
             for k in sorted({x["from"] for x in rels if x["kind"] == "crowd"})]
    start = []
    for d in sorted(scored, key=lambda d: (not d["floor"], d["delta"] if d["delta"] is not None else 0, d["now"])):
        if not (d["floor"] or (d["delta"] or 0) < 0):
            continue
        start.append(f"把{d['name']}拉回来，先补{d['weak']}" if d.get("weak") else f"把{d['name']}拉回来（{d['now']} 分）")
        if len(start) == 2:
            break
    full = [w for w in led["weeks"][:-1]] or led["weeks"]
    rule = ""
    if any(w["work"] > WORK_CAP for w in full):
        rule = f"工作每周不超过 {WORK_CAP} 小时"
    elif any(w["sleep"] < SLEEP_STD for w in full):
        rule = f"每周至少 {SLEEP_STD} 天 0 点前睡"
    elif any(w["train"] < TRAIN_STD for w in full):
        rule = f"每周训练 {TRAIN_STD} 次"
    return {"keep": keep, "pause": pause, "start": start, "rule": rule, "by": "rule"}


# ---------- 小伴 写完以后她又记了东西（社交补记了两笔，赛季页还写着“见朋友 0 次”） ----------
# 计分来源里带“记下时间”的表（UTC 时间串，和 season_notes.created_at 同一种写法）
RECORD_SOURCES = (("life_log", "at"), ("habit_log", "at"), ("place_events", "at"), ("english_sessions", "at"),
                  ("body_measures", "at"), ("metric_entries", "at"), ("tasks", "done_at"), ("wishes", "done_at"))
REWRITE_CAP = 3        # 因为“之后有新记录”每天最多重写几次（另有 bgwork 的 15 分钟锁和额度规则）
REWRITES_FILE = "season-note-rewrites.json"


def newer_records(db, note: dict) -> str:
    """小伴 写那几句以后，计分来源里最新一笔的记下时间；没有更新的返回空串。"""
    at = (note or {}).get("at") or ""
    if not at:
        return ""
    latest = ""
    for table, col in RECORD_SOURCES:
        try:
            v = db.conn.execute(f"SELECT MAX({col}) FROM {table} WHERE {col} > ? AND {col} GLOB '[0-9]*'", (at,)).fetchone()[0]
        except Exception:  # 这张表还没建（没接上这个来源）
            v = None
        if v and v > latest:
            latest = v
    return latest


def _has_number(text) -> bool:
    return any(c.isdigit() for c in str(text or ""))


def usable_note(note: dict, newer: str) -> dict:
    """能摆出来的那几句：她之后又记了东西时，带数字的句子先收起来（数字是写的时候的，现在对不上），用代码现算的兜底；
    只写判断、不带数字的照旧显示，旁边标写的日期。"""
    if not note or not newer:
        return note or {}
    out = dict(note)
    if _has_number(out.get("line")):
        out["line"] = ""
    out["why"] = {k: v for k, v in (out.get("why") or {}).items() if not _has_number(v)}
    return out


def note_label(note: dict) -> str:
    """“11 月 5 日”：给页面上“这几句是 X 月 X 日写的”用（存的是 UTC，换成她那边的日子）。"""
    at = (note or {}).get("at") or ""
    if not at:
        return ""
    try:
        d = clock.her_now(now=datetime.fromisoformat(at)).date()
    except ValueError:
        return ""
    return f"{d.month} 月 {d.day} 日"


def _rewrites(root, day: date) -> int:
    from pathlib import Path
    try:
        got = json.loads((Path(root) / REWRITES_FILE).read_text())
    except (OSError, ValueError):
        return 0
    return got.get("n", 0) if isinstance(got, dict) and got.get("day") == day.isoformat() else 0


def may_rewrite(root, day: date, cap: int = REWRITE_CAP) -> bool:
    """因为有新记录而重写：今天还没到上限。真起了后台再 count_rewrite 记一次（15 分钟锁挡下的不算）。"""
    return _rewrites(root, day) < cap


def count_rewrite(root, day: date):
    from pathlib import Path
    try:
        (Path(root) / REWRITES_FILE).write_text(json.dumps({"day": day.isoformat(), "n": _rewrites(root, day) + 1}))
    except OSError:
        pass


def stale(note: dict, day: date) -> bool:
    """小伴 的那句结论该重写了：还没写过，或者是 2 天前写的。"""
    at = (note or {}).get("at") or ""
    return not at or at[:10] < (day - timedelta(days=2)).isoformat()




def kick(root, spawn=None) -> bool:
    """打开赛季页发现结论旧了：后台让 小伴 重写（一次只跑一个，15 分钟内不重复，锁在 bgwork.kick 里）。"""
    from .bgwork import kick as _kick
    return bool(_kick(root, "season-note", (), spawn))


def main():
    import argparse
    from pathlib import Path
    from .cli import build_router
    from .clock import her_now
    from .memory import MemoryStore
    p = argparse.ArgumentParser(description="让 小伴 写赛季页顶上那句结论")
    p.add_argument("--data", type=Path, required=True)
    a = p.parse_args()
    store = MemoryStore(a.data)
    try:
        router = build_router(a.data, 50.0)
        s, start = current(router.settings)
        print(write_note(store.db, router, s, start, her_now(router.settings).date())["line"])
    finally:
        store.close()


if __name__ == "__main__":
    main()
