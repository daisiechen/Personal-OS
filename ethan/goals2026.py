"""录入 2026 年最后两个月的目标、量化指标和身体基线（设计第 0.5 节，2026-10-26 Mia 确认）。

可重复运行：按标题找父目标和已有目标，已存在的只更新指标和状态，不重复建。
运行：python3 -m ethan.goals2026 --data <数据目录>
"""

import argparse
import json
from pathlib import Path

from .memory import MemoryStore

DUE = "2026-12-31"
START = "2026-10-26"
FUNNEL = [["列出潜在客户", 30], ["联系", 20], ["聊过", 8], ["报价", 3], ["成交", 1]]

# (标题, 父目标标题, 状态, 指标参数, 挂到它下面的已有阶段目标)
YEAR_GOALS = [
    ("减脂塑形：年底体脂 27%–28%、体重约 56.5 kg", "常年保持健康体态", "active",
     dict(kind="weight_avg7", baseline=60.0, target=56.5, unit="kg", start_date=START),
     ["建立每周训练和饮食习惯"]),
    ("每周至少 1 次新体验", "拥有 2–3 个能长期坚持的爱好", "active",
     dict(kind="count_week", target=1, unit="次"), ["试 K-pop、古典舞、普拉提、网球，选出主爱好"]),
    ("年底前去 3 个新地方", "去世界各地看看", "paused",
     dict(kind="count_total", target=3, unit="个", start_date=START), []),
    ("创业拿到第一个订单", "做出自己的小事业", "active",
     dict(kind="funnel", target=1, spec=json.dumps({"steps": FUNNEL}, ensure_ascii=False)),
     ["创业项目跑起来：有产品交付或订单进来"]),
    ("DemoApp 上线", "做出自己的小事业", "active", dict(kind="launch"), []),
    ("个人管理系统上线", "这辈子干一件大事", "active", None, []),
]
# 已有目标的状态调整：她去掉了感情目标；进修计划这两个月暂停
STATUS_CHANGES = [("认识更多新朋友", "dropped", "2026-10-26 Mia 去掉这个目标"),
                  ("进修：确定要学的课程和时间", "paused", "2026-12-31 前暂停")]
BASELINE_WEIGHT = (START, 60.0)


def apply(store: MemoryStore) -> dict:
    db = store.db
    ids = {r["title"]: r["id"] for r in db.conn.execute("SELECT id, title FROM goals")}
    counts = {"created": 0, "updated": 0, "moved": 0, "status": 0}
    for title, parent, status, metric, children in YEAR_GOALS:
        parent_id = ids.get(parent)
        domain = db.conn.execute("SELECT domain FROM goals WHERE id = ?", (parent_id,)).fetchone()
        if title in ids:
            goal_id = ids[title]
            db.update_goal(goal_id, status=status)
            counts["updated"] += 1
        else:
            goal_id = db.add_goal(title, "year", parent_id=parent_id, status=status, due=DUE,
                                  domain=domain["domain"] if domain else None, source="2026-10-26 设计确认")
            ids[title] = goal_id
            counts["created"] += 1
        if metric:
            db.set_metric(goal_id, **metric)
        for child in children:
            if child in ids:
                db.conn.execute("UPDATE goals SET parent_id = ? WHERE id = ?", (goal_id, ids[child]))
                counts["moved"] += 1
    for title, status, note in STATUS_CHANGES:
        if title in ids:
            db.update_goal(ids[title], note=note, source="2026-10-26 设计确认", status=status)
            counts["status"] += 1
    day, weight = BASELINE_WEIGHT
    if not db.conn.execute("SELECT 1 FROM body_measures WHERE day = ? AND source = '基线'", (day,)).fetchone():
        db.add_body_measure(day, weight=weight, bodyfat=30.0, source="基线")
    db.conn.commit()
    return counts


def main():
    parser = argparse.ArgumentParser(description="录入 2026 年最后两个月的目标")
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    store = MemoryStore(args.data)
    print(apply(store))
    store.close()


if __name__ == "__main__":
    main()
