"""里程碑复盘由 小伴 先起草（她 2026-11-02 点头：“1-5 先做吧”，第 4 件）。

以前里程碑最后的“复盘”只是一件要勾的任务。现在复盘排进待办那天（阶段最后两天，或这阶段一次性任务都做完时），
早上排计划时 小伴 先写一份草稿：这一阶段做成了什么、差了什么（数字由代码算）、给下个阶段的 1–3 条调整建议。
草稿显示在副本详情里那一阶段的上面；她看完勾“复盘”，草稿的结论存成这一阶段的复盘，往后在已结束的里程碑里能看。
每周日的复盘也会带上还没勾的草稿。
"""

import json
from datetime import date, datetime, timedelta, timezone

from . import stages


def ensure(db):
    stages.ensure(db)
    if "draft" not in {r[1] for r in db.conn.execute("PRAGMA table_info(milestones)")}:
        db.conn.execute("ALTER TABLE milestones ADD COLUMN draft TEXT")


def draft(db, milestone_id: int):
    ensure(db)
    r = db.conn.execute("SELECT draft FROM milestones WHERE id = ?", (milestone_id,)).fetchone()
    return json.loads(r[0]) if r and r[0] else None


def to_write(db, day: date) -> list:
    """今天该起草的里程碑：进行中、复盘今天排进待办、还没有草稿。"""
    ensure(db)
    out = []
    for m in db.conn.execute("SELECT * FROM milestones WHERE status = 'open' AND draft IS NULL"):
        rev = next((t for t in stages.tasks_of(db, m["id"]) if t.get("is_review") and not t["done"]), None)
        if rev and stages.due_today(db, rev, day):
            out.append(dict(m))
    return out


def facts(db, m: dict, day: date) -> dict:
    """这一阶段的数：一次性的做完没有、反复做的达标几周、目标值和最近 7 天平均。"""
    goal = db.conn.execute("SELECT title FROM goals WHERE id = ?", (m["goal_id"],)).fetchone()
    done, missing, repeat = [], [], []
    for t in stages.tasks_of(db, m["id"]):
        if t.get("is_review"):
            continue
        if (t.get("cadence") or "once") == "once":
            (done if t["done"] else missing).append(t["title"])
        else:
            repeat.append(f"{t['title']}：{stages.progress(db, t, day)['text']}")
    out = {"副本": goal["title"] if goal else "", "里程碑": m["title"], "截止": m.get("due"), "做完的": done, "没做完的": missing,
           "反复做的": repeat}
    if m.get("target") is not None and db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'body_measures'").fetchone():
        r = db.conn.execute("SELECT AVG(weight) FROM body_measures WHERE weight IS NOT NULL AND day > ? AND day <= ?",
                            ((day - timedelta(days=7)).isoformat(), day.isoformat())).fetchone()
        out["目标"] = m["target"]
        out["最近 7 天平均"] = round(r[0], 1) if r and r[0] is not None else None
    nxt = db.conn.execute("SELECT title, due FROM milestones WHERE goal_id = ? AND seq > ? ORDER BY seq LIMIT 1",
                          (m["goal_id"], m["seq"])).fetchone()
    out["下个阶段"] = f"{nxt['title']}（{(nxt['due'] or '')[5:]}）" if nxt else "没有了，这是最后一个"
    return out


def fallback(f: dict) -> dict:
    n, miss = len(f["做完的"]), f["没做完的"]
    line = f"这一阶段做完 {n} 件" + (f"，还差「{'」「'.join(miss[:3])}」" if miss else "，一次性的都做完了")
    if f.get("目标") is not None:
        line += f"；目标 {f['目标']}，最近 7 天平均 {f.get('最近 7 天平均') if f.get('最近 7 天平均') is not None else '没有数据'}"
    return {"summary": line + "。", "suggest": [], "by": ""}


def write(db, router, m: dict, day: date) -> dict:
    """写一份草稿存起来；模型不可用就按数字写一句。"""
    f = facts(db, m, day)
    body = fallback(f)
    if router is not None:
        system = ("你是 小伴，在替她起草一个里程碑的阶段复盘。只用下面的事实，不编。只回一个 JSON："
                  '{"summary": "两三句：这一阶段做成了什么、差在哪（带数字），口吻是你本人，温柔但直说", '
                  '"suggest": ["给下个阶段的调整建议，最多 3 条，每条一句、具体到能改的（频次、截止日、加减哪件事）"]}'
                  "\n\n### 事实\n" + json.dumps(f, ensure_ascii=False, indent=1))
        try:
            reply = router.complete(system, [{"role": "user", "content": "（起草这一阶段的复盘）"}])
            text = reply.text.strip()
            data = json.loads(text[text.find("{"): text.rfind("}") + 1])
            if str(data.get("summary") or "").strip():
                body = {"summary": str(data["summary"]).strip()[:300],
                        "suggest": [str(x).strip()[:120] for x in (data.get("suggest") or []) if str(x).strip()][:3], "by": "小伴"}
        except Exception as e:
            print(f"[里程碑复盘] 草稿没写成，按数字写：{type(e).__name__} {str(e)[:80]}", flush=True)
    body["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ensure(db)
    db.conn.execute("UPDATE milestones SET draft = ? WHERE id = ?", (json.dumps(body, ensure_ascii=False), m["id"]))
    db.conn.commit()
    return body


def write_due(db, router, day: date) -> list:
    """早上排完计划后跑：今天该起草的都写好。返回写了哪几个里程碑。"""
    return [write(db, router, m, day) and m["title"] for m in to_write(db, day)]


def waiting(db) -> list:
    """有草稿、复盘还没勾的里程碑（每周复盘带上）。"""
    ensure(db)
    out = []
    for m in db.conn.execute("SELECT m.*, g.title AS goal FROM milestones m JOIN goals g ON g.id = m.goal_id "
                             "WHERE m.status = 'open' AND m.draft IS NOT NULL"):
        d = json.loads(m["draft"])
        out.append(f"{m['goal'].split('：')[0]} · {m['title']}：{d.get('summary')}" + (f"（建议：{'；'.join(d['suggest'])}）" if d.get("suggest") else ""))
    return out
