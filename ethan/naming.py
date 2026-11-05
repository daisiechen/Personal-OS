"""文案规范里“标题式”的那一条（docs/design/ui-spec.md 第 5 节，她 2026-11-02 同意）：
副本、里程碑、任务、待办一律写成标题：动词开头、短，不写聊天原话、不写大白话长句。

她亲手写的名字不动；系统自动起的（计划器、小伴 的标记、聊天抽取、邮件、帮办）存之前过一遍 title()。
这里只做代码能确定的整理（去口头语、去句末标点和语气词、截短），不改意思；写得好不好靠各处提示词里的同一条规矩。
"""

import re

MAX = 30
LEAD = re.compile(r"^(帮我|帮忙|麻烦|记得要?|记一下|提醒我|我要|我得|我需要|我想|要不要|得去|今天要|明天要)")
FILLER = re.compile(r"一下|一下子")
TAIL = re.compile(r"[\s。！!？?~～…,，、;；:：]+$|(吧|呀|哦|啦|呢|嘛|哈)+$")
RULE = "名字写成标题式：动词开头、短（30 字以内），不写聊天原话、不写“帮我”“记得”“一下”这类口头语，比如“递交芬兰签证材料”。"


def title(text: str) -> str:
    t = " ".join(str(text or "").split())
    t = LEAD.sub("", t).strip()
    t = FILLER.sub("", t)
    for _ in range(3):
        t = TAIL.sub("", t).strip()
    if len(t) > MAX:  # 太长：在第一个标点处断开，断不开就硬截
        cut = re.split(r"[，,；;。（(]", t)[0].strip()
        t = cut if 4 <= len(cut) <= MAX else t[:MAX]
    return t or " ".join(str(text or "").split())[:MAX]
