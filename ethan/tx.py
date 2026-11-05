"""一次动作一个事务（docs/design/ontology-contracts.md 1.7 节，Codex 第二轮评审 11-03）。

问题：lifebase、decisions、locks、ontology 里的辅助函数各自 commit；建表用的 executescript 也会顺手提交。
动作做到一半出错时，前半段已经写进去了，回滚不掉。

做法：
- 辅助函数改用 `tx.commit(db)`：在动作里面时不提交，由动作统一提交或回滚；动作外面照旧马上提交（现有调用方行为不变）。
- 建表只在每个连接第一次时跑（`tx.once`），动作里面不会再触发 executescript。
- `with tx.action(db, 种类, 对象, 依据版本, 谁):` 开一个事务：同一编号（种类 + 对象 + 依据版本）做过就跳过；
  里面出错整体回滚，并单独记一条失败；成功记一条 done。
"""

from contextlib import contextmanager
from datetime import datetime, timezone


class AlreadyDone(Exception):
    """同一编号的动作已经做过了：调用方直接用上次的结果。"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def in_action(db) -> bool:
    return bool(getattr(db, "_tx_depth", 0))


def commit(db) -> None:
    """辅助函数用：动作里面不提交，动作外面马上提交。"""
    if not in_action(db):
        db.conn.commit()


def once(db, name: str) -> bool:
    """这个连接上第一次做某件建表之类的事：返回 True（并记下），之后返回 False。"""
    done = getattr(db, "_tx_once", None)
    if done is None:
        done = set()
        try:
            db._tx_once = done
        except AttributeError:
            return True
    if name in done:
        return False
    done.add(name)
    return True


@contextmanager
def action(db, kind: str, target: str, basis: str, actor: str):
    """一次动作一个事务。编号 = 种类 | 对象 | 依据版本；同一编号做过（done）就抛 AlreadyDone。"""
    from . import decisions, lifebase, locks, ontology
    if in_action(db):
        raise RuntimeError("动作里不能再开动作（一次动作一个事务）")
    for ensure in (lifebase.ensure, decisions.ensure, locks.ensure, ontology.ensure):
        ensure(db)  # 先把表建好：动作里面不会再跑会自动提交的建表脚本
    if db.conn.in_transaction:
        db.conn.commit()  # 调用方之前没提交的，先落定，不混进这次动作
    key = f"{kind}|{target}|{basis}"
    if db.conn.execute("SELECT 1 FROM action_log WHERE action_key = ? AND result = 'done'", (key,)).fetchone():
        raise AlreadyDone(key)
    db.conn.execute("BEGIN IMMEDIATE")
    db._tx_depth = 1
    try:
        yield key
        db.conn.execute("DELETE FROM action_log WHERE action_key = ? AND result != 'done'", (key,))  # 以前失败过的这次成功了
        db.conn.execute("INSERT INTO action_log (kind, target, basis, action_key, actor, at, result) VALUES (?, ?, ?, ?, ?, ?, 'done')",
                        (kind, target, basis, key, actor, _now()))
        db.conn.commit()
    except BaseException:
        db.conn.rollback()
        db._tx_depth = 0
        try:
            db.conn.execute("INSERT OR REPLACE INTO action_log (kind, target, basis, action_key, actor, at, result) "
                            "VALUES (?, ?, ?, ?, ?, ?, 'failed')", (kind, target, basis, key, actor, _now()))
            db.conn.commit()
        except Exception:
            pass
        raise
    finally:
        db._tx_depth = 0


class _Held:
    """atomic 里替换 db.conn 的外壳：commit 不提交（由 atomic 最后统一提交），executescript 拆成一句一句执行（它本来会先提交）。"""

    def __init__(self, conn):
        object.__setattr__(self, "_conn", conn)

    def commit(self):
        pass

    def executescript(self, sql: str):
        import sqlite3
        buf = ""
        for part in sql.split(";"):
            buf += part + ";"
            if sqlite3.complete_statement(buf):
                if buf.strip(" \n\t;"):
                    self._conn.execute(buf)
                buf = ""
        if buf.strip(" \n\t;"):
            self._conn.execute(buf)
        return self

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)


@contextmanager
def atomic(db):
    """她的一次改动一个事务（Codex 评审 11-05）：写值和记“她改过”的锁一起提交，中间出错一起回滚。
    BEGIN IMMEDIATE 拿写锁：自动流程（每晚整理、人物卡整理、计划器）要么在她改之前写完，要么等她改完、看到锁再决定。
    里面调用的函数照旧 commit()，在这里都不提交，最后统一提交。已经在事务里（action / atomic）就直接并进去。"""
    if in_action(db):
        yield
        return
    real = db.conn
    if real.in_transaction:
        real.commit()  # 调用方之前没提交的，先落定
    real.execute("BEGIN IMMEDIATE")
    db._tx_depth = 1
    db.conn = _Held(real)
    try:
        yield
        real.commit()
    except BaseException:
        real.rollback()
        raise
    finally:
        db.conn = real
        db._tx_depth = 0
