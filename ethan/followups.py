"""等着办：“等……之后再……”这类事统一登记，程序每天看一次条件，到了就做（“剩下的事情还是一样的啊，等....之后再...，
这类的事情应该有个统一的处理法？”；“不用10分钟，这种一般都是长期的，每天看一次就行了吧？”；“只有等着我办的才需要进去吧”）。

两种：
- 等她办的（for_her = 1）：条件到了，变成她的待办（能挂到副本里程碑上，进副本）或“待你定”里的一条。还没到的在“待你定”最下面“以后再办”里列着。
- 系统自己在等的（for_her = 0，比如核对 Apple Pay 记账）：不进她的任何清单。到了在系统体检里列“到了，该做了”，负责的会话 / agent 做完用 --done 收掉；
  等太久（默认 14 天）条件还没到，也在体检里亮出来。
条件只能从写死的几种里选（程序能判断，不靠谁记得）：
  date：到了这一天；task_done：某件待办做完了；check：登记过的数据检查（CHECKS，比如“有了第一笔 Apple Pay 记账”）。
所有会话和 agent 遇到大概这个意思的事（不用是原话），都用命令行登记，不只写在聊天和交接文档里（AGENTS.md 有这条规矩）：
  python3 -m ethan.followups --data <数据目录> add --what "核对 Apple Pay 去重" --wait check:applepay_seen --owner "Mia OS 开发会话"
  python3 -m ethan.followups --data <数据目录> add --her --what "给赖总发 BP" --wait task_done:123 --then task --milestone 7
  python3 -m ethan.followups --data <数据目录> list | done <编号> | drop <编号>
"""

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS followups (id INTEGER PRIMARY KEY, what TEXT NOT NULL, for_her INTEGER NOT NULL DEFAULT 0,
    wait_kind TEXT NOT NULL, wait_arg TEXT NOT NULL, then_kind TEXT NOT NULL DEFAULT 'note', milestone_id INTEGER, owner TEXT, source TEXT,
    created_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', due_at TEXT, done_at TEXT, result TEXT, stale_days INTEGER NOT NULL DEFAULT 14)"""
WAITS = ("date", "task_done", "check")
THENS = ("task", "ask", "note")  # 她的待办 / 待你定 / 系统的：体检里提示负责的人
# 能用的数据检查：名字 → (说明, SQL 返回一个数, 到多少算到了)。要新的就在这里加一行
CHECKS = {
    "applepay_seen": ("有了第一笔 Apple Pay 记账", "SELECT COUNT(*) FROM transactions WHERE source = 'applepay'", 1),
    "people_with_city": ("人物卡里至少 5 个人填了常住城市", "SELECT COUNT(*) FROM people WHERE COALESCE(city, '') != ''", 5),
    # 她回国了：她最近说的所在城市（whereabouts 的 city_log，[在 城市名]）是国内城市
    "back_in_china": ("你回国", "SELECT COUNT(*) FROM (SELECT city FROM city_log ORDER BY at DESC, id DESC LIMIT 1) WHERE city IN "
                      "('深圳', '北京', '上海', '广州', '杭州', '珠海', '成都', '武汉', '南京', '苏州', '西安', '重庆', '天津', '长沙', '厦门', "
                      "'青岛', '济南', '惠民', '澳门', '中国', '国内')", 1),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.execute(SCHEMA)


def add(db, what: str, wait: str, for_her: bool = False, then: str = None, milestone_id: int = None, owner: str = "", source: str = "",
        stale_days: int = 14) -> int:
    """wait 写成“种类:参数”（date:2026-11-20 / task_done:123 / check:applepay_seen）。同一件还在等的不重复登记。"""
    ensure(db)
    kind, _, arg = (wait or "").partition(":")
    if kind not in WAITS or not arg:
        raise ValueError(f"等什么写成 {' / '.join(w + ':…' for w in WAITS)}")
    if kind == "date":
        date.fromisoformat(arg)
    if kind == "task_done" and not arg.isdigit():
        raise ValueError("task_done 后面是待办编号")
    if kind == "check" and arg not in CHECKS:
        raise ValueError(f"没登记过的检查：{arg}（能用的：{'、'.join(CHECKS)}）")
    then = then or ("task" if for_her else "note")
    if then not in THENS or (then != "note") != bool(for_her):
        raise ValueError("她办的“然后”是 task 或 ask；系统自己的是 note")
    what = " ".join((what or "").split())[:200]
    if not what:
        raise ValueError("写一句要办什么")
    old = db.conn.execute("SELECT id FROM followups WHERE status = 'waiting' AND what = ?", (what,)).fetchone()
    if old:
        return old["id"]
    cur = db.conn.execute("INSERT INTO followups (what, for_her, wait_kind, wait_arg, then_kind, milestone_id, owner, source, created_at, stale_days) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (what, int(bool(for_her)), kind, arg, then, milestone_id, owner or "", source or "", _now(), stale_days))
    db.conn.commit()
    return cur.lastrowid


def met(db, row, today: date) -> bool:
    kind, arg = row["wait_kind"], row["wait_arg"]
    if kind == "date":
        return today >= date.fromisoformat(arg)
    if kind == "task_done":
        r = db.conn.execute("SELECT done FROM tasks WHERE id = ?", (int(arg),)).fetchone()
        return bool(r and r["done"])
    if kind == "check":
        _, sql, need = CHECKS[arg]
        try:
            return (db.conn.execute(sql).fetchone()[0] or 0) >= need
        except Exception:  # 表还没有：没到
            return False
    return False


def wait_text(row) -> str:
    kind, arg = row["wait_kind"], row["wait_arg"]
    return {"date": f"到 {arg}", "task_done": f"待办 #{arg} 做完", "check": CHECKS.get(arg, (arg,))[0]}.get(kind, arg)


def daily(db, today: date) -> dict:
    """每天看一次（每晚整理里跑）：到了的，她的变成待办 / 待你定；系统的标成“到了”等负责的人收掉。"""
    ensure(db)
    got = {"triggered": 0, "her": 0}
    for r in db.conn.execute("SELECT * FROM followups WHERE status = 'waiting' ORDER BY id").fetchall():
        if not met(db, r, today):
            continue
        result = ""
        if r["then_kind"] == "task":
            if r["milestone_id"]:
                from . import stages
                tid = stages.add_task(db, r["milestone_id"], r["what"])
            else:
                from . import taskrules
                tid = taskrules.add(db, r["what"])
            result = f"task:{tid}"
        elif r["then_kind"] == "ask":
            from . import mepage
            result = f"pending:{mepage.add_question(db, r['what'])}"
        status = "done" if r["for_her"] else "due"  # 她的变成待办就算办完了；系统的等负责的人收掉
        db.conn.execute("UPDATE followups SET status = ?, due_at = ?, done_at = ?, result = ? WHERE id = ?",
                        (status, _now(), _now() if status == "done" else None, result, r["id"]))
        got["triggered"] += 1
        got["her"] += r["for_her"]
    db.conn.commit()
    return got


def problems(db, today: date) -> list:
    """系统体检用：系统的到了没人收、等太久还没到的。[{what, detail}]"""
    try:
        rows = db.conn.execute("SELECT * FROM followups WHERE status IN ('waiting', 'due')").fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        if r["status"] == "due":
            out.append({"what": "等着办：到了", "detail": f"#{r['id']} {r['what']}（{wait_text(r)}已经到了，负责：{r['owner'] or '没写'}；做完用 --done 收掉）"})
        elif (today - date.fromisoformat(r["created_at"][:10])).days > r["stale_days"]:
            out.append({"what": "等着办：等太久", "detail": f"#{r['id']} {r['what']}（等{wait_text(r)}，已经 {(today - date.fromisoformat(r['created_at'][:10])).days} 天，还等不等？）"})
    return out


def later(db) -> list:
    """“待你定”最下面“以后再办”：等她办、条件还没到的。"""
    try:
        rows = db.conn.execute("SELECT * FROM followups WHERE status = 'waiting' AND for_her = 1 ORDER BY id").fetchall()
    except Exception:
        return []
    return [{"id": r["id"], "what": r["what"], "wait": wait_text(r), "milestone_id": r["milestone_id"]} for r in rows]


def close(db, fid: int, status: str = "done") -> bool:
    ensure(db)
    n = db.conn.execute("UPDATE followups SET status = ?, done_at = ? WHERE id = ? AND status IN ('waiting', 'due')", (status, _now(), int(fid))).rowcount
    db.conn.commit()
    return bool(n)


def main():
    p = argparse.ArgumentParser(description="等着办：登记“等……之后再……”的事，每天看一次条件")
    p.add_argument("--data", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--what", required=True)
    a.add_argument("--wait", required=True, help="date:YYYY-MM-DD / task_done:编号 / check:名字")
    a.add_argument("--her", action="store_true", help="等她办的（到了变成她的待办或待你定）")
    a.add_argument("--then", choices=THENS)
    a.add_argument("--milestone", type=int)
    a.add_argument("--owner", default="")
    a.add_argument("--source", default="")
    sub.add_parser("list")
    for c in ("done", "drop"):
        sub.add_parser(c).add_argument("id", type=int)
    args = p.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        db = store.db
        ensure(db)
        if args.cmd == "add":
            print(f"登记了 #{add(db, args.what, args.wait, args.her, args.then, args.milestone, args.owner, args.source)}")
        elif args.cmd == "list":
            for r in db.conn.execute("SELECT * FROM followups WHERE status IN ('waiting', 'due') ORDER BY id"):
                print(f"#{r['id']} [{'她' if r['for_her'] else '系统'}·{r['status']}] {r['what']} —— 等{wait_text(r)}（{r['owner']}）")
        else:
            print("收好了" if close(db, args.id, "done" if args.cmd == "done" else "dropped") else "没有这条，或者已经收了")
    finally:
        store.close()


if __name__ == "__main__":
    main()
