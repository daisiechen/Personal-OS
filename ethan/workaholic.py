"""工作狂指数和晚上收工（她 2026-10-31 定）。

她说：“我现在跟 AI 工作起来就没完没了，也不愿意出门……希望扑在工作上的时间以后越来越少，但效率和产出越来越高，产出是稳定的。”

工作时长从哪来（ethan/worktime.py 的 5 分钟格，一格 5 分钟）：
- Mac 键盘鼠标真有人在动（Claude Code、Codex 在终端跑不碰键鼠，不算她的）；
- 她给 Claude Code 发的每句话（Mac、云端、服务器上的会话都算，她 10-31：手机上发的也算）；
- iPhone 打开 Claude / Codex App 到关掉这段（快捷指令自动化报开、关）；
- 工作台开着、在前台的时候（页面每分钟报一次）。
不算：在 Telegram 跟 小伴 聊天（她 10-31：那是生活）。

指数 0–100，一天一个，一周取平均。健康区间 20–49（她 2026-11-01：不是越低越好，定一个健康区间）：
  20 以下偏松（可能没在推进事业学术）、20–49 健康、50–69 偏高、70 以上过度。算法：
  总时长 40 分（12 小时封顶）+ 晚上 9 点后 20 分（3 小时封顶）+ 最长一段不停 15 分（4 小时封顶，中间停够 15 分钟才算歇）
  + 周末还在工作 15 分（周末 6 小时封顶）+ 一整天没出门 10 分（出门 2 小时就不扣）。
产出只算事业和学术（她 10-31：学术算、学习不算、生活体验不算）：这周完成的事业 / 学术任务经验，另记 agent 交的活被她验收通过几件。
效率 = 事业和学术经验 ÷ 工作小时。目标：时长一周比一周少，产出稳住，效率往上。

晚上（她 10-31 定）：9 点提醒收尾，10 点劝收工，11 点停；她跟 小伴 说“今晚要赶，到几点”就放行到那时（[加班 到 01:00]）。
11 点后 Mac 和云端的 Claude Code 新指令被钩子挡回（ethan/setup_hook.py 的收工钩子），Mac 弹全屏提醒（不锁屏），
终端里的 codex 先提示过点；iPhone 打开 Claude / Codex App 时快捷指令弹提醒并退回桌面。
"""

import json
import re
from datetime import date, datetime, time, timedelta, timezone

from . import worktime

WORK_DOMAINS = ("事业", "学术")
REMIND, PERSUADE, STOP = time(21, 0), time(22, 0), time(23, 0)
MORNING = time(5, 0)  # 凌晨 5 点前还算前一晚
OVERRIDE = "CREATE TABLE IF NOT EXISTS curfew_override (night TEXT PRIMARY KEY, until TEXT NOT NULL, at TEXT NOT NULL, said TEXT)"
APPS = "CREATE TABLE IF NOT EXISTS app_sessions (id INTEGER PRIMARY KEY, app TEXT NOT NULL, opened TEXT NOT NULL, closed TEXT)"
MARK = re.compile(r"\[加班\s*到\s*(\d{1,2})[:：](\d{2})\s*\]")
APP_MAX = timedelta(hours=3)  # 打开了忘了关：最多算 3 小时
APP_DANGLING = timedelta(minutes=30)  # 只报了打开、没报关掉：算 30 分钟


HEALTHY = (20, 49)


def band(index: int) -> dict:
    """指数落在哪一档：{"light": ok/warn/bad, "label"}。"""
    if index < HEALTHY[0]:
        return {"light": "warn", "label": "偏松"}
    if index <= HEALTHY[1]:
        return {"light": "ok", "label": "健康"}
    return {"light": "warn", "label": "偏高"} if index < 70 else {"light": "bad", "label": "过度"}


def ensure(db):
    worktime.ensure(db)
    db.conn.execute(OVERRIDE)
    db.conn.execute(APPS)


def _tz():
    return worktime._tz()


# ---------- 新的时长来源 ----------
def _slots_between(a: datetime, b: datetime) -> list:
    out, t = [], a
    while t < b:
        out.append(worktime.slot_of(t))
        t += timedelta(minutes=worktime.SLOT_MIN)
    return out


def app_event(db, event: str, app: str, now: datetime = None) -> dict:
    """iPhone 快捷指令：打开 / 关掉 Claude、Codex App。关掉时把这段记成工作格。返回现在是不是过了收工点（快捷指令据此退回桌面）。"""
    ensure(db)
    now = now or datetime.now(timezone.utc)
    app = (app or "app")[:20]
    if event == "open":
        _close_dangling(db, app, now)
        db.conn.execute("INSERT INTO app_sessions (app, opened) VALUES (?, ?)", (app, now.isoformat(timespec="seconds")))
    elif event == "close":
        r = db.conn.execute("SELECT * FROM app_sessions WHERE app = ? AND closed IS NULL ORDER BY id DESC LIMIT 1", (app,)).fetchone()
        if r:
            opened = datetime.fromisoformat(r["opened"])
            end = min(now, opened + APP_MAX)
            worktime.record(db, _slots_between(opened, end), "phone:" + app)
            db.conn.execute("UPDATE app_sessions SET closed = ? WHERE id = ?", (now.isoformat(timespec="seconds"), r["id"]))
    else:
        raise ValueError("event 只能是 open 或 close")
    db.conn.commit()
    return curfew(db, now)


def _close_dangling(db, app: str, now: datetime):
    for r in db.conn.execute("SELECT * FROM app_sessions WHERE app = ? AND closed IS NULL", (app,)).fetchall():
        opened = datetime.fromisoformat(r["opened"])
        worktime.record(db, _slots_between(opened, min(now, opened + APP_DANGLING)), "phone:" + app)
        db.conn.execute("UPDATE app_sessions SET closed = ? WHERE id = ?", (now.isoformat(timespec="seconds"), r["id"]))


def heartbeat(db, now: datetime = None) -> int:
    """工作台开着、在前台：记一格。"""
    return worktime.record(db, [worktime.slot_of(now or datetime.now(timezone.utc))], "workbench")


# ---------- 指数 ----------
def _slots_of_day(db, day: date) -> list:
    ensure(db)
    return sorted(datetime.strptime(r[0], "%Y-%m-%dT%H:%M") for r in
                  db.conn.execute("SELECT slot FROM work_slots WHERE slot >= ? AND slot < ?", worktime.day_range(day)))


def _outside_hours(db, day: date) -> float:
    try:
        from . import places_log
        if not places_log.has_data(db):
            return None
        total = 0.0
        for leave, back in places_log.trips(db, day, day):
            if leave.date() != day:
                continue
            end = back or datetime.combine(day, time(23, 59), tzinfo=leave.tzinfo)
            total += (end - leave).total_seconds() / 3600
        return round(total, 1)
    except Exception:
        return None


def day_stats(db, day: date) -> dict:
    slots = _slots_of_day(db, day)
    h = len(slots) * worktime.SLOT_MIN / 60
    late = sum(1 for s in slots if s.time() >= REMIND or s.time() < MORNING) * worktime.SLOT_MIN / 60  # 过了半夜也算晚上
    # 最长一段不停：按时间跨度算（中间不到 15 分钟的空当也算在“没停”里）
    stretch, start, prev = 0.0, None, None
    for s in slots:
        if prev is None or (s - prev) > timedelta(minutes=15):
            start = s
        stretch = max(stretch, (s - start).total_seconds() / 3600 + worktime.SLOT_MIN / 60)
        prev = s
    weekend = day.weekday() >= 5
    out = _outside_hours(db, day)
    score = (min(h / 12, 1) * 40 + min(late / 3, 1) * 20 + min(stretch / 4, 1) * 15
             + (min(h / 6, 1) * 15 if weekend else 0) + ((1 - min(out / 2, 1)) * 10 if out is not None and h > 0 else 0))
    return {"day": day.isoformat(), "hours": round(h, 1), "late": round(late, 1), "stretch": round(stretch, 1),
            "weekend": weekend, "outside": out, "index": round(score) if h > 0 else 0}


def output(db, start: date, end: date) -> dict:
    """事业和学术：这几天完成的任务经验；agent 交的活被她验收通过几件。"""
    pts = db.conn.execute(
        "SELECT COALESCE(SUM(p.delta), 0) FROM points p JOIN tasks t ON p.ref = 'task:' || t.id LEFT JOIN goals g ON g.id = t.goal_id "
        f"WHERE p.delta > 0 AND p.day >= ? AND p.day < ? AND COALESCE(t.domain, g.domain) IN ({','.join('?' * len(WORK_DOMAINS))})",
        (start.isoformat(), end.isoformat(), *WORK_DOMAINS)).fetchone()[0]
    try:
        acc = db.conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE status = 'accepted' AND reviewed_at >= ? AND reviewed_at < ?",
                              (start.isoformat(), end.isoformat())).fetchone()[0]
    except Exception:
        acc = 0
    return {"points": int(pts), "accepted": acc}


def week(db, day: date) -> dict:
    """day 所在这周（周一到 day）。"""
    monday = day - timedelta(days=day.weekday())
    days = [day_stats(db, monday + timedelta(days=i)) for i in range(day.weekday() + 1)]
    worked = [d for d in days if d["hours"] > 0]
    hours = round(sum(d["hours"] for d in days), 1)
    out = output(db, monday, day + timedelta(days=1))
    return {"from": monday.isoformat(), "days": days, "hours": hours,
            "index": round(sum(d["index"] for d in worked) / len(worked)) if worked else 0,
            "late": round(sum(d["late"] for d in days), 1), "weekend": round(sum(d["hours"] for d in days if d["weekend"]), 1),
            "output": out, "efficiency": round(out["points"] / hours, 1) if hours else None}


def compare(db, day: date) -> dict:
    """这周到今天 vs 上周同样几天（周报和世界页用）。"""
    this = week(db, day)
    last = week(db, day - timedelta(days=7))
    return {"this": this, "last": last}


def today_line(db, now_local: datetime) -> dict:
    d = day_stats(db, _night(now_local))  # 凌晨 5 点前还算前一天
    return {"hours": d["hours"], "index": d["index"], "has_data": worktime.has_data(db), **band(d["index"])}


# ---------- 晚上收工 ----------
def _night(now_local: datetime) -> date:
    return (now_local - timedelta(days=1)).date() if now_local.time() < MORNING else now_local.date()


def override_until(db, now_local: datetime):
    ensure(db)
    r = db.conn.execute("SELECT until FROM curfew_override WHERE night = ?", (_night(now_local).isoformat(),)).fetchone()
    if not r:
        return None
    hh, mm = map(int, r["until"].split(":"))
    end = datetime.combine(_night(now_local), time(hh, mm), tzinfo=now_local.tzinfo)
    if hh < 12:  # 到 1 点 = 第二天凌晨
        end += timedelta(days=1)
    return end


def note_chat(db, text: str, said: str = "", now: datetime = None) -> list:
    """小伴 回复里的 [加班 到 01:00]：今晚放行到那时。"""
    ensure(db)
    now_local = (now or datetime.now(timezone.utc)).astimezone(_tz())
    out = []
    for hh, mm in MARK.findall(text or ""):
        until = f"{int(hh) % 24:02d}:{mm}"
        db.conn.execute("INSERT OR REPLACE INTO curfew_override (night, until, at, said) VALUES (?, ?, ?, ?)",
                        (_night(now_local).isoformat(), until, now_local.isoformat(timespec="seconds"), (said or "")[:200]))
        out.append(until)
    db.conn.commit()
    return out


def strip(text: str) -> str:
    return MARK.sub("", text)


def phase(now_local: datetime) -> str:
    t = now_local.time()
    if t >= STOP or t < MORNING:
        return "stop"
    if t >= PERSUADE:
        return "persuade"
    if t >= REMIND:
        return "remind"
    return "ok"


def curfew(db, now: datetime = None) -> dict:
    """GET /cloud/curfew：Mac 记录脚本、Claude Code 收工钩子、iPhone 快捷指令都问这一个。stop=true 就停。"""
    now_local = (now or datetime.now(timezone.utc)).astimezone(_tz())
    p = phase(now_local)
    until = override_until(db, now_local)
    allowed = until is not None and now_local < until
    stop = p == "stop" and not allowed
    msg = (f"已经 {now_local.strftime('%H:%M')} 了，今天到这儿。真有明天一定要交的，跟 小伴 说“今晚要赶，到几点”，就放行到那时。"
           if stop else "")
    return {"ok": True, "phase": p, "stop": stop, "until": until.strftime("%H:%M") if allowed else None, "message": msg,
            "hours_today": day_stats(db, _night(now_local))["hours"]}


PROMPT = ("晚上收工（她 2026-10-31 定）：9 点提醒收尾，10 点劝收工，11 点停。她说今晚有明天一定要交的、要赶，"
          "先问清到几点，再在回复末尾写 [加班 到 01:00]（放行到那时，这个标记不会发给她），并提醒她一句别太晚。"
          "她没说要赶，就不要替她放行。")
