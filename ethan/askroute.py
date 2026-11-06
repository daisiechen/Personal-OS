"""小伴 聊天按题分流（ontology-plan 3.1 还差的那块）。

她每条消息先用便宜、确定的办法（下面的规则表，正则，不调模型）判断是不是在问某道能力题：
- 命中：调对应的题函数（ethan/onto_query.py，函数自己记 function_log），结构化结果（答案、依据的说法编号、来源、缺失 / 有争议）
  放进这一轮提示词，标“按题查到的”；兜底检索照旧做，但 memsearch.search 记上 via="q<题号>"（不算兜底）；
- 拿不准（不像问句、该带的话题抽不出来、问的是别人的事）就不分流，照旧只有兜底检索。
每条消息最多命中一道题（规则表从上往下，先命中的算）。只分流到 小伴 登记过要答的题（memusers.USERS["ethan"]["questions"]）。
没权限和“没有”一样：题函数返回 unknown，提示词里写“记忆里没有”，不暴露有没有。
"""

import json
import re

from . import memusers, onto_query

# 像在问：有问号，或者句末是问句语气，或者开头是“你记得 / 你知道 / 帮我查”这类
ASKING = re.compile(r"[?？]|什么|哪|谁|多少|几次|来着|(吗|么|呢|啥|没有)[。!！~～\s]*$|你(还)?记得|你知道|帮我(查|看|翻)|告诉我")
FILLER = re.compile(r"你(还)?记得|你知道|帮我(查|看|翻)(一下|下)?|告诉我|请问|那个|这个|一下|我们|吗|么|呢|啥|什么|哪些|"
                    r"是不是|有没有|来着|我的|的事情?|那件事|这件事|关于|时候|当时|怎么说|的[?？]|[?？!！。、~～\s]")
PRONOUNS = {"你", "我", "他", "她", "它", "他们", "她们", "这人", "那人", "谁"}

# 规则表：(题号, 规则, 要不要话题)。规则里的 (?P<topic>…) 是要交给题函数的话题 / 名字；没写的从整句去掉触发词后取。
RULES = [
    (26, re.compile(r"(这周|本周|这礼拜|这星期|这个星期)(一共|总共)?(花了|花|开销|消费|支出|用了多少钱)"), False),
    (28, re.compile(r"我(说过|讲过|答应过?|提过)要(做|弄|干)?.{0,20}(还没|没做|没弄|没记|没进待办)|说过要做.{0,6}(还没|没)"), False),
    (27, re.compile(r"我(之前|以前|是不是|有没有)?(删|否掉|取消|划掉)(过|掉过|了)(?P<topic>.{1,20}?)(吗|么|没有?)?[?？]*$"), True),
    (23, re.compile(r"(?P<topic>.{1,20}?)(上次|之前|最近)?(是)?(谁|什么时候|为什么|为啥)(改|动|挪)(的|了|过)"), True),
    (18, re.compile(r"(?P<topic>.{1,20}?)(和|跟|比起?)(上个月|以前|之前|去年)(比)?.{0,6}(有什么|有啥|哪些)?(变化|不一样|变了)"), True),
    (11, re.compile(r"你(为什么|为啥|凭什么|怎么)(会)?(觉得|认为|猜)我(?P<topic>.{1,20})"), True),
    (5, re.compile(r"我(定|立)(过|的|下)(哪些|什么|啥)?(长期)?(规矩|规则|原则)|我的(规矩|规则)(?P<topic>.{0,20})"), False),
    (22, re.compile(r"(关于|对)?(?P<topic>.{0,20}?)我(之前|以前)?(做|定)(过|了)(哪些|什么|啥)(决定)"), False),
    (2, re.compile(r"我(上次|上回|之前|以前|那天)(是)?(说|讲|提|聊)(过|到|起)?(?P<topic>.{1,30})"), True),
    (3, re.compile(r"^(你(还)?(记得|知道))?(?P<topic>[^，。？?\s]{1,12}?)(是谁|是哪位|是干嘛的)|"
                   r"(?P<topic2>[^，。？?\s]{1,12}?)(和|跟)我(是)?(什么|啥)关系"), True),
    (4, re.compile(r"我(平时|一般)?(最)?(喜欢|爱吃|爱喝|爱|不吃|不喝|讨厌|受不了|偏好|不爱)(?P<topic>.{0,20})"), False),
    (1, re.compile(r"^(你(还)?(记得|知道))?我(现在|目前|最近|平时|这阵子)?(住|待|人)?(在)?(哪|哪儿|哪里|哪个城市)(呢|啊|呀|来着)?[?？]*$|"
                   r"我(现在|目前|最近)(在)?(干嘛|做什么|干什么|忙什么)"), False),
]
NAMES = {1: "现在在做什么、住哪、今天在哪", 2: "她上次说到这件事", 3: "这个人是谁、和她什么关系", 4: "她的偏好",
         5: "她定过的长期规矩", 11: "系统对她某个特点的猜测和依据", 18: "这件事前后有什么变化", 22: "和这件事有关的生效决定",
         23: "这件事上次谁、什么时候、为什么改的", 26: "这周的开销", 27: "这件事她删过、改过、否掉过吗", 28: "她说过要做、还没进待办的"}


def _topic(m, text: str) -> str:
    raw = m.groupdict().get("topic") or m.groupdict().get("topic2") or ""
    return FILLER.sub("", raw).strip().strip("是").strip()


def match(text: str, user: str = "ethan"):
    """她这句话问的是哪道题：返回 (题号, 话题) 或 None。规则拿不准就不分流。"""
    said = (text or "").strip()
    if not said or len(said) > 80 or not ASKING.search(said):
        return None  # 太长的多半是在讲事情不是在问；不像问句不分流
    allowed = set(memusers.check(user).get("questions", []))
    for q, rule, need_topic in RULES:
        if q not in allowed:
            continue
        m = rule.search(said)
        if not m:
            continue
        topic = _topic(m, said)
        if q == 3 and (not topic or topic in PRONOUNS):
            continue  # “你是谁”“他是谁”：没有名字，不是在问人物卡
        if need_topic and not topic:
            continue
        return q, topic or None
    return None


def ask(db, q: int, topic, user: str = "ethan") -> dict:
    """调对应的题函数（题函数自己记 function_log）。"""
    fn = getattr(onto_query, onto_query.QUESTIONS[q])
    if q in (1, 26, 28):
        return fn(db, user)  # 不用话题
    return fn(db, user, topic)  # 其余题第三个参数都是话题 / 名字 / 事情的标题


def route(db, text: str, user: str = "ethan"):
    """命中就返回 {"q", "name", "topic", "got"}；没命中返回 None（照旧兜底）。题函数出错也当没命中，不挡聊天。"""
    hit = match(text, user)
    if not hit:
        return None
    q, topic = hit
    try:
        got = ask(db, q, topic, user)
    except Exception as e:
        print(f"[askroute] 第 {q} 题没查成：{type(e).__name__} {e}", flush=True)
        return None
    return {"q": q, "name": NAMES.get(q, ""), "topic": topic, "got": got}


DROP = {"sources", "locked", "guess", "review", "kind", "statement", "before_statement", "evidence", "person", "decision"}


def _brief(item) -> str:
    if not isinstance(item, dict):
        return str(item)[:200]
    keep = {k: v for k, v in item.items() if k not in DROP and v not in (None, "", [], {})}
    return json.dumps(keep, ensure_ascii=False, default=str)[:300]


def prompt_text(hit: dict) -> str:
    """给提示词：答案 + 依据 + 来源 + 缺失 / 有争议。"""
    got = hit["got"]
    status = got.get("status")
    head = f"第 {hit['q']} 题（{hit['name']}）" + (f"，问的是「{hit['topic']}」" if hit.get("topic") else "")
    if status in ("unknown", "unavailable", "forbidden") or not got.get("items"):
        why = "记忆这会儿用不了" if status == "unavailable" else "记忆里没有"
        return f"{head}：{why}" + (f"（{got['note']}）" if got.get("note") else "") + "。别编，如实说不知道，可以问她。"
    lines = onto_query.lines(got) or "\n".join(f"- {_brief(i)}" for i in got["items"][:6])
    tail = []
    if got.get("basis"):
        tail.append("依据的说法编号：" + "、".join(f"#{b}" for b in got["basis"][:8]))
    if got.get("kinds"):
        tail.append("来源：" + "、".join(got["kinds"]))
    if status == "disputed":
        tail.append("有争议：两种说法并列，没定，别挑一个当真" + (f"（{got['note']}）" if got.get("note") else ""))
    elif got.get("note"):
        tail.append(f"说明：{got['note']}")
    return "\n".join([f"{head}：", lines, *tail])
