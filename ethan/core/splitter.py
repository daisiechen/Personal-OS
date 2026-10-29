"""把一段回复拆成像真人发微信的多条短消息。"""

import re

SENTENCE_END = re.compile(r"(?<=[。！？!?～~])")
SOFT_BREAK = re.compile(r"(?<=[，,；;])")


def split_bubbles(text: str, max_len: int = 40) -> list:
    bubbles = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 一行就是一条消息；只有太长的行才按句子、逗号拆（2026-10-22 Mia 嫌每次都满满三条）
        sentences = [line] if len(line) <= max_len else SENTENCE_END.split(line)
        for sentence in (s.strip() for s in sentences):
            if not sentence:
                continue
            if len(sentence) <= max_len:
                bubbles.append(sentence)
                continue
            chunk = ""
            for part in SOFT_BREAK.split(sentence):
                if chunk and len(chunk) + len(part) > max_len:
                    bubbles.append(chunk.strip())
                    chunk = ""
                chunk += part
            if chunk.strip():
                bubbles.append(chunk.strip())
    return [_drop_trailing_period(b) for b in bubbles]


def _drop_trailing_period(bubble: str) -> str:
    # 真人发消息句尾常不带句号；问号、感叹号保留
    return bubble[:-1] if bubble.endswith(("。", "，", ",")) and len(bubble) > 1 else bubble


def limit_bubbles(bubbles: list, max_count: int = 3) -> list:
    """Mia 嫌一次回太多条（2026-10-22）：超出的部分并进最后一条。"""
    if len(bubbles) <= max_count:
        return bubbles
    head = bubbles[:max_count - 1]
    return head + ["，".join(bubbles[max_count - 1:])]


def typing_delay(bubble: str) -> float:
    """按字数模拟打字时间，0.5–1.5 秒；模型本身已经要想一会儿，不再刻意拖慢。"""
    return max(0.5, min(1.5, len(bubble) * 0.06))
