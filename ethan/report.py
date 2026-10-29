"""总览和周报：她说“看看总览”时当场汇报，每周日晚上主动发一次周报。

汇报按七块领地走（2026-10-28 接上领地和战报，见 docs/design/territories.md）：每块的健康灯和各项数（和战报同一套算法）、
副本进度、本周积分和满勤，再加这周新收藏串成的“这周你在关注什么”（docs/design/saved-feed.md）、答应她的事和心愿。
事实从记忆库取，由 小伴 用自己的口吻写；还没有数据来源的领地如实说，不编数字。

周报：python3 -m ethan.report --data <数据目录> --weekly（ethan2-weekly.timer 每周日 20:00 伦敦时间触发）
"""

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

from . import brain, english, focus, progress, review
from .core.agent import Ethan, build_system_prompt
from .memory import MemoryStore

OVERVIEW_WORDS = ("看看总览", "总览", "/overview")
MAX_TELEGRAM = 4000


def is_overview_request(text: str) -> bool:
    return text.strip().rstrip("。！!？?~～") in OVERVIEW_WORDS


def week_start(today: date) -> date:
    return today - timedelta(days=today.weekday())  # 周一


def collect_facts(store: MemoryStore, today: date) -> dict:
    db = store.db
    since = week_start(today).isoformat()

    def rows(sql, *params):
        return [dict(r) for r in db.conn.execute(sql, params)]

    goals = [g for g in db.active_goals() if g["level"] in ("year", "stage", "task")]
    for g in goals:
        g["本周进展"] = [r["note"] for r in rows(
            "SELECT note FROM progress_log WHERE goal_id = ? AND created_at >= ? ORDER BY id", g["id"], since)]
        g["本周完成的待办"] = [r["title"] for r in rows(
            "SELECT title FROM tasks WHERE goal_id = ? AND done = 1 AND done_at >= ? ORDER BY done_at",
            g["id"], since)]
    # 各方面这周有没有动静：方面下面任何目标有进展或完成待办就算有
    chain = db.goal_chain()
    active_ids = {g["id"] for g in goals if g["本周进展"] or g["本周完成的待办"]}
    quiet, current = [], None
    for g in chain:
        if g["level"] == "long":
            current = {"方面": g["title"], "有动静": False}
            quiet.append(current)
        elif current is not None and g["depth"] > 1 and g["id"] in active_ids:
            current["有动静"] = True
    week = review.compute_week(db, store.root, week_start(today), today)
    territories = [{"区域": t["name"], "灯": {"ok": "绿", "warn": "黄", "bad": "红", "none": "没数据"}[t["light"]],
                    "各项": [f"{r['name']}：{r['actual']}（保底 {r['floor']}）" for r in t["rows"] if r["light"] != "none"]}
                   for t in week["territories"]]
    brain.sync(store)
    saved = [{"标题": x["title"], "小伴 的摘要": x["extra"].get("ai_summary") or (x["summary"] or "")[:80],
              "区域": x["territory_name"]} for x in brain.search(db, "all", kind="saved", limit=200)
             if (x["created_at"] or "") >= since][:30]
    english.ensure(db)
    eng = db.conn.execute("SELECT COUNT(*) n, COALESCE(SUM(minutes), 0) m FROM english_sessions WHERE day >= ?", (since,)).fetchone()
    try:
        bosses = [{"副本": p["title"], "进度": p.get("text") or "", "状态": p.get("status") or ""} for p in progress.year_progress(db, today)]
    except Exception:
        bosses = []
    return {
        "今天": today.isoformat(),
        "本周从": since,
        "区域": territories,
        "本周经验": week["points"], "满勤天数": f"{week['full_days']} / {week['days']}",
        "副本": bosses,
        "英语陪练": {"次数": eng["n"], "分钟": eng["m"]},
        "这周新收藏": saved,
        "目标": goals,
        "这周没动静的方面": [q["方面"] for q in quiet if not q["有动静"]],
        "学习": {
            "本周新增": rows("SELECT subject, content FROM learning_items WHERE created_at >= ? ORDER BY id", since),
            "今天待复习条数": len(db.due_learning_items(today, limit=1000)),
        },
        "社交": db.people_to_contact(today),
        "小伴答应还没做的": rows("SELECT title, due FROM tasks WHERE done = 0 AND promised_by_ethan = 1 "
                              "ORDER BY due IS NULL, due, id"),
        "本周完成的待办": rows("SELECT title FROM tasks WHERE done = 1 AND done_at >= ? ORDER BY done_at", since),
        "待实现的心愿": [w["quote"] for w in db.open_wishes()],
        "本周实现的心愿": rows("SELECT quote FROM wishes WHERE status = 'done' AND done_at >= ?", since),
        "还没有数据来源的区域": [t["区域"] for t in territories if t["灯"] == "没数据"],
    }


def report_prompt(persona: str, store: MemoryStore, today: date, weekly: bool) -> str:
    ctx = store.load_context(today=today, recent_messages=0)
    kind = "本周周报（周日晚上你主动发给她）" if weekly else "总览（她刚说“看看总览”）"
    return "\n\n".join([
        build_system_prompt(persona, ctx),
        f"## 这次要写{kind}",
        "按这个顺序，每项一两句，用你自己的口吻，像发给她的一条长消息：\n"
        "1. 这周总的：经验、满勤几天、副本推到哪  2. 七个区域挨个过：绿的夸一句，黄的红的说清哪项差了多少、下周怎么补"
        "（健康看训练、蛋白质、睡眠、体重趋势；学习看英语陪练）  3. 社交：该联系谁、谁快过生日  "
        "4. 这周你在关注什么：把“这周新收藏”串成一段，挑一两条建议下周用上（没有就跳过）  "
        "5. 你答应她还没做的事、心愿清单里待实现的",
        "只用下面的事实，没有的不要编。“还没有数据来源的区域”合成一句带过。"
        + ("最后用一两句说说这周你看到的她，给下周一个小期待。然后提 3 件下周重点（从黄的红的区域、副本下一步和她最近着急的事里挑，"
           "每件一句、具体到能排进日程），问她行不行，并在消息最末尾加 [下周重点 第一件 | 第二件 | 第三件]（2026-10-29 她定）。" if weekly else ""),
        "这次不用 [VOICE]、[REACT]、[不回]，也不受平时聊天条数的限制，不超过 20 行。",
        "### 事实",
        json.dumps(collect_facts(store, today), ensure_ascii=False, indent=1),
    ])


def write_report(ethan: Ethan, channel: str, today: date = None, weekly: bool = False,
                 asked: str = None) -> str:
    """生成总览或周报并记入原始对话。asked 为她的原话（总览时）。"""
    today = today or date.today()
    if asked:
        ethan.store.raw.append(channel, "mia", asked)
    system = report_prompt(ethan.persona, ethan.store, today, weekly)
    ask = "（现在给她发本周周报）" if weekly else (asked or "看看总览")
    reply = ethan.router.complete(system, [{"role": "user", "content": ask}])
    text = reply.text.strip()
    if weekly:
        focus.note(ethan.store.db, text, today)
    text = focus.strip(text).strip()[:MAX_TELEGRAM]
    ethan.store.raw.append(channel, "ethan", text, backend=reply.backend, model=reply.model,
                           kind="weekly" if weekly else "overview")
    return text


def main():
    from .channels.telegram import TelegramAPI
    from .cli import build_router

    parser = argparse.ArgumentParser(description="给 Mia 发总览或周报")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--weekly", action="store_true")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    try:
        ethan = Ethan(store, build_router(args.data, args.budget))
        chat_id = ethan.router.settings.get("telegram_owner_chat_id")
        if chat_id is None:
            print("还没绑定 Telegram，跳过")
            return
        text = write_report(ethan, "telegram", weekly=args.weekly)
        TelegramAPI(os.environ["TELEGRAM_BOT_TOKEN"]).call("sendMessage", chat_id=chat_id, text=text)
        print("已发送")
    finally:
        store.close()


if __name__ == "__main__":
    main()
