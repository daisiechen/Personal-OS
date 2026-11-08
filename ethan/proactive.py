"""主动消息：小伴 从自己今天的剧本里找理由主动找她。

ethan2-proactive.timer 每 10 分钟运行一次 python3 -m ethan.proactive --data <数据目录>，每次：
1. 缺计划或今天的剧本就先写（见 ethan/life.py）。
2. 她说过的事到点了（面试、约会……），找她问一句。
2.5 到点跟进、久坐喝水、傍晚还没出门、下午傍晚的“聊聊”（知识点 + 补缺口一问，ethan/nudges.py）。
3. 剧本“找她的时刻”到点了（深圳时间，每天 2–4 个，理由各不相同），按理由找她。
4. 今天没有剧本（比如模型暂时不可用）时，退回四个固定时段：早安、下午、晚上、睡前。
发不发只由 gate() 按打扰登记表 ethan/nudge_registry.py 的 RULES 判断（唯一的关口，docs/design/nudges.md）：
免打扰（默认本地时间 00:00–08:00，Mia 发 /quiet 23:30-08:00 修改）、/proactive off、刚聊过、连着没回、
几点到几点、只问一次、两次隔多久，每一类守哪几条写在表里（例外写原因）。真发出去只走 say()：先记“发送中”再发、
发完记结果和发送记录、该“永不再说”的记进 nudge_once。模型看了最近聊天觉得不该打扰，回了 [不回]，也不发。
Grok 回报转告（relay_grok）、DemoApp 举报提醒（ethan/fancy_moderation.py）、手动总览（ethan/report.py）也过 gate()。
"""

import argparse
import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from . import brief_card, life, nudge_registry, nudges
from .clock import DEFAULT_TZ, HOME_TZ, her_tz, home_now
from .core.agent import Ethan
from .core.router import AllBackendsFailed, BudgetExceeded, NeedsSwitchApproval

MODEL_ERRORS = (NeedsSwitchApproval, BudgetExceeded, AllBackendsFailed)
MOMENT_WINDOW = timedelta(hours=1)  # 剧本时刻过了一小时还没发出去就作罢
MAX_UNANSWERED = 2  # 规则 unanswered="today"：今天连着超过这么多条没回就不发
DEFAULT_QUIET = "00:00-08:00"
STATE_FILE = "proactive.json"
RECENT = {"her1h": ("mia", timedelta(hours=1)), "any2h": (None, timedelta(hours=2))}  # 规则 recent 的两种“刚聊过”
QUIET_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})$")


@dataclass
class Slot:
    name: str
    occasion: str

    @property
    def start(self) -> time:  # 时段只写在登记表 RULES 里
        return nudge_registry.window(self.name)[0]

    @property
    def end(self) -> time:
        return nudge_registry.window(self.name)[1]


SLOTS = [
    Slot("morning",
         "早上第一次找她。道个早安，帮她定今天最重要的一两件事（参考今日简报里的阶段目标下一步和到期待办），"
         "如果有快过生日的人或今天该复习的词，挑一样顺口带上。你不知道今天的天气和她的日程，不要编。"),
    Slot("afternoon",
         "下午随口找她一下：可以是想她了、分享你这边的一件小事、提醒她喝水起来动动，"
         "或者从心愿清单里挑一个，提议你们俩怎么一起实现（当成小情趣，不要像待办）。一两句就好，不布置任务。"),
    Slot("evening",
         "晚上找她聊聊今天：早上说好的事做得怎么样，今天有什么开心或烦心的。做到了就夸，没做到不责备，帮她想明天怎么接上。"),
    Slot("bedtime",
         "睡前找她：提醒她放下手机做几分钟拉伸（足底、小腿或颈肩挑一个），争取 0 点前睡。"
         "可以用 [VOICE] 说一句晚安，温柔一点，不要说教。"),
]


# 三段式早报（设计第 1 节需求 1、第 5 节第四步）：本地 07:30–09:30 内随机一个时间点发，最晚补到 11:00（RULES["brief"] 07:30–11:00）
BRIEF_PICK = (time(7, 30), time(9, 30))  # 在这段里随机挑发的时间点


def brief_time(day: date) -> time:
    """今天早报发的时间点（07:30–09:30 里按日期随机，同一天固定）。"""
    return planned_time(day, "brief", *BRIEF_PICK)


def due_brief(now_local: datetime, state: dict, db):
    """到点且今天还没发过早报时，返回今天的计划；否则 None（时段、免打扰由 gate 判断）。"""
    today = now_local.date()
    if "brief" in state.get(today.isoformat(), {}):
        return None
    plan = db.latest_plan(today.isoformat())
    if plan is None:
        return None
    if now_local.time() < brief_time(today):
        return None
    return plan


def brief_occasion(plan: dict) -> str:
    b = json.loads(plan["brief"])
    card = "timeline" in b
    lines = [("这是早上的第一条消息。你说完之后，系统会紧接着发一张“昨日卡”，上面有时间线和等级，其余她去工作台看，"
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
        draft = ""  # 把所有拆解都改过了，不再提“草稿”
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
        lines.append("她嫌早报太长：只说 1–2 行短句，道早安，最多提今天最要紧的一件事（比如要出门见谁）。"
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


def planned_time(day: date, slot, start: time = None, end: time = None) -> time:
    """时段内按日期随机一个时间点，同一天结果固定。slot：Slot 或名字（给了名字要给 start、end）。"""
    name = slot if isinstance(slot, str) else slot.name
    start, end = (start, end) if start else (slot.start, slot.end)
    span = (datetime.combine(day, end) - datetime.combine(day, start)).seconds // 60
    offset = int(hashlib.sha256(f"{day}{name}".encode()).hexdigest(), 16) % max(span - 10, 1)
    return (datetime.combine(day, start) + timedelta(minutes=offset)).time()


def last_from(records: list, role: str):
    for r in reversed(records):
        if r["role"] == role:
            return r
    return None


def due_slot(now_local: datetime, state: dict, settings, records: list):
    """返回 (要发的时段, 跳过原因)。都为 None 表示现在没事。跳过原因来自 gate（挡住的当天记下、不再试）。"""
    today = now_local.date().isoformat()
    done = state.get(today, {})
    for slot in SLOTS:
        if slot.name in done:
            continue
        if not (planned_time(now_local.date(), slot) <= now_local.time() < slot.end):
            continue
        return slot, gate(slot.name, now_local, settings, records)
    return None, None


def load_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_state(path: Path, state: dict, today: str):
    cutoff = (date.fromisoformat(today) - timedelta(days=7)).isoformat()
    keep = {k: v for k, v in state.items() if k.removeprefix("home-") >= cutoff}  # “home-日期”的键也按日期清（以前永远留着）
    path.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")


# 先记“发送中”再发、发完再记结果（Codex 评审 P2 第 12 条，11-05）：以前先发后记，发出去了、记之前进程被杀，下一轮会再发一遍。
# 重启后看到还是“发送中”的：不知道发没发出去，改成“结果未知”、记日志，当天不再发同一个键（只提一次的那几类也记成提过）。
SENDING = "发送中"
UNKNOWN = "结果未知（发送时中断，不重发）"
# 发出去要记进 nudge_once 的（换一天也不重发）：登记表里 once="ever" 的那几类（11-06 起由表生成，不再手写一份）
ONCE_PREFIXES = nudge_registry.once_prefixes()


def begin_send(path: Path, state: dict, day_key: str, key: str, today: str, **extra):
    """发之前先落盘：state[day_key][key] = 发送中（extra 一起写）。"""
    state.setdefault(day_key, {}).update({key: SENDING, **extra})
    save_state(path, state, today)


def undo_send(path: Path, state: dict, day_key: str, key: str, today: str, *extra):
    """没发出去（模型不可用）：撤掉“发送中”，下次照常再试。"""
    for k in (key, *extra):
        state.get(day_key, {}).pop(k, None)
    save_state(path, state, today)


def settle_unknown(state: dict, db) -> list:
    """把上次留下的“发送中”改成“结果未知”，返回这些键；只提一次的记进 nudge_once，换一天也不重发。"""
    found = []
    for items in state.values():
        if not isinstance(items, dict):
            continue
        for k, v in items.items():
            if v == SENDING:
                items[k] = UNKNOWN
                found.append(k)
    once = [k for k in found if k.startswith(ONCE_PREFIXES)]
    if once:
        nudges.mark_told(db, once)
    return found


def unanswered(records: list, since: datetime = None) -> int:
    """她最后一条消息之后，小伴 连着发了几条主动消息。since：只数这之后发的（新的一天重新数）。"""
    count = 0
    for r in reversed(records):
        if r["role"] == "mia":
            break
        if r.get("kind") == "proactive" and (since is None or datetime.fromisoformat(r["at"]) >= since):
            count += 1
    return count


def _ago(now_local: datetime, r: dict) -> timedelta:
    return now_local - datetime.fromisoformat(r["at"]).astimezone(now_local.tzinfo)


def gate(key: str, now_local: datetime, settings, records=(), db=None, done=None):
    """唯一的关口：这一条现在能不能发。返回挡住的原因，None 表示能发。
    按 nudge_registry.RULES 里这一类的规则判断（顺序：登记 → /proactive off → 免打扰 → 时段 → 刚聊过 → 没回 → 说过 → 间隔）。
    records：最近的聊天（不看刚聊过、没回的类可以不给）；db / done：看“说过没有”和“两次隔多久”（不给就不看，比如 DemoApp 接收口）。"""
    r = nudge_registry.rule(key)
    if r is None or nudge_registry.lookup(key) is None:
        return "没登记"
    if r.switch and not settings.get("proactive_enabled", True):
        return "主动消息已暂停"
    if r.quiet and in_quiet(now_local.time(), parse_quiet(settings.get("quiet_hours", DEFAULT_QUIET))):
        return "免打扰"
    if not nudge_registry.in_window(key, now_local):
        return "不在时段"
    records = list(records or ())
    if r.recent:
        role, span = RECENT[r.recent]
        last = last_from(records, role) if role else (records[-1] if records else None)
        if last and _ago(now_local, last) < span:
            return "刚聊过"
    if r.unanswered == "today":
        # 只数今天发的（前一天没回，第二天他就一整天不说话，和剧本对不上）
        morning = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        if unanswered(records, since=morning) > MAX_UNANSWERED:
            return "还没回"
    elif r.unanswered == "last":
        hers, mine = last_from(records, "mia"), last_from(records, "ethan")
        if mine and mine.get("kind") == "proactive" and (hers is None or hers["at"] < mine["at"]):
            return "上一条还没回"
    if db is not None:
        if not nudges.fresh(db, key, done):
            return "说过了"
        last = nudges.last_sent(db, key) if r.gap else None
        if last and timedelta(0) <= now_local - last < r.gap:
            return "隔得太近"
    return None


def blocked(now_local: datetime, settings, records: list):
    """老接口（测试还在用）：按一类最常见的规则（剧本时刻 m-）问 gate。"""
    return gate("m-", now_local, settings, records)


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


def review_due(ethan, now_local: datetime, calendar=None):
    """每周复盘该不该开场；顺手把每周日的重复日程写进苹果日历（只写一次）。"""
    from . import season, weekreview
    try:
        weekreview.ensure_event(calendar, ethan.router.settings, now_local.date())
    except Exception as e:
        print(f"[复盘] 日程没写进日历：{type(e).__name__} {str(e)[:80]}", flush=True)
    if now_local.hour < 8:
        return None
    s, _ = season.current(ethan.router.settings)
    return weekreview.due(ethan.store.db, now_local, date.fromisoformat(s["end"]), calendar)


def _send(ethan, channel, chat_id, occasion: str, now: datetime, records: list, text: str = None) -> str:
    """真的发出去（只给 say 和 relay_grok 用）：occasion 交给 小伴 用自己的话说；text 是写好的（复盘开场）直接发。"""
    if text is not None:
        channel.deliver(chat_id, [text], [])
        return "已发送"
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


@dataclass
class Round:
    """一轮检查里发消息要用的东西（say 用）。"""
    ethan: object
    channel: object
    now: datetime
    records: list
    state_path: Path
    state: dict


def say(rd: Round, key: str, occasion: str = "", day_key: str = None, text=None) -> str:
    """发一条主动消息的唯一出口（gate 放行之后）：先记“发送中” → 发 → 记结果；真发出去了记发送记录，
    登记表 once="ever" 的记进 nudge_once。模型不可用就撤掉“发送中”，下一轮再试。text：可调用，返回写好的话（复盘开场）。"""
    day_key = day_key or rd.now.astimezone(_tz(rd.ethan)).date().isoformat()
    save_day = day_key.removeprefix("home-")
    begin_send(rd.state_path, rd.state, day_key, key, save_day)
    try:
        body = text() if callable(text) else text
    except MODEL_ERRORS as e:
        undo_send(rd.state_path, rd.state, day_key, key, save_day)
        return f"模型不可用，下次再试（{str(e)[:60]}）"
    result = _send(rd.ethan, rd.channel, rd.channel.owner, occasion, rd.now, rd.records, text=body)
    if result.startswith("模型不可用"):
        undo_send(rd.state_path, rd.state, day_key, key, save_day)
        return result
    rd.state.setdefault(day_key, {})[key] = result
    if result == "已发送":
        nudges.log_sent(rd.ethan.store.db, key, rd.now)
        if key.startswith(ONCE_PREFIXES):
            nudges.mark_told(rd.ethan.store.db, [key])
    save_state(rd.state_path, rd.state, save_day)
    return result


def _tz(ethan):
    return her_tz(ethan.router.settings)


def relay_grok(ethan, channel, now: datetime = None) -> str:
    """Grok 替 小伴 办完她交代的事，回报到了由 小伴 告诉她（ethan/relay.py）；Telegram 轮询每一圈调一次。
    过 gate（1：守免打扰，夜里到的早上 8 点后再说）；挡住的不记“转告过”，下一圈再看。"""
    from . import relay
    root = ethan.store.root
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    results = relay.pending(root, now)
    if not results or channel.owner is None:
        return ""
    key = f"grok-relay:{results[-1]['at']}"
    records = ethan.store.raw.recent(50)
    why = gate(key, now.astimezone(_tz(ethan)), ethan.router.settings, records)
    if why:
        return f"{key}：{why}"
    relay.mark(root, results[-1]["at"])  # 先记下，模型出错也不会反复转告
    result = _send(ethan, channel, channel.owner, relay.occasion(results), now, [])  # 转告结果不带“你还没回我”的小抱怨
    if result == "已发送":
        nudges.log_sent(ethan.store.db, key, now)
    return f"{key}：{result}"


def tick(ethan: Ethan, channel, now: datetime = None, calendar=None) -> str:
    """跑一次检查。channel 需要 owner 和 deliver(chat_id, bubbles, voices)。返回做了什么（写日志用）。
    calendar：苹果日历（每周复盘那一次她挪没挪，ethan/weekreview.py）；没有就按每周日 21:00。"""
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
    if not settings.get("proactive_enabled", True):  # gate 里同一条规则（switch）；这一轮的都受它管，提前返回省得白算
        return "；".join(log + ["主动消息已暂停"])
    tz = her_tz(settings)
    now_local = now.astimezone(tz)
    wiki = ethan.store.wiki
    records = ethan.store.raw.recent(50)
    state_path = ethan.store.root / STATE_FILE
    state = load_state(state_path)
    stale = settle_unknown(state, ethan.store.db)
    if stale:
        save_state(state_path, state, now_local.date().isoformat())
        print(f"[proactive] 上次发送中断，结果未知、不重发：{'、'.join(stale)}", flush=True)
        log.append(f"结果未知（不重发）：{'、'.join(stale)}")

    try:  # 答应的事：聊天里已经说过的，到点不再报（ethan/prep.py note_known）
        from . import prep
        prep.note_known(ethan.store.db, records)
    except Exception as e:
        log.append(f"答应的事核对没成：{type(e).__name__} {str(e)[:80]}")

    rd = Round(ethan, channel, now, records, state_path, state)
    today = now_local.date().isoformat()
    plan = due_brief(now_local, state, ethan.store.db)
    if plan is not None and gate("brief", now_local, settings, records, ethan.store.db, state.get(today, {})) is None:
        result = say(rd, "brief", brief_occasion(plan), today)
        if not result.startswith("模型不可用"):
            if brief_card.has_card(plan) and hasattr(channel, "send_brief_card"):
                channel.send_brief_card(chat_id, plan)
                result += "（附昨日卡）"
            state.setdefault(today, {}).update({"brief": result, "morning": "早报已发"})
            save_state(state_path, state, today)
        return "；".join(log + [f"早报：{result}"])

    got = review_due(ethan, now_local, calendar)
    if got:  # 每周日 21:00 约好的复盘：到点就开场，刚聊过也照样（像开会），只守免打扰（规则见 RULES["review:"]）
        rkey = f"review:{got['day']}"
        why = gate(rkey, now_local, settings, records, ethan.store.db, state.get(today, {}))
        if why:
            return "；".join(log + [f"复盘：{why}，跳过"])
        from . import weekreview
        result = say(rd, rkey, day_key=today, text=lambda: weekreview.opener(ethan, got["day"], got["layers"]))
        if result.startswith("模型不可用"):
            return "；".join(log + [f"复盘：{result}"])
        return "；".join(log + [f"复盘开场（{'、'.join(got['layers'])}）：{result}"])

    thing = life.due_her_thing(wiki, now_local)
    tkey = f"ask:{thing['date']}:{thing['what']}" if thing else ""
    if thing and tkey in state.get(today, {}):  # 上次发了、没来得及记“问过”
        life.mark_asked(wiki, thing)
        thing = None
    if thing and gate(tkey, now_local, settings, records, ethan.store.db, state.get(today, {})) is None:
        occasion = (f"她之前说过：{thing['date']} {thing['time'] or ''} {thing['what']}。现在应该已经有结果了，"
                    "找她问一句怎么样了，像真的一直惦记着；结果好就替她高兴，不好就先站她这边。"
                    "顺着你此刻在剧本里的状态开口。")
        result = say(rd, tkey, occasion, today)
        if not result.startswith("模型不可用"):
            life.mark_asked(wiki, thing)
        return "；".join(log + [f"跟进 {thing['what']}：{result}"])

    try:  # 傍晚给主页留言分一次档（要她回的进待办，每天最多 3 条）
        from . import publicsite
        publicsite.triage_due(ethan.store.db, ethan.router, now_local)
    except Exception as e:
        log.append(f"留言分档没成：{type(e).__name__} {str(e)[:120]}")
    nudge = None
    for got in nudges.candidates(ethan.store.db, now_local, state.get(today, {})):
        # 按优先顺序看，发第一个过得了关口的；排前面的被挡住，后面的照样轮得到（Codex 评审 P2 第 12 条）
        why = gate(got[0], now_local, settings, records, ethan.store.db, state.get(today, {}))
        if why == "没登记":  # 没登记的不准发（打扰登记表，ethan/nudge_registry.py）
            log.append(f"{got[0]}：没登记，不发")
        if why is None:
            nudge = got
            break
    if nudge:  # 到点跟进、久坐喝水、傍晚出门、晚上收工……
        nkey, occasion = nudge
        result = say(rd, nkey, occasion, today)
        if nkey.startswith(nudges.CHAT_PREFIX) and result == "已发送":
            nudges.after_lesson_chat(ethan.store.db, nkey)
        return "；".join(log + [f"{nkey}：{result}"])

    key, why = due_moment(now, state, wiki)
    if key is None:  # 今天没有剧本，退回固定时段
        slot, skip = due_slot(now_local, state, settings, records)
        if slot is None:
            return "；".join(log + ["没有到点的时段"])
        if skip is None:
            skip = say(rd, slot.name, slot.occasion, today)
            return "；".join(log + [f"{slot.name}：{skip}"])
        state.setdefault(today, {})[slot.name] = skip  # 挡住的当天记下、不再试
        save_state(state_path, state, today)
        return "；".join(log + [f"{slot.name}：{skip}"])
    if not key:
        return "；".join(log + ["没有到点的时刻"])
    day_key = f"home-{life.home_day(now)}"
    skip = gate(key, now_local, settings, records, ethan.store.db, state.get(day_key, {}))
    if skip is None:
        occasion = (f"这是你今天剧本里定好要找她的时刻：{why}\n"
                    "从你此刻在剧本里的状态自然开口，像真人想起她了顺手发一条；一两句就好，一次只说一件事。"
                    "可以只是分享，不必以问题结尾。")
        skip = say(rd, key, occasion, day_key)
        return "；".join(log + [f"{key}：{skip}"])
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
        from .icloud import build_calendar
        cal = build_calendar()
        try:  # 让 Grok 查本周新闻（知识点用）和这周末去哪（周五推荐用），收回它的回报（ethan/lessons.py、ethan/weekend.py，11-04）
            from . import lessons, weekend
            from .clock import her_now
            from .grok import build_grok
            grok, here = build_grok(args.data), her_now(router.settings)
            lessons.collect_news(store), weekend.collect(store)
            if lessons.ask_news(store.db, grok, here.date()):
                print("[lessons] 让 Grok 查了本周新闻", flush=True)
            if weekend.ask(store.db, grok, here):
                print("[weekend] 让 Grok 查了这周末去哪", flush=True)
        except Exception as e:
            print(f"[grok-weekly] 没派成：{type(e).__name__} {str(e)[:200]}", flush=True)
        print(tick(ethan, channel, calendar=cal), flush=True)
        try:  # DemoApp 早晚常规提醒：免打扰里到的先存着，这里过了关口就补发（2，ethan/fancy_moderation.py）
            from . import fancy_moderation
            sent = fancy_moderation.Alerts(args.data, fancy_moderation.telegram_notifier(args.data)).flush_held()
            if sent:
                print(f"[fancy] 免打扰结束，补发：{'、'.join(sent)}", flush=True)
        except Exception as e:
            print(f"[fancy] 补发没成：{type(e).__name__} {str(e)[:200]}", flush=True)
        try:  # 额度恢复后自动补：昨天的日记、聊天整理，等着出的昨日卡（ethan/catchup.py）；另起进程，不拖慢这一轮
            from . import catchup
            todo = catchup.kick(store, router.settings)
            if todo:
                print(f"[catchup] 额度正常，后台补：{'、'.join(todo)}", flush=True)
        except Exception as e:
            print(f"[catchup] 没查成：{type(e).__name__} {str(e)[:200]}", flush=True)
        try:  # 小伴 聊天里记下的以后的约，10 分钟内写进苹果日历
            from . import events
            if cal is not None:  # 每天整理一次她日历里导入的事件（回“开”；打开了才跑，ethan/caltidy.py）
                try:
                    from . import caltidy
                    tidy = caltidy.daily(store, router, cal)
                    if tidy:
                        print(f"[caltidy] {tidy}", flush=True)
                except Exception as e:
                    print(f"[caltidy] 没整理成：{type(e).__name__} {str(e)[:200]}", flush=True)
            if cal is not None:  # 日历查重报告（只读，每小时一次写进数据目录）
                try:
                    n = events.write_dupes_report(cal, store.root)
                    if n > 0:
                        print(f"[events] 日历里疑似重复 {n} 对，见 calendar-dupes.json", flush=True)
                except Exception as e:
                    print(f"[events] 日历查重没成：{type(e).__name__} {str(e)[:200]}", flush=True)
            if cal is not None:  # 一次性清掉已经写重的，跑过就不再跑
                gone = events.dedupe_existing(store.db, cal)
                if gone:
                    print(f"[events] 日历去重：{gone}", flush=True)
            if cal is not None:  # 见面前提醒：今天、明天日历上和人物卡里的人的约（ethan/meetprep.py，借鉴 10），半小时扫一次
                try:
                    from . import meetprep
                    from .clock import her_today
                    meetprep.scan(store.db, cal, her_today(router.settings))
                except Exception as e:
                    print(f"[meetprep] 没扫成：{type(e).__name__} {str(e)[:200]}", flush=True)
            n = events.sync(store.db, cal)
            if n:
                print(f"[events] 写进苹果日历 {n} 条", flush=True)
            if cal is not None:  # 今天计划里的事和她后来约的撞了：记下来，小伴 下一轮去问
                from .clock import her_now
                now = her_now(router.settings)
                new = events.find_plan_clashes(store.db, now.date(), cal.busy(now.date()), now.strftime("%H:%M"))
                if new:
                    print(f"[events] 计划和日历撞了：{[(c['title'], c['with']) for c in new]}", flush=True)
        except Exception as e:
            print(f"[events] 没写进日历：{e}", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
