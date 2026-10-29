"""战报：领地地图的“战报”标签（设计见 docs/design/workbench.md 页面表；2026-10-28 做）。

- 周战报：每周（周一到周日）每块领地的健康灯和各项数、积分、满勤天数、副本进度、小伴 周日写的周报。
  本周按此刻实时算；一周过完后第一次打开就把那周存下来（war_weeks），以后不变，副本进度也停在那一刻。
- 月度、赛季结算：把存下来的周汇总——每块领地几周绿 / 黄 / 红、积分合计、满勤天数。
- 她可以给复盘写一句反馈，记给管理系统，计划器排计划时用。
数字全由代码算，不经过模型。
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import quests, territory

SCHEMA = "CREATE TABLE IF NOT EXISTS war_weeks (week TEXT PRIMARY KEY, data TEXT NOT NULL, frozen_at TEXT NOT NULL);"


def ensure(db):
    db.conn.executescript(SCHEMA)


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _ethan_weekly(root: Path, start: date, end: date) -> str:
    """小伴 那周日晚上发的周报（原始对话里 kind=weekly 的那条）。"""
    for p in sorted((Path(root) / "raw").glob("*.jsonl"), reverse=True):
        if not (start.isoformat() <= p.stem <= (end + timedelta(days=1)).isoformat()):
            continue
        for line in reversed(p.read_text(encoding="utf-8").split("\n")):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("kind") == "weekly" and r.get("role") == "ethan":
                return r.get("text", "")
    return ""


def compute_week(db, root: Path, start: date, upto: date, bosses=None) -> dict:
    end = start + timedelta(days=6)
    day = min(end, upto)
    terrs = []
    for key, name, _ in territory.TERRITORIES:
        active = any(b.get("territory") == key for b in (bosses or []))
        rows = territory.health(db, key, day, active, root)
        terrs.append({"key": key, "name": name, "light": territory.worst([r["light"] for r in rows]),
                      "rows": [{k: r[k] for k in ("name", "quest", "floor", "actual", "light")} for r in rows]})
    pts = db.conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points WHERE delta > 0 AND day >= ? AND day <= ?",
                          (start.isoformat(), end.isoformat())).fetchone()[0]
    full = sum(1 for i in range((day - start).days + 1) if quests.full_day(db, start + timedelta(days=i)))
    return {"week": start.isoformat(), "end": end.isoformat(), "territories": terrs, "points": pts, "full_days": full,
            "days": (day - start).days + 1,
            "bosses": [{k: b.get(k) for k in ("name", "main", "frac", "line", "status")} for b in (bosses or [])],
            "ethan": _ethan_weekly(root, start, end)}


def weeks(db, root: Path, today: date, season_start: date, bosses=None) -> list:
    """赛季开始以来每一周，新的在前。过完的周存下来不再变。"""
    ensure(db)
    out, start = [], monday(today)
    first = monday(min(season_start, today))  # 赛季还没开始也至少有本周
    while start >= first:
        end = start + timedelta(days=6)
        row = db.conn.execute("SELECT data FROM war_weeks WHERE week = ?", (start.isoformat(),)).fetchone()
        if row:
            data = json.loads(row["data"])
        else:
            data = compute_week(db, root, start, today, bosses)
            if end < today:  # 这周过完了，存下来
                db.conn.execute("INSERT OR IGNORE INTO war_weeks (week, data, frozen_at) VALUES (?, ?, ?)",
                                (start.isoformat(), json.dumps(data, ensure_ascii=False),
                                 datetime.now(timezone.utc).isoformat(timespec="seconds")))
                db.conn.commit()
        data["live"] = end >= today
        out.append(data)
        start -= timedelta(days=7)
    return out


def rollup(ws: list) -> dict:
    """几周汇总：每块领地绿 / 黄 / 红各几周，积分、满勤天数合计。"""
    terr = {}
    for w in ws:
        for t in w["territories"]:
            c = terr.setdefault(t["key"], {"key": t["key"], "name": t["name"], "ok": 0, "warn": 0, "bad": 0, "none": 0})
            c[t["light"]] += 1
    return {"weeks": len(ws), "points": sum(w["points"] for w in ws), "full_days": sum(w["full_days"] for w in ws),
            "territories": list(terr.values())}


def green_streak(db) -> int:
    """某块领地连续全绿的最长周数（只看过完的周），给成就“某块领地连续全绿”用。"""
    ensure(db)
    rows = [json.loads(r["data"]) for r in db.conn.execute("SELECT data FROM war_weeks ORDER BY week")]
    best = 0
    for key, _, _ in territory.TERRITORIES:
        run = 0
        for w in rows:
            light = next((t["light"] for t in w["territories"] if t["key"] == key), "none")
            run = run + 1 if light == "ok" else 0
            best = max(best, run)
    return best


def page(db, root: Path, today: date, season: dict, bosses=None) -> dict:
    season_start = date.fromisoformat(season["start"])
    ws = weeks(db, root, today, season_start, bosses)
    months = {}
    for w in ws:
        months.setdefault(w["week"][:7], []).append(w)
    done = [w for w in ws if not w["live"]]
    return {"weeks": ws, "months": [{"month": m, **rollup(v)} for m, v in sorted(months.items(), reverse=True)],
            # 灯只数过完的周；积分和满勤把本周也算上（2026-10-29 她看到战报积分 0、首页 110 对不上）
            "season": {**season, **rollup(done), "points": sum(w["points"] for w in ws),
                       "full_days": sum(w["full_days"] for w in ws), "days_left": max(0, (date.fromisoformat(season["end"]) - today).days)}}
