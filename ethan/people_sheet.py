"""人物卡整表导出、填好导回（“现在关系里面1000多人，我一个一个check太慢了，你给我一个excel，我把重要程度、关系圈、多少天提醒一次选好，
你一次性处理历史数据，其他的近况、怎么认识的、关系、备注什么的一起给我，我能写的就写，写不了的以后我找机会慢慢跟小伴 聊补上”）。

- 导出：每张卡一行（rows），连同导出时的原值；Excel 在她 Mac 上由 scripts/people_xlsx.py 生成、读回（服务器上不装 openpyxl）。
- 导回：只写她**改过**的格子（新值和导出时的原值不同），走 relations.set_field——和她在卡上改一样：
  重要程度、关系圈、提醒间隔、生日、分组、名字是她的设置，自动整理不会改；关系、怎么认识的、近况、话题是参考，以后整理参考她写的再按聊天调整。
  没动的格子不碰，导出以后 小伴 新整理出的内容不会被表里的旧值盖掉。

python3 -m ethan.people_sheet --data <数据目录> --export 卡.json
python3 -m ethan.people_sheet --data <数据目录> --apply 改.json [--dry]
"""

import argparse
import json
from pathlib import Path

STARS = {0: "☆☆☆", 1: "★☆☆", 2: "★★☆", 3: "★★★"}
# (键, 表头, 能不能改)
COLUMNS = (("id", "编号（别改）", False), ("name", "名字", True), ("importance", "重要程度", True), ("circle", "关系圈", True),
           ("every", "多少天提醒一次", True), ("relation", "关系", True), ("how_met", "怎么认识的", True), ("recent", "对方近况", True),
           ("topics", "常聊的话题（逗号隔开）", True), ("birthday", "生日（MM-DD）", True), ("notes", "备注", True),
           ("labels", "分组（逗号隔开）", True), ("last_contact", "最近联系", False), ("n365", "近一年条数", False), ("source", "来源", False))
EDIT = {k for k, _, ok in COLUMNS if ok}
FIELD = {"importance": "importance", "every": "contact_every_days", "labels": "wx_labels"}  # 表里的键 → 卡上的字段


def _list(v) -> str:
    try:
        v = json.loads(v) if isinstance(v, str) and v.startswith("[") else v
    except ValueError:
        pass
    return "，".join(str(x) for x in v) if isinstance(v, list) else str(v or "")


def _every(v) -> str:
    from .relations import NEVER
    return "默认" if v is None else "永不" if int(v) == NEVER else str(int(v))


def rows(db) -> list:
    """每张卡一行，值都是表里显示的样子（字符串）。按近一年聊得多的排前面，先填常联系的。"""
    from . import relations
    relations.ensure(db)
    out = []
    for r in db.conn.execute("SELECT * FROM people"):
        p = dict(r)
        st = relations._j(p.get("stats"), {})
        out.append({"id": p["id"], "name": p["name"] or "", "importance": STARS.get(int(p.get("importance") or 0), "☆☆☆"),
                    "circle": p.get("circle") or "", "every": _every(p.get("contact_every_days")), "relation": p.get("relation") or "",
                    "how_met": p.get("how_met") or "", "recent": p.get("recent") or "", "topics": _list(p.get("topics")),
                    "birthday": p.get("birthday") or "", "notes": p.get("notes") or "", "labels": _list(p.get("wx_labels")),
                    "last_contact": str(p.get("last_contact") or "")[:10], "n365": int(st.get("n365") or 0), "source": p.get("source") or ""})
    out.sort(key=lambda x: (-x["n365"], x["last_contact"] == "", x["last_contact"]), reverse=False)
    return out


def _value(key: str, v: str):
    """表里填的 → set_field 收的值；认不出的抛 ValueError。"""
    v = " ".join(str(v if v is not None else "").split())
    if key == "importance":
        n = v.count("★") if "★" in v or "☆" in v else int(v or 0) if v.isdigit() else -1
        if v in ("", "没星"):
            n = 0
        if n not in (0, 1, 2, 3):
            raise ValueError(f"重要程度填 ☆☆☆ / ★☆☆ / ★★☆ / ★★★：{v}")
        return n
    if key == "every":
        if v in ("", "默认"):
            return ""
        if v == "永不":
            return "never"
        if not v.rstrip("天").strip().isdigit():
            raise ValueError(f"提醒间隔填 默认 / 永不 / 天数：{v}")
        return v.rstrip("天").strip()
    return v


def apply(db, changed: list, dry: bool = False) -> dict:
    """changed：[{id, key, old, new}]，表里改过的格子（old 是导出时的原值）。逐格写进卡；返回 {写了, 没写成: [原因]}。"""
    from . import relations
    got = {"written": 0, "skipped": [], "people": set()}
    for c in changed:
        key = c.get("key")
        if key not in EDIT or c.get("new") == c.get("old"):
            continue
        name = c.get("name") or c.get("id")
        try:
            pid = int(c["id"])
            v = _value(key, c.get("new"))
        except (ValueError, TypeError, KeyError) as e:
            got["skipped"].append(f"{name}：{e}")
            continue
        if db.conn.execute("SELECT 1 FROM people WHERE id = ?", (pid,)).fetchone() is None:
            got["skipped"].append(f"{name}：这张卡不在了（合并或删掉了）")
            continue
        if dry:
            got["written"] += 1
            got["people"].add(pid)
            continue
        r = relations.set_field(db, pid, FIELD.get(key, key), v)
        if r.get("ok"):
            got["written"] += 1
            got["people"].add(pid)
        else:
            got["skipped"].append(f"{name}：{r.get('msg')}")
    got["people"] = len(got["people"])
    return got


def main():
    parser = argparse.ArgumentParser(description="人物卡整表导出、填好导回")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--export", type=Path)
    parser.add_argument("--apply", type=Path)
    parser.add_argument("--dry", action="store_true", help="只看会改多少，不写")
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        if args.export:
            args.export.write_text(json.dumps(rows(store.db), ensure_ascii=False), encoding="utf-8")
            print(f"导出 {len(rows(store.db))} 张卡 → {args.export}")
        if args.apply:
            print(json.dumps(apply(store.db, json.loads(args.apply.read_text(encoding="utf-8")), dry=args.dry), ensure_ascii=False, indent=1))
    finally:
        store.close()


if __name__ == "__main__":
    main()
