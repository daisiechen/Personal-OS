"""时间：小伴 设定住在深圳。服务器在洛杉矶（系统时区 UTC），任何日期都不能用服务器本地时间算。"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = os.environ.get("ETHAN_TZ", "").strip() or "Europe/London"  # 开源版用户在 ethan.env 里改
LOCAL = ZoneInfo(DEFAULT_TZ)
HOME_TZ = ZoneInfo("Asia/Shanghai")  # 小伴 在深圳
WEEKDAYS = "一二三四五六日"
# 她的一天从凌晨 5 点算起（工作时长、工作狂指数、作息统计共用；工作时长，11-06 决定 9：作息也按 5 点）。
# 只管“这一格 / 这一晚算哪天”的统计口径；日期、截止日、“今天”照旧按 0 点（her_today）。小伴 深圳剧本的 4 点另算（life.py）。
DAY_START_HOUR = 5


def her_tz(settings=None) -> ZoneInfo:
    return ZoneInfo(settings.get("timezone", DEFAULT_TZ) if settings is not None else DEFAULT_TZ)


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


def work_day(local: datetime):
    """按凌晨 5 点分天：local 是她那边的本地时间（带不带时区都行），0–5 点算前一天。"""
    from datetime import timedelta
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
