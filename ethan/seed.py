"""冷启动导入：把初始资料一次性写进记忆库。

种子目录格式（私人数据，只放服务器，不进本仓库）：
  <seed>/wiki/<页面名>.md   直接成为 Wiki 页面；第一行 “# 标题” 作为页面标题
  <seed>/seed.json          结构化数据：
    {"goals":  [{"key": "vision", "title": "...", "level": "vision", "parent": null,
                 "status": "active", "next_step": "...", "due": "2026-12-31", "domain": "..."}],
     "people": [{"name": "小雨", "circle": "挚友", "birthday": "MM-DD", "notes": "..."}],
     "wishes": [{"quote": "...", "context": "..."}],
     "learning": [{"subject": "english", "content": "...", "note": "..."}]}

用法：python3 -m ethan.seed --seed <种子目录> --data <数据目录>
重复导入时，已存在的 Wiki 页面和人物卡会被更新，目标和心愿按标题去重。
"""

import argparse
import json
from pathlib import Path

from .memory import MemoryStore

GOAL_FIELDS = ("motivation", "success_criteria", "next_step", "due", "domain")


def import_seed(seed: Path, store: MemoryStore, source: str = "冷启动导入") -> dict:
    seed = Path(seed)
    counts = {"wiki": 0, "goals": 0, "people": 0, "wishes": 0, "learning": 0}
    wiki_dir = seed / "wiki"
    if wiki_dir.is_dir():
        for path in sorted(wiki_dir.rglob("*.md")):
            slug = path.relative_to(wiki_dir).with_suffix("").as_posix()
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            title = lines[0].lstrip("# ").strip() if lines and lines[0].startswith("#") else slug
            body = "\n".join(lines[1:] if lines and lines[0].startswith("#") else lines)
            store.wiki.write(slug, title, body, sources=[source])
            counts["wiki"] += 1

    data_path = seed / "seed.json"
    data = json.loads(data_path.read_text(encoding="utf-8")) if data_path.exists() else {}
    db = store.db

    existing = {r["title"]: r["id"] for r in db.conn.execute("SELECT id, title FROM goals")}
    keys = {}
    for g in data.get("goals", []):
        parent = keys.get(g.get("parent")) if g.get("parent") else None
        if g["title"] in existing:
            keys[g.get("key")] = existing[g["title"]]
            continue
        extra = {k: g[k] for k in GOAL_FIELDS if g.get(k)}
        goal_id = db.add_goal(g["title"], g["level"], parent_id=parent,
                              status=g.get("status", "not_started"), source=source, **extra)
        keys[g.get("key")] = goal_id
        existing[g["title"]] = goal_id
        counts["goals"] += 1

    for p in data.get("people", []):
        fields = {k: v for k, v in p.items() if k != "name" and v is not None}
        db.upsert_person(p["name"], **fields)
        counts["people"] += 1

    open_quotes = {w["quote"] for w in db.open_wishes()}
    for w in data.get("wishes", []):
        if w["quote"] not in open_quotes:
            db.add_wish(w["quote"], w.get("context"))
            counts["wishes"] += 1

    known = {r["content"] for r in db.conn.execute("SELECT content FROM learning_items")}
    for item in data.get("learning", []):
        if item["content"] not in known:
            db.add_learning_item(item["subject"], item["content"], item.get("note"))
            counts["learning"] += 1
    return counts


def main():
    parser = argparse.ArgumentParser(description="小伴 记忆冷启动导入")
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    store = MemoryStore(args.data)
    try:
        print(import_seed(args.seed, store))
    finally:
        store.close()


if __name__ == "__main__":
    main()
