"""她的决定和授权（docs/design/ontology.md 3.1、6.3 节，第一批地基）：先建结构，不改现有行为。

决定：她定下的事（规矩 / 取舍、暂停、约束、阶段门禁、例外），说了就记、记了就生效，和代码发布分开。
每次改都是新版本，旧的标“被取代”，不覆盖；行动前按版本号复核（7.5 节，第二批接上）。
授权：她允许谁做什么、到哪一步、到什么时候；只来自她明确的话，默认会到期，她随时能撤回。
"""

import json
from datetime import datetime, timezone

from . import tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, scope TEXT NOT NULL, statement TEXT NOT NULL, roles TEXT NOT NULL DEFAULT '[]',
    context TEXT, starts_at TEXT NOT NULL, ends_at TEXT, end_cond TEXT, priority INTEGER NOT NULL DEFAULT 0,
    supersedes INTEGER, exception_of INTEGER, quote TEXT, source TEXT, version INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'active', needs_code INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY, grantee TEXT NOT NULL, action TEXT NOT NULL, scope TEXT NOT NULL, each_confirm INTEGER NOT NULL DEFAULT 1,
    until TEXT, one_off INTEGER NOT NULL DEFAULT 0, quote TEXT, created_at TEXT NOT NULL, used_at TEXT, revoked_at TEXT);
"""
KINDS = ("rule", "pause", "constraint", "gate", "exception", "grant")  # 规矩 / 取舍、暂停、约束、阶段门禁、例外、授权
ROLES = ("planner", "prep", "ethan", "dais", "patrol", "maintainer")
ACTIONS = ("draft", "send", "book", "pay", "promise", "disclose")


def ensure(db):
    if tx.once(db, "decisions"):
        db.conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def add(db, kind: str, scope: str, statement: str, roles=(), context: str = None, starts_at: str = None, ends_at: str = None,
        end_cond: str = None, priority: int = 0, supersedes: int = None, exception_of: int = None, quote: str = None,
        source: str = None, needs_code: bool = False) -> int:
    """记一条决定。scope 必填（管哪个项目或对象，不能只写“全部”）；supersedes：取代哪条（旧的标被取代，版本加一）。"""
    ensure(db)
    if kind not in KINDS:
        raise ValueError(f"不认识的决定种类：{kind}")
    if not str(scope or "").strip() or str(scope).strip() in ("全部", "all", "*"):
        raise ValueError("决定要写清管哪个项目或对象，不能只写“全部”")
    bad = [r for r in roles if r not in ROLES]
    if bad:
        raise ValueError(f"不认识的角色：{bad}")
    version = 1
    if supersedes:
        old = db.conn.execute("SELECT version FROM decisions WHERE id = ?", (supersedes,)).fetchone()
        if old is None:
            raise ValueError("要取代的决定不存在")
        version = old["version"] + 1
        db.conn.execute("UPDATE decisions SET status = 'superseded' WHERE id = ?", (supersedes,))
    cur = db.conn.execute(
        "INSERT INTO decisions (kind, scope, statement, roles, context, starts_at, ends_at, end_cond, priority, supersedes, exception_of, "
        "quote, source, version, needs_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (kind, scope.strip(), statement.strip(), json.dumps(list(roles)), context, starts_at or _now(), ends_at, end_cond, priority,
         supersedes, exception_of, quote, source, version, 1 if needs_code else 0, _now()))
    tx.commit(db)
    return cur.lastrowid


def active(db, role: str = None, scope: str = None, now: str = None) -> list:
    """现在生效的决定：没被取代、已开始、没结束；按角色、对象筛。"""
    ensure(db)
    now = now or _now()
    out = []
    for r in db.conn.execute("SELECT * FROM decisions WHERE status = 'active' AND starts_at <= ? AND (ends_at IS NULL OR ends_at > ?) "
                             "ORDER BY priority DESC, id", (now, now)):
        d = {**dict(r), "roles": json.loads(r["roles"])}
        if role and d["roles"] and role not in d["roles"]:
            continue
        if scope and scope not in d["scope"] and d["scope"] not in scope:
            continue
        out.append(d)
    return out


def versions(db, ids) -> dict:
    """{编号: 版本}，行动前复核用：开工时记下，写入前再比一次（ontology.md 7.5 节）。"""
    ensure(db)
    ids = list(ids)
    if not ids:
        return {}
    rows = db.conn.execute(f"SELECT id, version, status FROM decisions WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    return {r["id"]: (r["version"], r["status"]) for r in rows}


def changed_since(db, seen: dict) -> list:
    """开工时看到的那几条决定，现在有哪些被取代或结束了。"""
    now = versions(db, seen)
    return [i for i, v in seen.items() if now.get(i) != v]


def grant(db, grantee: str, action: str, scope: str, each_confirm: bool = True, until: str = None, one_off: bool = False,
          quote: str = None) -> int:
    """记一条授权。只来自她明确的话（quote 写原话）。付款、替她发消息、承诺这类永远每次确认（ontology.md 6.2 节）。"""
    ensure(db)
    if action not in ACTIONS:
        raise ValueError(f"不认识的动作：{action}")
    if action in ("pay", "send", "promise"):
        each_confirm = True
    cur = db.conn.execute("INSERT INTO grants (grantee, action, scope, each_confirm, until, one_off, quote, created_at) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (grantee, action, scope, 1 if each_confirm else 0, until, 1 if one_off else 0,
                                                              quote, _now()))
    tx.commit(db)
    return cur.lastrowid


def allowed(db, grantee: str, action: str, scope: str, now: str = None):
    """这件事有没有生效的授权：返回授权记录（含要不要每次确认），没有返回 None（= 只能起草，等她确认）。"""
    ensure(db)
    now = now or _now()
    for r in db.conn.execute("SELECT * FROM grants WHERE grantee = ? AND action = ? AND revoked_at IS NULL AND (until IS NULL OR until > ?) "
                             "AND NOT (one_off = 1 AND used_at IS NOT NULL) ORDER BY id DESC", (grantee, action, now)):
        if r["scope"] in scope or scope in r["scope"]:
            return dict(r)
    return None


def revoke(db, grant_id: int) -> bool:
    ensure(db)
    n = db.conn.execute("UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(), grant_id)).rowcount
    tx.commit(db)
    return n > 0
