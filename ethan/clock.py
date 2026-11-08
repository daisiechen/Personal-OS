"""时间：小伴 设定住在深圳。服务器在洛杉矶（系统时区 UTC），任何日期都不能用服务器本地时间算。"""

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = os.environ.get("ETHAN_TZ", "").strip() or "Europe/London"  # 开源版用户在 ethan.env 里改
LOCAL = ZoneInfo(DEFAULT_TZ)
HOME_TZ = ZoneInfo("Asia/Shanghai")  # 小伴 在深圳
WEEKDAYS = "一二三四五六日"
# 她的一天从凌晨 5 点算起（工作时长、工作狂指数、作息统计共用；工作时长，11-06 决定 9：作息也按 5 点）。
# 只管“这一格 / 这一晚算哪天”的统计口径；日期、截止日、“今天”照旧按 0 点（her_today）。小伴 深圳剧本的 4 点另算（life.py）。
DAY_START_HOUR = 5


def tz_name(settings=None) -> str:
    """她的时区名（IANA，比如 Europe/London）：设置里的 timezone 优先，没有就用 ETHAN_TZ / 默认。写日历 TZID、显示用。"""
    return (settings.get("timezone", DEFAULT_TZ) if settings is not None else DEFAULT_TZ) or DEFAULT_TZ


def her_tz(settings=None) -> ZoneInfo:
    return ZoneInfo(tz_name(settings))


def utc_now(now: datetime = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def her_now(settings=None, now: datetime = None) -> datetime:
    return utc_now(now).astimezone(her_tz(settings))


def her_today(settings=None, now: datetime = None):
    """她的“今天”（按她的时区，0 点起算；docs/design/system-rules.md 第 5 节）。不要用 date.today()：那是服务器的日期。"""
    return her_now(settings, now).date()


def her_day_start_utc(settings=None, now: datetime = None) -> str:
    """她今天 0 点换成 UTC 的 ISO 字符串：数据库里存 UTC 时间戳（sent_at、created_at…），查“她今天的”用 >= 这个，
    不要用 SQL 的 date('now')（那是 UTC 的今天，夏令时 0–1 点会差一天）。"""
    here = her_now(settings, now)
    return here.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat(timespec="seconds")


def week_start(day: date = None, settings=None, now: datetime = None) -> date:
    """“本周一”：一周从周一开始、按自然周（system-rules 第 5 节）。全系统只在这里算，别处不许自己写
    day - timedelta(days=day.weekday())（tests/test_time_guard.py 守着）。不给 day 就按她的“今天”（她的时区）。"""
    if day is None:
        day = her_today(settings, now)
    return day - timedelta(days=day.weekday())


def week_end(day: date = None, settings=None, now: datetime = None) -> date:
    """这周日（含）。"""
    return week_start(day, settings, now) + timedelta(days=6)


def season_week(day: date, start: date) -> int:
    """day 是从 start 那周算起的第几周（按自然周，start 那周是第 1 周；看到过“第 1、1 周”）。"""
    return (week_start(day) - week_start(start)).days // 7 + 1


def work_day(local: datetime):
    """按凌晨 5 点分天：local 是她那边的本地时间（带不带时区都行），0–5 点算前一天。"""
    return (local - timedelta(hours=DAY_START_HOUR)).date()


def home_now(now: datetime = None) -> datetime:
    return utc_now(now).astimezone(HOME_TZ)


def stamp(dt: datetime) -> str:
    """2026-10-23 周五 19:40"""
    return f"{dt:%Y-%m-%d} 周{WEEKDAYS[dt.weekday()]} {dt:%H:%M}"


def ago(then: datetime, now: datetime) -> str:
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 1:
        return "刚刚"
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} 小时前"
    return f"{hours // 24} 天前"


def time_brief(records: list, settings=None, now: datetime = None) -> str:
    """每轮注入的时间信息：两边现在几点、星期几，双方上一条消息是多久以前。"""
    now = utc_now(now)
    her, home = her_now(settings, now), home_now(now)
    lines = [f"她那边（伦敦）现在：{stamp(her)}", f"你这边（深圳）现在：{stamp(home)}"]
    for role, who in (("mia", "她"), ("ethan", "你")):
        last = next((r for r in reversed(records) if r["role"] == role), None)
        if last:
            at = datetime.fromisoformat(last["at"])
            lines.append(f"{who}上一条消息：{ago(at, now)}（伦敦 {stamp(at.astimezone(her.tzinfo))}）")
    return "\n".join(lines)


# ---------- 问句里说的是哪一天 / 哪几天（第 29 题“某天她做了什么”） ----------
# 只认往回看的说法；“今天”照旧从 her_today 来。返回 (开始, 结束, 原话里那几个字)；认不出返回 None。
_CN = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_WD = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6, "1": 0, "2": 1, "3": 2, "4": 3, "5": 4, "6": 5, "7": 6}
_NUM = r"\d{1,2}|[一二三四五六七八九十两]{1,3}"


def cn_int(s: str):
    """“3”“十一”“二十三”“两” → 整数；认不出返回 None。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s or any(c not in _CN and c != "十" for c in s):
        return None
    if "十" not in s:
        return _CN[s] if len(s) == 1 else None
    a, _, b = s.partition("十")
    return (_CN.get(a, 0) if a else 1) * 10 + (_CN.get(b, 0) if b else 0)


def _md(year: int, month: int, day_: int, today: date):
    try:
        d = date(year, month, day_)
    except ValueError:
        return None
    return d


def day_range(text: str, today: date = None):
    """“昨天”“前天”“上周五”“周三”“11 月 3 号”“3 号”“11/3”“2026-11-03”“这周”“上周”“这个月”“三天前”“这几天” → (开始, 结束, 原话)。
    没说年份、算出来在今天以后的，按去年；只说“几号”在今天以后的，按上个月；光说“周三”取最近的那个（含今天）。"""
    import re
    t = text or ""
    today = today or her_today()
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        d = _md(int(m.group(1)), int(m.group(2)), int(m.group(3)), today)
        return (d, d, m.group(0)) if d else None
    m = re.search(rf"(?:(\d{{4}})\s*年\s*)?({_NUM})\s*月\s*({_NUM})\s*(?:日|号)?", t) or \
        re.search(r"(?<![\d.])()(\d{1,2})/(\d{1,2})(?![\d/])", t)
    if m:
        mo, dd = cn_int(m.group(2)), cn_int(m.group(3))
        if mo and dd:
            year = int(m.group(1)) if m.group(1) else today.year
            d = _md(year, mo, dd, today)
            if d and not m.group(1) and d > today:
                d = _md(year - 1, mo, dd, today)
            return (d, d, m.group(0).strip()) if d else None
    for word, back in (("大前天", 3), ("前天", 2), ("昨天", 1), ("昨日", 1), ("昨儿", 1), ("今天", 0), ("今日", 0), ("今儿", 0)):
        if word in t:
            d = today - timedelta(days=back)
            return d, d, word
    m = re.search(rf"({_NUM})\s*天(?:以)?前", t)
    if m and cn_int(m.group(1)):
        d = today - timedelta(days=cn_int(m.group(1)))
        return d, d, m.group(0)
    m = re.search(r"(上上|上个|上|这个|这|本)?(?:周|星期|礼拜)([一二三四五六日天1-7])", t)
    if m:
        wd = _WD[m.group(2)]
        back = {"上上": 2, "上个": 1, "上": 1}.get(m.group(1) or "", 0)
        if m.group(1):
            d = week_start(today) - timedelta(days=7 * back) + timedelta(days=wd)
        else:  # 光说“周三”：最近的那个周三（含今天）
            d = today - timedelta(days=(today.weekday() - wd) % 7)
        return d, d, m.group(0)
    m = re.search(r"(上上|上个|上|这个|这|本)(?:周|星期|礼拜)", t)
    if m:
        back = {"上上": 2, "上个": 1, "上": 1}.get(m.group(1), 0)
        start = week_start(today) - timedelta(days=7 * back)
        return start, min(start + timedelta(days=6), today), m.group(0)
    m = re.search(r"(上个|这个|本)月", t)
    if m:
        first = today.replace(day=1)
        if m.group(1) == "上个":
            end = first - timedelta(days=1)
            return end.replace(day=1), end, m.group(0)
        return first, today, m.group(0)
    m = re.search(r"(?:最近|这|过去)(?:几|两|三)天", t)
    if m:
        return today - timedelta(days=3), today, m.group(0)
    m = re.search(rf"(?<![\d月])({_NUM})\s*(?:号|日)(?!子|记|本|程|常)", t)
    if m and cn_int(m.group(1)) and 1 <= cn_int(m.group(1)) <= 31:
        n = cn_int(m.group(1))
        d = _md(today.year, today.month, n, today)
        if d is None or d > today:
            prev = today.replace(day=1) - timedelta(days=1)
            d = _md(prev.year, prev.month, n, today)
        return (d, d, m.group(0)) if d else None
    return None
