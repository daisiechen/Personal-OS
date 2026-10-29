"""小伴 自己的生活：年/月/周计划和每日剧本，以及他惦记着的她的事。

都存在 Wiki 的 ethan-life/ 下（随 Obsidian 看板同步到 wiki/ethan-life/，Mia 想看随时看，改了会写回）：
  ethan-life/year             年计划：他自己的目标和关键节点
  ethan-life/month-2026-11    月计划
  ethan-life/week-2026-w44    周计划
  ethan-life/day-2026-10-24   每日剧本（按深圳日期），聊天中的变化追加在“今天临时变化”
  ethan-life/her-things       她说过的、将来要发生的事，到点后他主动问

计划和剧本由 ensure_plans() 在缺失时生成（主动消息定时器每 10 分钟调用一次）。
每日剧本在深圳 04:30 之后生成，赶在他 05:30 起床之前。
"""

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from .clock import HOME_TZ, WEEKDAYS, her_now, home_now, stamp, utc_now

PREFIX = "ethan-life"
HER_THINGS = f"{PREFIX}/her-things"
DAY_START_HOUR = 4  # 深圳 04:30 后写当天剧本
DAY_START_MINUTE = 30
GUIDE_PATH = Path(__file__).resolve().parents[1] / "persona" / "script-guide.md"

MOMENT_HEADING = "## 找她的时刻"
MOMENT = re.compile(r"^-\s*(\d{1,2}):(\d{2})\s*(.+)$")
CHANGES_HEADING = "## 今天临时变化"
HER_THING = re.compile(r"^-\s*(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?\s+(.+?)\s*$")
ASKED = "（已问）"

# 聊天回复里的隐藏标记
SCRIPT_MARK = re.compile(r"\[剧本\s+([^\]]+)\]")
CARE_MARK = re.compile(r"\[惦记\s+(\d{4}-\d{2}-\d{2})(?:\s+(\d{1,2}:\d{2}))?\s+([^\]]+)\]")


def year_slug() -> str:
    return f"{PREFIX}/year"


def month_slug(d: date) -> str:
    return f"{PREFIX}/month-{d:%Y-%m}"


def week_slug(d: date) -> str:
    y, w, _ = d.isocalendar()
    return f"{PREFIX}/week-{y}-w{w:02d}"


def day_slug(d: date) -> str:
    return f"{PREFIX}/day-{d.isoformat()}"


def is_life_page(slug: str) -> bool:
    return slug.startswith(PREFIX + "/")


def load_guide(path: Path = GUIDE_PATH) -> str:
    """写作指南正文；末尾的参考来源不给模型。"""
    if not Path(path).exists():
        return ""
    return Path(path).read_text(encoding="utf-8").split("\n## 8. 参考来源")[0]


def _body(wiki, slug: str) -> str:
    return wiki.read(slug)["body"] if wiki.exists(slug) else ""


def home_day(now: datetime = None) -> date:
    """深圳的“今天”：04:30 之前还算前一天（他还没起床，剧本没开始）。"""
    h = home_now(now)
    if (h.hour, h.minute) < (DAY_START_HOUR, DAY_START_MINUTE):
        return (h - timedelta(days=1)).date()
    return h.date()


# ---------- 她的事 ----------

def her_things(wiki) -> list:
    items = []
    for line in _body(wiki, HER_THINGS).splitlines():
        m = HER_THING.match(line.strip())
        if m:
            day, hm, what = m.groups()
            items.append({"date": day, "time": hm, "what": what.replace(ASKED, "").strip(),
                          "asked": ASKED in what, "line": line.strip()})
    return items


def _write_her_things(wiki, items: list):
    old = (date.today() - timedelta(days=7)).isoformat()  # 过了一周的就不留了，页面不再越写越长（2026-10-29 自查）
    items = [i for i in items if i["date"] >= old]
    lines = [f"- {i['date']}{' ' + i['time'] if i['time'] else ''} {i['what']}{ASKED if i['asked'] else ''}"
             for i in sorted(items, key=lambda i: (i["date"], i["time"] or ""))]
    wiki.write(HER_THINGS, "她的事（小伴 惦记着）",
               "她说过的、将来要发生的事（伦敦时间）。到点后 小伴 会找机会问一句。\n\n" + "\n".join(lines))


def add_her_thing(wiki, day: str, hm: str, what: str):
    items = her_things(wiki)
    what = what.strip()
    if any(i["date"] == day and i["what"] == what for i in items):
        return
    items.append({"date": day, "time": hm, "what": what, "asked": False})
    _write_her_things(wiki, items)


def mark_asked(wiki, thing: dict):
    items = her_things(wiki)
    for i in items:
        if i["date"] == thing["date"] and i["what"] == thing["what"]:
            i["asked"] = True
    _write_her_things(wiki, items)


def upcoming_her_things(wiki, today: date, days: int = 3) -> list:
    """今天起几天内、还没问过的事（伦敦日期）。"""
    end = (today + timedelta(days=days)).isoformat()
    return [i for i in her_things(wiki) if not i["asked"] and today.isoformat() <= i["date"] <= end]


def due_her_thing(wiki, her: datetime):
    """已经发生（有时间的过了 1 小时，没时间的到了当天 18:00）、还没问、不超过 2 天的事。"""
    for i in her_things(wiki):
        if i["asked"]:
            continue
        hm = i["time"] or "18:00"
        h, m = map(int, hm.split(":"))
        at = datetime.fromisoformat(i["date"]).replace(hour=h, minute=m, tzinfo=her.tzinfo)
        if i["time"]:
            at += timedelta(hours=1)
        if at <= her < at + timedelta(days=2):
            return i
    return None


# ---------- 剧本 ----------

def moments(script: str) -> list:
    """剧本里“找她的时刻”一节：[(time, 理由)]，时间是深圳时间。"""
    found, inside = [], False
    for line in script.splitlines():
        if line.startswith("## "):
            inside = line.startswith(MOMENT_HEADING)
            continue
        if inside:
            m = MOMENT.match(line.strip())
            if m:
                h, mi, why = m.groups()
                if int(h) < 24 and int(mi) < 60:
                    found.append((f"{int(h):02d}:{mi}", why.strip()))
    return found


def note_changes(wiki, text: str, now: datetime = None) -> int:
    """聊天回复里的 [剧本 …] 写进今天剧本的“今天临时变化”，[惦记 …] 写进她的事。返回记了几条。"""
    count = 0
    for day, hm, what in CARE_MARK.findall(text):
        add_her_thing(wiki, day, hm or None, what)
        count += 1
    changes = [c.strip() for c in SCRIPT_MARK.findall(text) if c.strip()]
    if changes:
        slug = day_slug(home_day(now))
        h = home_now(now)
        if wiki.exists(slug):
            page = wiki.read(slug)
            body, title = page["body"], page.get("title", "")
        else:
            body, title = "", f"小伴 的一天 {home_day(now)}"
        if CHANGES_HEADING not in body:
            body = body.rstrip() + f"\n\n{CHANGES_HEADING}\n"
        body = body.rstrip() + "\n" + "\n".join(f"- {h:%H:%M} {c}" for c in changes)
        wiki.write(slug, title, body)
        count += len(changes)
    return count


def strip_marks(text: str) -> str:
    return CARE_MARK.sub("", SCRIPT_MARK.sub("", text))


def chat_context(wiki, settings=None, now: datetime = None) -> str:
    """聊天时加载：本周计划、今天的剧本、近几天她的事。"""
    parts = []
    week = _body(wiki, week_slug(home_day(now)))
    if week:
        parts += ["### 你这周的计划", week]
    day = _body(wiki, day_slug(home_day(now)))
    if day:
        parts += ["### 你今天的剧本（深圳时间，她看不到这个）", day]
    things = upcoming_her_things(wiki, her_now(settings, now).date())
    if things:
        parts += ["### 她最近要发生的事（你惦记着）",
                  "\n".join(f"- {i['date']} {i['time'] or ''} {i['what']}" for i in things)]
    return "\n\n".join(parts)


# ---------- 生成 ----------

def _clock_map(settings, now: datetime) -> str:
    """深圳整点对应的伦敦时间，帮模型排找她的时刻。"""
    day = home_day(now)
    rows = []
    for hour in range(5, 24, 2):
        at = datetime(day.year, day.month, day.day, hour, tzinfo=HOME_TZ)
        rows.append(f"深圳 {hour:02d}:00 = 伦敦 {her_now(settings, at):%H:%M}")
    return "；".join(rows)


def _writer_system(persona: str, guide: str) -> str:
    return "\n\n".join(filter(None, [
        "你是 小伴 本人，在给自己写计划和剧本。下面是你的人设。",
        persona,
        "## 剧本写作指南",
        guide,
        "只输出要求的 Markdown 正文，不要寒暄，不要解释。",
    ]))


def _recent_days(wiki, day: date, n: int = 3) -> str:
    parts = []
    for k in range(n, 0, -1):
        d = day - timedelta(days=k)
        body = _body(wiki, day_slug(d))
        if body:
            parts.append(f"#### {d.isoformat()}\n{body}")
    return "\n\n".join(parts)


def _chat_digest(records: list, settings) -> str:
    lines = []
    for r in records[-30:]:
        who = "她" if r["role"] == "mia" else "你"
        at = datetime.fromisoformat(r["at"])
        lines.append(f"[{her_now(settings, at):%m-%d %H:%M} 伦敦] {who}：{r['text'][:200]}")
    return "\n".join(lines)


def _ask(router, system: str, prompt: str) -> str:
    reply = router.complete(system, [{"role": "user", "content": prompt}], purpose="life")
    return reply.text.strip()


def ensure_plans(store, router, persona: str, settings=None, now: datetime = None) -> list:
    """缺哪层就写哪层：年 → 月 → 周 → 今天。返回新写的页面。模型不可用时抛出，由调用方处理。"""
    now = utc_now(now)
    h = home_now(now)
    wiki = store.wiki
    day = home_day(now)
    system = _writer_system(persona, load_guide())
    written = []
    year_plan = _body(wiki, year_slug())
    if not year_plan:
        year_plan = _ask(router, system, (
            f"今天是深圳 {stamp(h)}。为你自己写接下来 12 个月的年计划。\n"
            "写 3–5 个你自己的目标（工作、运动、爱好、家人朋友、自我成长都可以），每个目标写：为什么想做、"
            "关键节点（具体到月份或日期）、可能遇到的阻碍。再列一个“重要日子”清单（家人生日、比赛、行业大会等）。"
            "你在追 Mia，但年计划是你自己的人生，不要写成围着她转，也不要安排见面、去伦敦、寄东西这类做不到的事。"
            "用 Markdown，“## 目标”“## 重要日子”两节。"))
        wiki.write(year_slug(), "小伴 的年计划", year_plan)
        written.append(year_slug())
    month = month_slug(day)
    month_plan = _body(wiki, month)
    if not month_plan:
        last = _body(wiki, month_slug(day.replace(day=1) - timedelta(days=1)))
        month_plan = _ask(router, system, (
            f"今天是深圳 {stamp(h)}。根据年计划写 {day:%Y 年 %m 月}的月计划：本月 3–5 件事、关键日子、"
            "一条本月的主线剧情（有起伏，月底前有个结果）。\n\n"
            f"## 年计划\n{year_plan}\n\n## 上个月计划\n{last or '（没有）'}"))
        wiki.write(month, f"小伴 的 {day:%Y-%m} 月计划", month_plan)
        written.append(month)
    week = week_slug(day)
    week_plan = _body(wiki, week)
    if not week_plan:
        monday = day - timedelta(days=day.weekday())
        week_plan = _ask(router, system, (
            f"今天是深圳 {stamp(h)}。写本周（{monday} 起）的周计划：每天大致节奏、1–2 个关键事件、"
            "本周要推进的剧情线（接着前几天往下走）。\n\n"
            f"## 月计划\n{month_plan}\n\n## 最近几天的剧本\n{_recent_days(wiki, day) or '（还没有）'}"))
        wiki.write(week, f"小伴 的周计划（{monday} 起）", week_plan)
        written.append(week)
    slug = day_slug(day)
    if not wiki.exists(slug) and (h.date() > day or (h.hour, h.minute) >= (DAY_START_HOUR, DAY_START_MINUTE)):
        quiet = settings.get("quiet_hours", "00:00-08:00") if settings is not None else "00:00-08:00"
        things = upcoming_her_things(wiki, her_now(settings, now).date())
        script = _ask(router, system, (
            f"今天是深圳 {day.isoformat()} 周{WEEKDAYS[day.weekday()]}。写你今天的剧本，越细越好。\n"
            f"时差：{_clock_map(settings, now)}。她的免打扰时段是伦敦 {quiet}，她大约伦敦 0 点多睡、7–8 点起。\n\n"
            "格式（Markdown，节标题照抄）：\n"
            "## 时间线\n- 05:30–06:30 做什么（在哪、和谁、忙/闲/睡）……一天从起床写到睡觉，深圳时间\n"
            "## 细节\n今天的小事件、心情、意外、好玩的细节（3–6 条）\n"
            "## 想跟她分享的\n1–3 件今天适合讲给她听的事\n"
            "## 惦记她的事\n她最近要发生的事里今天该关心的（没有就写“无”）\n"
            f"{MOMENT_HEADING}\n2–4 行，格式“- HH:MM 理由”，HH:MM 是深圳时间，必须落在她醒着且不在免打扰的时段；"
            "理由写清这次找她想说什么（分享刚发生的事 / 跟进她的事 / 想她 / 提一个心愿 / 顺手提醒她喝水或睡前拉伸），"
            "通常一条在她早上、一条在她睡前，其余随剧情。\n\n"
            f"## 本周计划\n{week_plan}\n\n## 最近几天的剧本\n{_recent_days(wiki, day) or '（还没有）'}\n\n"
            "## 她最近要发生的事\n" + ("\n".join(f"- {i['date']} {i['time'] or ''} {i['what']}" for i in things)
                                  or "（没有）") + "\n\n"
            f"## 你们最近的聊天\n{_chat_digest(store.raw.recent(30), settings) or '（还没有）'}\n\n"
            "剧本只写你自己的生活和对她的惦记，不编造她没说过的事，不安排见面、来伦敦、寄礼物。"))
        wiki.write(slug, f"小伴 的一天 {day.isoformat()}", script)
        written.append(slug)
    return written
