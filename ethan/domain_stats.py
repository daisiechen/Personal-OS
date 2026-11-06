"""七个领域页的分析。

每个领域页上面一块“这个领域”：
- 分数：七项属性（ethan/attributes.py）现在的分数、开季的分数、由哪几项组成（各自实际、标准、得分），以及每天的分数走势；
- 关键指标的变化图：按领域挑 1–2 个最能说明问题的数，看最近 8 周（工作时长看最近 14 天）。
财务另有每日开销（她指定）：默认最近 7 天，能选起止日期，给区间总额和保底平均线。
数字都由代码算，页面只画。
"""

from datetime import date, timedelta

from . import attributes, lifelog, quests, worktime

WEEKS = 8


def score_block(db, key: str, season_start: date, day: date) -> dict:
    now = {r["key"]: r for r in attributes.compute(db, day, trend=False)}[key]
    attributes.history(db)
    hist = [{"day": r["day"], "v": round(r["score"])} for r in db.conn.execute(
        "SELECT day, score FROM attribute_daily WHERE key = ? AND day >= ? AND score IS NOT NULL ORDER BY day",
        (key, season_start.isoformat()))]
    start = hist[0]["v"] if hist else None
    return {"now": None if now["score"] is None else round(now["score"]), "start": start, "label": now["label"],
            "items": [{"name": i["name"], "actual": i["actual"], "standard": i["standard"], "weight": i["weight"],
                       "score": None if i["score"] is None else round(i["score"])} for i in now["items"]],
            "history": hist}


def _weeks(day: date, n: int = WEEKS) -> list:
    monday = day - timedelta(days=day.weekday())
    return [(monday - timedelta(weeks=i), min(monday - timedelta(weeks=i) + timedelta(days=6), day)) for i in range(n - 1, -1, -1)]


def _habit_weekly(db, title: str, day: date) -> list:
    h = quests.habit_by_title(db, title)
    if not h:
        return []
    return [{"x": a.strftime("%m-%d"), "v": db.conn.execute("SELECT COUNT(DISTINCT day) FROM habit_log WHERE habit_id = ? AND day >= ? AND day <= ?",
                                                            (h["id"], a.isoformat(), b.isoformat())).fetchone()[0]} for a, b in _weeks(day)]


def _log_weekly(db, kind: str, day: date, total: bool = False, scale: float = 1) -> list:
    lifelog.ensure(db)
    out = []
    for a, b in _weeks(day):
        v = lifelog.total(db, kind, a, b) if total else lifelog.count(db, kind, a, b, distinct_days=True)
        out.append({"x": a.strftime("%m-%d"), "v": round((v or 0) * scale, 1)})
    return out


def _chart(title, unit, points, target=None, kind="bar", note=""):
    return {"title": title, "unit": unit, "points": points, "target": target, "kind": kind, "note": note,
            "empty": not points or not any(p["v"] for p in points)}


def charts(db, key: str, day: date) -> list:
    """每个领域挑最能说明问题的 1–2 个数。"""
    if key == "body":
        return [_chart("每周训练", "次", _habit_weekly(db, "训练", day), 5),
                _chart("每周 0 点前睡", "天", _habit_weekly(db, "0 点前睡", day), 6)]
    if key == "career":
        days = [day - timedelta(days=n) for n in range(13, -1, -1)]
        return [_chart("每天工作时长", "小时", [{"x": d.strftime("%m-%d"), "v": worktime.hours(db, d)} for d in days], 8, "line",
                       "超过 8 小时的天标橙；一周不超过 45")]
    if key == "study":
        return [_chart("每周英语陪练", "次", _habit_weekly(db, "英语陪练（Grok）", day), 5),
                _chart("每周认知拓展", "天", _cognitive_weekly(db, day), 3, note="聊到新话题或 小伴 教了一个知识点")]
    if key == "social":
        return [_chart("每周见朋友", "次", _log_weekly(db, "meet", day), 1)]
    if key == "academic":
        return [_chart("每周研究时间", "小时", _log_weekly(db, "research", day, total=True, scale=1 / 60), 2)]
    if key == "life":
        return [_chart("每周新体验", "次", _habit_weekly(db, "每周 1 次新体验", day), 1),
                _chart("每周出门天数", "天", _habit_weekly(db, "每天出门", day), 7)]
    return []


def _cognitive_weekly(db, day: date) -> list:
    from .territory import cognitive_days
    return [{"x": a.strftime("%m-%d"), "v": cognitive_days(db, a, b) or 0} for a, b in _weeks(day)]


def daily_spend(db, start: date, end: date) -> dict:
    """财务：start–end 每天花了多少（按预算币种折算）、区间总额、保底平均线（月预算按天折）。的。"""
    from . import finance, territory
    import calendar
    if end < start:
        start, end = end, start
    end = min(end, start + timedelta(days=370))
    budget = territory.settings_budget(db, end)
    cur, cap = next(iter(budget.items())) if budget else ("GBP", None)
    rate = finance.fx_rate(db)
    points, total, d = [], 0.0, start
    while d <= end:
        v = round(finance.convert(finance.spend_between(db, d, d), cur, rate))
        total += v
        points.append({"x": d.isoformat(), "v": v})
        d += timedelta(days=1)
    per_day = round(cap / calendar.monthrange(end.year, end.month)[1]) if cap else None
    return {"cur": "£" if cur == "GBP" else "¥", "start": start.isoformat(), "end": end.isoformat(), "points": points,
            "total": round(total), "floor_avg": per_day, "avg": round(total / len(points)) if points else 0}
