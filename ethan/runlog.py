"""系统自己的输出和她的反应（ontology-plan 1.6，S23）：统一一张表，能查“系统上次做了什么、她怎么反应的”。

系统的输出本身是派生物，**不算关于她的证据**；她的反应（通过、要改、不要、自己改了、删了）是行为记录。
每晚从各处汇总一次（可以重复跑，按 key 去重）：
- 准备员做的事：她通过 / 要改（她的意见）/ 不要了；
- 小伴 写的日记初稿：她自己改过再存（source = her）；
- 邮件草稿：她发出去 / 点了不用管；
- 计划器排的事：她挪了、删了。
同一类反应 4 周里出现 3 次以上，才在 lifebase.write_behavior 里写成一条行为说法，她改一次不算长期习惯。
"""

import json
from datetime import date, datetime, timedelta, timezone

SCHEMA = """CREATE TABLE IF NOT EXISTS run_log (key TEXT PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
    title TEXT NOT NULL, reaction TEXT, reaction_at TEXT, note TEXT)"""
KINDS = {"prep": "替你准备的事", "diary": "日记初稿", "mail": "邮件草稿", "plan": "计划里排的事"}
REACTIONS = {"adopted": "通过 / 用了", "revised": "要改", "edited": "自己改了", "dropped": "不要了 / 删了", "ignored": "没理"}
MIN_REPEAT = 3


def ensure(db):
    db.conn.execute(SCHEMA)


def _put(db, key, at, actor, kind, title, reaction=None, reaction_at=None, note=None) -> int:
    old = db.conn.execute("SELECT reaction FROM run_log WHERE key = ?", (key,)).fetchone()
    if old is None:
        db.conn.execute("INSERT INTO run_log (key, at, actor, kind, title, reaction, reaction_at, note) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (key, at or "", actor, kind, (title or "")[:120], reaction, reaction_at, (note or "")[:300] or None))
        return 1
    if reaction and old[0] != reaction:
        db.conn.execute("UPDATE run_log SET reaction = ?, reaction_at = ?, note = COALESCE(?, note) WHERE key = ?",
                        (reaction, reaction_at, (note or "")[:300] or None, key))
        return 1
    return 0


def _table(db, name) -> bool:
    return bool(db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone())


def collect(db) -> int:
    """从各处汇总进 run_log。返回这次新加或变了的条数。"""
    ensure(db)
    n = 0
    if _table(db, "prep_work"):
        for r in db.conn.execute("SELECT p.task_id, p.state, p.feedback, p.updated_at, t.title FROM prep_work p JOIN tasks t ON t.id = p.task_id"):
            reaction = {"approved": "adopted", "revising": "revised"}.get(r["state"])
            n += _put(db, f"prep:{r['task_id']}", r["updated_at"], "ethan", "prep", r["title"], reaction,
                      r["updated_at"] if reaction else None, r["feedback"] if reaction == "revised" else None)
    if _table(db, "prep_trash"):
        for r in db.conn.execute("SELECT task_id, title, at FROM prep_trash WHERE task_id IS NOT NULL"):
            n += _put(db, f"prep:{r['task_id']}", r["at"], "ethan", "prep", r["title"], "dropped", r["at"])
    if _table(db, "diary"):
        for r in db.conn.execute("SELECT day, source, updated_at FROM diary"):
            n += _put(db, f"diary:{r['day']}", r["day"], "ethan", "diary", f"{r['day']} 的日记",
                      "edited" if r["source"] == "her" else None, r["updated_at"] if r["source"] == "her" else None)
    if _table(db, "mail_items"):
        cols = {c[1] for c in db.conn.execute("PRAGMA table_info(mail_items)")}
        if {"draft", "desk_state"} <= cols:
            for r in db.conn.execute("SELECT id, subject, desk_state, sent_at, created_at FROM mail_items WHERE draft IS NOT NULL AND draft != ''"):
                reaction = {"sent": "adopted", "ignored": "ignored"}.get(r["desk_state"])
                n += _put(db, f"mail:{r['id']}", r["created_at"], "ethan", "mail", f"回复：{r['subject'] or ''}", reaction, r["sent_at"])
    if _table(db, "plan_overrides"):
        for r in db.conn.execute("SELECT day, title, action, at FROM plan_overrides"):
            n += _put(db, f"plan:{r['day']}:{r['title']}", r["day"], "planner", "plan", r["title"],
                      "dropped" if r["action"] in ("delete", "drop", "remove") else "edited", r["at"], r["action"])
    db.conn.commit()
    return n


def recent(db, kind: str = None, limit: int = 20) -> list:
    """系统最近做了什么、她怎么反应的（新的在前）。"""
    ensure(db)
    sql = "SELECT * FROM run_log" + (" WHERE kind = ?" if kind else "") + " ORDER BY at DESC LIMIT ?"
    return [dict(r) for r in db.conn.execute(sql, ((kind, limit) if kind else (limit,)))]


def reaction_counts(db, day: date, weeks: int = 4) -> dict:
    """{kind: {reaction: n}}，只算最近几周有反应的。"""
    ensure(db)
    since = (day - timedelta(days=7 * weeks)).isoformat()
    out = {}
    for r in db.conn.execute("SELECT kind, reaction, COUNT(*) n FROM run_log WHERE reaction IS NOT NULL AND substr(COALESCE(reaction_at, at), 1, 10) >= ? "
                             "GROUP BY kind, reaction", (since,)):
        out.setdefault(r["kind"], {})[r["reaction"]] = r["n"]
    return out


def behavior_items(db, day: date, weeks: int = 4) -> list:
    """给 lifebase.write_behavior：同一类反应攒够 MIN_REPEAT 次才写，只描述次数。"""
    items = []
    for kind, counts in reaction_counts(db, day, weeks).items():
        total = sum(counts.values())
        if total < MIN_REPEAT:
            continue
        parts = "、".join(f"{REACTIONS[k]} {v} 次" for k, v in sorted(counts.items(), key=lambda x: -x[1]) if k in REACTIONS)
        items.append({"key": f"reaction_{kind}", "what": f"对 小伴 的{KINDS.get(kind, kind)}的反应", "value": parts, "samples": total,
                      "note": "只是次数；为什么这样另看每一次的记录"})
    return items
