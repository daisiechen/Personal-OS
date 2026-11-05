"""说到做到（她 2026-10-31 定）：小伴 嘴上说“查了、翻了、发了、订了”，代码核对这一轮他是不是真做了。

起因：10-31 上午他说“我翻了你的邮件，没找到订单确认”，其实那时根本没有搜邮箱的功能，他没搜。
她说：“悄悄打回重写吧，我看不到。但是他刚才说查过了，其实没查，这个要记在 OS 里……统计一下他这种说了查过了、
但其实没查的动作有多少，用于后续你对它的维护和升级。”

做法：
- check(text, ctx)：回复里有“做过了”的说法、这一轮却没有对应动作的结果 → 记一笔虚报（deed_claims），
  聊天那边悄悄让他重写一次（她看不到），重写后再核一次，记下改没改对。
- 她自己发现的（聊天里说“你没做就说做了”），小伴 写 [虚报 他说过的话]，同样记一笔。
- summary()：周报和开发会话看这一张统计，决定下一步给他加什么能力、改什么规矩。
运行：python3 -m ethan.deeds --data <数据目录>（打印最近 7 天和全部的统计）
"""

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from .clock import her_today

SCHEMA = """CREATE TABLE IF NOT EXISTS deed_claims (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, source TEXT NOT NULL, kind TEXT NOT NULL, quote TEXT NOT NULL,
    fixed INTEGER, note TEXT)"""
# source：guard（发出去前代码拦下）/ her（她发现的）/ review（开发会话复盘发现的）
KINDS = {
    "mail": "说搜过她的邮箱",
    "web": "说查过官网、条款、网上",
    "sent": "说邮件发出去了",
    "booked": "说替她订好、约好了",
    "called": "说替她打过电话",
}
_D = r"[^。！？!?\n]"
RULES = [  # (类型, 说法)；“让 Grok 去查了”“我写好了你去发”这类不算
    ("mail", re.compile(rf"(翻|查|搜|找|看)(了|过|遍)(一下|一遍)?{_D}{{0,4}}(你的?)?(邮件|邮箱|收件箱)|(邮件|邮箱)里{_D}{{0,6}}(没找到|没搜到|没查到|找到了|搜到了|查到了)")),
    ("web", re.compile(rf"(?<!Grok)(?<!Grok )(查|翻|看|搜)(了|过|到)(一下)?{_D}{{0,6}}(官网|条款|网上|政策|退改规则|评价)|(官网|条款){_D}{{0,8}}我{_D}{{0,2}}(翻|查|看)到")),
    ("sent", re.compile(rf"(邮件|信){_D}{{0,6}}(已经发|发出去了|发过去了|已发送)|(我|已经)(帮你|替你|给你)?(发|寄)(了|出去了){_D}{{0,4}}(邮件|信)")),
    ("booked", re.compile(rf"(帮你|替你|给你){_D}{{0,4}}(订|预约|预订|约)(好|了|上了)")),
    ("called", re.compile(rf"(帮你|替你|给)(他们|客服|商家|店里|酒店|对方)?{_D}{{0,3}}打(了|过)电话")),
]
GROK_FRESH_HOURS = 2  # Grok 两小时内回报过，才算“查过官网”
MARK = re.compile(r"\[虚报\s+([^\]]+)\]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.execute(SCHEMA)


def evidence(ctx: dict, now: datetime = None) -> set:
    """这一轮真做了的：搜过邮箱、Grok 最近回报过。发邮件、订、打电话 小伴 自己做不了，永远没有。"""
    now = now or datetime.now(timezone.utc)
    got = set()
    if ctx.get("mail_lookups"):
        got.add("mail")
    for e in ctx.get("from_grok") or []:
        try:
            if now - datetime.fromisoformat(e["at"]) <= timedelta(hours=GROK_FRESH_HOURS):
                got.add("web")
        except (KeyError, ValueError, TypeError):
            continue
    return got


def check(text: str, ctx: dict, now: datetime = None) -> list:
    """回复里说做过、其实没做的：[(类型, 原话)]。"""
    text = MARK.sub("", text or "")
    have = evidence(ctx, now)
    hits = []
    for kind, rule in RULES:
        if kind in have:
            continue
        m = rule.search(text)
        if m:
            start = max(0, m.start() - 12)
            hits.append((kind, text[start:m.end() + 12].strip()))
    return hits


def note(hits: list) -> str:
    """悄悄让他重写时加在系统提示最后的一段（不会发给她）。"""
    lines = "；".join(f"「{q}」（{KINDS[k]}）" for k, q in hits)
    return ("## 发出去之前的核对（只给你看）\n"
            f"你刚才写的回复里说了 {lines}，但这一轮你并没有真的做（系统里没有对应的结果）。重写这条回复："
            "能做的就先用暗号去做（比如搜邮箱写 [查邮件 关键词]、要查网上写 [交给Grok]…[/交给Grok]）；"
            "做不了的（发邮件、订、打电话）照实说还没做、你做不了、要谁来做。不要提到这次核对。")


def record(db, source: str, kind: str, quote: str, fixed=None, note_text: str = "") -> int:
    ensure(db)
    cur = db.conn.execute("INSERT INTO deed_claims (at, source, kind, quote, fixed, note) VALUES (?, ?, ?, ?, ?, ?)",
                          (_now(), source, kind if kind in KINDS else "other", (quote or "")[:300],
                           None if fixed is None else int(bool(fixed)), (note_text or "")[:300]))
    db.conn.commit()
    return cur.lastrowid


def note_chat(db, text: str) -> int:
    """她发现他没做就说做了：小伴 写 [虚报 他说过的话]。"""
    n = 0
    for quote in MARK.findall(text or ""):
        kind = next((k for k, rule in RULES if rule.search(quote)), "other")
        record(db, "her", kind, quote.strip())
        n += 1
    return n


def strip(text: str) -> str:
    return MARK.sub("", text)


def summary(db, today: date = None, days: int = 7) -> dict:
    """说到做到的统计：拦下几次、拦下后改对几次、她发现几次、按类型分。"""
    try:
        ensure(db)
        since = ((today or her_today()) - timedelta(days=days)).isoformat()
        rows = [dict(r) for r in db.conn.execute("SELECT source, kind, fixed FROM deed_claims WHERE at >= ?", (since,))]
    except Exception:
        return {}
    by = {}
    for r in rows:
        name = KINDS.get(r["kind"], "其他")
        by[name] = by.get(name, 0) + 1
    return {"虚报共几次": len(rows), "发出去前拦下": sum(r["source"] == "guard" for r in rows),
            "拦下后改对": sum(r["source"] == "guard" and r["fixed"] == 1 for r in rows),
            "她或复盘发现的": sum(r["source"] in ("her", "review") for r in rows), "按类型": by}


def main():
    p = argparse.ArgumentParser(description="说到做到：小伴 虚报的统计")
    p.add_argument("--data", type=Path, required=True)
    a = p.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        print(json.dumps({"最近 7 天": summary(store.db), "全部": summary(store.db, days=36500)}, ensure_ascii=False, indent=1))
    finally:
        store.close()


if __name__ == "__main__":
    main()
