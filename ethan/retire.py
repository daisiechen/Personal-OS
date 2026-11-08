"""旧结构下线（ontology-plan 3.4，目标 11-17，不是门禁；docs/design/ontology-structure.md 9.2 节“下线替代清单”）。

旧 facts 表从 11-03 起各角色已改读新结构（statements，经 onto_query / memsearch / portrait.chat_text 统一入口），
这里管三件事，**不删任何表**（表先只读一个月，删之前问她）：
1. 静态清单 LEGACY：还有哪些模块直接写 SQL 读写 facts、为什么还不能改。`tests/test_retired.py` 按它把关：
   清单外的文件不许新增直接读 facts 的地方，清单里的文件次数不许变多（改走新结构了就把数字调小）。
2. 运行期计数：`watch()` 给每个数据库连接挂一个 SQLite authorizer（只在编译 SQL 时调一次，不是每次执行），
   记下哪个模块、哪一行编译了读 / 写 facts 的语句，攒在内存里，隔 5 分钟和退出时写进 <数据目录>/legacy-reads.json（按天）。
   触发器里的（双写）不算。关掉：环境变量 ETHAN_LEGACY_TRACE=0。
3. 下线体检 `python3 -m ethan.onto_query --retire-check --data …`：静态清单 + 运行期计数 + 对账差异 + 回退演练，
   回退演练在内存里的副本上做，不动她的数据。

回退开关（3.4 起改走新结构的读路径用）：`fallback(db)` 为真时这些地方切回读旧 facts。
  打开：python3 -m ethan.onto_query --data /home/ethan/data --fallback on；关掉：--fallback off。
"""

import json
import os
import re
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HERE = Path(__file__).resolve().parent

# 文件 → (直接读的次数, 直接写的次数, 为什么还在这里)。次数按 scan() 的口径（SQL 里从 facts 表查（FROM、JOIN）算读，
# 往 facts 表插、改、删（INSERT、UPDATE、DELETE）算写，调 lifebase.current_facts 算读）。
# 写路径保留双写（触发器同步说法），读路径能改的已改；改不了的写原因，等对应的东西进新结构再改。
LEGACY = {
    "ethan/lifebase.py": (35, 14, "facts 的主人：写入、双写触发器、迁移、对账、删除和暂停、去重判断、合并人物卡时改主语、挑规矩 / 在做的事 / 住处的候选（11-07）都在这里；"
                                  "source_health 本月贡献 3.4 已改读说法（回退用的旧查询留着，算 1 次读）。读的大多是写之前的去重 / 锁判断，属于写路径；"
                                  "删后停用在说法上只是 candidate，fact_state 的 SQL 版（_state_case）/ statement_gone / disclose 按对照查回旧状态挡掉"
                                  "（3 次读，Codex 评审 11-05；11-06“算不算数”统一成 fact_state 后 NOT_GONE_STATEMENT 并进 _state_case），"
                                  "等说法有了自己的停用状态再改；临时状态到期（借鉴 6，11-05）：写入时带 until、迁移补 until、"
                                  "触发器 v5 补说法的 until（2 次读、2 次写，都是写路径）；撤销“待你定”的恢复从 mepage 挪进 undo_fact"
                                  "（先读现在的样子、删掉停用的和后来改过的不动，1 次读、1 次写，Codex 评审 11-07 第 2 条）"),
    "ethan/mepage.py": (14, 11, "“我”页：十个方面的厚薄、全部记忆、待你定、纠错都按“方面（dimension）”算；"
                                "说法里细属性不带方面，要等“看法（views）”进新结构（ontology-structure 第 6 节）才能改；改的是她的直接操作，写路径保留双写；"
                                "全部记忆每条下面“意思相近的”（借鉴 8，11-05）按编号取回那几条的原文和状态（1 次读），跟着全部记忆一起改"),
    "ethan/memusers.py": (1, 3, "纠错单：按检索结果找到那条旧记录再改对 / 作废，是写路径，保留双写"),
    "ethan/portrait.py": (5, 3, "画像 chat_text 是登记过的统一入口（3.3 对外部分逐条过 lifebase.disclose，多读一次）；每晚整理的去重和置信度衰减是写路径；"
                                 "按方面给 小伴 的画像同样等 views 进新结构"),
    "ethan/memsearch.py": (3, 0, "统一检索入口本身（3.3 对外检索按 disclose 逐条判断，多读一次）：索引从 facts 建、命中后取原行，改成从说法建索引要重建全部向量，排在旧表只读那个月里做"),
    "ethan/extract.py": (1, 1, "即时抽取写入前的去重（同方面现有的几条）和刷新确认时间，是写路径"),
}
TRACE_FILE = "legacy-reads.json"
FLUSH_EVERY = 300  # 秒
_READ = re.compile(r"\b(?:FROM|JOIN)\s+facts\b(?!_)")
_WRITE = re.compile(r"\b(?:INSERT\s+(?:OR\s+\w+\s+)?INTO|UPDATE|DELETE\s+FROM)\s+facts\b(?!_)")
_HELPER = re.compile(r"\blifebase\.current_facts\(")


# ---------- 1. 静态清单 ----------
def scan(root: Path = ROOT) -> dict:
    """ethan/ 下每个 .py：直接读、写 facts 的次数。DELETE FROM 只算写。"""
    out = {}
    for p in sorted((root / "ethan").rglob("*.py")):
        if p.name == "retire.py":
            continue
        text = p.read_text(encoding="utf-8")
        writes = len(_WRITE.findall(text))
        reads = len(_READ.findall(text)) - len(re.findall(r"\bDELETE\s+FROM\s+facts\b(?!_)", text)) + len(_HELPER.findall(text))
        if reads or writes:
            out[str(p.relative_to(root))] = {"reads": reads, "writes": writes}
    return out


def unlisted(root: Path = ROOT) -> list:
    """清单外新增的、或者比清单记的次数多了的（test_retired 和下线体检都看这个）。"""
    bad = []
    for f, n in scan(root).items():
        allow = LEGACY.get(f)
        if allow is None:
            bad.append(f"{f}：清单外直接读写旧 facts（读 {n['reads']}、写 {n['writes']}），改走 onto_query / memsearch，或写清原因登记进 retire.LEGACY")
        elif n["reads"] > allow[0] or n["writes"] > allow[1]:
            bad.append(f"{f}：读 {n['reads']}（清单 {allow[0]}）、写 {n['writes']}（清单 {allow[1]}），比清单多了")
    return bad


# ---------- 2. 运行期计数 ----------
_lock = threading.Lock()
_counts = {}  # 数据目录 → {天: {模块: {"read": n, "write": n}}}
_last = {"flush": time.time(), "key": None}
_WRITES = (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE)


def _caller():
    f = sys._getframe(2)
    while f is not None:
        fn = f.f_code.co_filename
        if fn.startswith(str(HERE)) and not fn.endswith(("retire.py", os.path.join("memory", "db.py"))):
            return f, str(Path(fn).relative_to(ROOT))
        f = f.f_back
    return None, None


def watch(conn, path) -> None:
    """给这个连接挂上计数钩子（内存库不挂）。钩子出任何错都放行，不影响正常读写。"""
    if os.environ.get("ETHAN_LEGACY_TRACE", "1") == "0" or str(path) == ":memory:":
        return
    home = str(Path(path).parent)

    def hook(action, table, _col, _db, trigger):
        try:
            if table == "facts" and trigger is None and (action == sqlite3.SQLITE_READ or action in _WRITES):
                frame, mod = _caller()
                if mod:
                    key = (id(frame), frame.f_lasti, home)
                    if key != _last["key"]:  # 一条语句编译时每一列都会来一次，只算一次
                        _last["key"] = key
                        kind = "write" if action in _WRITES else "read"
                        day = datetime.now(timezone.utc).date().isoformat()
                        with _lock:
                            slot = _counts.setdefault(home, {}).setdefault(day, {}).setdefault(mod, {"read": 0, "write": 0})
                            slot[kind] += 1
                    if time.time() - _last["flush"] > FLUSH_EVERY:
                        flush()
        except Exception:
            pass
        return sqlite3.SQLITE_OK

    try:
        conn.set_authorizer(hook)
    except Exception:
        pass


def flush() -> None:
    """把内存里的计数加进各数据目录的 legacy-reads.json（只留最近 60 天）。"""
    with _lock:
        pending = {h: d for h, d in _counts.items() if d}
        _counts.clear()
        _last["flush"] = time.time()
    for home, days in pending.items():
        p = Path(home) / TRACE_FILE
        try:
            if not p.parent.exists():
                continue
            have = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
            for day, mods in days.items():
                for mod, n in mods.items():
                    slot = have.setdefault(day, {}).setdefault(mod, {"read": 0, "write": 0})
                    slot["read"] += n["read"]
                    slot["write"] += n["write"]
            have = dict(sorted(have.items())[-60:])
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(have, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass


import atexit  # noqa: E402
atexit.register(flush)


def runtime(data: Path, days: int = 7) -> dict:
    """最近几天运行期里哪些模块编译过读 / 写 facts 的语句（次数是编译次数，不是执行次数）。"""
    flush()
    p = Path(data) / TRACE_FILE
    try:
        have = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"days": 0, "modules": {}}
    keep = sorted(have)[-days:]
    mods = {}
    for d in keep:
        for mod, n in have[d].items():
            slot = mods.setdefault(mod, {"read": 0, "write": 0})
            slot["read"] += n["read"]
            slot["write"] += n["write"]
    return {"days": len(keep), "since": keep[0] if keep else None, "modules": dict(sorted(mods.items(), key=lambda x: -x[1]["read"]))}


# ---------- 回退开关 ----------
FALLBACK = "_legacy_reads"  # 存在 onto_query 的 onto_switch 表里（不是登记过的使用者，只是一行开关）


def fallback(db) -> bool:
    """3.4 起改走新结构的读路径要不要切回读旧 facts（默认不切）。读不到开关当不切。"""
    try:
        r = db.conn.execute("SELECT on_at, off_at FROM onto_switch WHERE user = ?", (FALLBACK,)).fetchone()
    except Exception:
        return False
    return bool(r and r["on_at"] and not r["off_at"])


def set_fallback(db, on: bool, note: str = "") -> None:
    from . import onto_query
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    db.conn.execute(onto_query.SWITCH)
    if on:
        db.conn.execute("INSERT OR REPLACE INTO onto_switch (user, on_at, off_at, note) VALUES (?, ?, NULL, ?)", (FALLBACK, now, note))
    else:
        db.conn.execute("UPDATE onto_switch SET off_at = ?, note = ? WHERE user = ?", (now, note, FALLBACK))
    db.conn.commit()


# ---------- 3. 下线体检 ----------
class _Copy:
    """内存里的数据库副本：回退演练在这上面做，不动她的数据。"""

    def __init__(self, conn):
        if conn.in_transaction:  # 源库有没提交的事务时 backup 会一直等
            conn.commit()
        self.conn = sqlite3.connect(":memory:", check_same_thread=False)
        conn.backup(self.conn)
        self.conn.row_factory = sqlite3.Row

    def close(self):
        self.conn.close()


def drill(db) -> dict:
    """回退演练：在副本上① 已切到新结构的角色逐个切回旧的、再切回来；② 打开 3.4 的回退开关，
    切回旧读路径后 source_health 的数字和新读路径一样。都对就算过。"""
    from . import lifebase, onto_query
    copy = _Copy(db.conn)
    out = {"users": [], "fallback_same": None, "ok": True, "problems": []}
    try:
        try:
            users = [r[0] for r in copy.conn.execute("SELECT user FROM onto_switch WHERE on_at IS NOT NULL AND off_at IS NULL AND user != ?", (FALLBACK,))]
        except sqlite3.OperationalError:
            users = []
        for u in users:
            onto_query.switch(copy, u, False, "回退演练")
            back = not onto_query.reading_new(copy, u)
            onto_query.switch(copy, u, True, "回退演练")
            again = onto_query.reading_new(copy, u)
            out["users"].append({"user": u, "rolled_back": back, "restored": again})
            if not (back and again):
                out["problems"].append(f"{u} 的开关切不动")
        now = datetime.now(timezone.utc)
        new = [(s["code"], s["facts_this_month"]) for s in lifebase.source_health(copy, now)]
        set_fallback(copy, True, "回退演练")
        on = fallback(copy)
        old = [(s["code"], s["facts_this_month"]) for s in lifebase.source_health(copy, now)]
        set_fallback(copy, False, "回退演练")
        out["fallback_same"] = on and new == old and not fallback(copy)
        if not out["fallback_same"]:
            out["problems"].append(f"回退开关切回旧读路径后数字不一样：新 {new}，旧 {old}")
    finally:
        copy.close()
    out["ok"] = not out["problems"]
    return out


def check(db, data: Path = None, root: Path = ROOT) -> dict:
    """下线体检（只读）：还有谁在读旧表、对账差异、没对照的旧记录、回退演练、计划（不删表）。"""
    from . import lifebase
    lifebase.ensure(db)
    diffs = lifebase.reconcile_statements(db)
    static = scan(root)
    report = {
        "static": [{"file": f, **n, "why": (LEGACY.get(f) or (0, 0, "（清单外）"))[2]} for f, n in static.items()],
        "unlisted": unlisted(root),
        "runtime": runtime(data) if data else {"days": 0, "modules": {}},
        "reconcile_diffs": len(diffs), "reconcile_examples": diffs[:5],
        "unmapped_facts": lifebase.unmapped_facts(db),
        "drill": drill(db),
        "fallback_on": fallback(db),
        "plan": "旧 facts 照常双写；各模块读路径改完、对账一直 0 差异、回退演练过了，才进“只读一个月”（停写，触发器留着对账）；"
                "删表之前问她，不自动删。",
    }
    report["ready_to_freeze"] = (not report["unlisted"] and not diffs and not report["unmapped_facts"] and report["drill"]["ok"]
                                 and sum(n["reads"] for f, n in static.items()) == 0)
    return report
