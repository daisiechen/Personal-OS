"""七项属性：她这个角色在各领域的健康度（docs/design/attributes.md，算法）。

每项属性由几个小项组成，每个小项按“达到她自己的标准”算 0–100，按权重加起来。
- 看最近 30 天的真实记录；没有数据来源的小项不算（剩下的按比例重新分权重），全都没有就是“未解锁”（None）。
- 70 分以上良好、40–69 一般、40 以下受损。
- 涨跌：和 30 天前同样算法的分数比，30 天前那一整段都有记录才给。
- 系统开始记录不满 30 天时，按已有的天数折算标准（比如才记了 9 天，训练标准就是 17×9/30 天）。
- 每晚存一份当天的快照（attribute_daily），以后“我”页的回看条按天取。
页面怎么展示她还在想（“卡册和第二大脑的前端先不动”），这里只管算。
运行：python3 -m ethan.attributes --data <数据目录> [--day 2026-10-30]
"""

import argparse
import json
from datetime import date, timedelta
from pathlib import Path

from . import standards
from . import lifelog, places_log, territory, worktime
from .clock import her_today

WINDOW = 30
NAMES = {k: n for k, n, _ in territory.TERRITORIES}
ORDER = ["body", "study", "life", "career", "money", "social", "academic"]
SNAP_SQL = """CREATE TABLE IF NOT EXISTS attribute_daily (
    day TEXT NOT NULL, key TEXT NOT NULL, score REAL, items TEXT NOT NULL, PRIMARY KEY (day, key))"""


def label(score):
    if score is None:
        return "未解锁"
    return "良好" if score >= 70 else "一般" if score >= 40 else "受损"


def _ratio(actual, full) -> float:
    return round(max(0.0, min(1.0, actual / full)) * 100, 1) if full else 0.0


def _k(start, day) -> float:
    """这段实际有多少天（系统 10 月 22 日才开始记，头一个月按已有的天数折算标准，不因为还没满 30 天就判受损）。"""
    return ((day - start).days + 1) / WINDOW


def _item(name, weight, score, actual, standard):
    return {"name": name, "weight": weight, "score": score, "actual": actual, "standard": standard}


def _habit(db, title, start, day):
    return territory._habit_days(db, title, start, day)


def _ontime(db, key, start, day):
    """这块领地里截止日落在这 30 天的待办，按时做完的比例；没有就 None。"""
    rows = db.conn.execute("SELECT title, due, done, done_at FROM tasks WHERE due >= ? AND due <= ?",
                           (start.isoformat(), day.isoformat())).fetchall()
    mine = [r for r in rows if territory.guess(r["title"]) == key]
    if not mine:
        return None, "这段时间没有带截止日的事"
    ok = sum(1 for r in mine if r["done"] and (r["done_at"] or "")[:10] <= r["due"])
    return _ratio(ok, len(mine)), f"{ok}/{len(mine)} 件按时"


# ---------- 各项 ----------
def body(db, start, day):
    out = []
    # 满分 = 领域标准表的目标（ethan/standards.py，她的决定 8：领地灯和属性分按同一张表）
    n = _habit(db, "训练", start, day)
    out.append(_item("训练", 30, None if n is None else _ratio(n, standards.full("training", start, day)), f"{n} 天" if n is not None else "没有记录", standards.window_label("training")))
    n = _habit(db, "0 点前睡", start, day)
    out.append(_item("睡眠", 25, None if n is None else _ratio(n, standards.full("sleep", start, day)), f"{n} 天零点前睡" if n is not None else "没有记录", standards.window_label("sleep")))
    n = _habit(db, "吃够蛋白质", start, day)
    out.append(_item("饮食", 20, None if n is None else _ratio(n, standards.full("protein", start, day)), f"{n} 天吃够蛋白质" if n is not None else "没有记录", standards.window_label("protein")))
    w = territory._weights(db, day, WINDOW)
    if len(w) >= 2 and date.fromisoformat(w[0]["day"]) <= day - timedelta(days=14):
        change = round(w[-1]["avg"] - w[0]["avg"], 2)
        span = (date.fromisoformat(w[-1]["day"]) - date.fromisoformat(w[0]["day"])).days or 1
        monthly = change * WINDOW / span
        losing = db.conn.execute("SELECT 1 FROM goal_metrics m JOIN goals g ON g.id = m.goal_id "
                                 "WHERE m.kind = 'weight_avg7' AND g.status = 'active'").fetchone()
        if losing:
            score = 100.0 if -2.8 <= monthly <= -1.2 else 50.0 if monthly < 0 or monthly == 0 else 0.0
            if monthly < -2.8:
                score = 70.0  # 掉太快也不好
        else:
            score = 100.0 if abs(monthly) <= 1 else 50.0
        out.append(_item("体重走势", 25, score, f"30 天{'降' if change <= 0 else '涨'} {abs(change)} kg", "减脂期一个月降 1.2–2.8 kg"))
    else:
        out.append(_item("体重走势", 25, None, "记录不满两周", "减脂期一个月降 1.2–2.8 kg"))
    return out


def study(db, start, day):
    from . import english  # 和健康线、学习页的图同一个口径：有练习记录的天 ∪ 打卡的天
    days = english.practice_days(db, start, day)
    n = None if days is None else len(days)
    out = [_item("英语陪练", 50, None if n is None else _ratio(n, standards.full("english", start, day)), f"{n} 次" if n is not None else "没有开", standards.window_label("english"))]
    n = territory.cognitive_days(db, start, day)
    if n is not None:
        out.append(_item("认知拓展", 30, _ratio(n, standards.full("cognitive", start, day)), f"{n} 天聊到新话题或学了知识点", standards.window_label("cognitive")))
    else:
        out.append(_item("认知拓展", 30, None, "还没有记录", standards.window_label("cognitive")))
    if lifelog.has(db, "course"):
        n = lifelog.count(db, "course", start, day)
        out.append(_item("课程和读书", 20, _ratio(n, standards.full("course", start, day)), f"{n} 次", standards.window_label("course")))
    else:
        out.append(_item("课程和读书", 20, None, "还没有记录来源", standards.window_label("course")))
    return out


def life(db, start, day):
    n = _habit(db, "每周 1 次新体验", start, day)
    out = [_item("新体验", 50, None if n is None else _ratio(n, standards.full("new_exp", start, day)), f"{n} 次" if n is not None else "没有记录", standards.window_label("new_exp"))]
    if places_log.has_data(db):
        n = places_log.non_work_outings(db, start, day, territory._training_leaves(db, start, day))
        out.append(_item("出门做工作以外的事", 25, _ratio(n, standards.full("outing", start, day)), f"{n} 次", standards.window_label("outing") + "，按手机定位"))
    else:
        out.append(_item("出门做工作以外的事", 25, None, "等手机定位", standards.window_label("outing")))
    if db.conn.execute("SELECT 1 FROM wishes LIMIT 1").fetchone():
        n = db.conn.execute("SELECT COUNT(*) FROM wishes WHERE status = 'done' AND done_at >= ?",
                            ((day - timedelta(days=90)).isoformat(),)).fetchone()[0]
        out.append(_item("实现心愿", 25, 100.0 if n else 0.0, f"90 天实现 {n} 个", "90 天至少 1 个"))
    else:
        out.append(_item("实现心愿", 25, None, "还没有心愿", "90 天至少 1 个"))
    return out


def _goal_pace(db, goal, m, start, day):
    """事业目标这 30 天推进了多少：实际增加 ÷ 按截止日均摊到 30 天应增加的。"""
    entries = db.metric_entries(goal["id"])
    if m["kind"] == "funnel":
        steps = json.loads(m["spec"] or "{}").get("steps", [])
        before = {}
        for e in entries:
            if (e["at"] or "")[:10] < start.isoformat():
                before[e["step"]] = before.get(e["step"], 0) + e["value"]
        stuck = next(((s, t) for s, t in steps if before.get(s, 0) < t), None)
        if not stuck:
            return None
        name, target = stuck
        gained = sum(e["value"] for e in entries if e["step"] == name and (e["at"] or "")[:10] >= start.isoformat())
        remaining = target - before.get(name, 0)
    elif m["kind"] == "count_total":
        done_before = sum(e["value"] for e in entries if (e["at"] or "")[:10] < start.isoformat())
        gained = sum(e["value"] for e in entries if (e["at"] or "")[:10] >= start.isoformat())
        remaining = (m["target"] or 1) - done_before
        name = goal["title"]
    else:
        return None
    if remaining <= 0:
        return 100.0, f"{name} 已达成"
    days_left = max((date.fromisoformat(goal["due"]) - start).days, WINDOW) if goal.get("due") else 90
    expected = remaining * ((day - start).days + 1) / days_left
    return _ratio(gained, expected), f"{name} +{gained:g}（应 +{expected:.1f}）"


def _milestone_pace(db, goal, day):
    """没有量化指标的事业目标按里程碑是否按期算（“你没有指标，那么这个领域的整体数值你是怎么算的呢？……
    至少给指定一个指标，哪怕你是把里程碑按进度算呢？”）：到今天该完成的里程碑（截止日 ≤ 今天）完成了几个；
    一个都还没到期就算按期（100）。没拆里程碑的不算。"""
    try:
        ms = [dict(r) for r in db.conn.execute("SELECT title, due, status FROM milestones WHERE goal_id = ? ORDER BY seq, id", (goal["id"],))]
    except Exception:
        return None
    if not ms:
        return None
    d = day.isoformat()
    due = [m for m in ms if m["due"] and m["due"] <= d]
    done = [m for m in due if m["status"] == "done"]
    late = [m for m in due if m["status"] != "done"]
    nxt = next((m for m in ms if m["status"] != "done"), None)
    name = goal["title"]
    if not due:
        return 100.0, f"{name} 里程碑按期（还没有到期的" + (f"，下一个 {nxt['due'][5:]} 到期）" if nxt and nxt["due"] else "）")
    return _ratio(len(done), len(due)), (f"{name} 到期的里程碑完成 {len(done)}/{len(due)}"
                                        + (f"，过期没做完：{late[0]['title'][:16]}" if late else ""))


def career(db, start, day):
    paces = []
    for g in db.conn.execute("SELECT * FROM goals WHERE status = 'active' AND domain = '事业'"):
        m = db.metric(g["id"])
        p = _goal_pace(db, dict(g), m, start, day) if m else _milestone_pace(db, dict(g), day)
        if p:
            paces.append(p)
    out = [_item("主要目标推进", 50, round(sum(p[0] for p in paces) / len(paces), 1) if paces else None,
                 "；".join(p[1] for p in paces) or "事业目标还没有指标，也没拆里程碑",
                 "有量化指标的按截止日均摊到这 30 天；没有的看到期的里程碑完成了几个")]
    if worktime.has_data(db):
        days = [worktime.hours(db, start + timedelta(days=i)) for i in range((day - start).days + 1)]
        worked = [h for h in days if h > 0]
        ok = sum(1 for h in worked if h <= 8)
        out.append(_item("工作时长健康", 30, _ratio(ok, len(worked)) if worked else None, f"{ok}/{len(worked)} 天不超过 8 小时", "每天不超过 8 小时"))
    else:
        out.append(_item("工作时长健康", 30, None, "等 Mac 上的记录脚本", "每天不超过 8 小时"))
    works = lifelog.count(db, "book", start, day) + lifelog.count(db, "song", start, day) if (lifelog.has(db, "book") or lifelog.has(db, "song")) else None
    out.append(_item("作品和发帖", 20, None if works is None else _ratio(works, standards.full("works", start, day)), f"{works} 次进展" if works is not None else "账号还没接", standards.window_label("works") + "进展"))
    return out


def money(db, start, day):
    from . import finance
    out = []
    budget = territory.settings_budget(db, day)
    if budget and (finance.has_source(db, "monzo") or finance.has_source(db, "sms") or finance.has_source(db, "chat")):
        cur, cap = next(iter(budget.items()))
        total = finance.convert(finance.month_spend(db, day), cur, finance.fx_rate(db))
        paced_cap = cap * day.day / 30
        ratio = total / paced_cap if paced_cap else 0
        score = 100.0 if ratio <= 1 else max(0.0, round(100 - (ratio - 1) * 200, 1))
        sym = {"GBP": "£", "CNY": "¥", "EUR": "€"}.get(cur, cur + " ")
        # 写清花了多少、到今天该用多少预算
        out.append(_item("花销控制", 60, score, f"本月到今天花了 {sym}{total:,.2f}，按天算到今天的预算 {sym}{paced_cap:,.2f}（用了 {ratio:.0%}）",
                         "不超过按天算到今天的预算（每月预算 ÷ 30 × 今天几号）；每超 10% 扣 20 分"))
    else:
        out.append(_item("花销控制", 60, None, "没有预算或没有记账来源", "不超预算"))
    s, text = _ontime(db, "money", start, day)
    out.append(_item("账单按时", 20, s, text, "截止日前做完"))
    out.append(_item("存款", 20, None, "还没有账户余额", "—"))
    return out


def social(db, start, day):
    if not (lifelog.has(db, "meet") or lifelog.has(db, "new_friend") or lifelog.has(db, "contact")):
        return [_item("和朋友见面", 40, None, "还没有记录", standards.window_label("meet")), _item("联系重要的人", 35, None, "还没有记录", "按各自频率"),
                _item("认识新朋友", 25, None, "还没有记录", standards.window_label("new_friend"))]
    met = lifelog.count(db, "meet", start, day)
    new = lifelog.count(db, "new_friend", start, day)
    from . import relations  # “该联系谁”只用人物卡那套（5）
    cs = relations.contact_status(db, day, "me_page")
    people, due = cs["tracked"], cs["contact"]
    return [_item("和朋友见面", 40, _ratio(met, standards.full("meet", start, day)), f"{met} 次", standards.window_label("meet")),
            _item("联系重要的人", 35, _ratio(people - len(due), people) if people else None,
                  f"{people - len(due)}/{people} 人按时联系" if people else "人物卡里还没设联系频率", "按各自频率"),
            _item("认识新朋友", 25, _ratio(new, standards.full("new_friend", start, day)), f"{new} 个", standards.window_label("new_friend"))]


def academic(db, start, day):
    if lifelog.has(db, "research"):
        mins = lifelog.total(db, "research", start, day)
        out = [_item("研究时间", 60, _ratio(mins, standards.full("research", start, day)), f"{mins / 60:.1f} 小时", standards.window_label("research"))]
    else:
        out = [_item("研究时间", 60, None, "还没有记录", standards.window_label("research"))]
    s, text = _ontime(db, "academic", start, day)
    out.append(_item("学术事项推进", 40, s, text, "截止日前做完"))
    return out


PARTS = {"body": body, "study": study, "life": life, "career": career, "money": money, "social": social, "academic": academic}


def score_of(items: list):
    have = [i for i in items if i["score"] is not None]
    if not have:
        return None
    return round(sum(i["score"] * i["weight"] for i in have) / sum(i["weight"] for i in have), 1)


def _earliest(db):
    days = []
    for sql in ("SELECT MIN(day) FROM habit_log", "SELECT MIN(day) FROM body_measures", "SELECT MIN(day) FROM life_log",
                "SELECT MIN(day) FROM transactions"):
        try:
            v = db.conn.execute(sql).fetchone()[0]
        except Exception:
            v = None
        if v:
            days.append(v)
    return date.fromisoformat(min(days)[:10]) if days else None


def window(db, day: date) -> tuple:
    """算分看的那一段：最近 30 天；系统开始记录不满 30 天就从第一条记录算起。"""
    lifelog.ensure(db)
    start = day - timedelta(days=WINDOW - 1)
    first = _earliest(db)
    return (first if first and first > start else start), day


def compute(db, day: date, trend: bool = True) -> list:
    start, _ = window(db, day)
    first = _earliest(db)
    out = []
    for key in ORDER:
        items = PARTS[key](db, start, day)
        score = score_of(items)
        row = {"key": key, "name": NAMES[key], "score": score, "label": label(score), "items": items, "delta": None}
        if trend and score is not None and first and first <= day - timedelta(days=2 * WINDOW - 1):
            before = score_of(PARTS[key](db, start - timedelta(days=WINDOW), day - timedelta(days=WINDOW)))
            row["delta"] = None if before is None else round(score - before, 1)
        out.append(row)
    return out


def snapshot(db, day: date) -> int:
    """存当天的属性（回看用）；同一天重跑覆盖。"""
    db.conn.execute(SNAP_SQL)
    rows = compute(db, day, trend=False)
    db.conn.executemany("INSERT OR REPLACE INTO attribute_daily (day, key, score, items) VALUES (?, ?, ?, ?)",
                        [(day.isoformat(), r["key"], r["score"], json.dumps(r["items"], ensure_ascii=False)) for r in rows])
    db.conn.commit()
    return len(rows)


def history(db, key: str = None, since: date = None) -> list:
    db.conn.execute(SNAP_SQL)
    q, args = "SELECT day, key, score FROM attribute_daily WHERE 1=1", []
    if key:
        q += " AND key = ?"
        args.append(key)
    if since:
        q += " AND day >= ?"
        args.append(since.isoformat())
    return [dict(r) for r in db.conn.execute(q + " ORDER BY day, key", args)]


def trail(db, key: str, since: date, day: date, now=None) -> list:
    """一块领域从 since 到 day 每天的分数 [{"day", "v"}]：之前的日子用每晚快照，今天这一点现算。
    赛季页的分数柱（开季那条线）和领域页的分数、走势都从这里取，只算这一处（社交记了一笔，赛季页没更新过来）。
    now：已经现算好的今天的分数（省得再算一遍），不传就现算。"""
    db.conn.execute(SNAP_SQL)
    out = [{"day": r["day"], "v": round(r["score"])} for r in db.conn.execute(
        "SELECT day, score FROM attribute_daily WHERE key = ? AND day >= ? AND day < ? AND score IS NOT NULL ORDER BY day",
        (key, since.isoformat(), day.isoformat()))]
    if now is None:
        now = score_of(PARTS[key](db, *window(db, day)))
    if now is not None:
        out.append({"day": day.isoformat(), "v": round(now)})
    return out


def first_of(tr: list, day: date):
    """开季那天的分数：trail 里最早一张快照；只有今天现算的那一点（开季以来还没存过快照）就是没有，不拿今天冒充开季。"""
    return tr[0]["v"] if tr and tr[0]["day"] < day.isoformat() else None


def main():
    parser = argparse.ArgumentParser(description="七项属性")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--day", type=date.fromisoformat)
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        day = args.day or her_today()
        for r in compute(store.db, day):
            print(f"{r['name']}：{r['score'] if r['score'] is not None else '—'}（{r['label']}）"
                  + (f" {'▲' if r['delta'] >= 0 else '▼'}{abs(r['delta'])}" if r["delta"] is not None else ""), flush=True)
            for i in r["items"]:
                print(f"  · {i['name']}（{i['weight']}%）：{i['score'] if i['score'] is not None else '不计'}  {i['actual']}", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
