"""下周重点（2026-10-29 她选的方案 A）：周日周报最后 小伴 提 3 件下周重点，她回一句“行”或改一改，
小伴 在聊天里定下来；计划器下周每天排计划时优先照顾这几件，小伴 聊天时也心里有数。

标记：周报里 [下周重点 第一件 | 第二件 | 第三件]（提议）；她点头或改了以后，聊天里 [下周重点 定 第一件 | 第二件 | 第三件]（定稿）。
她一直没回，下周照提议的来（她说“只需要点头”），只是标成“她还没确认”。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

MARK = re.compile(r"\[下周重点\s+(定\s+)?([^\]]+)\]")
SCHEMA = "CREATE TABLE IF NOT EXISTS week_focus (week TEXT PRIMARY KEY, items TEXT NOT NULL, status TEXT NOT NULL, at TEXT NOT NULL)"


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _items(body: str) -> list:
    parts = [re.sub(r"^\s*\d+[.、)]\s*", "", p).strip() for p in re.split(r"[|｜；;]", body)]
    return [p[:60] for p in parts if p][:3]


def note(db, text: str, today: date) -> list:
    """从 小伴 的话里记下周重点；返回记下的 [(状态, 几件)]。
    提议一律记给下一周；定稿记给最近一条还没过去的提议那周（她周一才回也算），没有就记给下一周。"""
    db.conn.execute(SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    nxt = (_monday(today) + timedelta(days=7)).isoformat()
    got = []
    for done, body in MARK.findall(text):
        items = _items(body)
        if not items:
            continue
        if done:
            r = db.conn.execute("SELECT week FROM week_focus WHERE status = 'proposed' ORDER BY week DESC LIMIT 1").fetchone()
            week = r["week"] if r and r["week"] >= _monday(today).isoformat() else nxt
            status = "confirmed"
        else:
            week, status = nxt, "proposed"
        db.conn.execute("INSERT OR REPLACE INTO week_focus (week, items, status, at) VALUES (?, ?, ?, ?)",
                        (week, json.dumps(items, ensure_ascii=False), status, now))
        got.append((status, len(items)))
    db.conn.commit()
    return got


def strip(text: str) -> str:
    return MARK.sub("", text)


def _row(db, week: str):
    db.conn.execute(SCHEMA)
    r = db.conn.execute("SELECT * FROM week_focus WHERE week = ?", (week,)).fetchone()
    return {"week": r["week"], "items": json.loads(r["items"]), "confirmed": r["status"] == "confirmed"} if r else None


def current(db, day: date):
    """这一周的重点：{"week", "items", "confirmed"} 或 None。"""
    return _row(db, _monday(day).isoformat())


def waiting(db, day: date):
    """周报提了、她还没点头的（下周的，或者已经到了这周还没点头的），给 小伴 聊天时接上。"""
    for week in ((_monday(day) + timedelta(days=7)).isoformat(), _monday(day).isoformat()):
        r = _row(db, week)
        if r and not r["confirmed"]:
            return r
    return None
