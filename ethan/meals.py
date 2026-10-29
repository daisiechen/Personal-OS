"""每天吃什么（设计第 3 节饮食）：从空气炸锅菜谱里由代码选三餐和加餐，凑到热量目标和蛋白质目标。

规则：蛋白质不少于目标、尽量不超过目标 35 克；加餐越少越好；总热量在目标 −150 到 +100 千卡之间；和昨天的午餐、晚餐不重复；
满足条件的组合里按日期轮换。没有组合满足时，取蛋白质够、热量最接近的，并如实说明。
"""

import hashlib
import json
import re
from datetime import date, timedelta
from itertools import combinations, product
from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[1] / "playbooks" / "food" / "recipes.json"
KCAL_BELOW, KCAL_ABOVE = 150, 100
PROTEIN_ABOVE = 35


def load() -> dict:
    data = json.loads(PLAYBOOK.read_text(encoding="utf-8"))
    table = data["ingredients"]
    recipes = []
    for r in data["recipes"]:
        kcal = sum(table[k][0] * g / 100 for k, g in r["items"].items())
        protein = sum(table[k][1] * g / 100 for k, g in r["items"].items())
        recipes.append({**r, "kcal": round(kcal), "protein": round(protein, 1)})
    return {r["id"]: r for r in recipes}


def _combos(recipes: dict):
    by = {}
    for r in recipes.values():
        by.setdefault(r["meal"], []).append(r)
    snacks = [()] + [(s,) for s in by["加餐"]] + list(combinations(by["加餐"], 2))
    for b, l, d in product(by["早餐"], by["午餐"], by["晚餐"]):
        for s in snacks:
            yield (b, l, d, *s)


def pick(kcal: int, protein: int, day: date, avoid=()) -> dict:
    """返回 {"meals": [菜谱...], "kcal", "protein", "note"}。avoid：昨天的菜谱编号。"""
    recipes = load()
    ok, close = [], []
    for combo in _combos(recipes):
        if any(r["id"] in avoid and r["meal"] in ("午餐", "晚餐") for r in combo):
            continue
        k = sum(r["kcal"] for r in combo)
        p = sum(r["protein"] for r in combo)
        if p >= protein and kcal - KCAL_BELOW <= k <= kcal + KCAL_ABOVE:
            ok.append((combo, k, p))
        elif p >= protein - 10:
            close.append((abs(k - kcal), combo, k, p))
    if ok:
        # 越简单越好：先挑加餐最少的，再挑蛋白质不超过目标太多的（够了就行，不必吃到 140 克）
        fewest = min(len(c) for c, _, _ in ok)
        ok = [x for x in ok if len(x[0]) == fewest]
        modest = [x for x in ok if x[2] <= protein + PROTEIN_ABOVE]
        ok = modest or ok
        ok.sort(key=lambda x: [r["id"] for r in x[0]])
        n = int(hashlib.sha256(day.isoformat().encode()).hexdigest(), 16) % len(ok)
        combo, k, p = ok[n]
        note = ""
    else:
        _, combo, k, p = min(close, key=lambda x: x[0])
        note = f"菜谱库里凑不出刚好的组合，今天约 {k} 千卡、蛋白质 {p:g} 克，最接近目标"
    return {"meals": list(combo), "kcal": k, "protein": round(p, 1), "note": note}


def yesterday_ids(db, day: date) -> list:
    row = db.conn.execute("SELECT brief FROM daily_plans WHERE day = ? ORDER BY version DESC LIMIT 1",
                          ((day - timedelta(days=1)).isoformat(),)).fetchone()
    if not row:
        return []
    return [m["id"] for m in json.loads(row["brief"]).get("meals", {}).get("items", [])]


def describe(menu: dict) -> str:
    lines = [f"今天约 {menu['kcal']} 千卡，蛋白质 {menu['protein']:g} 克"]
    if menu["note"]:
        lines.append(menu["note"])
    for r in menu["meals"]:
        items = "、".join(f"{k} {g} 克" for k, g in r["items"].items())
        lines += [f"{r['meal']}：{r['name']}（约 {r['kcal']} 千卡，蛋白质 {r['protein']:g} 克，{r['minutes']} 分钟）",
                  f"   用料：{items}", f"   做法：{r['steps']}"]
    return "\n".join(lines)


def summary(menu: dict) -> dict:
    return {"kcal": menu["kcal"], "protein": menu["protein"], "note": menu["note"],
            "items": [{"id": r["id"], "meal": r["meal"], "name": r["name"], "kcal": r["kcal"],
                       "protein": r["protein"]} for r in menu["meals"]]}


MEAL_LINE = re.compile(r"^(\S+?)：(.+?)（约 (\d+) 千卡，蛋白质 ([\d.]+) 克，(\d+) 分钟）$")


def parse_text(text: str):
    """把 describe() 写的三餐文字拆回结构，给工作台排版用（2026-10-28 她嫌一坨）；拆不出就返回 None，页面照原文显示。"""
    if not text:
        return None
    lines = [l.rstrip() for l in text.splitlines() if l.strip()]
    head = re.match(r"今天约 (\d+) 千卡，蛋白质 ([\d.]+) 克", lines[0]) if lines else None
    if not head:
        return None
    out = {"kcal": int(head.group(1)), "protein": head.group(2), "note": "", "meals": []}
    for line in lines[1:]:
        m = MEAL_LINE.match(line.strip())
        if m:
            out["meals"].append({"meal": m.group(1), "name": m.group(2), "kcal": int(m.group(3)), "protein": m.group(4),
                                 "minutes": int(m.group(5)), "items": [], "steps": []})
        elif line.strip().startswith("用料：") and out["meals"]:
            out["meals"][-1]["items"] = [x.strip() for x in line.strip()[3:].split("、") if x.strip()]
        elif line.strip().startswith("做法：") and out["meals"]:
            steps = re.split(r"(?<=[。；;])|\s*\d+[.、)）]\s*", line.strip()[3:])
            out["meals"][-1]["steps"] = [x.strip(" ；;。") for x in steps if x and x.strip(" ；;。")]
        elif not out["meals"]:
            out["note"] = (out["note"] + " " + line.strip()).strip()
    return out if out["meals"] else None
