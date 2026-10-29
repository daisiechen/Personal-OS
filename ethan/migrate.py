"""数据改动随上线执行。她原话（2026-10-29，云端开发会话）：“同意：我在开发会话里同意的数据改动，随上线自动执行”。
她在开发会话里点过头的改名、删重复待办这类数据改动，不用再到服务器会话里确认。

每个改动是 migrations/ 下的一个脚本，文件名按日期开头（如 20261029_del_105.py），里面写一个 run(store) 函数，
返回一句话说明做了什么。自动部署在备份数据库、跑完测试之后执行还没跑过的脚本，按文件名顺序，每个只跑一次，
跑过的记在 <数据目录>/migrations.json。某个脚本出错就回滚它那一次的改动、记下错误，不影响已经上线的代码。
只放她在会话里明确同意的改动；脚本开头写上她的原话和日期。

运行：python3 -m ethan.migrate --data <数据目录> [--repo <仓库目录>]
"""

import argparse
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

DONE_FILE = "migrations.json"


def pending(repo: Path, data: Path) -> list:
    done = load(data)
    return [p for p in sorted((Path(repo) / "migrations").glob("[0-9]*.py")) if p.stem not in done]


def load(data: Path) -> dict:
    try:
        return json.loads((Path(data) / DONE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def run_all(store, repo: Path, data: Path) -> list:
    """返回 [(名字, 成功没有, 说明)]。"""
    done, out = load(data), []
    for path in pending(repo, data):
        spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
            note = str(mod.run(store) or "")
            store.db.conn.commit()
            ok = True
        except Exception as e:  # 出错就回滚这一个，记下来，下次不再重试（要改就写新脚本）
            store.db.conn.rollback()
            note, ok = f"{type(e).__name__}: {str(e)[:200]}", False
        done[path.stem] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "ok": ok, "note": note[:300]}
        out.append((path.stem, ok, note))
    if out:
        (Path(data) / DONE_FILE).write_text(json.dumps(done, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def main():
    parser = argparse.ArgumentParser(description="执行还没跑过的数据改动")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        for name, ok, note in run_all(store, args.repo, args.data):
            print(f"{name}：{'成功' if ok else '失败'} {note}", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
