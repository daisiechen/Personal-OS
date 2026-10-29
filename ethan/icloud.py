"""iCloud 日历（设计第 9.6 节，2026-10-26 Mia 定用苹果日历，计划精确到小时）。

通过 CalDAV 接入：只写一个单独的“小伴 计划”日历（没有就建），不改她其他日历；
只读其他日历的忙闲，让计划器避开她已有的安排。事件时间一律用 UTC 写入，苹果日历按她手机的时区显示。

环境变量（Mia 用 python3 -m ethan.setup_secrets 自己录入，不进聊天和仓库）：
  ICLOUD_USERNAME      Apple ID 邮箱
  ICLOUD_APP_PASSWORD  在 account.apple.com → 登录与安全 → App 专用密码 生成的密码
"""

import base64
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .clock import LOCAL

ROOT = "https://caldav.icloud.com/"
CALENDAR_NAME = "小伴 计划"
LONDON = LOCAL  # 名字沿用，实际是她所在的时区（ETHAN_TZ，默认伦敦）
NS = {"d": "DAV:", "c": "urn:ietf:params:xml:ns:caldav"}


class CalendarError(RuntimeError):
    pass


def _body(xml: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>' + xml).encode("utf-8")


def _ics_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def event_ics(uid: str, start: datetime, end: datetime, title: str, description: str = "",
              location: str = "", alarm: str = "", place: dict = None, travel_min: int = None,
              alarm_before: int = 0) -> str:
    """place：{"name", "address", "lat", "lon"}，给苹果日历显示地点和算通行时间；
    travel_min：通行时间（分钟），苹果日历在事件前留出这段；alarm_before：开始前多少分钟提醒。"""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//小伴//Mias OS//ZH", "BEGIN:VEVENT",
             f"UID:{uid}", f"DTSTAMP:{_ics_time(datetime.now(timezone.utc))}",
             f"DTSTART:{_ics_time(start)}", f"DTEND:{_ics_time(end)}", f"SUMMARY:{_escape(title)}"]
    if description:
        lines.append(f"DESCRIPTION:{_escape(description)}")
    if place and place.get("address"):
        lines.append(f"LOCATION:{_escape(place.get('name') or location)}\\n{_escape(place['address'])}")
    elif location:
        lines.append(f"LOCATION:{_escape(location)}")
    if place and place.get("lat") is not None:
        lines.append(f"X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-ADDRESS={_escape(place.get('address', ''))};"
                     f"X-APPLE-RADIUS=70;X-TITLE={_escape(place.get('name') or location)}:geo:{place['lat']},{place['lon']}")
    if travel_min:
        lines += ["X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC", f"X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT{travel_min}M"]
    if alarm:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(alarm)}",
                  f"TRIGGER:-PT{alarm_before}M" if alarm_before else "TRIGGER:PT0M", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(lines) + "\r\n"


def parse_ics_time(value: str, params: str) -> datetime:
    """支持 ...Z（UTC）、TZID=...（带时区）、不带时区（按伦敦时间）；全天事件返回 None。"""
    if "VALUE=DATE" in params and "T" not in value:
        return None
    naive = datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return naive.replace(tzinfo=timezone.utc)
    tzid = next((p.split("=", 1)[1] for p in params.split(";") if p.startswith("TZID=")), None)
    try:
        return naive.replace(tzinfo=ZoneInfo(tzid) if tzid else LONDON)
    except Exception:
        return naive.replace(tzinfo=LONDON)


def _unescape(value: str) -> str:
    return value.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").strip()


def parse_vevents(ics: str) -> list:
    """从 VCALENDAR 文本里取出事件：{"start", "end", "title", "location", "geo", "travel_min"}，跳过全天事件。
    travel_min 是她在苹果日历里给这个事件设的通行时间（X-APPLE-TRAVEL-DURATION），没设就是 None。"""
    text = ics.replace("\r\n ", "").replace("\n ", "")
    events, cur = [], None
    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            if cur.get("start") and cur.get("end"):
                events.append({"title": "", "location": "", "geo": None, "travel_min": None, **cur})
            cur = None
        elif cur is not None and ":" in line:
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            if name == "DTSTART":
                cur["start"] = parse_ics_time(value, params)
            elif name == "DTEND":
                cur["end"] = parse_ics_time(value, params)
            elif name == "SUMMARY":
                cur["title"] = _unescape(value)
            elif name == "LOCATION":
                cur["location"] = _unescape(value)
            elif name == "X-APPLE-STRUCTURED-LOCATION" and "geo:" in line:
                lat, _, lon = line.rsplit("geo:", 1)[1].partition(",")
                try:
                    cur["geo"] = (float(lat), float(lon))
                except ValueError:
                    pass
            elif name == "X-APPLE-TRAVEL-DURATION":
                m = re.match(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?)", value)
                if m and (m.group(1) or m.group(2)):
                    cur["travel_min"] = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    return events


def parse_events(ics: str) -> list:
    """[(开始, 结束, 标题)]，跳过全天事件。"""
    return [(e["start"], e["end"], e["title"]) for e in parse_vevents(ics)]


class ICloudCalendar:
    def __init__(self, username: str, password: str, root: str = ROOT, opener=urllib.request.urlopen):
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.auth = f"Basic {token}"
        self.root = root
        self.opener = opener
        self._calendars = None

    # 底层请求
    def request(self, method: str, url: str, body: bytes = None, depth: str = None, ctype="application/xml") -> tuple:
        headers = {"Authorization": self.auth, "Content-Type": f"{ctype}; charset=utf-8"}
        if depth is not None:
            headers["Depth"] = depth
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self.opener(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code == 404 and method == "DELETE":
                return 404, ""
            raise CalendarError(f"{method} {urllib.parse.urlparse(url).path} 失败：HTTP {e.code}") from e

    def _href(self, xml: str, prop: str) -> str:
        node = ET.fromstring(xml).find(f".//{prop}/d:href", NS)
        if node is None or not node.text:
            raise CalendarError(f"找不到 {prop}")
        return urllib.parse.urljoin(self.root, node.text.strip())

    def calendars(self) -> dict:
        """{名字: 完整地址}，只含能放事件的日历。"""
        if self._calendars is not None:
            return self._calendars
        _, xml = self.request("PROPFIND", self.root, _body(
            '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'), depth="0")
        principal = self._href(xml, "d:current-user-principal")
        _, xml = self.request("PROPFIND", principal, _body(
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">'
            '<d:prop><c:calendar-home-set/></d:prop></d:propfind>'), depth="0")
        self.home = self._href(xml, "c:calendar-home-set")
        _, xml = self.request("PROPFIND", self.home, _body(
            '<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
            '<d:displayname/><d:resourcetype/><c:supported-calendar-component-set/></d:prop></d:propfind>'),
            depth="1")
        found = {}
        for resp in ET.fromstring(xml).findall("d:response", NS):
            if resp.find(".//d:resourcetype/c:calendar", NS) is None:
                continue
            comps = [c.get("name") for c in resp.findall(".//c:supported-calendar-component-set/c:comp", NS)]
            if comps and "VEVENT" not in comps:
                continue
            name = resp.findtext(".//d:displayname", default="", namespaces=NS).strip()
            href = urllib.parse.urljoin(self.home, resp.findtext("d:href", namespaces=NS).strip())
            found[name or href] = href
        self._calendars = found
        return found

    def ethan_calendar(self) -> str:
        cals = self.calendars()
        if CALENDAR_NAME in cals:
            return cals[CALENDAR_NAME]
        url = urllib.parse.urljoin(self.home, f"ethan-{uuid.uuid4().hex[:8]}/")
        self.request("MKCALENDAR", url, _body(
            '<c:mkcalendar xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:set><d:prop>'
            f'<d:displayname>{CALENDAR_NAME}</d:displayname>'
            '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
            '</d:prop></d:set></c:mkcalendar>'))
        cals[CALENDAR_NAME] = url
        return url

    # 读忙闲
    def busy(self, day: date) -> list:
        """她其他日历当天（伦敦时间）的安排：[(HH:MM, HH:MM, 标题, {"location", "geo", "travel_min"})]，
        跳过全天事件和 小伴 计划日历。"""
        start = datetime(day.year, day.month, day.day, tzinfo=LONDON)
        end = start + timedelta(days=1)
        rng = f'start="{_ics_time(start)}" end="{_ics_time(end)}"'
        query = _body(
            '<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"><d:prop>'
            f'<c:calendar-data><c:expand {rng}/></c:calendar-data></d:prop>'
            '<c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">'
            f'<c:time-range {rng}/></c:comp-filter></c:comp-filter></c:filter></c:calendar-query>')
        out = []
        for name, url in self.calendars().items():
            if name == CALENDAR_NAME:
                continue
            _, xml = self.request("REPORT", url, query, depth="1")
            if not xml.strip():
                continue
            for node in ET.fromstring(xml).findall(".//c:calendar-data", NS):
                for ev in parse_vevents(node.text or ""):
                    s, e = ev["start"], ev["end"]
                    if not s or not e or e <= start or s >= end:
                        continue
                    s, e = max(s, start).astimezone(LONDON), min(e, end).astimezone(LONDON)
                    out.append((s.strftime("%H:%M"), "23:59" if e >= end else e.strftime("%H:%M"), ev["title"],
                                {"location": ev["location"], "geo": ev["geo"], "travel_min": ev["travel_min"]}))
        return sorted(out)

    # 写计划
    def sync_day(self, day: date, items: list, stale_uids=(), places: dict = None, remind_before: int = 60) -> dict:
        """把当天计划写进“小伴 计划”日历，先删掉旧版本的事件。返回 {计划条目编号: UID}。"""
        cal = self.ethan_calendar()
        for uid in stale_uids:
            self.request("DELETE", urllib.parse.urljoin(cal, f"{uid}.ics"))
        written = {}
        for it in items:
            uid = it.get("calendar_uid") or f"ethan-{day.isoformat()}-{it['id']}"
            s = datetime.combine(day, datetime.strptime(it["start"], "%H:%M").time(), LONDON)
            e = datetime.combine(day, datetime.strptime(it["end"], "%H:%M").time(), LONDON)
            desc = "\n".join(x for x in (it.get("detail") or "",
                                         f"办法：{it['how']}" if it.get("how") else "",
                                         f"不想做时：{it['smaller']}" if it.get("smaller") else "",
                                         f"计划条目 #{it['id']}") if x)
            travel = it.get("travel_min")
            place = (places or {}).get(it.get("place") or "")
            alarm = f"{remind_before} 分钟后出发：{it['title']}" if travel else ""
            ics = event_ics(uid, s, e, it["title"], desc, it.get("place") or "", alarm, place=place,
                            travel_min=travel, alarm_before=(travel + remind_before) if travel else 0)
            self.request("PUT", urllib.parse.urljoin(cal, f"{uid}.ics"), ics.encode("utf-8"), ctype="text/calendar")
            written[it["id"]] = uid
        return written


def build_calendar(env=os.environ):
    user, pw = env.get("ICLOUD_USERNAME", "").strip(), env.get("ICLOUD_APP_PASSWORD", "").strip()
    if not (user and pw):
        return None
    return ICloudCalendar(user, pw)
