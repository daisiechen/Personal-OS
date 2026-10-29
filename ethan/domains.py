"""她要管理的各个方面，以及习惯模块的两周冷启动（设计第 0.6、9.5 节）。

各方面的状态全部由代码从数据里算，早报每天一行一个方面，不会漏掉。
习惯模块（财务、社交、学习、休闲、专注）冷启动一次：从开始那天起每天排一个，作为当天“必须做”的待办，
小伴 在聊天里带她做，做完记 [建档 模块 完成]；没做完就留到第二天，一天只排一个。
"""

from datetime import date, timedelta

from .progress import year_progress

# (模块, 小伴 带她做时要收集什么)
COLD_START = [  # 2026-10-28 按 territories.md 清理：Apple Pay 不单独接（Monzo 和招行覆盖），赛季 1 只练英语，专注不是领地
    ("财务", "一起定应急金目标（几个月生活费）和每月预算（人民币，定了这个月就锁住，在工作台财务页填）；"
             "问清她现在每月大概花多少、有哪些固定订阅。账单不用她导：Monzo 和招行邮件已自动收。"),
    ("社交", "先口述最想保持联系的 10 个人：怎么认识的、多久联系一次合适、生日；每说一个记一张人物卡 [人物 名字 …]。"),
    ("学习", "赛季 1 只练英语（Grok 陪练，每周 5 次）：问她每周哪几天、什么时间段方便练；认知拓展每天一个话题，问她最近想听什么。"),
    ("休闲", "一起列体验清单：想去的店、展、演出、想试的运动和课（K-pop、古典舞、普拉提、网球）；问预算和喜欢的时间。"),
]
# 七块领地（2026-10-27 晚她定，见 docs/design/territories.md）；专注不再是领地，只当复盘指标
DOMAINS = ["身体", "事业", "财务", "社交", "学习", "学术", "生活体验"]
# 领地对应的冷启动模块（module_status 里的名字）
MODULE_OF = {"财务": "财务", "社交": "社交", "学习": "学习", "生活体验": "休闲"}
OLD_NAMES = {"健康": "身体", "休闲": "生活体验", "旅行": "生活体验", "感情": "社交"}  # 重整前的领地名


def _status_rows(db) -> dict:
    return {r["module"]: dict(r) for r in db.conn.execute("SELECT * FROM module_status")}


def set_module(db, module: str, state: str, note: str = None):
    db.conn.execute("INSERT INTO module_status (module, cold_start, note, updated_at) VALUES (?, ?, ?, datetime('now')) "
                    "ON CONFLICT(module) DO UPDATE SET cold_start = excluded.cold_start, note = excluded.note, "
                    "updated_at = excluded.updated_at", (module, state, note))
    db.conn.commit()


def todays_cold_start(db, today: date, start: date):
    """今天该做哪个模块的冷启动：(模块, 怎么做)；还没开始或都做完返回 None。"""
    if start is None or today < start:
        return None
    done = {m for m, r in _status_rows(db).items() if r["cold_start"] == "已完成"}
    for module, how in COLD_START:
        if module not in done:
            return module, how
    return None


def domain_lines(db, today: date, start: date = None) -> list:
    """每个方面一行状态。"""
    rows = _status_rows(db)
    progress = year_progress(db, today)
    lines = []
    of_goal = {r["id"]: OLD_NAMES.get(r["domain"], r["domain"]) for r in db.conn.execute("SELECT id, domain FROM goals")}
    for domain in DOMAINS:
        parts = [f"{p['title']}：{p['status']}（{p['text']}）" for p in progress if of_goal.get(p["goal_id"]) == domain]
        if domain == "身体":
            # 和健康区域、成就用同一个数：日常“训练”打卡的天数（她自己打卡）
            done = db.conn.execute(
                "SELECT COUNT(DISTINCT l.day) FROM habit_log l JOIN habits h ON h.id = l.habit_id WHERE h.title = '训练' AND l.day >= ?",
                ((today - timedelta(days=today.weekday())).isoformat(),)).fetchone()[0]
            parts.append(f"本周训练 {done}/5 次")
        if domain == "学术":
            parts.append("进修计划这两个月暂停")
        module = MODULE_OF.get(domain)
        if module:
            state = rows.get(module, {}).get("cold_start", "待建档")
            if state != "已完成":
                parts.append(f"{state}（冷启动{'从 ' + start.isoformat() + ' 开始' if start else '还没开始'}）")
            elif rows[module].get("note"):
                parts.append(rows[module]["note"])
            else:
                parts.append("已建档")
        shown = {"身体": "健康"}.get(domain, domain)  # 库里还记“身体”，给她看的叫“健康”（2026-10-29）
        lines.append(f"{shown}：" + "；".join(parts) if parts else f"{shown}：还没有数据")
    return lines
