"""时间：Mia 在伦敦，小伴 设定住在深圳。服务器在洛杉矶（系统时区 UTC），任何日期都不能用服务器本地时间算。"""

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = os.environ.get("ETHAN_TZ", "").strip() or "Europe/London"  # 开源版用户在 ethan.env 里改（2026-10-29）
LOCAL = ZoneInfo(DEFAULT_TZ)
HOME_TZ = ZoneInfo("Asia/Shanghai")  # 小伴 在深圳
WEEKDAYS = "一二三四五六日"


def her_tz(settings=None) -> ZoneInfo:
    return ZoneInfo(settings.get("timezone", DEFAULT_TZ) if settings is not None else DEFAULT_TZ)


def utc_now(now: datetime = None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def her_now(settings=None, now: datetime = None) -> datetime:
    return utc_now(now).astimezone(her_tz(settings))


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
