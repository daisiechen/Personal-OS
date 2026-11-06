"""关系：微信导出的统计 + 每晚整理成人物卡 + 社交管理三张清单（docs/design/relations.md，方案 A）。

“你迁移之后能不能读到我跟这个人的聊天记录，并帮我建立这个人的人物卡？小伴 也要帮我做社交管理：
哪些人物是重要的，哪些人物我最近没有跟他联系，哪些人物我最近要跟他发点什么。”页面名叫“关系”。

1. 统计（ingest_stats）：她 Mac 上的 ethan/wechatexport.py 每次 --send 时按私聊联系人算好交过来（POST /cloud/people_stats，
   云端钥匙）。只覆盖数字字段（最近联系、90 / 365 天条数、她说的占比、谁先开口、共同群）；重要程度没被她改过就按统计自动排。
   微信 username 在 Mac 上就哈希成 wx_key，只用来去重，不显示。客服、中介、商家、酒店管家这类按名字跳过（is_service）。
2. 整理（organize，每晚整理里跑，每晚最多 PER_NIGHT 个人）：从这个人的微信交接记录（访谈原始记录，会话名“微信 · <名字>”）
   用模型整理关系、怎么认识的、常聊话题、对方近况、生日和重要日子、没了结的事、可以发的一句。结构化输出 + 代码校验
   （没了结的事要带原话片段，原话必须在记录里）。她改过的字段有锁（ethan/locks.py，kind = person），自动流程不动。
3. 社交管理（due）：该联系了（超过提醒间隔）、生日和重要日子（提前 7 天）、待回复（对方问了她没回、7 天内）。
   周报的“社交”、小伴 聊天时顺口提一两个都从这里取。替她发消息不做：卡上只给“可以发的一句”草稿，发不发、怎么发是她的事。
4. 删卡：记一把删除锁（kind = person，field = deleted，按 wx_key 或名字），以后导出的统计不再把这个人建回来。
"""

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone

from . import lifebase, locks, memusers, tx

DEFAULT_EVERY = {3: 14, 2: 30, 1: 90}  # 她定的默认提醒间隔：3 级 14 天、2 级 30 天、1 级 90 天
PER_NIGHT = 8  # 每晚整理里最多几个人（夜里任务有 15 分钟上限）；统计一到会另起后台把所有人一次整理完（kick_all）
MAX_TEXT = 16000  # 一个人最多给模型看多少字的交接记录（最近的优先）
SOON_DAYS = 7  # 生日和重要日子提前几天提醒
REPLY_DAYS = 7  # 待回复看最近几天的
STAT_KEYS = ("last", "n90", "n365", "total", "her_share", "her_starts", "rounds", "groups")
COLUMNS = (("wx_key", "TEXT"), ("wx_name", "TEXT"), ("relation", "TEXT"), ("importance", "INTEGER"), ("topics", "TEXT"),
           ("source", "TEXT"), ("recent", "TEXT"), ("open_loops", "TEXT"), ("loops_done", "TEXT"), ("dates", "TEXT"),
           ("draft", "TEXT"), ("frequency", "TEXT"), ("stats", "TEXT"), ("stats_at", "TEXT"), ("organized_at", "TEXT"),
           ("source_ids", "TEXT"))
# 整理从聊天记录里写出来的字段：遗忘某份记录时这些清空等重做（她锁住的不动，Codex 评审 11-05 第 9 条）
ORGANIZED = ("relation", "how_met", "circle", "topics", "recent", "draft", "dates", "open_loops", "birthday")
MAX_DONE = 5000  # 她了结过的事最多记多少个编号（只记编号，不截到 50：截掉的会被下次整理加回来）
# 她能在卡上改的字段（改了锁住）；数字统计不让改（每次导出覆盖）
EDITABLE = {"name": 30, "circle": 20, "relation": 60, "how_met": 200, "notes": 500, "birthday": 5, "recent": 300,
            "topics": 200, "draft": 200, "importance": 1, "contact_every_days": 4}
CIRCLES = ("家人", "挚友", "朋友", "同学", "同事", "合作", "老师", "其他")

# 客服、中介、商家这类默认跳过
SERVICE = re.compile(r"客服|中介|商家|商城|旗舰店|专营|店铺|小店|酒店|管家|签证|代办|代理|房产|地产|租房|公寓|物业|置业|"
                     r"专属服务|VIP|vip|小助手|助手|顾问|销售|售后|官方|服务号|经纪|留学|移民|保险|贷款|理财经理|"
                     r"快递|外卖|师傅|门店|前台|售楼|招聘|HR|猎头|课程顾问|健身教练|美容|美甲|医美")
FAMILY = re.compile(r"母上|父上|爸|妈|爹|娘|爷爷|奶奶|外公|外婆|姥姥|姥爷|老公|老婆|哥哥|姐姐|弟弟|妹妹")
MMDD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
YMD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")
HEX = re.compile(r"^[0-9a-f]{12,64}$")

CARD_SCHEMA = {"type": "object", "required": ["relation", "topics", "recent", "dates", "open_loops"], "properties": {
    "relation": {"type": "string", "maxLength": 60}, "circle": {"type": "string", "enum": list(CIRCLES) + [""]},
    "how_met": {"type": "string", "maxLength": 200}, "topics": {"type": "array", "maxItems": 6, "items": {"type": "string", "maxLength": 20}},
    "recent": {"type": "string", "maxLength": 300},
    "dates": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["date", "what"], "properties": {
        "date": {"type": "string"}, "what": {"type": "string", "maxLength": 40}}}},
    "open_loops": {"type": "array", "maxItems": 6, "items": {"type": "object", "required": ["kind", "date", "text", "quote"], "properties": {
        "kind": {"type": "string", "enum": ["she_promised", "they_asked"]}, "date": {"type": "string"},
        "text": {"type": "string", "maxLength": 80}, "quote": {"type": "string", "maxLength": 200}}}},
    "draft": {"type": "string", "maxLength": 200}}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(people)")}
    for col, ddl in COLUMNS:
        if col not in have:
            db.conn.execute(f"ALTER TABLE people ADD COLUMN {col} {ddl}")
    db.conn.execute("CREATE INDEX IF NOT EXISTS people_wx ON people (wx_key)")
    locks.ensure(db)
    db.conn.commit()


def is_service(name: str, extra=()) -> bool:
    """客服、中介、商家、酒店管家、签证代办、房产这类：不建卡。extra：她用 --people-skip 补的名字（包含就算）。"""
    n = str(name or "")
    return bool(SERVICE.search(n)) or any(x and x in n for x in extra)


def is_family(name: str) -> bool:
    return bool(FAMILY.search(str(name or "")))


def wx_key(username: str) -> str:
    """微信 username → 去重用的指纹（Mac 上算，服务器只见指纹）。"""
    return hashlib.sha256(("wx|" + username).encode()).hexdigest()[:24]


def auto_importance(name: str, st: dict) -> int:
    """重要程度默认按统计排（她改了就锁）：家人称呼直接 3；一年 500 条以上或 90 天 150 条以上 3；一年 50 条或 90 天 15 条 2；其余 1。"""
    if is_family(name):
        return 3
    n90, n365 = int(st.get("n90") or 0), int(st.get("n365") or 0)
    if n365 >= 500 or n90 >= 150:
        return 3
    if n365 >= 50 or n90 >= 15:
        return 2
    return 1


def frequency(st: dict) -> str:
    n90, n365 = int(st.get("n90") or 0), int(st.get("n365") or 0)
    if n90 >= 90:
        return "几乎天天聊"
    if n90 >= 26:
        return "每周都聊"
    if n90 >= 6:
        return "每月聊几次"
    if n365:
        return "偶尔聊"
    return "一年多没聊"


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def _deleted(db, key: str, name: str) -> bool:
    if key and locks.locked(db, "person", "wx:" + key, "deleted"):
        return True
    return name in locks.deleted_titles(db, "person")


def upsert(db, name: str, source: str = "", **fields):
    """建人物卡 / 改卡上的字段的唯一入口（修盘点 bug：删了的卡被聊天抽取、每晚整理、[人物] 标记建回来）。
    她删过这个人（删除锁）→ 不建、不改，返回 None；卡已在 → 她改过（锁住）的字段不动，其余照写。返回卡的 id。
    除这里、数据库层和初始化种子外，别处不直接调 db.upsert_person（tests/test_person_upsert.py 守卫）。"""
    ensure(db)
    name = " ".join(str(name or "").split())[:30]
    if not name:
        return None
    row = db.conn.execute("SELECT id, wx_key FROM people WHERE name = ?", (name,)).fetchone()
    if row is None and _deleted(db, "", name):
        return None
    if row is None and source:
        fields.setdefault("source", source)
    if row is not None:
        if row["wx_key"] and locks.locked(db, "person", "wx:" + row["wx_key"], "deleted"):
            return None
        fields = {k: v for k, v in fields.items() if not locks.locked(db, "person", row["id"], k)}
    return db.upsert_person(name, **fields)


def _clean_stats(p: dict) -> dict:
    st = {}
    last = str(p.get("last") or "")[:10]
    st["last"] = last if YMD.match(last) else None
    for k in ("n90", "n365", "total", "rounds"):
        try:
            st[k] = max(0, int(p.get(k) or 0))
        except (TypeError, ValueError):
            st[k] = 0
    for k in ("her_share", "her_starts"):
        try:
            st[k] = round(min(1.0, max(0.0, float(p.get(k) or 0))), 3)
        except (TypeError, ValueError):
            st[k] = 0.0
    st["groups"] = [" ".join(str(g).split())[:40] for g in (p.get("groups") or [])[:5] if str(g).strip()]
    return st


def ingest_stats(db, payload: dict, now: datetime = None) -> tuple:
    """收她 Mac 交来的统计。正文 {"people": [{"key", "name", "last", "n90", "n365", "total", "her_share", "her_starts", "rounds", "groups"}]}。
    只覆盖数字字段；名字、关系圈、重要程度她改过的不动。"""
    ensure(db)
    items = payload.get("people") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return 400, {"ok": False, "error": "要有 people 列表"}
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    got = {"ok": True, "new": 0, "updated": 0, "skipped": 0}
    for p in items[:5000]:
        if not isinstance(p, dict):
            continue
        key = str(p.get("key") or "").lower()
        name = " ".join(str(p.get("name") or "").split())[:30]
        if not HEX.match(key) or not name or is_service(name) or _deleted(db, key, name):
            got["skipped"] += 1
            continue
        st = _clean_stats(p)
        row = db.conn.execute("SELECT * FROM people WHERE wx_key = ?", (key,)).fetchone() \
            or db.conn.execute("SELECT * FROM people WHERE name = ? AND wx_key IS NULL", (name,)).fetchone()
        if row is None:
            card = name
            if db.conn.execute("SELECT 1 FROM people WHERE name = ?", (card,)).fetchone():
                card = f"{name}·{key[:4]}"
            pid = db._insert("people", name=card, wx_key=key, wx_name=name, source="wechat", updated_at=at)
            row = db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
            got["new"] += 1
        else:
            got["updated"] += 1
        pid = row["id"]
        fields = {"wx_key": key, "wx_name": name, "stats": json.dumps(st, ensure_ascii=False), "stats_at": at,
                  "frequency": frequency(st), "source": row["source"] or "wechat"}
        if st["last"] and (not row["last_contact"] or st["last"] > str(row["last_contact"])[:10]):
            fields["last_contact"] = st["last"]
        if not locks.locked(db, "person", pid, "importance"):
            fields["importance"] = auto_importance(name, st)
        if is_family(name) and not row["circle"] and not locks.locked(db, "person", pid, "circle"):
            fields["circle"] = "家人"
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.conn.execute(f"UPDATE people SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), at, pid))
    db.conn.commit()
    return 200, got


# ---------- 每晚整理 ----------
WX_TAG = re.compile(r"；wx：([0-9a-f]{12,64})(?:；|$)")


def _like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _records(db, wx_name: str, key: str = None) -> tuple:
    """这个人的微信交接记录（访谈原始记录，会话名“微信 · <名字>”）：最近的优先，最多 MAX_TEXT 字。返回 (原文, 用到的证据编号)。
    新的是整段对话（每行【她】/【对方名字】，ethan/wechatexport.py），旧的是问/答，两种都原样给模型（提示词里写了怎么认）。
    认人（Codex 评审 11-05 第 13 条）：记录头带了微信指纹（wx：…）的按指纹认；没带的按会话名精确认（LIKE 转义），
    同一个显示名字有两张卡时，没带指纹的旧记录分不清是谁的，谁都不给。已删除的记录不读。"""
    memusers.check("relations")
    session = f"微信 · {wx_name}"[:80]
    head = f"【访谈】会话：{session}；"
    rows = db.conn.execute("SELECT id, text FROM evidence WHERE source = 'interview' AND deleted_at IS NULL AND text IS NOT NULL "
                           "AND (text LIKE ? ESCAPE '\\' OR (? != '' AND instr(text, ?) > 0)) ORDER BY id DESC",
                           (_like(head) + "%", key or "", f"；wx：{key}" if key else "")).fetchall()
    shared = db.conn.execute("SELECT COUNT(*) FROM people WHERE wx_name = ?", (wx_name,)).fetchone()[0] > 1
    out, ids, n = [], [], 0
    for r in rows:
        first, _, body = r["text"].partition("\n")
        tag = WX_TAG.search(first)
        if tag:
            if not key or tag.group(1) != key:
                continue
        elif shared or not first.startswith(head):
            continue
        if n + len(body) > MAX_TEXT and out:
            break
        out.append(body[-MAX_TEXT:])
        ids.append(r["id"])
        n += len(body)
    return "\n\n".join(reversed(out)), sorted(ids)


def _transcript(db, wx_name: str, key: str = None) -> str:
    return _records(db, wx_name, key)[0]


def _day(s: str):
    """MM-DD（每年，2 月 29 日算数）或 YYYY-MM-DD：真解析，不存在的日子（02-30）返回 None。"""
    s = str(s or "")
    try:
        if YMD.match(s):
            return date.fromisoformat(s)
        if MMDD.match(s):
            return date(2000, int(s[:2]), int(s[3:]))
    except ValueError:
        return None
    return None


def _norm(s: str) -> str:
    import unicodedata
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(s or "")).lower())


def _lkey(s: str) -> str:
    """没了结的事的稳定编号：文本归一化（去空白标点、全半角、大小写）后哈希。"""
    return "h:" + hashlib.sha256(_norm(s).encode()).hexdigest()[:16]


def _done_keys(row) -> set:
    """她了结过的事（编号）；以前存的是原文，读的时候换成编号。"""
    return {x if str(x).startswith("h:") else _lkey(x) for x in _j(row["loops_done"], [])}


def _is_done(x: dict, done: set) -> bool:
    return _lkey(x.get("text")) in done or bool(x.get("quote") and _lkey(x["quote"]) in done)


def _check(data: dict, text: str) -> list:
    from .llm import quote_in
    errs = []
    for i, d in enumerate(data.get("dates") or []):
        if not (MMDD.match(d.get("date") or "") or YMD.match(d.get("date") or "")):
            errs.append(f"dates[{i}].date 要是 MM-DD（每年）或 YYYY-MM-DD（一次）")
    for i, x in enumerate(data.get("open_loops") or []):
        if not YMD.match(x.get("date") or ""):
            errs.append(f"open_loops[{i}].date 要是 YYYY-MM-DD")
        if not quote_in(x.get("quote") or "", text):
            errs.append(f"open_loops[{i}].quote 不在聊天记录里")
    return errs


def card_from_text(router, db, name: str, st: dict, text: str, ref: str = "") -> dict:
    """一个人的交接记录 → 人物卡草稿（校验过的 dict；两次都不合格返回 None）。题集 tests/evals/person_card.jsonl 用的也是这个。"""
    from . import llm
    version, system = llm.load_prompt("person_card")
    from . import style
    block = style.style_block(db, "relations", quotes=False) if db is not None else ""
    if block:
        system += "\n\n写 draft 时照着她的写作规范（按对方是朋友、长辈还是工作上的人选口吻）：\n" + block
    head = (f"这个人：{name}\n统计：最近一次联系 {st.get('last') or '不知道'}；最近 90 天 {st.get('n90', 0)} 条、一年 {st.get('n365', 0)} 条；"
            f"她说的占 {round(100 * float(st.get('her_share') or 0))}%；共同群：{'、'.join(st.get('groups') or []) or '没有'}\n\n聊天记录：\n")
    return llm.structured(router, db, "person_card", system, head + text, CARD_SCHEMA, check=lambda d: _check(d, text),
                          version=version, input_ref=ref, purpose="relations-card")


def _apply_card(db, row, data: dict, today: date, source_ids=()) -> list:
    """模型整理的结果写进卡：她改过的字段（锁）不动。返回写了哪些字段。
    查锁和写在一个事务里（拿写锁后重读这张卡）：她这时在卡上改，要么等这次写完，要么这次看到她的锁（Codex 评审 11-05）。"""
    with tx.atomic(db):
        row = db.conn.execute("SELECT * FROM people WHERE id = ?", (row["id"],)).fetchone()
        return _apply_card_now(db, row, data, today, source_ids) if row is not None else []


def _apply_card_now(db, row, data: dict, today: date, source_ids=()) -> list:
    pid, wrote = row["id"], []
    done = _done_keys(row)
    dates = [d for d in data.get("dates") or [] if _day(d.get("date"))]  # 不存在的日子（02-30）丢掉这一项，不让清单整个报错
    birthday = next((d["date"][-5:] for d in dates if "生日" in d.get("what", "")), None)
    loops = [x for x in data.get("open_loops") or [] if YMD.match(x.get("date") or "") and _day(x["date"])
             and not _is_done(x, done) and x["date"] <= today.isoformat()]
    fields = {"relation": data.get("relation") or None, "how_met": data.get("how_met") or None,
              "topics": json.dumps([t for t in data.get("topics") or [] if t.strip()], ensure_ascii=False),
              "recent": data.get("recent") or None, "draft": data.get("draft") or None,
              "dates": json.dumps(dates, ensure_ascii=False),
              "open_loops": json.dumps(loops, ensure_ascii=False)}
    if data.get("circle") and not row["circle"]:
        fields["circle"] = data["circle"]
    if birthday and MMDD.match(birthday):
        fields["birthday"] = birthday
    for k, v in fields.items():
        if locks.locked(db, "person", pid, k):
            continue
        if v is None and row[k]:  # 这次没提到的不清掉以前整理的
            continue
        db.conn.execute(f"UPDATE people SET {k} = ? WHERE id = ?", (v, pid))
        wrote.append(k)
    db.conn.execute("UPDATE people SET organized_at = ?, updated_at = ?, source_ids = ? WHERE id = ?",
                    (_now(), _now(), json.dumps(sorted(set(source_ids))), pid))
    db.conn.commit()
    return wrote


def forget_evidence(db, evidence_ids) -> list:
    """她忘掉某些记录（lifebase.forget 调用，在它的事务里，不单独提交）：用到这些记录的人物卡，整理出来的字段清空、
    标成待整理（下次只用剩下的记录重做）；她锁住的字段不动。返回清过的卡的编号（Codex 评审 11-05 第 9 条）。"""
    gone = {int(i) for i in evidence_ids}
    if not gone or not db.conn.execute("SELECT 1 FROM pragma_table_info('people') WHERE name = 'source_ids'").fetchone():
        return []
    held = {(r["obj"], r["field"]) for r in db.conn.execute(
        "SELECT obj, field FROM manual_locks WHERE obj LIKE 'person:%' AND released_at IS NULL")} \
        if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'manual_locks'").fetchone() else set()
    out = []
    for r in db.conn.execute("SELECT id, source_ids FROM people WHERE source_ids IS NOT NULL").fetchall():
        used = {int(i) for i in _j(r["source_ids"], [])}
        if not used & gone:
            continue
        sets = {k: ("[]" if k in ("topics", "dates", "open_loops") else None) for k in ORGANIZED if (f"person:{r['id']}", k) not in held}
        sets.update(source_ids=json.dumps(sorted(used - gone)), organized_at=None)
        db.conn.execute(f"UPDATE people SET {', '.join(k + ' = ?' for k in sets)} WHERE id = ?", (*sets.values(), r["id"]))
        out.append(r["id"])
    _scrub_deleted(db, gone)
    return out


def _unorganize(snap: dict, gone: set) -> bool:
    """删掉的卡留着的样子（恢复用）里，用到已忘掉记录的整理字段清空；改了返回 True。"""
    used = {int(i) for i in _j(snap.get("source_ids"), [])}
    if not used & gone:
        return False
    for k in ORGANIZED:
        snap[k] = "[]" if k in ("topics", "dates", "open_loops") else None
    snap.update(source_ids=json.dumps(sorted(used - gone)), organized_at=None)
    return True


def _scrub_deleted(db, gone: set):
    """她忘掉记录时，删掉的人留着的样子也一起清（不然恢复时会把忘掉的内容带回来）。"""
    if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'manual_locks'").fetchone():
        return
    for r in db.conn.execute("SELECT id, value FROM manual_locks WHERE obj LIKE 'person:%' AND field = 'deleted' "
                             "AND released_at IS NULL AND value IS NOT NULL").fetchall():
        snap = _j(r["value"], None)
        if isinstance(snap, dict) and _unorganize(snap, gone):
            db.conn.execute("UPDATE manual_locks SET value = ? WHERE id = ?", (json.dumps(snap, ensure_ascii=False), r["id"]))


def candidates(db, limit: int = PER_NIGHT) -> list:
    """今晚整理谁：还没卡的、最近联系多的优先；整理过、之后又聊过的排在后面。"""
    ensure(db)
    rows = [dict(r) for r in db.conn.execute(
        "SELECT * FROM people WHERE wx_name IS NOT NULL AND (organized_at IS NULL OR substr(organized_at, 1, 10) < last_contact)")]
    rows.sort(key=lambda r: (r["organized_at"] is not None, -int(_j(r["stats"], {}).get("n90") or 0), r["last_contact"] or ""), reverse=False)
    return rows[:limit]


def organize(db, router, today: date, limit: int = PER_NIGHT, stop=None) -> dict:
    """整理最多 limit 个人（不拿锁：每晚整理走 nightly，后台走 main，两处都先拿 LOCK）。没有交接记录的跳过（等她 Mac 上交过来）。
    stop：每个人开始前问一句，为真就停（有限批）。"""
    got = {"done": 0, "skipped": 0, "failed": 0, "names": []}
    if not allowed(db, "relations"):
        return got  # 记忆暂停中不读交接记录
    for r in candidates(db, limit * 3):
        if got["done"] + got["failed"] >= limit or (stop and stop()):
            break
        text, ids = _records(db, r["wx_name"], r["wx_key"])
        if not text.strip():
            got["skipped"] += 1
            continue
        data = card_from_text(router, db, r["name"], _j(r["stats"], {}), text, f"person:{r['id']}")
        if data is None:
            got["failed"] += 1
            continue
        row = db.conn.execute("SELECT * FROM people WHERE id = ?", (r["id"],)).fetchone()
        _apply_card(db, row, data, today, ids)
        got["done"] += 1
        got["names"].append(r["name"])
    return got


# ---------- 社交管理 ----------
def interval(p: dict) -> int:
    """提醒间隔：她设过的（contact_every_days）优先，没设按重要程度的默认。"""
    if p.get("contact_every_days"):
        return int(p["contact_every_days"])
    return DEFAULT_EVERY.get(int(p["importance"])) if p.get("importance") else None


def _topic(p: dict) -> str:
    topics = _j(p.get("topics"), [])
    if p.get("recent"):
        return f"问问近况：{p['recent'][:40]}"
    return f"可以聊聊{topics[0]}" if topics else ""


def _next_day(d: str, today: date):
    """MM-DD（每年）或 YYYY-MM-DD（一次）→ 今天往后最近的那天（过了的一次性日子返回 None）。"""
    if YMD.match(d):
        x = _day(d)
        return x if x and x >= today else None
    if not _day(d):
        return None
    m, dd = (int(v) for v in d.split("-"))
    for y in (today.year, today.year + 1):
        try:
            x = date(y, m, dd)
        except ValueError:
            x = date(y, 3, 1)  # 2 月 29 日在平年按 3 月 1 日算
        if x >= today:
            return x
    return None


def allowed(db, user: str) -> bool:
    """人物卡全是她的私密社交信息：对外的（合作方、公开两档，主页助手、主页）一律不给；记忆暂停中只给她本人和系统维护
    （同 onto_query._gate、portrait.chat_text；Codex 评审 11-05）。"""
    who = memusers.check(user)
    if memusers.external(user):
        return False
    return who.get("all") or not lifebase.on_hold(db)


def due(db, today: date, user: str = "relations") -> dict:
    """三张清单：该联系了、生日和重要日子（提前 7 天）、待回复（对方问了她没回，7 天内）。"""
    contact, days, reply = [], [], []
    if not allowed(db, user):
        return {"contact": contact, "days": days, "reply": reply}
    ensure(db)
    for r in db.conn.execute("SELECT * FROM people ORDER BY id"):
        p = dict(r)
        every = interval(p)
        if every and p["last_contact"]:
            try:
                gap = (today - date.fromisoformat(str(p["last_contact"])[:10])).days
            except ValueError:
                gap = None
            if gap is not None and gap >= every:
                contact.append({"id": p["id"], "name": p["name"], "days": gap, "every": every, "importance": p["importance"] or 0,
                                "topic": _topic(p), "draft": p["draft"] or ""})
        marks = [{"date": p["birthday"], "what": "生日"}] if p["birthday"] else []
        marks += [d for d in _j(p["dates"], []) if not (p["birthday"] and "生日" in d.get("what", ""))]
        for m in marks:
            x = _next_day(str(m.get("date") or ""), today)
            if x and (x - today).days <= SOON_DAYS:
                days.append({"id": p["id"], "name": p["name"], "date": x.isoformat(), "in": (x - today).days, "what": m.get("what") or ""})
        for x in _j(p["open_loops"], []):
            if x.get("kind") != "they_asked" or not YMD.match(x.get("date") or "") or not _day(x["date"]):
                continue
            ago = (today - date.fromisoformat(x["date"])).days
            if 0 <= ago <= REPLY_DAYS:
                reply.append({"id": p["id"], "name": p["name"], "date": x["date"], "text": x.get("text") or "", "ago": ago})
    contact.sort(key=lambda x: (-x["importance"], -x["days"] / x["every"]))
    days.sort(key=lambda x: x["in"])
    reply.sort(key=lambda x: x["ago"])
    return {"contact": contact, "days": days, "reply": reply}


def contact_status(db, today: date, user: str) -> dict:
    """“联系重要的人”这一项（领地页、属性分、Obsidian 看板共用，11-06 决定 5 起“该联系谁”只用人物卡这一套）：
    {"tracked": 有提醒间隔、有最近联系日期的人数, "due": 该联系了的那张清单, "days": 生日和重要日子}。
    提醒间隔她设过的优先，没设按重要程度默认（interval）；老的“只看手设间隔 + 生日”（db.people_to_contact）已下线。"""
    d = due(db, today, user)
    if not allowed(db, user):
        return {"tracked": 0, **d}
    ensure(db)
    n = sum(1 for r in db.conn.execute("SELECT contact_every_days, importance, last_contact FROM people")
            if r["last_contact"] and interval(dict(r)))
    return {"tracked": n, **d}


def week_line(db, today: date) -> dict:
    """周报用：这周该联系谁（最多 5 个）、这周的生日和重要日子、还没回的。"""
    d = due(db, today, user="review")
    return {"这周该联系谁": [f"{x['name']}（{x['days']} 天没联系{'，' + x['topic'] if x['topic'] else ''}）" for x in d["contact"][:5]],
            "生日和重要日子": [f"{x['name']} {x['date'][5:]} {x['what']}" for x in d["days"]],
            "对方问了她还没回": [f"{x['name']}：{x['text']}" for x in d["reply"]]}


def chat_hint(db, today: date, n: int = 2) -> str:
    """小伴 聊天时可以顺口提的一两个（她来聊时，别像播报）。"""
    d = due(db, today, user="ethan")
    lines = [f"- {x['name']}：{x['what']}，{'就是今天' if x['in'] == 0 else str(x['in']) + ' 天后'}" for x in d["days"][:n]]
    lines += [f"- {x['name']} {x['ago']} 天前问她：{x['text']}（她还没回）" for x in d["reply"][:n]]
    lines += [f"- {x['name']}：{x['days']} 天没联系了{'，' + x['topic'] if x['topic'] else ''}" for x in d["contact"][:n]]
    return "\n".join(lines[:n])


# ---------- 工作台“关系”页 ----------
def _card(p: dict, today: date, locked=()) -> dict:
    st = _j(p.get("stats"), {})
    every = interval(p)
    gap = None
    if p.get("last_contact"):
        try:
            gap = (today - date.fromisoformat(str(p["last_contact"])[:10])).days
        except ValueError:
            pass
    pid = p["id"]
    return {"id": pid, "name": p["name"], "circle": p.get("circle") or "", "relation": p.get("relation") or "",
            "how_met": p.get("how_met") or "", "notes": p.get("notes") or "", "birthday": p.get("birthday") or "",
            "recent": p.get("recent") or "", "topics": _j(p.get("topics"), []), "draft": p.get("draft") or "",
            "importance": p.get("importance") or 0, "every": every, "every_set": bool(p.get("contact_every_days")),
            "last_contact": p.get("last_contact") or "", "days": gap, "overdue": bool(every and gap is not None and gap >= every),
            "frequency": p.get("frequency") or "", "stats": st, "dates": _j(p.get("dates"), []),
            "open_loops": _j(p.get("open_loops"), []), "source": p.get("source") or "", "organized": bool(p.get("organized_at")),
            "locked": sorted(locked)}


def page(db, today: date, user: str = "me_page") -> dict:
    """工作台“关系”页：她本人看自己的人物卡（记忆暂停中也能看，同“我”页）。"""
    if not allowed(db, user):
        return {"due": due(db, today, user), "people": [], "defaults": DEFAULT_EVERY, "circles": list(CIRCLES), "deleted": []}
    ensure(db)
    held = {}
    for r in db.conn.execute("SELECT obj, field FROM manual_locks WHERE obj LIKE 'person:%' AND released_at IS NULL"):
        held.setdefault(r["obj"], set()).add(r["field"])
    people = [_card(dict(r), today, held.get(f"person:{r['id']}", ()))
              for r in db.conn.execute("SELECT * FROM people ORDER BY importance IS NULL, importance DESC, "
                                       "last_contact IS NULL, last_contact DESC, name")]
    return {"due": due(db, today, user), "people": people, "defaults": DEFAULT_EVERY, "circles": list(CIRCLES),
            "deleted": deleted_people(db)}


def set_field(db, pid: int, field: str, value) -> dict:
    """她在卡上改一个字段：改了就锁，自动流程以后不动这个字段。"""
    ensure(db)
    row = db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return {"ok": False, "msg": "这张卡不在了"}
    if field not in EDITABLE:
        return {"ok": False, "msg": "这一项不能改"}
    v = value
    if field == "importance":
        v = int(value)
        if v not in (1, 2, 3):
            return {"ok": False, "msg": "重要程度是 1–3"}
    elif field == "contact_every_days":
        v = int(value) if str(value).strip() else None
        if v is not None and not 1 <= v <= 3650:
            return {"ok": False, "msg": "提醒间隔是 1–3650 天"}
    elif field == "topics":
        v = json.dumps([t.strip() for t in re.split(r"[，、;；]", str(value or "")) if t.strip()][:6], ensure_ascii=False)
    else:
        v = " ".join(str(value or "").split())[:EDITABLE[field]] or None
        if field == "birthday" and v and not MMDD.match(v):
            return {"ok": False, "msg": "生日写成 MM-DD，比如 03-14"}
        if field == "name":
            if not v:
                return {"ok": False, "msg": "名字不能空"}
            if db.conn.execute("SELECT 1 FROM people WHERE name = ? AND id != ?", (v, pid)).fetchone():
                return {"ok": False, "msg": "已经有同名的卡了"}
        if field == "circle" and v and v not in CIRCLES:
            return {"ok": False, "msg": "关系圈从这几个里选：" + "、".join(CIRCLES)}
    with tx.atomic(db):  # 写值和锁一个事务：整理在中间插进来也看得到锁（Codex 评审 11-05）
        db.conn.execute(f"UPDATE people SET {field} = ?, updated_at = ? WHERE id = ?", (v, _now(), pid))
        locks.lock(db, "person", pid, field, value, title=row["name"])
    return {"ok": True, "msg": "改好了，以后自动整理不动这一项", "log": f"关系：{row['name']} 改了 {field}"}


def loop_done(db, pid: int, text: str) -> dict:
    """没了结的事她点“了结”：拿掉，以后整理也不再加回来。"""
    ensure(db)
    row = db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return {"ok": False, "msg": "这张卡不在了"}
    hit = [x for x in _j(row["open_loops"], []) if x.get("text") == text]
    loops = [x for x in _j(row["open_loops"], []) if x.get("text") != text]
    keys = [_lkey(text)] + [_lkey(x["quote"]) for x in hit if x.get("quote")]  # 按编号记（原话的编号也记，改写了说法也认得出）
    done = list(dict.fromkeys([k for k in _j(row["loops_done"], []) if k not in keys] + keys))
    done = [k if str(k).startswith("h:") else _lkey(k) for k in done][-MAX_DONE:]
    db.conn.execute("UPDATE people SET open_loops = ?, loops_done = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(loops, ensure_ascii=False), json.dumps(list(dict.fromkeys(done))), _now(), pid))
    db.conn.commit()
    return {"ok": True, "msg": "了结了", "log": f"关系：{row['name']} 了结一件事"}


def delete(db, pid: int) -> dict:
    """删卡：删了以后统计、聊天抽取、每晚整理、[人物] 标记都不再把这个人建回来（删除锁，按微信指纹和名字）。
    卡上的内容存在删除锁里（value），关系页“删掉的人”能原样恢复（4）。"""
    ensure(db)
    row = db.conn.execute("SELECT * FROM people WHERE id = ?", (pid,)).fetchone()
    if row is None:
        return {"ok": False, "msg": "这张卡不在了"}
    ref = "wx:" + row["wx_key"] if row["wx_key"] else "name:" + row["name"]
    snap = {k: row[k] for k in row.keys() if k != "id"}
    with tx.atomic(db):
        locks.lock(db, "person", ref, "deleted", value=snap, title=row["name"])
        if row["wx_name"] and row["wx_name"] != row["name"]:
            locks.lock(db, "person", "name:" + row["wx_name"], "deleted", title=row["wx_name"], note=ALIAS + ref)
        db.conn.execute("UPDATE manual_locks SET released_at = ? WHERE obj = ? AND released_at IS NULL", (_now(), f"person:{pid}"))
        db.conn.execute("DELETE FROM people WHERE id = ?", (pid,))
    return {"ok": True, "msg": f"删了 {row['name']} 的卡，以后不再自动建回来；要恢复去最下面“删掉的人”", "log": f"关系：删了 {row['name']} 的卡"}


ALIAS = "alias:"  # 删卡时按微信名字另记的那把删除锁，note 写主锁是哪个


def deleted_people(db) -> list:
    """关系页“删掉的人”：[{id 锁编号, name, at 删的时间, relation, kept 卡上内容留着没有}]，新删的在前。"""
    locks.ensure(db)
    out = []
    for r in db.conn.execute("SELECT id, title, value, at, note FROM manual_locks WHERE obj LIKE 'person:%' AND field = 'deleted' "
                             "AND released_at IS NULL ORDER BY id DESC"):
        if (r["note"] or "").startswith(ALIAS):
            continue
        snap = _j(r["value"], None)
        snap = snap if isinstance(snap, dict) else {}
        out.append({"id": r["id"], "name": snap.get("name") or r["title"] or "", "at": (r["at"] or "")[:10],
                    "relation": snap.get("relation") or snap.get("circle") or "", "kept": bool(snap)})
    return out


def restore(db, lock_id: int) -> dict:
    """她在“删掉的人”里点恢复：放开删除锁，卡按删的时候的样子建回来（11-06 以前删的没留内容，只建回名字，下次统计和整理补上）。
    建卡走 upsert 这一个入口；用到已忘掉记录的整理字段不带回来。"""
    locks.ensure(db)
    ensure(db)
    r = db.conn.execute("SELECT * FROM manual_locks WHERE id = ? AND obj LIKE 'person:%' AND field = 'deleted'", (lock_id,)).fetchone()
    if r is None or r["released_at"]:
        return {"ok": False, "msg": "这个人已经恢复过了"}
    snap = _j(r["value"], None)
    snap = dict(snap) if isinstance(snap, dict) else {}
    name = snap.pop("name", None) or r["title"]
    ref = r["obj"].split(":", 1)[1]
    if not snap and ref.startswith("wx:"):
        snap["wx_key"] = ref[3:]
    gone = {e[0] for e in db.conn.execute("SELECT id FROM evidence WHERE deleted_at IS NOT NULL")} \
        if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'evidence'").fetchone() else set()
    used = {int(i) for i in _j(snap.get("source_ids"), [])}
    if used:
        have = {e[0] for e in db.conn.execute(f"SELECT id FROM evidence WHERE id IN ({','.join('?' * len(used))})", tuple(used))} \
            if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'evidence'").fetchone() else set()
        _unorganize(snap, gone | (used - have))
    cols = {c[1] for c in db.conn.execute("PRAGMA table_info(people)")}
    fields = {k: v for k, v in snap.items() if k in cols and k not in ("id", "updated_at")}
    names = {name, snap.get("wx_name")} - {None, ""}
    with tx.atomic(db):
        db.conn.execute("UPDATE manual_locks SET released_at = ? WHERE released_at IS NULL AND field = 'deleted' AND obj LIKE 'person:%' "
                        f"AND (id = ? OR note = ? OR title IN ({','.join('?' * len(names))}))",
                        (_now(), lock_id, ALIAS + ref, *names))
        if db.conn.execute("SELECT 1 FROM people WHERE name = ?", (name,)).fetchone():
            return {"ok": True, "msg": f"已经有一张叫 {name} 的卡了，只是不再挡着自动建卡", "log": f"关系：恢复 {name}（已有同名卡）"}
        upsert(db, name, **fields)
    return {"ok": True, "msg": f"{name} 的卡恢复了", "log": f"关系：恢复了 {name} 的卡"}


# ---------- 统计一到就把所有人整理完 ----------
LOCK = ".relations-organize.lock"  # 后台 --organize-all 和每晚整理共用（Codex 评审 P2 第 10 条：以前每晚那边不拿锁，可能同时整理同一个人）


def lock(root):
    """拿到人物卡整理锁返回打开的锁文件（用完 close），别人在整理返回 None。"""
    from .bgwork import try_lock
    return try_lock(root, LOCK)


def nightly(store, router, today: date) -> dict:
    """每晚整理（ethan/nightly.py 调）：后台在整理就跳过，不和它同时整理同一个人。"""
    from .bgwork import paid_block
    b = paid_block(store.root)
    if b:  # 在用付费额度 / 订阅额度用完：大批整理不跑
        return {"skipped": b["reason"]}
    f = lock(store.root)
    if f is None:
        return {"skipped": "后台正在整理"}
    try:
        return organize(store.db, router, today)
    finally:
        f.close()


def kick_all(root, spawn=None) -> bool:
    from .bgwork import kick
    return bool(kick(root, "relations", ["--organize-all"], spawn))


def organize_all(db, router, today: date, batch: int = 20, stop=None, beat=None) -> dict:
    """一批一批整理，直到没有能整理的人（没有交接记录的跳过）。stop：有限批，到点停下标 more；beat：每批报一次进展。"""
    total = {"done": 0, "skipped": 0, "failed": 0, "names": []}
    while True:
        got = organize(db, router, today, limit=batch, stop=stop)
        for k in ("done", "skipped", "failed"):
            total[k] += got[k]
        total["names"] += got["names"]
        if got["done"] and beat:
            beat(f"人物卡：这一轮整理了 {total['done']} 个人")
        if got["done"] and stop and stop():
            total["more"] = True
            break
        if got["done"] == 0:
            break
    return total


def main(argv=None) -> int:
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser(description="人物卡整理（ethan/relations.py）")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--organize-all", action="store_true", help="把所有待整理的人一次整理完")
    p.add_argument("--max-minutes", type=float, default=0, help="这一轮最多跑几分钟（0 = 不设上限）")
    args = p.parse_args(argv)
    from .bgwork import gate
    if gate(args.data, "relations"):  # 在用付费额度 / 订阅额度用完：后台不跑
        return 0
    lockf = lock(args.data)
    if lockf is None:
        print("已经有一个在整理了")
        return 0
    from .cli import build_router
    from .clock import her_today
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        from .bgwork import deadline, heartbeat, patient  # 她在聊就让路、额度满了歇一会儿，不碰备用模型
        from .bgwork import retry_locked
        router = patient(build_router(args.data, 50.0), store)
        from .bgwork import QuotaBlocked, stop_or_blocked
        stop = stop_or_blocked(args.data, deadline(args.max_minutes))
        try:
            print(retry_locked(lambda: organize_all(store.db, router, her_today(), stop=stop, beat=lambda n: heartbeat(args.data, n))),
                  flush=True)
        except QuotaBlocked as e:  # 整理到一半发现在用付费额度：停（这个人下次再整理）
            print(f"[relations] 停了：{e}", flush=True)
    finally:
        store.close()
        lockf.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
