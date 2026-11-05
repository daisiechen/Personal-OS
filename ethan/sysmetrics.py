"""系统指标（2026-10-29 她说：“偏系统的量化指标应该是你要干的活儿”，由开发会话负责）。

量的是这套系统好不好用，不是她的目标。每周一个数，和上周比；数字变差由开发会话去修，不要她盯。
都从已有记录里算，不另外让她填：
- 她说 小伴 忘了 / 不记得：她的消息里出现“你忘了”“又忘了”“我说过”“跟你说过”这类
- 小伴 说不知道 / 不记得：他的回复里出现“不记得”“不知道你”“没有记录”“你没告诉我”这类
- 准备的事被打回重做 / 一次通过：prep_events 里的记录（prep.revise / prep.approve 时记）
- 后台任务出错：job_runs 里带错误的次数
- 上线失败：deploy-log.jsonl 里不是“已上线”的次数
设置页“系统体检”下半部分显示；巡检看到连续两周变差会去查原因。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SHE_FORGOT = re.compile(r"你(又)?忘了|你不记得|我(之前|早就|刚才)?(都)?(跟你)?说过|跟你说过|你怎么不记得")
ETHAN_BLANK = re.compile(r"不记得|不知道你|没有.{0,6}记录|你没(有)?(告诉|跟我说)|我这边(看不到|没有)")
SCHEMA = "CREATE TABLE IF NOT EXISTS prep_events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, task_id INTEGER, kind TEXT NOT NULL)"


def note_prep(db, task_id: int, kind: str):
    """kind：revise（她打回）/ approve（她通过）。"""
    db.conn.execute(SCHEMA)
    db.conn.execute("INSERT INTO prep_events (at, task_id, kind) VALUES (?, ?, ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), task_id, kind))


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _chat_counts(raw, start: date, end: date) -> tuple:
    forgot = blank = 0
    d = start
    while d < end:
        for r in raw.read_day(d.isoformat()):
            text = str(r.get("text") or "")
            if r.get("role") == "mia" and SHE_FORGOT.search(text):
                forgot += 1
            elif r.get("role") == "ethan" and ETHAN_BLANK.search(text):
                blank += 1
        d += timedelta(days=1)
    return forgot, blank


def _week(store, root: Path, start: date) -> dict:
    end = start + timedelta(days=7)
    a, b = start.isoformat(), end.isoformat()
    db = store.db
    db.conn.execute(SCHEMA)
    forgot, blank = _chat_counts(store.raw, start, end)
    ev = {r["kind"]: r["n"] for r in db.conn.execute(
        "SELECT kind, COUNT(*) AS n FROM prep_events WHERE at >= ? AND at < ? GROUP BY kind", (a, b))}
    errors = db.conn.execute("SELECT COUNT(*) AS n FROM job_runs WHERE error IS NOT NULL AND error != '' "
                             "AND finished_at >= ? AND finished_at < ?", (a, b)).fetchone()["n"]
    deploy_fail = 0
    try:
        for line in (Path(root) / "deploy-log.jsonl").read_text(encoding="utf-8").split("\n"):
            if line.strip():
                rec = json.loads(line)
                if a <= str(rec.get("at", ""))[:10] < b and not str(rec.get("result", "")).startswith("已上线"):
                    deploy_fail += 1
    except (OSError, ValueError):
        pass
    return {"forgot": forgot, "blank": blank, "revise": ev.get("revise", 0), "approve": ev.get("approve", 0),
            "errors": errors, "deploy_fail": deploy_fail}


LABELS = (("forgot", "你说 小伴 忘了", "越少越好"), ("blank", "小伴 说不知道、不记得", "越少越好"),
          ("revise", "准备的事被你打回重做", "越少越好"), ("approve", "准备的事你通过了", "越多越好"),
          ("errors", "后台任务出错", "越少越好"), ("deploy_fail", "上线失败", "越少越好"))


def weekly(store, root, today: date = None) -> list:
    """[{name, this_week, last_week, better: True/False/None, hint}]"""
    today = today or datetime.now(timezone.utc).date()
    now_w, last_w = _week(store, root, _monday(today)), _week(store, root, _monday(today) - timedelta(days=7))
    out = []
    for key, name, hint in LABELS:
        a, b = now_w[key], last_w[key]
        better = None if a == b else (a > b) == (hint == "越多越好")
        out.append({"key": key, "name": name, "this_week": a, "last_week": b, "better": better, "hint": hint})
    return out
