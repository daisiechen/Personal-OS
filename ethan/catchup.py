"""额度恢复后自动补：昨天的日记、聊天记忆，等着出的昨日卡。

11-06 凌晨每晚整理一调 Claude 就报额度满，整个进程退出，昨天的日记、聊天记忆都没写；06:31 出卡时补写日记、
卡片导演也都因额度失败，卡退回按训练和天气随机拼图。09:00 UTC 额度恢复后没有任何东西自动重试，
直到她 13:37 打开日记本才补出日记，卡只换了那句话。她下午 1 点问：“伊森昨天的日记为什么还没写完？”

现在：主动消息每 10 分钟那一轮（ethan/proactive.py）顺手问一句 due()——便宜、只读文件和数据库，不调模型。
有该补的、额度停跑标记不在（ethan/core/quota.py）、这一样一小时内没试过，就另起进程 python3 -m ethan.catchup 补：
- journal：昨天有聊天但没有聊天记忆 journal/<昨天>（每晚整理第一步没跑成）→ 重跑 nightly.run；
- diary：昨天的日记还没写 → diary.backfill（开始以来漏的一起补）；
- card：今天的昨日卡在等（日记补不出、或导演没编出来，记在 card-wait.json），
  或今天的卡是导演没编出来时退回随机拼的（卡文件没有 shot）
  → 再出一次（图、卡名、那句话一起；退回拼的那张重出不成就原样留着）；还不行就接着等，不生图；卡已经出了、只是日记后来补上对不上 → 照旧只重写那句话（cards.refresh）。
每晚整理还没跑过今天这一轮（她那边 12 点前）不补，免得和它抢着写同一天。
每样一小时最多试一次（试之前先记时间，进程中途死了也算试过），停跑标记在时一样都不试，不反复刷额度。
运行：python3 -m ethan.catchup --data <数据目录>
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE = "catchup.json"      # 每样最近一次试的时间（UTC）：{"journal": ..., "diary": ..., "card": ...}
EVERY = timedelta(hours=1)
NOON = 12                   # 她那边过了 12 点，每晚整理没跑也补


def _now(now=None):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _load(root) -> dict:
    try:
        d = json.loads((Path(root) / STATE).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _tried(root, key: str, now: datetime):
    s = _load(root)
    s[key] = now.isoformat(timespec="seconds")
    path = Path(root) / STATE
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def recently_tried(root, key: str, now: datetime = None) -> bool:
    at = _load(root).get(key)
    try:
        return bool(at) and _now(now) - datetime.fromisoformat(at) < EVERY
    except ValueError:
        return False


def nightly_done(root, settings=None, now: datetime = None) -> bool:
    """每晚整理今天这一轮跑过了（开始时间按她的时区是今天、而且已经结束），或者她那边已经过了中午。"""
    from . import jobs
    from .clock import her_now, her_tz
    here = her_now(settings, now)
    if here.hour >= NOON:
        return True
    rec = jobs.load(root).get("memory") or {}
    try:
        started = datetime.fromisoformat(rec["started"]).astimezone(her_tz(settings))
        finished = datetime.fromisoformat(rec["finished"])
        running = rec.get("running_since") and datetime.fromisoformat(rec["running_since"]) > finished
    except (KeyError, TypeError, ValueError):
        return False
    return started.date() == here.date() and not running


def card_need(root, db, day) -> str:
    """今天的卡要补什么："make" 在等日记 / 导演还没出，或卡是导演没编出来时退回拼的（没有 shot，11-06 那张）；
    "refresh" 出了但日记后来变了（只重写那句话）；没有就空。"""
    from . import cards
    if cards.waiting(root, day) or cards.patched(root, day):
        return "make"
    if not (Path(root) / "cards" / f"{day.isoformat()}.json").exists():
        return ""
    try:
        return "refresh" if cards.stale(root, db, day) else ""
    except Exception:
        return ""


def due(store, settings=None, now: datetime = None) -> list:
    """现在该补哪几样（不调模型）。额度停跑标记在、或每晚整理还没跑今天这一轮，一样都不补。"""
    from . import diary
    from .clock import her_today
    from .core import quota
    root = store.root
    if quota.blocked(root, now) or not nightly_done(root, settings, now):
        return []
    today = her_today(settings, now)
    yday = today - timedelta(days=1)
    out = []
    if not store.wiki.exists(f"journal/{yday.isoformat()}"):
        from . import nightly
        if nightly.transcript(store, yday, limit=5):
            out.append("journal")
    diary.ensure(store.db)
    if not diary.get(store.db, yday.isoformat()):
        out.append("diary")
    if card_need(root, store.db, today) or ("diary" in out and (Path(root) / "cards" / f"{today.isoformat()}.json").exists()):
        out.append("card")  # 日记这轮补上了，卡多半也要跟着对一遍
    from . import bgjobs  # 停跑期被拦下的别的后台（后台任务表 ethan/bgjobs.py 的 CATCHUP，决定 7）
    out += [k for k in bgjobs.catchups(root) if k.startswith("rerun:") and k not in out]
    if bgjobs.skipped(root).get("replay"):
        out.append("replay")
    return [k for k in out if not recently_tried(root, k, now)]


def run(store, router, settings=None, now: datetime = None, maker=None, ledger=None) -> dict:
    """补该补的，每样一句结果。每样试之前先记时间；中途看到额度停跑标记就不再往下试。"""
    from .clock import her_today
    from .core import quota
    root = store.root
    today = her_today(settings, now)
    yday = today - timedelta(days=1)
    got = {}
    for key in due(store, settings, now):
        if quota.blocked(root, now):
            got[key] = "额度停跑标记在，不试"
            break
        _tried(root, key, _now(now))
        try:
            if key == "journal":
                from . import nightly
                got[key] = nightly.run(store, router, yday)
            elif key == "diary":
                from . import cards, diary
                cards.weather(root, yday)  # 日记写昨天的实际天气（日记只读缓存，这里先取好）
                got[key] = diary.backfill(store, router, today)
            elif key == "card":
                got[key] = _card(store, router, today, maker, ledger)
            elif key.startswith("rerun:"):
                got[key] = _rerun(root, key.split(":", 1)[1])
            elif key == "replay":
                got[key] = _replay(root, now)
            _done(root, key)
        except Exception as e:
            got[key] = f"没成：{type(e).__name__} {str(e)[:200]}"
    return got


def _done(root, key: str):
    """补过了：这一样对应的“被拦下”记录清掉（没补成的会在下次停跑时再记）。"""
    from . import bgjobs
    bgjobs.clear_skipped(root, [p for p, j in bgjobs.TABLE.items() if j.catchup == key])


def _rerun(root, name: str) -> str:
    """照定时任务那条命令再跑一次，经 ethan.jobs 留痕（体检看得到）。"""
    from . import bgjobs, jobs
    code = jobs.run(Path(root), name, [sys.executable, *bgjobs.RERUN[name], "--data", str(root)])
    return "补完" if code == 0 else f"没成（退出码 {code}）"


def _replay(root, now=None) -> str:
    """停跑期被拦下的那几次起进程，照原样再起（起了就从清单里去掉）。"""
    from . import bgjobs
    from .bgwork import kick
    items = list(bgjobs.skipped(root).get("replay") or [])
    started = [it for it in items if kick(root, it[0], it[1:], now=now)]
    bgjobs.clear_skipped(root, replay=started)
    return f"重起 {len(started)}/{len(items)} 个"


def _card(store, router, today, maker, ledger) -> str:
    from . import cards
    need = card_need(store.root, store.db, today)
    if need == "make":  # 退回拼的那张：旧的先挪开再出，没编出来挪回来（cards.remake），她的卡不丢
        make = cards.remake if cards.patched(store.root, today) else cards.make_card
        card = make(store.root, store.db, today, None,  # 今天的计划不进昨日卡
                    maker=maker, ledger=ledger, router=router, store=store)
        return (f"接着等：{card['waiting']}" if card.get("waiting") else f"没重出，原来那张留着：{card['redo_failed']}"
                if card.get("redo_failed") else f"出了：{card.get('title')}")
    if need == "refresh":
        card = cards.refresh(store.root, store.db, today, router, store=store)
        return f"日记补上了，那句话重写：{card.get('line')}"
    return "卡不用动"


def kick(store, settings=None, now: datetime = None, spawn=None) -> list:
    """主动消息那一轮调：有该补的就另起进程补（30 分钟内只起一个，锁在 bgwork.kick 里），不拖慢主动消息。返回要补的几样。"""
    from .bgwork import kick as _kick
    todo = due(store, settings, now)
    if not todo or not _kick(store.root, "catchup", (), spawn, now=now):
        return []
    return todo


def main():
    p = argparse.ArgumentParser(description="额度恢复后补昨天的日记、聊天记忆，出等着的昨日卡")
    p.add_argument("--data", type=Path, required=True)
    a = p.parse_args()
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        router = build_router(a.data, 50.0)
        key = os.environ.get("OPENROUTER_API_KEY")
        maker = None
        if key:
            from .photos import OpenRouter
            maker = OpenRouter(key)
        print(f"[catchup] {run(store, router, router.settings, maker=maker, ledger=router.ledger)}", flush=True)
    finally:
        store.close()
        from .bgwork import release
        release(a.data, "catchup")


if __name__ == "__main__":
    main()
