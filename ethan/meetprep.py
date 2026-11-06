"""见面前提醒（借鉴清单第 10 条，docs/research/borrow-2026-11.md）。

她日历上有一件和人物卡里某个人有关的事（吃饭、见面、开会），小伴 提前顺口给她一段：这个人最近怎样、上次聊了什么、要记得的事。
- 认人：只认日历标题里出现的、人物卡里有的名字；名字太短（中文一个字、英文两个字母以内）、像客服商家的、
  被另一个也出现在标题里的更长名字包住的，都不认（拿不准不提）。
- 什么时候：前一晚 20:00–22:30 提明天的；没赶上前一晚（比如今天新约的），见面前 2.5 到 1 小时提。每个约只提一次（nudge_once）。
- 走现有打扰规则：键 meet:<日期>:<开始>:<人物卡编号> 登记在 ethan/nudge_registry.py，发不发由 ethan/proactive.py 照免打扰、
  刚聊过、连着没回这些关把；时间从 ethan/clock.py 取。
- 读人物卡只走登记过的入口：onto_query.person(db, "heads_up", 名字)（ethan/memusers.py 登记 heads_up），对外的一律拿不到。
- 不写她的数据：这里只记“日历上哪天和谁有约”的扫描结果（meetups 表，每次扫描整天重写），人物卡和日历都不改。
"""

import re
from datetime import date, datetime, timedelta, timezone

from . import relations

SCHEMA = """CREATE TABLE IF NOT EXISTS meetups (day TEXT NOT NULL, start TEXT NOT NULL, end TEXT, title TEXT NOT NULL,
    person_id INTEGER NOT NULL, name TEXT NOT NULL, seen_at TEXT NOT NULL, PRIMARY KEY (day, start, title, person_id));
CREATE TABLE IF NOT EXISTS meetups_meta (key TEXT PRIMARY KEY, value TEXT)"""
USER = "heads_up"  # 记忆使用者登记名（ethan/memusers.py）
PREFIX = "meet:"
SCAN_EVERY = timedelta(minutes=30)  # 日历每半小时扫一次就够（主动消息每 10 分钟跑一次）
EVENING = ("20:00", "22:30")  # 前一晚提明天的
BEFORE = (timedelta(minutes=150), timedelta(minutes=60))  # 当天：见面前 2.5 到 1 小时
MAX_PEOPLE = 3  # 一件事最多认几个人（聚会再多就不一一提了）
ASCII = re.compile(r"^[A-Za-z][A-Za-z .'-]*$")


def ensure(db):
    db.conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _names(db) -> list:
    """人物卡里能拿来认人的名字：[(编号, 名字)]。"""
    try:
        rows = db.conn.execute("SELECT id, name FROM people").fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        name = (r["name"] or "").strip()
        if not name or relations.is_service(name):
            continue
        if ASCII.match(name) and len(name) < 3:
            continue
        if not ASCII.match(name) and len(name) < 2:
            continue
        out.append((r["id"], name))
    return out


def _hits(title: str, names: list) -> list:
    """标题里认出来的人：[(编号, 名字)]。英文名按整词、不分大小写；被另一个也认出来的更长名字包住的不算。"""
    found = []
    for pid, name in names:
        if ASCII.match(name):
            if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", title, re.I):
                found.append((pid, name))
        elif name in title:
            found.append((pid, name))
    found = [(pid, n) for pid, n in found if not any(n != m and n.lower() in m.lower() for _, m in found)]
    return found[:MAX_PEOPLE]


def match(db, title: str) -> list:
    """给测试和扫描用：一个日历标题里认出的人。"""
    return _hits(title or "", _names(db))


def scan(db, calendar, today: date, now: datetime = None, force: bool = False) -> int:
    """扫今天和明天的日历，记下和人物卡里的人有关的事。半小时内扫过就不重扫。返回这次记下几条。
    日历读不到就不动上次的结果（读不到不等于没约）。"""
    if calendar is None or not relations.allowed(db, USER):
        return 0
    ensure(db)
    now = now or datetime.now(timezone.utc)
    last = db.conn.execute("SELECT value FROM meetups_meta WHERE key = 'scanned_at'").fetchone()
    if not force and last and now - datetime.fromisoformat(last[0]) < SCAN_EVERY:
        return 0
    names = _names(db)
    n = 0
    for day in (today, today + timedelta(days=1)):
        try:
            busy = calendar.busy(day)
        except Exception as e:
            print(f"[meetprep] {day} 的日历读不到：{type(e).__name__} {str(e)[:80]}", flush=True)
            continue
        db.conn.execute("DELETE FROM meetups WHERE day = ?", (day.isoformat(),))  # 她挪了、删了的约，重扫就没了
        for b in busy:
            start, end, title = b[0], b[1], b[2]
            for pid, name in _hits(title or "", names):
                n += db.conn.execute("INSERT OR IGNORE INTO meetups (day, start, end, title, person_id, name, seen_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                     (day.isoformat(), start, end, title, pid, name, _now())).rowcount
    db.conn.execute("DELETE FROM meetups WHERE day < ?", (today.isoformat(),))
    db.conn.execute("INSERT OR REPLACE INTO meetups_meta (key, value) VALUES ('scanned_at', ?)", (now.isoformat(timespec="seconds"),))
    db.conn.commit()
    return n


def upcoming(db, today: date) -> list:
    ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT * FROM meetups WHERE day IN (?, ?) ORDER BY day, start",
                                             (today.isoformat(), (today + timedelta(days=1)).isoformat()))]


def key_of(row) -> str:
    return f"{PREFIX}{row['day']}:{row['start']}:{row['person_id']}"


def brief(db, name: str, today: date) -> str:
    """人物卡里这个人的提醒要点（几行）；读不到、分不清是哪位、卡上什么都没有，返回空（不提）。"""
    from . import onto_query
    got = onto_query.person(db, USER, name)
    if got["status"] not in ("answer", "disputed") or len(got["items"]) != 1:
        return ""
    p = got["items"][0]
    if p["name"] != name:
        return ""  # 只认人物卡上一模一样的名字
    lines = []
    who = "、".join(x for x in (p.get("circle"), p.get("relation_text")) if x)
    if who:
        lines.append(f"是谁：{who}")
    if p.get("recent"):
        lines.append(f"最近怎样：{p['recent']}")
    last = p.get("latest") or {}
    if last.get("text"):
        lines.append(f"上次往来（{(last.get('day') or '')[:10]}）：{str(last['text'])[:120]}")
    elif p.get("last_contact"):
        lines.append(f"上次联系：{str(p['last_contact'])[:10]}")
    if p.get("topics"):
        lines.append("在聊的话题：" + "、".join(str(t) for t in p["topics"][:3]))
    loops = [x for x in p.get("open_loops") or [] if x.get("text")]
    if loops:
        lines.append("要记得的：" + "；".join(("对方问过她：" if x.get("kind") == "they_asked" else "") + str(x["text"])[:60] for x in loops[:3]))
    soon = []
    marks = ([{"date": p["birthday"], "what": "生日"}] if p.get("birthday") else []) + list(p.get("dates") or [])
    for m in marks:
        d = relations._next_day(str(m.get("date") or ""), today)
        if d and 0 <= (d - today).days <= 14:
            soon.append(f"{m.get('what') or '重要日子'} {d.isoformat()[5:]}")
    if soon:
        lines.append("快到的日子：" + "、".join(soon))
    return "\n".join(f"- {x}" for x in lines)


def due(db, now_local: datetime, done: dict):
    """现在该不该提一个见面前提醒：返回 (状态键, 场合说明) 或 None。"""
    today = now_local.date()
    hm = now_local.strftime("%H:%M")
    for r in upcoming(db, today):
        key = key_of(r)
        from .nudges import fresh
        if not fresh(db, key, done):
            continue
        try:
            h, m = map(int, r["start"].split(":"))
        except ValueError:
            continue
        at = now_local.replace(hour=h, minute=m, second=0, microsecond=0) + timedelta(days=(date.fromisoformat(r["day"]) - today).days)
        if r["day"] == (today + timedelta(days=1)).isoformat() and EVENING[0] <= hm < EVENING[1]:
            when = f"明天 {r['start']}"
        elif r["day"] == today.isoformat() and at - BEFORE[0] <= now_local <= at - BEFORE[1]:
            when = f"今天 {r['start']}"
        else:
            continue
        text = brief(db, r["name"], today)
        if not text:
            continue
        return key, (f"她{when}要和{r['name']}见面（日历上是「{r['title']}」）。下面是人物卡上关于这个人的（私密，只拿来提醒她，别原样念、别像播报）：\n"
                     f"{text}\n"
                     "用一两句话顺口提醒她：这个人最近怎样、你们上次聊到哪、有什么要记得的（对方问过她还没回的、快到的生日）。"
                     "卡上没有的别编；替她给对方发消息不由你发，最多帮她想一句开场。")
    return None
