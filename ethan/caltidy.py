"""每天整理她日历里导入的事件（她 2026-11-04：“小伴 每天帮我整理日历的时候，除了去重，还要做必要的调整，比如 11.10 的日历，
我是从网上下载的时间直接安装的，小伴 要把全部内容换成中文，要从中提取时间和地点填进日历里，以及必要的信息放在描述里。”）

- 看她自己的日历（不含“小伴 计划”），今天起 14 天内、标题不是中文、或者描述很长但没填地点的事件。
- 模型给出中文标题、地点、（描述里明确写了的）时间、要点；描述写成“要点 + 原文”，原文一字不改留在最下面。
- 改过的打上 X-ETHAN-TIDY 标记，以后不再动（她自己再改也不会被覆盖）；改之前把原样存进 cal_tidy 表，--undo 能恢复。
- 循环日程整串一起改标题、地点、描述，不改时间。
- 先预览：--preview 只出改法不写；她看过、点头后用 --on 打开每天自动整理（默认关着）。
"""

import argparse
import hashlib
import json
import re
import urllib.parse
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .clock import her_today

SCHEMA = """CREATE TABLE IF NOT EXISTS cal_tidy (id INTEGER PRIMARY KEY, href TEXT NOT NULL, uid TEXT, day TEXT, before_ics TEXT NOT NULL,
    after_ics TEXT, title_before TEXT, title_after TEXT, at TEXT NOT NULL, undone_at TEXT)"""
MARK = "X-ETHAN-TIDY"
DAYS_AHEAD = 14
OUT = {"type": "object", "required": ["changed", "title", "location", "notes"], "properties": {
    "changed": {"type": "boolean"}, "title": {"type": "string", "maxLength": 40}, "location": {"type": "string", "maxLength": 160},
    "start": {"type": ["string", "null"]}, "end": {"type": ["string", "null"]},
    "notes": {"type": "array", "items": {"type": "string", "maxLength": 120}, "maxItems": 6}}}
CJK = re.compile(r"[\u4e00-\u9fff]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.execute(SCHEMA)
    db.conn.execute("CREATE TABLE IF NOT EXISTS cal_tidy_state (key TEXT PRIMARY KEY, value TEXT)")
    # 预览过的改法存下来，真写的时候照这份写（服务器 11-04：同一条两次预览，模型起的标题不一样）
    db.conn.execute("CREATE TABLE IF NOT EXISTS cal_tidy_plan (href TEXT NOT NULL, ics_hash TEXT NOT NULL, got TEXT NOT NULL, at TEXT NOT NULL, "
                    "PRIMARY KEY (href, ics_hash))")


def is_on(db) -> bool:
    ensure(db)
    r = db.conn.execute("SELECT value FROM cal_tidy_state WHERE key = 'on'").fetchone()
    return bool(r and r[0] == "1")


def set_on(db, on: bool):
    ensure(db)
    db.conn.execute("INSERT OR REPLACE INTO cal_tidy_state (key, value) VALUES ('on', ?)", ("1" if on else "0",))
    db.conn.commit()


# ---------- ICS 小工具 ----------
def unfold(ics: str) -> list:
    return ics.replace("\r\n ", "").replace("\r\n\t", "").replace("\n ", "").replace("\r\n", "\n").split("\n")


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def _escape(v: str) -> str:
    return (v or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def master(lines: list) -> dict:
    """第一个不带 RECURRENCE-ID 的 VEVENT：{title, location, description, start_line, end_line, recurring, tidied, uid}"""
    out, cur, depth = None, None, 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {"title": "", "location": "", "description": "", "start_line": "", "end_line": "", "recurring": False,
                   "override": False, "tidied": False, "uid": ""}
        elif line == "END:VEVENT" and cur is not None:
            if not cur["override"] and out is None:
                out = cur
            cur = None
        elif line.startswith("BEGIN:VALARM"):
            depth += 1
        elif line.startswith("END:VALARM"):
            depth -= 1
        elif cur is not None and depth == 0 and ":" in line:
            head, value = line.split(":", 1)
            name = head.split(";", 1)[0].upper()
            if name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "DESCRIPTION":
                cur["description"] = _unescape(value)
            elif name == "DTSTART":
                cur["start_line"] = line
            elif name == "DTEND":
                cur["end_line"] = line
            elif name == "RRULE":
                cur["recurring"] = True
            elif name == "RECURRENCE-ID":
                cur["override"] = True
            elif name == MARK:
                cur["tidied"] = True
            elif name == "UID":
                cur["uid"] = value
    return out


def needs_tidy(ev: dict) -> bool:
    if not ev or ev["tidied"]:
        return False
    title_foreign = bool(ev["title"].strip()) and not CJK.search(ev["title"])
    buried = len(ev["description"]) > 200 and not ev["location"].strip()
    return title_foreign or buried


def _day_of(start_line: str):
    m = re.search(r":(\d{8})", start_line or "")
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else None


def rewrite(ics: str, ev: dict, got: dict) -> str:
    """只改主事件的 SUMMARY / LOCATION / DESCRIPTION（和不循环时的时间），其余原样保留，加上 X-ETHAN-TIDY。"""
    notes = [n.strip() for n in got.get("notes") or [] if n.strip()]
    desc = "\n".join(f"· {n}" for n in notes)
    original = (f"原标题：{ev['title']}\n" if got["title"] != ev["title"] else "") + (ev["description"] or "")
    if original.strip():
        desc += ("\n\n" if desc else "") + "——原文（小伴 整理前）——\n" + original.strip()
    stamp = hashlib.sha1(f"{got['title']}|{got['location']}|{desc}".encode()).hexdigest()[:12]
    new_start = new_end = None
    day = _day_of(ev["start_line"])
    if not ev["recurring"] and day and got.get("start") and got.get("end") and re.fullmatch(r"\d{2}:\d{2}", got["start"]) \
            and re.fullmatch(r"\d{2}:\d{2}", got["end"]) and got["end"] > got["start"]:
        d = day.replace("-", "")
        new_start = f"DTSTART;TZID=Europe/London:{d}T{got['start'].replace(':', '')}00"
        new_end = f"DTEND;TZID=Europe/London:{d}T{got['end'].replace(':', '')}00"
    lines, out, cur, depth, done = unfold(ics), [], None, 0, False
    for line in lines:
        if line == "BEGIN:VEVENT":
            cur = {"buf": [line], "override": False}
            continue
        if cur is not None:
            if line.startswith("BEGIN:VALARM"):
                depth += 1
            elif line.startswith("END:VALARM"):
                depth -= 1
            if line.upper().startswith("RECURRENCE-ID"):
                cur["override"] = True
            if line == "END:VEVENT":
                buf = cur["buf"]
                if not cur["override"] and not done:
                    keep, inner = [], 0  # 提醒（VALARM）里的 DESCRIPTION 不动
                    for l in buf:
                        if l.startswith("BEGIN:VALARM"):
                            inner += 1
                        elif l.startswith("END:VALARM"):
                            inner -= 1
                        elif inner == 0 and l.split(":", 1)[0].split(";", 1)[0].upper() in ("SUMMARY", "LOCATION", "DESCRIPTION", MARK):
                            continue
                        keep.append(l)
                    if new_start:
                        keep = [new_start if l.upper().startswith("DTSTART") else new_end if l.upper().startswith("DTEND") else l for l in keep]
                    buf = keep + [f"SUMMARY:{_escape(got['title'])}", f"LOCATION:{_escape(got['location'])}",
                                  f"DESCRIPTION:{_escape(desc)}", f"{MARK}:{stamp}"]
                    done = True
                out += buf + [line]
                cur = None
                continue
            cur["buf"].append(line)
            continue
        out.append(line)
    return "\r\n".join(l for l in out if l != "") + "\r\n"


# ---------- 读她日历里的原样事件 ----------
def raw_events(cal, start: date, days: int = DAYS_AHEAD) -> list:
    """她自己日历里（不含 小伴 计划）这段时间的事件原文：[{href, ics, calendar}]。不展开循环，拿到的是整串的原文。"""
    import xml.etree.ElementTree as ET
    from .icloud import CALENDAR_NAME, LONDON, NS, _body, _ics_time
    s = datetime(start.year, start.month, start.day, tzinfo=LONDON)
    rng = f'start="{_ics_time(s)}" end="{_ics_time(s + timedelta(days=days))}"'
    query = _body('<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop><c:calendar-data/></d:prop>'
                  '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
                  f'<c:time-range {rng}/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
    out = []
    for name, url in cal.calendars().items():
        if name == CALENDAR_NAME:
            continue
        _, xml = cal.request("REPORT", url, query, depth="1")
        if not xml.strip():
            continue
        for resp in ET.fromstring(xml).findall("d:response", NS):
            node = resp.find(".//c:calendar-data", NS)
            href = urllib.parse.urljoin(url, (resp.findtext("d:href", default="", namespaces=NS) or "").strip())
            if node is not None and node.text:
                out.append({"href": href, "ics": node.text.replace("\r\n", "\n").replace("\n", "\r\n"), "calendar": name})  # XML 解析吃掉了 \r
    return out


def propose(router, db, ev: dict) -> dict:
    from . import llm
    version, system = llm.load_prompt("cal_tidy")
    text = (f"标题：{ev['title']}\n地点：{ev['location'] or '（空）'}\n开始：{ev['start_line']}\n结束：{ev['end_line']}\n"
            f"循环：{'是' if ev['recurring'] else '否'}\n描述：\n{ev['description'][:4000]}")
    return llm.structured(router, db, "cal_tidy", system, text, OUT, version=version, input_ref=ev.get("uid", ""), purpose="cal-tidy")


def run(db, cal, router, start: date = None, days: int = DAYS_AHEAD, preview: bool = True, only_day: str = None) -> list:
    """整理（或只预览）这段时间要整理的事件。返回每条的改法。"""
    ensure(db)
    start = start or her_today()
    out = []
    for r in raw_events(cal, start, days):
        ev = master(unfold(r["ics"]))
        if not needs_tidy(ev):
            continue
        day = _day_of(ev["start_line"])
        if only_day and day != only_day and not ev["recurring"]:
            continue
        h = hashlib.sha1(r["ics"].encode()).hexdigest()[:16]
        saved = db.conn.execute("SELECT got FROM cal_tidy_plan WHERE href = ? AND ics_hash = ?", (r["href"], h)).fetchone()
        got = json.loads(saved[0]) if saved else propose(router, db, ev)
        if not got or not got.get("changed"):
            continue
        if not saved:
            db.conn.execute("INSERT OR REPLACE INTO cal_tidy_plan (href, ics_hash, got, at) VALUES (?, ?, ?, ?)",
                            (r["href"], h, json.dumps(got, ensure_ascii=False), _now()))
            db.conn.commit()
        new_ics = rewrite(r["ics"], ev, got)
        item = {"day": day, "calendar": r["calendar"], "before": {"title": ev["title"], "location": ev["location"]},
                "after": {"title": got["title"], "location": got["location"], "start": got.get("start"), "end": got.get("end"),
                          "notes": got.get("notes") or []}, "recurring": ev["recurring"]}
        if not preview:
            db.conn.execute("INSERT INTO cal_tidy (href, uid, day, before_ics, after_ics, title_before, title_after, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (r["href"], ev["uid"], day, r["ics"], new_ics, ev["title"], got["title"], _now()))
            db.conn.commit()  # 先存原样再写，写坏了也能恢复
            cal.request("PUT", r["href"], new_ics.encode("utf-8"), ctype="text/calendar")
            item["written"] = True
        out.append(item)
    return out


def undo(db, cal, hours: float = 48) -> int:
    """把最近整理过的恢复成原样。"""
    ensure(db)
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    n = 0
    for r in db.conn.execute("SELECT * FROM cal_tidy WHERE undone_at IS NULL AND at >= ? ORDER BY id DESC", (cut,)).fetchall():
        cal.request("PUT", r["href"], r["before_ics"].encode("utf-8"), ctype="text/calendar")
        db.conn.execute("UPDATE cal_tidy SET undone_at = ? WHERE id = ?", (_now(), r["id"]))
        n += 1
    db.conn.commit()
    return n


def daily(store, router, cal=None) -> str:
    """每天跑一次（打开了才跑；同一天跑过就不再跑）。放在每 10 分钟那一轮里（ethan/proactive.py，那里有日历账号）。"""
    db = store.db
    if not is_on(db):
        return ""
    ensure(db)
    today = her_today().isoformat()
    r = db.conn.execute("SELECT value FROM cal_tidy_state WHERE key = 'last_day'").fetchone()
    if r and r[0] == today:
        return ""
    if cal is None:
        from .icloud import build_calendar
        cal = build_calendar()
    if cal is None:
        return "没有日历账号"
    got = run(db, cal, router, preview=False)
    db.conn.execute("INSERT OR REPLACE INTO cal_tidy_state (key, value) VALUES ('last_day', ?)", (today,))
    db.conn.commit()
    return f"整理了 {len(got)} 条：" + "；".join(f"{x['day']} {x['before']['title']} → {x['after']['title']}" for x in got)


def main():
    p = argparse.ArgumentParser(description="整理她日历里导入的事件（换中文、填时间地点、要点进描述）")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--preview", action="store_true", help="只出改法，不写日历")
    p.add_argument("--apply", action="store_true", help="照改法写进日历")
    p.add_argument("--day", help="只看这一天（YYYY-MM-DD）")
    p.add_argument("--on", action="store_true", help="打开每天自动整理")
    p.add_argument("--off", action="store_true")
    p.add_argument("--undo", action="store_true", help="恢复最近 48 小时整理过的")
    a = p.parse_args()
    from .cli import build_router
    from .icloud import build_calendar
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        if a.on or a.off:
            set_on(store.db, a.on)
            print("每天自动整理：" + ("开" if a.on else "关"))
            return
        cal = build_calendar()
        if cal is None:
            print("没有日历账号")
            return
        if a.undo:
            print(f"恢复了 {undo(store.db, cal)} 条")
            return
        got = run(store.db, cal, build_router(a.data, 50.0), preview=not a.apply,
                  start=date.fromisoformat(a.day) if a.day else None, days=1 if a.day else DAYS_AHEAD, only_day=a.day)
        print(json.dumps(got, ensure_ascii=False, indent=1))
    finally:
        store.close()


if __name__ == "__main__":
    main()
