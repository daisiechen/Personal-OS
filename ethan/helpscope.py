"""小伴 帮办按新结构挑（quest-structure.md 第 6 节；她 2026-11-02：“1-5 先做吧”，第 2 件）。

以前 小伴 替她准备的事，来自他在聊天里“答应”的和每天回头看时从聊天里补的，常常和她这周真要做的对不上。现在每天一次：
- 范围：未来 7 天的待办和当前里程碑的阶段任务里，她本人要做、没有前置挡着、还没开始的；
  给开发会话 / Claude / Codex 的权限、放行、配置这类系统运维事务不挑（prep.is_ops，她 11-05）；
- 由 小伴 判断哪几件他能提前替她做一部分：起草、查资料、比价、准备材料、订位前的准备；要她本人出面的不挑；
- 挑中的（一天最多 3 件、同时最多 6 件在准备）记成他的准备任务，挂在同一个副本下、截止跟着那件走，并记下是替哪件准备的；
  进准备队列时先问她要不要（ethan/prep.py 原来的规矩：新记下的先问，两件隔 2 小时）。
- 花钱、预订、替她发消息照旧只准备到等她确认为止。
"""

import json
from datetime import date, timedelta

from . import stages

PER_DAY, MAX_OPEN = 3, 6
PROMPT = """你是 Mia 的 AI 伙伴 小伴。下面是她未来 7 天要做的事（她本人的待办和阶段任务）。
挑出你能提前替她做一部分的：起草（邮件、消息、文案、提纲）、查资料、比价、整理清单、准备材料、订位前把选项和规则查清。
不挑：要她本人出面或亲手做的（训练、上课、见人、签字、她自己的判断）、你做不了实质部分的、已经很小不用帮的。
花钱、预订、替她发消息的，只能准备到等她确认为止，照样可以挑，在 what 里写清准备到哪一步。
宁缺毋滥，最多挑 {n} 件，没有合适的就返回空列表。只回一个 JSON：
{{"help": [{{"id": 待办编号, "what": "你替她准备什么，一句话，20 字以内，比如“起草给内测用户的邀请消息”"}}]}}"""


def ensure(db):
    stages.ensure(db)
    if "help_for" not in {r[1] for r in db.conn.execute("PRAGMA table_info(tasks)")}:
        db.conn.execute("ALTER TABLE tasks ADD COLUMN help_for INTEGER")


def candidates(db, day: date) -> list:
    """未来 7 天她要做的：截止在 7 天内的待办，加上当前里程碑里一次性的阶段任务；去掉做完的、被挡着的、还没到开始日的、已经在帮的。"""
    ensure(db)
    end = (day + timedelta(days=7)).isoformat()
    current = {r[0] for r in db.conn.execute(
        "SELECT m.id FROM milestones m WHERE m.status = 'open' AND m.seq = (SELECT MIN(seq) FROM milestones x "
        "WHERE x.goal_id = m.goal_id AND x.status = 'open')")}
    helped = {r[0] for r in db.conn.execute("SELECT help_for FROM tasks WHERE help_for IS NOT NULL")}
    out = []
    for r in db.conn.execute("SELECT t.*, g.title AS goal, m.title AS ms FROM tasks t LEFT JOIN goals g ON g.id = t.goal_id "
                             "LEFT JOIN milestones m ON m.id = t.milestone_id WHERE t.done = 0 AND t.promised_by_ethan = 0 "
                             "AND COALESCE(g.status, 'active') = 'active'"):
        t = dict(r)
        from . import prep
        if t["id"] in helped or t.get("habit_id") or t.get("is_review") or stages.blocked(db, t) or prep.is_ops(t["title"]):  # 系统运维的不帮（11-05）
            continue
        if t.get("start_day") and t["start_day"] > end:
            continue
        soon = t.get("due") and t["due"] <= end
        stage_now = t.get("milestone_id") in current and (t.get("cadence") or "once") == "once"
        if soon or stage_now:
            out.append(t)
    return out


def listing(items: list) -> str:
    def where(t):
        bits = [t["goal"].split("：")[0] if t.get("goal") else "", t.get("ms") or "", f"截止 {t['due']}" if t.get("due") else ""]
        return "（" + "，".join(b for b in bits if b) + "）" if any(bits) else ""
    return "\n".join(f"#{t['id']} {t['title']}{where(t)}" for t in items)


def pick(db, router, day: date) -> list:
    """每天一次：挑几件记成 小伴 的准备任务。返回新建的任务编号。"""
    ensure(db)
    db.conn.execute("CREATE TABLE IF NOT EXISTS prep_meta (key TEXT PRIMARY KEY, value TEXT)")
    key = f"help:{day.isoformat()}"
    if db.conn.execute("SELECT 1 FROM prep_meta WHERE key = ?", (key,)).fetchone():
        return []
    open_n = db.conn.execute("SELECT COUNT(*) FROM tasks WHERE help_for IS NOT NULL AND done = 0").fetchone()[0]
    room = min(PER_DAY, MAX_OPEN - open_n)
    items = candidates(db, day)
    added = []
    if room > 0 and items:
        reply = router.complete(PROMPT.format(n=room), [{"role": "user", "content": listing(items)}], purpose="prep-help")
        text = reply.text
        data = json.loads(text[text.find("{"): text.rfind("}") + 1])
        by_id = {t["id"]: t for t in items}
        for h in (data.get("help") or [])[:room]:
            tid = int(h["id"]) if str(h.get("id", "")).isdigit() else None
            from . import naming
            what = naming.title(h.get("what") or "")  # 标题式（docs/design/ui-spec.md 第 5 节）
            from . import prep
            if tid not in by_id or not what or prep.is_ops(what):
                continue
            t = by_id[tid]
            from . import locks
            from . import onto_query
            gone = onto_query.she_removed(db, "prep", what) if onto_query.reading_new(db, "prep") else locks.she_deleted(db, what)
            if locks.she_deleted(db, what, "prep") or gone:  # 她不要过的同一件，不再挑回来
                continue
            new = db.add_task(what, due=t.get("due"), goal_id=t.get("goal_id"), promised_by_ethan=True)
            db.conn.execute("UPDATE tasks SET help_for = ? WHERE id = ?", (tid, new))
            added.append(new)
    db.conn.execute("INSERT OR REPLACE INTO prep_meta (key, value) VALUES (?, ?)", (key, json.dumps(added)))
    db.conn.commit()
    return added


def for_task(db, task_id: int):
    """这件 小伴 准备的事是替她哪件做的：标题，没有返回 None。"""
    ensure(db)
    r = db.conn.execute("SELECT o.title FROM tasks t JOIN tasks o ON o.id = t.help_for WHERE t.id = ?", (task_id,)).fetchone()
    return r[0] if r else None
