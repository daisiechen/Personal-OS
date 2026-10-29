"""iPhone 定位：出门和到家（身体领地“每天出门”、生活体验“出门做非工作的事”，见 docs/design/territories.md）。

她的 iPhone 用快捷指令的“个人自动化”：离开家时、到家时各发一条到接收口 /cloud/place（设置步骤见
docs/operations/devices.md）。只报“离开 / 到达家”这一件事，不报具体在哪，不追踪路线。
- 当天第一次离开家：自动打卡日常“每天出门”（第一次收到定位时把这个日常打开）。
- 一趟出门（离开 → 到家）不是去训练的、出去满 1 小时，算一次“出门做非工作的事”。
"""

from datetime import date, datetime, timedelta, timezone

from . import quests

SCHEMA = "CREATE TABLE IF NOT EXISTS place_events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, place TEXT);"
EVENTS = ("leave", "arrive")


def ensure(db):
    db.conn.executescript(SCHEMA)


def _local(t: datetime) -> datetime:
    try:
        from .clock import LOCAL
        return t.astimezone(LOCAL)
    except Exception:
        return t


def record(db, event: str, place: str = "home", now: datetime = None) -> dict:
    if event not in EVENTS:
        raise ValueError("event 只能是 leave 或 arrive")
    ensure(db)
    now = now or datetime.now(timezone.utc)
    db.conn.execute("INSERT INTO place_events (at, event, place) VALUES (?, ?, ?)",
                    (now.isoformat(timespec="seconds"), event, place[:20]))
    gained = 0
    if event == "leave":
        if not db.conn.execute("SELECT 1 FROM habits WHERE auto = 'outing'").fetchone():  # 第一赛季加过；没有就补上
            db.conn.execute("INSERT INTO habits (title, cadence, target, points, auto, active, sort) "
                            "VALUES ('每天出门', 'daily', 1, 10, 'outing', 1, 7)")
        db.conn.execute("UPDATE habits SET active = 1 WHERE auto = 'outing' AND active = 0")  # 接上定位就打开这个日常
        gained = quests.auto_check(db, "outing", _local(now).date())
    db.conn.commit()
    return {"outing": event == "leave", "gained": gained}


def has_data(db) -> bool:
    ensure(db)
    return db.conn.execute("SELECT 1 FROM place_events LIMIT 1").fetchone() is not None


def trips(db, start: date, end: date) -> list:
    """[(出门, 到家)]，本地时间；还没回来的到家是 None。"""
    ensure(db)
    rows = db.conn.execute("SELECT at, event FROM place_events WHERE at >= ? AND at < ? ORDER BY at",
                           ((start - timedelta(days=1)).isoformat(), (end + timedelta(days=2)).isoformat())).fetchall()
    out, leave = [], None
    for r in rows:
        t = _local(datetime.fromisoformat(r["at"]))
        if r["event"] == "leave":
            leave = t
        elif leave is not None:
            out.append((leave, t))
            leave = None
    if leave is not None:
        out.append((leave, None))
    return [(a, b) for a, b in out if start <= a.date() <= end]


def non_work_outings(db, start: date, end: date, training_leaves=()) -> int:
    """出去满 1 小时、又不是去训练的那几趟（训练按计划里的出发时间前后 1 小时认）。"""
    n = 0
    for a, b in trips(db, start, end):
        if b is None or b - a < timedelta(hours=1):
            continue
        if any(abs((a - t).total_seconds()) <= 3600 for t in training_leaves if t.date() == a.date()):
            continue
        n += 1
    return n
