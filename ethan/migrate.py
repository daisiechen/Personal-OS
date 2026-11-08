"""数据改动随上线执行。。
她在开发会话里点过头的改名、删重复待办这类数据改动，不用再到服务器会话里确认。

每个改动是 migrations/ 下的一个脚本，文件名按日期开头（如 20261029_del_105.py），里面写一个 run(store) 函数，
返回一句话说明做了什么。自动部署在备份数据库、跑完测试之后执行还没跑过的脚本，按文件名顺序，每个只跑一次。
跑过的记在数据库的 migrations_done 表里，和脚本的数据改动同一个事务提交（Codex 评审 11-05：先提交数据、后写文件，
中间断了会把改过的再跑一遍）：跑之前先写一行“跑到一半”，脚本自己 commit 时这一行跟着一起落库；跑完改成成功 / 失败。
<数据目录>/migrations.json 只是给人看的副本；以前只记在 json 里的，第一次运行时导入表里。
某个脚本出错就回滚它那一次（还没提交的）改动、记下错误，不影响已经上线的代码。
只放她在会话里明确同意的改动；脚本开头写上她的原话和日期。

运行：python3 -m ethan.migrate --data <数据目录> [--repo <仓库目录>]
"""

import argparse
import importlib.util
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DONE_FILE = "migrations.json"
DB_FILE = "ethan.sqlite"
SCHEMA = "CREATE TABLE IF NOT EXISTS migrations_done (name TEXT PRIMARY KEY, at TEXT NOT NULL, ok INTEGER NOT NULL, note TEXT)"
HALFWAY = "跑到一半被打断（脚本自己提交过的改动已经落库；不再重跑，要补就写新脚本）"


def _json(data: Path) -> dict:
    try:
        return json.loads((Path(data) / DONE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _table(conn) -> dict:
    try:
        rows = conn.execute("SELECT name, at, ok, note FROM migrations_done").fetchall()
    except sqlite3.Error:
        return {}
    return {r[0]: {"at": r[1], "ok": bool(r[2]), "note": r[3] or ""} for r in rows}


def load(data: Path, db=None) -> dict:
    """跑过的：数据库表为准，加上 json 里的（老记录还没导入时）。"""
    done = _json(data)
    if db is not None:
        done.update(_table(db.conn))
        return done
    path = Path(data) / DB_FILE
    if path.exists():
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                done.update(_table(conn))
            finally:
                conn.close()
        except sqlite3.Error:
            pass
    return done


def pending(repo: Path, data: Path, db=None) -> list:
    done = load(data, db)
    return [p for p in sorted((Path(repo) / "migrations").glob("[0-9]*.py")) if p.stem not in done]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mark(conn, name: str, ok: bool, note: str):
    conn.execute("INSERT OR REPLACE INTO migrations_done (name, at, ok, note) VALUES (?, ?, ?, ?)", (name, _now(), 1 if ok else 0, note[:300]))


def run_all(store, repo: Path, data: Path, only=None) -> list:
    """返回 [(名字, 成功没有, 说明)]。only(名字) 为真的才跑（检验题闸门先跑改检验题的那几个，见 memeval）。"""
    conn = store.db.conn
    if conn.in_transaction:
        conn.commit()  # 之前没提交的先落定，不混进第一个脚本
    _json_import(conn, data)
    out = []
    for path in pending(repo, data, store.db):
        if only is not None and not only(path.stem):
            continue
        spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        # 先在同一个事务里记一行“跑到一半”：脚本自己 commit 时跟数据改动一起落库，断在哪里都不会把改过的再跑一遍
        _mark(conn, path.stem, False, HALFWAY)
        try:
            spec.loader.exec_module(mod)
            note = str(mod.run(store) or "")
            ok = True
            _mark(conn, path.stem, True, note)
            conn.commit()
        except Exception as e:  # 出错就回滚这一个，记下来，下次不再重试（要改就写新脚本）
            conn.rollback()
            note, ok = f"{type(e).__name__}: {str(e)[:200]}", False
            _mark(conn, path.stem, False, note)
            conn.commit()
        out.append((path.stem, ok, note))
        _write_copy(store.db, data)  # 给人看的副本
    return out


def _json_import(conn, data: Path):
    conn.execute(SCHEMA)
    for name, r in _json(data).items():
        conn.execute("INSERT OR IGNORE INTO migrations_done (name, at, ok, note) VALUES (?, ?, ?, ?)",
                     (name, str(r.get("at") or ""), 1 if r.get("ok") else 0, str(r.get("note") or "")))
    conn.commit()


def _write_copy(db, data: Path):
    try:
        (Path(data) / DONE_FILE).write_text(json.dumps(_table(db.conn), ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser(description="执行还没跑过的数据改动")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    # 部署时后台整理可能正占着写锁（11-07 15:39 那次 10 秒没拿到，迁移整个没跑）：迁移最多等 2 分钟
    store.db.conn.execute("PRAGMA busy_timeout = 120000")
    try:
        got = run_all(store, args.repo, args.data)
    finally:
        store.close()
    for name, ok, note in got:
        print(f"{name}：{'成功' if ok else '失败'} {note}", flush=True)
    bad = [name for name, ok, _ in got if not ok]
    if bad:  # 有失败的就非零退出：自动部署据此把结果写成“已上线，但数据改动出错”，巡检会报（Codex 评审 11-05）
        print("出错的：" + "、".join(bad), file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
