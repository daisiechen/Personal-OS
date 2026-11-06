"""任务结构：领域 → 副本 → 里程碑 → 阶段任务 → 每天的待办（docs/design/quest-structure.md）。

这一层只管数据：
- 里程碑（milestones）：副本下面可检查的阶段结果，带截止日（和可选的目标值），按顺序一个个过；副本进度按过了几个算。
- 阶段任务：还是 tasks 表里 quest = 'main' 的行，多了几列——属于哪个里程碑、频次（一次性 / 每天 / 每周 N 次 / 每个里程碑一次）、
  前置条件（等哪件做完）、引用哪个日常周常（引用的不另计分）、是不是阶段复盘。
- 反复做的任务每做一次记一笔（stage_log），不把整件任务勾掉。
计划器每天从这里推出当天的待办（due_today），任务页按这里显示进度（progress）。
"""

import json
from datetime import date, datetime, timedelta, timezone

CADENCES = ("once", "daily", "weekly", "per_milestone")
CADENCE_NAME = {"once": "一次性", "daily": "每天", "weekly": "每周", "per_milestone": "每个阶段一次"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS milestones (
    id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL REFERENCES goals(id), seq REAL NOT NULL, title TEXT NOT NULL,
    due TEXT, target REAL, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'missed')),
    review TEXT, closed_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref INTEGER NOT NULL, from_state TEXT, to_state TEXT NOT NULL,
    at TEXT NOT NULL, effects TEXT NOT NULL DEFAULT '{}', undone_at TEXT);
CREATE INDEX IF NOT EXISTS transitions_ref ON transitions (kind, ref);
CREATE TABLE IF NOT EXISTS stage_log (
    task_id INTEGER NOT NULL REFERENCES tasks(id), day TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (task_id, day));
"""
TASK_COLS = (("milestone_id", "INTEGER"), ("cadence", "TEXT"), ("per_week", "INTEGER"), ("depends_on", "INTEGER"),
             ("habit_id", "INTEGER"), ("is_review", "INTEGER NOT NULL DEFAULT 0"),
             ("start_day", "TEXT"))  # 从哪天起才做（比如“11/12 前没出签就催问”）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.executescript(SCHEMA)
    have = {r["name"] for r in db.conn.execute("PRAGMA table_info(tasks)")}
    for col, ddl in TASK_COLS:
        if col not in have:
            db.conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")


# ---------- 里程碑 ----------
def add_milestone(db, goal_id: int, title: str, due: str = None, target: float = None) -> int:
    ensure(db)
    title = (title or "").strip()[:120]
    if not title:
        raise ValueError("里程碑要有名字")
    if due:
        date.fromisoformat(due)
    seq = db.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM milestones WHERE goal_id = ?", (goal_id,)).fetchone()[0]
    cur = db.conn.execute("INSERT INTO milestones (goal_id, seq, title, due, target, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (goal_id, seq, title, due, target, _now()))
    db.conn.commit()
    return cur.lastrowid


def milestones(db, goal_id: int) -> list:
    ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT * FROM milestones WHERE goal_id = ? ORDER BY seq, id", (goal_id,))]


def current(db, goal_id: int):
    """当前阶段：第一个还没关掉的里程碑。"""
    return next((m for m in milestones(db, goal_id) if m["status"] == "open"), None)


# ---------- 转换记录（docs/design/system-rules.md 第 2 节） ----------
# 每一步记下“前后状态”和“这一步连带产生了什么”（加了哪笔分、建了哪几件任务、有没有顺带结束里程碑）。
# 撤回时按这条记录原样收回，不再按名字猜（11-01 M1 重开时按名字删掉了带到 M2 的 6 件任务）。
def _record(db, kind: str, ref: int, frm, to: str, effects: dict) -> int:
    cur = db.conn.execute("INSERT INTO transitions (kind, ref, from_state, to_state, at, effects) VALUES (?, ?, ?, ?, ?, ?)",
                          (kind, ref, frm, to, _now(), json.dumps(effects, ensure_ascii=False)))
    return cur.lastrowid


def _last(db, kind: str, ref: int, to_states: tuple):
    """这样东西最近一次、还没撤回的、走到 to_states 的转换。"""
    r = db.conn.execute(f"SELECT * FROM transitions WHERE kind = ? AND ref = ? AND undone_at IS NULL AND to_state IN "
                        f"({','.join('?' * len(to_states))}) ORDER BY id DESC LIMIT 1", (kind, ref, *to_states)).fetchone()
    return {**dict(r), "effects": json.loads(r["effects"])} if r else None


def _undo(db, tid: int):
    db.conn.execute("UPDATE transitions SET undone_at = ? WHERE id = ?", (_now(), tid))


def history(db, kind: str, ref: int) -> list:
    ensure(db)
    return [{**dict(r), "effects": json.loads(r["effects"])} for r in
            db.conn.execute("SELECT * FROM transitions WHERE kind = ? AND ref = ? ORDER BY id", (kind, ref))]


def close_milestone(db, mid: int, reached: bool, review: str = "") -> None:
    """阶段结束：达成或没达成，写下复盘结论（没达成不算失败，复盘决定下一阶段怎么调）。"""
    ensure(db)
    db.conn.execute("UPDATE milestones SET status = ?, review = ?, closed_at = ? WHERE id = ?",
                    ("done" if reached else "missed", (review or "")[:2000], _now(), mid))
    db.conn.commit()


def window(db, m: dict, day: date) -> tuple:
    """这个里程碑的时间段：上一个里程碑截止的第二天（或副本开始）到它自己的截止日（没有就到 day）。"""
    prev = db.conn.execute("SELECT MAX(due) FROM milestones WHERE goal_id = ? AND seq < ? AND due IS NOT NULL",
                           (m["goal_id"], m["seq"])).fetchone()[0]
    start = date.fromisoformat(prev) + timedelta(days=1) if prev else date.fromisoformat(m["created_at"][:10])
    end = date.fromisoformat(m["due"]) if m.get("due") else day
    return start, end


def goal_progress(db, goal_id: int) -> dict:
    """副本进度按里程碑算：过了几个 / 一共几个（没达成但复盘过的也算走过）。"""
    ms = milestones(db, goal_id)
    passed = sum(1 for m in ms if m["status"] != "open")
    return {"passed": passed, "total": len(ms), "frac": round(passed / len(ms), 2) if ms else None,
            "current": next((m for m in ms if m["status"] == "open"), None)}


# ---------- 阶段任务 ----------
def add_task(db, milestone_id: int, title: str, cadence: str = "once", per_week: int = None, depends_on: int = None,
             habit_id: int = None, size: str = "S", review: bool = False) -> int:
    ensure(db)
    if cadence not in CADENCES:
        raise ValueError("频次只能是一次性、每天、每周、每个阶段一次")
    if cadence == "weekly" and not per_week:
        per_week = 1
    m = db.conn.execute("SELECT goal_id FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if m is None:
        raise ValueError("没有这个里程碑")
    seq = db.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tasks WHERE milestone_id = ?", (milestone_id,)).fetchone()[0]
    tid = db.add_task((title or "").strip()[:200], goal_id=m["goal_id"])
    db.conn.execute("UPDATE tasks SET quest = 'main', size = ?, seq = ?, milestone_id = ?, cadence = ?, per_week = ?, depends_on = ?, "
                    "habit_id = ?, is_review = ? WHERE id = ?",
                    (size, seq, milestone_id, cadence, per_week, depends_on, habit_id, 1 if review else 0, tid))
    db.conn.commit()
    return tid


def tasks_of(db, milestone_id: int) -> list:
    ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT * FROM tasks WHERE milestone_id = ? ORDER BY is_review, seq, id", (milestone_id,))]


def blocked(db, task: dict) -> bool:
    """前置条件没完成：不排进待办，小伴 也不提前做。"""
    dep = task.get("depends_on")
    if not dep:
        return False
    r = db.conn.execute("SELECT done FROM tasks WHERE id = ?", (dep,)).fetchone()
    if not r or r["done"]:
        return False
    return not db.conn.execute("SELECT 1 FROM stage_log WHERE task_id = ?", (dep,)).fetchone()  # 反复做的做过一次就算


def log(db, task_id: int, day: date) -> bool:
    """反复做的任务做了一次（一天最多记一次）。一次性的直接勾掉整件。返回是不是新记的。"""
    ensure(db)
    t = db.conn.execute("SELECT cadence FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None:
        raise ValueError("没有这件")
    if (t["cadence"] or "once") == "once":
        new = db.conn.execute("SELECT done FROM tasks WHERE id = ?", (task_id,)).fetchone()["done"] == 0
        if new:
            db.complete_task(task_id)
        return new
    cur = db.conn.execute("INSERT OR IGNORE INTO stage_log (task_id, day, at) VALUES (?, ?, ?)", (task_id, day.isoformat(), _now()))
    db.conn.commit()
    return cur.rowcount > 0


def unlog(db, task_id: int, day: date) -> bool:
    ensure(db)
    n = db.conn.execute("DELETE FROM stage_log WHERE task_id = ? AND day = ?", (task_id, day.isoformat())).rowcount
    db.conn.commit()
    return n > 0


def _count(db, task: dict, start: date, end: date) -> int:
    if task.get("habit_id"):  # 引用日常周常：看那边的打卡
        return db.conn.execute("SELECT COUNT(DISTINCT day) FROM habit_log WHERE habit_id = ? AND day >= ? AND day <= ?",
                               (task["habit_id"], start.isoformat(), end.isoformat())).fetchone()[0]
    return db.conn.execute("SELECT COUNT(*) FROM stage_log WHERE task_id = ? AND day >= ? AND day <= ?",
                           (task["id"], start.isoformat(), end.isoformat())).fetchone()[0]


def progress(db, task: dict, day: date) -> dict:
    """任务页显示的进度：{"done", "need", "text"}。"""
    cad = task.get("cadence") or "once"
    if cad == "once":
        return {"done": int(bool(task["done"])), "need": 1, "text": "✓" if task["done"] else ""}
    m = db.conn.execute("SELECT * FROM milestones WHERE id = ?", (task["milestone_id"],)).fetchone()
    start, end = window(db, dict(m), day) if m else (day, day)
    if cad == "daily":
        upto = min(day, end)
        need = max(0, (upto - start).days + 1)
        got = _count(db, task, start, upto)
        return {"done": got, "need": need, "text": f"{got} / {need} 天"}
    if cad == "weekly":  # 每周 N 次：这周做了几次；这个阶段一共几周、达标了几周（M2 两周要买两次菜）
        per = task.get("per_week") or 1
        monday = day - timedelta(days=day.weekday())
        got = _count(db, task, monday, day)
        weeks, ok, w = 0, 0, start - timedelta(days=start.weekday())
        while w <= end:
            weeks += 1
            ok += _count(db, task, max(w, start), min(w + timedelta(days=6), end)) >= per
            w += timedelta(days=7)
        return {"done": got, "need": per, "text": f"本周 {got} / {per}" + (f" · 这阶段 {ok} / {weeks} 周" if weeks > 1 else "")}
    got = _count(db, task, start, end)
    return {"done": min(got, 1), "need": 1, "text": "✓" if got else "本阶段还没做"}


def due_today(db, task: dict, day: date) -> bool:
    """今天要不要排进待办：完成了的、被前置挡着的、今天已经做过的、这周 / 这个阶段已经够了的都不排。"""
    if task.get("done") or blocked(db, task):
        return False
    if task.get("start_day") and day.isoformat() < task["start_day"]:  # 还没到开始做的那天
        return False
    cad = task.get("cadence") or "once"
    if task.get("is_review"):  # 复盘：阶段最后两天才排；没有截止日的，阶段里其他一次性任务都做完才排（今天怎么这么多 M1 复盘）
        m = db.conn.execute("SELECT due FROM milestones WHERE id = ?", (task.get("milestone_id"),)).fetchone()
        if m and m["due"]:
            return day >= date.fromisoformat(m["due"]) - timedelta(days=2)
        others = [t for t in tasks_of(db, task["milestone_id"]) if not t.get("is_review") and (t.get("cadence") or "once") == "once"]
        return bool(others) and all(t["done"] for t in others)
    if cad == "once":
        return True
    p = progress(db, task, day)
    if cad == "daily":
        return _count(db, task, day, day) == 0
    return p["done"] < p["need"] and _count(db, task, day, day) == 0


# ---------- 打卡、计分、推进（quest-structure.md 第 3、7 节） ----------
REPEAT_POINTS, PER_MILESTONE_POINTS, REACHED_POINTS, REVIEWED_POINTS = 10, 40, 300, 50


def is_stage(task: dict) -> bool:
    return bool(task.get("milestone_id"))


def points_for(task: dict) -> int:
    """引用日常周常的不另给分；每天 / 每周的每做一次 +10；每个阶段一次的 +40；一次性的按大小。"""
    from . import quests
    if task.get("habit_id"):
        return 0
    cad = task.get("cadence") or "once"
    if cad in ("daily", "weekly"):
        return REPEAT_POINTS
    if cad == "per_milestone":
        return PER_MILESTONE_POINTS
    return quests.SIZE_POINTS.get(task.get("size") or "M", 100)


def done_on(db, task: dict, day: date) -> bool:
    """这件今天算不算做了：一次性的看整件勾没勾，反复做的看今天记没记。"""
    if (task.get("cadence") or "once") == "once":
        return bool(task.get("done"))
    return _count(db, task, day, day) > 0


def check(db, task_id: int, day: date) -> int:
    """她勾了一件阶段任务：记一笔、给分；阶段复盘勾完就结束这个里程碑、进下一个。返回这次加的分。"""
    from . import quests
    ensure(db)
    t = db.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None:
        raise ValueError("没有这件")
    t = dict(t)
    if not log(db, task_id, day):
        return 0
    cad = t.get("cadence") or "once"
    ref = f"task:{task_id}" if cad == "once" else f"stage:{task_id}:{day.isoformat()}"
    gain = quests.award(db, points_for(t), f"主线：{t['title']}", day, ref) if points_for(t) else 0
    closed = None
    if t.get("is_review") and t.get("milestone_id"):  # 小伴 起草的复盘结论存成这一阶段的复盘（ethan/msreview.py）
        from . import msreview
        d = msreview.draft(db, t["milestone_id"]) or {}
        gain += advance(db, t["milestone_id"], day, d.get("summary") or "")
        closed = t["milestone_id"]
    elif t.get("milestone_id") and cad == "once":  # 没有复盘的里程碑（交给 agent 的副本）：一次性任务都做完就算达成
        ts = tasks_of(db, t["milestone_id"])
        if not any(x.get("is_review") for x in ts) and all(x["done"] for x in ts if (x.get("cadence") or "once") == "once"):
            gain += advance(db, t["milestone_id"], day)
            closed = t["milestone_id"]
    _record(db, "task", task_id, "open", "done" if cad == "once" else f"logged:{day.isoformat()}",
            {"points": [ref] if points_for(t) else [], "closed_milestone": closed})
    db.conn.commit()
    return gain


def uncheck(db, task_id: int, day: date) -> int:
    """撤销：一次性的改回没做，反复做的删掉今天这笔；收回给的分。"""
    from . import quests
    t = db.conn.execute("SELECT cadence, is_review, milestone_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if t is None:
        raise ValueError("没有这件")
    ensure(db)
    if (t["cadence"] or "once") == "once":
        last = _last(db, "task", task_id, ("done",))
        db.conn.execute("UPDATE tasks SET done = 0, done_at = NULL WHERE id = ?", (task_id,))
        back = quests._take_back(db, f"task:{task_id}")
        # 这一下当时顺带结束了里程碑（复盘，或交给 agent 的副本最后一件）：里程碑重新打开（不小心勾了减脂 M1 复盘，退不回去）
        mid = (last or {}).get("effects", {}).get("closed_milestone") or (t["milestone_id"] if t["is_review"] else None)
        if mid:
            back += reopen(db, mid)
        if last:
            _undo(db, last["id"])
        _record(db, "task", task_id, "done", "open", {"undo_of": last["id"] if last else None})
        db.conn.commit()
        return back
    if unlog(db, task_id, day):
        last = _last(db, "task", task_id, (f"logged:{day.isoformat()}",))
        if last:
            _undo(db, last["id"])
            db.conn.commit()
    return quests._take_back(db, f"stage:{task_id}:{day.isoformat()}")


def reached(db, m: dict, day: date) -> bool:
    """里程碑达成没有：有目标值的（体重）看 7 天平均；没有的看这个阶段的一次性任务是不是都做完了。"""
    if m.get("target") is not None:
        rows = db.conn.execute("SELECT AVG(weight) FROM body_measures WHERE weight IS NOT NULL AND day > ? AND day <= ?",
                               ((day - timedelta(days=7)).isoformat(), day.isoformat())).fetchone()
        return bool(rows and rows[0] is not None and rows[0] <= m["target"])
    once = [t for t in tasks_of(db, m["id"]) if (t.get("cadence") or "once") == "once" and not t.get("is_review")]
    return all(t["done"] for t in once)


def advance(db, milestone_id: int, day: date, review: str = "") -> int:
    """结束这个里程碑（达成 +300，没达成但复盘了 +50），把反复做的阶段任务带到下一个里程碑。返回加的分。"""
    from . import quests
    m = db.conn.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if m is None or m["status"] != "open":
        return 0
    m = dict(m)
    ok = reached(db, m, day)
    close_milestone(db, milestone_id, ok, review)
    gain = quests.award(db, REACHED_POINTS if ok else REVIEWED_POINTS, f"里程碑{'达成' if ok else '复盘'}：{m['title']}", day,
                        f"milestone:{milestone_id}")
    nxt = current(db, m["goal_id"])
    created = []
    if nxt:
        have = {t["title"] for t in tasks_of(db, nxt["id"])}
        from . import locks
        for t in tasks_of(db, milestone_id):
            if (t.get("cadence") or "once") != "once" and t["title"] not in have \
                    and not locks.she_deleted(db, t["title"], goal_id=m["goal_id"]):  # 她在这个副本里删掉过的，不再带过去
                created.append(add_task(db, nxt["id"], t["title"], t["cadence"], t.get("per_week"), habit_id=t.get("habit_id"),
                                        size=t.get("size") or "S"))
    _record(db, "milestone", milestone_id, "open", "done" if ok else "missed",
            {"points": [f"milestone:{milestone_id}"], "created": created, "next": nxt["id"] if nxt else None})
    db.conn.commit()
    return gain


def reopen(db, milestone_id: int) -> int:
    """撤销结束一个里程碑：改回进行中，收回里程碑的分，删掉结束时自动带到下一个里程碑、还没动过的反复任务。返回收回的分。"""
    from . import quests
    m = db.conn.execute("SELECT * FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    if m is None or m["status"] == "open":
        return 0
    last = _last(db, "milestone", milestone_id, ("done", "missed"))
    if last:  # 按当时的记录收回：只删那次带过去的、她还没动过的
        for tid in last["effects"].get("created") or []:
            t = db.conn.execute("SELECT done FROM tasks WHERE id = ?", (tid,)).fetchone()
            if t and not t["done"] and not db.conn.execute("SELECT 1 FROM stage_log WHERE task_id = ?", (tid,)).fetchone():
                db.delete_task(tid)
        _undo(db, last["id"])
        _record(db, "milestone", milestone_id, m["status"], "open", {"undo_of": last["id"]})
    nxt = db.conn.execute("SELECT id FROM milestones WHERE goal_id = ? AND seq > ? ORDER BY seq LIMIT 1", (m["goal_id"], m["seq"])).fetchone()
    if nxt and m["closed_at"] and not last:  # 没有记录的老数据：按以前的办法（名字 + 时间）猜
        repeat = {t["title"] for t in tasks_of(db, milestone_id) if (t.get("cadence") or "once") != "once"}
        for t in tasks_of(db, nxt["id"]):
            if (t["title"] in repeat and t["created_at"] >= m["closed_at"] and not t["done"]
                    and not db.conn.execute("SELECT 1 FROM stage_log WHERE task_id = ?", (t["id"],)).fetchone()):
                db.delete_task(t["id"])
    db.conn.execute("UPDATE milestones SET status = 'open', review = NULL, closed_at = NULL WHERE id = ?", (milestone_id,))
    db.conn.commit()
    return quests._take_back(db, f"milestone:{milestone_id}")


def today_ids(db, goal_id: int, day: date) -> list:
    """这个副本今天该排的阶段任务：当前里程碑里 due_today 的；引用日常周常的在日常里打卡，不另排。"""
    m = current(db, goal_id)
    if not m:
        return []
    return [t["id"] for t in tasks_of(db, m["id"]) if not t.get("habit_id") and due_today(db, t, day)]


def has_structure(db, goal_id: int) -> bool:
    ensure(db)
    return db.conn.execute("SELECT 1 FROM milestones WHERE goal_id = ?", (goal_id,)).fetchone() is not None


def view(db, goal_id: int, day: date) -> list:
    """副本详情显示用：每个里程碑和它的阶段任务（以后的里程碑不列任务）。"""
    from . import msreview, taskrules
    msreview.ensure(db)
    out, cur = [], current(db, goal_id)
    rules = {t["id"]: t for t in taskrules.rows(db, "quest_detail", day, None) if t.get("goal_id") == goal_id}  # 她说算了的不列，在等的标出来（11-06）
    titles = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM tasks WHERE goal_id = ?", (goal_id,))}
    habits = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM habits")}
    for m in milestones(db, goal_id):
        state = "past" if m["status"] != "open" else "now" if cur and m["id"] == cur["id"] else "later"
        tasks = []
        if True:  # 以后的里程碑也列出来看，只是勾的时候要再确认
            for t in tree([x for x in tasks_of(db, m["id"]) if x["id"] in rules]):
                p = progress(db, t, day)
                r = rules[t["id"]]
                tasks.append({"id": t["id"], "title": t["title"], "cadence": t.get("cadence") or "once",
                              "cadence_name": CADENCE_NAME.get(t.get("cadence") or "once", ""), "per_week": t.get("per_week"),
                              "habit": habits.get(t.get("habit_id")), "review": bool(t.get("is_review")),
                              "depends_on": t.get("depends_on"), "depends": titles.get(t.get("depends_on")),
                              "blocked": blocked(db, t), "done": bool(t["done"]), "today": done_on(db, t, day),
                              "progress": p["text"], "points": points_for(t), "depth": t["depth"], "habit_id": t.get("habit_id"),
                              "state": r["state"], "label": r["label"] if r["state"] in ("waiting", "not_started", "postponed") else ""})
        if state == "later" and cur:  # 以后的阶段：反复做的会在进入时从当前阶段带过来，先列出来让她看到全貌（M3 M4 没有引用任务了么）
            have = {t["title"] for t in tasks}
            for t in tasks_of(db, cur["id"]):
                if (t.get("cadence") or "once") != "once" and t["title"] not in have:
                    tasks.insert(len([x for x in tasks if not x["review"]]), {
                        "id": -t["id"], "title": t["title"], "cadence": t["cadence"], "cadence_name": CADENCE_NAME.get(t["cadence"], ""),
                        "per_week": t.get("per_week"), "habit": habits.get(t.get("habit_id")), "habit_id": t.get("habit_id"),
                        "review": False, "depends_on": None, "depends": None, "blocked": False, "done": False, "today": False,
                        "progress": "还没开始", "points": points_for(t), "depth": 0, "carry": True})
        out.append({"id": m["id"], "title": m["title"], "due": m["due"], "target": m["target"], "status": m["status"],
                    "review": m["review"], "state": state, "tasks": tasks,
                    "draft": json.loads(m["draft"]) if m.get("draft") and m["status"] == "open" else None})
    return out


def tree(tasks: list) -> list:
    """按前置条件排成树：要等的挂在它等的那件后面，可以一级级挂下去；复盘放最后。每件带上 depth（第几级）。"""
    ids = {t["id"] for t in tasks}
    kids = {}
    for t in tasks:
        parent = t.get("depends_on") if t.get("depends_on") in ids else None
        kids.setdefault(parent, []).append(t)
    out, seen = [], set()

    def walk(parent, depth):
        for t in sorted(kids.get(parent, []), key=lambda x: (x.get("is_review") or 0, x.get("seq") or 0, x["id"])):
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            out.append({**t, "depth": depth})
            walk(t["id"], depth + 1)
    walk(None, 0)
    out += [{**t, "depth": 0} for t in tasks if t["id"] not in seen]  # 万一有环，也别丢
    return out


def set_task(db, task_id: int, cadence: str = None, per_week: int = None, depends_on="keep", habit_id="keep") -> None:
    ensure(db)
    if habit_id != "keep":  # 改引用哪个日常周常，或者不引用、自己打卡（引用日常的我自己改不了么）
        db.conn.execute("UPDATE tasks SET habit_id = ? WHERE id = ?", (int(habit_id) if habit_id else None, task_id))
    if cadence is not None:
        if cadence not in CADENCES:
            raise ValueError("频次只能是一次性、每天、每周、每个阶段一次")
        db.conn.execute("UPDATE tasks SET cadence = ?, per_week = ? WHERE id = ?",
                        (cadence, (per_week or 1) if cadence == "weekly" else None, task_id))
    if depends_on != "keep":
        dep = int(depends_on) if depends_on else None
        while dep:  # 挂上去以后不能绕回自己
            if dep == int(task_id):
                raise ValueError("不能等自己")
            r = db.conn.execute("SELECT depends_on FROM tasks WHERE id = ?", (dep,)).fetchone()
            dep = r["depends_on"] if r else None
        db.conn.execute("UPDATE tasks SET depends_on = ? WHERE id = ?", (int(depends_on) if depends_on else None, task_id))
    db.conn.commit()


def set_milestone(db, mid: int, title: str = None, due="keep", target="keep") -> None:
    ensure(db)
    if title is not None:
        if not title.strip():
            raise ValueError("里程碑要有名字")
        db.conn.execute("UPDATE milestones SET title = ? WHERE id = ?", (title.strip()[:120], mid))
    if due != "keep":
        if due:
            date.fromisoformat(due)
        db.conn.execute("UPDATE milestones SET due = ? WHERE id = ?", (due or None, mid))
    if target != "keep":
        db.conn.execute("UPDATE milestones SET target = ? WHERE id = ?", (float(target) if target not in (None, "") else None, mid))
    db.conn.commit()
