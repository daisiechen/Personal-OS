"""到点跟进和身体提醒（mias-os §6、territories.md 事业 / 身体健康线；2026-10-28 补上）。

主动消息每 10 分钟检查一次（ethan/proactive.py），这里只判断“现在该不该提一句、提什么”，发不发还要过免打扰、刚聊过、
连着没回这些关：
- 拆了惊喜：她在工作台拆开商店里的惊喜，小伴 主动来揭晓、说清接下来怎么玩。
- 出门提醒：她日历里本来就有、要出门的事，提前 1 小时提醒去哪、路上多久、几点出发（计划器排的已进苹果日历带提醒）。
- 到点跟进：今天计划里有开始时间的事，过了 20 分钟还没有结果、待办也没勾，问一句开始了没有；
  她不想做就给计划器写好的缩小版本（smaller），先做一小步也算。
- 久坐：Mac 上连续 90 分钟都在用（ethan/worktime.py 的 5 分钟格），提醒起来走走、喝口水；两次之间至少隔 2 小时。
- 喝水：上午 11 点、下午 3 点各一次，只在她那天在 Mac 上干活时提。
- 傍晚出门：接上 iPhone 定位以后，17:30–18:30 她今天还没出过门，不光催，要用一个具体好玩的去处勾她出门
  （2026-10-29 她说“别光催，引导一下、告诉我哪里有好玩的吸引我”）：先从她自己收藏的地方里挑，没有就让 Grok 查附近的。
"""

from datetime import datetime, time, timedelta

from . import places_log, worktime

# 只该提一次的（惊喜揭晓）：不按天记，揭晓过就不再提（2026-10-29 自查：按天记的话第二天会再“刚拆开”一遍）
ONCE = "CREATE TABLE IF NOT EXISTS nudge_once (key TEXT PRIMARY KEY, at TEXT NOT NULL)"


def redeem_key(r) -> str:
    return f"redeem:{r['id']}:{r['at']}"  # 带上时间：删掉的兑换编号可能被重用


def told(db, key: str) -> bool:
    db.conn.execute(ONCE)
    return db.conn.execute("SELECT 1 FROM nudge_once WHERE key = ?", (key,)).fetchone() is not None


def mark_told(db, keys):
    db.conn.execute(ONCE)
    db.conn.executemany("INSERT OR IGNORE INTO nudge_once (key, at) VALUES (?, datetime('now'))", [(k,) for k in keys])
    db.conn.commit()


FOLLOW_AFTER, FOLLOW_UNTIL = timedelta(minutes=20), timedelta(minutes=90)
SIT_SLOTS = 18  # 90 分钟
SIT_GAP = timedelta(hours=2)
WATER = {"water-am": time(11, 0), "water-pm": time(15, 0)}
OUTING = (time(17, 30), time(18, 30))


def _follow(db, now_local: datetime, done: dict):
    plan = db.latest_plan(now_local.date().isoformat())
    for i in (plan or {}).get("items", []):
        if not i.get("start") or i.get("result") or i["kind"] == "travel":
            continue
        key = f"follow:{i['start']}:{i['title']}"  # 不用条目编号：重排后编号会变，同一件事会再问一遍
        if key in done:
            continue
        h, m = map(int, i["start"].split(":"))
        start = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if start + FOLLOW_AFTER <= now_local <= start + FOLLOW_UNTIL:
            smaller = f"她要是不想做，就给这个缩小版本：{i['smaller']}，先做这一小步也算数。" if i.get("smaller") else \
                "她要是不想做，帮她想一个五分钟就能开始的小版本。"
            return key, (f"计划里 {i['start']} 的「{i['title']}」到点 20 分钟了，系统里还没看到结果。"
                         f"轻轻问一句开始了没有，别像查岗；{smaller}她已经在做了就给她打打气。"
                         "她说做完了，记得加 [结果 #编号 完成]（编号看今天计划）。")
    return None


def _surprise(db, now_local: datetime, done: dict):
    """她在工作台拆了惊喜：小伴 主动来找她揭晓（2026-10-29 她说拆开之后什么都没有，也不知道接下来怎么做）。"""
    from . import quests
    for r in quests.pending_redemptions(db):
        key = redeem_key(r)
        if key in done or told(db, key):
            continue
        return key, (f"她刚在工作台拆开了「{r['title']}」（兑换 #{r['id']}）。这是她攒积分换来的，你现在主动找她："
                     "先说你收到了、有点得意地卖个关子，再揭晓你给她准备的是什么、接下来怎么玩（今天能开始的就今天）；"
                     "要花她钱的先说大概多少、等她点头。真的兑现了加 [惊喜 #兑换编号 已兑现 一句话]。")
    return None


def _depart(db, now_local: datetime, done: dict):
    """她日历里本来就有、要出门的事（比如递签证）：提前 1 小时提醒准备，说清去哪、路上多久、几点出发。
    计划器自己排的事（训练）已经写进苹果日历带了提醒，这里不重复（2026-10-29 她问递签证为什么没有路程和出发时间）。"""
    import json
    plan = db.latest_plan(now_local.date().isoformat())
    rows = (json.loads(plan["brief"]).get("timeline") or []) if plan and plan.get("brief") else []
    for r in rows:
        x = r.get("trip")
        if not x or r.get("icon") != "⏰" or not r["text"].startswith("准备出发去"):
            continue
        key = f"depart:{x.get('start')}:{x.get('title')}"
        if key in done:
            continue
        h, m = map(int, r["at"].split(":"))
        at = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= now_local <= at + timedelta(minutes=30):
            way = f"路上约 {x['trip_min']} 分钟（{x.get('route') or '公共交通'}）" if x.get("trip_min") else ""
            return key, (f"她 {x.get('start')} 要去「{x.get('title')}」" + (f"（{x['place']}）" if x.get("place") else "")
                         + f"，{way}，{x.get('leave')} 得出门。轻轻提醒她准备一下、带好要带的东西，说清几点出发；一两句就好。")
    return None


def _sitting(db, now_local: datetime, done: dict):
    slots = {r[0] for r in db.conn.execute("SELECT slot FROM work_slots WHERE slot LIKE ?", (now_local.date().isoformat() + "%",))}
    cur = now_local.replace(minute=now_local.minute - now_local.minute % worktime.SLOT_MIN, second=0, microsecond=0)
    run = 0
    for k in range(1, SIT_SLOTS + 2):  # 从上一格往回数（这一格可能还没报上来）
        if (cur - timedelta(minutes=worktime.SLOT_MIN * k)).strftime("%Y-%m-%dT%H:%M") in slots:
            run += 1
        else:
            break
    last = done.get("sit_at")
    if run >= SIT_SLOTS and (not last or now_local - datetime.fromisoformat(last) >= SIT_GAP):
        return "sit", ("她已经在电脑前连续坐了一个半小时。提醒她起来走两步、伸伸腰、喝口水，"
                       "一两句就好，像你心疼她一直坐着；别讲道理，别列清单。")
    for key, at in WATER.items():
        if key not in done and slots and at <= now_local.time() < (datetime.combine(now_local.date(), at) + timedelta(minutes=40)).time():
            return key, "她今天一直在忙。顺口提醒她喝杯水，一句就够，可以带点你自己此刻在干嘛。"
    return None


def _outing(db, now_local: datetime, done: dict):
    if "outing" in done or not (OUTING[0] <= now_local.time() < OUTING[1]) or not places_log.has_data(db):
        return None
    left = [a for a, _ in places_log.trips(db, now_local.date(), now_local.date())]
    if left:
        return None
    from . import brain
    try:
        picks = [i for i in brain.ideas(db, limit=12) if i["where"]][:4]
    except Exception:
        picks = []
    if picks:
        lst = "；".join(f"{i['title']}（{i['where']}）" for i in picks)
        where = (f"她自己收藏过这些地方：{lst}。挑一个此刻最合适的（近、现在开着、适合傍晚），"
                 "说出它具体好在哪（那里有什么、去了能干嘛、为什么现在去正好），用它把她勾出门。")
    else:
        where = ("她收藏里还没有合适的地方：你自己说一个伦敦傍晚好玩的具体去处（一条河边步道、一家小书店、一个夜市、一场免费的展），"
                 "说出具体好在哪；拿不准开没开门就交给 Grok 查附近现在有什么好玩的，查到再告诉她。")
    return "outing", ("她今天还没出过门（手机定位看到的）。傍晚了，别光催她出去，要引导、要吸引她："
                      + where + "可以约她“回来跟我讲讲”或者“拍张照给我看”，语气轻松带点撩，不要说教，也不要列一串选项。")


def due(db, now_local: datetime, done: dict):
    """返回 (状态键, 场合说明) 或 None。done：今天已经提过的（proactive 的当天状态）。"""
    worktime.ensure(db)
    for check in (_surprise, _depart, _follow, _sitting, _outing):
        got = check(db, now_local, done)
        if got:
            return got
    return None
