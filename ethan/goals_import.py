"""从一份目标表格冷启动（2026-10-29 开源准备：用户有一张目标表，AI agent 按 SETUP-FOR-AGENTS.md 的规则整理成 JSON，
这里写进系统）。可重复运行：标题一样的不重复建，只更新。

  python3 -m ethan.goals_import --data <数据目录> goals.json

goals.json 的样子见 examples/goals.json：
  {"season_end": "2026-12-31",
   "bosses": [{"title": "…", "territory": "健康", "main": true, "measure": {"kind": "count_week", "target": 3, "unit": "次"}}],
   "habits": [{"title": "…", "cadence": "daily"}, {"title": "…", "cadence": "weekly"}],
   "todos": ["…"], "for_ai": ["…"], "later": ["…"]}
副本写进去以后，计划器第二天早上自动拆主线步骤给用户过目；measure 的 kind 和 progress.py 认的一样（count_week、count_total、
weight_avg7、funnel、launch），不知道怎么量就不写。
"""

import argparse
import json
from pathlib import Path

from . import quests, territory
from .memory import MemoryStore


def apply(store: MemoryStore, data: dict) -> dict:
    db = store.db
    ids = {r["title"]: r["id"] for r in db.conn.execute("SELECT id, title FROM goals")}
    got = {"bosses": 0, "habits": 0, "todos": 0, "later": 0}
    due = data.get("season_end")
    for n, b in enumerate(data.get("bosses") or []):
        title = str(b["title"]).strip()
        key = territory.KEY_OF.get(str(b.get("territory") or ""))
        domain = territory.DOMAIN.get(key) if key else None
        if title in ids:
            db.update_goal(ids[title], status="active", due=b.get("due") or due)
        else:
            ids[title] = db.add_goal(title, "year", status="active", due=b.get("due") or due, domain=domain, source="目标表格导入")
        db.conn.execute("UPDATE goals SET sort = ? WHERE id = ?", (-1 if b.get("main") else n, ids[title]))  # 主副本排最前
        if b.get("measure"):
            db.set_metric(ids[title], **b["measure"])
        got["bosses"] += 1
    for title in data.get("later") or []:  # 候选副本：先不进这个赛季
        if title not in ids:
            ids[title] = db.add_goal(str(title), "year", status="paused", source="目标表格导入")
            got["later"] += 1
    for h in data.get("habits") or []:
        quests.add_habit(db, str(h["title"]), h.get("cadence", "daily"))
        got["habits"] += 1
    have = {r["title"] for r in db.conn.execute("SELECT title FROM tasks WHERE done = 0")}
    for title in data.get("todos") or []:
        if title not in have:
            db.add_task(str(title))
            got["todos"] += 1
    for title in data.get("for_ai") or []:
        if title not in have:
            db.add_task(str(title), promised_by_ethan=True)
            got["todos"] += 1
    db.conn.commit()
    return got


def main():
    p = argparse.ArgumentParser(description="从目标表格冷启动")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("file", type=Path)
    a = p.parse_args()
    store = MemoryStore(a.data)
    try:
        print(apply(store, json.loads(a.file.read_text(encoding="utf-8"))))
    finally:
        store.close()


if __name__ == "__main__":
    main()
