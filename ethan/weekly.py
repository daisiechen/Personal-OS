"""每周固定出的下周安排：下周 7 天的餐单和采购清单、下周 5 次训练。

以前 小伴 在聊天里“答应”帮她排，进了“小伴 帮我办”当成一次性的活；她说这是长期固定的事，不该由他答应。
现在由代码每周日凌晨（每晚整理那一轮）算好，存进 week_plans；工作台“三餐”“训练详情”里能看整周，周日傍晚 小伴 说一声。
- 餐单：每天的三餐和平时同一套算法（ethan/meals.py），连算 7 天、相邻两天午晚餐不重复；食材合并成采购清单。
  每 3 天备一次蛋白质主菜（她的待办“每 3 天备一次蛋白质主菜和加餐”），标在第 1、4、7 天。
- 训练：按训练模板（ethan/training.py）周一到周五各一次，时间地点和每天计划一样；每天早上计划器会再按当天日程核一次。
"""

import json
from datetime import date, datetime, timedelta, timezone

from . import meals, training
from . import clock

SCHEMA = "CREATE TABLE IF NOT EXISTS week_plans (week TEXT PRIMARY KEY, plan TEXT NOT NULL, created_at TEXT NOT NULL, told INTEGER NOT NULL DEFAULT 0)"
PREP_DAYS = (0, 3, 6)
WD = "一二三四五六日"


def ensure(db):
    db.conn.execute(SCHEMA)


def next_monday(today: date) -> date:
    return clock.week_start(today) + timedelta(days=7)


def build(db, monday: date) -> dict:
    from .planner import TRAINING_END, TRAINING_PLACE, TRAINING_START, nutrition, weekly_adjust
    weights = [r["weight"] for r in db.body_measures((monday - timedelta(days=8)).isoformat()) if r["weight"]]
    weight = round(sum(weights) / len(weights), 1) if weights else 60.0
    adj = weekly_adjust(db, monday)
    food = nutrition(weight, monday, adj["offset"])
    days, shop, avoid = [], {}, meals.yesterday_ids(db, monday)
    for i in range(7):
        d = monday + timedelta(days=i)
        menu = meals.pick(food["kcal"], food["protein_g"], d, avoid=avoid)
        avoid = [r["id"] for r in menu["meals"]]
        for r in menu["meals"]:
            for k, g in r["items"].items():
                shop[k] = shop.get(k, 0) + g
        days.append({"day": d.isoformat(), "weekday": WD[i], "prep": i in PREP_DAYS, **meals.summary(menu)})
    sessions = []
    for i in range(7):
        d = monday + timedelta(days=i)
        rx = training.prescribe(db, d)
        if rx:
            sessions.append({"day": d.isoformat(), "weekday": WD[i], "start": TRAINING_START, "end": TRAINING_END, "place": TRAINING_PLACE,
                             "focus": rx["focus"], "exercises": [f"{x['name']} {x['sets']}×{x['reps'][0]}–{x['reps'][1]}{x['unit']}"
                                                                 + (f" {x['weight']:g}kg" if x.get("weight") else "") for x in rx["exercises"]]})
    return {"week": monday.isoformat(), "kcal": food["kcal"], "protein": food["protein_g"], "days": days,
            "shopping": [{"item": k, "grams": round(g)} for k, g in sorted(shop.items(), key=lambda x: -x[1])], "training": sessions}


def make(db, today: date, force: bool = False) -> dict:
    """算下周的（周日那一轮跑；别的天也能补）。已经有了就不重算，除非 force。"""
    ensure(db)
    monday = next_monday(today)
    if not force and db.conn.execute("SELECT 1 FROM week_plans WHERE week = ?", (monday.isoformat(),)).fetchone():
        return {"week": monday.isoformat(), "made": False}
    plan = build(db, monday)
    # 训练前称体重只在训练日要打卡（今天不是训练日就不该有这个任务）：跟着下周排的训练日走
    days = ",".join(str(date.fromisoformat(x["day"]).weekday()) for x in plan["training"])
    if days:
        db.conn.execute("UPDATE habits SET weekdays = ? WHERE title = '训练前称体重'", (days,))
    db.conn.execute("INSERT OR REPLACE INTO week_plans (week, plan, created_at, told) VALUES (?, ?, ?, 0)",
                    (monday.isoformat(), json.dumps(plan, ensure_ascii=False), datetime.now(timezone.utc).isoformat(timespec="seconds")))
    db.conn.commit()
    return {"week": monday.isoformat(), "made": True}


def get(db, today: date) -> dict:
    """工作台看：这周的和下周的。这周的还没有（刚上线）就当场算一份；周末就把下周的也算好。"""
    ensure(db)
    this = clock.week_start(today)
    if not db.conn.execute("SELECT 1 FROM week_plans WHERE week = ?", (this.isoformat(),)).fetchone():
        make(db, this - timedelta(days=1))
    if today.weekday() >= 5:
        make(db, today)
    out = {}
    for key, monday in (("this", this), ("next", this + timedelta(days=7))):
        r = db.conn.execute("SELECT plan FROM week_plans WHERE week = ?", (monday.isoformat(),)).fetchone()
        out[key] = json.loads(r["plan"]) if r else None
    return out


def to_tell(db, today: date):
    """周日傍晚 小伴 说一声下周的好了；返回 (状态键, 场合说明) 或 None。"""
    ensure(db)
    if today.weekday() != 6:
        return None
    monday = next_monday(today).isoformat()
    r = db.conn.execute("SELECT plan FROM week_plans WHERE week = ?", (monday,)).fetchone()
    if not r:
        return None
    plan = json.loads(r["plan"])
    top = "、".join(x["item"] for x in plan["shopping"][:6])
    return (f"week-plan:{monday}", f"下周的餐单、采购清单（{len(plan['shopping'])} 样，最多的是 {top}）和 {len(plan['training'])} 次训练已经排好了，"
                                   "在工作台“三餐”“训练详情”里点“看一周”。用一两句告诉她，问她要不要明天一起去采购。不要列全部清单。")

