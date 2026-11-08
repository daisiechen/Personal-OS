"""打扰登记表（docs/design/ontology.md 6.4 节，system-rules.md 第 7 节第 7 条）：所有主动找她的东西都要登记，没登记的不准发。

每一种登记：名字、形式、什么时候触发、点了去哪、频率上限、守不守免打扰（例外写理由）、去重键的样子。
红点（工作台 badges）的键也要在这里，并写清点进去是哪个列表（数字等于那个列表的条数，ethan/invariants.py 检查）。

2026-11-06 起（全系统模块化第一批）：RULES 是真正执行的规则表，不再只是文字——
免打扰、刚聊过、连着没回、只问一次、两次隔多久、几点到几点，都只在 ethan/proactive.py 的 gate() 里按这张表判断；
所有主动消息（含 Grok 回报转告、DemoApp 举报提醒、手动发的总览）都先过 gate()，没登记的不发。
设计和规则表的大白话版：docs/design/nudges.md。
"""

from dataclasses import dataclass
from datetime import time, timedelta

# (前缀, 名字, 形式, 触发, 去哪 / 她怎么处理, 频率上限, 免打扰)
MESSAGES = [
    ("brief", "早报", "主动消息", "每天早上计划排好后", "聊天里看；工作台首页", "每天 1 次", "守"),
    ("morning", "上午固定时段", "主动消息", "没有剧本时的固定时段", "聊天", "每天 1 次", "守"),
    ("afternoon", "下午固定时段", "主动消息", "同上", "聊天", "每天 1 次", "守"),
    ("evening", "晚上固定时段", "主动消息", "没有剧本时的固定时段", "聊天", "每天 1 次", "守"),
    ("evening:", "晚上收工（evening:日期:档）", "主动消息", "21/22/23 点她还在工作", "聊天", "每档每晚 1 次", "收工提醒刚聊过也说，只守免打扰"),
    ("bedtime", "睡前固定时段", "主动消息", "同上", "聊天", "每天 1 次", "守"),
    ("m-", "剧本时刻", "主动消息", "小伴 当天剧本里写的时刻", "聊天", "每个时刻 1 次", "守"),
    ("follow:", "到点跟进", "主动消息", "计划里有时间的事过了 20 分钟还没结果", "聊天；她说做没做", "每件 1 次", "守"),
    ("redeem:", "惊喜揭晓", "主动消息", "她在工作台拆了惊喜", "聊天", "每次兑换 1 次", "守"),
    ("redeem-check:", "惊喜玩了没有", "主动消息", "揭晓两天后还没记兑现", "聊天", "每两天最多 1 次", "守"),
    ("can-redeem:", "积分够换惊喜", "主动消息", "积分够某一档", "工作台商店", "每档每次够了 1 次", "守"),
    ("clash:", "约撞时间", "主动消息", "邮件 / 聊天记的约和她日历撞了", "聊天里选去哪个", "每个约 1 次", "守"),
    ("plan-clash:", "计划和日历撞了", "主动消息", "计划里的事和她后来在日历上约的撞了", "聊天里点头就挪", "每对 1 次", "守"),
    ("route:", "路线排不开", "主动消息", "今天的路线赶不上", "聊天里点头就挪", "每个冲突 1 次", "守"),
    ("prep-ask:", "准备员问要不要做", "主动消息", "新记下要替她准备的事", "聊天里说要不要；工作台 小伴 在准备", "每件 1 次，两件隔 2 小时", "守"),
    ("promise:", "答应的事到点报进度", "主动消息",
     "小伴 答应的事到了说好的时间（标记里的几点；只写日期按那天 11:00；没写按当天 19:00 / 第二天 10:30），9–21 点",
     "聊天里说行就通过、要改就改；工作台“小伴 在准备”看全文",
     "每件“卡住”最多 1 次、“做好了”1 次；两次隔 2 小时；她已知道的不报；过时两天的不翻", "守"),
    ("checkin-ask", "打卡提醒", "主动消息", "晚上 9 点还有日常没打卡", "聊天里说做了就替她打", "每天 1 次", "守"),
    ("depart:", "出门提醒", "主动消息", "她日历上要出门的事，出发前 1 小时", "聊天", "每个约 1 次", "守"),
    ("meet:", "见面前提醒（这个人最近怎样、上次聊到哪、要记得的）", "主动消息",
     "她日历上和人物卡里某个人的约：前一晚 20:00–22:30，或没赶上时见面前 2.5–1 小时（借鉴 10，ethan/meetprep.py）",
     "聊天", "每个约 1 次", "守"),
    ("sit", "久坐", "主动消息", "Mac 上连续 90 分钟", "聊天", "两次隔 2 小时", "守"),
    ("water-am", "上午喝水", "主动消息", "11 点、她在 Mac 上干活", "聊天", "每天 1 次", "守"),
    ("water-pm", "下午喝水", "主动消息", "15 点、她在 Mac 上干活", "聊天", "每天 1 次", "守"),
    ("outing", "傍晚出门", "主动消息", "17:30–18:30 今天还没出门", "聊天", "每天 1 次", "守"),
    ("perk:", "权益到期", "主动消息", "会员、积分、保险到期前 30 / 7 / 1 天", "聊天", "每档 1 次", "守"),
    ("week-plan:", "下周餐单和训练排好了", "主动消息", "周日 17 点后", "工作台首页", "每周 1 次", "守"),
    ("mail:", "邮件和留言汇总", "主动消息", "傍晚有要她看的邮件或留言", "工作台邮件和留言", "每批 1 次", "守"),
    ("gap-ask:", "补缺口的提问", "聊天里顺口问", "她来聊天、今天挑了题（每天最多 2 个，ontology 1.7）", "聊天里答，或“我”页待你定里写",
     "同一题三天内不重复；她说“今天别问”当天不问", "她来聊天时顺口问；主动来问的只有每天一次的“聊聊”（lesson-chat）"),
    ("lesson-chat:", "聊聊（今天的知识点 + 一个补缺口的问题）", "主动消息",
     "15:00–20:30、她此刻没在开会训练、和 小伴 最近 2 小时没在聊、今天的知识点还没讲或补缺口还没问",
     "聊天里接着聊；补缺口也能在“我”页待你定里答", "每天 1 次；两样都讲过问过就不发；“今天别问”只讲知识点", "守"),
    ("weekend:", "这周末可以去（3–5 个：收藏优先、Grok 补本周新的、舞蹈单列）", "主动消息",
     "周五 12:00–20:30；Grok 派出去还没回时 16 点前先等（ethan/weekend.py）",
     "聊天里说想去哪个，小伴 写 [日程 …] 排进日历；要付钱的等她点头", "每周 1 次", "守"),
    ("ask:", "问她说过的事结果怎样", "主动消息", "她说过的面试、约会这类事到点了（life.due_her_thing）", "聊天", "每件 1 次", "守"),
    ("grok-relay:", "Grok 回报转告", "主动消息", "她在聊天里托的事，Grok 回报到了",
     "聊天", "每批 1 次", "守免打扰：夜里到的早上 8 点后再说（1）；刚聊过照说（她在等结果）"),
    ("fancy-urgent:", "DemoApp 紧急举报", "主动消息（不经 小伴）", "DemoApp 推来紧急举报", "DemoApp 后台链接",
     "10 分钟内按条数合并", "不守：紧急照发（2）"),
    ("fancy:", "DemoApp 早晚两次举报汇总", "主动消息（不经 小伴）", "DemoApp 推来早 / 晚汇总", "DemoApp 后台链接",
     "同一类 10 分钟内 1 次", "守：免打扰里到的先存着，结束后由主动消息那一轮补发（2）"),
    ("overview:", "手动发的总览", "主动消息", "命令行 python3 -m ethan.report（不带 --weekly）", "聊天", "每次手动 1 次", "守"),
    ("review:", "每周复盘开场", "主动消息", "周日 21:00（按日历上那一次）", "聊天里答单子；工作台复盘记录", "每周 1 次", "约好的，刚聊过也照样开场，只守免打扰"),
]

@dataclass(frozen=True)
class Rule:
    """一类主动消息的打扰规则（proactive.gate 按它判断，别处不再各写一遍）。"""
    quiet: bool = True          # 守免打扰（/quiet 设的时段，默认 00:00–08:00）
    recent: str = "her1h"       # 刚聊过就不发：None 不看；her1h 她 1 小时内说过话；any2h 她或 小伴 2 小时内说过话
    unanswered: str = "today"   # 没回就不发：None 不看；today 今天连着超过 2 条主动消息没回；last 上一条主动消息没回
    once: str = "day"           # 去重：day 当天说过不再说（按天的状态文件）；ever 说过永不再说（nudge_once 表）；None 不去重（靠 gap）
    window: tuple = None        # 她本地时间几点到几点（"HH:MM"，结束不含）；None 不限
    gap: timedelta = None       # 同一类两次之间至少隔多久（按 nudge_sent 发送记录）
    switch: bool = True         # 她 /proactive off 时停
    hint: bool = False          # 只问一次、事情没了结就挂在首页的小提示上（3；“事情没了结就一直挂着”）
    why: str = ""               # 和默认不一样的地方，写原因


H2 = timedelta(hours=2)
# 每一类一行；名字、触发、去哪见上面 MESSAGES。窗口、间隔的数字只写在这里（各检查函数用 in_window 读）。
RULES = {
    "brief": Rule(recent=None, unanswered=None, window=("07:30", "11:00"), why="早报是一天的开头：刚聊过、昨天没回都照发"),
    "morning": Rule(unanswered=None, window=("08:00", "09:30"), why="固定时段（没剧本时）以前就不看没回"),
    "afternoon": Rule(unanswered="last", window=("14:00", "17:00"), why="下午这条可有可无：上一条没回就不发"),
    "evening": Rule(unanswered=None, window=("21:00", "22:15"), why="固定时段（没剧本时）以前就不看没回"),
    "bedtime": Rule(unanswered=None, window=("23:00", "23:40"), why="固定时段（没剧本时）以前就不看没回"),
    "m-": Rule(why="剧本时刻按深圳时间，窗口是剧本里写的那一刻起 1 小时（proactive.MOMENT_WINDOW）"),
    "ask:": Rule(),
    "follow:": Rule(),
    "redeem:": Rule(once="ever"),
    "redeem-check:": Rule(once="ever", window=("10:00", "21:00")),
    "can-redeem:": Rule(once="ever", window=("10:00", "21:00")),
    "clash:": Rule(once="ever", hint=True),
    "plan-clash:": Rule(once="ever", hint=True),
    "route:": Rule(once="ever", hint=True),
    "prep-ask:": Rule(once="ever", window=("10:00", "20:00"), gap=H2, why="所有都问，但别同一时间问"),
    "promise:": Rule(once="ever", window=("09:00", "21:00"), gap=H2),
    "checkin-ask": Rule(once="ever", window=("21:00", "23:00")),
    "depart:": Rule(),
    "meet:": Rule(once="ever"),
    "sit": Rule(once=None, gap=H2, why="一天可以提几次，两次隔 2 小时"),
    "water-am": Rule(window=("11:00", "11:40")),
    "water-pm": Rule(window=("15:00", "15:40")),
    "outing": Rule(window=("17:30", "18:30")),
    "perk:": Rule(once="ever", window=("10:00", "20:00")),
    "week-plan:": Rule(once="ever", window=("17:00", "24:00")),
    "mail:": Rule(once="ever", window=("18:00", "21:00")),
    "evening:": Rule(recent=None, unanswered=None, once="ever", why="收工提醒：她可能正跟你聊工作，刚聊过、没回也说"),
    "lesson-chat:": Rule(recent="any2h", window=("15:00", "20:30"), why="她和 小伴 最近 2 小时在聊就不来开话题"),
    "weekend:": Rule(once="ever", window=("12:00", "20:30")),
    "review:": Rule(recent=None, unanswered=None, why="约好的复盘像开会：刚聊过也照样开场"),
    "grok-relay:": Rule(recent=None, unanswered=None, once=None, window=("08:00", "24:00"), switch=False,
                        why="她托办的事的结果：她在等，刚聊过照说；夜里到的早上 8 点后再说（1）；"
                            "/proactive off 不挡（不是 小伴 自己开话题）；去重靠 grok-relay.json"),
    "fancy-urgent:": Rule(quiet=False, recent=None, unanswered=None, once=None, switch=False,
                          why="紧急举报：免打扰也照发（2）；去重在 fancy_moderation"),
    "fancy:": Rule(recent=None, unanswered=None, once=None, switch=False,
                   why="早晚两次常规的守免打扰，挡住的先存着、结束后补发（2）；去重在 fancy_moderation"),
    "overview:": Rule(recent=None, unanswered=None, once=None, why="她让发的总览（手动命令）"),
}


def rule(key: str):
    """状态键对应的规则；没登记返回 None。按最长前缀匹配（和 lookup 一样）。"""
    hits = [p for p in RULES if key == p or key.startswith(p)]
    return RULES[max(hits, key=len)] if hits else None


def _hm(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(23, 59, 59, 999999) if h == 24 else time(h, m)


def in_window(key: str, now_local) -> bool:
    """现在（她本地时间）在不在这一类的时段里；没写时段的总是在。"""
    r = rule(key)
    if r is None or not r.window:
        return True
    start, end = _hm(r.window[0]), _hm(r.window[1])
    t = now_local.time() if hasattr(now_local, "time") else now_local
    return start <= t < end if start <= end else (t >= start or t < end)


def window(key: str) -> tuple:
    """(开始, 结束) 的 time；没写返回 None。"""
    r = rule(key)
    return (_hm(r.window[0]), _hm(r.window[1])) if r and r.window else None


def once_prefixes() -> tuple:
    """发出去要记进 nudge_once（换一天也不再说）的前缀。"""
    return tuple(p for p, r in RULES.items() if r.once == "ever")


def hint_prefixes() -> tuple:
    """问过一次、事情没了结就挂在首页小提示上的（nudges.hints；首页一行，了结了或点 × 才收）。"""
    return tuple(p for p, r in RULES.items() if r.hint)


# 导航和按钮上的红点：键 → 点进去是哪个列表
BADGES = {
    "brain": "“我”页 → 收藏和资料里 小伴 拿不准、要她定的",
    "main": "首页“小伴 今天在准备” → 做好了等她看的",
    "desk": "首页“邮件和留言” → 等她处理的",
    "me": "“我”页 → 待你定（加上收藏和资料里要她看的）",
    "quests": "首页积分 → 商店里能拆的惊喜",
    "map": "赛季页“七个领域”旁的“待归属” → 还没定领域的待办",
    "parked": "首页“小伴 今天在准备”上的小红点 → 弹层里“⏸ 先放着”那栏（同样的红点和数，点开看过就消失；state.parked_remind）",
}

# 苹果日历里的提醒（日历 App 自己弹）
CALENDAR = [
    ("计划里的事", "出发前 1 小时提醒（计划器写进“小伴 计划”日历）"),
    ("每周复盘", "周日 21:00 前 10 分钟提醒"),
]


def lookup(key: str):
    """状态键对应的登记项；没登记返回 None。按最长前缀匹配。"""
    hits = [row for row in MESSAGES if key == row[0] or key.startswith(row[0])]
    return max(hits, key=lambda r: len(r[0])) if hits else None


def registered(key: str) -> bool:
    """登记了（MESSAGES 里有名字、RULES 里有规则）才算。"""
    return lookup(key) is not None and rule(key) is not None
