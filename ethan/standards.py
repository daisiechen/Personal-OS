"""领域标准表：每项一个“保底”、一个“目标”（全系统模块化第三批，她的决定 8）。

决定 8：领域标准统一成一张表（每项一个“保底”、一个“目标”），
领地灯（ethan/territory.py health）和属性分（ethan/attributes.py）都按它算；两边原来的数字不一样时，松的当保底、严的当目标。
原来两边各写一套：领地灯训练 目标 5 / 保底 4 天每周，属性分 30 天 17 天；蛋白质领地灯每周保底 5 天、属性分 30 天天天……
对照数字见 docs/design/territories.md“领域标准表”。
过目后（“其他的按你的建议”）：属性分满分按“目标”算（保持）；0 点前睡目标从“每天”改成“每周 6 天”（保底仍 5 天）；
认知拓展保底每周 5 天保持。

用法：
- 领地灯：按周看，floor / target 是整周的次数（天数），_paced 按本周过了几天折算。
- 属性分：按最近 30 天看，满分 = 目标 × 天数 ÷ 7（按月的 × 天数 ÷ 30）；系统开始不满 30 天的按已有天数折算。
守卫：tests/test_standards.py——territory / attributes 里不许再写这些标准的数字。
"""

from collections import namedtuple
from datetime import date

# per：week = 每周几次（天）；month = 每月几次；unit：天 / 次 / 个 / 分钟
Std = namedtuple("Std", "name habit territory per unit floor target was why")

STANDARDS = {
    "training": Std("训练", "训练", "body", "week", "天", 4, 5,
                    "领地灯 目标 5 / 保底 4（每周）；属性分 30 天 17 天（≈每周 4）", "两边保底一致；目标取领地灯的 5"),
    "protein": Std("吃够蛋白质", "吃够蛋白质", "body", "week", "天", 5, 7,
                   "领地灯 目标 7 / 保底 5；属性分 30 天天天（每周 7）", "保底取领地灯的 5，目标 7"),
    "sleep": Std("0 点前睡", "0 点前睡", "body", "week", "天", 5, 6,
                 "领地灯 目标 6 / 保底 5；属性分 30 天天天（每周 7）", "保底 5；目标每周 6 天（从“每天”改成“每周 6 天”）"),
    "outside": Std("每天出门", "每天出门", "body", "week", "天", 6, 7,
                   "只在领地灯：目标 7 / 保底 6", "只有一边，照旧"),
    "english": Std("英语陪练（Grok）", "英语陪练（Grok）", "study", "week", "次", 5, 5,
                   "领地灯 保底每周 5 次、没写目标；属性分 30 天 20 次（≈每周 4.7）", "两边都是“每周 5 次”，保底 = 目标 = 5"),
    "cognitive": Std("认知拓展", None, "study", "week", "天", 5, 7,
                     "领地灯 保底每天 1 个话题（每周 7）；属性分 30 天 20 天（≈每周 5）", "松的 5 当保底，严的 7 当目标"),
    "course": Std("课程和读书", None, "study", "week", "次", 2, 2,
                  "只在属性分：30 天 8 次（≈每周 2）", "只有一边，照旧"),
    "new_exp": Std("新体验", "每周 1 次新体验", "life", "week", "次", 1, 1,
                   "领地灯 每周 1 次；属性分 30 天 4 次（≈每周 1）", "一致"),
    "outing": Std("出门做工作以外的事", None, "life", "week", "次", 1, 1,
                  "领地灯 保底每周 1 次；属性分 30 天 4 次（≈每周 1）", "一致"),
    "meet": Std("和朋友见面", None, "social", "week", "次", 1, 1,
                "领地灯 保底每周 1 次；属性分 30 天 4 次（≈每周 1）", "一致"),
    "new_friend": Std("认识新朋友", None, "social", "month", "个", 3, 4,
                      "领地灯 每月 3–4 个；属性分 30 天 3 个", "松的 3 当保底，严的 4 当目标"),
    "works": Std("作品和发帖", None, "career", "month", "次", 2, 2,
                 "只在属性分：30 天 2 次进展（领地灯的“小红书 + Instagram 每周各 2 条”是另一项，账号还没接）", "只有一边，照旧"),
    "research": Std("研究时间", None, "academic", "week", "分钟", 120, 120,
                    "领地灯 每周约 2 小时；属性分 30 天 8 小时（≈每周 1.9 小时）", "两边都是“每周 2 小时”"),
}


def get(key: str) -> Std:
    return STANDARDS[key]


def week(key: str, which: str = "floor"):
    """领地灯用：整周的次数（天数）。"""
    s = STANDARDS[key]
    return getattr(s, which)


def full(key: str, start: date, day: date, which: str = "target") -> float:
    """属性分用：这段（start..day，含两头）按标准该做到多少，满分 = 这个数。"""
    s = STANDARDS[key]
    days = (day - start).days + 1
    return getattr(s, which) * days / (30 if s.per == "month" else 7)


def _n(v) -> str:
    return f"{v:g}"


def week_label(key: str, which: str = "floor") -> str:
    """领地灯“目标 / 保底”那两格：每周 5 天、每天、每周 1 次、每月 3 个、每周 2 小时。"""
    s = STANDARDS[key]
    v = getattr(s, which)
    if s.unit == "分钟":
        return f"每周 {_n(v / 60)} 小时"
    if s.per == "month":
        return f"每月 {_n(v)} {s.unit}"
    return "每天" if v >= 7 and s.unit == "天" else f"每周 {_n(v)} {s.unit}"


def window_label(key: str, days: int = 30) -> str:
    """属性分“标准”那一格：每周 5 天（30 天约 21 天），保底不一样时带上保底。"""
    s = STANDARDS[key]
    base = week_label(key, "target")
    per = 30 if s.per == "month" else 7
    n = s.target * days / per
    approx = f"{_n(round(n / 60, 1))} 小时" if s.unit == "分钟" else f"{_n(round(n))} {s.unit}"
    text = base if (s.per == "month" and days == 30) or base == "每天" else f"{base}（{days} 天约 {approx}）"
    return text + (f"；保底{week_label(key, 'floor')}" if s.floor != s.target else "")
