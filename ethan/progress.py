"""目标进度：全部由代码从数据里算，模型只拿结果说话（设计第 0 节原则 1、第 5 节第一步）。

每个有量化指标的目标（goal_metrics 表）按 kind 算：
  weight_avg7  体重 7 天平均，对比从起点到截止日按直线应到的值（越低越好）
  count_week   每周次数，对比 target（如每周 1 次新体验）
  count_total  累计次数，对比 target（如年底前去 3 个地方）
  funnel       漏斗：spec 里按顺序列出各步和目标数，进度看卡在哪一步
  launch       上线日期：看离截止日还有几天，有没有记过“已上线”
目标是 paused 时进度显示“暂停”，不算落后。没有数据就说没有数据，不编数字。
"""

import json
from datetime import date, datetime, timedelta, timezone

STATUS_PAUSED = "暂停"
NO_DATA = "还没有数据"


def week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _day_start_utc(day: date) -> str:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc).isoformat(timespec="seconds")


def goal_progress(db, goal: dict, today: date) -> dict:
    """返回 {"goal_id", "title", "kind", "status", "text", ...数字}；status 是 领先/正常/落后/达成/暂停/还没有数据。"""
    m = db.metric(goal["id"])
    base = {"goal_id": goal["id"], "title": goal["title"], "due": goal.get("due"), "kind": m["kind"] if m else None}
    if goal.get("status") == "paused":  # 先看暂停：没有指标的暂停副本以前漏判成“没有数据”，还显示在副本进度里（2026-10-29）
        return {**base, "status": STATUS_PAUSED, "text": "暂停中，不算落后"}
    if not m:
        return {**base, "status": NO_DATA, "text": "没有量化指标"}
    fn = {"weight_avg7": _weight, "count_week": _count_week, "count_total": _count_total,
          "funnel": _funnel, "launch": _launch}[m["kind"]]
    return {**base, **fn(db, goal, m, today)}


def _weight(db, goal, m, today):
    rows = [r for r in db.body_measures((today - timedelta(days=6)).isoformat()) if r["weight"]]
    if not rows:
        return {"status": NO_DATA, "text": "最近 7 天没有体重记录"}
    avg = round(sum(r["weight"] for r in rows) / len(rows), 1)
    start = date.fromisoformat(m["start_date"]) if m["start_date"] else today
    due = date.fromisoformat(goal["due"]) if goal.get("due") else today
    span = max((due - start).days, 1)
    frac = min(max((today - start).days / span, 0), 1)
    expected = round(m["baseline"] + (m["target"] - m["baseline"]) * frac, 1)
    gap = round(avg - expected, 1)
    if avg <= m["target"]:
        status = "达成"
    elif gap <= -0.2:
        status = "领先"
    elif gap < 0.3:
        status = "正常"
    else:
        status = "落后"
    return {"status": status, "current": avg, "expected": expected, "target": m["target"], "baseline": m["baseline"],
            "samples": len(rows),
            "text": f"7 天平均 {avg} kg（{len(rows)} 次记录），按计划今天应到 {expected} kg，目标 {m['target']} kg"}


def _count_week(db, goal, m, today):
    monday = week_start(today)
    n = sum(e["value"] for e in db.metric_entries(goal["id"], since=_day_start_utc(monday)))
    target = m["target"] or 1
    days_left = 6 - today.weekday()
    status = "达成" if n >= target else ("正常" if days_left >= 2 else "落后")
    return {"status": status, "current": n, "target": target,
            "text": f"本周 {n:g}/{target:g} 次，本周还剩 {days_left} 天"}


def _count_total(db, goal, m, today):
    since = _day_start_utc(date.fromisoformat(m["start_date"])) if m["start_date"] else None
    n = sum(e["value"] for e in db.metric_entries(goal["id"], since=since))
    target = m["target"] or 1
    return {"status": "达成" if n >= target else "正常", "current": n, "target": target,
            "text": f"已完成 {n:g}/{target:g}"}


def _funnel(db, goal, m, today):
    steps = json.loads(m["spec"] or "{}").get("steps", [])
    counts = {}
    for e in db.metric_entries(goal["id"]):
        counts[e["step"]] = counts.get(e["step"], 0) + e["value"]
    parts, stuck = [], None
    for name, target in steps:
        n = counts.get(name, 0)
        parts.append(f"{name} {n:g}/{target}")
        if stuck is None and n < target:
            stuck = name
    last_name, last_target = steps[-1] if steps else ("成交", 1)
    done = counts.get(last_name, 0) >= last_target
    return {"status": "达成" if done else "正常", "steps": parts, "stuck_at": stuck,
            "text": "；".join(parts) + (f"。现在卡在：{stuck}" if stuck and not done else "")}


def _launch(db, goal, m, today):
    if not goal.get("due"):
        return {"status": NO_DATA, "text": "没有上线日期"}
    left = (date.fromisoformat(goal["due"]) - today).days
    launched = any("上线" in (e["note"] or "") for e in db.metric_entries(goal["id"]))
    status = "达成" if launched else ("落后" if left < 0 else "正常")
    return {"status": status, "days_left": left,
            "text": "已上线" if launched else f"计划 {goal['due']} 上线，还有 {left} 天"}


def year_progress(db, today: date) -> list:
    """所有进行中或暂停的“今年”目标的进度，先按她排的顺序，没排的按截止日。"""
    rows = db.conn.execute(
        "SELECT * FROM goals WHERE level = 'year' AND status IN ('active', 'paused', 'not_started') "
        "ORDER BY sort IS NULL, sort, due IS NULL, due, id").fetchall()
    return [goal_progress(db, dict(r), today) for r in rows]
