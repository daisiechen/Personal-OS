"""今天的路线：把她一天要去的地方串成一条路（她 2026-11-02）。

她的原话：“你在排日程的时候，应该自己先串联一下我的行为路径……我今天的路线是什么样子的？时间线上有没有冲突？
我是应该吃完 Sunday roast 直接回家，再出发去训练，还是直接去健身房锻炼？”

以前每个约各算一遍“从家出门、回家”，拼起来就是好几次出门、到家比训练结束还早。现在：
- 把今天所有有固定时间的事（她日历上的、邮件 / 聊天记下的约、计划里的训练）按开始时间排好；
- 从家出发，一站一站往下走：上一站结束到下一站开始，如果回家待得了至少一小时就先回家，否则直接过去；
- 出门前 1 小时提醒“准备出发”，只在从家出发时提醒；
- 赶不上（上一站结束太晚）或者两件事时间重叠，标 ⚠；
- 最后一站结束后回家。
路上时间用 TfL（ethan/travel.py，按“几点要到”问，缓存一周）；查不到按 30 分钟估。
"""

import re

from .travel import BUFFER_MIN, DEFAULT_MIN, where

HOME = "家"
REMIND_BEFORE = 60
HOME_STAY = 60  # 回家至少能待这么久才回家，不然直接去下一站


def _m(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _hm(n: int) -> str:
    n = max(0, min(n, 23 * 60 + 59))
    return f"{n // 60:02d}:{n % 60:02d}"


def stops(busy: list, items: list, places: dict) -> list:
    """今天要到场的事：[{title, start, end, dest（TfL 能用的地点）, label, home_min（她日历里设的从家过去的通行时间）}]。
    busy 是 ICloudCalendar.busy 的格式（加上 小伴 记下的约），items 是计划条目。没有地点的（线上、在家）dest 为 None。"""
    out = []
    for b in busy:
        loc = b[3] if len(b) > 3 and b[3] else {}
        out.append({"title": b[2], "start": b[0], "end": b[1], "dest": where(loc) if loc else None,
                    "label": loc.get("location") or "", "home_min": loc.get("travel_min")})
    for i in items:
        if i.get("kind") != "event" or not i.get("start"):
            continue
        place = i.get("place") or ""
        dest = place if place in places else ((where({"location": place}) or {"name": place}) if place else None)
        trip = i.get("travel_min") - BUFFER_MIN if i.get("travel_min") else None  # 早上排计划时算好的（含 20 分钟余量）
        out.append({"title": i["title"], "start": i["start"], "end": i.get("end") or i["start"], "dest": dest, "label": place,
                    "home_min": trip if trip and trip > 0 else None, "item": True})
    return sorted(out, key=lambda s: (s["start"], s["end"]))


FLEX_END = re.compile(r"饭|餐|午餐|晚餐|早午|brunch|lunch|dinner|roast|咖啡|喝|聚|逛|看展|散步", re.I)  # 可以早点走的
FIXED = re.compile(r"会|课|面试|预约|医生|牙|签证|考试|讲座|航班|火车|seminar|class|meeting|interview", re.I)  # 时间改不了的
MOVABLE = re.compile(r"训练|健身|跑步|普拉提|运动|练")  # 自己定时间的


def fix_tip(prev: dict, nxt: dict, direct: int, prev_end: int) -> dict:
    """上一站结束直接过去也赶不上下一站：判断哪边能动。
    - 上一站是吃饭、喝咖啡这类能早点走的，又不会缩掉一半以上：提醒几点吃完走；
    - 否则下一站是训练这类自己定时间的：建议往后挪到上一站结束 + 路上 + 余量；
    - 都动不了：只说清晚到多少。返回 {"text", "leave"（几点该走）}。"""
    need_leave = _m(nxt["start"]) - direct - BUFFER_MIN // 2
    dur = _m(prev["end"]) - _m(prev["start"])
    if prev and FLEX_END.search(prev["title"]) and not FIXED.search(prev["title"]) and need_leave - _m(prev["start"]) >= dur / 2:
        return {"leave": _hm(need_leave), "text": f"「{prev['title']}」{_hm(need_leave)} 吃完就走，直接去「{nxt['title']}」（路上约 {direct} 分钟）"}
    if nxt.get("item") or MOVABLE.search(nxt["title"]):
        new = prev_end + direct + BUFFER_MIN // 2
        new += (-new) % 15
        return {"leave": _hm(prev_end), "text": f"「{prev['title']}」到 {_hm(prev_end)} 才结束，建议「{nxt['title']}」挪到 {_hm(new)}，结束后直接过去"}
    return {"leave": _hm(prev_end), "text": f"「{prev['title']}」{_hm(prev_end)} 结束，赶去「{nxt['title']}」会晚到约 {prev_end + direct - _m(nxt['start'])} 分钟，两边都定死了，要你选"}


def plan(stops_: list, minutes) -> dict:
    """minutes(起点, 终点, 几点要到) → 路上分钟数（起点 / 终点是地点名或 {"postcode"}）。
    返回 {"rows": 时间线, "conflicts": [一句话]}。"""
    rows, conflicts = [], []
    here, free_at, here_title, prev = HOME, None, None, None
    for s in stops_:
        if s["dest"] is None:  # 不用出门的（线上、在家）：只列出来，看撞不撞
            if free_at is not None and _m(s["start"]) < free_at and here != HOME:
                conflicts.append(f"「{s['title']}」{s['start']} 开始时你还在「{here_title}」")
            rows.append((s["start"], "📌", f"{s['title']}（到 {s['end']}）", None))
            continue
        if free_at is not None and here != HOME:
            if _m(s["start"]) < free_at:
                conflicts.append(f"「{s['title']}」和「{here_title}」时间重叠")
                rows.append((s["start"], "⚠️", f"「{s['title']}」和「{here_title}」撞时间了", None))
            direct = minutes(here, s["dest"], s["start"])
            to_home = minutes(here, HOME, _hm(free_at + 60))
            from_home = s["home_min"] or minutes(HOME, s["dest"], s["start"])
            gap = _m(s["start"]) - free_at
            if gap >= to_home + from_home + BUFFER_MIN + HOME_STAY:  # 回得了家：先回家，再出门
                rows.append((_hm(free_at + to_home), "🏠", "到家", None))
                here = HOME
            else:
                leave = _m(s["start"]) - direct - BUFFER_MIN
                info = {"title": s["title"], "place": s["label"], "start": s["start"], "leave": _hm(max(leave, free_at)),
                        "trip_min": direct, "route": f"从「{here_title}」直接过去"}
                if leave < free_at and _m(s["start"]) >= free_at:
                    late = free_at + direct - _m(s["start"])
                    if late > 0:  # 赶不上：想清楚是前面早点走，还是后面往后挪（她 2026-11-02）
                        tip = fix_tip(prev, s, direct, free_at)
                        conflicts.append(f"「{here_title}」{_hm(free_at)} 结束，直接去「{s['title']}」也要晚到 {late} 分钟。{tip['text']}")
                        rows.append((tip["leave"], "⚠️", tip["text"], {**info, "leave": tip["leave"]}))
                    else:
                        rows.append((_hm(free_at), "🚶", f"结束后直接去 {s['title']}（路上约 {direct} 分钟）", info))
                elif _m(s["start"]) >= free_at:
                    rows.append((_hm(leave), "🚶", f"从「{here_title}」直接去 {s['title']}（路上约 {direct} 分钟）", info))
        if here == HOME:
            t = s["home_min"] or minutes(HOME, s["dest"], s["start"])
            leave = _m(s["start"]) - t - BUFFER_MIN
            leave -= leave % 5
            info = {"title": s["title"], "place": s["label"], "start": s["start"], "leave": _hm(leave),
                    "remind": _hm(leave - REMIND_BEFORE), "trip_min": t, "route": ""}
            rows.append((_hm(leave - REMIND_BEFORE), "⏰", f"准备出发去 {s['title']}", info))
            rows.append((_hm(leave), "🚶", f"出门 → {s['title']}（路上约 {t} 分钟）", info))
        rows.append((s["start"], "📍", f"{s['title']}（到 {s['end']}）", None))
        here, here_title, prev = s["dest"], s["title"], s
        free_at = max(free_at or 0, _m(s["end"]))
    if here != HOME and free_at is not None:
        rows.append((_hm(free_at + minutes(here, HOME, _hm(free_at + 60))), "🏠", "到家", None))
    rows.sort(key=lambda r: r[0])
    return {"rows": [{"at": at, "icon": icon, "text": text, **({"trip": info} if info else {})} for at, icon, text, info in rows],
            "conflicts": conflicts}


def timeline(busy: list, items: list, places: dict, travel=None, day=None) -> dict:
    """计划器和工作台都用：travel 为 None 时路上一律按 30 分钟估。"""
    def minutes(a, b, arrive):
        if travel is None or day is None:
            return DEFAULT_MIN
        return travel.minutes(a, b, day, arrive)["minutes"]
    return plan(stops(busy, items, places), minutes)
