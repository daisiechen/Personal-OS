"""路上要多久（设计第 9.6 节：伦敦用 TfL 官方出行接口，不需要账号）。

地点存在服务器数据目录的 settings.json 的 places 里（她的住址不进仓库），例如：
  {"places": {"家": {"postcode": "..."}, "健身房": {"postcode": "...", "name": "..."}}}
算法：按“几点要到”问 TfL，取最快一条线路的时长，再加 20 分钟余量。结果按（起点、终点、星期几、几点）缓存一周，
TfL 不可用时用默认 30 分钟并记日志，不让计划卡住。
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://api.tfl.gov.uk/Journey/JourneyResults/{a}/to/{b}?date={d}&time={t}&timeIs=Arriving"
BUFFER_MIN = 20  # 2026-10-29 她定：出发时间 = 日程开始 − 路上时间 − 20 分钟
DEFAULT_MIN = 30
CACHE_FILE = "travel-cache.json"
CACHE_DAYS = 7


def places(settings) -> dict:
    return (settings.get("places", {}) if settings is not None else {}) or {}


def _code(place: dict) -> str:
    return (place.get("postcode") or "").replace(" ", "").upper()


class Travel:
    def __init__(self, root: Path, settings, opener=urllib.request.urlopen):
        self.cache_path = Path(root) / CACHE_FILE
        self.settings = settings
        self.opener = opener

    def _cache(self) -> dict:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def minutes(self, origin: str, dest, day: date, arrive: str) -> dict:
        """返回 {"minutes": 路上分钟数（不含余量）, "route": 线路说明, "source": tfl/缓存/默认}。
        dest 是登记过的地点名，或 {"postcode": 邮编或"纬度,经度"}。"""
        known = places(self.settings)
        a, b = known.get(origin), (dest if isinstance(dest, dict) else known.get(dest))
        if not a or not b or not _code(a) or not _code(b):
            return {"minutes": DEFAULT_MIN, "route": "地点没有邮编，按 30 分钟估", "source": "默认"}
        key = f"{_code(a)}>{_code(b)}>{day.weekday()}>{arrive}"
        cache = self._cache()
        hit = cache.get(key)
        if hit and datetime.fromisoformat(hit["at"]) > datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS):
            return {"minutes": hit["minutes"], "route": hit["route"], "source": "缓存"}
        url = API.format(a=urllib.parse.quote(_code(a)), b=urllib.parse.quote(_code(b)),
                         d=day.strftime("%Y%m%d"), t=arrive.replace(":", ""))
        try:
            with self.opener(urllib.request.Request(url, headers={"User-Agent": "Ethan/2.0"}), timeout=20) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            best = min(data["journeys"], key=lambda j: j["duration"])
            route = " → ".join(leg["instruction"]["summary"] for leg in best["legs"])
            result = {"minutes": int(best["duration"]), "route": route}
        except Exception as e:  # 网络、限流、没线路都按默认，不让计划卡住
            print(f"[travel] TfL 查询失败，按 {DEFAULT_MIN} 分钟估：{str(e)[:120]}", flush=True)
            return {"minutes": DEFAULT_MIN, "route": f"TfL 暂时查不到，按 {DEFAULT_MIN} 分钟估", "source": "默认"}
        cache[key] = {**result, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        try:
            self.cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass
        return {**result, "source": "tfl"}


POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b", re.I)


def where(loc: dict) -> dict:
    """日历事件的地点 → TfL 能用的写法：优先坐标，其次地址里的英国邮编；都没有返回 None。"""
    if loc.get("geo"):
        lat, lon = loc["geo"]
        return {"postcode": f"{lat:.6f},{lon:.6f}"}
    m = POSTCODE.search(loc.get("location") or "")
    return {"postcode": m.group(1)} if m else None


def depart_at(start: str, minutes: int) -> str:
    """几点出发：开始时间 − 路上时间 − 20 分钟余量，按 5 分钟取整往前。"""
    h, m = map(int, start.split(":"))
    t = h * 60 + m - minutes - BUFFER_MIN
    t -= t % 5
    return f"{t // 60:02d}:{t % 60:02d}"
