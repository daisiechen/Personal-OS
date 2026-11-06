"""其他会话里的访谈、她发过的文件，整理成关于她的说法（ontology 总方案 1.1、1.2，docs/design/ontology-plan.md）。

文件（1.2，“我现有文档之前都发你了”）：已经收下的文件（原始记录来源 file，算“她提供的资料”，不自动算她的原话）分批读，
先判断是不是关于她本人的（她写的、记录她经历的），是的才提取，原文片段必须在文件里；别人的资料只留在第二大脑。
一份一份记进度（file_digest），失败的下次重试，最多试 3 次。

访谈（1.1）：

她在别的 Claude 会话（比如个人主页那边聊过往）、别的 agent 那里亲口说的话，整理成一份交过来：
  python3 -m ethan.cloud interview --session "2- 个人主页" --scene "聊过往的访谈" --file 访谈.md
服务器收下（POST /cloud/interview，云端钥匙）：
- 原文存成一条原始记录，来源 interview，**算她的原话**，注明哪个会话、什么场合；私密，不对外；
- 每晚整理时（ethan/nightly.py）从她的回答里提取一条条说法，按画像的规则对照旧的（新增 / 更新 / 作废），
  每条带语境“在 <会话> 的 <场合> 里说的”。拿不准、只在问题里出现的不提取。
- 详细的过往是私密内容，只进 小伴 的记忆，**不进个人主页的仓库**（主页只放她批准公开的那几句）。

整段对话（format = "transcript"，
“我们现在做的是初始化！！！不是未来的维护！！！”）：微信、Grok、Codex 的导出交两边的话，一行一句标说话人【她】/【某某】
（〔……〕开头的行是会话 / 日期抬头）。整理用 transcript_facts 提示词，按说话人分来源：
- 她说的 → 关于她的说法，her_words，照访谈的规则（画像对照新增 / 更新 / 作废）；
- 别人说的、关于她的（评价、提醒、和她约的事、提到她的经历）→ other_person，语境“某某在微信里说的（日期）”，只新增、私密；
- Grok / agent 说的建议和结论 → agent_relay，只记成假设（资料，不当她的偏好），私密；
- 关于别人的事不写成她的说法，人物卡（ethan/relations.py）从原始记录自己整理。
原话片段必须原样出现在那个说话人的行里（代码校验）。收到就在后台一次整理完（--digest-all，不受夜里 15 分钟上限），
每晚整理里的 digest 照旧补漏。
"""

import hashlib
import json
import os
import re
from datetime import date, datetime, timezone

from . import lifebase
from .clock import her_today

MAX_CHARS = 60000
# ok = 0：这次没整理成（校验两次没过、模型出错、有段没过），tries < MAX_TRIES 的下次接着试，到了就放弃（巡检报放弃了几份）
# （Codex 评审 11-05：以前失败的也记成整理过，之后不会再补）
SCHEMA = ("CREATE TABLE IF NOT EXISTS interview_digest (evidence_id INTEGER PRIMARY KEY, at TEXT NOT NULL, result TEXT, "
          "ok INTEGER NOT NULL DEFAULT 1, tries INTEGER NOT NULL DEFAULT 1)")
FACTS_SCHEMA = {"type": "object", "required": ["facts"], "properties": {"facts": {"type": "array", "items": {
    "type": "object", "required": ["statement", "dimension", "quote"],
    "properties": {"statement": {"type": "string"}, "dimension": {"type": "string"}, "quote": {"type": "string"},
                   "when": {"type": "string"}, "property": {"type": "string"},
                   "kind": {"type": "string", "enum": ["fact", "self_report"]}}}}}}
PROPS = ("studied_at", "worked_at", "project", "skill")  # 经历类的具体属性（ontology.py BACKGROUND）
FILE_SCHEMA = ("CREATE TABLE IF NOT EXISTS file_digest (evidence_id INTEGER PRIMARY KEY, at TEXT NOT NULL, about_her INTEGER, "
               "facts INTEGER NOT NULL DEFAULT 0, tries INTEGER NOT NULL DEFAULT 0, result TEXT)")
# 每晚分批：小批次 7 份 11-03 验收过（她只改了某大学那几条，是以前聊天里就有的误会），11-04 打开，每晚 15 份（夜里任务有 15 分钟上限）
FILE_CHUNK, FILE_MAX_CHUNKS, FILES_PER_NIGHT, MAX_TRIES = 12000, 4, 15, 3
FILE_FACTS_SCHEMA = {"type": "object", "required": ["about_her", "facts"], "properties": {
    "about_her": {"type": "boolean"}, "why": {"type": "string"}, "facts": FACTS_SCHEMA["properties"]["facts"]}}


# 整段对话（format = "transcript"）
HER = "她"
AGENT_LABELS = ("Grok", "Codex", "Claude", "Claude Code", "ChatGPT", "Gemini", "豆包", "Kimi", "DeepSeek", "WorkBuddy", "Trae")  # 这些说话人算 agent（交接时没写 agent 也认）
TX_MARK = "格式：整段对话"
SPEAKER = re.compile(r"^【([^】\n]{1,40})】(.*)$")
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
TX_PIECE = 12000  # 一次给模型的字数（和文件整理的一段一样大）；交接一块最多 MAX_CHARS
NIGHT_LIMIT = 30  # 每晚补漏最多几份（夜里任务有 15 分钟上限；收到时已在后台整理）
TX_SCHEMA = {"type": "object", "required": ["facts"], "properties": {"facts": {"type": "array", "items": {
    "type": "object", "required": ["speaker", "statement", "dimension", "quote"],
    "properties": {"speaker": {"type": "string"}, "statement": {"type": "string"}, "dimension": {"type": "string"},
                   "quote": {"type": "string"}, "when": {"type": "string"}, "property": {"type": "string"},
                   "kind": {"type": "string", "enum": ["fact", "self_report"]}}}}}}


def ensure_digest(db):
    """建表；老表补上 ok、tries 两列（老记录先当整理成了，错标的由 migrations/20261105_retry_failed_digests.py 改回待重试）。"""
    db.conn.execute(SCHEMA)
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(interview_digest)")}
    for col in ("ok", "tries"):
        if col not in cols:
            db.conn.execute(f"ALTER TABLE interview_digest ADD COLUMN {col} INTEGER NOT NULL DEFAULT 1")


DONE_SQL = (f"SELECT evidence_id FROM interview_digest WHERE (ok = 1 AND COALESCE(result, '') NOT LIKE '%\"failed%') "
            f"OR tries >= {MAX_TRIES}")  # 整理成了，或试满了放弃；旧代码写的“ok=1 但结果里带 failed”也当没成（上线时还在跑的旧进程会这样写）


def failed(result: dict) -> bool:
    return bool(result.get("failed") or result.get("failed_pieces"))


def is_transcript(text: str) -> bool:
    """看着是整段对话：有【她】开头的行、没有旧格式的“答：”行（旧的交接命令不用改，服务器自己认）。"""
    return bool(re.search(r"^【她】", text or "", re.M)) and not re.search(r"^答：", text or "", re.M)


def parse_transcript(body: str) -> list:
    """整段对话 → [{"who", "text", "day"}]；不是【某某】开头的行接在上一句后面，〔……〕行里的日期管后面的句子。"""
    out, day = [], ""
    for ln in (body or "").split("\n"):
        s = ln.strip()
        if s.startswith("〔") and s.endswith("〕"):
            m = DAY.search(s)
            day = m.group(0) if m else day
            continue
        m = SPEAKER.match(s)
        if m:
            out.append({"who": m.group(1).strip(), "text": m.group(2), "day": day})
        elif out and s:
            out[-1]["text"] += "\n" + s
    return out


def source_of(speaker: str, agent: str = "") -> str:
    if speaker == HER:
        return "her_words"
    if speaker == agent or speaker in AGENT_LABELS:
        return "agent_relay"
    return "other_person"


def _said_by(turns: list) -> dict:
    by = {}
    for t in turns:
        by.setdefault(t["who"], []).append(t["text"])
    return {k: "\n".join(v) for k, v in by.items()}


def check_transcript(data: dict, turns: list) -> list:
    """原话片段必须原样在那个说话人的行里；别人 / agent 说的，说法里要写明是谁说的。"""
    by, errs = _said_by(turns), []
    for i, f in enumerate(data["facts"]):
        who, q = (f.get("speaker") or "").strip(), (f.get("quote") or "").strip()
        if who not in by:
            errs.append(f"facts[{i}].speaker 要是记录里的说话人（{'、'.join(by)}）之一")
        elif not q or q not in by[who]:
            errs.append(f"facts[{i}].quote 不在【{who}】说的话里")
        elif who != HER and who not in (f.get("statement") or ""):
            errs.append(f"facts[{i}].statement 要写明是{who}说的（比如“{who}说她……”）")
        if f.get("dimension") not in lifebase.DIMENSIONS:
            errs.append(f"facts[{i}].dimension 不认识")
        if f.get("property") and f["property"] not in PROPS:
            errs.append(f"facts[{i}].property 只能是 {'、'.join(PROPS)} 或空")
    return errs


def extract_transcript(router, db, piece: str, session: str, ref: str, purpose: str = "transcript-facts"):
    """一段整段对话 → 校验过的 {"facts": [...]}（每条加 "day"：原话那句的日期）；两次都不合格返回 None。"""
    from . import llm
    turns = parse_transcript(piece)
    version, system = llm.load_prompt("transcript_facts")
    data = llm.structured(router, db, "transcript_facts", system, f"会话：{session}\n\n{piece}", TX_SCHEMA,
                          check=lambda d: check_transcript(d, turns), version=version, input_ref=ref, purpose=purpose)
    if data is not None:
        for f in data["facts"]:
            hit = next((t for t in turns if t["who"] == f["speaker"].strip() and f["quote"].strip() in t["text"]), None)
            f["day"] = hit["day"] if hit else ""
    return data


def _pieces(body: str, size: int = TX_PIECE) -> list:
    """按行切成不超过 size 字的几段；每段开头补上当时的〔日期〕抬头。"""
    out, cur, n, head = [], [], 0, ""
    for ln in body.split("\n"):
        s = ln.strip()
        if s.startswith("〔") and s.endswith("〕"):
            head = s
        if cur and n + len(ln) + 1 > size:
            out.append("\n".join(cur))
            cur, n = ([head] if head and head != s else []), len(head) + 1
        cur.append(ln)
        n += len(ln) + 1
    if cur:
        out.append("\n".join(cur))
    return out


def digest_transcript(store, router, ev: dict, today: date = None, only=None) -> dict:
    """一份整段对话：分段提取，她说的走画像规则，别人 / agent 说的按来源另记。一段没过校验（或模型出错）不影响别的段。
    only：重试时只做上次没过的那几段（上次过了的已经落库，不再做一遍）。"""
    db = store.db
    first, _, body = ev["text"].partition("\n")
    bits = _header(first)
    session, agent = bits.get("会话", "别的会话"), bits.get("agent", "")
    day = date.fromisoformat((ev["occurred_at"] or "")[:10]) if DAY.fullmatch((ev["occurred_at"] or "")[:10]) else (today or her_today())
    facts, failed = [], []
    for n, piece in enumerate(_pieces(body)):
        if (only is not None and n not in only) or not parse_transcript(piece):
            continue
        try:
            data = extract_transcript(router, db, piece, session, f"ev:{ev['id']}:{n}")
        except Exception as e:  # 模型出错（额度满、超时）：这一段下次再试
            data = None
            db.conn.rollback()
            print(f"[interviews] ev {ev['id']} 第 {n} 段模型出错：{type(e).__name__} {str(e)[:120]}", flush=True)
        if data is None:
            failed.append(n)
            continue
        facts += data["facts"]
    hers = [f for f in facts if f["speaker"].strip() == HER]
    result = _land(store, router, ev["id"], hers, day, _context(ev["text"], ev.get("source_ref") or "")) if hers else {"ADD": 0, "UPDATE": 0}
    others = [{**f, "source_kind": source_of(f["speaker"].strip(), agent)} for f in facts if f["speaker"].strip() != HER]
    result.update(lifebase.write_said_facts(db, ev["id"], others, session, day.isoformat(), PROPS))
    if failed:
        result["failed_pieces"] = failed
    return result


def _header(first: str) -> dict:
    return dict(p.split("：", 1) for p in first[len("【访谈】"):].split("；") if "：" in p) if first.startswith("【访谈】") else {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def receive(store, payload: dict, now: datetime = None) -> tuple:
    """收下一份访谈。正文 {"session": 会话名, "scene": 场合, "text": 记录（问题和她的回答）, "at": 访谈时间（可选）}。
    kind = "file"：她在这个会话里直接给的文件，{"name": 文件名, "text": 文件的文字}，
    算“她提供的资料”（来源 file），和她发给 小伴 的文件一样整理。"""
    if payload.get("kind") == "file":
        return _receive_file(store, payload, now)
    if payload.get("format") == "memory_export" or (not payload.get("format") and is_memory_export(str(payload.get("text") or ""))):
        res = receive_memory_export(store, str(payload.get("text") or ""), via=str(payload.get("session") or "访谈通道"), now=now)
        return (200 if res["ok"] else 400), res
    session = " ".join(str(payload.get("session") or "").split())[:80]
    scene = " ".join(str(payload.get("scene") or "").split())[:80]
    text = str(payload.get("text") or "").strip()
    if not session or not text:
        return 400, {"ok": False, "error": "要有会话名（session）和访谈记录（text）"}
    if len(text) > MAX_CHARS:
        return 400, {"ok": False, "error": f"一次最多 {MAX_CHARS} 字，分几次交"}
    fmt = payload.get("format")
    if fmt not in (None, "", "transcript", "qa", "memory_export"):
        return 400, {"ok": False, "error": "format 只能是 transcript（整段对话）或不写（问/答）"}
    transcript = fmt == "transcript" or (not fmt and is_transcript(text))
    agent = " ".join(str(payload.get("agent") or "").split()).replace("；", "")[:20]
    key = str(payload.get("key") or "").lower()  # 微信指纹（relations.wx_key）：人物卡按它认人，同名的不混（Codex 评审 11-05）
    key = key if re.fullmatch(r"[0-9a-f]{12,64}", key) else ""
    at = str(payload.get("at") or (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))[:25]
    ref = "interview:" + hashlib.sha256(f"{session}|{scene}|{text}".encode()).hexdigest()[:16]
    db = store.db
    lifebase.ensure(db)
    header = (f"【访谈】会话：{session}；场合：{scene or '没写'}；时间：{at}" + (f"；{TX_MARK}" if transcript else "")
              + (f"；agent：{agent}" if transcript and agent else "") + (f"；wx：{key}" if key else "") + "\n")
    ev = lifebase.try_evidence(db, "interview", ref, at, header + text)
    db.conn.commit()
    if ev is None:
        return 200, {"ok": True, "skipped": "这份她要求删掉过，不再收"}
    if transcript:  # 初始化：收到就在后台一次整理完
        if not os.environ.get("ETHAN_NO_KICK"):
            kick_digest_all(store.root)
        return 200, {"ok": True, "evidence": ev, "msg": "收到了，后台马上整理进记忆"}
    return 200, {"ok": True, "evidence": ev, "msg": "收到了，今晚整理进记忆"}


# ---------- 别的 AI 的记忆导出（借鉴清单第 1 条，docs/operations/agent-export.md“其他 AI 怎么导”） ----------
# 她把固定的“导出提示词”贴给 Grok App、ChatGPT、Gemini、豆包……，把它们的回答原样转给 小伴（Telegram 或工作台）。
# 认出这个格式就不当聊天：原文存一条原始记录（来源 agent：agent 转述，不是她的原话），每条记成假设（kind = hypothesis，
# source_kind = agent_relay，私密），同时在“我”页“待你定”放一题请她逐条确认。不用模型，按格式直接导。
MEMX_HEAD = re.compile(r"^[ \t>*#]*(?:【\s*AI\s*记忆导出\s*】|\[\s*AI MEMORY EXPORT\s*\])\s*(?:(?:来自|from|source)\s*[:：]?)?\s*(.*)$",
                       re.I | re.M)
MEMX_SAID = ("她说过", "她说的", "说过", "她亲口说", "said", "stated", "she said", "user said")
MEMX_GUESS = ("推测", "我推测", "猜测", "推断", "inferred", "guess", "guessed", "inference")
MEMX_ITEM = re.compile(r"^\s*(?:[-*•·]|\d+[.)、])?\s*[\[【]\s*([^\]】]{1,12}?)\s*[\]】]\s*(?:[\[【]\s*([^\]】]{1,16}?)\s*[\]】])?\s*(.+)$")
MEMX_WHEN = re.compile(r"\s*[（(]\s*([^()]{1,30})\s*[）)]\s*[。.]?\s*$")
# 类别 → 方面（lifebase.DIMENSIONS）；提示词里列的就是这些词，认不出放“偏好”（和每晚整理的“其他”一样）
MEMX_CATS = {"values": ("价值观", "价值", "信念", "values", "beliefs"),
             "temperament": ("性格", "情绪", "personality", "temperament", "emotions"),
             "thinking": ("思维", "决策", "想法", "thinking", "decisions"),
             "abilities": ("经历", "技能", "工作", "学习", "教育", "职业", "background", "skills", "work", "education", "career"),
             "expression": ("表达", "沟通", "说话", "写作", "communication", "expression", "writing"),
             "preferences": ("偏好", "喜好", "审美", "饮食", "兴趣", "preferences", "likes", "tastes", "interests", "food"),
             "body": ("身体", "健康", "运动", "health", "body", "fitness"),
             "relationships": ("关系", "家人", "朋友", "人", "relationships", "family", "friends", "people"),
             "resources": ("处境", "住处", "钱", "财务", "生活", "situation", "location", "money", "finance", "living"),
             "rhythms": ("习惯", "作息", "时间", "日常", "habits", "routine", "schedule")}
MEMX_MAX = 80  # 一次最多导几条（多的让她分几次贴）
MEMX_SCHEMA = "CREATE TABLE IF NOT EXISTS memx_seen (fp TEXT PRIMARY KEY, fact_id INTEGER, at TEXT NOT NULL)"


def _memx_lines(text: str) -> list:
    return [ln.replace("\\[", "[").replace("\\]", "]").replace("**", "") for ln in (text or "").replace("\r", "").split("\n")]


def _memx_kind(tag: str):
    t = (tag or "").strip().lower()
    if any(t == w or t.startswith(w) for w in MEMX_SAID):
        return "said"
    if any(t == w or t.startswith(w) for w in MEMX_GUESS):
        return "guess"
    return None


def _memx_dim(cat: str) -> str:
    c = (cat or "").strip().lower()
    for dim, words in MEMX_CATS.items():
        if c and any(c == w or w in c for w in words):
            return dim
    return "preferences"


def _memx_when(raw: str):
    """“2025-03”“2025 年 3 月”“2025”“around 2024”→ (给她看的时间, 有效期开始 YYYY-MM-DD 或 None)。说不准的不填开始。"""
    raw = (raw or "").strip()
    m = re.search(r"((?:19|20)\d{2})\s*(?:[-/.年]\s*(\d{1,2})\s*(?:[-/.月]\s*(\d{1,2}))?)?", raw)
    if not m:
        return raw, None
    y, mo, d = m.group(1), int(m.group(2) or 1), int(m.group(3) or 1)
    try:
        return raw, date(int(y), mo, d).isoformat()
    except ValueError:
        return raw, f"{y}-01-01"


def parse_memory_export(text: str):
    """认出导出格式 → {"ai": 哪个 AI, "items": [{"kind": said / guess, "dim", "text", "when", "since"}]}；不是这个格式返回 None。
    要有抬头行（【AI 记忆导出】来自：某某 / [AI MEMORY EXPORT] from: X），且至少一条“- [她说过|推测][类别] 一句话（时间）”。"""
    lines = _memx_lines(text)
    head = next((MEMX_HEAD.match(ln) for ln in lines if MEMX_HEAD.match(ln)), None)
    if not head:
        return None
    ai = " ".join(re.sub(r"[`*_\[\]【】()]", " ", head.group(1)).split())[:20] or "某个 AI"
    items, seen = [], set()
    for ln in lines:
        m = MEMX_ITEM.match(ln)
        kind = _memx_kind(m.group(1)) if m else None
        if not kind:
            continue
        body = m.group(3).strip()
        w = MEMX_WHEN.search(body)
        when, since = _memx_when(w.group(1)) if w else ("", None)
        body = (body[:w.start()] if w else body).strip().rstrip("。.;；")
        key = re.sub(r"\s+", "", body)
        if len(key) < 3 or key in seen:
            continue
        seen.add(key)
        items.append({"kind": kind, "dim": _memx_dim(m.group(2)), "text": body, "when": when, "since": since})
    return {"ai": ai, "items": items} if items else None


def is_memory_export(text: str) -> bool:
    return parse_memory_export(text) is not None


def _memx_fp(ai: str, text: str) -> str:
    flat = re.sub(r"[\s，。.]+", "", text)  # 不放进 f-string 里：服务器是 Python 3.11，f-string 里不能有反斜杠
    return hashlib.sha256(f"{ai.lower()}|{flat}".encode()).hexdigest()[:24]


def receive_memory_export(store, text: str, via: str = "", now: datetime = None) -> dict:
    """导入一份别的 AI 的记忆导出：原文存成 agent 转述的原始记录，每条一个假设 + 一道“待你定”的确认题。
    同一条（同一个 AI、同样的话）不导第二次；和已有的说法（含她说过“不像我”的）重复的跳过。返回给她看的一句话。"""
    from .core.agent import similar_task
    from .docquotes import redact
    got = parse_memory_export(text)
    if not got:
        return {"ok": False, "error": "没认出 AI 记忆导出的格式"}
    ai, items = got["ai"], got["items"]
    db = store.db
    lifebase.ensure(db)
    db.conn.execute(MEMX_SCHEMA)
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    body = redact(text.strip())[:MAX_CHARS]
    ref = "memx:" + hashlib.sha256(f"{ai}|{body}".encode()).hexdigest()[:16]
    ev = lifebase.try_evidence(db, "agent", ref, at, f"【AI 记忆导出】来自：{ai}；经：{via or '没写'}；agent 转述，不是她的原话\n{body}")
    if ev is None:
        db.conn.commit()
        return {"ok": True, "ai": ai, "added": 0, "skipped": len(items), "msg": "这份你要求删掉过，不再收"}
    olds = lifebase.known_statements(db)
    from . import mepage
    mepage.ensure(db)
    added, said, skipped, today = [], 0, 0, her_today().isoformat()
    for it in items[:MEMX_MAX]:
        fp = _memx_fp(ai, it["text"])
        if db.conn.execute("SELECT 1 FROM memx_seen WHERE fp = ?", (fp,)).fetchone():
            skipped += 1
            continue
        tag = "记得她说过" if it["kind"] == "said" else "推测"
        statement = redact(f"{ai}{tag}：{it['text']}")[:60]
        if similar_task(it["text"], olds) or similar_task(statement, olds):
            skipped += 1
            db.conn.execute("INSERT OR IGNORE INTO memx_seen (fp, fact_id, at) VALUES (?, NULL, ?)", (fp, at))
            continue
        ctx = f"{ai} 的记忆导出里{'说她说过' if it['kind'] == 'said' else '它自己推测'}的" + (f"（大概 {it['when']}）" if it["when"] else "") + "，只当资料"
        try:
            fid = lifebase.add_fact(db, it["dim"], statement, [ev], kind="hypothesis", source_kind="agent_relay",
                                    valid_from=it["since"] or today, confidence=0.3 if it["kind"] == "said" else 0.2,
                                    importance=2, visibility="private", context=ctx,
                                    detail=redact(it["text"])[:500] if len(statement) >= 60 else None)
        except lifebase.Forgotten:
            skipped += 1
            continue
        olds.insert(0, statement)
        db.conn.execute("INSERT OR IGNORE INTO memx_seen (fp, fact_id, at) VALUES (?, ?, ?)", (fp, fid, at))
        f = mepage._fact(db, fid)
        opt = {**mepage._option(db, f), "src": f"{ai} 转述（{'说你说过' if it['kind'] == 'said' else '它推测的'}）"
               + (f" · 大概 {it['when']}" if it["when"] else "")}
        db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) "
                        "VALUES ('confirm', 'long', ?, ?, ?, '[]', 0, ?)",
                        (f"{ai} 记得的关于你：对吗？（不对就点不对，不会当成你说的）", json.dumps([opt], ensure_ascii=False),
                         json.dumps([fid]), at))
        added.append(fid)
        said += it["kind"] == "said"
    db.conn.commit()
    more = len(items) - MEMX_MAX if len(items) > MEMX_MAX else 0
    msg = (f"收到 {ai} 的记忆导出：新记了 {len(added)} 条（说你说过的 {said} 条、它推测的 {len(added) - said} 条），"
           f"都只当{ai}的转述、先存成猜测，不算你说的；放进“我”页的“待你定”，你一条条点对不对。"
           + (f"重复的 {skipped} 条跳过了。" if skipped else "")
           + (f"一次最多 {MEMX_MAX} 条，后面 {more} 条再贴一次。" if more else ""))
    return {"ok": True, "ai": ai, "evidence": ev, "added": len(added), "said": said, "skipped": skipped, "fact_ids": added, "msg": msg}


def _receive_file(store, payload: dict, now: datetime = None) -> tuple:
    session = " ".join(str(payload.get("session") or "").split())[:80]
    name = " ".join(str(payload.get("name") or "").split())[:120]
    text = str(payload.get("text") or "").strip()
    if not session or not name or not text:
        return 400, {"ok": False, "error": "要有会话名（session）、文件名（name）和文件的文字（text）"}
    if len(text) > MAX_CHARS:
        return 400, {"ok": False, "error": f"一次最多 {MAX_CHARS} 字"}
    at = str(payload.get("at") or (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))[:25]
    ref = "file:handoff:" + hashlib.sha256(f"{name}|{text}".encode()).hexdigest()[:16]
    db = store.db
    lifebase.ensure(db)
    ev = lifebase.try_evidence(db, "file", ref, at, text, path=f"handoff/{name}")
    db.conn.commit()
    if ev is None:
        return 200, {"ok": True, "skipped": "这份她要求删掉过，不再收"}
    return 200, {"ok": True, "evidence": ev, "msg": f"收到了（{session} 交来的文件）"}


def pending(db) -> list:
    lifebase.ensure(db)
    ensure_digest(db)
    return [dict(r) for r in db.conn.execute(
        "SELECT id, text, occurred_at, source_ref FROM evidence WHERE (source IN ('interview', 'sent') OR (source = 'workbench' AND "
        "(source_ref LIKE 'diary:%' OR source_ref LIKE 'wish:%'))) AND deleted_at IS NULL AND text IS NOT NULL "
        f"AND id NOT IN ({DONE_SQL}) ORDER BY id")]


def _priority(ev: dict) -> tuple:
    """后台一次整理很多时的顺序：先不是微信的（访谈、邮件、日记），再私聊，最后群聊；同类里最近的先。"""
    head = (ev.get("text") or "")[:300]
    rank = 2 if "微信群 · " in head else 1 if "微信 · " in head else 0
    recent_first = "".join(chr(0x10FFFF - ord(ch)) for ch in (ev.get("occurred_at") or "")[:19]) if rank else ""
    return (rank, recent_first, ev.get("id") or 0)


def _context(text: str, ref: str = "") -> str:
    if ref.startswith("diary:") and ":edit-" in ref:  # 她在系统草稿上改的句子
        return f"在工作台 {ref[6:16]} 的日记里，改系统草稿时自己写的"
    if ref.startswith("diary:"):  # 她亲手写、存过的日记（1.5，S17）
        return f"在工作台 {ref[6:16]} 的日记里自己写的"
    if ref.startswith("wish:"):
        return "在昨日卡的心愿里写的"
    if ref.startswith("sent:"):
        return "在她自己发出去的邮件里写的"
    first = text.split("\n", 1)[0]
    if first.startswith("【访谈】"):
        bits = _header(first)
        return f"在“{bits.get('会话', '别的会话')}”的{bits.get('场合', '访谈')}里说的"
    return "在别的会话里说的"


def _extract(router, db, prompt: str, text: str, schema: dict, ref: str, purpose: str):
    """要一组说法：原话片段必须原样在 text 里，方面必须认识。"""
    from . import llm
    version, system = llm.load_prompt(prompt)
    return llm.structured(router, db, prompt, system, text, schema, input_ref=ref, purpose=purpose, version=version,
                          check=lambda d: [f"facts[{i}].quote 不在原文里" for i, f in enumerate(d["facts"])
                                           if (f.get("quote") or "").strip() not in text]
                          + [f"facts[{i}].dimension 不认识" for i, f in enumerate(d["facts"]) if f.get("dimension") not in lifebase.DIMENSIONS]
                          + [f"facts[{i}].property 只能是 {'、'.join(PROPS)} 或空" for i, f in enumerate(d["facts"]) if f.get("property") and f["property"] not in PROPS])


def _land(store, router, ev_id: int, facts: list, day: date, context: str) -> dict:
    """一组说法交给画像规则（新增 / 更新 / 作废）落库，带语境。"""
    from . import portrait
    db = store.db
    kept = [f for f in facts if f["statement"].strip()][:60]
    lines = [(f["statement"].strip() + (f"（{f['when']}）" if f.get("when") else ""))[:60] for f in kept]
    if not lines:
        return {"ADD": 0, "UPDATE": 0}
    ops = portrait.propose_ops(router, db, lines, portrait.related(db, lines), day, f"ev:{ev_id}")
    if not ops:
        return {"failed": "对照旧事实没通过校验"}
    props = {f"n{i + 1}": f["property"] for i, f in enumerate(kept) if f.get("property") in PROPS}
    kinds = {f"n{i + 1}": f["kind"] for i, f in enumerate(kept) if f.get("kind") in ("fact", "self_report")}
    return portrait.apply_ops(db, ops, ev_id, day, context=context, props=props, kinds=kinds)


def files_pending(db, limit: int = FILES_PER_NIGHT, ids=None) -> list:
    """还没整理的文件。ids：只挑这几份（小批次验收用）。"""
    lifebase.ensure(db)
    db.conn.execute(FILE_SCHEMA)
    only = f" AND e.id IN ({','.join(str(int(i)) for i in ids)})" if ids else ""
    return [dict(r) for r in db.conn.execute(
        "SELECT e.id, e.text, e.occurred_at, e.source_ref, e.path FROM evidence e LEFT JOIN file_digest d ON d.evidence_id = e.id "
        "WHERE e.source = 'file' AND e.deleted_at IS NULL AND e.text IS NOT NULL AND TRIM(e.text) != '' "
        "AND (d.evidence_id IS NULL OR (d.about_her IS NULL AND d.tries < ?))" + only + " ORDER BY e.id LIMIT ?", (MAX_TRIES, limit))]


def _file_title(db, ev) -> str:
    try:
        r = db.conn.execute("SELECT title FROM brain_items WHERE ref = ?", (ev["source_ref"],)).fetchone()
    except Exception:
        r = None
    return (r[0] if r else None) or (ev["path"] or "").rsplit("/", 1)[-1] or "一份文件"


def digest_files(store, router, today: date = None, limit: int = FILES_PER_NIGHT, ids=None) -> dict:
    """每晚分批：已收的文件逐份判断是不是关于她、是就提取成说法。一份失败不影响别的，下次重试（最多 3 次）。"""
    db = store.db
    out = {"files": 0, "about_her": 0, "facts": 0, "failed": 0}
    for ev in files_pending(db, limit, ids):
        text = ev["text"]
        day = date.fromisoformat(ev["occurred_at"][:10]) if (ev["occurred_at"] or "")[:10] else (today or her_today())
        title = _file_title(db, ev)
        chunks = [text[i:i + FILE_CHUNK] for i in range(0, min(len(text), FILE_CHUNK * FILE_MAX_CHUNKS), FILE_CHUNK)]
        about, facts, ok = None, [], True
        for n, chunk in enumerate(chunks):
            data = _extract(router, db, "file_facts", f"文件名：{title}\n\n{chunk}", FILE_FACTS_SCHEMA, f"ev:{ev['id']}:{n}", "file-facts")
            if data is None:
                ok = False
                break
            about = bool(about) or data["about_her"]
            facts += data["facts"] if data["about_her"] else []
        result, landed = {}, 0
        if ok and about and facts:
            result = _land(store, router, ev["id"], facts, day, f"在她发来的文件《{title[:40]}》里写的")
            landed = result.get("ADD", 0) + result.get("UPDATE", 0)
            ok = "failed" not in result
        elif ok and not about:
            result = {"skipped": "不是关于她本人的"}
        db.conn.execute(FILE_SCHEMA)
        db.conn.execute("INSERT INTO file_digest (evidence_id, at, about_her, facts, tries, result) VALUES (?, ?, ?, ?, 1, ?) "
                        "ON CONFLICT(evidence_id) DO UPDATE SET at = excluded.at, about_her = excluded.about_her, facts = excluded.facts, "
                        "tries = file_digest.tries + 1, result = excluded.result",
                        (ev["id"], _now(), (1 if about else 0) if ok else None, landed, json.dumps(result or {"failed": "提取没通过校验"}, ensure_ascii=False)))
        db.conn.commit()
        out["files"] += 1
        out["about_her"] += 1 if ok and about else 0
        out["facts"] += landed
        out["failed"] += 0 if ok else 1
    return out


def files_progress(db) -> dict:
    """进度（给服务器验收和“我”页）：总共多少份、整理了多少、关于她的多少、提取了多少条。"""
    lifebase.ensure(db)
    db.conn.execute(FILE_SCHEMA)
    total = db.conn.execute("SELECT COUNT(*) FROM evidence WHERE source = 'file' AND deleted_at IS NULL AND text IS NOT NULL AND TRIM(text) != ''").fetchone()[0]
    r = db.conn.execute("SELECT COUNT(*), SUM(about_her = 1), SUM(facts), SUM(about_her IS NULL) FROM file_digest").fetchone()
    return {"files": total, "done": (r[0] or 0) - (r[3] or 0), "about_her": r[1] or 0, "facts": r[2] or 0, "retrying": r[3] or 0}


def digest(store, router, today: date = None, limit: int = None, skip=None, stop=None) -> dict:
    """把还没整理的访谈逐份提取成说法，交给画像规则落库。一份失败不影响别的，记一次没成，下次再试（最多 MAX_TRIES 次）。
    limit：这次最多几份（每晚补漏用 NIGHT_LIMIT；不写 = 全部）。整段对话按说话人分来源（digest_transcript）。
    skip：这一轮已经试过的编号（digest_all 用，失败的这一轮不再马上重试，留给下次）。
    stop：每份开始前问一句，为真就停（有限批，digest_all 用）。"""
    db = store.db
    out = {"interviews": 0, "facts": 0, "others": 0, "failed": 0}
    todo = sorted((e for e in pending(db) if not skip or e["id"] not in skip), key=_priority)
    for ev in todo[:limit] if limit else todo:
        if stop and stop():
            break
        if skip is not None:
            skip.add(ev["id"])
        try:
            result = _digest_one(store, router, ev, today)
        except Exception as e:  # 模型出错（额度满、超时）之类：这一份记一次没成
            db.conn.rollback()
            from .bgwork import QuotaBlocked
            if isinstance(e, QuotaBlocked):  # 在用付费额度 / 额度用完：整轮停，这份不算试过
                if skip is not None:
                    skip.discard(ev["id"])
                break
            result = {"failed": f"整理出错：{type(e).__name__} {str(e)[:200]}"}
        out["failed"] += 1 if failed(result) else 0
        out["facts"] += result.get("ADD", 0) + result.get("UPDATE", 0)
        out["others"] += result.get("other_person", 0) + result.get("agent_relay", 0)
        _done(db, ev["id"], result)
        out["interviews"] += 1
    return out


def _digest_one(store, router, ev: dict, today: date = None) -> dict:
    """一份：整段对话走 digest_transcript（重试时只做上次没过的段），别的整份提取。"""
    db = store.db
    text = ev["text"]
    if TX_MARK in text.split("\n", 1)[0]:
        prev = _last(db, ev["id"])
        only = set(prev["failed_pieces"]) if prev and prev.get("failed_pieces") and not prev.get("failed") else None
        return digest_transcript(store, router, ev, today, only=only)
    day = date.fromisoformat((ev["occurred_at"] or "")[:10]) if (ev["occurred_at"] or "")[:10] else (today or her_today())
    ref = ev.get("source_ref") or ""
    if ref.startswith("sent:"):  # 她自己发出去的邮件（引用的别人的话已经去掉）
        text = "【她自己发出去的一封邮件，下面是她写的部分，整段都是她的回答（原话），没有提问；只提取关于她本人的，收信人的事不算】\n" + text
    elif ref.startswith("diary:") and ":edit-" in ref:  # 系统替她写的日记、她改过：只有改动的句子是她的
        text = ("【她在工作台上改了系统替她写的日记草稿。下面是她在系统草稿上改动的句子（新增或改过的），是她的原话，没有提问；"
                "没改的句子是系统写的，不在这里，也不算她的话】\n") + text
    elif ref.startswith(("diary:", "wish:")):  # 工作台上她亲手写的：整段都是她的话，没有提问
        text = ("【她在工作台上亲手写的日记，下面整篇都是她的回答（原话），没有提问】\n" if ref.startswith("diary:")
                else "【她在昨日卡上写的心愿，下面整段都是她的回答（原话），没有提问】\n") + text
    data = _extract(router, db, "interview_facts", text, FACTS_SCHEMA, f"ev:{ev['id']}", "interview-facts")
    if data is None:
        return {"failed": "两次都没通过校验"}
    return _land(store, router, ev["id"], data["facts"], day, _context(text, ev.get("source_ref") or ""))


def _last(db, ev_id: int):
    r = db.conn.execute("SELECT result FROM interview_digest WHERE evidence_id = ?", (ev_id,)).fetchone()
    try:
        return json.loads(r[0]) if r and r[0] else None
    except ValueError:
        return None


def _done(db, ev_id: int, result: dict):
    """记一次：成了 ok = 1；没成 ok = 0、tries + 1（到 MAX_TRIES 就不再试）。"""
    ensure_digest(db)
    ok = 0 if failed(result) else 1
    db.conn.execute("INSERT INTO interview_digest (evidence_id, at, result, ok, tries) VALUES (?, ?, ?, ?, 1) "
                    "ON CONFLICT(evidence_id) DO UPDATE SET at = excluded.at, result = excluded.result, ok = excluded.ok, "
                    "tries = interview_digest.tries + 1", (ev_id, _now(), json.dumps(result, ensure_ascii=False), ok))
    db.conn.commit()


def given_up(db) -> int:
    """试满 MAX_TRIES 次都没整理成、放弃了的份数（巡检看）。"""
    ensure_digest(db)
    return db.conn.execute("SELECT COUNT(*) FROM interview_digest WHERE ok = 0 AND tries >= ?", (MAX_TRIES,)).fetchone()[0]


# ---------- 收到整段对话就在后台一次整理完（初始化；夜里的 digest 照旧补漏） ----------
LOCK = ".interviews-digest.lock"


def kick_digest_all(root, spawn=None) -> bool:
    from .bgwork import kick
    return bool(kick(root, "interviews", ["--digest-all"], spawn))


def lock(root):
    """拿到整理锁返回打开的锁文件（用完 close），别人在整理返回 None。后台的 --digest-all 和每晚补漏共用，不会两边同时整理同一份。"""
    from .bgwork import try_lock
    return try_lock(root, LOCK)


def digest_all(store, router, today: date = None, batch: int = 20, stop=None, beat=None) -> dict:
    """一批一批整理，直到没有待整理的（整理时新收到的也接着整理）。
    stop：有限批（ethan2-backlog 每轮最多跑几十分钟，到点停下、标 more，下一轮接着跑，让人物卡也轮得到）；不给 = 不设上限。
    beat：每批之后报一次进展（心跳进 jobs.json）。"""
    total = {"interviews": 0, "facts": 0, "others": 0, "failed": 0}
    tried = set()  # 这一轮试过的（失败的留给下次，不在这一轮里连着试满 3 次）
    while True:
        got = digest(store, router, today, limit=batch, skip=tried, stop=stop)
        for k in total:
            total[k] += got.get(k, 0)
        if got["interviews"] and beat:
            beat(f"访谈：这一轮整理了 {total['interviews']} 份")
        if stop and stop() and pending(store.db):
            total["more"] = True
            return total
        if got["interviews"] == 0:
            return total


def nightly(store, router) -> dict:
    """每晚补漏：后台在整理就不碰；最多 NIGHT_LIMIT 份，还剩的交给后台一次整理完。"""
    from .bgwork import paid_block
    b = paid_block(store.root)
    if b:  # 在用付费额度 / 订阅额度用完：大批整理不跑
        return {"skipped": b["reason"]}
    f = lock(store.root)
    if f is None:
        return {"skipped": "后台正在整理"}
    try:
        out = digest(store, router, limit=NIGHT_LIMIT)
        left = len(pending(store.db))
    finally:
        f.close()
    if left and not os.environ.get("ETHAN_NO_KICK"):
        out["left"] = left
        kick_digest_all(store.root)
    return out


def main():
    import argparse
    from pathlib import Path
    from .cli import build_router
    from .memory import MemoryStore
    parser = argparse.ArgumentParser(description="访谈、已收文件整理成关于她的说法")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--files", type=int, default=0, help="这次最多整理几份文件（0 = 只看进度）")
    parser.add_argument("--ids", default="", help="只整理这几份（原始记录编号，逗号分隔；小批次验收用）")
    parser.add_argument("--digest-all", action="store_true", help="把待整理的访谈 / 整段对话全部整理完（收到时后台自动起）")
    parser.add_argument("--max-minutes", type=float, default=0, help="和 --digest-all 一起用：这一轮最多跑几分钟（0 = 不设上限）")
    args = parser.parse_args()
    if args.digest_all:
        from .bgwork import gate
        if gate(args.data, "interviews"):  # 在用付费额度 / 订阅额度用完：后台不跑
            return
        f = lock(args.data)
        if f is None:
            print("已经有一个在整理了", flush=True)
            return
        store = MemoryStore(args.data)
        try:
            from .bgwork import deadline, heartbeat, patient  # 她在聊就让路、额度满了歇一会儿，不碰备用模型
            from .bgwork import retry_locked
            router = patient(build_router(args.data, 50.0), store)
            from .bgwork import stop_or_blocked
            stop = stop_or_blocked(args.data, deadline(args.max_minutes))
            print(json.dumps(retry_locked(lambda: digest_all(store, router, stop=stop, beat=lambda n: heartbeat(args.data, n))),
                             ensure_ascii=False), flush=True)
        finally:
            store.close()
            f.close()
        return
    store = MemoryStore(args.data)
    try:
        if args.files:
            ids = [int(x) for x in args.ids.split(",") if x.strip()] or None
            print(json.dumps(digest_files(store, build_router(args.data, 50.0), limit=args.files, ids=ids), ensure_ascii=False), flush=True)
        print(json.dumps(files_progress(store.db), ensure_ascii=False), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
