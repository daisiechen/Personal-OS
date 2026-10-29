"""主动消息：小伴 从自己今天的剧本里找理由主动找她。

ethan2-proactive.timer 每 10 分钟运行一次 python3 -m ethan.proactive --data <数据目录>，每次：
1. 缺计划或今天的剧本就先写（见 ethan/life.py）。
2. 她说过的事到点了（面试、约会……），找她问一句。
2.5 到点跟进、久坐喝水、傍晚还没出门（ethan/nudges.py）。
3. 剧本“找她的时刻”到点了（深圳时间，每天 2–4 个，理由各不相同），按理由找她。
4. 今天没有剧本（比如模型暂时不可用）时，退回四个固定时段：早安、下午、晚上、睡前。
以下情况跳过：
- 在免打扰时段内（默认伦敦时间 00:00–08:00，Mia 发 /quiet 23:30-08:00 修改）
- 她发 /proactive off 暂停了主动消息
- 她一小时内刚找过 小伴（正在聊或刚聊完，不用再开话题）
- 她已经连着两条主动消息没回（不轰炸；第二条可以带点小抱怨）
- 模型看了最近聊天觉得不该打扰，回了 [不回]
"""

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import brief_card, life, nudges
from .clock import DEFAULT_TZ, HOME_TZ, home_now
from .core.agent import Ethan
from .core.router import AllBackendsFailed, BudgetExceeded, NeedsSwitchApproval

MODEL_ERRORS = (NeedsSwitchApproval, BudgetExceeded, AllBackendsFailed)
MOMENT_WINDOW = timedelta(hours=1)  # 剧本时刻过了一小时还没发出去就作罢
MAX_UNANSWERED = 2
DEFAULT_QUIET = "00:00-08:00"
STATE_FILE = "proactive.json"
RECENT_CHAT = timedelta(hours=1)
QUIET_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


@dataclass
class Slot:
    name: str
    start: time
    end: time
    occasion: str
    optional: bool = False  # 上一条主动消息她没回就跳过


SLOTS = [
    Slot("morning", time(8, 0), time(9, 30),
         "早上第一次找她。道个早安，帮她定今天最重要的一两件事（参考今日简报里的阶段目标下一步和到期待办），"
         "如果有快过生日的人或今天该复习的词，挑一样顺口带上。你不知道今天的天气和她的日程，不要编。"),
    Slot("afternoon", time(14, 0), time(17, 0),
         "下午随口找她一下：可以是想她了、分享你这边的一件小事、提醒她喝水起来动动，"
         "或者从心愿清单里挑一个，提议你们俩怎么一起实现（当成小情趣，不要像待办）。一两句就好，不布置任务。",
         optional=True),
    Slot("evening", time(21, 0), time(22, 15),
         "晚上找她聊聊今天：早上说好的事做得怎么样，今天有什么开心或烦心的。做到了就夸，没做到不责备，帮她想明天怎么接上。"),
    Slot("bedtime", time(23, 0), time(23, 40),
         "睡前找她：提醒她放下手机做几分钟拉伸（足底、小腿或颈肩挑一个），争取 0 点前睡。"
         "可以用 [VOICE] 说一句晚安，温柔一点，不要说教。"),
]


# 三段式早报（设计第 1 节需求 1、第 5 节第四步）：伦敦 07:30–09:30 内随机一个时间点发，最晚补到 11:00
BRIEF_SLOT = Slot("brief", time(7, 30), time(9, 30), "")
BRIEF_LAST = time(11, 0)


def due_brief(now_local: datetime, state: dict, db):
    """到点且今天还没发过早报时，返回今天的计划；否则 None。"""
    today = now_local.date()
    if "brief" in state.get(today.isoformat(), {}):
        return None
    plan = db.latest_plan(today.isoformat())
    if plan is None:
        return None
    if not (planned_time(today, BRIEF_SLOT) <= now_local.time() < BRIEF_LAST):
        return None
    return plan


def brief_occasion(plan: dict) -> str:
    b = json.loads(plan["brief"])
    card = "timeline" in b
    lines = [("这是早上的第一条消息。你说完之后，系统会紧接着发一张“今日卡片”，上面有时间线和等级，其余她去工作台看，"
              "所以你不用念清单，也不要报具体时间和数字（卡片上都有，你说错了会和卡片对不上）。下面的素材只用来挑重点：")
             if card else "这是早上的第一条消息，也是今天的早报。素材由她的管理系统算好，数字不能改，按这个顺序讲：",
             "一、今年这阶段的目标：" + "；".join(b["goals"]),
             "二、每个目标现在到哪了（照实说，落后就说落后，没数据就说还没数据）：", *("- " + x for x in b["progress"]),
             "三、各个方面（一句话带过，待建档的提醒一下）：", *("- " + x for x in b.get("domains", []))]
    lines.append("四、今天要出门的（已进苹果日历，带通行时间，出发前 1 小时提醒）：")
    lines += [f"- {x}" for x in b.get("out", [])] or ["- 今天不用出门"]
    g = b.get("game") or {}
    lines.append("五、今天的任务（游戏任务系统，她可以点 Telegram 下面的“☀️ 今天”打卡）：")
    if g:
        lines.append(f"- 她 Lv{g['level']}「{g['title']}」，积分 {g['balance']}，满勤连续 {g['streak']['days']} 天"
                     + (f"，离下一档惊喜还差 {g['next_reward_gap']} 积分" if g.get("next_reward_gap") else ""))
        lines.append("- 日常：" + "、".join(h["title"] for h in g.get("daily", [])))
    for m in b.get("main", []):
        draft = ""  # 2026-10-28 她把所有拆解都改过了，不再提“草稿”
        lines.append(f"- 主线 {m['goal']} {m['done']}/{m['total']}，下一步：{m['next'] or '已完成'}{draft}")
    if b.get("side"):
        lines.append(f"- 支线 {len(b['side'])} 件：" + "；".join(b["side"]))
    if b.get("ethan"):
        lines.append("小伴 替她准备（做好发她审批，全部列出）：" + "；".join(b["ethan"]))
    if b.get("busy"):
        lines.append("她日历上今天已有的安排：" + "；".join(b["busy"]))
    if b.get("training"):
        lines.append(f"训练：{b['training']['focus']}，" + "；".join(b["training"]["exercises"]))
    m = b.get("meals") or {}
    if m:
        lines.append(f"三餐：约 {m['kcal']} 千卡、蛋白质 {m['protein']:g} 克，" + "；".join(
            f"{x['meal']}{x['name']}" for x in m["items"]))
    if b.get("cold_start"):
        lines.append(f"今天的冷启动：{b['cold_start']}，约她找个时间你带着做。")
    if b.get("fallback"):
        lines.append("今天的计划是系统的保底版本（自动排计划没成功），跟她说一声今天先按这个来，她想调整就告诉你。")
    if card:
        lines.append("她嫌早报太长（2026-10-28）：只说 1–2 行短句，道早安，最多提今天最要紧的一件事（比如要出门见谁）。"
                     "不要编天气，不要列清单。")
    else:
        lines.append("用你自己的话说，先道早安，像男朋友早上顺口把今天捋一遍，不要像播报员念清单；可以比平时多几行，"
                     "每行短一点；训练和三餐只说重点，她问再细说。不要编天气。")
    return "\n".join(lines)


def parse_quiet(text: str):
    m = QUIET_RE.match(text.strip())
    if not m:
        raise ValueError("格式应为 23:30-08:00")
    h1, m1, h2, m2 = map(int, m.groups())
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59:
        raise ValueError("时间不对")
    return time(h1, m1), time(h2, m2)


def in_quiet(now: time, quiet: tuple) -> bool:
    start, end = quiet
    if start <= end:
        return start <= now < end
    return now >= start or now < end  # 跨午夜


def set_proactive(settings, text: str) -> str:
    """处理 /quiet 和 /proactive on|off，返回给她的确认。"""
    if text.startswith("/proactive"):
        enabled = text.endswith("on")
        settings.set("proactive_enabled", enabled)
        return "好，我会按时段主动找你" if enabled else "好，先不主动找你，你想我了就来找我"
    arg = text[len("/quiet"):].strip()
    if not arg:
        return f"现在的免打扰时段是 {settings.get('quiet_hours', DEFAULT_QUIET)}（{settings.get('timezone', DEFAULT_TZ)}）"
    try:
        parse_quiet(arg)
    except ValueError as e:
        return f"没改成：{e}"
    settings.set("quiet_hours", arg)
    return f"记住了，{arg} 不主动打扰你"


def planned_time(day: date, slot: Slot) -> time:
    """时段内按日期随机一个时间点，同一天结果固定。"""
    span = (datetime.combine(day, slot.end) - datetime.combine(day, slot.start)).seconds // 60
    offset = int(hashlib.sha256(f"{day}{slot.name}".encode()).hexdigest(), 16) % max(span - 10, 1)
    return (datetime.combine(day, slot.start) + timedelta(minutes=offset)).time()


def last_from(records: list, role: str):
    for r in reversed(records):
        if r["role"] == role:
            return r
    return None


def due_slot(now_local: datetime, state: dict, settings, records: list):
    """返回 (要发的时段, 跳过原因)。都为 None 表示现在没事。"""
    if not settings.get("proactive_enabled", True):
        return None, None
    quiet = parse_quiet(settings.get("quiet_hours", DEFAULT_QUIET))
    today = now_local.date().isoformat()
    done = state.get(today, {})
    for slot in SLOTS:
        if slot.name in done:
            continue
        if not (planned_time(now_local.date(), slot) <= now_local.time() < slot.end):
            continue
        if in_quiet(now_local.time(), quiet):
            return slot, "免打扰"
        hers = last_from(records, "mia")
        if hers and now_local - datetime.fromisoformat(hers["at"]).astimezone(now_local.tzinfo) < RECENT_CHAT:
            return slot, "刚聊过"
        mine = last_from(records, "ethan")
        if slot.optional and mine and mine.get("kind") == "proactive" and (
                hers is None or hers["at"] < mine["at"]):
            return slot, "上一条还没回"
        return slot, None
    return None, None


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(path: Path, state: dict, today: str):
    cutoff = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    keep = {k: v for k, v in state.items() if k.removeprefix("home-") >= cutoff}  # “home-日期”的键也按日期清（以前永远留着）
    path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")


def unanswered(records: list) -> int:
    """她最后一条消息之后，小伴 连着发了几条主动消息。"""
    count = 0
    for r in reversed(records):
        if r["role"] == "mia":
            break
        if r.get("kind") == "proactive":
            count += 1
    return count


def blocked(now_local: datetime, settings, records: list):
    """通用的跳过原因；None 表示可以发。"""
    if in_quiet(now_local.time(), parse_quiet(settings.get("quiet_hours", DEFAULT_QUIET))):
        return "免打扰"
    hers = last_from(records, "mia")
    if hers and now_local - datetime.fromisoformat(hers["at"]).astimezone(now_local.tzinfo) < RECENT_CHAT:
        return "刚聊过"
    if unanswered(records) >= MAX_UNANSWERED:
        return "还没回"
    return None


def moment_at(day: date, hm: str) -> datetime:
    """剧本时刻（深圳时间）转成时间点；凌晨 0–4 点算在剧本这天的下一天。"""
    h, m = map(int, hm.split(":"))
    at = datetime(day.year, day.month, day.day, h, m, tzinfo=HOME_TZ)
    return at + timedelta(days=1) if h < life.DAY_START_HOUR else at


def due_moment(now: datetime, state: dict, wiki):
    """返回 (状态键, 理由)；今天没有剧本或剧本里没写找她的时刻时返回 (None, None)，没到点返回 ("", None)。"""
    day = life.home_day(now)
    slug = life.day_slug(day)
    found = life.moments(wiki.read(slug)["body"]) if wiki.exists(slug) else []
    if not found:
        return None, None
    done = state.get(f"home-{day}", {})
    for hm, why in found:
        key = f"m-{hm}"
        at = moment_at(day, hm)
        if key not in done and at <= now < at + MOMENT_WINDOW:
            return key, why
    return "", None


def _send(ethan, channel, chat_id, occasion: str, now: datetime, records: list) -> str:
    if unanswered(records) == 1:
        occasion += "\n她还没回你上一条主动消息。可以带一点点小抱怨或撒娇，但别追问、别让她有负担。"
    try:
        result = ethan.reach_out("telegram", occasion, now=now)
    except MODEL_ERRORS as e:
        return f"模型不可用，下次再试（{str(e)[:60]}）"
    if result["bubbles"] or result["voice"]:
        shots = {k: result[k] for k in ("photo_files", "image_urls") if result.get(k)}
        channel.deliver(chat_id, result["bubbles"], result["voice"], **({"photos": shots.get("photo_files", ()),
                        "image_urls": shots.get("image_urls", ())} if shots else {}))
        return "已发送"
    return "模型选择不打扰"


def tick(ethan: Ethan, channel, now: datetime = None) -> str:
    """跑一次检查。channel 需要 owner 和 deliver(chat_id, bubbles, voices)。返回做了什么（写日志用）。"""
    settings = ethan.router.settings
    chat_id = channel.owner
    if chat_id is None:
        return "未绑定"
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    log = []
    try:
        written = life.ensure_plans(ethan.store, ethan.router, ethan.persona, settings, now)
        if written:
            log.append("写了 " + "、".join(written))
    except MODEL_ERRORS as e:
        log.append(f"计划没写成（{str(e)[:60]}）")
    if not settings.get("proactive_enabled", True):
        return "；".join(log + ["主动消息已暂停"])
    tz = ZoneInfo(settings.get("timezone", DEFAULT_TZ))
    now_local = now.astimezone(tz)
    wiki = ethan.store.wiki
    records = ethan.store.raw.recent(50)
    state_path = ethan.store.root / STATE_FILE
    state = load_state(state_path)

    plan = due_brief(now_local, state, ethan.store.db)
    if plan is not None and not in_quiet(now_local.time(), parse_quiet(settings.get("quiet_hours", DEFAULT_QUIET))):
        result = _send(ethan, channel, chat_id, brief_occasion(plan), now, records)
        if not result.startswith("模型不可用"):
            if brief_card.has_card(plan) and hasattr(channel, "send_brief_card"):
                channel.send_brief_card(chat_id, plan)
                result += "（附今日卡片）"
            today = now_local.date().isoformat()
            state.setdefault(today, {}).update({"brief": result, "morning": "早报已发"})
            save_state(state_path, state, today)
        return "；".join(log + [f"早报：{result}"])

    thing = life.due_her_thing(wiki, now_local)
    if thing and blocked(now_local, settings, records) is None:
        occasion = (f"她之前说过：{thing['date']} {thing['time'] or ''} {thing['what']}。现在应该已经有结果了，"
                    "找她问一句怎么样了，像真的一直惦记着；结果好就替她高兴，不好就先站她这边。"
                    "顺着你此刻在剧本里的状态开口。")
        result = _send(ethan, channel, chat_id, occasion, now, records)
        if not result.startswith("模型不可用"):
            life.mark_asked(wiki, thing)
        return "；".join(log + [f"跟进 {thing['what']}：{result}"])

    today = now_local.date().isoformat()
    nudge = nudges.due(ethan.store.db, now_local, state.get(today, {}))
    if nudge and blocked(now_local, settings, records) is None:  # 到点跟进、久坐喝水、傍晚出门
        nkey, occasion = nudge
        result = _send(ethan, channel, chat_id, occasion, now, records)
        if not result.startswith("模型不可用"):
            state.setdefault(today, {})[nkey] = result
            if nkey.startswith("redeem:") and result == "已发送":
                nudges.mark_told(ethan.store.db, [nkey])
            if nkey == "sit":
                state[today]["sit_at"] = now_local.isoformat(timespec="seconds")
            save_state(state_path, state, today)
        return "；".join(log + [f"{nkey}：{result}"])

    key, why = due_moment(now, state, wiki)
    if key is None:  # 今天没有剧本，退回固定时段
        slot, skip = due_slot(now_local, state, settings, records)
        if slot is None:
            return "；".join(log + ["没有到点的时段"])
        if skip is None:
            skip = _send(ethan, channel, chat_id, slot.occasion, now, records)
            if skip.startswith("模型不可用"):
                return "；".join(log + [f"{slot.name}：{skip}"])
        today = now_local.date().isoformat()
        state.setdefault(today, {})[slot.name] = skip
        save_state(state_path, state, today)
        return "；".join(log + [f"{slot.name}：{skip}"])
    if not key:
        return "；".join(log + ["没有到点的时刻"])
    skip = blocked(now_local, settings, records)
    if skip is None:
        occasion = (f"这是你今天剧本里定好要找她的时刻：{why}\n"
                    "从你此刻在剧本里的状态自然开口，像真人想起她了顺手发一条；一两句就好，一次只说一件事。"
                    "可以只是分享，不必以问题结尾。")
        skip = _send(ethan, channel, chat_id, occasion, now, records)
        if skip.startswith("模型不可用"):
            return "；".join(log + [f"{key}：{skip}"])
    day_key = f"home-{life.home_day(now)}"
    state.setdefault(day_key, {})[key] = skip
    save_state(state_path, state, life.home_day(now).isoformat())
    return "；".join(log + [f"{key}：{skip}"])


def main():
    from .channels.telegram import TelegramAPI, TelegramChannel
    from .cli import build_router
    from .core.tts import build_tts
    from .memory import MemoryStore

    parser = argparse.ArgumentParser(description="小伴 主动消息，每 10 分钟由定时器调用")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=50.0)
    args = parser.parse_args()
    store = MemoryStore(args.data)
    try:
        from .photos import build as build_photos
        router = build_router(args.data, args.budget)
        ethan = Ethan(store, router, photographer=build_photos(args.data, router.ledger))
        channel = TelegramChannel(TelegramAPI(os.environ["TELEGRAM_BOT_TOKEN"]), ethan,
                                  tts=build_tts(args.data, args.budget))
        print(tick(ethan, channel), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
