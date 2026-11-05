"""数字生命的地基（docs/design/digital-life-spec.md 第 1、2 节，2026-10-30 她认可）。

三层：证据（evidence，只追加）→ 事件（events，她经历了什么）→ 事实（facts，关于她这个人，可更新、作废不删）。
再加画像摘要（profile_blocks，每个维度一段，每轮聊天常驻）和公开规则（visibility_rules，她只设一次）。
这里只管“存”：表、写入函数、权限定级、把现有数据补登记进来。抽取（模型）在后面的步骤里做，写入都走这里的函数。

补登记：python3 -m ethan.lifebase --data <数据目录>（可重复跑，按 source_ref 去重；每晚整理时也会顺带跑一次）
"""

import argparse
import hashlib
import json
import os
import sqlite3
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import tx

DIMENSIONS = ("values", "temperament", "thinking", "abilities", "expression", "preferences",
              "body", "relationships", "resources", "rhythms")
DIM_NAMES = {"values": "价值与信念", "temperament": "性格与情绪", "thinking": "思维与决策", "abilities": "知识与技能",
             "expression": "表达风格", "preferences": "偏好与审美", "body": "身体", "relationships": "关系",
             "resources": "资源与处境", "rhythms": "时间与习惯"}
SOURCES = ("chat", "correction", "file", "link", "recording", "wechat", "social", "email", "bank", "health", "place",
           "computer", "workbench", "grok", "agent", "visitor", "interview", "sent")
LEVELS = ("private", "internal", "work", "public")  # 从严到宽
EMOTIONS = ("joy", "calm", "tired", "anxious", "sad", "angry", "excited", "proud", "mixed")
TERRITORIES = ("body", "career", "money", "social", "study", "academic", "life")
NEVER_PUBLIC = {"body", "resources", "relationships", "temperament"}  # 规格 2.11：永远 private，规则放宽不了
# 每晚整理的旧主题 → 新维度（迁移用；“其他”先放偏好、标 candidate，等每晚整理重新归类）
TOPIC_DIMS = {"身体与健康": "body", "饮食": "preferences", "作息与习惯": "rhythms", "事业与工作": "abilities",
              "钱": "resources", "学习与研究": "abilities", "人际与感情": "relationships", "家人": "relationships",
              "喜好与雷区": "preferences", "性格与想法": "temperament", "生活安排与地点": "resources", "其他": "preferences"}
# “关于她”页开头她自己写的部分：按小标题里的词猜维度（猜不到放偏好，每周复盘会再归类）
HEAD_REF = "wiki:about-mia:head"
HEAD_DIMS = (("身体|健康|运动|睡眠|过敏|饮食", "body"), ("性格|情绪|MBTI|星座", "temperament"), ("价值|信念|原则|人生观", "values"),
             ("思维|决策|想法", "thinking"), ("工作|事业|学习|技能|专业|学校|教育", "abilities"), ("说话|表达|语气|风格", "expression"),
             ("家人|朋友|感情|关系|伴侣", "relationships"), ("钱|收入|住|城市|地点|签证|处境|生活安排", "resources"),
             ("作息|习惯|时间|日程|节奏", "rhythms"), ("喜欢|偏好|审美|雷区|口味", "preferences"))


def _head_dim(heading: str) -> str:
    for pat, dim in HEAD_DIMS:
        if re.search(pat, heading or ""):
            return dim
    return "preferences"


def head_lines(body: str):
    """“关于她”开头（每晚整理那一节之前）她手写的每一句 → (维度, 句子)。长段落按句号拆开。"""
    from .nightly import SECTION
    heading, out = "", []
    for line in body.partition(SECTION)[0].splitlines():
        line = line.strip()
        if not line or line.startswith(("# ", "---", "|", "```")):
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        line = re.sub(r"^([-*+]|\d+[.、)])\s*", "", line)
        line = re.sub(r"\[\[([^\]|]+)(\|[^\]]+)?\]\]", r"\1", line).strip()
        for part in (re.findall(r"[^。！？!?；;]+[。！？!?；;]?", line) if len(line) > 60 else [line]):
            part = part.strip()
            if len(part) >= 2:
                out.append((_head_dim(heading), part))
    return out


def head_migrated(db) -> bool:
    """她手写的那部分已经迁成事实了：聊天和准备员不用再整页读“关于她”。"""
    try:
        return bool(db.conn.execute("SELECT 1 FROM evidence WHERE source_ref = ?", (HEAD_REF,)).fetchone())
    except Exception:
        return False


TOPIC_SLUGS = {"body": "身体与健康", "food": "饮食", "habits": "作息与习惯", "work": "事业与工作", "money": "钱",
               "study": "学习与研究", "people": "人际与感情", "family": "家人", "likes": "喜好与雷区",
               "mind": "性格与想法", "life": "生活安排与地点", "other": "其他"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, source_ref TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL, text TEXT, path TEXT, participants TEXT NOT NULL DEFAULT '[]',
    involves_others INTEGER NOT NULL DEFAULT 0, visibility TEXT NOT NULL DEFAULT 'private');
CREATE INDEX IF NOT EXISTS evidence_time ON evidence (occurred_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, occurred_at TEXT NOT NULL, ended_at TEXT, title TEXT NOT NULL, summary TEXT,
    place_id INTEGER, people TEXT NOT NULL DEFAULT '[]', territory TEXT, dims TEXT NOT NULL DEFAULT '[]',
    emotion TEXT, intensity INTEGER, importance INTEGER NOT NULL DEFAULT 3, evidence_ids TEXT NOT NULL,
    created_by TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'private');
CREATE INDEX IF NOT EXISTS events_time ON events (occurred_at);
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY, dimension TEXT NOT NULL, also_dims TEXT NOT NULL DEFAULT '[]', territory TEXT,
    subject TEXT NOT NULL DEFAULT 'self', statement TEXT NOT NULL, detail TEXT, tags TEXT NOT NULL DEFAULT '[]',
    confidence REAL NOT NULL DEFAULT 0.6, importance INTEGER NOT NULL DEFAULT 3, valid_from TEXT, valid_to TEXT,
    recorded_at TEXT NOT NULL, superseded_by INTEGER, evidence_ids TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active', last_confirmed_at TEXT, visibility TEXT NOT NULL DEFAULT 'private');
CREATE INDEX IF NOT EXISTS facts_dim ON facts (dimension, status);
CREATE TABLE IF NOT EXISTS profile_blocks (
    dimension TEXT PRIMARY KEY, summary TEXT NOT NULL DEFAULT '', fact_ids TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 0, updated_at TEXT);
CREATE TABLE IF NOT EXISTS profile_history (
    id INTEGER PRIMARY KEY, dimension TEXT NOT NULL, version INTEGER NOT NULL, summary TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS visibility_rules (
    id INTEGER PRIMARY KEY, match TEXT NOT NULL, level TEXT NOT NULL, auto INTEGER NOT NULL DEFAULT 1, note TEXT);
CREATE TABLE IF NOT EXISTS places (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, lat REAL, lon REAL, radius_m INTEGER);
"""
# 默认公开规则（规格 2.11）：match 是 JSON，键可以是 dimension / source / tag
DEFAULT_RULES = [
    ({"tag": "cv"}, "public", 1, "简历整理出的经历、能力"),
    ({"tag": "work"}, "public", 1, "作品、项目"),
    ({"tag": "public_view"}, "public", 0, "公开观点：进每周清单"),
    ({"dimension": "abilities"}, "work", 1, "能力可以给工作 agent"),
    ({"dimension": "expression"}, "work", 1, "表达风格可以给工作 agent"),
    ({"dimension": "thinking"}, "work", 1, "思维方式可以给工作 agent"),
]
TASK_COLUMNS = (("owner", "TEXT"), ("source_type", "TEXT"), ("source_ref", "TEXT"), ("status", "TEXT"),
                ("claimed_done_at", "TEXT"))
WISH_COLUMNS = (("category", "TEXT"), ("mention_count", "INTEGER NOT NULL DEFAULT 1"), ("last_mentioned_at", "TEXT"),
                ("quotes", "TEXT NOT NULL DEFAULT '[]'"), ("realizable", "INTEGER"), ("realized_event_id", "INTEGER"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# locked：她亲口纠正、确认过的事实，系统不能自己改，冲突时只能问她（docs/design/memory-alignment.md，她 2026-10-31 同意）
FACT_COLUMNS = (("locked", "INTEGER NOT NULL DEFAULT 0"),
                # 有效期（她 2026-10-31：“我的习惯可能是会变的，过去的不见得是错的”）：until 到哪天为止，until_cond 在什么条件下（city:London）
                ("until", "TEXT"), ("until_cond", "TEXT"),
                # ontology 第一批地基（docs/design/ontology.md 第 3、6.1、7.1 节，2026-11-02）：先加结构、不改现有行为
                ("kind", "TEXT NOT NULL DEFAULT 'fact'"),  # 知识类型：KINDS
                ("source_kind", "TEXT"),  # 来源类型：SOURCE_KINDS
                ("context", "TEXT"),  # 适用条件 / 语境（偏好的条件、自述的场合和情绪）
                ("read_by", "TEXT"),  # 读取与推理：JSON [{"user": 使用者, "purpose": 用途}]；空 = 按现有的级别规则
                ("disclose_to", "TEXT"),  # 披露：JSON [{"to": 对象, "text": 能说的那一版}]；空 = 按现有的级别规则
                ("root_evidence_ids", "TEXT"),  # 根证据（转述指回原始的，去重后），JSON
                ("derived_from", "TEXT"),  # 派生自哪些条目（摘要、反思、转述），JSON
                ("prop", "TEXT"))  # 具体属性（ontology 属性登记表的 key，比如 studied_at）；空 = 这个方面“总的”（ontology-plan 1.2，11-03）

EVIDENCE_COLUMNS = (("deleted_at", "TEXT"),)  # 她要求删除：内容清掉，只留编号和删除时间（ontology.md 7.3 节）
FORGET_SCHEMA = """
CREATE TABLE IF NOT EXISTS forgotten (fp TEXT PRIMARY KEY, source TEXT, at TEXT NOT NULL, allow_reimport INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS deletion_requests (id INTEGER PRIMARY KEY, scope TEXT NOT NULL, report TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS disclosures (
    id INTEGER PRIMARY KEY, text TEXT NOT NULL, to_users TEXT NOT NULL, dimension TEXT, quote TEXT NOT NULL,
    until TEXT, created_at TEXT NOT NULL, revoked_at TEXT);
CREATE TABLE IF NOT EXISTS disclosure_policy (tier TEXT NOT NULL, category TEXT NOT NULL, mode TEXT NOT NULL,
    updated_at TEXT NOT NULL, by TEXT NOT NULL, PRIMARY KEY (tier, category));
"""
DISCLOSURE_COLUMNS = (("once", "INTEGER NOT NULL DEFAULT 0"), ("used_at", "TEXT"), ("renew_at", "TEXT"), ("to_whom", "TEXT"),
                      ("sent_at", "TEXT"),  # used_at = 领取时间；sent_at = 确认发出去了
                      # 逐条例外（ontology-plan 3.3，11-04）：mode allow = 这句放行 / deny = 这句挡住；指着哪条说法（可以空，按原文对）
                      ("mode", "TEXT NOT NULL DEFAULT 'allow'"), ("statement_id", "INTEGER"), ("fact_id", "INTEGER"))
RENEW_DAYS = 30  # 长期的对外表达：30 天后在“待你定”问一次要不要续（她 2026-11-03 同意）
# 对外表达能说给谁（登记表里对外的使用者）
PUBLIC_USERS = ("twin", "site")
FORGOTTEN_FILE = "forgotten.jsonl"  # 指纹另存一份在数据库外：从旧备份恢复后，按它把删除重做一遍
# 再存一份在数据目录外（~/.config/ethan/，不随数据目录一起备份、回退）：整个数据目录退回旧备份时，最近的删除不丢（Codex 评审 11-03）
MIRROR_DIR = Path.home() / ".config" / "ethan"


def _log_paths(root) -> list:
    import hashlib
    key = hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]  # 按数据目录区分（恢复回同一个目录就对得上）
    return [Path(root) / FORGOTTEN_FILE, MIRROR_DIR / f"forgotten-{key}.jsonl"]


def _append_log(root, lines: list) -> int:
    """写到两处，返回写成了几处（0 = 都没写成，调用方要报出来）。"""
    saved = 0
    for path in _log_paths(root):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                for line in lines:
                    f.write(json.dumps(line) + "\n")
                f.flush()
                os.fsync(f.fileno())
            saved += 1
        except OSError:
            continue
    return saved


class Forgotten(ValueError):
    """这条来源她要求删掉了：不再导入、不再生成知识（她明确选“重新导入”才放开）。"""


# 知识类型（ontology.md 第 3 节）：系统推断永远是 hypothesis，不会因为被引用多了就变成 fact
KINDS = ("fact", "self_report", "goal", "preference", "hypothesis", "prediction", "decision", "disclosure")
# 来源类型（ontology.md 8.3 节）
SOURCE_KINDS = ("her_words", "her_confirmation", "her_provided", "behavior", "other_person", "system_inference", "agent_relay")
# her_provided：她提供的资料（她发来的文件、图片、链接）——不自动等于她的原话：合同可能是别人写的，简历可能已经过时（Codex 评审 11-03）
# 证据来源 → 来源类型（回填老数据、新条目没写时用）
SOURCE_KIND_OF = {"interview": "her_words",  # 她在别的会话里亲口说的（她 2026-11-03 定算原话）
                  "chat": "her_words", "correction": "her_confirmation", "workbench": "her_words", "file": "her_provided",
                  "recording": "her_words", "wechat": "her_words", "social": "her_words", "link": "her_provided",
                  "email": "other_person", "visitor": "other_person",
                  "sent": "her_words",  # 她自己发出去的邮件（Gmail“已发送”，引用的别人的话去掉了；ontology-plan 1.6）
                  "bank": "behavior", "health": "behavior", "place": "behavior", "computer": "behavior",
                  "grok": "agent_relay", "agent": "agent_relay"}


def ensure(db):
    if tx.once(db, "lifebase"):  # 建表、加列每个连接只做一次：executescript 会顺手提交，动作里面不能再跑（tx.py）
        db.conn.executescript(SCHEMA)
        db.conn.executescript(FORGET_SCHEMA)
        for table, cols in (("tasks", TASK_COLUMNS), ("wishes", WISH_COLUMNS), ("facts", FACT_COLUMNS), ("evidence", EVIDENCE_COLUMNS),
                            ("disclosures", DISCLOSURE_COLUMNS)):
            have = {r[1] for r in db.conn.execute(f"PRAGMA table_info({table})")}
            for col, ddl in cols:
                if col not in have:
                    db.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        from . import ontology
        ontology.ensure(db)  # 新骨架的表（迁移第 1 步）
        if not db.conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name = 'facts_mirror_ins_v4'").fetchone():
            db.conn.executescript("DROP TRIGGER IF EXISTS facts_mirror_ins; DROP TRIGGER IF EXISTS facts_mirror_upd; "
                                  "DROP TRIGGER IF EXISTS facts_mirror_ins_v2; DROP TRIGGER IF EXISTS facts_mirror_upd_v2; "
                                  "DROP TRIGGER IF EXISTS facts_mirror_ins_v3; DROP TRIGGER IF EXISTS facts_mirror_upd_v3;" + MIRROR_SQL)  # 新旧双写
            # v3 起说法也带权限（可见级别、读取名单、她锁定的）：已经迁过的补上
            db.conn.execute("UPDATE statements SET visibility = (SELECT f.visibility FROM fact_map m JOIN facts f ON f.id = m.fact_id "
                            "WHERE m.statement_id = statements.id), read_by = (SELECT f.read_by FROM fact_map m JOIN facts f ON f.id = m.fact_id "
                            "WHERE m.statement_id = statements.id), locked = COALESCE((SELECT f.locked FROM fact_map m JOIN facts f "
                            "ON f.id = m.fact_id WHERE m.statement_id = statements.id), 0)")
            tx.commit(db)
    # 新列补默认值：谁做（取代 promised_by_ethan）、状态（和 done 对齐）
    db.conn.execute("UPDATE tasks SET owner = CASE WHEN promised_by_ethan = 1 THEN 'ethan' ELSE 'mia' END WHERE owner IS NULL")
    db.conn.execute("UPDATE tasks SET status = CASE WHEN done = 1 THEN 'done' ELSE 'open' END WHERE status IS NULL")
    db.conn.execute("UPDATE wishes SET last_mentioned_at = created_at WHERE last_mentioned_at IS NULL")
    _backfill_kinds(db)
    if not db.conn.execute("SELECT 1 FROM visibility_rules LIMIT 1").fetchone():
        db.conn.executemany("INSERT INTO visibility_rules (match, level, auto, note) VALUES (?, ?, ?, ?)",
                            [(json.dumps(m, ensure_ascii=False), lvl, auto, note) for m, lvl, auto, note in DEFAULT_RULES])
    tx.commit(db)


def level_for(db, dimension: str = None, source: str = None, tags=()) -> str:
    """按公开规则定级（只用 auto=1 的规则）；永远私密的维度和来源，规则放宽不了。没有规则命中就是 internal。"""
    if dimension in NEVER_PUBLIC or source in ("bank", "health", "wechat", "recording", "email", "place"):
        return "private"
    best = "internal"
    for r in db.conn.execute("SELECT match, level FROM visibility_rules WHERE auto = 1"):
        m = json.loads(r["match"])
        if (m.get("dimension") and m["dimension"] != dimension) or (m.get("source") and m["source"] != source) \
                or (m.get("tag") and m["tag"] not in tags):
            continue
        if LEVELS.index(r["level"]) > LEVELS.index(best):
            best = r["level"]
    return best


# ---------- 写入（后面的抽取步骤都走这里） ----------
def add_evidence(db, source: str, source_ref: str, occurred_at: str, text: str = None, path: str = None,
                 participants=(), involves_others: bool = False, visibility: str = "private") -> int:
    """登记一条证据；source_ref 已经有就返回原来的编号，不重复。"""
    if source not in SOURCES:
        raise ValueError(f"不认识的来源：{source}")
    if visibility not in LEVELS:
        raise ValueError(f"不认识的级别：{visibility}")
    if _forgotten(db, source, source_ref):
        raise Forgotten(f"这条来源她要求删掉了，不再导入：{source}")
    row = db.conn.execute("SELECT * FROM evidence WHERE source_ref = ?", (source_ref,)).fetchone()
    if row and "deleted_at" in row.keys() and row["deleted_at"]:
        # 她选了“重新导入”（指纹放开了）：把内容放回去、去掉删除标记，让检索重新收它
        db.conn.execute("UPDATE evidence SET text = ?, path = ?, participants = ?, occurred_at = ?, deleted_at = NULL WHERE id = ?",
                        (text, path, json.dumps(list(participants), ensure_ascii=False), occurred_at, row["id"]))
        try:
            db.conn.execute("UPDATE mem_state SET last_id = MIN(last_id, ?) WHERE source = 'evidence'", (row["id"] - 1,))
        except sqlite3.OperationalError:
            pass
        return row["id"]
    if row:
        return row["id"]
    cur = db.conn.execute(
        "INSERT INTO evidence (source, source_ref, occurred_at, recorded_at, text, path, participants, involves_others, visibility) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, source_ref, occurred_at, _now(), text, path, json.dumps(list(participants), ensure_ascii=False),
         1 if involves_others else 0, visibility))
    return cur.lastrowid


def _check_evidence(db, ids):
    ids = [int(i) for i in ids]
    if not ids:
        raise ValueError("至少要有一条证据")
    rows = db.conn.execute(f"SELECT * FROM evidence WHERE id IN ({','.join('?' * len(ids))})", ids).fetchall()
    found = {r["id"] for r in rows}
    missing = set(ids) - found
    if missing:
        raise ValueError(f"证据不存在：{sorted(missing)}")
    gone = {r["id"] for r in rows if "deleted_at" in r.keys() and r["deleted_at"]}
    if gone and gone == set(ids):
        raise Forgotten("这些证据她要求删掉了，不能再拿来生成知识")
    return [i for i in ids if i not in gone]


def add_event(db, occurred_at: str, title: str, evidence_ids, created_by: str, summary: str = "", ended_at: str = None,
              people=(), territory: str = None, dims=(), emotion: str = None, intensity: int = None,
              importance: int = 3, place_id: int = None, visibility: str = "private") -> int:
    ids = _check_evidence(db, evidence_ids)
    if created_by not in ("realtime", "nightly", "manual"):
        raise ValueError("created_by 只能是 realtime / nightly / manual")
    if emotion is not None and emotion not in EMOTIONS:
        raise ValueError(f"不认识的情绪：{emotion}")
    if territory is not None and territory not in TERRITORIES:
        raise ValueError(f"不认识的领域：{territory}")
    bad = [d for d in dims if d not in DIMENSIONS]
    if bad:
        raise ValueError(f"不认识的维度：{bad}")
    cur = db.conn.execute(
        "INSERT INTO events (occurred_at, ended_at, title, summary, place_id, people, territory, dims, emotion, intensity, "
        "importance, evidence_ids, created_by, visibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (occurred_at, ended_at, title.strip()[:30], (summary or "")[:300], place_id, json.dumps(list(people), ensure_ascii=False),
         territory, json.dumps(list(dims)), emotion, intensity, max(1, min(5, int(importance))), json.dumps(ids), created_by, visibility))
    return cur.lastrowid


def add_fact(db, dimension: str, statement: str, evidence_ids, subject: str = "self", also_dims=(), territory: str = None,
             detail: str = None, tags=(), confidence: float = 0.6, importance: int = 3, valid_from: str = None,
             status: str = "active", visibility: str = None, kind: str = "fact", source_kind: str = None,
             context: str = None, prop: str = None) -> int:
    if dimension not in DIMENSIONS:
        raise ValueError(f"不认识的维度：{dimension}")
    also = [d for d in also_dims if d in DIMENSIONS and d != dimension][:3]
    ids = _check_evidence(db, evidence_ids)
    if status not in ("active", "candidate", "invalid"):
        raise ValueError("status 只能是 active / candidate / invalid")
    if not (subject == "self" or re.fullmatch(r"person:\d+", subject)):
        raise ValueError("subject 只能是 self 或 person:<编号>")
    vis = visibility or level_for(db, dimension, None, tags)
    if subject != "self":
        vis = "private"  # 别人的事永远私密
    ensure_columns = {r[1] for r in db.conn.execute("PRAGMA table_info(facts)")}
    sk = source_kind or source_kind_for(db, ids)
    if kind != "fact" or source_kind:  # 显式指定了类型或来源的才校验；老的调用方（都按事实记她的话和记录）照旧
        check_kind(kind, sk)
    from . import ontology
    ontology.object_for_ref(db, subject)  # 主语对应的对象先有（双写触发器靠它），跟着这次事务
    now = _now()
    cur = db.conn.execute(
        "INSERT INTO facts (dimension, also_dims, territory, subject, statement, detail, tags, confidence, importance, valid_from, "
        "recorded_at, evidence_ids, status, last_confirmed_at, visibility) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (dimension, json.dumps(also), territory, subject, statement.strip()[:60], detail, json.dumps(list(tags), ensure_ascii=False),
         max(0.0, min(1.0, float(confidence))), max(1, min(5, int(importance))), valid_from, now, json.dumps(ids), status, now, vis))
    if "kind" in ensure_columns:
        db.conn.execute("UPDATE facts SET kind = ?, source_kind = ?, context = ?, root_evidence_ids = ?, prop = ? WHERE id = ?",
                        (kind, sk, context, json.dumps(sorted(set(ids))), prop, cur.lastrowid))
    return cur.lastrowid


def source_kind_for(db, evidence_ids) -> str:
    """一条条目的来源类型：看它的证据从哪来。证据里有她本人说的就算她说的；全是转述就是转述；其余取第一条的。"""
    kinds = []
    for i in evidence_ids or []:
        r = db.conn.execute("SELECT source FROM evidence WHERE id = ?", (int(i),)).fetchone()
        if r:
            kinds.append(SOURCE_KIND_OF.get(r["source"], "other_person"))
    for k in ("her_confirmation", "her_words"):
        if k in kinds:
            return k
    return kinds[0] if kinds else "system_inference"


def _backfill_kinds(db):
    """老数据补来源类型和根证据（一次性，只补空的）。知识类型老数据一律算 fact（现有的都是从她的话和记录里抽的事实）。"""
    rows = db.conn.execute("SELECT id, evidence_ids FROM facts WHERE source_kind IS NULL").fetchall()
    for r in rows:
        ids = json.loads(r["evidence_ids"] or "[]")
        db.conn.execute("UPDATE facts SET source_kind = ?, root_evidence_ids = ? WHERE id = ?",
                        (source_kind_for(db, ids), json.dumps(sorted(set(ids))), r["id"]))
    if rows:
        tx.commit(db)


def check_kind(kind: str, source_kind: str):
    """类型和来源对不上就拒绝（ontology.md 第 10 节第 2 步）：系统推断、agent 转述不能写成关于她的事实或偏好。"""
    if kind not in KINDS:
        raise ValueError(f"不认识的知识类型：{kind}")
    if source_kind and source_kind not in SOURCE_KINDS:
        raise ValueError(f"不认识的来源类型：{source_kind}")
    if source_kind in ("system_inference", "agent_relay") and kind in ("fact", "preference", "self_report", "decision", "disclosure"):
        raise ValueError("系统推断和转述只能记成假设，不能写成关于她的事实、偏好、自述、决定或对外表达")


def _locked(db, fact_id) -> bool:
    try:
        r = db.conn.execute("SELECT locked FROM facts WHERE id = ?", (fact_id,)).fetchone()
    except Exception:
        return False
    return bool(r and r["locked"])


def ask_locked(db, fact_id: int, heard: str):
    """她锁定的事实可能不成立了：不改，进“待你定”等她定（小伴 聊天时也会找机会问，2026-10-31 两套“问她”合一）。"""
    r = db.conn.execute("SELECT statement FROM facts WHERE id = ?", (fact_id,)).fetchone()
    from . import mepage
    mepage.add_question(db, f"你之前说过“{r['statement'] if r else ''}”，最近听到“{heard}”，现在还对吗？", fact_id, commit=False)


def supersede(db, old_id: int, new_id: int, when: str = None, force: bool = False) -> bool:
    """旧事实被新事实取代：旧的填失效时间和取代者，不删。她锁定的不动（force 只给她本人的纠错用），返回 False 并去问她。"""
    if not force and _locked(db, old_id):
        db.conn.execute("UPDATE facts SET status = 'candidate' WHERE id = ?", (new_id,))
        try:  # 工作台“我”页的“待你定”里也列出来，两条让她选（ethan/mepage.py）
            from . import mepage
            mepage.add_conflict(db, old_id, new_id)
        except Exception as e:
            print(f"[lifebase] 待你定没加上：{type(e).__name__} {e}", flush=True)
        return False
    db.conn.execute("UPDATE facts SET valid_to = ?, superseded_by = ? WHERE id = ? AND valid_to IS NULL",
                    (when or _now(), new_id, old_id))
    return True


def invalidate(db, fact_id: int, when: str = None, force: bool = False) -> bool:
    if not force and _locked(db, fact_id):
        ask_locked(db, fact_id, "这条可能不成立了")
        return False
    db.conn.execute("UPDATE facts SET valid_to = ?, status = 'invalid' WHERE id = ?", (when or _now(), fact_id))
    return True


def current_facts(db, dimension: str = None, max_level: str = "private") -> list:
    """现在仍成立的事实；max_level 是这个场景能看到的最宽级别（规格 5.2）。"""
    allowed = LEVELS[LEVELS.index(max_level):] if max_level != "private" else LEVELS
    q = "SELECT * FROM facts WHERE valid_to IS NULL AND status NOT IN ('invalid', 'deleted', 'suspended') AND visibility IN (%s)" % ",".join("?" * len(allowed))
    args = list(allowed)
    if dimension:
        q += " AND (dimension = ? OR also_dims LIKE ?)"
        args += [dimension, f'%"{dimension}"%']
    return [dict(r) for r in db.conn.execute(q + " ORDER BY importance DESC, id", args)]


# ---------- 补登记现有数据 ----------
def backfill(store) -> dict:
    """把已有的聊天、文件、交易、Grok 回报登记成证据，把每晚整理的事实迁进事实表。可重复跑。"""
    db, root = store.db, Path(store.root)
    ensure(db)
    got = {"chat": 0, "file": 0, "bank": 0, "grok": 0, "facts": 0}
    before = db.conn.execute("SELECT COUNT(*) n FROM evidence").fetchone()["n"]
    for path in sorted((root / "raw").glob("*.jsonl")):
        for i, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
            if not line.strip():
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("role") not in ("mia", "ethan") or not r.get("text"):
                continue
            try_evidence(db, "chat", f"raw:{path.stem}:{i}", r.get("at") or path.stem, f"{r['role']}：{r['text']}",
                         path=f"raw/{path.name}")
    got["chat"] = db.conn.execute("SELECT COUNT(*) n FROM evidence WHERE source = 'chat'").fetchone()["n"]
    try:
        rows = db.conn.execute("SELECT ref, title, summary, extra, happened_at FROM brain_items WHERE kind = 'file'").fetchall()
    except Exception:
        rows = []
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        text_path = root / extra.get("text_path", "")
        text = text_path.read_text(encoding="utf-8") if extra.get("text_path") and text_path.is_file() else r["summary"]
        try_evidence(db, "file", r["ref"], r["happened_at"] or _now(), text, path=extra.get("path"))
    got["file"] = len(rows)
    try:
        txs = db.conn.execute("SELECT id, source, ext_id, at, amount, currency, merchant FROM transactions").fetchall()
    except Exception:
        txs = []
    for t in txs:
        try_evidence(db, "bank", f"tx:{t['ext_id'] or t['id']}", t["at"],
                     f"{t['source']} {t['amount']} {t['currency']} {t['merchant'] or ''}".strip())
    got["bank"] = len(txs)
    for path in sorted((root / "inbox").glob("*.jsonl")) if (root / "inbox").exists() else []:
        for i, line in enumerate(path.read_text(encoding="utf-8").split("\n")):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            src = "grok" if "grok" in f"{e.get('label', '')}{e.get('path', '')}".lower() else "agent"
            try_evidence(db, src, f"inbox:{path.stem}:{i}", e.get("at") or path.stem, e.get("text"), path=f"inbox/{path.name}")
            got["grok"] += 1
    # 以前每晚整理记下的事实 → 事实表。三处来源（2026-10-30 线上发现只读详情页一条都没迁到：详情页是当天才加的）：
    # 1. 每天的记忆页 journal/<日期>：“新知道的：”下面每行“- 【主题】事实”，日期就是页名；
    # 2. 详情页 about/<主题>：每行“- （日期）事实”；
    # 3. about-mia 的“小伴 每晚整理”一节：### 主题 下面每行“- （日期）事实”或没有日期的“- 事实”。
    from .core.agent import find_similar
    wiki = store.wiki
    found = []  # (主题, 事实, 日期, 来源页)
    for page in wiki.pages():
        m = re.fullmatch(r"journal/(\d{4}-\d{2}-\d{2})", page)
        if not m:
            continue
        body, on = wiki.read(page)["body"], False
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("新知道的"):
                on = True
            elif on and line.startswith("- "):
                t = re.match(r"-\s*【([^】]+)】(.+)", line)
                found.append((t.group(1), t.group(2).strip(), m.group(1), page) if t else ("其他", line[2:].strip(), m.group(1), page))
            elif on and line:
                on = False
    for slug, topic in TOPIC_SLUGS.items():
        page = f"about/{slug}"
        if wiki.exists(page):
            for line in wiki.read(page)["body"].splitlines():
                t = re.match(r"-\s*（(\d{4}-\d{2}-\d{2})）(.+)", line.strip())
                if t:
                    found.append((topic, t.group(2).strip(), t.group(1), page))
    if wiki.exists("about-mia"):
        from .nightly import SECTION
        topic = "其他"
        for line in wiki.read("about-mia")["body"].partition(SECTION)[2].splitlines():
            line = line.strip()
            if line.startswith("### "):
                topic = line[4:].strip()
            elif line.startswith("- "):
                t = re.match(r"-\s*（(\d{4}-\d{2}-\d{2})）(.+)", line)
                found.append((topic, t.group(2).strip(), t.group(1), "about-mia") if t else (topic, line[2:].strip(), None, "about-mia"))
    have = {}  # 维度 → 已有的说法（去重：一字不差或意思重复都只记一次）
    for r in db.conn.execute("SELECT dimension, statement FROM facts"):
        have.setdefault(r["dimension"], []).append(r["statement"])
    pages = {}
    for topic, fact, day, page in found:
        fact = re.sub(r"\s+", " ", fact)[:60]
        dim = TOPIC_DIMS.get(topic, "preferences")
        if not fact or find_similar(fact, have.get(dim, [])):
            continue
        if page not in pages:
            pages[page] = try_evidence(db, "chat", f"wiki:{page}", day or _now(), wiki.read(page)["body"], path=f"wiki/{page}.md")
        if pages[page] is None:  # 这页她要求删掉过
            continue
        add_fact(db, dim, fact, [pages[page]], valid_from=day,
                 status="active" if topic in TOPIC_DIMS and topic != "其他" else "candidate")
        have.setdefault(dim, []).append(fact)
        got["facts"] += 1
    # 4. 开头她自己写的（2026-10-31 收口：以后聊天和准备员只读事实和画像，不再整页读这页）。
    #    她写的当真（信心 1.0），但不锁：分维度是猜的，每周复盘可以再归类；她在 Obsidian 改了新句子，下次照样迁。
    if wiki.exists("about-mia"):
        body = wiki.read("about-mia")["body"]
        lines = head_lines(body)
        if lines:
            ev = try_evidence(db, "chat", HEAD_REF, _now(), body.partition(SECTION)[0].strip(), path="wiki/about-mia.md")
            for dim, text in (lines if ev else []):
                if any(find_similar(text, v) for v in have.values()):
                    continue
                add_fact(db, dim, text, [ev], confidence=1.0, detail=text if len(text) > 60 else None)
                have.setdefault(dim, []).append(text)
                got["facts"] += 1
    tx.commit(db)
    got["workbench"] = backfill_handwritten(db)
    got["new_evidence"] = db.conn.execute("SELECT COUNT(*) n FROM evidence").fetchone()["n"] - before
    return got


def backfill_handwritten(db) -> int:
    """她在工作台上亲手写的（ontology-plan 1.5，S17）登记成证据（来源“工作台”，算她的原话），每晚的访谈整理会拆成说法：
    - 她自己存过的日记（source = 'her'；系统自动写的初稿不算她的话）；改了再存是新的一份，同一份不重复；
    - 今日卡上她写的心愿。
    人物卡、心愿单是系统从聊天里摘的，聊天本身已经进记忆，不重复登记。"""
    import hashlib
    n = 0
    try:
        rows = db.conn.execute("SELECT day, body FROM diary WHERE source = 'her' AND TRIM(body) != ''").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        h = hashlib.sha1(r["body"].encode("utf-8")).hexdigest()[:12]
        n += 1 if try_evidence(db, "workbench", f"diary:{r['day']}:{h}", r["day"], r["body"]) else 0
    try:
        rows = db.conn.execute("SELECT id, text, created_at FROM card_wishes WHERE TRIM(text) != ''").fetchall()
    except sqlite3.OperationalError:
        rows = []
    for r in rows:
        n += 1 if try_evidence(db, "workbench", f"wish:{r['id']}", r["created_at"], r["text"]) else 0
    return n


# ---------- 删除（她要求忘掉）：ontology.md 7.3 节 ----------
def fingerprint(source: str, source_ref: str) -> str:
    """不含原文的指纹：来源 + 来源编号的单向哈希。"""
    import hashlib
    return hashlib.sha256(f"{source}|{source_ref}".encode()).hexdigest()[:32]


def _forgotten(db, source: str, source_ref: str) -> bool:
    try:  # 不用 executescript 建表：它会顺手提交调用方没提交的事务
        r = db.conn.execute("SELECT allow_reimport FROM forgotten WHERE fp = ?", (fingerprint(source, source_ref),)).fetchone()
    except sqlite3.OperationalError:
        return False
    return bool(r) and not r["allow_reimport"]


is_forgotten = _forgotten


def try_evidence(db, source: str, source_ref: str, *a, **kw):
    """同 add_evidence，但她删掉过的来源返回 None（各导入入口用它，删过的不复活）。"""
    try:
        return add_evidence(db, source, source_ref, *a, **kw)
    except Forgotten:
        return None


def allow_reimport(db, source: str, source_ref: str, root=None) -> bool:
    """她明确选“重新导入”：放开这条来源。"""
    ensure(db)
    fp = fingerprint(source, source_ref)
    n = db.conn.execute("UPDATE forgotten SET allow_reimport = 1 WHERE fp = ?", (fp,)).rowcount
    if root:
        _append_log(root, [{"fp": fp, "allow": 1, "at": _now()}])
    tx.commit(db)
    return n > 0


def forget(db, evidence_ids=(), fact_ids=(), root=None) -> dict:
    """删除她点名的证据和条目，连带处理（ontology.md 7.3 节、ontology-contracts.md 1.8 节）。只能在她勾选确认后调用。
    返回删除报告（只有编号和数量，不含内容）。
    - 先把删除记录写到库外两处（数据目录里、目录外），再改数据库：中途断掉，恢复时也会按记录重做；
      两处都没写成，报告里 log_saved = 0，每晚体检报出来（不静默当成完成）。
    - 证据：内容清掉、只留编号和删除时间，记不含原文的指纹（以后同步进来直接跳过）；
    - 只靠这些证据撑着的条目一起删；**还有别的证据撑着的，先停用（suspended）**，等她或每周复盘看剩下的依据还够不够，
      不直接保留原话（原话可能就是从删掉的那条来的，Codex 评审 11-03 复现过）；
    - 派生自被删 / 停用条目的条目也停用；用到它们的画像摘要清空等重做，画像历史里系统写的旧版本一起删；
    - 检索索引里的副本删掉；她自己写的原件（日记、批注）不动，只在报告里提醒她去看。"""
    ensure(db)
    now = _now()
    ev = {int(i) for i in evidence_ids}
    report = {"evidence": [], "facts_deleted": [], "facts_suspended": [], "events_deleted": [], "profile_cleared": [],
              "history_removed": 0, "index_removed": 0}
    rows = db.conn.execute(f"SELECT id, source, source_ref FROM evidence WHERE id IN ({','.join('?' * len(ev)) or 'NULL'})", list(ev)).fetchall()
    lines = [{"fp": fingerprint(r["source"], r["source_ref"]), "source": r["source"], "allow": 0, "at": now} for r in rows]
    lines += [{"fact": int(i), "at": now} for i in fact_ids]  # 只删条目的也记下（恢复后条目编号不变，按编号重做）
    report["log_saved"] = _append_log(root, lines) if root and lines else (None if not root else 0)
    for r, line in zip(rows, lines):
        db.conn.execute("INSERT OR REPLACE INTO forgotten (fp, source, at, allow_reimport) VALUES (?, ?, ?, 0)", (line["fp"], r["source"], now))
        db.conn.execute("UPDATE evidence SET text = NULL, path = NULL, participants = '[]', deleted_at = ? WHERE id = ?", (now, r["id"]))
        report["evidence"].append(r["id"])
    dead = set(int(i) for i in fact_ids)
    paused = set()
    for f in db.conn.execute("SELECT id, evidence_ids FROM facts WHERE status != 'deleted'").fetchall():
        ids = set(json.loads(f["evidence_ids"] or "[]"))
        if not ids & ev or f["id"] in dead:
            continue
        left = ids - ev
        if left:
            db.conn.execute("UPDATE facts SET evidence_ids = ?, root_evidence_ids = ?, status = 'suspended' WHERE id = ?",
                            (json.dumps(sorted(left)), json.dumps(sorted(left)), f["id"]))
            paused.add(f["id"])
        else:
            dead.add(f["id"])
    changed = True
    while changed:  # 沿“派生自”往下：派生自被删或停用条目的，也停用
        changed = False
        for f in db.conn.execute("SELECT id, derived_from FROM facts WHERE status NOT IN ('deleted', 'suspended') AND derived_from IS NOT NULL").fetchall():
            try:
                src = {int(x) for x in json.loads(f["derived_from"] or "[]")}
            except (ValueError, TypeError):
                continue
            if src & (dead | paused) and f["id"] not in dead:
                db.conn.execute("UPDATE facts SET status = 'suspended' WHERE id = ?", (f["id"],))
                paused.add(f["id"])
                changed = True
    for fid in sorted(dead):
        db.conn.execute("UPDATE facts SET status = 'deleted', statement = '', detail = NULL, evidence_ids = '[]', root_evidence_ids = '[]' "
                        "WHERE id = ?", (fid,))
        report["facts_deleted"].append(fid)
    report["facts_suspended"] = sorted(paused)
    for e in db.conn.execute("SELECT id, evidence_ids FROM events").fetchall():
        ids = set(json.loads(e["evidence_ids"] or "[]"))
        if ids and ids <= ev:
            db.conn.execute("UPDATE events SET title = '（已删除）', summary = NULL, people = '[]', evidence_ids = '[]' WHERE id = ?", (e["id"],))
            report["events_deleted"].append(e["id"])
    touched = dead | paused
    for b in db.conn.execute("SELECT dimension, fact_ids FROM profile_blocks").fetchall():
        if set(json.loads(b["fact_ids"] or "[]")) & touched:
            db.conn.execute("UPDATE profile_blocks SET summary = '', fact_ids = '[]', version = version + 1, updated_at = ? WHERE dimension = ?",
                            (now, b["dimension"]))
            report["profile_cleared"].append(b["dimension"])
            report["history_removed"] += db.conn.execute("DELETE FROM profile_history WHERE dimension = ?", (b["dimension"],)).rowcount
    if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'mem_docs'").fetchone():
        keys = [f"ev:{i}" for i in report["evidence"]] + [f"fact:{i}" for i in sorted(touched)] + \
               [f"event:{i}" for i in report["events_deleted"]]
        for k in keys:
            report["index_removed"] += db.conn.execute("DELETE FROM mem_docs WHERE key = ?", (k,)).rowcount
            db.conn.execute("DELETE FROM mem_fts WHERE key = ?", (k,))
            try:
                db.conn.execute("DELETE FROM mem_vec WHERE key = ?", (k,))  # 向量和原文一起删（ontology.md 7.3 节）
            except sqlite3.OperationalError:
                pass
    from . import relations  # 人物卡里从这些记录整理出来的字段清空等重做（她锁住的不动，Codex 评审 11-05 第 9 条）
    report["people_cleared"] = relations.forget_evidence(db, report["evidence"])
    report["still_may_remain"] = ["她自己写过的日记和批注（不自动改，请她自己看）", "加密备份（按保留期自然过期）", "已经发给模型服务商的请求（不在我们控制里）"]
    if paused:
        report["still_may_remain"].insert(0, "还有别的依据的条目先停用了，要她或每周复盘看剩下的依据还够不够")
    db.conn.execute("INSERT INTO deletion_requests (scope, report, at) VALUES (?, ?, ?)",
                    (json.dumps({"evidence": sorted(ev), "facts": sorted(int(i) for i in fact_ids)}), json.dumps(report, ensure_ascii=False), now))
    tx.commit(db)
    return report


def unsaved_deletions(db) -> list:
    """删除记录没写到库外的删除请求（每晚体检报出来）。"""
    ensure(db)
    out = []
    for r in db.conn.execute("SELECT id, report FROM deletion_requests"):
        try:
            if json.loads(r["report"]).get("log_saved") == 0:
                out.append(r["id"])
        except ValueError:
            continue
    return out


def external_users() -> list:
    """登记表里对外的使用者（合作方、公开两档）：新登记的只要写了 tier 就自动算进来。"""
    from . import memusers
    return [u for u in memusers.USERS if memusers.tier_of(u) in EXTERNAL_TIERS]


def approve_disclosure(db, text: str, quote: str, to_users=PUBLIC_USERS, dimension: str = None, until: str = None,
                       once: bool = False, to_whom: str = None, statement_id: int = None, fact_id: int = None) -> int:
    """她逐条批准的一句对外表达（ontology.md 6.1 节，她 2026-11-02：“允许我逐条破例”）。
    身体、钱、关系、性格这类默认不对外；她点头一句就只放这一句、这一版，对谁、到什么时候、她的原话都记下。
    同类的别的内容照样不说，Dais 也不能顺着这句往下推（检索只给这句原文）。
    两种（她 2026-11-03 同意）：once=True 是“这次对这个人可以说”（to_whom 写对象），说过一次就失效；
    长期的不自动到期，RENEW_DAYS 天后进“待你定”问一次要不要续（renewals_due）。"""
    ensure(db)
    text, quote = (text or "").strip(), (quote or "").strip()
    if not text or not quote:
        raise ValueError("要有能说的那一句和她批准的原话")
    users = [u for u in to_users if u in external_users()]
    if not users:
        raise ValueError("对外表达只给对外的使用者（合作方、公开两档）")
    if dimension and dimension not in DIMENSIONS:
        raise ValueError(f"不认识的维度：{dimension}")
    if once and not to_whom:
        raise ValueError("“这次可以说”要写清对谁")
    now = datetime.now(timezone.utc)
    renew = None if once else (now + timedelta(days=RENEW_DAYS)).isoformat(timespec="seconds")
    cur = db.conn.execute("INSERT INTO disclosures (text, to_users, dimension, quote, until, created_at, once, to_whom, renew_at, "
                          "statement_id, fact_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (text, json.dumps(users), dimension, quote, until, now.isoformat(timespec="seconds"), 1 if once else 0,
                           to_whom, renew, statement_id, fact_id))
    tx.commit(db)
    return cur.lastrowid


def revoke_disclosure(db, disclosure_id: int) -> bool:
    """她撤回：立刻不再对外说（检索时按这张表现查）。"""
    ensure(db)
    n = db.conn.execute("UPDATE disclosures SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(), disclosure_id)).rowcount
    tx.commit(db)
    return n > 0


def _who(name) -> str:
    return re.sub(r"\s+", "", str(name or "")).lower()


def disclosure_ok(db, disclosure_id: int, user: str, now: str = None, recipient: str = None) -> bool:
    """这句对外表达现在能不能说给这个使用者：没撤回、没过期、对象里有它；
    指定了对象的（“只能告诉 Leo”），来问的人必须核实是那个人，不知道是谁就不说（Codex 评审 11-03）。"""
    try:
        r = db.conn.execute("SELECT * FROM disclosures WHERE id = ?", (disclosure_id,)).fetchone()
    except sqlite3.OperationalError:
        return False
    now = now or _now()
    if not r or r["revoked_at"] or (r["until"] and r["until"] <= now) or user not in json.loads(r["to_users"]):
        return False
    if "mode" in r.keys() and r["mode"] == "deny":  # 挡住的那句：不是能说的话
        return False
    if "to_whom" in r.keys() and r["to_whom"] and (not recipient or _who(recipient) != _who(r["to_whom"])):
        return False
    return not ("once" in r.keys() and r["once"] and r["used_at"])  # “这次可以说”的，说过就失效


def claim_disclosure(db, disclosure_id: int, user: str, now: str = None, recipient: str = None):
    """要对外说这句之前先“领”：一条语句里查和记一起做，两个访客同时问，“这次可以说”的只有一个领得到。
    返回领取时间（发送失败时拿它退回），领不到返回 None。长期的每次都领得到，只更新最近一次时间。"""
    ensure(db)
    if not disclosure_ok(db, disclosure_id, user, now, recipient):
        return None
    stamp = datetime.now(timezone.utc).isoformat(timespec="microseconds")
    now = now or _now()
    n = db.conn.execute("UPDATE disclosures SET used_at = ? WHERE id = ? AND revoked_at IS NULL AND (until IS NULL OR until > ?) "
                        "AND (once = 0 OR used_at IS NULL)", (stamp, disclosure_id, now)).rowcount
    tx.commit(db)
    return stamp if n else None


def confirm_disclosure_sent(db, disclosure_id: int, stamp: str) -> bool:
    """确认真的发出去了。"""
    ensure(db)
    n = db.conn.execute("UPDATE disclosures SET sent_at = ? WHERE id = ? AND used_at = ?", (_now(), disclosure_id, stamp)).rowcount
    tx.commit(db)
    return n > 0


def unclaim_disclosure(db, disclosure_id: int, stamp: str) -> bool:
    """**确认没发出去**才退回刚才那次领取（只退自己领的那次）。
    发送结果不知道（超时）时不要调用：宁可这次机会用掉，也不重复说（ontology-contracts.md 1.6 节）。"""
    ensure(db)
    n = db.conn.execute("UPDATE disclosures SET used_at = NULL WHERE id = ? AND once = 1 AND used_at = ? AND sent_at IS NULL",
                        (disclosure_id, stamp)).rowcount
    tx.commit(db)
    return n > 0


def mark_disclosure_used(db, disclosure_id: int) -> None:
    """Dais 真的对外说了这句：记下时间；“这次可以说”的从此失效。"""
    ensure(db)
    db.conn.execute("UPDATE disclosures SET used_at = COALESCE(used_at, ?) WHERE id = ?", (_now(), disclosure_id))
    tx.commit(db)


def renewals_due(db, now: str = None) -> list:
    """长期的对外表达到了该问她要不要续的时候（没撤回、没过期）。"""
    ensure(db)
    now = now or _now()
    return [dict(r) for r in db.conn.execute("SELECT * FROM disclosures WHERE once = 0 AND revoked_at IS NULL AND renew_at IS NOT NULL "
                                             "AND renew_at <= ? AND (until IS NULL OR until > ?) ORDER BY id", (now, now))]


def renew_disclosure(db, disclosure_id: int) -> bool:
    """她说续：再过 RENEW_DAYS 天再问。"""
    ensure(db)
    later = (datetime.now(timezone.utc) + timedelta(days=RENEW_DAYS)).isoformat(timespec="seconds")
    n = db.conn.execute("UPDATE disclosures SET renew_at = ? WHERE id = ? AND revoked_at IS NULL", (later, disclosure_id)).rowcount
    tx.commit(db)
    return n > 0


def disclosures(db, user: str = None, now: str = None, mode: str = "allow") -> list:
    """她批准的对外表达（mode=allow，默认）或挡住的句子（mode=deny）；mode=None 两种都给。"""
    ensure(db)
    rows = [dict(r) for r in db.conn.execute("SELECT * FROM disclosures ORDER BY id")]
    rows = [r for r in rows if mode is None or (r.get("mode") or "allow") == mode]
    return [r for r in rows if user is None or disclosure_ok(db, r["id"], user, now)]


# ---------- 对外开关：三档受众 × 类别（ontology-plan 3.3，她 2026-11-04 认可） ----------
# 她原话：“不能是只拿几句吧？应该是按照记忆提取出来的知识，批准她拿哪部分？”“以后的适用方不只是Dais吧？我要每一个都这么搞一遍么？”
# → 按三档设一次：自己人（internal，照内部规则全看）/ 合作方（partner）/ 公开（public）。登记表里每个使用者写 tier，新来的自动按那一档。
TIERS = ("internal", "partner", "public")
EXTERNAL_TIERS = ("partner", "public")
TIER_NAMES = {"internal": "自己人", "partner": "合作方", "public": "公开"}
MODES = ("allow", "ask", "deny")  # 可以说 / 问我 / 不说
MODE_NAMES = {"allow": "可以说", "ask": "问我", "deny": "不说"}
# 类别：十个方面 + 细属性（ontology.PROPERTIES 的敏感标签）分组成她看得懂的样子
DISCLOSE_CATEGORIES = (
    ("career", "职业经历", "在哪工作过、做什么"),
    ("education", "学历", "在哪读书、读什么"),
    ("skills", "技能", "会什么、擅长什么"),
    ("projects", "项目", "做过和在做的项目"),
    ("hobbies", "公开爱好", "感兴趣的话题和爱好"),
    ("views", "观点和做事方式", "价值观、怎么想事、怎么说话"),
    ("whereabouts", "现在在哪", "住在哪个城市、今天人在哪"),
    ("recent", "最近在忙什么", "手头的目标和事"),
    ("rules", "她定的规矩", "她定下的做事规矩"),
    ("prefs", "偏好", "吃什么、喜欢怎样的安排"),
    ("rhythms", "作息和日程", "几点起、什么时候忙、预约"),
    ("body", "身体", "健康、体重、训练、生日"),
    ("money", "钱", "收入、开销、账户、申请进度"),
    ("family", "感情和家人", "伴侣、家人、朋友，别人的事"),
    ("temperament", "性格情绪", "性格、情绪、心情"),
    ("address", "住址联系方式", "门牌、电话、邮箱"),
)
CATEGORY_NAMES = {k: n for k, n, _ in DISCLOSE_CATEGORIES}
LOCKED_DENY = ("address",)  # 所有对外档锁死不说，开关打不开
_PUBLIC_OK = ("career", "education", "skills", "projects", "hobbies", "views")
DEFAULT_POLICY = {
    "public": {**{k: "deny" for k in CATEGORY_NAMES}, **{k: "allow" for k in _PUBLIC_OK}, "whereabouts": "ask", "recent": "ask"},
    "partner": {**{k: "deny" for k in CATEGORY_NAMES}, **{k: "allow" for k in _PUBLIC_OK + ("recent", "rules", "prefs")},
                "whereabouts": "ask", "rhythms": "ask"},
}
PROP_CATEGORY = {"worked_at": "career", "studied_at": "education", "skill": "skills", "project": "projects", "interest": "hobbies",
                 "lives_in": "whereabouts", "located_in": "whereabouts", "situation_start": "recent", "situation_end": "recent",
                 "preference": "prefs", "reaction_prep": "prefs", "reaction_diary": "prefs", "reaction_mail": "prefs", "reaction_plan": "prefs",
                 "wake_time": "rhythms", "work_start": "rhythms", "sleep_late": "rhythms", "outings_per_week": "rhythms",
                 "calendar_per_week": "rhythms", "workbench_edits": "rhythms", "appointment": "rhythms",
                 "birthday": "body", "body_weight": "body", "weekly_spend": "money", "application_status": "money",
                 "account_status": "money", "relation_to_her": "family"}
DIM_CATEGORY = {"values": "views", "thinking": "views", "expression": "views", "abilities": "skills", "preferences": "prefs",
                "body": "body", "relationships": "family", "resources": "money", "rhythms": "rhythms", "temperament": "temperament"}
CATEGORY_DIM = {"body": "body", "money": "resources", "family": "relationships", "temperament": "temperament"}  # 旧的逐条批准按方面记
# 原文里像住址、电话、邮箱的：不管归在哪类，都按“住址联系方式”锁死
CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[a-z]{2,}|\+\d[\d \-]{7,}\d|(?<!\d)1\d{10}(?!\d)|(?<!\d)0\d{9,10}(?!\d)|门牌|邮编|"
                     r"\d+\s*(号楼|单元|室)|\b\d+[a-z]?\s+\w+\s+(street|road|avenue|lane)\b|\b[a-z]{1,2}\d{1,2}[a-z]?\s+\d[a-z]{2}\b", re.I)
NOT_OUT = ("other_person", "system_inference", "agent_relay")  # 别人说的、系统推的、agent 转述的：类别开着也不对外
GONE = ("deleted", "invalid", "suspended")  # 删掉的、她说“不对”的、删后停用的


def category_of(dimension: str = None, prop: str = None, kind: str = None, text: str = None):
    """一条说法归哪一类（DISCLOSE_CATEGORIES 的 key）；认不出返回 None（按不说算）。"""
    if text and CONTACT.search(text):
        return "address"
    if kind == "decision":
        return "rules"
    if kind == "goal":
        return "recent"
    prop = prop or ""
    if prop.startswith("dim:"):
        dimension, prop = prop[4:], ""
    if prop in PROP_CATEGORY:
        return PROP_CATEGORY[prop]
    return DIM_CATEGORY.get(dimension)


def _policy_rows(db) -> dict:
    try:
        return {(r["tier"], r["category"]): dict(r) for r in db.conn.execute("SELECT * FROM disclosure_policy")}
    except sqlite3.OperationalError:
        return {}


def policy_mode(db, tier: str, category: str) -> str:
    """这一档对这一类现在是：allow 可以说 / ask 问我 / deny 不说。自己人档不走开关（照内部规则）。"""
    if tier == "internal":
        return "allow"
    if tier not in EXTERNAL_TIERS or category not in CATEGORY_NAMES or category in LOCKED_DENY:
        return "deny"
    row = _policy_rows(db).get((tier, category))
    mode = row["mode"] if row else DEFAULT_POLICY[tier][category]
    return mode if mode in MODES else "deny"


def policy(db) -> list:
    """给“我”页的对外开关：每档一张表，每行一个类别（现在的档、默认的档、她改过没有、锁死没有）。"""
    ensure(db)
    rows = _policy_rows(db)
    out = []
    for tier in ("public", "partner"):
        items = []
        for key, name, hint in DISCLOSE_CATEGORIES:
            r = rows.get((tier, key))
            items.append({"key": key, "name": name, "hint": hint, "mode": policy_mode(db, tier, key), "default": DEFAULT_POLICY[tier][key],
                          "changed": bool(r and r["by"] != "default"), "updated_at": (r or {}).get("updated_at", ""),
                          "locked": key in LOCKED_DENY})
        out.append({"tier": tier, "name": TIER_NAMES[tier], "rows": items})
    return out


def set_policy(db, tier: str, category: str, mode: str, by: str = "mia") -> bool:
    """她改一个开关（谁改的、什么时候记在表里）。住址联系方式锁死，改不了。"""
    ensure(db)
    if tier not in EXTERNAL_TIERS:
        raise ValueError("只有合作方、公开两档有开关；自己人照内部规则")
    if category not in CATEGORY_NAMES:
        raise ValueError(f"不认识的类别：{category}")
    if mode not in MODES:
        raise ValueError("只能是 allow / ask / deny")
    if category in LOCKED_DENY:
        raise ValueError("住址和联系方式对外永远不说，开关打不开")
    db.conn.execute("INSERT INTO disclosure_policy (tier, category, mode, updated_at, by) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(tier, category) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at, by = excluded.by",
                    (tier, category, mode, _now(), by))
    tx.commit(db)
    return True


def deny_disclosure(db, text: str, quote: str, to_users=None, statement_id: int = None, fact_id: int = None,
                    dimension: str = None) -> int:
    """逐条挡住：这一类可以说，但这一句不说（对哪几个对外使用者，默认全部）。撤回用 revoke_disclosure。"""
    ensure(db)
    text, quote = (text or "").strip(), (quote or "").strip() or "她说这句不对外"
    if not text and not statement_id and not fact_id:
        raise ValueError("要说清挡住哪一句")
    users = [u for u in (to_users or external_users()) if u in external_users()]
    cur = db.conn.execute("INSERT INTO disclosures (text, to_users, dimension, quote, created_at, mode, statement_id, fact_id) "
                          "VALUES (?, ?, ?, ?, ?, 'deny', ?, ?)", (text or f"#{statement_id or fact_id}", json.dumps(users), dimension, quote,
                                                                   _now(), statement_id, fact_id))
    tx.commit(db)
    return cur.lastrowid


def _g(row, *keys):
    for k in keys:
        try:
            v = row[k]
        except (KeyError, IndexError):
            continue
        if v is not None:
            return v
    return None


def _exception(db, user: str, sid, fid, text: str, recipient: str = None):
    """她对这一句定过的例外：'deny' 挡住 / 'allow' 放行 / None 没有。挡住优先。"""
    try:
        rows = db.conn.execute("SELECT * FROM disclosures WHERE revoked_at IS NULL AND (statement_id = ? OR fact_id = ? OR text = ?)",
                               (sid or -1, fid or -1, (text or "").strip() or None)).fetchall()
    except sqlite3.OperationalError:
        return None
    rows = [r for r in rows if user in json.loads(r["to_users"] or "[]")]
    if any((r["mode"] or "allow") == "deny" for r in rows):
        return "deny"
    if any(disclosure_ok(db, r["id"], user, recipient=recipient) for r in rows):
        return "allow"
    return None


def disclose(db, user: str, fact, recipient: str = None) -> str:
    """这条记忆能不能给这个使用者往外说：'allow' 可以说 / 'ask' 先问她 / 'deny' 不说。所有对外的路径都走这一个
    （onto_query 第 14–16 题和 _visible、portrait.chat_text、memsearch.search）。fact 可以是 facts 的一行，也可以是 statements 的一行。
    顺序：自己人照内部规则（allow，读取把关在 memusers.may_read）→ 删掉的、她说不对的、别人的事不说 → 住址联系方式锁死 →
    她对这一句的例外（挡住 > 放行）→ 两道保险（别人说的、系统推的、agent 转述的、猜测和预测）→ 这一档这一类的开关。
    候选和有争议的（还没确认）最多到“问我”。"""
    from . import memusers
    tier = memusers.tier_of(user)
    if tier == "internal":
        return "allow"
    if on_hold(db):
        return "deny"
    sid, fid = None, None
    if _g(fact, "property") is not None or _g(fact, "said_by") is not None:  # statements 的一行
        sid = _g(fact, "id")
        text, prop, dim = _g(fact, "value_text") or "", _g(fact, "property"), None
        src, review = _g(fact, "said_by"), _g(fact, "review")
        if review in ("retracted", "forgotten"):
            return "deny"
        from . import ontology
        if _g(fact, "subject_id") not in (None, ontology.self_id(db)):
            return "deny"
        m = db.conn.execute("SELECT fact_id FROM fact_map WHERE statement_id = ?", (sid,)).fetchone() if sid else None
        fid = m[0] if m else None
        if fid and (db.conn.execute("SELECT status FROM facts WHERE id = ?", (fid,)).fetchone() or [None])[0] in GONE:
            return "deny"  # 旧 facts 删后停用的，说法上只是 candidate：按旧的状态挡（Codex 评审 11-05）
        if prop and not prop.startswith("dim:"):
            dim = {"body": "body", "resources": "resources", "relationships": "relationships"}.get(
                (db.conn.execute("SELECT sensitive FROM properties WHERE key = ?", (prop,)).fetchone() or [None])[0])
    else:
        fid = _g(fact, "id")
        text, prop, dim = _g(fact, "statement", "text") or "", _g(fact, "prop"), _g(fact, "dimension")
        src, review = _g(fact, "source_kind"), {"candidate": "candidate"}.get(_g(fact, "status"), "accepted")
        if _g(fact, "status") in GONE or _g(fact, "valid_to") or (_g(fact, "subject") or "self") != "self":
            return "deny"
        m = db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (fid,)).fetchone() if fid else None
        sid = m[0] if m else None
    kind = _g(fact, "kind")
    cat = category_of(dim, prop, kind, text)
    if cat is None or cat in LOCKED_DENY:
        return "deny"
    exc = _exception(db, user, sid, fid, text, recipient)
    if exc:
        return exc
    if kind in ("hypothesis", "prediction") or src in NOT_OUT:
        return "deny"
    read_by = _g(fact, "read_by")
    if read_by and not memusers.may_read(user, None, read_by):
        return "deny"
    mode = policy_mode(db, tier, cat)
    if mode == "allow" and review in ("candidate", "disputed"):
        return "ask"
    return mode


def reapply_deletions(db, root, may_hold: bool = True) -> dict:
    """从备份恢复后、启动各服务之前跑一次：按库外的删除记录把删除重做一遍（ontology-contracts.md 1.8 节）。
    核不清的时候（数据目录外那份记录不在，而库里或目录里有删过东西的痕迹，或者记录读坏了）：
    她 2026-11-03 选“相关记忆先不用，等核清再说”——记一条“记忆暂停”，除了她本人和系统维护，谁都查不到记忆，
    等她确认后用 --release-hold 放开。
    可重复跑（幂等）：已经删过的不再删。每晚整理开头也跑一次（may_hold=False：只重做删除，不再记暂停——
    她放开暂停后目录外那份仍可能不在，每晚都暂停会把她放开的又关上；暂停只在恢复时那次判断）。"""
    ensure(db)
    data_log, mirror = _log_paths(root)
    entries, broken = [], 0
    for path in (data_log, mirror):  # 两份合起来，按时间先后重放
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        broken += 1
    had_deletions = data_log.exists() or db.conn.execute("SELECT 1 FROM deletion_requests LIMIT 1").fetchone() is not None
    verified = not broken and (mirror.exists() or not had_deletions)
    state, facts = {}, set()
    for d in sorted(entries, key=lambda d: d.get("at") or ""):
        if "fact" in d:
            facts.add(int(d["fact"]))
        elif "fp" in d:
            state[d["fp"]] = {**state.get(d["fp"], {}), **d}
    for fp, d in state.items():
        db.conn.execute("INSERT OR REPLACE INTO forgotten (fp, source, at, allow_reimport) VALUES (?, ?, ?, ?)",
                        (fp, d.get("source"), d.get("at") or _now(), d.get("allow", 0)))
    blocked = {fp for fp, d in state.items() if not d.get("allow")}
    todo = [r["id"] for r in db.conn.execute("SELECT id, source, source_ref, deleted_at FROM evidence").fetchall()
            if not r["deleted_at"] and fingerprint(r["source"], r["source_ref"]) in blocked]
    live = [i for i in facts if db.conn.execute("SELECT 1 FROM facts WHERE id = ? AND status != 'deleted'", (i,)).fetchone()]
    rep_ = forget(db, todo, live) if (todo or live) else {"evidence": [], "facts_deleted": []}
    if not verified and may_hold:
        hold(db, "从备份恢复后核不清删除记录" + ("（记录有读坏的行）" if broken else "（数据目录外那份不在）"))
    tx.commit(db)
    return {"redone": len(rep_["evidence"]), "facts_redone": len(live), "verified": verified}


HOLD_SCHEMA = "CREATE TABLE IF NOT EXISTS memory_hold (id INTEGER PRIMARY KEY, reason TEXT NOT NULL, at TEXT NOT NULL, released_at TEXT)"


def hold(db, reason: str) -> None:
    db.conn.execute(HOLD_SCHEMA)
    db.conn.execute("INSERT INTO memory_hold (reason, at) VALUES (?, ?)", (reason, _now()))
    tx.commit(db)


def on_hold(db):
    """记忆暂停中：返回原因，没有返回 None。"""
    try:
        r = db.conn.execute("SELECT reason FROM memory_hold WHERE released_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return r["reason"] if r else None


def release_hold(db) -> int:
    """她确认核清了（或者把最近删过的再删了一遍）：放开。"""
    db.conn.execute(HOLD_SCHEMA)
    n = db.conn.execute("UPDATE memory_hold SET released_at = ? WHERE released_at IS NULL", (_now(),)).rowcount
    tx.commit(db)
    return n


def main():
    parser = argparse.ArgumentParser(description="数字生命地基：补登记现有数据")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--reapply-deletions", action="store_true", help="从备份恢复后：把她要求的删除重做一遍")
    parser.add_argument("--release-hold", action="store_true", help="她确认删除记录核清了：放开记忆暂停")
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        if args.release_hold:
            print(json.dumps({"released": release_hold(store.db)}), flush=True)
        elif args.reapply_deletions:
            print(json.dumps(reapply_deletions(store.db, args.data), ensure_ascii=False), flush=True)
            return
        print(json.dumps(backfill(store), ensure_ascii=False), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()


RELAY_SOURCES = ("grok", "agent")  # 转述来的（docs/design/ontology.md 7.1 节）：不算独立证据


# 迁移第 2 步（ontology-structure.md 9.2 节，她 2026-11-03 同意）：新旧双写。旧 facts 还是各角色读的那份（说了算），
# 每次写 facts（不管哪个模块、哪条路径），数据库触发器在**同一个事务里**同步写一条说法，失败就一起失败。
# 第 3 步起（v2）：关于她本人的和别人的（人物卡）都同步，主语按 object_refs 对应到对象；老数据由 migrate_old_facts 一次迁过来。
# 说法变化记录只记审核状态和止日，不记内容（遗忘时内容不能留在变化记录里）。
MIRROR_SQL = """
CREATE TRIGGER IF NOT EXISTS facts_mirror_ins_v4 AFTER INSERT ON facts
WHEN (SELECT object_id FROM object_refs WHERE ref = NEW.subject) IS NOT NULL
BEGIN
  INSERT INTO statements (subject_id, property, value_text, context, kind, said_by, valid_from, valid_to, recorded_at, review, superseded_by,
                          visibility, read_by, locked)
  VALUES ((SELECT object_id FROM object_refs WHERE ref = NEW.subject), COALESCE(NEW.prop, 'dim:' || NEW.dimension), COALESCE(NEW.statement, ''), NEW.context, COALESCE(NEW.kind, 'fact'),
          COALESCE(NEW.source_kind, 'unknown'), NEW.valid_from, NEW.valid_to, COALESCE(NEW.recorded_at, datetime('now')), (CASE NEW.status WHEN 'active' THEN 'accepted' WHEN 'invalid' THEN 'retracted' WHEN 'deleted' THEN 'forgotten' ELSE 'candidate' END), (SELECT statement_id FROM fact_map WHERE fact_id = NEW.superseded_by LIMIT 1), NEW.visibility, NEW.read_by, COALESCE(NEW.locked, 0));
  INSERT INTO fact_map (fact_id, statement_id) VALUES (NEW.id, last_insert_rowid());
  INSERT INTO statement_sources (statement_id, evidence_id, role)
    SELECT (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1), CAST(value AS INTEGER), 'supports' FROM json_each(COALESCE(NEW.evidence_ids, '[]'));
  INSERT INTO statement_sources (statement_id, role, source_unknown)
    SELECT (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1), 'supports', 1 WHERE json_array_length(COALESCE(NEW.evidence_ids, '[]')) = 0;
END;
CREATE TRIGGER IF NOT EXISTS facts_mirror_upd_v4 AFTER UPDATE ON facts
WHEN EXISTS (SELECT 1 FROM fact_map WHERE fact_id = NEW.id)
BEGIN
  INSERT INTO statement_changes (statement_id, field, old, new, at)
    SELECT st.id, 'review', st.review, (CASE NEW.status WHEN 'active' THEN 'accepted' WHEN 'invalid' THEN 'retracted' WHEN 'deleted' THEN 'forgotten' ELSE 'candidate' END), datetime('now') FROM statements st WHERE st.id = (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1) AND st.review != (CASE NEW.status WHEN 'active' THEN 'accepted' WHEN 'invalid' THEN 'retracted' WHEN 'deleted' THEN 'forgotten' ELSE 'candidate' END);
  INSERT INTO statement_changes (statement_id, field, old, new, at)
    SELECT st.id, 'valid_to', st.valid_to, NEW.valid_to, datetime('now') FROM statements st WHERE st.id = (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1) AND st.valid_to IS NOT NEW.valid_to;
  UPDATE statements SET property = COALESCE(NEW.prop, 'dim:' || NEW.dimension), value_text = COALESCE(NEW.statement, ''), context = NEW.context,
    kind = COALESCE(NEW.kind, 'fact'), said_by = COALESCE(NEW.source_kind, 'unknown'), valid_from = NEW.valid_from, valid_to = NEW.valid_to,
    review = (CASE NEW.status WHEN 'active' THEN 'accepted' WHEN 'invalid' THEN 'retracted' WHEN 'deleted' THEN 'forgotten' ELSE 'candidate' END), superseded_by = (SELECT statement_id FROM fact_map WHERE fact_id = NEW.superseded_by LIMIT 1),
    visibility = NEW.visibility, read_by = NEW.read_by, locked = COALESCE(NEW.locked, 0),
    retracted_at = CASE WHEN (CASE NEW.status WHEN 'active' THEN 'accepted' WHEN 'invalid' THEN 'retracted' WHEN 'deleted' THEN 'forgotten' ELSE 'candidate' END) IN ('retracted', 'forgotten') THEN COALESCE(retracted_at, datetime('now')) ELSE NULL END
  WHERE id = (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1);
  DELETE FROM statement_sources WHERE statement_id = (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1) AND NEW.evidence_ids IS NOT OLD.evidence_ids;
  INSERT INTO statement_sources (statement_id, evidence_id, role)
    SELECT (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1), CAST(value AS INTEGER), 'supports' FROM json_each(COALESCE(NEW.evidence_ids, '[]')) WHERE NEW.evidence_ids IS NOT OLD.evidence_ids;
  INSERT INTO statement_sources (statement_id, role, source_unknown)
    SELECT (SELECT statement_id FROM fact_map WHERE fact_id = NEW.id LIMIT 1), 'supports', 1 WHERE NEW.evidence_ids IS NOT OLD.evidence_ids AND json_array_length(COALESCE(NEW.evidence_ids, '[]')) = 0;
END;
"""
# 旧 facts 删后停用（suspended）、删掉、作废的，双写到说法里分别是 candidate / forgotten / retracted；停用的和“还没审”的候选
# 在说法上分不出来，读说法的地方（onto_query、disclose）按对照查回旧 facts 的状态挡掉（Codex 评审 11-05：她删过的知识 小伴 按题还能读到）。
NOT_GONE_STATEMENT = ("id NOT IN (SELECT m.statement_id FROM fact_map m JOIN facts f ON f.id = m.fact_id "
                      "WHERE f.status IN ('suspended', 'deleted', 'invalid'))")


def statement_gone(db, statement_id) -> bool:
    """这条说法对应的旧 facts 是不是删了、作废了或删后停用了。"""
    return bool(statement_id) and bool(db.conn.execute(
        "SELECT 1 FROM fact_map m JOIN facts f ON f.id = m.fact_id WHERE m.statement_id = ? "
        "AND f.status IN ('suspended', 'deleted', 'invalid')", (statement_id,)).fetchone())


REVIEW_OF = {"active": "accepted", "invalid": "retracted", "deleted": "forgotten"}  # 其余（候选、停用）都是 candidate


def migrate_old_facts(db) -> dict:
    """迁移第 3 步（ontology-structure.md 9.2 节）：还没有对照的旧 facts 逐条复制成说法。**只复制，不改旧的**；
    找不到原始记录的出处标 source_unknown，不让模型补；她锁定的照旧锁在旧 facts 上（第 4 步读的时候一起看）。
    可以重复跑：已经有对照的跳过。一个事务里做完，出错整体回滚。"""
    from . import ontology
    ensure(db)
    ontology.ensure(db)
    if db.conn.in_transaction:
        db.conn.commit()
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        got = {"migrated": 0, "source_unknown": 0, "people": 0, "skipped": 0}
        before_people = db.conn.execute("SELECT COUNT(*) FROM object_types WHERE type = 'person'").fetchone()[0]
        rows = db.conn.execute("SELECT * FROM facts WHERE id NOT IN (SELECT fact_id FROM fact_map) ORDER BY id").fetchall()
        for f in rows:
            subj = ontology.object_for_ref(db, f["subject"] or "self")
            if subj is None:
                got["skipped"] += 1
                continue
            keys = f.keys()
            cur = db.conn.execute(
                "INSERT INTO statements (subject_id, property, value_text, context, kind, said_by, valid_from, valid_to, recorded_at, "
                "review, retracted_at, visibility, read_by, locked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (subj, (f["prop"] if "prop" in keys and f["prop"] else None) or "dim:" + f["dimension"], f["statement"] or "", f["context"] if "context" in keys else None,
                 (f["kind"] if "kind" in keys else None) or "fact", (f["source_kind"] if "source_kind" in keys else None) or "unknown",
                 f["valid_from"], f["valid_to"], f["recorded_at"] or _now(), REVIEW_OF.get(f["status"], "candidate"),
                 _now() if f["status"] in ("invalid", "deleted") else None, f["visibility"],
                 f["read_by"] if "read_by" in keys else None, (f["locked"] if "locked" in keys else 0) or 0))
            sid = cur.lastrowid
            db.conn.execute("INSERT INTO fact_map (fact_id, statement_id) VALUES (?, ?)", (f["id"], sid))
            ids = json.loads(f["evidence_ids"] or "[]")
            for e in ids:
                db.conn.execute("INSERT INTO statement_sources (statement_id, evidence_id, role) VALUES (?, ?, 'supports')", (sid, int(e)))
            if not ids:
                db.conn.execute("INSERT INTO statement_sources (statement_id, role, source_unknown) VALUES (?, 'supports', 1)", (sid,))
                got["source_unknown"] += 1
            got["migrated"] += 1
        db.conn.execute("UPDATE statements SET superseded_by = (SELECT m2.statement_id FROM fact_map m JOIN facts f ON f.id = m.fact_id "
                        "JOIN fact_map m2 ON m2.fact_id = f.superseded_by WHERE m.statement_id = statements.id) "
                        "WHERE superseded_by IS NULL AND id IN (SELECT m.statement_id FROM fact_map m JOIN facts f ON f.id = m.fact_id "
                        "WHERE f.superseded_by IS NOT NULL)")
        got["people"] = db.conn.execute("SELECT COUNT(*) FROM object_types WHERE type = 'person'").fetchone()[0] - before_people
        db.conn.commit()
    except BaseException:
        db.conn.rollback()
        raise
    return got


def memory_stats(db) -> dict:
    """设置页“记忆”一栏：关于她的记忆有多少条、各来源进了多少。只给数字。"""
    ensure(db)
    n = db.conn.execute("SELECT COUNT(*) FROM facts WHERE subject = 'self' AND status NOT IN ('invalid', 'deleted', 'suspended') "
                        "AND valid_to IS NULL").fetchone()[0]
    by = {r[0] or "unknown": r[1] for r in db.conn.execute(
        "SELECT source_kind, COUNT(*) FROM facts WHERE subject = 'self' AND status NOT IN ('invalid', 'deleted', 'suspended') "
        "AND valid_to IS NULL GROUP BY source_kind")}
    return {"facts": n, "by_source": by, "on_hold": on_hold(db)}


def unmapped_facts(db) -> int:
    """还没有新旧对照的旧记录条数（第 3 步迁完应该是 0）。"""
    try:
        return db.conn.execute("SELECT COUNT(*) FROM facts WHERE id NOT IN (SELECT fact_id FROM fact_map)").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def reconcile_statements(db) -> list:
    """每晚对账（9.2 节）：按新旧对照表逐条比内容、审核状态、成立时间、出处集合，不比条数。返回不一致的（只有编号和哪一项）。"""
    try:
        rows = db.conn.execute("SELECT f.id fid, f.statement, f.status, f.valid_from, f.valid_to, f.evidence_ids, s.id sid, "
                               "f.prop, f.dimension, s.property, f.visibility, f.read_by, f.locked, s.visibility s_vis, s.read_by s_read, s.locked s_locked, "
                               "s.value_text, s.review, s.valid_from s_from, s.valid_to s_to FROM fact_map m "
                               "JOIN facts f ON f.id = m.fact_id JOIN statements s ON s.id = m.statement_id").fetchall()
    except sqlite3.OperationalError:
        return []
    out = []
    for r in rows:
        diff = []
        if (r["statement"] or "") != (r["value_text"] or ""):
            diff.append("内容")
        if REVIEW_OF.get(r["status"], "candidate") != r["review"]:
            diff.append("审核状态")
        if (r["valid_from"], r["valid_to"]) != (r["s_from"], r["s_to"]):
            diff.append("成立时间")
        if (r["visibility"], r["read_by"], r["locked"] or 0) != (r["s_vis"], r["s_read"], r["s_locked"] or 0):
            diff.append("权限")
        if (r["prop"] or "dim:" + (r["dimension"] or "")) != r["property"]:
            diff.append("属性")
        src = {x[0] for x in db.conn.execute("SELECT evidence_id FROM statement_sources WHERE statement_id = ? AND evidence_id IS NOT NULL", (r["sid"],))}
        if src != set(json.loads(r["evidence_ids"] or "[]")):
            diff.append("出处")
        if diff:
            out.append(f"事实 #{r['fid']} 和说法 #{r['sid']} 对不上：{'、'.join(diff)}")
    return out


def integrity_problems(db) -> list:
    """给巡检和检查用（ethan/invariants.py）：
    - 只靠 Grok / 其他 agent 转述撑着、她也没确认过的事实，不该是“生效”的高把握事实；
    - 身体、钱、关系、性格这几类默认不对外，她逐条破例之前不能是公开级（她 2026-11-02 定可逐条破例）。"""
    ensure(db)
    out = []
    src = {r["id"]: r["source"] for r in db.conn.execute("SELECT id, source FROM evidence")}
    confirmed = set()  # 她在“我”页或聊天里确认过的、锁定过的（last_confirmed_at 建的时候就填了，不能拿来判断）
    if db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'confirmations'").fetchone():
        confirmed |= {r[0] for r in db.conn.execute("SELECT fact_id FROM confirmations WHERE fact_id IS NOT NULL")}
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(facts)")}
    if "locked" in cols:
        confirmed |= {r[0] for r in db.conn.execute("SELECT id FROM facts WHERE locked = 1")}
    for f in db.conn.execute("SELECT id, statement, dimension, visibility, evidence_ids, confidence "
                             "FROM facts WHERE status = 'active' AND superseded_by IS NULL"):
        if f["dimension"] in NEVER_PUBLIC and f["visibility"] == "public":
            out.append(f"事实 #{f['id']}（{f['dimension']}）标成了公开，这类默认不对外")
        ids = json.loads(f["evidence_ids"] or "[]")
        if ids and all(src.get(i) in RELAY_SOURCES for i in ids) and f["id"] not in confirmed and (f["confidence"] or 0) >= 0.8:
            out.append(f"事实 #{f['id']}「{str(f['statement'])[:30]}」只有转述撑着、她没确认过，却是高把握事实")
    for rid in unsaved_deletions(db):
        out.append(f"删除请求 #{rid} 的删除记录没写到库外，从备份恢复时可能复活")
    if on_hold(db):
        out.append(f"记忆暂停中：{on_hold(db)}（她确认后 --release-hold 放开）")
    out += reconcile_statements(db)
    return out


# ---------- 行为记录每周整理成说法（ontology-plan 1.5） ----------
# 只描述、带统计时段和样本数，不推性格；来源类型是“行为记录”。同一属性新一周的取代上一周的（旧的留着、填失效时间），
# 她锁定过的不动（supersede 会转去问她）。按门禁 1 只写说法，不碰待办、计划和对外内容。
BEHAVIOR_PROPS = {"work_start": ("computer", "最近 {w} 周工作日一般 {v} 开始用 Mac（样本 {n} 天）"),
                  "sleep_late": ("computer", "最近 {w} 周晚上一般用 Mac 到 {v}（过了午夜算前一晚，样本 {n} 晚）"),
                  "outings_per_week": ("place", "最近 {w} 周平均每周出门 {v} 天（按 iPhone 离家记录，{n} 天）"),
                  "calendar_per_week": ("workbench", "最近 {w} 周日历上平均每周 {v} 个安排（共 {n} 个，只是安排，去没去另算）"),
                  "workbench_edits": ("workbench", "最近 {w} 周在工作台上亲手改了 {v} 次（改计划、删待办、锁住不让系统改）")}


def write_behavior(db, day=None, weeks: int = 4) -> dict:
    """每周跑一次（周日夜里），同一周重复跑不会重复记。返回 {写了, 取代, 跳过}。"""
    from datetime import timedelta
    from . import clock, onto_query
    ensure(db)
    day = day or clock.her_today()
    week = (day - timedelta(days=day.weekday())).isoformat()
    out = {"written": 0, "superseded": 0, "skipped": 0}
    from . import runlog
    try:
        runlog.collect(db)
        reactions = runlog.behavior_items(db, day, weeks)
    except Exception:
        reactions = []
    for it in onto_query.rhythm_items(db, day, weeks) + reactions:
        source, text = BEHAVIOR_PROPS.get(it["key"]) or ("workbench", "最近 {w} 周" + it["what"] + "：{v}（共 {n} 次）")
        line = text.format(w=weeks, v=it["value"], n=it["samples"])
        text_ev = f"{line}。{it['note']}"
        ref = f"behavior:{it['key']}:{week}"
        prev = db.conn.execute("SELECT text FROM evidence WHERE source_ref = ?", (ref,)).fetchone()
        if prev and prev[0] != text_ev:  # 同一周里数据或算法变了（如 11-04 修开始时间）：另记一条，新的取代旧的
            ref += ":" + hashlib.sha1(text_ev.encode()).hexdigest()[:8]
        try:
            ev = add_evidence(db, source, ref, day.isoformat(), text=text_ev)
        except Forgotten:
            out["skipped"] += 1
            continue
        if db.conn.execute("SELECT 1 FROM facts WHERE evidence_ids = ? AND prop = ? AND valid_to IS NULL AND status != 'invalid'",
                           (json.dumps([ev]), it["key"])).fetchone():
            out["skipped"] += 1
            continue
        old = [r["id"] for r in db.conn.execute(
            "SELECT id FROM facts WHERE prop = ? AND source_kind = 'behavior' AND valid_to IS NULL AND status != 'invalid'", (it["key"],))]
        new = add_fact(db, "rhythms", line, [ev], kind="fact", source_kind="behavior", prop=it["key"], valid_from=day.isoformat(),
                       confidence=0.7, importance=2, context=f"{(day - timedelta(days=7 * weeks)).isoformat()} 到 {day.isoformat()} 的记录",
                       detail=it["note"])
        out["written"] += 1
        for o in old:
            out["superseded"] += 1 if supersede(db, o, new, when=day.isoformat()) else 0
    from . import tx
    tx.commit(db)
    return out


# ---------- 来源健康（ontology-plan 1.10） ----------
# 每个数据源：(编号, 名字, 最近一条怎么查, 多少天没新的算断档；None = 不定期，不报断档)
# 只查已经接上的（有过记录的）；从没来过的标“没接上”，不当断档报。按各自的频率判断，月度的晚一两天不报。
WX_HEAD = "e.text LIKE '【访谈】会话：微信%'"  # 微信导出交来的整段对话（ethan/wechatexport.py，会话名“微信 · ”“微信群 · ”）
AGENT_HEAD = "instr(substr(e.text, 1, 300), '；agent：{a}') > 0"  # 其他 agent 交来的整段对话，头一行写着 agent（ethan/interviews.py receive）
SOURCE_REGISTRY = (
    ("S1", "和 小伴 聊天", "SELECT MAX(occurred_at) FROM evidence WHERE source = 'chat' AND source_ref LIKE 'raw:%'", 2),
    ("S2", "你发的文件", "SELECT MAX(recorded_at) FROM evidence WHERE source = 'file'", None),
    ("S4", "邮件", "SELECT MAX(created_at) FROM mail_items", 2),
    ("S4b", "你发出去的邮件", "SELECT MAX(recorded_at) FROM evidence WHERE source = 'sent'", None),  # 她想发才发，不定期
    ("S5", "银行流水", "SELECT MAX(at) FROM transactions", 4),
    ("S6", "Grok 和其他 agent 的回报", "SELECT MAX(recorded_at) FROM evidence WHERE source IN ('grok', 'agent')", None),
    ("S7", "收藏和链接", "SELECT MAX(created_at) FROM brain_items", 7),
    ("S10", "其他会话里你说的、给的", "SELECT MAX(e.recorded_at) FROM evidence e WHERE e.source = 'interview' AND NOT " + WX_HEAD
     + " AND NOT " + AGENT_HEAD.format(a=""), None),
    ("S13", "日历", "SELECT MAX(day) FROM future_events", None),
    ("S14", "iPhone 到家离家", "SELECT MAX(at) FROM place_events", 3),
    ("S15", "Mac 工作时长", "SELECT MAX(slot) FROM work_slots", 4),
    ("S17", "日记（每晚自动写）", "SELECT MAX(updated_at) FROM diary", 3),
    # 她自己在工作台写的日记、今日卡心愿（S17 里她亲手写的那部分，ethan/interviews.py 拆成说法）：想写才写，不报断档
    ("S17b", "工作台上你亲手写的", "SELECT MAX(recorded_at) FROM evidence WHERE source = 'workbench' AND "
     "(source_ref LIKE 'diary:%' OR source_ref LIKE 'wish:%')", None),
    # 她 Mac 上每周日 20:43 自动导出（launchd）；Mac 关着会晚一点，隔 9 天没收到才算断档。服务器只看“最近一次收到”
    ("S19", "微信聊天导出（Mac）", "SELECT MAX(e.recorded_at) FROM evidence e WHERE e.source = 'interview' AND " + WX_HEAD, 9),
    # Codex 每周日 20:13 导出，但只交新的会话：她一周没用 Codex 就没有新的，不按断档报
    ("S20", "Codex 导出（Mac）", "SELECT MAX(e.recorded_at) FROM evidence e WHERE e.source = 'interview' AND " + AGENT_HEAD.format(a="Codex"), None),
    # 每月提醒她点一次导出（ethan/grokexport.py）；晚一两周不报，隔 45 天没有新的才算断档
    ("S20g", "Grok Bot App 导出", "SELECT MAX(at) FROM grok_files WHERE is_grok = 1", 45),
)
# 本月贡献了几条说法：每个来源对应哪些原始记录（evidence 别名 e）
FACT_SOURCES = {
    "S1": "e.source = 'chat'", "S2": "e.source = 'file'", "S4": "e.source = 'email'", "S4b": "e.source = 'sent'", "S5": "e.source = 'bank'",
    "S6": "e.source IN ('grok', 'agent')", "S7": "e.source = 'link'",
    "S10": "e.source = 'interview' AND NOT " + WX_HEAD + " AND NOT " + AGENT_HEAD.format(a=""),
    "S14": "e.source = 'place'", "S15": "e.source = 'computer'",
    "S17b": "e.source = 'workbench' AND (e.source_ref LIKE 'diary:%' OR e.source_ref LIKE 'wish:%')",
    "S19": "e.source = 'interview' AND " + WX_HEAD,
    "S20": "e.source = 'interview' AND " + AGENT_HEAD.format(a="Codex"),
    "S20g": "e.source = 'interview' AND " + AGENT_HEAD.format(a="Grok"),
}


def source_health(db, now: datetime = None, registry=SOURCE_REGISTRY) -> list:
    """每个来源：最近一条、隔了几天、本月贡献了几条说法、是不是断档。给每晚体检和“我”页的来源一栏用。"""
    ensure(db)
    now = now or datetime.now(timezone.utc)
    month = now.strftime("%Y-%m")
    cases = " ".join(f"WHEN {cond} THEN '{code}'" for code, cond in FACT_SOURCES.items())
    made = {}
    from . import retire
    if retire.fallback(db):  # 3.4 回退开关：切回读旧表
        sql = (f"SELECT CASE {cases} END code, COUNT(DISTINCT f.id) n FROM facts f, json_each(f.evidence_ids) j "
               "JOIN evidence e ON e.id = j.value WHERE substr(f.recorded_at, 1, 7) = ? GROUP BY code")
    else:  # ontology-plan 3.4：本月贡献按说法和出处数（新旧一一对照，数字一样）
        sql = (f"SELECT CASE {cases} END code, COUNT(DISTINCT s.id) n FROM statements s JOIN statement_sources ss ON ss.statement_id = s.id "
               "JOIN evidence e ON e.id = ss.evidence_id WHERE substr(s.recorded_at, 1, 7) = ? GROUP BY code")
    try:
        for r in db.conn.execute(sql, (month,)):
            made[r["code"]] = r["n"]
    except sqlite3.OperationalError:
        pass
    out = []
    for code, name, sql, gap in registry:
        try:
            last = db.conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            last = None
        item = {"code": code, "name": name, "last": last, "every": gap, "facts_this_month": made.get(code, 0),
                "days": None, "state": "not_connected" if not last else "ok"}
        if last:
            try:
                t = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
                item["days"] = (now - t).days
            except ValueError:
                item["days"] = None
            if gap is not None and item["days"] is not None and item["days"] > gap:
                item["state"] = "stale"
        out.append(item)
    return out


def stale_sources(db, now: datetime = None) -> list:
    """体检用：断档的来源，一句话一个。"""
    return [f"{s['code']} {s['name']}：{s['days']} 天没有新记录（平时 {s['every']} 天内会有）" for s in source_health(db, now) if s["state"] == "stale"]



# ---------- 收藏里反复出现的话题 → “可能感兴趣”的候选（ontology-plan 1.6，S3 S7） ----------
# 收藏只说明她收藏过，不直接写成兴趣或偏好；同一话题 30 天里收藏 INTEREST_MIN 次以上、聊天里她也提到过，
# 才记一条系统推的“可能感兴趣”（kind = hypothesis），她在“我”页可以点“像我 / 不像我 / 说不清”。
INTEREST_MIN = 3


def write_interests(db, day=None) -> dict:
    from collections import Counter
    from . import clock
    ensure(db)
    day = day or clock.her_today()
    out = {"written": 0, "skipped": 0}
    try:
        rows = db.conn.execute("SELECT tags FROM brain_items WHERE kind IN ('saved', 'link') AND substr(created_at, 1, 10) >= ?",
                               ((day - timedelta(days=30)).isoformat(),)).fetchall()
    except sqlite3.OperationalError:
        return out
    tags = Counter(t.strip() for r in rows for t in json.loads(r["tags"] or "[]") if isinstance(t, str) and len(t.strip()) >= 2)
    since = (day - timedelta(days=60)).isoformat()
    for tag, n in tags.items():
        if n < INTEREST_MIN:
            continue
        if not db.conn.execute("SELECT 1 FROM evidence WHERE source = 'chat' AND deleted_at IS NULL AND occurred_at >= ? AND text LIKE ? LIMIT 1",
                               (since, f"%{tag}%")).fetchone():
            out["skipped"] += 1  # 只收藏、没聊到过：不算
            continue
        if db.conn.execute("SELECT 1 FROM facts WHERE prop = 'interest' AND statement LIKE ? AND valid_to IS NULL AND status != 'invalid'",
                           (f"%「{tag}」%",)).fetchone():
            continue
        try:
            ev = add_evidence(db, "link", f"interest:{tag}:{day.isoformat()[:7]}", day.isoformat(),
                              text=f"30 天里收藏了 {n} 条带「{tag}」的，聊天里也提到过")
        except Forgotten:
            continue
        add_fact(db, "preferences", f"可能对「{tag}」感兴趣（30 天收藏 {n} 条，聊天里也提过）", [ev], kind="hypothesis",
                 source_kind="system_inference", prop="interest", confidence=0.4, importance=2,
                 context="系统从收藏和聊天推的，她没说过")
        out["written"] += 1
    tx.commit(db)
    return out


# ---------- 收到的邮件里关于她的有效信息 → 说法（ontology-plan 1.6，11-04） ----------
# 录取、签证进度、预约改期、账户开通、案件进度：算第三方来源（other_person），语境写“谁的邮件里写的（哪天）”。
# 只写说法，不改她的待办和计划（1.5 节门禁 1）；邮件她删过的不会走到这里（证据记不进来）；
# 同一件事已经有（包括她说过“不对”的）就不再记；她锁过的不碰（这里只新增，不改旧的）。
MAIL_FACT_TYPES = {"application": ("resources", "application_status"), "visa": ("resources", "application_status"),
                   "case": ("resources", "application_status"), "account": ("resources", "account_status"),
                   "booking": ("rhythms", "appointment")}
# 进度的关键词：标题差不多、但状态变了（递交 → 获批）或日期变了（改期），算新进展，不当重复
NEWS_WORDS = re.compile(r"递交|提交|受理|补材料|补充材料|获批|批准|通过|拒|录取|候补|确认|改期|改到|取消|开通|关闭|停用|寄出|寄回|退款|到账|"
                        r"发货|签收|预约|面试|入学|注册|结案|立案|撤销")


def mail_sender_name(sender: str) -> str:
    """语境里写发件人：有名字写名字，没有写域名；不写完整邮箱地址（不留联系方式）。"""
    sender = (sender or "").strip()
    m = re.match(r'\s*"?([^"<]*?)"?\s*<([^>]*)>', sender)
    name, addr = (m.group(1).strip(), m.group(2)) if m else ("", sender)
    if name and "@" not in name:
        return name[:40]
    return (addr.split("@")[-1] if "@" in addr else addr)[:40] or "发件人不明"


def _same_news(new: str, old: str) -> bool:
    from .core.agent import similar_task
    if not similar_task(new, [old]):
        return False
    a, b = set(re.findall(r"\d+", new)), set(re.findall(r"\d+", old))
    if not (a <= b or b <= a):  # 日期、编号对不上：改期之类的新进展
        return False
    return set(NEWS_WORDS.findall(new)) == set(NEWS_WORDS.findall(old))


def write_mail_facts(db, evidence_id: int, sender: str, received_at: str, items) -> int:
    """一封收到的邮件里关于她的 0–3 条说法写进记忆。返回写了几条。"""
    ensure(db)
    who, day = mail_sender_name(sender), (received_at or _now())[:10]
    olds = [r["statement"] for r in db.conn.execute(
        "SELECT statement FROM facts WHERE subject = 'self' AND statement != '' AND (status = 'invalid' "
        "OR (status NOT IN ('deleted', 'suspended') AND valid_to IS NULL)) ORDER BY id DESC LIMIT 2000")]  # 她说过“不对”的也算有过
    n = 0
    for it in list(items or [])[:3]:
        text = " ".join(str((it or {}).get("statement") or "").split())
        dim, prop = MAIL_FACT_TYPES.get((it or {}).get("type"), ("resources", None))
        if len(text) < 4 or any(_same_news(text, o) for o in olds):
            continue
        try:
            add_fact(db, dim, text, [evidence_id], kind="fact", source_kind="other_person", prop=prop, valid_from=day,
                     confidence=0.7, importance=3, visibility="private", context=f"{who} 的邮件里写的（{day}）")
        except Forgotten:
            return n
        olds.insert(0, text)
        n += 1
    return n


# ---------- 整段对话里别人 / agent 说的关于她的 → 说法（ontology-plan 1.9，ethan/interviews.py，她 11-04） ----------
# other_person：别人对她的评价、提醒、和她约的事，语境“某某在微信里说的（日期）”；agent_relay：Grok 等给的建议和结论，
# 只能记成假设（check_kind），只当资料。都私密、只新增（不改、不作废她的说法，她锁过的不碰），同一件事已经有（包括她说过“不对”的）就不再记。
def write_said_facts(db, evidence_id: int, facts, session: str, day: str, props=()) -> dict:
    """facts：[{speaker, statement, dimension, property?, day?, source_kind: other_person / agent_relay}]。返回各写了几条。"""
    from .core.agent import similar_task
    ensure(db)
    got = {"other_person": 0, "agent_relay": 0}
    olds = [r["statement"] for r in db.conn.execute(
        "SELECT statement FROM facts WHERE subject = 'self' AND statement != '' AND (status = 'invalid' "
        "OR (status NOT IN ('deleted', 'suspended') AND valid_to IS NULL)) ORDER BY id DESC LIMIT 2000")]
    where = "微信里" if (session or "").startswith("微信") else f"“{session}”里"
    for f in facts or []:
        who, text, sk = str(f.get("speaker") or "").strip(), " ".join(str(f.get("statement") or "").split())[:60], f.get("source_kind")
        when = str(f.get("day") or day or _now())[:10]
        if sk not in got or f.get("dimension") not in DIMENSIONS or len(text) < 4 or similar_task(text, olds):
            continue
        try:
            if sk == "agent_relay":
                add_fact(db, f["dimension"], text, [evidence_id], kind="hypothesis", source_kind="agent_relay", valid_from=when,
                         confidence=0.3, importance=2, visibility="private", context=f"{who} 在{where}说的（{when}），只当资料")
            else:
                add_fact(db, f["dimension"], text, [evidence_id], kind="fact", source_kind="other_person",
                         prop=f.get("property") if f.get("property") in props else None, valid_from=when,
                         confidence=0.6, importance=3, visibility="private", context=f"{who}在{where}说的（{when}）")
        except Forgotten:
            break
        olds.insert(0, text)
        got[sk] += 1
    tx.commit(db)
    return got


# ---------- 老说法按已登记的属性回头整理（ontology-plan 1.11） ----------
# 老的说法大多只有“方面”（prop 为空，新结构里算 dim:xxx 总属性）。先用规则归，规则拿不准的、给了模型才让模型判；
# 只改 facts.prop（双写触发器同步 statements.property），不改内容、不删；她锁过的、已经有 prop 的都不动。
# 只归到值是一句话（text）、关于她本人的属性。lives_in（值是对象、单值、按“现在”答）、birthday（日期）、body_weight（数字）、
# 行为整理写的（work_start、outings_per_week……）、系统推的 interest 不在这里归，免得旧的一句话被当成“现在住哪 / 现在体重”。
REGROUP_TARGETS = ("studied_at", "worked_at", "project", "skill", "wake_time", "preference",
                   "application_status", "account_status", "appointment")
_PLAN = r"想|打算|计划|希望|准备要|考虑"
# 公司名样式不好判，worked_at 的“在 X 负责 / 待了、离开 X、X 的产品经理”只认常见公司名（宁缺勿滥）
_COMPANY = (r"(腾讯|字节跳动|字节|抖音|某短视频公司|某电商|虾皮|某资讯公司|今日头条|头条|阿里巴巴|阿里|蚂蚁|百度|美团|京东|网易|小红书|快手|"
            r"华为|小米|携程|滴滴|拼多多|微软|Microsoft|Google|谷歌|Amazon|亚马逊|Meta|Apple|苹果公司)")
_WORK_AT_CO = (rf"在\s*{_COMPANY}[^，。,；;]{{0,8}}?(负责|待了|待过|工作|上班|当|任|做过|带领|带|管理)|离开(了)?\s*{_COMPANY}|"
               rf"{_COMPANY}\s*的\s*(PM|产品经理|产品|运营|工程师|设计师|分析师|实习生|员工)|负责[^，。,；;]{{0,6}}{_COMPANY}")
_WORK_WORDS = (r"任职|就职|入职|在职|实习|全职|兼职|离职|工作过|上班|担任|雇主|"
               r"(在|于).{1,20}(公司|集团|银行|基金|咨询|事务所|工作室)(工作|任|做)|" + _WORK_AT_CO)
# 带金额的一笔流水（扣费、还款、退款、付款、收到、转账、年费……）：是交易记录，不是账户 / 申请的状态
_MONEY = r"\d[\d,，.]*\s*(元|块|万|英镑|镑|美元|美金|刀|欧元|港币|£|\$|¥|GBP|USD|EUR|RMB|CNY|HKD)|[£$¥€]\s*\d"
_TXN = r"扣费|扣款|扣了|还款|还了|退款|退了|退回|支付|付款|付了|交了|收到|转账|转了|消费|花了|年费|费"
# 某天哪笔钱进出：几月几日 / YYYY-MM-DD / MM-DD + 金额，或者一看就是流水的词（第二次试跑）
_DATE = r"\d{1,2}\s*月\s*\d{1,2}\s*日|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}|(?<![\d-])\d{1,2}-\d{1,2}(?![\d-])"
_TXN_WORDS = r"入账|购汇|转出|转入|charge|一笔|两笔|几笔|各有一笔|汇率"
# 求职：投简历、校招、面试、岗位……不是签证 / 录取 / 证件 / 账户的申请，也不是在哪任职
_JOB_HUNT = r"招聘|校招|校园招聘|实习生|岗位|简历|面试|职位|发邮件申请"
# worked_at 要说得出具体单位：常见公司名，或“在 / 于 X 公司 / 银行……”
_WORK_UNIT = _COMPANY + r"|(在|于)[^，。,；;]{1,20}(公司|集团|银行|基金|咨询|事务所|工作室)"
# (属性, 只看哪些方面, 必须都命中的关键词, 命中就不归的词)。一条命中两个属性 = 拿不准，留在“总的”（或交给模型）
# 正则管不到的排除（同句、带金额的流水、别人喜欢……）在 _regroup_blocked 里。总原则：宁可留在“总的”里，也不要归错
REGROUP_RULES = (
    ("studied_at", ("abilities", "resources", "values"),
     (r"本科|硕士|博士|进修|研究生|学士|学位|毕业于|就读|在读|读书|读的是|入学|主修|专业是|交换生|留学|(大学|学院|商学院|高中)(读|学|毕业)",),
     _PLAN + r"|喜欢读书|爱读书|读书会|申请|offer|录取|面试|认为|觉得|看法|以为|奖学金|获奖|俱乐部|社团|学生会|主席|部长"),
    ("worked_at", ("abilities", "resources"),
     (_WORK_WORDS,),
     _PLAN + r"|申请|面试|求职|找工作|投递|offer|投了|投简历|简历|招聘|看到[^。]{0,20}消息|(了解|学到|学会)了[^。]{0,20}流程"),
    ("project", ("abilities", "resources"),
     # 只提到“项目”不算（“项目因此停滞”）：要有做 / 牵头 / 立项 / 负责……项目这类动作
     (r"(做|在做|牵头|主导|发起|立项|负责|参与|开发|带)[^，。,；;]{0,15}项目|项目[^，。,；;]{0,6}(上线|立项|落地)|"
      r"创办|创立|创业|开发了|做了一个|做过一个|作品|参赛|比赛|获奖",),
     _PLAN + r"|停滞|搁置|耽误|因此|写邮件时|打不开|没人接|还不知道|晋级情况"),
    ("skill", ("abilities",),
     (r"会用|擅长|熟练|精通|掌握|会写|会说|技能|证书|雅思|托福|编程|Python|SQL|Excel|Figma|Photoshop",),
     _PLAN + r"|不会|不擅长|想学|在学"),
    ("wake_time", ("rhythms", "body"),
     (r"起床|醒来|早起|起来", r"\d|点|早上|上午|清晨"),
     r"睡觉|入睡|就寝|午睡|睡前|熬夜"),
    ("preference", ("preferences",),
     (r"喜欢|偏好|偏爱|爱吃|爱喝|爱看|不吃|不喝|讨厌|更喜欢|倾向于|宁愿|口味|习惯用|首选|雷区",),
     r"想过|打算|计划"),
    ("application_status", ("resources", "abilities"),
     (r"申请|签证|offer|录取|递交|获批|审批|BRP|eVisa|拒信|候补",),
     _PLAN + r"|支付|付款|付了|缴费|扣费|退款|" + _JOB_HUNT),
    ("account_status", ("resources",),
     (r"开户|开通|账户|银行卡|信用卡|注册了|额度",),
     _PLAN + r"|账单|消费|花了|余额|还款|扣费|扣款|退款|年费|挂失|转账|会员"),
    ("appointment", ("resources", "body", "rhythms"),
     (r"预约|预订|挂号|订单|订了",),
     _PLAN),
)


def _same_sentence(text: str, a: str, b: str) -> bool:
    return any(re.search(a, x, re.I) and re.search(b, x, re.I) for x in re.split(r"[。！？!?；;\n]", text))


def _regroup_blocked(prop: str, text: str) -> bool:
    """关键词命中了但其实不是这个属性的几种情况（服务器试跑里归错的）。"""
    if prop in ("account_status", "application_status") and re.search(_MONEY, text, re.I) and re.search(_TXN, text):
        return True  # 带金额的一笔流水
    if prop == "account_status" and (re.search(_TXN_WORDS, text, re.I) or (re.search(_DATE, text) and re.search(_MONEY, text, re.I))):
        return True  # 某天哪笔钱进出（入账、购汇、某日某笔 Charge）：流水，不是账户状态
    if prop == "worked_at" and not re.search(_WORK_UNIT, text, re.I):
        return True  # 没说在哪个单位（“实习的工作地点很灵活”）：不知道是哪段任职
    if prop == "application_status" and _same_sentence(text, r"offer|录取", r"工作|入职|求职|公司|上班|实习"):
        return True  # “工作一年半时拿到过某公司的 offer”：过去的求职经历
    if prop == "preference":
        if re.search(_WORK_WORDS, text, re.I):
            return True  # “在某公司只待了一年就离开，因为不喜欢……”：工作经历
        if re.search(r"(因为|由于|原因是)[^，。,；;]*(喜欢|讨厌)", text):
            return True  # 喜欢 / 不喜欢 出现在原因从句里
        if re.search(r"(?<![她我最更很也都不挺蛮还就])(喜欢|讨厌)的", text) and not re.search(r"(自己|本人)(最|更)?喜欢的", text):
            return True  # “女性喜欢的耳饰”：别人喜欢的，修饰名词
    if prop == "project" and re.search(_WORK_AT_CO, text, re.I):
        return True  # “在某公司负责……”：算任职，不算项目
    return False


UNSURE = "不确定"
REGROUP_BATCH = 30


def regroup_rule(dimension: str, statement: str) -> list:
    """规则：这条说法能归到哪些属性（0 个 = 规则没把握，2 个以上 = 拿不准）。"""
    out = []
    for prop, dims, must, never in REGROUP_RULES:
        if dimension not in dims:
            continue
        text = statement or ""
        if all(re.search(p, text, re.I) for p in must) and not (never and re.search(never, text, re.I)) \
                and not _regroup_blocked(prop, text):
            out.append(prop)
    return out


def _regroup_ok(db, f, prop: str) -> bool:
    """归过去以后不能少一道权限：身体、钱、关系、性格这几类的，只能去同样敏感的属性，或者本身就是私密级的。"""
    if f["dimension"] not in NEVER_PUBLIC or (f["visibility"] or "private") == "private":
        return True
    r = db.conn.execute("SELECT sensitive FROM properties WHERE key = ?", (prop,)).fetchone()
    return bool(r and r[0] == f["dimension"])


def _regroup_model(router, db, rows) -> dict:
    """规则拿不准的交给模型：只能在 REGROUP_TARGETS 里选或填“不确定”。返回 {fact_id: 属性}（不确定的不放进来）。"""
    from . import llm
    version, system = llm.load_prompt("prop_regroup")
    enum = list(REGROUP_TARGETS) + [UNSURE]
    schema = {"type": "array", "maxItems": REGROUP_BATCH, "items": {"type": "object", "required": ["id", "property"], "properties": {
        "id": {"type": "integer"}, "property": {"type": "string", "enum": enum}, "why": {"type": "string", "maxLength": 80}}}}
    got = {}
    for i in range(0, len(rows), REGROUP_BATCH):
        batch = rows[i:i + REGROUP_BATCH]
        ids = {r["id"] for r in batch}
        user = "\n".join(f"#{r['id']}（方面：{DIM_NAMES.get(r['dimension'], r['dimension'])}）{r['statement']}" for r in batch)
        try:
            data = llm.structured(router, db, "prop_regroup", system, user, schema, version=version, input_ref=f"facts:{min(ids)}-{max(ids)}",
                                  check=lambda d: [f"#{x['id']} 不在这批里" for x in d if x["id"] not in ids], purpose="prop-regroup")
        except Exception:
            data = None
        for x in data or []:
            if x["property"] != UNSURE:
                got[x["id"]] = x["property"]
    return got


def regroup_props(db, router=None, dry_run: bool = True) -> dict:
    """ontology-plan 1.11 回头整理：老说法（关于她本人、prop 为空）按已登记的属性重新归一次，拆不准的留在“总的”里。
    dry_run=True 只算不写。写的时候一个事务做完，出错整体回滚；每条改动在 statement_changes 记一笔（field=property），能按它退回。
    返回 {changed: 改了几条（dry_run 时是会改几条）, by_prop, by_rule, by_model, left: 留在总的几条, locked: 锁着没动的, samples}。"""
    from . import locks, ontology
    ensure(db)
    ontology.ensure(db)
    locks.ensure(db)
    rows = db.conn.execute("SELECT id, dimension, statement, visibility, locked FROM facts WHERE subject = 'self' "
                           "AND (prop IS NULL OR prop = '') AND status != 'deleted' AND COALESCE(statement, '') != '' ORDER BY id").fetchall()
    got = {"dry_run": dry_run, "changed": 0, "by_prop": {}, "by_rule": 0, "by_model": 0, "left": 0, "locked": 0, "samples": []}
    plan, unsure = {}, []
    for f in rows:
        if f["locked"] or locks.locked(db, "fact", f["id"], "prop"):
            got["locked"] += 1
            continue
        hit = [p for p in regroup_rule(f["dimension"], f["statement"]) if _regroup_ok(db, f, p)]
        if len(hit) == 1:
            plan[f["id"]] = (hit[0], "rule")
        else:
            unsure.append(f)
    if router is not None and unsure:
        by_id = {f["id"]: f for f in unsure}
        for fid, prop in _regroup_model(router, db, unsure).items():
            if fid in by_id and prop in REGROUP_TARGETS and _regroup_ok(db, by_id[fid], prop):
                plan[fid] = (prop, "model")
    by_id = {f["id"]: f for f in rows}
    for fid, (prop, how) in sorted(plan.items()):
        got["by_prop"][prop] = got["by_prop"].get(prop, 0) + 1
        got["by_rule" if how == "rule" else "by_model"] += 1
        if len(got["samples"]) < 12:
            got["samples"].append({"id": fid, "statement": by_id[fid]["statement"], "dimension": by_id[fid]["dimension"], "prop": prop, "how": how})
    got["changed"] = len(plan)
    got["left"] = len(rows) - got["locked"] - len(plan)
    if dry_run or not plan:
        return got
    if db.conn.in_transaction:
        db.conn.commit()
    db.conn.execute("BEGIN IMMEDIATE")
    try:
        n = 0
        for fid, (prop, _) in sorted(plan.items()):
            old = db.conn.execute("SELECT s.id, s.property FROM fact_map m JOIN statements s ON s.id = m.statement_id WHERE m.fact_id = ?",
                                  (fid,)).fetchone()
            # 再判一次条件，防这期间她锁了或者别处填了属性
            done = db.conn.execute("UPDATE facts SET prop = ? WHERE id = ? AND (prop IS NULL OR prop = '') AND COALESCE(locked, 0) = 0",
                                   (prop, fid)).rowcount
            n += done
            if done and old and old["property"] != prop:
                db.conn.execute("INSERT INTO statement_changes (statement_id, field, old, new, at) VALUES (?, 'property', ?, ?, ?)",
                                (old["id"], old["property"], prop, _now()))
        db.conn.commit()
    except BaseException:
        db.conn.rollback()
        raise
    got["changed"] = n
    return got
