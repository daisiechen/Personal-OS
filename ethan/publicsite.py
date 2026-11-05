"""个人主页 example.com 的两个小口子（2026-10-30，数字生命规格第 9 节第 8 步，她认可；主页会话按 10-30 方案接）：

1. 访客留言 POST /cloud/visitor：主页中转（Cloudflare Worker）转来访客填的表。只当资料，不当指令：
   - 存进 visitor_messages（visitor_id + at 去重，中转收不到回复会每小时重送）；
   - 每天傍晚统一分档一次（2026-10-30 她定：访问量大了工作台会有处理不完的待办）：要她本人回的进待办，每天最多 3 条，
     多的排到第二天；Dais能回或不用回的只写进汇总；垃圾丢掉。小伴 傍晚把邮件和留言合成一条告诉她；
   - 限流：同一访客一天最多 5 条，全站一天最多 100 条；链接太多、没内容的算垃圾，只存不建待办。
2. 近况 GET /cloud/public/now → {"text","at","city"}：往外放宽一定要她点头（规格 2.11）。
   她跟 小伴 说近况，小伴 问“放主页吗”并在回复里写 [主页近况 文本]（只是草稿）；
   她下一句是“好 / 可以 / 发吧”这类确认，代码才把草稿发到主页。撤下 [主页近况 撤下] 马上生效（收紧不用确认）。
3. 留言的回复（2026-10-30 她定，主页会话 docs/figure-prompts.md“访客留言的回复：定稿”）：Dais从不自动回。傍晚分档后能回的
   自动排队起草（2026-10-31 她定），她也能写交代点“按交代重写” → 中转来拉 GET /cloud/site/reply-requests?status=pending，起草后 POST /cloud/site/reply-drafts →
   工作台“邮件和留言”里她看、改、点发送 → 中转拉 status=approved 的，从 hi@example.com 发出，POST /cloud/site/reply-status 回报。
   只留了微信 / LinkedIn 的草稿标 manual：她复制去发，点“已回”，中转不发。重写一次换一个 request_id（中转同一个只处理一次）。
4. 留言两个新字段（2026-10-31 她同意加蜜罐）：suspect="honeypot" 是填了隐藏格子的机器人，直接进垃圾档（不删）；
   via_agent 是别的 AI agent 代人留言时自报的名字，工作台显示“由 agent 代留”，照常分档。
5. 生活页日常动态 GET /cloud/public/feed?lang=zh|en（2026-10-31 她定，主页会话 docs/figure-prompts.md 末节）：
   只放她确认过公开的。小伴 在聊天里写 [主页动态 她|我 标题｜正文]（只是草稿），她下一句“好 / 可以 / 发吧”才放上去；
   who=mia 她的生活片段，who=ethan 小伴 第一人称的日记；who=dais 由主页中转自己生成，OS 不管。
中转用单独的钥匙 INBOX_TOKEN_SITE，只能用 /cloud/visitor、/cloud/site/*、/cloud/public/now、/cloud/public/feed、/cloud/profile（只给城市和“今天是不是生日”）。
"""

import json
import re
from datetime import datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitor_messages (
    id INTEGER PRIMARY KEY, visitor_id TEXT NOT NULL, at TEXT NOT NULL, received_at TEXT NOT NULL, name TEXT, contact TEXT,
    org TEXT, intent TEXT NOT NULL, message TEXT NOT NULL, page TEXT, lang TEXT, status TEXT NOT NULL, task_id INTEGER,
    UNIQUE (visitor_id, at));
CREATE TABLE IF NOT EXISTS public_now (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, city TEXT, status TEXT NOT NULL, proposed_at TEXT NOT NULL, published_at TEXT);
CREATE TABLE IF NOT EXISTS public_feed (
    id INTEGER PRIMARY KEY, who TEXT NOT NULL, day TEXT NOT NULL, title TEXT, body TEXT NOT NULL, lang TEXT NOT NULL,
    status TEXT NOT NULL, proposed_at TEXT NOT NULL, published_at TEXT);
"""
INTENTS = ("合作", "约聊", "媒体", "其他")
DAILY_TASKS = 3  # 每天最多几条留言进待办；多的排到第二天
LIMITS = {"name": 60, "contact": 120, "org": 80, "message": 2000, "page": 200, "lang": 10, "visitor_id": 80, "at": 40}
PER_VISITOR_DAY, PER_DAY = 5, 100
NOW_MARK = re.compile(r"\[主页近况\s+([^\]]+?)\s*\]")
YES = re.compile(r"^(好|好的|好呀|可以|行|发吧|放吧|发|放|嗯|嗯嗯|ok|okay|没问题|同意|就这样)[!！。.~～啊呀吧哦]*$", re.I)
DRAFT_HOURS = 24
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
REPLY_COLS = (("message_id", "TEXT"), ("email", "TEXT"), ("source", "TEXT"), ("request_id", "TEXT"), ("reply_round", "INTEGER NOT NULL DEFAULT 0"),
              ("reply_status", "TEXT"), ("reply_channel", "TEXT"), ("reply_error", "TEXT"), ("draft_subject", "TEXT"), ("draft", "TEXT"),
              ("draft_zh", "TEXT"), ("sent_at", "TEXT"), ("desk_state", "TEXT NOT NULL DEFAULT 'open'"), ("desk_note", "TEXT"),
              ("handed_at", "TEXT"), ("body_zh", "TEXT"), ("suspect", "TEXT"), ("via_agent", "TEXT"))  # 后四个是工作台“邮件和留言”用的（ethan/desk.py）


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.executescript(SCHEMA)


# ---------- 访客留言 ----------
def receive(db, payload: dict, now: datetime = None) -> tuple:
    """返回 (HTTP 状态码, JSON)。"""
    ensure(db)
    now = now or datetime.now(timezone.utc)
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "bad json"}
    f = {k: " ".join(str(payload.get(k) or "").split())[:n] if k != "message" else str(payload.get(k) or "").strip()[:n]
         for k, n in LIMITS.items()}
    intent = f["intent"] = str(payload.get("intent") or "其他").strip()
    if intent not in INTENTS:
        f["intent"] = intent = "其他"
    if not f["visitor_id"] or not f["at"]:
        return 400, {"ok": False, "error": "visitor_id and at required"}
    if db.conn.execute("SELECT 1 FROM visitor_messages WHERE visitor_id = ? AND at = ?", (f["visitor_id"], f["at"])).fetchone():
        return 200, {"ok": True, "duplicate": True}
    day = now.date().isoformat()
    if db.conn.execute("SELECT COUNT(*) FROM visitor_messages WHERE substr(received_at, 1, 10) = ?", (day,)).fetchone()[0] >= PER_DAY:
        return 429, {"ok": False, "error": "too many today"}
    if db.conn.execute("SELECT COUNT(*) FROM visitor_messages WHERE visitor_id = ? AND substr(received_at, 1, 10) = ?",
                       (f["visitor_id"], day)).fetchone()[0] >= PER_VISITOR_DAY:
        return 429, {"ok": False, "error": "too many from this visitor"}
    suspect = str(payload.get("suspect") or "").strip()[:20] or None
    spam = not f["message"] or len(re.findall(r"https?://", f["message"])) > 3 or suspect == "honeypot"  # 蜜罐：机器人，进垃圾档不删
    status = "spam" if spam else "new"  # 先存着，每天傍晚统一分档（triage），不再一条一个待办（2026-10-30 她定）
    cur = db.conn.execute(
        "INSERT INTO visitor_messages (visitor_id, at, received_at, name, contact, org, intent, message, page, lang, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (f["visitor_id"], f["at"], now.isoformat(timespec="seconds"), f["name"], f["contact"], f["org"], intent, f["message"],
         f["page"], f["lang"], status))
    mid = cur.lastrowid
    ensure_triage(db)
    email = " ".join(str(payload.get("email") or "").split())[:120]
    db.conn.execute("UPDATE visitor_messages SET message_id = ?, email = ?, source = ?, suspect = ?, via_agent = ?, "
                    "tier = CASE WHEN ? THEN 'junk' ELSE tier END WHERE id = ?",
                    (str(payload.get("message_id") or "")[:80] or None, email if EMAIL_RE.match(email) else None,
                     str(payload.get("source") or "")[:40] or None, suspect,
                     " ".join(str(payload.get("via_agent") or "").split())[:80] or None, int(spam), mid))
    from . import lifebase
    lifebase.try_evidence(db, "visitor", f"visitor:{mid}", now.isoformat(timespec="seconds"),
                          json.dumps({k: f[k] for k in ("name", "contact", "org", "intent", "message", "page")}, ensure_ascii=False))
    db.conn.commit()
    return 200, {"ok": True}


def recent_visitors(db, days: int = 7) -> list:
    ensure(db)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM visitor_messages WHERE received_at >= ? AND status != 'spam' ORDER BY id DESC", (since,))]


TRIAGE_SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["id", "tier", "summary"], "properties": {
        "id": {"type": "integer"}, "tier": {"type": "string", "enum": ["reply", "fyi", "junk"]},
        "priority": {"type": ["integer", "null"], "minimum": 1, "maximum": 5}, "summary": {"type": "string", "maxLength": 60}}}}}}
TRIAGE_FROM = 17  # 她那边几点以后分档


def ensure_triage(db):
    ensure(db)
    from . import lifebase
    lifebase.ensure(db)  # 待办的来源列
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(visitor_messages)")}
    for col, ddl in (("tier", "TEXT"), ("priority", "INTEGER"), ("summary", "TEXT"), ("digested", "INTEGER NOT NULL DEFAULT 0")) + REPLY_COLS:
        if col not in have:
            db.conn.execute(f"ALTER TABLE visitor_messages ADD COLUMN {col} {ddl}")


# ---------- 留言的回复：Dais起草，她确认，中转从 hi@ 发 ----------
def can_reply(row) -> bool:
    return bool(row["email"] or (row["contact"] or "").strip())


def request_reply(db, rid: int, note: str = "") -> dict:
    """她点“交给Dais回”（或按交代重写）：换一个新的 request_id 排队，等中转来拉。"""
    ensure_triage(db)
    r = db.conn.execute("SELECT * FROM visitor_messages WHERE id = ?", (int(rid),)).fetchone()
    if r is None:
        raise ValueError("找不到这一条")
    if not can_reply(r):
        return {"ok": False, "msg": "对方没留联系方式，回不了"}
    n = (r["reply_round"] or 0) + 1
    db.conn.execute("UPDATE visitor_messages SET reply_round = ?, request_id = ?, reply_status = 'pending', reply_error = NULL, "
                    "desk_state = 'handed', desk_note = ?, handed_at = ? WHERE id = ?", (n, f"v{rid}-{n}", note or None, _now(), int(rid)))
    db.conn.commit()
    return {"ok": True, "msg": "交给Dais了，草稿写好会出现在这里"}


def _style(db) -> str:
    """Dais 起草回复用的表达方式（2026-11-05）：只给她确认过的写作规范的规则本身（工作上 + 通用），不给出处原句；
    只管怎么说，不涉及能说什么（那个照对外开关）。没有确认过的就空。"""
    try:
        from . import style
        return style.style_block(db, "twin", audience="work", quotes=False)
    except Exception:
        return ""


def reply_requests(db, status: str) -> dict:
    ensure_triage(db)
    if status == "pending":
        rows = db.conn.execute("SELECT * FROM visitor_messages WHERE reply_status = 'pending' ORDER BY id LIMIT 50").fetchall()
        style_text = _style(db) if rows else ""
        items = [{"request_id": r["request_id"], "message_id": r["message_id"], "email": r["email"] or "", "contact": r["contact"] or "",
                  "name": r["name"] or "", "message": r["message"], "instruction": r["desk_note"] or "", "lang": r["lang"] or "",
                  "at": r["received_at"], "style": style_text} for r in rows if can_reply(r)]
    elif status == "approved":  # 只放邮件渠道的；手动的她自己发
        rows = db.conn.execute("SELECT * FROM visitor_messages WHERE reply_status = 'approved' AND reply_channel = 'email' ORDER BY id LIMIT 50")
        items = [{"request_id": r["request_id"], "message_id": r["message_id"], "email": r["email"], "subject": r["draft_subject"] or "",
                  "body": r["draft"] or ""} for r in rows]
    else:
        return {"ok": False, "error": "status 只能是 pending 或 approved"}
    return {"ok": True, "items": items}


def _by_request(db, request_id):
    return db.conn.execute("SELECT * FROM visitor_messages WHERE request_id = ?", (str(request_id or ""),)).fetchone()


def reply_draft(db, payload: dict) -> tuple:
    """中转交来的草稿：只认还在等草稿的那一轮（她重写过的旧 request_id 不认）。"""
    ensure_triage(db)
    r = _by_request(db, payload.get("request_id"))
    body = str(payload.get("body") or "").strip()[:5000]
    if r is None or r["reply_status"] != "pending":
        return 200, {"ok": True, "ignored": True}
    if not body:
        return 400, {"ok": False, "error": "body required"}
    channel = "email" if payload.get("channel") == "email" and r["email"] else "manual"
    db.conn.execute("UPDATE visitor_messages SET draft = ?, draft_zh = NULL, draft_subject = ?, reply_channel = ?, reply_status = 'drafted', "
                    "desk_state = 'draft' WHERE id = ?", (body, str(payload.get("subject") or "")[:200], channel, r["id"]))
    db.conn.commit()
    return 200, {"ok": True}


def approve(db, rid: int, text: str) -> dict:
    """她点了“发送”：放进待发，中转下一轮（10 分钟内）从 hi@example.com 发出。"""
    ensure_triage(db)
    r = db.conn.execute("SELECT * FROM visitor_messages WHERE id = ?", (int(rid),)).fetchone()
    text = (text or "").strip()
    if r is None or r["reply_status"] not in ("drafted", "failed") or r["reply_channel"] != "email":
        return {"ok": False, "msg": "这条现在不能发"}
    if not text:
        return {"ok": False, "msg": "回复是空的"}
    db.conn.execute("UPDATE visitor_messages SET draft = ?, draft_zh = CASE WHEN draft = ? THEN draft_zh END, reply_status = 'approved', "
                    "reply_error = NULL, desk_state = 'sending' WHERE id = ?", (text, text, int(rid)))
    db.conn.commit()
    return {"ok": True, "msg": "好了，主页那边 10 分钟内从 hi@example.com 发出"}


def reply_status(db, payload: dict) -> tuple:
    ensure_triage(db)
    r = _by_request(db, payload.get("request_id"))
    if r is None or r["reply_status"] != "approved":
        return 200, {"ok": True, "ignored": True}
    if payload.get("status") == "sent":
        db.conn.execute("UPDATE visitor_messages SET reply_status = 'sent', desk_state = 'sent', sent_at = ? WHERE id = ?", (_now(), r["id"]))
    elif payload.get("status") == "failed":  # 回到草稿，她能再点一次发送
        db.conn.execute("UPDATE visitor_messages SET reply_status = 'failed', reply_error = ?, desk_state = 'draft' WHERE id = ?",
                        (str(payload.get("error") or "没发出去")[:200], r["id"]))
    else:
        return 400, {"ok": False, "error": "status 只能是 sent 或 failed"}
    db.conn.commit()
    return 200, {"ok": True}


def triage(db, router, today: str) -> dict:
    """把还没分档的留言分档；“要她回”的按优先级取前 DAILY_TASKS 条建待办，其余排队等明天。"""
    ensure_triage(db)
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM visitor_messages WHERE status = 'new' ORDER BY id")]
    got = {"triaged": 0, "tasks": 0}
    if rows:
        from . import llm
        version, system = llm.load_prompt("visitor_triage")
        ids = {r["id"] for r in rows}
        text = "\n\n".join(f"#{r['id']} {r['name'] or '匿名'}（意图：{r['intent']}；联系方式{'有' if r['contact'] or r['email'] else '无'}"
                             f"{'；由 AI agent「' + r['via_agent'] + '」代留' if r.get('via_agent') else ''}）：{r['message'][:800]}" for r in rows)
        data = llm.structured(router, db, "visitor_triage", system, text, TRIAGE_SCHEMA,
                              check=lambda d: [f"编号 {i['id']} 不在今天的留言里" for i in d["items"] if i["id"] not in ids]
                              + ([f"漏了编号 {sorted(ids - {i['id'] for i in d['items']})}"] if ids - {i["id"] for i in d["items"]} else []),
                              version=version, input_ref=today, purpose="visitor-triage")
        if data is None:
            return {"failed": "两次都没通过校验，明天再分"}
        for i in data["items"]:
            status = {"reply": "queued", "fyi": "fyi", "junk": "spam"}[i["tier"]]
            db.conn.execute("UPDATE visitor_messages SET status = ?, tier = ?, priority = ?, summary = ? WHERE id = ?",
                            (status, i["tier"], i.get("priority") or 3, i["summary"].strip(), i["id"]))
            got["triaged"] += 1
        db.conn.commit()
        for i in data["items"]:  # 2026-10-31 她定：能回的自动交给Dais起草，她在工作台看过点发送才发；外文的先译好给她看
            if i["tier"] == "junk":
                continue
            r = db.conn.execute("SELECT * FROM visitor_messages WHERE id = ?", (i["id"],)).fetchone()
            if can_reply(r) and not r["reply_status"]:
                request_reply(db, r["id"])
            try:
                from . import desk
                desk.translate(db, router, "visitor", r["id"], "body")
            except Exception:
                pass
    made_today = db.conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type = 'visitor' AND substr(created_at, 1, 10) = ?", (today,)).fetchone()[0]
    for r in db.conn.execute("SELECT * FROM visitor_messages WHERE status = 'queued' ORDER BY priority DESC, id LIMIT ?",
                             (max(0, DAILY_TASKS - made_today),)).fetchall():
        tid = db.add_task(f"回复 {r['name'] or '访客'}（主页留言）：{r['summary'] or r['message'][:24]}")
        db.conn.execute("UPDATE tasks SET owner = 'mia', source_type = 'visitor', source_ref = ?, status = 'open' WHERE id = ?",
                        (f"visitor:{r['id']}", tid))
        db.conn.execute("UPDATE visitor_messages SET status = 'task', task_id = ? WHERE id = ?", (tid, r["id"]))
        got["tasks"] += 1
    db.conn.commit()
    return got


def triage_due(db, router, now_local) -> dict:
    """傍晚分一次档（主动消息每 10 分钟检查一次时顺带做）。"""
    ensure_triage(db)
    today = now_local.date().isoformat()
    if now_local.hour < TRIAGE_FROM:
        return None
    from . import nudges
    if nudges.told(db, f"visitor-triage:{today}"):
        return None
    got = triage(db, router, today)
    if "failed" not in got:
        nudges.mark_told(db, [f"visitor-triage:{today}"])
    return got


def to_digest(db) -> list:
    """今天分好档、还没告诉她的（垃圾不算）。"""
    ensure_triage(db)
    return [dict(r) for r in db.conn.execute(
        "SELECT v.id, v.name, v.tier, v.priority, v.summary, v.status, v.task_id FROM visitor_messages v LEFT JOIN tasks t ON t.id = v.task_id "
        "WHERE v.tier IN ('reply', 'fyi') AND v.digested = 0 AND COALESCE(v.desk_state, 'open') NOT IN ('sent', 'ignored') "
        "AND COALESCE(t.done, 0) = 0 ORDER BY v.priority DESC, v.id")]


def mark_digested(db, ids):
    db.conn.executemany("UPDATE visitor_messages SET digested = 1 WHERE id = ?", [(i,) for i in ids])
    db.conn.commit()


# ---------- 近况 ----------
def public_now(db) -> dict:
    ensure(db)
    r = db.conn.execute("SELECT text, city, published_at FROM public_now WHERE status = 'live' ORDER BY id DESC LIMIT 1").fetchone()
    return {"text": r["text"], "at": r["published_at"], "city": r["city"]} if r else {"text": None, "at": None, "city": None}


def draft(db):
    ensure(db)
    cut = (datetime.now(timezone.utc) - timedelta(hours=DRAFT_HOURS)).isoformat()
    r = db.conn.execute("SELECT * FROM public_now WHERE status = 'draft' AND proposed_at >= ? ORDER BY id DESC LIMIT 1", (cut,)).fetchone()
    return dict(r) if r else None


def note_reply(db, text: str, city: str = None) -> list:
    """小伴 回复里的 [主页近况 …]：写成草稿（等她确认）；[主页近况 撤下]：马上撤。返回做了什么。"""
    ensure(db)
    done = []
    for body in NOW_MARK.findall(text):
        if body in ("撤下", "撤掉", "清空", "删掉"):
            db.conn.execute("UPDATE public_now SET status = 'cleared' WHERE status IN ('live', 'draft')")
            done.append("cleared")
        else:
            db.conn.execute("UPDATE public_now SET status = 'replaced' WHERE status = 'draft'")
            db.conn.execute("INSERT INTO public_now (text, city, status, proposed_at) VALUES (?, ?, 'draft', ?)",
                            (body.strip()[:140], city, _now()))
            done.append("draft")
    from .clock import her_now
    for who, title, body in FEED_MARK.findall(text):  # 生活页动态：也只是草稿，她点头才放
        body = body.strip()[:500]
        if body:
            db.conn.execute("INSERT INTO public_feed (who, day, title, body, lang, status, proposed_at) VALUES (?, ?, ?, ?, ?, 'draft', ?)",
                            ("mia" if who == "她" else "ethan", her_now(None).date().isoformat(), title.strip()[:60] or None,
                             body, _lang(body), _now()))
            done.append("feed-draft")
    db.conn.commit()
    return done


def note_her(db, said: str) -> bool:
    """她这句是对草稿的确认（“好”“可以”“发吧”……）才发到主页。"""
    d, fd = draft(db), feed_drafts(db)
    if not (d or fd) or not YES.match(said.strip()):
        return False
    if d:
        db.conn.execute("UPDATE public_now SET status = 'old' WHERE status = 'live'")
        db.conn.execute("UPDATE public_now SET status = 'live', published_at = ? WHERE id = ?", (_now(), d["id"]))
    for x in fd:
        db.conn.execute("UPDATE public_feed SET status = 'live', published_at = ? WHERE id = ?", (_now(), x["id"]))
    db.conn.commit()
    return True


# ---------- 生活页日常动态 ----------
FEED_MARK = re.compile(r"\[主页动态\s+(她|我)\s+([^\]｜|]*)[｜|]([^\]]+)\]")
FEED_MAX = 30


def _lang(text: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", text or "") else "en"


def feed_drafts(db) -> list:
    ensure(db)
    cut = (datetime.now(timezone.utc) - timedelta(hours=DRAFT_HOURS)).isoformat()
    return [dict(r) for r in db.conn.execute("SELECT * FROM public_feed WHERE status = 'draft' AND proposed_at >= ? ORDER BY id", (cut,))]


def feed(db, lang: str = None) -> dict:
    """GET /cloud/public/feed：她确认过的，新的在前，最多 30 条。lang 有这种语言的就只给这种，一条都没有就都给（每条带 lang）。"""
    ensure(db)
    rows = [{"who": r["who"], "date": r["day"], "title": r["title"] or None, "body": r["body"], "lang": r["lang"]}
            for r in db.conn.execute("SELECT * FROM public_feed WHERE status = 'live' ORDER BY day DESC, id DESC LIMIT ?", (FEED_MAX * 2,))]
    same = [x for x in rows if x["lang"] == lang] if lang in ("zh", "en") else rows
    return {"ok": True, "items": (same or rows)[:FEED_MAX]}


def strip(text: str) -> str:
    return FEED_MARK.sub("", NOW_MARK.sub("", text))


PROMPT = ("她的个人主页 example.com 上有一句“近况”。她跟你说了适合放上去的近况（在做什么项目、去了哪、最近在忙什么，不涉及隐私和别人），"
          "可以问她一句要不要放主页，同时在回复末尾写 [主页近况 一句话近况]（20–60 字，第一人称，像她自己写的）；这只是草稿，"
          "要她下一句明确说“好 / 可以 / 发吧”才会发上去，你不要说已经发了。她说撤掉，写 [主页近况 撤下]。\n"
          "主页还有个“生活”栏目，放她和你的日常（2026-10-31 她定）。她说“这条可以放主页”，或者她讲了一段适合公开的新体验、旅行、运动，"
          "你复述一遍问她放不放，同时写 [主页动态 她 标题｜正文]（正文用她的第一人称，500 字内）；你自己今天陪她过了哪一关、学会了什么，"
          "偶尔（一天最多一条）可以写成你的日记 [主页动态 我 标题｜正文]（你的第一人称），也要问她。都只是草稿，她说“好 / 可以 / 发吧”才放上去。"
          "不写体重等身体数据、不写感情、不写具体住址和行程、不写别人。")
