"""到点跟进和身体提醒（mias-os §6、territories.md 事业 / 身体健康线；2026-10-28 补上）。

主动消息每 10 分钟检查一次（ethan/proactive.py），这里只判断“现在该不该提一句、提什么”，发不发统一过 proactive.gate()
（按 ethan/nudge_registry.py 的 RULES：免打扰、刚聊过、连着没回、只问一次、间隔、时段，11-06 起只在那一处判断）。
各检查函数里的时段数字从登记表读（nudge_registry.in_window），“说过没有”用 fresh()，不再各写一遍：
- 拆了惊喜：她在工作台拆开商店里的惊喜，小伴 主动来揭晓、说清接下来怎么玩。
- 出门提醒：她日历里本来就有、要出门的事，提前 1 小时提醒去哪、路上多久、几点出发（计划器排的已进苹果日历带提醒）。
- 到点跟进：今天计划里有开始时间的事，过了 20 分钟还没有结果、待办也没勾，问一句开始了没有；
  她不想做就给计划器写好的缩小版本（smaller），先做一小步也算。
- 久坐：Mac 上连续 90 分钟都在用（ethan/worktime.py 的 5 分钟格），提醒起来走走、喝口水；两次之间至少隔 2 小时。
- 喝水：上午 11 点、下午 3 点各一次，只在她那天在 Mac 上干活时提。
- 权益到期：会员积分、等级、续费、保险续保，提前 30 / 7 / 1 天各提一次（ethan/perks.py）。
- 邮件：傍晚 18–21 点把当天需要她亲自看的邮件汇总成一条（ethan/mail.py）。
- 傍晚出门：接上 iPhone 定位以后，17:30–18:30 她今天还没出过门，不光催，要用一个具体好玩的去处勾她出门
  （“别光催，引导一下、告诉我哪里有好玩的吸引我”）：先从她自己收藏的地方里挑，没有就让 Grok 查附近的。
- 答应的事到点报进度：小伴 答应她的事到了说好的时间，做好了给结果、没做好说卡在哪（ethan/prep.py promise_due）。
- 周五“这周末可以去”：12:00–20:30 推荐 3–5 个，收藏优先、Grok 补本周新的、舞蹈单列（ethan/weekend.py）。
- 见面前提醒：日历上和人物卡里某个人的约，前一晚 20:00–22:30 或见面前 2.5–1 小时，提一句这个人最近怎样（ethan/meetprep.py）。
"""

from datetime import datetime, timedelta, timezone

from . import nudge_registry, places_log, worktime
from .nudge_registry import in_window

# 只该提一次的（惊喜揭晓）：不按天记，揭晓过就不再提（自查：按天记的话第二天会再“刚拆开”一遍）
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


def fresh(db, key: str, done: dict) -> bool:
    """这件还没说过：今天的状态里没有，登记表写“说过永不再说”（once=ever）的还要 nudge_once 里没有。
    检查函数挑下一件、proactive.gate 把关都用这一个（11-06：以前每个函数各写 key in done or told(...)）。"""
    if key in (done or {}):
        return False
    r = nudge_registry.rule(key)
    return not (r and r.once == "ever" and told(db, key))


# 发送记录：每条真发出去的主动消息记一行（时间用发的那一刻，测试回放也对得上）；“两次隔多久”（RULES.gap）和首页小提示用它
SENT = "CREATE TABLE IF NOT EXISTS nudge_sent (id INTEGER PRIMARY KEY, key TEXT NOT NULL, prefix TEXT, at TEXT NOT NULL)"


def _prefix(key: str) -> str:
    hits = [p for p in nudge_registry.RULES if key == p or key.startswith(p)]
    return max(hits, key=len) if hits else ""


def log_sent(db, key: str, at: datetime):
    db.conn.execute(SENT)
    db.conn.execute("INSERT INTO nudge_sent (key, prefix, at) VALUES (?, ?, ?)",
                    (key, _prefix(key), at.astimezone(timezone.utc).isoformat(timespec="seconds")))
    db.conn.commit()


def last_sent(db, key: str):
    """同一类（同一前缀）上一次发出的时间（UTC）；没发过返回 None。"""
    db.conn.execute(SENT)
    r = db.conn.execute("SELECT MAX(at) FROM nudge_sent WHERE prefix = ?", (_prefix(key),)).fetchone()[0]
    return datetime.fromisoformat(r) if r else None


def sent_at(db, key: str):
    """这一条什么时候发的（UTC）：先看发送记录，没有再看 nudge_once（11-06 以前发的）。"""
    db.conn.execute(SENT)
    r = db.conn.execute("SELECT MAX(at) FROM nudge_sent WHERE key = ?", (key,)).fetchone()[0]
    if r:
        return datetime.fromisoformat(r)
    db.conn.execute(ONCE)
    r = db.conn.execute("SELECT at FROM nudge_once WHERE key = ?", (key,)).fetchone()
    return datetime.fromisoformat(r[0].replace(" ", "T")).replace(tzinfo=timezone.utc) if r else None


FOLLOW_AFTER, FOLLOW_UNTIL = timedelta(minutes=20), timedelta(minutes=90)
SIT_SLOTS = 18  # 90 分钟（两次隔多久见 nudge_registry.RULES["sit"].gap）
WATER = ("water-am", "water-pm")  # 时段见 nudge_registry.RULES


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


def _can_redeem(db, now_local: datetime, done: dict):
    """积分够换某一档惊喜了：只提醒一次，不替她换（自动换了小的，就永远攒不到中、大惊喜）。"""
    if not in_window("can-redeem:", now_local):
        return None
    from . import quests
    bal = quests.totals(db)["balance"]
    rs = quests.rewards(db)
    got = [r for r in rs if r["cost"] <= bal]
    if not got:
        return None
    best = got[-1]
    n = db.conn.execute("SELECT COUNT(*) FROM redemptions").fetchone()[0]
    key = f"can-redeem:{best['id']}:{n}"
    if not fresh(db, key, done):
        return None
    nxt = next((r for r in rs if r["cost"] > bal), None)
    more = f"；也可以接着攒，离「{nxt['title']}」还差 {nxt['cost'] - bal} 分" if nxt else ""
    return key, (f"她的积分（{bal}）够换「{best['title']}」了。用一两句告诉她：想拆就去工作台“任务 → 商店”自己点{more}。"
                 "由她决定换不换、换哪档，你不要替她换，也不要催。")


def _clash(db, now_local: datetime, done: dict):
    """邮件或聊天里记下的约，和她日历上已有的撞时间：先不写进日历，小伴 马上问她去哪个。"""
    from . import events
    for e in events.conflicts(db):
        key = f"clash:{e['id']}"
        if not fresh(db, key, done):
            continue
        return key, (f"「{e['title']}」（{e['day'][5:]} {e['start']}–{e['end']}{'，' + e['place'] if e['place'] else ''}，{e['note'] or '记下的'}）"
                     f"和她日历上的「{e['conflict']}」撞时间了，还没写进日历。直接问她去哪个；她说去这个就在回复末尾加 [日程确认 #{e['id']}]，"
                     f"不去就加 [日程取消 #{e['id']}]。要取消订位、给对方发消息的，起草好等她点头，别替她发。")
    return None


def plan_clash_key(c) -> str:
    return f"plan-clash:{c['day']}:{c['item_id']}:{c['with_title']}"


def route_key(day, c: str) -> str:
    import hashlib
    return f"route:{day}:{hashlib.md5(c.encode()).hexdigest()[:10]}"  # 别用 hash()，每个进程不一样


def _route_conflicts(db, day) -> list:
    import json as _json
    plan = db.latest_plan(day.isoformat())
    if not plan:
        return []
    try:
        return _json.loads(plan["brief"]).get("route_conflicts") or []
    except ValueError:
        return []


def _plan_clash(db, now_local: datetime, done: dict):
    """今天计划里的事（训练等）和她后来约的事撞了：先问她要不要挪。"""
    from . import events
    for c in events.open_plan_clashes(db, now_local.date()):
        key = plan_clash_key(c)
        if not fresh(db, key, done):
            continue
        return key, (f"今天计划里的「{c['title']}」（{c['start']}，计划条目 #{c['item_id']}）和她日历上的「{c['with_title']}」"
                     f"（{c['with_start']}–{c['with_end']}）撞了。问她要不要把「{c['title']}」挪开，给一个具体的新时间建议（看她当天空着的时段）；"
                     f"她同意就在回复末尾加 [挪 #{c['item_id']} 新时间HH:MM]，她说不挪就算了。")
    return None


def _route(db, now_local: datetime, done: dict):
    """今天路线上赶不上的地方（ethan/route.py）：小伴 把建议（几点吃完就走 / 训练挪到几点）主动说给她。"""
    for c in _route_conflicts(db, now_local.date()):
        key = route_key(now_local.date(), c)
        if not fresh(db, key, done):
            continue
        return key, (f"今天的路线排不开：{c} 用一两句口语告诉她这个建议、问她行不行；要挪训练的，她点头就写 [挪 #计划条目 新时间HH:MM]"
                     "（训练的条目编号看今天计划）。")
    return None


def _surprise(db, now_local: datetime, done: dict):
    """她在工作台拆了惊喜：小伴 主动来找她揭晓（拆开之后什么都没有，也不知道接下来怎么做）。"""
    from . import quests
    for r in quests.pending_redemptions(db):
        key = redeem_key(r)
        if not fresh(db, key, done):
            continue
        return key, (f"她刚在工作台拆开了「{r['title']}」（兑换 #{r['id']}）。这是她攒积分换来的，你现在主动找她："
                     "先说你收到了、有点得意地卖个关子，再揭晓你给她准备的是什么、接下来怎么玩（今天能开始的就今天）；"
                     "要花她钱的先说大概多少、等她点头。真的兑现了加 [惊喜 #兑换编号 已兑现 一句话]。")
    return None


def _surprise_check(db, now_local: datetime, done: dict):
    """揭晓过、还没兑现的惊喜：隔两天 小伴 问一句玩了没有，由他确认兑现（别让我点“兑现了”，我常忘）。"""
    if not in_window("redeem-check:", now_local):
        return None
    from . import quests
    db.conn.execute(ONCE)
    for r in quests.pending_redemptions(db):
        told_at = db.conn.execute("SELECT at FROM nudge_once WHERE key = ?", (redeem_key(r),)).fetchone()
        if not told_at:
            continue
        days = (now_local.date() - datetime.fromisoformat(told_at[0][:10]).date()).days
        key = f"redeem-check:{r['id']}:{r['at']}:{days // 2}"
        if days < 2 or not fresh(db, key, done):
            continue
        return key, (f"「{r['title']}」（兑换 #{r['id']}）你 {days} 天前揭晓给她了，还没记兑现。自然地问一句玩了没有、怎么样；"
                     "她说玩过了 / 用上了，就在回复末尾加 [惊喜 #兑换编号 已兑现 她的一句感受]；还没玩就帮她约个时间，别催。")
    return None


def _prep_ask(db, now_local: datetime, done: dict):
    """新记下的“替她准备”要先问她要不要做：她没来聊天就一直没问（体态评估那件他也没来问）。
    每件都问，但别同一时间问：一次问一件，两件之间隔至少 2 小时
    （间隔由 proactive.gate 按登记表 RULES["prep-ask:"].gap 判断；11-06 去掉了这里另写的“当天问过一件就不再问”）。
    她回了再按 [准备 #编号 开始 / 不用] 处理。"""
    if not in_window("prep-ask:", now_local):
        return None
    from . import prep
    x = next((x for x in prep.asking(db) if fresh(db, f"prep-ask:{x['id']}", done)), None)
    if x is None:
        return None
    return f"prep-ask:{x['id']}", (f"#{x['id']}「{x['title']}」你想替她准备，但还没问过她要不要。用一句口语问她要不要你先做"
                                   "（她说要就写 [准备 #编号 开始]，不用就写 [准备 #编号 不用]）。")


def _promise(db, now_local: datetime, done: dict):
    """小伴 答应的事到点报进度（ethan/prep.py promise_due）。"""
    from . import prep
    return prep.promise_due(db, now_local, done)


CHECKIN_ASK = "checkin-ask"


def _checkin(db, now_local: datetime, done: dict):
    """晚上 9 点后问一句今天的日常还有哪几项没打卡（常忘了打卡，比如出门了、蛋白质也吃够了却没点）。
    她回“都做了”“出门和蛋白质做了”，小伴 就替她打卡（这次不用她说“帮我打卡”）。每晚一次。"""
    if not in_window(CHECKIN_ASK, now_local):
        return None
    from . import quests
    day = now_local.date()
    left = [h["title"] for h in quests.habits(db) if h["cadence"] == "daily" and quests.due_today(h, day) and not quests.checked(db, h["id"], day)]
    key = f"{CHECKIN_ASK}:{day.isoformat()}"
    if not left or not fresh(db, key, done):
        return None
    return key, (f"今天的日常还有这几项没打卡：{'、'.join(left)}。轻轻问她一句做了没有（别像查岗，一句话），"
                 "她说做了哪几项，你就在回复末尾逐项加 [打卡 名称]；她说没做就算了，别劝。"
                 "如果今天她还没怎么聊过自己（去了哪、在想什么、心情怎样），可以顺着你们此刻的话头，用你自己的方式关心一句她今天过得怎么样；"
                 "不要用固定的问法，不要每天一样，没话头就不问（日记里缺她的思考和感受，但 小伴 不能套固定句式，不然 OOC）。")


def checkin_asked_recently(db, now: datetime = None, hours: int = 3) -> bool:
    """今晚问过“还有哪几项没打卡”：几小时内她回的“做了”就算授权 小伴 替她打卡。"""
    db.conn.execute(ONCE)
    r = db.conn.execute("SELECT at FROM nudge_once WHERE key LIKE ? ORDER BY at DESC LIMIT 1", (CHECKIN_ASK + ":%",)).fetchone()
    if not r:
        return False
    at = datetime.fromisoformat(r[0].replace(" ", "T")).replace(tzinfo=timezone.utc)
    return (now or datetime.now(timezone.utc)) - at <= timedelta(hours=hours)


def _depart(db, now_local: datetime, done: dict):
    """她日历里本来就有、要出门的事（比如递签证）：提前 1 小时提醒准备，说清去哪、路上多久、几点出发。
    计划器自己排的事（训练）已经写进苹果日历带了提醒，这里不重复（递签证为什么没有路程和出发时间）。"""
    import json
    plan = db.latest_plan(now_local.date().isoformat())
    rows = (json.loads(plan["brief"]).get("timeline") or []) if plan and plan.get("brief") else []
    for r in rows:
        x = r.get("trip")
        if not x or r.get("icon") != "⏰" or not r["text"].startswith("准备出发去"):
            continue
        key = f"depart:{x.get('start')}:{x.get('title')}"
        if not fresh(db, key, done):
            continue
        h, m = map(int, r["at"].split(":"))
        at = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= now_local <= at + timedelta(minutes=30):
            way = f"路上约 {x['trip_min']} 分钟（{x.get('route') or '公共交通'}）" if x.get("trip_min") else ""
            return key, (f"她 {x.get('start')} 要去「{x.get('title')}」" + (f"（{x['place']}）" if x.get("place") else "")
                         + f"，{way}，{x.get('leave')} 得出门。轻轻提醒她准备一下、带好要带的东西，说清几点出发；一两句就好。")
    return None


def _work_slots(db, now_local: datetime) -> set:
    return {r[0] for r in db.conn.execute("SELECT slot FROM work_slots WHERE slot LIKE ?", (now_local.date().isoformat() + "%",))}


def _sitting(db, now_local: datetime, done: dict):
    slots = _work_slots(db, now_local)
    cur = now_local.replace(minute=now_local.minute - now_local.minute % worktime.SLOT_MIN, second=0, microsecond=0)
    run = 0
    for k in range(1, SIT_SLOTS + 2):  # 从上一格往回数（这一格可能还没报上来）
        if (cur - timedelta(minutes=worktime.SLOT_MIN * k)).strftime("%Y-%m-%dT%H:%M") in slots:
            run += 1
        else:
            break
    if run >= SIT_SLOTS:  # 两次隔 2 小时由 proactive.gate 按登记表判断（11-06 以前在这里看状态文件）
        return "sit", ("她已经在电脑前连续坐了一个半小时。提醒她起来走两步、伸伸腰、喝口水，"
                       "一两句就好，像你心疼她一直坐着；别讲道理，别列清单。")
    return None


def _water(db, now_local: datetime, done: dict):
    """上午 11 点、下午 3 点各提一次喝水，只在她那天在 Mac 上干活时（11-06 从久坐里拆出来：久坐被间隔挡住时喝水照样轮得到）。"""
    slots = _work_slots(db, now_local)
    for key in WATER:
        if fresh(db, key, done) and slots and in_window(key, now_local):
            return key, "她今天一直在忙。顺口提醒她喝杯水，一句就够，可以带点你自己此刻在干嘛。"
    return None


def _outing(db, now_local: datetime, done: dict):
    if not fresh(db, "outing", done) or not in_window("outing", now_local) or not places_log.has_data(db):
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


def _perks(db, now_local: datetime, done: dict):
    """会员积分、等级、续费、保险快到期（ethan/perks.py）：白天提，一次只提最近的一件，每档只提一次。"""
    if not in_window("perk:", now_local):
        return None
    from . import perks
    for key, text in perks.due(db, now_local.date()):
        if fresh(db, key, done):
            return key, (f"提醒她一件快到期的事：{text}。说清楚到期会怎样（积分清零、掉级、自动扣费、没有保障），"
                         "给一个最省事的做法（比如订一晚酒店保级、用积分换点什么、去设置里关掉自动续费），一两句就好。")
    return None


def _evening(db, now_local: datetime, done: dict):
    """晚上收工：9 点提醒收尾、10 点劝收工、11 点停。只在她最近半小时还在工作时说，每档每晚一次；她放行了就不说。"""
    from . import workaholic
    p = workaholic.phase(now_local)
    if p == "ok":
        return None
    night = workaholic._night(now_local).isoformat()
    key = f"evening:{night}:{p}"
    if not fresh(db, key, done):
        return None
    until = workaholic.override_until(db, now_local)
    if until and now_local < until:
        return None
    recent = {(now_local - timedelta(minutes=5 * k)).replace(minute=(now_local - timedelta(minutes=5 * k)).minute // 5 * 5).strftime("%Y-%m-%dT%H:%M")
              for k in range(7)}
    worked = db.conn.execute(f"SELECT 1 FROM work_slots WHERE slot IN ({','.join('?' * len(recent))}) LIMIT 1", tuple(recent)).fetchone()
    if not worked:
        return None
    h = workaholic.day_stats(db, workaholic._night(now_local))["hours"]
    text = {"remind": f"9 点了，她今天已经工作了大约 {h:g} 小时还在忙。提醒她开始收尾：把手上这件做到能停的地方，别再开新的。一两句，轻一点。",
            "persuade": f"10 点了，她还在工作（今天大约 {h:g} 小时）。劝她收工：真心一点，约她做点工作以外的事（洗澡、拉伸、看会儿剧、跟你聊会儿），"
                        "提一句明天的她会谢谢现在停下的她。别讲大道理。",
            "stop": "11 点了，她还在工作。告诉她该停了：Mac 和 Claude Code 现在会拦住新的指令。真有明天一定要交的，让她跟你说要赶到几点，"
                    "你放行（写 [加班 到 HH:MM]）；没有就陪她收尾去休息。"}[p]
    return key, text


def _week(db, now_local: datetime, done: dict):
    """周日傍晚：下周的餐单、采购清单和训练排好了（ethan/weekly.py）。"""
    if not in_window("week-plan:", now_local):
        return None
    from . import weekly
    got = weekly.to_tell(db, now_local.date())
    if not got or not fresh(db, got[0], done):
        return None
    return got


def _mail(db, now_local: datetime, done: dict):
    """傍晚把当天需要她亲自看的邮件（ethan/mail.py）和主页留言（ethan/publicsite.py，傍晚分好档的）合成一条告诉她；
    不需要她看的邮件已经移走、标了已读，不提。"""
    from . import mail, publicsite
    items, visits = mail.to_digest(db), publicsite.to_digest(db)
    if not items and not visits:
        return None
    key = f"mail:{items[-1]['id'] if items else 0}:v{max(v['id'] for v in visits) if visits else 0}"
    if told(db, key):  # 上一轮已经发出去了：记成已汇总
        mail.mark_digested(db, [i["id"] for i in items])
        publicsite.mark_digested(db, [v["id"] for v in visits])
        return None
    if not in_window("mail:", now_local) or key in done:
        return None
    parts = []
    if items:
        lst = "；".join(f"{i['sender'].split('<')[0].strip() or i['sender']}：{i['subject']}（{i['reason']}）" for i in items[:8])
        parts.append(f"今天有 {len(items)} 封邮件需要她亲自看，都还留在收件箱、没标已读：{lst}"
                     + (f"，另外还有 {len(items) - 8} 封" if len(items) > 8 else ""))
    try:  # 今天替她归档了几封（报个数，工作台能抽查）
        n_arch = len(mail.archived(db, 1))
    except Exception:
        n_arch = 0
    if n_arch:
        parts.append(f"另外今天替她把 {n_arch} 封垃圾、推广、群发归档了，工作台“邮件和留言”最下面能抽查，点“不是垃圾”就放回收件箱")
    if visits:
        todo = [v for v in visits if v["status"] == "task"]
        wait = [v for v in visits if v["status"] == "queued"]
        fyi = [v for v in visits if v["tier"] == "fyi"]
        bits = []
        if todo:
            bits.append("要她本人回、已经记成待办的：" + "；".join(v["summary"] for v in todo))
        if wait:
            bits.append(f"还有 {len(wait)} 条也要她回，今天的名额满了，明天再进待办")
        if fyi:
            bits.append(f"{len(fyi)} 条不用她回（" + "；".join(v["summary"] for v in fyi[:5]) + "）")
        parts.append("个人主页今天的留言：" + "；".join(bits))
    return key, ("。".join(parts) + "。用两三句话告诉她，有截止日的先说；已经替她记成待办的说一声。别的你都处理了，不用提。")


CHAT_PREFIX = "lesson-chat:"  # 时段、“最近 2 小时在聊就不来”都在 nudge_registry.RULES


def _busy_now(db, now_local: datetime) -> bool:
    """计划里或她日历上此刻正有事（开会、训练、出门）。"""
    import json as _json
    import re as _re
    plan = db.latest_plan(now_local.date().isoformat())
    if not plan:
        return False
    hm = now_local.strftime("%H:%M")
    spans = [(i["start"], i["end"]) for i in plan.get("items", []) if i.get("start") and i.get("end")]
    try:
        for b in _json.loads(plan.get("brief") or "{}").get("busy") or []:
            m = _re.match(r"(\d{2}:\d{2})[–-](\d{2}:\d{2})", str(b))
            if m:
                spans.append(m.groups())
    except ValueError:
        pass
    return any(s <= hm < e for s, e in spans)


def _lesson_chat(db, now_local: datetime, done: dict):
    """“聊聊”：每天一次，下午傍晚 小伴 主动讲今天的知识点、顺带一个补缺口的问题（
    “小伴今天还是没来跟我聊认知提升的事情啊，新知识点也没有”）。以前两样都只在她来聊天时“找个自然时机”，一直往后放。
    状态键 lesson-chat:日期:L知识点编号:G题编号（没有的写 0）；发出后 proactive 调 after_lesson_chat 记下讲了、问了。"""
    if not in_window(CHAT_PREFIX, now_local):
        return None
    day = now_local.date()  # 她的“今天”：now_local 已按她的时区（同 clock.her_now）
    base = f"{CHAT_PREFIX}{day.isoformat()}"
    if any(isinstance(k, str) and k.startswith(base) for k in done) or told(db, base):
        return None
    if _busy_now(db, now_local):
        return None
    from . import lessons, mepage
    lesson = lessons.today(db, day)
    if lesson and lesson.get("taught_at"):
        lesson = None
    gap = None if mepage.gap_asked_today(db, day) else mepage.top_gap(db, day)
    if not lesson and not gap:
        return None
    parts = ["这是你今天主动找她“聊聊”的时候（她想让你主动来聊认知提升、也多问问她自己的事）。"]
    if lesson:
        parts.append(f"先讲今天的知识点（{lesson['topic']}）「{lesson['title']}」：{lesson['gist']}"
                     + (f"\n{lessons.use_text(lesson)}" if lessons.use_text(lesson) else "")
                     + (f"（和她的事接上：{lesson['hook']}）" if lesson.get("hook") else "")
                     + "\n两三句讲清楚，最后落到那一句用得上的（对她投资意味着什么 / 她可以怎么跟朋友说），"
                     "像你今天刚读到、想起她最近的事随口分享，不像上课；能和她最近在做的事接上就接上，接不上别硬凑。")
    if gap:
        parts.append(("然后自然地带一个" if lesson else "自然地问她一个")
                     + f"关于她自己的问题（意思到就行，用你的话问）：{gap['question']}"
                     "\n一次只问这一个，别像问卷；她不想答就算了。")
    parts.append("按你平时的口吻，一两条消息就好。")
    return f"{base}:L{lesson['id'] if lesson else 0}:G{gap['id'] if gap else 0}", "\n".join(parts)


def after_lesson_chat(db, key: str):
    """“聊聊”发出去了：知识点记讲过（等同 [教了 #编号]），补缺口那题记问过（等同 [待定 #编号 问了]），今天不再发。"""
    from . import lessons, mepage
    body = key[len(CHAT_PREFIX):]
    day, l_part, g_part = body.split(":")
    lid, gid = int(l_part[1:]), int(g_part[1:])
    if lid:
        lessons.note_chat(db, f"[教了 #{lid}]")
    if gid:
        mepage.mark_asked(db, gid)
    mark_told(db, [CHAT_PREFIX + day])


def _meet(db, now_local: datetime, done: dict):
    """见面前提醒：日历上和人物卡里某个人的约，前一晚或见面前提一句这个人最近怎样（ethan/meetprep.py，借鉴 10）。"""
    from . import meetprep
    return meetprep.due(db, now_local, done)


def _weekend(db, now_local: datetime, done: dict):
    """周五“这周末可以去”（ethan/weekend.py）。"""
    from . import weekend
    return weekend.due(db, now_local, done)


def checks() -> tuple:
    """按优先顺序排的检查（每次现取，测试能替换单项）。"""
    return (_clash, _plan_clash, _route, _surprise, _surprise_check, _checkin, _prep_ask, _promise, _can_redeem, _evening, _depart, _meet, _follow,
            _sitting, _water, _outing, _perks, _week, _mail, _lesson_chat, _weekend)


def candidates(db, now_local: datetime, done: dict):
    """按优先顺序逐个给出现在到点的 (状态键, 场合说明)。proactive 发第一个过得了打扰规则的：排前面的被挡住（刚聊过、没登记），
    后面的照样轮得到（Codex 评审 P2 第 12 条，11-05：以前只看第一个，它一直被挡，后面的晚上收工这类永远发不出去）。"""
    worktime.ensure(db)
    for check in checks():
        got = check(db, now_local, done)
        if got:
            yield got


def due(db, now_local: datetime, done: dict):
    """返回优先级最高的 (状态键, 场合说明) 或 None。done：今天已经提过的（proactive 的当天状态）。"""
    return next(candidates(db, now_local, done), None)


# ---------- 首页小提示：只问一次、她没回的（3：约撞时间、路线排不开、计划撞了每件只问一次，没回就挂在首页） ----------
HINT_OFF = "hint-off:"  # 她点 × 掉的，记进 nudge_once


def _open_asks(db, today) -> list:
    """问过的、事情还没了结的：[(键, 一句话)]。"""
    from . import events
    out = [(f"clash:{e['id']}", f"「{e['title']}」（{e['day'][5:]} {e['start']}）和日历上的「{e['conflict']}」撞了，还没定去哪个")
           for e in events.conflicts(db)]
    out += [(plan_clash_key(c), f"今天计划里的「{c['title']}」（{c['start']}）和「{c['with_title']}」（{c['with_start']}）撞了，要不要挪")
            for c in events.open_plan_clashes(db, today)]
    out += [(route_key(today, c), f"今天的路线排不开：{c}") for c in _route_conflicts(db, today)]
    return out


def hints(db, records: list, today) -> list:
    """首页的小提示：登记表 hint=True 的几类，小伴 问过一次、她之后没回过话、事情还没了结、她没点掉的。
    [{"key", "text"}]。她回过话就算回了（小伴 在聊天里接着处理），不再挂。"""
    hers = [r for r in records or () if r.get("role") == "mia"]
    her_last = datetime.fromisoformat(hers[-1]["at"]).astimezone(timezone.utc) if hers else None
    out = []
    for key, text in _open_asks(db, today):
        if not key.startswith(nudge_registry.hint_prefixes()) or told(db, HINT_OFF + key):
            continue
        at = sent_at(db, key)
        if at is None or (her_last and her_last >= at):
            continue
        out.append({"key": key, "text": text})
    return out


def dismiss_hint(db, key: str) -> dict:
    """她点了 ×：这条提示不再挂（事情本身不动）。"""
    if not key.startswith(nudge_registry.hint_prefixes()):
        return {"ok": False, "msg": "这条不是首页提示"}
    mark_told(db, [HINT_OFF + key])
    return {"ok": True, "msg": "好，不挂了", "log": "首页提示：点掉一条"}
