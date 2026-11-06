"""“她改过”的锁（docs/design/system-rules.md 第 1 节，、先试行）。

她在工作台上亲手改过的东西（日程时间、待办名字、截止、星标、删除、日记正文、副本和里程碑的设定……）记一把锁：
所有自动流程（计划器排计划和重排、日记补写、准备员每天回头看、小伴 的标记、里程碑推进带任务）写数据前先查锁，锁住的不动。
- 删除也是锁：她删掉的，自动流程不能再建一个同样的回来（按名字比，换了说法的用 core.agent.find_similar 兜底）。
- 解开：她自己改回去、或者明说“按系统的来”（release）；跟着某一天的（scope=day，比如当天日程的改动）到那天结束自动失效；长期的一直留着。
- 空内容不算她写的：她清空保存等于交还系统（日记就是这样，见 ethan/diary.py）。
"""

import json
from datetime import date, datetime, timezone

from . import tx

SCHEMA = """CREATE TABLE IF NOT EXISTS manual_locks (
    id INTEGER PRIMARY KEY, obj TEXT NOT NULL, field TEXT NOT NULL, value TEXT, scope TEXT NOT NULL DEFAULT 'long',
    day TEXT, title TEXT, note TEXT, at TEXT NOT NULL, released_at TEXT);
CREATE INDEX IF NOT EXISTS manual_locks_obj ON manual_locks (obj, field);"""

# 工作台上她的操作 → 锁哪样东西的哪个字段（act 成功后由工作台统一记）
ACTION_LOCKS = {
    "task.rename": ("task", "title", "long"), "task.postpone": ("task", "due", "long"), "task.star": ("task", "star", "long"),
    "task.domain": ("task", "domain", "long"), "task.delete": ("task", "deleted", "long"), "task.move": ("task", "goal", "long"),
    "task.milestone": ("task", "milestone", "long"), "goal.set": ("goal", "settings", "long"),
    "milestone.set": ("milestone", "settings", "long"), "stage.set": ("task", "cadence", "long"),
    "event.time": ("event", "time", "day"), "event.delete": ("event", "deleted", "day"), "event.add": ("event", "added", "day"),
    "today.order": ("today", "order", "day"), "diary.save": ("diary", "body", "long"),
    "prep.pending": ("prep", "state", "long"), "prep.drop": ("prep", "deleted", "long"), "prep.star": ("prep", "star", "long"),
    "prep.order": ("prep", "order", "long"),
}


def ensure(db):
    if tx.once(db, "locks"):
        db.conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def lock(db, kind: str, ref, field: str, value=None, scope: str = "long", day: date = None, title: str = None, note: str = None) -> int:
    """记一把锁；同一样东西同一个字段再改，旧锁放开、记新的。"""
    ensure(db)
    obj = f"{kind}:{ref}"
    db.conn.execute("UPDATE manual_locks SET released_at = ? WHERE obj = ? AND field = ? AND released_at IS NULL", (_now(), obj, field))
    cur = db.conn.execute("INSERT INTO manual_locks (obj, field, value, scope, day, title, note, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                          (obj, field, json.dumps(value, ensure_ascii=False) if value is not None else None, scope,
                           day.isoformat() if day else None, title, note, _now()))
    tx.commit(db)
    return cur.lastrowid


def _alive(r, day: date = None) -> bool:
    if r["released_at"]:
        return False
    return r["scope"] != "day" or (day is not None and r["day"] == day.isoformat())


def locked(db, kind: str, ref, field: str, day: date = None) -> bool:
    """这个字段她改过、还算数吗。day：按天的锁要传今天。"""
    ensure(db)
    rows = db.conn.execute("SELECT * FROM manual_locks WHERE obj = ? AND field = ? AND released_at IS NULL",
                           (f"{kind}:{ref}", field)).fetchall()
    return any(_alive(r, day) for r in rows)


def release(db, kind: str, ref, field: str = None) -> int:
    """她说“按系统的来”：放开这样东西（某个字段或全部）的锁。返回放开了几把。"""
    ensure(db)
    q, args = "UPDATE manual_locks SET released_at = ? WHERE obj = ? AND released_at IS NULL", [_now(), f"{kind}:{ref}"]
    if field:
        q += " AND field = ?"
        args.append(field)
    n = db.conn.execute(q, args).rowcount
    tx.commit(db)
    return n


def release_event(db, day: date, title: str) -> int:
    """她说某个日程“按系统的来”：放开这天这条的锁，也清掉计划器记的“她改过”（ethan/planner.py 的 plan_overrides），下次重排照系统的排。"""
    n = release(db, "event", f"{day.isoformat()}:{title}")
    if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'plan_overrides'").fetchone():
        n += db.conn.execute("DELETE FROM plan_overrides WHERE day = ? AND title = ?", (day.isoformat(), title)).rowcount
        tx.commit(db)
    return n


def release_deleted(db, title: str, kind: str = "task") -> int:
    """她把删掉的恢复了：按名字放开删除的锁。"""
    ensure(db)
    n = db.conn.execute("UPDATE manual_locks SET released_at = ? WHERE obj LIKE ? AND field = 'deleted' AND title = ? "
                        "AND released_at IS NULL", (_now(), f"{kind}:%", title)).rowcount
    tx.commit(db)
    return n


def renamed_from(db, title: str):
    """她把哪件待办从这个旧名字改掉了：返回那件的编号（自动流程用旧名字提到它时挂回去，不另建一件）。"""
    ensure(db)
    rows = db.conn.execute("SELECT obj, title FROM manual_locks WHERE obj LIKE 'task:%' AND field = 'title' AND released_at IS NULL "
                           "AND title IS NOT NULL ORDER BY id DESC").fetchall()
    if not rows or not title:
        return None
    from .core.agent import find_similar
    for r in rows:
        if r["title"] == title or find_similar(title, [r["title"]]):
            tid = int(r["obj"].split(":")[1])
            if db.conn.execute("SELECT 1 FROM tasks WHERE id = ? AND done = 0", (tid,)).fetchone():
                return tid
    return None


def deleted_titles(db, kind: str = "task", goal_id=None) -> list:
    """她删掉的（名字）：自动流程不能再建同样的回来。goal_id：只看这个副本下删的（里程碑推进带任务用）。"""
    ensure(db)
    rows = db.conn.execute("SELECT title, note FROM manual_locks WHERE obj LIKE ? AND field = 'deleted' AND released_at IS NULL "
                           "AND title IS NOT NULL", (f"{kind}:%",)).fetchall()
    if goal_id is None:
        return [r["title"] for r in rows]
    return [r["title"] for r in rows if r["note"] == f"goal:{goal_id}"]


def she_deleted(db, title: str, kind: str = "task", goal_id=None) -> bool:
    """这件是不是她删掉过的同一件（同名，或换了说法的同一件事）。"""
    gone = deleted_titles(db, kind, goal_id)
    if not gone or not title:
        return False
    if title in gone:
        return True
    from .core.agent import find_similar
    return find_similar(title, gone) is not None


def record_action(db, action: str, p: dict, day: date, before: dict = None):
    """工作台上她的一次操作成功后调用：按 ACTION_LOCKS 记锁。before：操作前那条记录（删除时拿名字和副本）。"""
    rule = ACTION_LOCKS.get(action)
    if not rule:
        return None
    kind, field, scope = rule
    ref = p.get("id") or p.get("day") or p.get("title") or "-"
    if kind == "event":
        ref = f"{day.isoformat()}:{(before or {}).get('title') or p.get('title') or p.get('id')}"
    if kind == "today":
        ref = day.isoformat()
    if kind == "diary":
        if not str(p.get("text") or p.get("body") or "").strip():  # 清空保存 = 交还系统
            return release(db, "diary", ref)
    if action == "task.domain" and p.get("undo") and not p.get("prev"):  # 撤销了一次归类、原来没归过：交还系统（小伴 猜 / 跟着副本）
        return release(db, kind, ref, field)
    if action == "task.star" and not p.get("on"):
        field = "star"
    title = (before or {}).get("title")
    note = f"goal:{before['goal_id']}" if before and before.get("goal_id") else None
    return lock(db, kind, ref, field, {k: v for k, v in p.items() if k not in ("id",)} or None, scope, day if scope == "day" else None,
                title, note)
