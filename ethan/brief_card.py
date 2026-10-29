"""今日卡片：早报和“☀️ 今天”按钮用同一张（2026-10-27 她定）。

早报分两条：小伴 先用自己的话说几句（不报时间和数字），紧跟着这张卡；
平时点输入框下的“☀️ 今天”再发一张最新的到聊天最底下，不用往上翻找训练和三餐。
卡上的时间和数字全由代码照抄计划和数据库，不经过模型，所以不会念错。
2026-10-28 她嫌太长：卡上只留时间线和等级，目标、日常、主线、支线她去工作台看。
"""

import html
import json
from datetime import date

from . import quests

WEEKDAYS = "一二三四五六日"
BAR = 10
BOTTOM = [{"text": "🏋️ 训练", "callback_data": "bf:train"}, {"text": "🍳 三餐", "callback_data": "bf:meals"},
          {"text": "📋 全部", "callback_data": "bf:all"}, {"text": "🔄", "callback_data": "qs:refresh:quests"}]


def has_card(plan: dict) -> bool:
    """旧版计划没有时间线素材，就还按以前的方式只发 小伴 的话。"""
    return "timeline" in json.loads(plan["brief"])


def bar(frac: float) -> str:
    n = round(max(0.0, min(frac, 1.0)) * BAR)
    return "▓" * n + "░" * (BAR - n)


def short(goal: str) -> str:
    return goal.split("：")[0]


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def next_steps(db) -> list:
    """每个目标的主线下一步（按此刻的数据），[(目标短名, 任务, 拆解是否还是草稿)]。"""
    goals = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM goals")}
    out = []
    for gid, qs in quests.main_quests(db).items():
        nxt = next((q for q in qs if not q["done"]), None)
        if nxt:
            out.append((short(goals.get(gid, "")), nxt, any(q["draft"] for q in qs)))
    return out


def render(plan, db, day: date, start: date = None) -> str:
    """Telegram HTML 格式的今日卡片。plan 可以是 None（今天还没排计划）；start 是系统上线那天。"""
    e = html.escape
    b = json.loads(plan["brief"]) if plan else {}
    g = quests.today_summary(db, day)
    head = f"☀️ <b>{day.month}月{day.day}日 周{WEEKDAYS[day.weekday()]}</b>"
    if start and day >= start:
        head += f" · 系统上线第 {(day - start).days + 1} 天"
    out = [head]
    if b.get("fallback"):
        out.append("<i>今天是保底计划（自动排计划没成功）</i>")
    if plan is None:
        out.append("<i>今天的计划还没排好</i>")
    elif "timeline" in b:
        out += ["", "🕐 <b>时间线</b>"]
        out += [f"<code>{t['at']}</code>  {t['icon']} {e(t['text'])}" for t in b["timeline"]] or ["今天不用出门"]
    game = f"🎮 <b>Lv{g['level']} {e(g['title'])}</b> · 积分 {g['balance']} · 连续满勤 {g['streak']['days']} 天"
    nxt = min((r for r in quests.rewards(db) if r["cost"] > g["balance"]), key=lambda r: r["cost"], default=None)
    if nxt:
        game += f" · 离{e(nxt['title'])}还差 {nxt['cost'] - g['balance']}"
    out += ["", game]
    return "\n".join(out)


def keyboard(db, day: date) -> list:
    """卡片下的按钮：没打卡的日常和周常、每个目标的主线下一步、支线（每行两个），最后一行是训练、三餐、全部、刷新。"""
    g = quests.today_summary(db, day)
    taps = [{"text": f"✓ {_clip(h['title'], 10)}", "callback_data": f"qs:h:{h['id']}"} for h in g["daily"] if not h["done"]]
    taps += [{"text": f"✓ {_clip(h['title'], 10)}", "callback_data": f"qs:h:{h['id']}"}
             for h in g["weekly"] if h["count"] < h["target"]]
    taps += [{"text": f"✓ {_clip(name, 8)} +{quests.task_points(q)}", "callback_data": f"qs:t:{q['id']}"}
             for name, q, _ in next_steps(db)]
    taps += [{"text": f"✓ {_clip(q['title'], 10)} +{quests.task_points(q)}", "callback_data": f"qs:t:{q['id']}"}
             for q in quests.side_quests(db)[:8]]
    return [taps[i:i + 2] for i in range(0, len(taps), 2)] + [BOTTOM]


def detail(plan: dict, which: str) -> str:
    """按钮“训练”“三餐”另发的一条（纯文字）。"""
    if which == "train":
        item = next((i for i in plan["items"] if i["kind"] == "event" and i["title"].startswith("训练")), None)
        return f"🏋️ {item['title']}\n\n{item['detail']}" if item and item.get("detail") else "今天没有训练，休息日"
    if which == "meals":
        text = json.loads(plan["brief"]).get("meals_text")
        return f"🍳 {text}" if text else "今天还没有三餐安排"
    raise ValueError(which)
