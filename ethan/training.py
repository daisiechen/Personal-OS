"""训练处方（设计第 3 节）：按模板定今天练哪些动作，按她记下的每组重量和次数算下次用多重。

全部由代码算，模型不改动作和重量：
- 双重递增：上次所有组都做到次数上限 → 这次加 increment；连续两次有组没做到下限 → 重量降 10%；否则不变。
- 没有记录的动作：让她选一个能标准做到上限、还能再做 2 次的重量，练完报给 小伴。
- 疼痛规则：上次这个动作疼痛超过 3 分（满分 10），换成动作库里更容易的替代。
她练完说给 小伴，小伴 记成 [训练 动作 重量x次数/次数/次数 疼痛N]。
"""

import json
import re
from datetime import date
from pathlib import Path

PLAYBOOK = Path(__file__).resolve().parents[1] / "playbooks" / "body"
PAIN_LIMIT = 3
MARK = re.compile(r"\[训练\s+(\S+)\s+([\d.]+)\s*[x×*X]\s*([\d/、,，\s]+?)(?:\s*疼痛\s*(\d+))?\s*\]")


def load():
    lib = json.loads((PLAYBOOK / "exercises.json").read_text(encoding="utf-8"))["exercises"]
    tpl = json.loads((PLAYBOOK / "template.json").read_text(encoding="utf-8"))
    return lib, tpl


def _round(weight: float, step: float) -> float:
    step = abs(step) or 1
    return round(round(weight / step) * step, 1)


def _sessions(db, ex_id: str, limit: int = 2) -> list:
    """最近几次训练这个动作的记录，新的在前：[{"day", "weight", "reps": [...], "pain"}]"""
    days = [r["day"] for r in db.conn.execute(
        "SELECT DISTINCT day FROM workout_sets WHERE exercise = ? ORDER BY day DESC LIMIT ?", (ex_id, limit))]
    out = []
    for d in days:
        rows = db.conn.execute("SELECT weight, reps, pain FROM workout_sets WHERE exercise = ? AND day = ? "
                               "ORDER BY set_no", (ex_id, d)).fetchall()
        out.append({"day": d, "weight": rows[0]["weight"], "reps": [r["reps"] for r in rows],
                    "pain": max((r["pain"] or 0) for r in rows)})
    return out


def next_weight(sessions: list, low: int, high: int, increment: float):
    """返回 (重量, 说明)。重量 None 表示还没有记录。"""
    if not sessions:
        return None, f"第一次：选一个能标准做到 {high} 次、还能再做 2 次的重量，练完告诉 小伴"
    last = sessions[0]
    w = last["weight"]
    if increment == 0:
        return w, ("上次每组都做满了，这次每组多做 1–2 次或多停 5 秒" if min(last["reps"]) >= high
                   else "保持，争取每组做满")
    if min(last["reps"]) >= high:
        return _round(w + increment, increment), f"上次每组都做满 {high} 次，加 {abs(increment):g} kg"
    missed = [s for s in sessions[:2] if min(s["reps"]) < low]
    if len(sessions) >= 2 and len(missed) == 2:
        return _round(w * 0.9, increment), f"连续两次有组没做到 {low} 次，降 10%"
    return w, "保持这个重量，争取每组做到上限"


def prescribe(db, day: date) -> dict:
    """今天的训练；周末返回 None。"""
    lib, tpl = load()
    plan = tpl["days"].get(str(day.weekday()))
    if not plan:
        return None
    out = []
    for item in plan["exercises"]:
        ex_id, note = item["id"], ""
        sessions = _sessions(db, ex_id)
        if sessions and sessions[0]["pain"] > PAIN_LIMIT and lib[ex_id].get("easier"):
            note = f"上次{lib[ex_id]['name']}疼痛 {sessions[0]['pain']} 分，今天换成{lib[lib[ex_id]['easier']]['name']}。"
            ex_id = lib[ex_id]["easier"]
            sessions = _sessions(db, ex_id)
        ex = lib[ex_id]
        low, high = item["reps"]
        weight, why = next_weight(sessions, low, high, ex["increment"]) if ex["increment"] or sessions else (None, "")
        out.append({"id": ex_id, "name": ex["name"], "sets": item["sets"], "reps": [low, high],
                    "unit": item.get("unit", "次"), "per_side": item.get("per_side", False),
                    "weight": weight, "note": (note + why).strip(), "cues": ex["cues"], "mistake": ex["mistake"]})
    return {"focus": plan["focus"], "warmup": tpl["warmup"], "exercises": out}


def describe(rx: dict) -> str:
    """写进日历备注和 Obsidian 的训练内容。"""
    lines = [f"{rx['focus']}", "", f"热身 {rx['warmup']['minutes']} 分钟：" + "；".join(rx["warmup"]["items"]), ""]
    for n, e in enumerate(rx["exercises"], 1):
        reps = f"{e['reps'][0]}–{e['reps'][1]}" if e["reps"][0] != e["reps"][1] else f"{e['reps'][0]}"
        side = "（每侧）" if e["per_side"] else ""
        weight = f"，{e['weight']:g} kg" if e.get("weight") else ""
        lines.append(f"{n}. {e['name']}：{e['sets']} 组 × {reps} {e['unit']}{side}{weight}")
        lines.append(f"   要点：{'；'.join(e['cues'])}。别：{e['mistake']}")
        if e["note"]:
            lines.append(f"   {e['note']}")
    lines += ["", "疼痛超过 3 分（满分 10）就停这个动作。练完把每组重量和次数告诉 小伴。"]
    return "\n".join(lines)


def record(db, text: str, day: date) -> list:
    """把 [训练 …] 标记写进 workout_sets；返回没记上的说明。"""
    lib, _ = load()
    by_name = {v["name"]: k for k, v in lib.items()}
    failed = []
    for name, weight, reps, pain in MARK.findall(text):
        ex_id = name if name in lib else by_name.get(name)
        counts = [int(x) for x in re.split(r"[/、,，\s]+", reps.strip()) if x]
        if ex_id is None or not counts:
            failed.append(f"训练记录“{name}”认不出是哪个动作")
            continue
        db.conn.execute("DELETE FROM workout_sets WHERE exercise = ? AND day = ?", (ex_id, day.isoformat()))
        for n, c in enumerate(counts, 1):
            db.conn.execute("INSERT INTO workout_sets (day, exercise, set_no, weight, reps, pain) VALUES (?, ?, ?, ?, ?, ?)",
                            (day.isoformat(), ex_id, n, float(weight), c, int(pain) if pain else 0))
    db.conn.commit()
    return failed
