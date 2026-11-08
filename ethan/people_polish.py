"""人物卡“关系”“怎么认识的”过一遍话术。

一次性：把现有的两项交给模型改写成完整、清楚的一句话，只用卡上已有的内容（关系、怎么认识的、关系圈、话题、近况），不添新事实；
微信“通过好友验证”这类加好友手续去掉，只剩手续、看不出缘由的“怎么认识的”写空。她自己改过的（manual_locks 里锁着的）一律不动。
先 --dry 出改前改后对照（只放她的数据目录，不进仓库），看过再真写。

python3 -m ethan.people_polish --data <数据目录> --dry --out 对照.json
python3 -m ethan.people_polish --data <数据目录>
"""

import argparse
import json
from pathlib import Path

PROMPT = """你在帮 Mia 整理她微信人物卡上的两项：relation（这个人和她是什么关系）、how_met（怎么认识的、为什么加的微信）。
下面每行是一张卡（JSON），字段：id、name、relation、how_met，以及参考用的 circle（关系圈）、topics（常聊的话题）、recent（近况）。
把 relation、how_met 改写成完整、清楚、通顺的话：
- 这是改话术，不是改内容：意思必须和原文完全一样。只用这一行里已经有的信息，不添、不猜；不要把 recent 里的近况写进 relation。
- 谁做了什么不能改：原文是“她自我介绍”就还是她，原文是“对方加她”就还是对方；“她”永远指 Mia，对方用名字或“对方”。
- 不加原文没有的先后顺序、关系定性（“后来成为”“同门”“自称”这类原文没有就不写）。
- 原文只是标点不统一、多了半角逗号，就只改标点。
- relation：一句话说清对方是谁、和她什么关系，20–40 字；“或”“可能”这类含糊的，挑卡上更有依据的那个说，拿不准就保留原意。
- how_met：说清缘由和渠道（在哪认识、谁介绍、为什么加的微信），可带年月；“通过了好友验证”“她发了好友请求”“对方通过了”这类加好友手续去掉；
  谁主动加的、谁先自我介绍算缘由的一部分，保留（比如“2026-01-19 对方主动加她微信并自我介绍”原样留着）；去掉手续后什么都不剩的才写空字符串。
- 不要半句话、不要以逗号结尾；中文标点统一用全角（。）；不要用“她”以外的称呼指 Mia。
- 原来已经清楚完整的，原样返回。原来是空的，保持空。
只输出 JSON 数组，每张卡一项：{"id": 编号, "relation": "...", "how_met": "..."}。"""
BATCH = 30


def candidates(db) -> list:
    from . import relations
    relations.ensure(db)
    mine = {}
    try:
        for r in db.conn.execute("SELECT obj, field FROM manual_locks WHERE obj LIKE 'person:%' AND released_at IS NULL AND field IN ('relation', 'how_met')"):
            mine.setdefault(int(r["obj"].split(":")[1]), set()).add(r["field"])
    except Exception:
        pass
    out = []
    for r in db.conn.execute("SELECT id, name, relation, how_met, circle, topics, recent FROM people "
                             "WHERE COALESCE(relation, '') != '' OR COALESCE(how_met, '') != '' ORDER BY id"):
        locked = mine.get(r["id"], set())
        if {"relation", "how_met"} <= locked:
            continue
        out.append({**dict(r), "locked": sorted(locked)})
    return out


def _clean(v, limit: int) -> str:
    v = " ".join(str(v or "").split()).replace(",", "，").rstrip("")
    return v[:limit]


def polish(db, router, dry: bool = False, limit: int = None) -> dict:
    rows = candidates(db)[:limit] if limit else candidates(db)
    changes = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        by_id = {r["id"]: r for r in chunk}
        listing = "\n".join(json.dumps({k: r[k] for k in ("id", "name", "relation", "how_met", "circle", "topics", "recent")}, ensure_ascii=False)
                            for r in chunk)
        try:
            text = router.complete(PROMPT, [{"role": "user", "content": listing}], purpose="relations-polish").text
            items = json.loads(text[text.find("["):text.rfind("]") + 1])
        except Exception as e:
            print(f"[people_polish] 第 {i // BATCH + 1} 批没成：{type(e).__name__} {str(e)[:120]}", flush=True)
            continue
        for it in items if isinstance(items, list) else []:
            r = by_id.get(it.get("id")) if isinstance(it, dict) else None
            if r is None:
                continue
            new = {"relation": _clean(it.get("relation"), 60), "how_met": _clean(it.get("how_met"), 200)}
            for k in ("relation", "how_met"):
                old = r[k] or ""
                if k in r["locked"] or new[k] == old or (not old and new[k]):  # 她改过的不动；原来空的不让模型补
                    continue
                if k == "relation" and not new[k]:  # 关系不清空
                    continue
                changes.append({"id": r["id"], "name": r["name"], "field": k, "old": old, "new": new[k]})
    if not dry:
        for c in changes:
            db.conn.execute(f"UPDATE people SET {c['field']} = ? WHERE id = ?", (c["new"] or None, c["id"]))
        db.conn.commit()
    return {"looked": len(rows), "changed": len(changes), "people": len({c["id"] for c in changes}), "changes": changes}


def main():
    parser = argparse.ArgumentParser(description="人物卡“关系”“怎么认识的”改写成完整的话")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        got = polish(store.db, build_router(args.data, 50.0), dry=args.dry, limit=args.limit)
        if args.out:
            args.out.write_text(json.dumps(got["changes"], ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps({k: got[k] for k in ("looked", "changed", "people")}, ensure_ascii=False), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
