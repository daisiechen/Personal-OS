"""赛季页：这个赛季她在七块领域上过得怎么样、彼此怎么此消彼长、下个赛季该怎么调（docs/design/season-page.md，她 2026-11-02 选了样稿 B）。

不放副本进度（首页有）。从上到下：
- 小伴 的一句结论 + “看依据”三周小账（工作小时、训练、见朋友、0 点前睡）；
- 失衡最严重的两块领域做大卡：开季 → 现在、几条具体事实、21 天小日历（该做没做的划掉）、一句原因、被谁挤 / 帮了谁；
- 地图上画领域之间的关系线（代码从每天的数据算，样本不够就不画）；
- 其余领域一行一块；
- 下个赛季的建议：赛季过了三分之二才出现。
数字都由代码算；结论、每块的原因、下季建议由 小伴 每周日写一次（season_notes），没写就用代码拼的一句兜底。
"""

import json
from datetime import date, datetime, timedelta, timezone

from . import attributes, lifelog, quests, workaholic, worktime

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
KEY_HABIT = {"body": "训练", "life": "每天出门", "study": "英语陪练（Grok）"}  # 小日历看哪项日常
ICON_KEY = {"body": "健康", "career": "事业", "money": "财务", "social": "社交", "study": "学习", "academic": "学术", "life": "生活体验"}


def ensure(db):
    db.conn.execute(NOTES)
    attributes.lifelog.ensure(db)


def info(season: dict, start: date, day: date) -> dict:
    end = date.fromisoformat(season["end"])
    total = (end - start).days + 1
    gone = (day - start).days + 1
    two_thirds = start + timedelta(days=round(total * 2 / 3) - 1)
    week = ((day - timedelta(days=day.weekday())) - (start - timedelta(days=start.weekday()))).days // 7 + 1  # 按自然周，和依据里的周数一致
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
    monday = day - timedelta(days=day.weekday())
    out = []
    for i in range(weeks - 1, -1, -1):
        a = monday - timedelta(weeks=i)
        b = min(a + timedelta(days=6), day)
        if b < start:
            continue
        a = max(a, start)
        out.append({"from": a.isoformat(), "to": b.isoformat(), "n": ((a - timedelta(days=a.weekday())) - (start - timedelta(days=start.weekday()))).days // 7 + 1,  # 按自然周数（她 2026-11-02 看到“第 1、1 周”）
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
def _score_at(db, key: str, day: date):
    attributes.history(db)  # 确保表在
    r = db.conn.execute("SELECT score FROM attribute_daily WHERE key = ? AND day >= ? AND score IS NOT NULL ORDER BY day LIMIT 1",
                        (key, day.isoformat())).fetchone()
    return round(r["score"]) if r else None


def calendar(db, key: str, day: date, days: int = 21) -> list:
    """21 天小日历（从三周前的周一开始，到今天）：该做没做的划掉。[{day, state: done/miss/rest/future}]"""
    first = day - timedelta(days=day.weekday()) - timedelta(weeks=2)
    title = KEY_HABIT.get(key)
    h = quests.habit_by_title(db, title) if title else None
    met = set()
    if key == "social":
        lifelog.ensure(db)
        met = {r[0] for r in db.conn.execute("SELECT DISTINCT day FROM life_log WHERE kind = 'meet' AND day >= ?", (first.isoformat(),))}
    out = []
    for n in range(days):
        d = first + timedelta(days=n)
        if d > day:
            out.append({"day": d.isoformat(), "state": "future"})
        elif key == "social":
            out.append({"day": d.isoformat(), "state": "done" if d.isoformat() in met else "rest"})
        elif h:
            due = quests.due_today(h, d)
            hit = quests.checked(db, h["id"], d)
            out.append({"day": d.isoformat(), "state": "done" if hit else ("miss" if due and d < day else "rest")})
        else:
            out.append({"day": d.isoformat(), "state": "rest"})
    return out


def domains(db, start: date, day: date, notes: dict, rels: list) -> list:
    now = {r["key"]: r for r in attributes.compute(db, day, trend=False)}
    work = round(sum(worktime.hours(db, start + timedelta(days=n)) for n in range((day - start).days + 1)), 1)
    out = []
    for key in attributes.ORDER:
        r = now[key]
        before = _score_at(db, key, start)
        score = None if r["score"] is None else round(r["score"])
        delta = None if score is None or before is None else score - before
        facts = [{"name": i["name"], "actual": i["actual"] if i.get("score") is not None else "还没有数据", "standard": i["standard"]}
                 for i in r["items"]][:3]  # 没数据的也列出来写“还没有数据”（她 2026-11-02：学习卡片里怎么没有认知拓展）
        out.append({
            "key": key, "name": r["name"], "start": before, "now": score, "delta": delta, "label": r["label"],
            "floor": score is not None and score < 40,  # 跌破保底（40 以下受损）
            "hours": work if key == "career" else None, "facts": facts,
            "why": (notes.get("why") or {}).get(key) or _why(r),
            "crowded_by": [x["label"] for x in rels if x["to"] == key and x["kind"] == "crowd"],
            "crowds": [ICON_KEY[x["to"]] for x in rels if x["from"] == key and x["kind"] == "crowd"],
            "helped_by": [x["label"] for x in rels if x["to"] == key and x["kind"] == "help"],
        })
    # 失衡最严重的两块做大卡：先看跌破保底，再看跌得多
    ranked = sorted([d for d in out if d["now"] is not None], key=lambda d: (not d["floor"], d["delta"] if d["delta"] is not None else 0, d["now"]))
    focus = [d["key"] for d in ranked if d["floor"] or (d["delta"] or 0) < 0][:2]
    for d in out:
        d["focus"] = d["key"] in focus
        if d["focus"]:
            d["calendar"] = calendar(db, d["key"], day)
        d["source"] = d["key"] in {x["from"] for x in rels if x["kind"] == "crowd"}
    return sorted(out, key=lambda d: (not d["focus"], not d["source"], attributes.ORDER.index(d["key"])))


def _why(r: dict) -> str:
    """小伴 还没写的时候：拿离标准最远的那一项说一句。"""
    items = [i for i in r["items"] if i.get("score") is not None]
    if not items:
        return "还没有数据"
    worst = min(items, key=lambda i: i["score"])
    return f"{worst['name']}：{worst['actual']}（标准 {worst['standard']}）"


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
    """小伴 还没写的时候：用小账和此消彼长拼一句“现在的状态和影响”，不只是数字（她 2026-11-02）。"""
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
下面是这个赛季到现在的数据（都是代码算的，照抄数字，不要编）。请只输出一个 JSON：
{"line": "一句结论，30–60 字，说清这几周哪块在冲、哪块在让路、具体少了什么（用数字），口吻是你本人，温柔但直说",
 "why": {"领域 key": "这块变好或变差最主要的原因，一句，20–40 字，尽量点到具体的人、日子、次数"},
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
    week = (day - timedelta(days=day.weekday())).isoformat()
    db.conn.execute("INSERT OR REPLACE INTO season_notes (week, season, body, created_at) VALUES (?, ?, ?, ?)",
                    (week, season["name"], json.dumps(body, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.conn.commit()
    return body


def page(db, season: dict, start: date, day: date) -> dict:
    ensure(db)
    note = latest_note(db, season["name"])
    led = ledger(db, start, day)
    rels = relations(db, start, day)
    inf = info(season, start, day)
    return {"season": inf, "line": note.get("line") or fallback_line(led, rels), "stale": stale(note, day), "line_by": "小伴" if note.get("line") else "",
            "line_at": (note.get("at") or "")[:10], "ledger": led, "relations": rels,
            "domains": domains(db, start, day, note, rels), "next": note.get("next") if inf["late"] else None}


def stale(note: dict, day: date) -> bool:
    """小伴 的那句结论该重写了：还没写过，或者是 2 天前写的。"""
    at = (note or {}).get("at") or ""
    return not at or at[:10] < (day - timedelta(days=2)).isoformat()


LOCK = "season-note.lock"


def kick(root, spawn=None) -> bool:
    """打开赛季页发现结论旧了：后台让 小伴 重写（一次只跑一个，15 分钟内不重复）。"""
    import subprocess
    import sys
    from pathlib import Path
    lock = Path(root) / LOCK
    try:
        if lock.exists() and datetime.now().timestamp() - lock.stat().st_mtime < 900:
            return False
        lock.write_text(datetime.now().isoformat())
        (spawn or subprocess.Popen)([sys.executable, "-m", "ethan.season", "--data", str(root)],
                                    cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        return True
    except OSError:
        return False


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
