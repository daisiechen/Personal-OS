"""提前登记的日程（发来周六的午餐订位：“这个帮我加进明天的日历”）。

工作台“加日程”只能加到已经排好计划的那天；以后的日子还没有计划，硬建一份会让当天 06:30 的计划器以为排过了。
所以以后的日程先记在这里：马上写进苹果日历“小伴 计划”（有自己的 UID，计划器同步时不会删它），
当天排计划时当成她已有的安排（busy），不会在这段时间排别的事。
小伴 聊天里也能记：她说了某天几点的约，他在回复里加 [日程 2026-10-31 14:45-16:45 标题 @地点]。

  python3 -m ethan.events add --data <数据目录> --day 2026-10-31 --start 14:45 --end 16:45 --title "午餐" [--place ..] [--note ..]
"""

import argparse
import re
from datetime import date, datetime
from pathlib import Path
from .clock import her_today

SCHEMA = """CREATE TABLE IF NOT EXISTS future_events (id INTEGER PRIMARY KEY, day TEXT NOT NULL, start TEXT NOT NULL,
    end TEXT NOT NULL, title TEXT NOT NULL, place TEXT, note TEXT, uid TEXT, created_at TEXT NOT NULL)"""
MARK = re.compile(r"\[日程\s+(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})\s*[-–~到]\s*(\d{1,2}:\d{2})\s+([^\]@]+?)\s*(?:@\s*([^\]]+?))?\s*\]")


DECIDE = re.compile(r"\[日程(确认|取消)\s*#(\d+)\]")
MOVE = re.compile(r"\[挪\s*#(\d+)\s+(\d{1,2}:\d{2})\]")  # 计划里的事挪到几点（她点头以后 小伴 写）
CLASH_SQL = """CREATE TABLE IF NOT EXISTS plan_clashes (day TEXT NOT NULL, item_id INTEGER NOT NULL, title TEXT, start TEXT,
    with_title TEXT, with_start TEXT, with_end TEXT, at TEXT, PRIMARY KEY (day, item_id, with_title))"""


def ensure(db):
    db.conn.execute(SCHEMA)
    if "conflict" not in {r[1] for r in db.conn.execute("PRAGMA table_info(future_events)")}:
        db.conn.execute("ALTER TABLE future_events ADD COLUMN conflict TEXT")  # 和她日历上已有的撞了：先不写进日历，等她定


def _hm(t: str) -> str:
    return datetime.strptime(t, "%H:%M").strftime("%H:%M")


def _mins(hm: str) -> int:
    h, m = hm.split(":")[:2]
    return int(h) * 60 + int(m)


_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{3,}")
_COMMON = set("的了和与在去到一个活动会议")


def same_event(a, b) -> bool:
    """同一天的两条安排是不是同一件事（——同一个约在邮件、聊天、她自己日历里
    叫法不一样，比如“某商学院 交换生送别活动”和“IEPAUT26 Farewell Event”）。a、b 是 (开始, 结束, 标题, 地点)。
    时间要重叠、开始相差不到 30 分钟，再加上标题像、地点对得上、或共用两个以上的字 / 一个英文词，才算同一件。"""
    (s1, e1, t1, p1), (s2, e2, t2, p2) = a, b
    if not (s1 < e2 and s2 < e1) or abs(_mins(s1) - _mins(s2)) > 30:
        return False
    t1, t2, p1, p2 = (t1 or "").strip(), (t2 or "").strip(), (p1 or "").strip().lower(), (p2 or "").strip().lower()
    from .core.agent import similar_task
    if t1 and t2 and similar_task(t1, [t2]):
        return True
    if len(p1) >= 4 and len(p2) >= 4 and (p1 in p2 or p2 in p1):
        return True
    cjk = lambda t: {c for c in t if "\u4e00" <= c <= "\u9fff" and c not in _COMMON}
    if len(cjk(t1) & cjk(t2)) >= 2:
        return True
    words = lambda t: {w.lower() for w in _WORD.findall(t)}
    return bool(words(t1 + " " + p1) & words(t2 + " " + p2))


def add(db, day: str, start: str, end: str, title: str, place: str = "", note: str = "") -> int:
    ensure(db)
    date.fromisoformat(day)
    start, end = _hm(start), _hm(end)
    if end <= start:
        raise ValueError("结束要晚于开始")
    same = db.conn.execute("SELECT id FROM future_events WHERE day = ? AND start = ? AND title = ?", (day, start, title.strip())).fetchone()
    if same:  # 同一个约说了两遍，不重复记
        return same["id"]
    for r in db.conn.execute("SELECT * FROM future_events WHERE day = ?", (day,)).fetchall():  # 换了个叫法也算同一个约
        if same_event((start, end, title, place), (r["start"], r["end"], r["title"], r["place"])):
            return r["id"]
    cur = db.conn.execute("INSERT INTO future_events (day, start, end, title, place, note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                          (day, start, end, title.strip()[:80], (place or "").strip()[:80], (note or "").strip()[:300],
                           datetime.now().isoformat(timespec="seconds")))
    db.conn.commit()
    return cur.lastrowid


def note_chat(db, text: str) -> list:
    out = []
    for verb, eid in DECIDE.findall(text):  # 撞时间的那条：她定了去不去
        ensure(db)
        if verb == "确认":
            db.conn.execute("UPDATE future_events SET conflict = NULL, note = '她确认过' WHERE id = ?", (int(eid),))
        else:
            db.conn.execute("DELETE FROM future_events WHERE id = ?", (int(eid),))
        db.conn.commit()
    for day, s, e, title, place in MARK.findall(text):
        try:
            out.append(add(db, day, s, e, title, place or ""))
        except ValueError:
            continue
    return out


def strip(text: str) -> str:
    return MOVE.sub("", DECIDE.sub("", MARK.sub("", text)))


# ---------- 计划里的事和她后来约的事撞了（Sunday Roast 和训练撞了，为什么没先问要不要把训练往后推） ----------
def find_plan_clashes(db, day: date, busy: list, now_hm: str = "00:00") -> list:
    """今天计划里还没开始的日程（训练等），和她日历上的安排撞了的，记下来等 小伴 问她。返回新记下的。"""
    db.conn.execute(CLASH_SQL)
    plan = db.latest_plan(day.isoformat())
    if not plan:
        return []
    new = []
    for i in plan["items"]:
        if i["kind"] != "event" or not i.get("start") or i["start"] < now_hm:
            continue
        for b in busy:
            if b[0] < (i.get("end") or i["start"]) and b[1] > i["start"]:
                cur = db.conn.execute("INSERT OR IGNORE INTO plan_clashes (day, item_id, title, start, with_title, with_start, with_end, at) "
                                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (day.isoformat(), i["id"], i["title"], i["start"], b[2], b[0], b[1],
                                                                          datetime.now().isoformat(timespec="seconds")))
                if cur.rowcount:
                    new.append({"item_id": i["id"], "title": i["title"], "start": i["start"], "end": i.get("end"), "with": b[2], "with_start": b[0], "with_end": b[1]})
    db.conn.commit()
    return new


def open_plan_clashes(db, day: date) -> list:
    db.conn.execute(CLASH_SQL)
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM plan_clashes WHERE day = ?", (day.isoformat(),))]
    out = []
    for r in rows:  # 已经挪开了的不算
        it = db.conn.execute("SELECT start, end FROM plan_items WHERE id = ?", (r["item_id"],)).fetchone()
        if it and it["start"] < r["with_end"] and (it["end"] or it["start"]) > r["with_start"]:
            out.append(r)
    return out


def move_items(db, text: str) -> list:
    """[挪 #计划条目 HH:MM]：保持时长挪到新时间；返回挪了的 (编号, 日期)。"""
    out = []
    for iid, hm in MOVE.findall(text):
        it = db.conn.execute("SELECT i.*, p.day FROM plan_items i JOIN daily_plans p ON p.id = i.plan_id WHERE i.id = ? AND i.kind = 'event'",
                             (int(iid),)).fetchone()
        if not it:
            continue
        h, m = map(int, hm.split(":"))
        sh, sm = map(int, it["start"].split(":"))
        eh, em = map(int, (it["end"] or it["start"]).split(":"))
        dur = (eh * 60 + em) - (sh * 60 + sm)
        end = min(23 * 60 + 59, h * 60 + m + max(dur, 0))
        db.conn.execute("UPDATE plan_items SET start = ?, end = ? WHERE id = ?", (f"{h:02d}:{m:02d}", f"{end // 60:02d}:{end % 60:02d}", it["id"]))
        out.append((it["id"], it["day"]))
    db.conn.commit()
    return out


def clashes(busy: list, start: str, end: str) -> list:
    """她那天已有的安排里和 start–end 撞的：[标题]。busy 是 ICloudCalendar.busy 的格式。"""
    return [b[2] for b in busy if b[0] < end and b[1] > start]


def conflicts(db) -> list:
    ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT * FROM future_events WHERE conflict IS NOT NULL AND day >= ? ORDER BY day, start",
                                             (her_today().isoformat(),))]


def for_day(db, day: date) -> list:
    """当天提前登记的日程，给计划器当 busy：[(开始, 结束, 标题, {"location": 地点})]"""
    ensure(db)
    return [(r["start"], r["end"], r["title"], {"location": r["place"] or ""})
            for r in db.conn.execute("SELECT * FROM future_events WHERE day = ? ORDER BY start", (day.isoformat(),))]


def sync(db, calendar) -> int:
    """还没写进苹果日历的写进去（今天和以后的）。返回写了几条。"""
    ensure(db)
    if calendar is None:
        return 0
    from .icloud import LONDON, event_ics
    import urllib.parse
    rows = db.conn.execute("SELECT * FROM future_events WHERE uid IS NULL AND day >= ?", (her_today().isoformat(),)).fetchall()
    if not rows:
        return 0
    cal = calendar.ethan_calendar()
    written = 0
    for r in rows:
        if r["conflict"] is None and r["note"] != "她确认过":  # 写之前先看她日历上那段时间有没有别的安排（撞了为什么没人找我确认）
            try:
                busy = calendar.busy(date.fromisoformat(r["day"]))
            except Exception:
                busy = []
            dup = next((b[2] for b in busy if same_event((r["start"], r["end"], r["title"], r["place"]),
                                                         (b[0], b[1], b[2], (b[3] if len(b) > 3 else {}).get("location")))), None)
            hit = clashes(busy, r["start"], r["end"])
            if dup:  # 她日历里已经有这件（叫法不同）：不再写一遍，也不算撞了
                db.conn.execute("UPDATE future_events SET uid = ?, note = ? WHERE id = ?", (f"dup:{r['id']}", f"和你日历里的「{dup}」是同一件"[:300], r["id"]))
                continue
            if hit:
                db.conn.execute("UPDATE future_events SET conflict = ? WHERE id = ?", ("、".join(hit)[:200], r["id"]))
                continue
        elif r["conflict"] is not None:
            continue  # 等她定
        uid = f"ethan-event-{r['id']}"
        d = date.fromisoformat(r["day"])
        s = datetime.combine(d, datetime.strptime(r["start"], "%H:%M").time(), LONDON)
        e = datetime.combine(d, datetime.strptime(r["end"], "%H:%M").time(), LONDON)
        ics = event_ics(uid, s, e, r["title"], r["note"] or "", r["place"] or "", alarm=f"一小时后：{r['title']}", alarm_before=60)
        calendar.request("PUT", urllib.parse.urljoin(cal, f"{uid}.ics"), ics.encode("utf-8"), ctype="text/calendar")
        db.conn.execute("UPDATE future_events SET uid = ? WHERE id = ?", (uid, r["id"]))
        written += 1
    db.conn.commit()
    return written


def dedupe_existing(db, cal) -> str:
    """一次性清掉今天以后已经写重的。只动 小伴 写的那份，她自己的不删。
    迁移脚本跑的时候拿不到日历账号，改在每 10 分钟那一轮里跑一次（ethan/proactive.py）。"""
    import urllib.parse
    ensure(db)
    db.conn.execute("CREATE TABLE IF NOT EXISTS events_meta (key TEXT PRIMARY KEY, value TEXT)")
    if db.conn.execute("SELECT 1 FROM events_meta WHERE key = 'deduped_20261104'").fetchone():
        return ""
    ethan_cal = cal.ethan_calendar()
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM future_events WHERE day >= ? ORDER BY day, id", (her_today().isoformat(),))]
    removed, merged = [], []

    def unwrite(r):
        if r["uid"] and r["uid"].startswith("ethan-event-"):
            cal.request("DELETE", urllib.parse.urljoin(ethan_cal, f"{r['uid']}.ics"))

    kept = []
    for r in rows:  # 小伴 自己记重复的：留最早那条
        twin = next((k for k in kept if k["day"] == r["day"] and same_event(
            (r["start"], r["end"], r["title"], r["place"]), (k["start"], k["end"], k["title"], k["place"]))), None)
        if twin:
            unwrite(r)
            db.conn.execute("DELETE FROM future_events WHERE id = ?", (r["id"],))
            merged.append(f"{r['day']} {r['title']}（同 {twin['title']}）")
        else:
            kept.append(r)
    busy_cache = {}
    for r in kept:  # 和她日历里的是同一件：小伴 那份不留在日历上
        if not (r["uid"] or "").startswith("ethan-event-"):
            continue
        if r["day"] not in busy_cache:
            busy_cache[r["day"]] = cal.busy(date.fromisoformat(r["day"]))
        dup = next((b[2] for b in busy_cache[r["day"]] if same_event(
            (r["start"], r["end"], r["title"], r["place"]), (b[0], b[1], b[2], (b[3] if len(b) > 3 else {}).get("location")))), None)
        if dup:
            unwrite(r)
            db.conn.execute("UPDATE future_events SET uid = ?, note = ? WHERE id = ?",
                            (f"dup:{r['id']}", f"和你日历里的「{dup}」是同一件"[:300], r["id"]))
            removed.append(f"{r['day']} {r['title']}（同 {dup}）")
    db.conn.execute("INSERT OR REPLACE INTO events_meta (key, value) VALUES ('deduped_20261104', ?)", (her_today().isoformat(),))
    db.conn.commit()
    return f"合并 小伴 自己重复记的 {len(merged)} 条：{merged}；删掉和你日历重复的 {len(removed)} 条：{removed}"


def find_dupes(cal, start: date, days: int) -> list:
    """只读：这几天所有日历里像是同一件的两两一对。"""
    from datetime import timedelta
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        evs = cal.all_events(d)
        for a in range(len(evs)):
            for b in range(a + 1, len(evs)):
                x, y = evs[a], evs[b]
                if same_event((x[0], x[1], x[2], x[3].get("location")), (y[0], y[1], y[2], y[3].get("location"))):
                    out.append({"day": d.isoformat(), "a": f"{x[3].get('calendar')}｜{x[0]}-{x[1]}｜{x[2]}｜{x[3].get('location') or ''}",
                                "b": f"{y[3].get('calendar')}｜{y[0]}-{y[1]}｜{y[2]}｜{y[3].get('location') or ''}"})
        out.append({"day": d.isoformat(), "all": [f"{e[3].get('calendar')}｜{e[0]}-{e[1]}｜{e[2]}｜{e[3].get('location') or ''}" for e in evs]})
    return out


def write_dupes_report(cal, root, today=None, days: int = 14) -> int:
    """每小时一次，把今天起几天里像是同一件的日程写到数据目录的 calendar-dupes.json（只读日历；
    服务器上的只读会话碰不了日历密码，读这个文件就行）。返回疑似重复几对。"""
    import json
    from datetime import datetime, timezone
    p = Path(root) / "calendar-dupes.json"
    try:
        import time
        if p.exists() and (time.time() - p.stat().st_mtime) < 3600:
            return -1
    except OSError:
        pass
    got = find_dupes(cal, today or her_today(), days)
    pairs = [x for x in got if "a" in x]
    p.write_text(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "pairs": pairs,
                             "days": [x for x in got if "all" in x]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return len(pairs)


def main():
    p = argparse.ArgumentParser(description="提前登记以后的日程")
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--data", type=Path, required=True)
    a.add_argument("--day", required=True)
    a.add_argument("--start", required=True)
    a.add_argument("--end", required=True)
    a.add_argument("--title", required=True)
    a.add_argument("--place", default="")
    a.add_argument("--note", default="")
    s = sub.add_parser("sync")
    s.add_argument("--data", type=Path, required=True)
    du = sub.add_parser("dupes", help="只读：列出这几天所有日历的事，标出像是同一件的")
    du.add_argument("--data", type=Path, required=True)
    du.add_argument("--day", required=True)
    du.add_argument("--days", type=int, default=1)
    args = p.parse_args()
    from .icloud import build_calendar
    from .memory import MemoryStore
    if args.cmd == "dupes":
        import json
        print(json.dumps(find_dupes(build_calendar(), date.fromisoformat(args.day), args.days), ensure_ascii=False, indent=1))
        return
    store = MemoryStore(args.data)
    try:
        if args.cmd == "add":
            print(f"记下了 #{add(store.db, args.day, args.start, args.end, args.title, args.place, args.note)}")
        print(f"写进苹果日历 {sync(store.db, build_calendar())} 条")
    finally:
        store.close()


if __name__ == "__main__":
    main()
