"""任务系统在 Telegram 里的样子（2026-10-26 Mia 要固定按钮，不用记暗号）。

输入框下面常驻四个按钮：☀️ 今天、🛍 商店、📊 进度、📋 清单。点了弹出对应页面，页面里用内联按钮打卡、完成、兑换。
“☀️ 今天”就是早报那张今日卡片（2026-10-27 她要合成一张，见 brief_card.py），打卡在卡片上点。
兑换要点两次（先选、再确认），避免手滑扣分。所有页面都按 Telegram HTML 发。
"""

import html
from datetime import date

from . import brief_card, quests

BUTTONS = {"☀️ 今天": "quests", "今天": "quests", "🎯 任务": "quests", "任务": "quests",
           "🛍 商店": "shop", "商店": "shop", "📊 进度": "progress", "进度": "progress"}


def which(text: str):
    return BUTTONS.get(text.strip())


def _short(text: str, n: int = 16) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def render_shop(db) -> tuple:
    quests.seed(db)
    t = quests.totals(db)
    lines = [f"🛍 惊喜商店 · 积分 {t['balance']}", "",
             "每次拆开都不一样；要花你钱的会先问你。", ""]
    kb = []
    for r in quests.rewards(db):
        lines.append(f"{r['title']} · {r['cost']} 积分\n   {r['note'] or ''}")
        if t["balance"] >= r["cost"]:
            kb.append([{"text": f"兑换 {r['title']}（{r['cost']} 分）", "callback_data": f"qs:r:{r['id']}"}])
        else:
            lines.append(f"   还差 {r['cost'] - t['balance']} 积分")
    pending = quests.pending_redemptions(db)
    if pending:
        lines += ["", "🎁 已兑换、小伴 正在策划：" + "、".join(f"{p['title']}" for p in pending)]
    return "\n".join(lines), kb


def render_progress(db, day: date) -> tuple:
    quests.seed(db)
    g = quests.today_summary(db, day)
    nxt = g["next_level_at"]
    bar = ""
    if nxt:
        filled = int(10 * g["earned"] / nxt)
        bar = "▰" * filled + "▱" * (10 - filled) + f" {g['earned']}/{nxt}"
    lines = [f"📊 Lv{g['level']} {g['title']}", bar,
             f"经验 {g['earned']} · 积分 {g['balance']} · 今天 +{g['today_points']}",
             f"连续满勤 {g['streak']['days']} 天 · 最长 {g['streak']['best']} 天 · 本周补签卡用了 {g['streak']['freezes_used']} 张",
             "", "🗺 主线进度"]
    goals = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM goals")}
    for gid, qs in quests.main_quests(db).items():
        done = sum(q["done"] for q in qs)
        lines.append(f"· {goals.get(gid, '')} {done}/{len(qs)}")
    recent = db.conn.execute("SELECT reason, delta FROM points WHERE delta > 0 ORDER BY id DESC LIMIT 5").fetchall()
    if recent:
        lines += ["", "最近得分"] + [f"+{r['delta']} {r['reason']}" for r in recent]
    return "\n".join(x for x in lines if x is not None), [[{"text": "🔄 刷新", "callback_data": "qs:refresh:progress"}]]


def handle(db, data: str, day: date) -> tuple:
    """处理 qs: 开头的按钮，返回 (顶部提示, 要刷新成哪个页面)。"""
    parts = data.split(":")
    if len(parts) < 3:
        return "这个按钮过期了", None
    _, action, arg = parts[0], parts[1], parts[2]
    if action == "refresh":
        return "已刷新", arg
    if action == "h":
        gain = quests.check_habit(db, int(arg), day)
        return (f"打卡成功 +{gain} 分" if gain else "今天已经打过卡啦"), "quests"
    if action == "t":
        gain = quests.complete_task(db, int(arg), day)
        return (f"完成！+{gain} 分" if gain else "已经完成过了"), "quests"
    if action == "r":
        return "再点一次“确认兑换”就扣分", f"confirm:{arg}"
    if action == "rc":
        ok, msg = quests.redeem(db, int(arg), day)
        return msg, "shop"
    return "这个按钮过期了", None


def render(db, view: str, day: date, plan: dict = None, start: date = None) -> tuple:
    """返回 (Telegram HTML 文字, 内联按钮)。plan 是今天的计划，“今天”页的时间线要用。"""
    quests.seed(db)
    if view.startswith("confirm:"):
        rid = int(view.split(":")[1])
        r = db.conn.execute("SELECT * FROM rewards WHERE id = ?", (rid,)).fetchone()
        text = f"确认用 {r['cost']} 分兑换「{r['title']}」吗？" if r else "这个奖励不在了"
        return html.escape(text), [[{"text": "✓ 确认兑换", "callback_data": f"qs:rc:{rid}"},
                                    {"text": "算了", "callback_data": "qs:refresh:shop"}]]
    if view == "shop":
        text, kb = render_shop(db)
    elif view == "progress":
        text, kb = render_progress(db, day)
    else:
        return brief_card.render(plan, db, day, start), brief_card.keyboard(db, day)
    return html.escape(text), kb
