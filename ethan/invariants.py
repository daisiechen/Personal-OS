"""永远要成立的检查（docs/design/system-rules.md 第 7 节，她 2026-11-02 认可）。

测试里的“整天回放”每一步后跑一遍；以后巡检也跑（python3 -m ethan.invariants --data <数据目录>）。
每个检查返回问题列表（空 = 没问题），只读，不改数据。
"""

import argparse
import json
from datetime import date
from pathlib import Path


def today_consistent(wb, day) -> list:
    """今日任务在首页和“日历 · 改计划”里是同一份（ontology 7.6 节唯一来源）。"""
    from datetime import datetime
    now = datetime.combine(day, datetime.min.time()).replace(hour=12)
    st = wb.state(now)
    home = {t["id"] for t in st["today"] if not t["done"]}
    panel = {t["id"] for t in (st.get("planner") or {}).get("todos", []) if t.get("star")}
    missing = home - panel
    return [f"今日任务 #{i} 在首页有、计划面板里没标今天" for i in sorted(missing)]


def badges_have_targets(wb, day) -> list:
    """每个红点点进去都有东西，数字等于去处的条数（ontology 6.4 节）。"""
    from . import mepage, prep, territory
    out = []
    b = wb.badges(day)
    if "map" in b:
        waste = sum(1 for t in territory.open_tasks(wb.db) if not territory.task_territory(t))
        if waste != b["map"]:
            out.append(f"赛季红点 {b['map']}，荒原里有 {waste} 件")
    if "main" in b:
        ready = sum(1 for x in prep.queue(wb.db) if x["state"] == "ready" and not x["secret"])
        if ready != b["main"]:
            out.append(f"首页准备红点 {b['main']}，等她看的有 {ready} 件")
    if "me" in b:
        n = mepage.pending_count(wb.db) + b.get("brain", 0)
        if n != b["me"]:
            out.append(f"“我”红点 {b['me']}，待你定 + 资料里要看的共 {n} 件")
    # 2026-11-04 统一巡检补上：收藏和资料、邮件和留言的红点，和点进去的列表对数；每个红点都在打扰登记表里
    from . import brain, desk, nudge_registry
    asks = brain.counts(wb.db, "ask")["tabs"].get("ask", 0)  # “我”页收藏和资料里“要你看”那一栏
    if b.get("brain", 0) != asks:
        out.append(f"收藏和资料红点 {b.get('brain', 0)}，“要你看”那一栏有 {asks} 条")
    waiting = sum(1 for x in desk.items(wb.db) if x["state"] in ("open", "draft"))  # 首页“邮件和留言”里等她处理的（“知道一下”不算）
    if b.get("desk", 0) != waiting:
        out.append(f"邮件和留言红点 {b.get('desk', 0)}，等你处理的有 {waiting} 条")
    out += [f"红点「{k}」没在打扰登记表里，不知道点进去是哪" for k in b if k not in nudge_registry.BADGES]
    return out


def ids_not_reused(db) -> list:
    """待办编号只往上走：表定义带 AUTOINCREMENT（system-rules 第 2 节）。"""
    sql = db.conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'").fetchone()[0]
    return [] if "AUTOINCREMENT" in sql.upper() else ["待办表还没改成编号不重用"]


def locks_respected(db, day) -> list:
    """她改过名字的待办，现在的名字还是她改的；她删掉的，没被自动流程建回来（system-rules 第 1 节）。"""
    from . import locks
    locks.ensure(db)
    out = []
    for r in db.conn.execute("SELECT obj, value FROM manual_locks WHERE field = 'title' AND released_at IS NULL AND obj LIKE 'task:%'"):
        want = (json.loads(r["value"]) or {}).get("title") if r["value"] else None
        tid = int(r["obj"].split(":")[1])
        row = db.conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()
        if want and row and row["title"] != want:
            out.append(f"#{tid} 她改成「{want}」，现在是「{row['title']}」")
    for r in db.conn.execute("SELECT obj, title FROM manual_locks WHERE field = 'title' AND released_at IS NULL AND obj LIKE 'task:%' "
                             "AND title IS NOT NULL"):
        dup = db.conn.execute("SELECT id FROM tasks WHERE done = 0 AND title = ? AND id != ?", (r["title"], int(r["obj"].split(":")[1]))).fetchone()
        if dup:
            out.append(f"#{dup['id']} 用的是她改掉的旧名字「{r['title']}」，另建了一件")
    gone = locks.deleted_titles(db)
    for t in db.conn.execute("SELECT id, title FROM tasks WHERE done = 0 AND promised_by_ethan = 0"):
        if t["title"] in gone:
            out.append(f"#{t['id']}「{t['title']}」她删过，又出现了")
    return out


def states_valid(db) -> list:
    """不可能的状态组合（system-rules 第 2 节）：结束了的里程碑里复盘还没做、也没有结束记录。"""
    from . import stages
    stages.ensure(db)
    out = []
    for m in db.conn.execute("SELECT * FROM milestones WHERE status IN ('done', 'missed')"):
        review = db.conn.execute("SELECT done FROM tasks WHERE milestone_id = ? AND is_review = 1", (m["id"],)).fetchone()
        logged = db.conn.execute("SELECT 1 FROM transitions WHERE kind = 'milestone' AND ref = ? AND undone_at IS NULL "
                                 "AND to_state IN ('done', 'missed')", (m["id"],)).fetchone()
        if review is not None and not review["done"] and logged:
            out.append(f"里程碑 #{m['id']}「{m['title']}」结束了，复盘却没勾")
    from . import transitions
    out += transitions.unknown_states(db)
    out += [f"待办 #{r['id']} 标着做完却没有完成时间" for r in db.conn.execute("SELECT id FROM tasks WHERE done = 1 AND done_at IS NULL")]
    for table, col, allowed in (("reviews", "status", transitions.STATES["review"]), ("season_drafts", "status", transitions.STATES["season_draft"]),
                                ("prep_work", "state", transitions.STATES["prep"])):
        if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (table,)).fetchone():
            for r in db.conn.execute(f"SELECT {col} AS st, COUNT(*) n FROM {table} GROUP BY {col}"):
                if r["st"] not in allowed:
                    out.append(f"{table} 里有 {r['n']} 条状态是「{r['st']}」，不在状态图里")
    return out


def inference_not_fact(db) -> list:
    """推断和转述不能写成关于她的事实，私密类不能标成公开（ontology.md 第 3、6.1 节，system-rules.md 第 7 节第 8 条）。
    记忆表只有 lifebase 能直接读（ethan/memusers.py 的规矩），这里调它的检查。"""
    from . import lifebase
    return lifebase.integrity_problems(db)


def run_all(wb, day) -> dict:
    return {"今日任务一致": today_consistent(wb, day), "红点有去处": badges_have_targets(wb, day),
            "编号不重用": ids_not_reused(wb.db), "她改过的没被改回": locks_respected(wb.db, day), "状态合法": states_valid(wb.db),
            "推断没写成事实": inference_not_fact(wb.db)}


def main():
    from .clock import her_today
    from .core.router import Settings
    from .memory import MemoryStore
    from .workbench.api import Workbench
    p = argparse.ArgumentParser(description="跑一遍永远要成立的检查（只读）")
    p.add_argument("--data", type=Path, required=True)
    a = p.parse_args()
    store = MemoryStore(a.data)
    try:
        wb = Workbench(store, Settings(a.data / "settings.json"))
        got = run_all(wb, her_today())
        bad = {k: v for k, v in got.items() if v}
        print(json.dumps(bad or {"ok": True}, ensure_ascii=False, indent=1))
    finally:
        store.close()


if __name__ == "__main__":
    main()
