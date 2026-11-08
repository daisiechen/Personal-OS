"""状态转换记录（docs/design/system-rules.md 第 2 节）：所有有状态的东西共用一张表。

里程碑和阶段任务在 ethan/stages.py 里记（撤回按记录收回）；这里给其余的用：副本（goal）、待办（todo）、
准备员的事（prep）、每周复盘（review）、下季草稿（season_draft）。每一步记前后状态和连带的东西，巡检按状态图查不可能的组合。
"""

import json

# 每种东西允许的状态（状态图）：from → to 都要在这里
STATES = {
    "goal": {"not_started", "active", "paused", "done", "dropped"},
    "todo": {"open", "done", "postponed", "deleted", "dropped"},  # dropped：她说算了（taskrules.drop，11-06）
    "prep": {"ask", "todo", "working", "waiting_grok", "ready", "revising", "approved", "pending"},
    "review": {"open", "done"},
    "season_draft": {"draft", "opened"},
    # 收件台三张表的 desk_state（ethan/desk.py 的 DESK_STATES；“等你处理”的判断在 desk.state_of，11-06）
    "mail_desk": {"open", "todo", "drafting", "draft", "sending", "sent", "ignored", "closed"},
    "visitor_desk": {"open", "todo", "handed", "draft", "sending", "sent", "ignored"},
    "outbox_desk": {"draft", "drafting", "sending", "sent", "ignored"},  # sending：发之前先占住（Codex 评审 11-07 第 7 条）  # ignored：收件台里她点“不发了”（desk.ignore 允许 out，11-06 巡检补）
}


def record(db, kind: str, ref, frm, to: str, effects: dict = None) -> int:
    """记一步。frm 可以是 None（新建）。不认识的状态照记，巡检会报出来。"""
    from . import stages
    stages.ensure(db)
    cur = db.conn.execute("INSERT INTO transitions (kind, ref, from_state, to_state, at, effects) VALUES (?, ?, ?, ?, ?, ?)",
                          (kind, int(ref), frm, to, stages._now(), json.dumps(effects or {}, ensure_ascii=False)))
    return cur.lastrowid


def history(db, kind: str, ref) -> list:
    from . import stages
    return stages.history(db, kind, int(ref))


def unknown_states(db) -> list:
    """记录里出现了状态图之外的状态：返回问题列表。"""
    from . import stages
    stages.ensure(db)
    out = []
    for kind, allowed in STATES.items():
        for r in db.conn.execute("SELECT id, ref, from_state, to_state FROM transitions WHERE kind = ?", (kind,)):
            for st in (r["from_state"], r["to_state"]):
                if st is not None and st not in allowed:
                    out.append(f"{kind} #{r['ref']} 出现了状态图之外的状态「{st}」（记录 #{r['id']}）")
    return out
