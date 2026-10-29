"""她现在在哪个城市（2026-10-29 她定：她告诉了 小伴 就同步给个人主页，没告诉就默认最近的常驻地）。

iPhone 只报“离开家 / 到家”，不报具体在哪（places_log.py）。所以城市只从聊天里来：她说到了别的城市，
小伴 在回复里加 [在 巴黎]；回到常驻地加 [在 伦敦]。iPhone 报“到家”说明她回来了，之前说的城市就不算了。
常驻地：环境变量 ETHAN_HOME_CITY，没设就按时区（Europe/London → 伦敦）。
个人主页（example.com）从 GET /cloud/profile 取。
"""

import os
import re
from datetime import datetime, timezone

MARK = re.compile(r"\[在\s+([^\]\s][^\]]{0,29}?)\s*\]")
SCHEMA = "CREATE TABLE IF NOT EXISTS city_log (id INTEGER PRIMARY KEY, at TEXT NOT NULL, city TEXT NOT NULL)"
TZ_CITY = {"Europe/London": "伦敦", "Asia/Shanghai": "上海", "Asia/Hong_Kong": "香港", "Asia/Tokyo": "东京",
           "Asia/Singapore": "新加坡", "Europe/Paris": "巴黎", "America/New_York": "纽约",
           "America/Los_Angeles": "洛杉矶"}


def home_city() -> str:
    if os.environ.get("ETHAN_HOME_CITY"):
        return os.environ["ETHAN_HOME_CITY"].strip()
    from .clock import DEFAULT_TZ
    return TZ_CITY.get(DEFAULT_TZ, DEFAULT_TZ.split("/")[-1].replace("_", " "))


def note(db, text: str, now: datetime = None) -> list:
    db.conn.execute(SCHEMA)
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    got = [c.strip() for c in MARK.findall(text) if c.strip()]
    for city in got:
        db.conn.execute("INSERT INTO city_log (at, city) VALUES (?, ?)", (at, city))
    db.conn.commit()
    return got


def strip(text: str) -> str:
    return MARK.sub("", text)


def current(db) -> dict:
    """{"city": 城市, "source": "told"（她说的）/"home"（常驻地）, "at": 她说的时间}"""
    db.conn.execute(SCHEMA)
    home = home_city()
    told = db.conn.execute("SELECT at, city FROM city_log ORDER BY at DESC, id DESC LIMIT 1").fetchone()
    if not told or told["city"] == home:
        return {"city": home, "source": "home", "at": told["at"] if told else None}
    try:
        back = db.conn.execute("SELECT at FROM place_events WHERE event = 'arrive' AND at > ? LIMIT 1", (told["at"],)).fetchone()
    except Exception:  # 还没装 iPhone 定位
        back = None
    if back:
        return {"city": home, "source": "home", "at": back["at"]}
    return {"city": told["city"], "source": "told", "at": told["at"]}
