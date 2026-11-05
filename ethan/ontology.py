"""ontology 新骨架：迁移第 1 步，只建表、登记元数据（docs/design/ontology-structure.md 9.1 节、第 10 节）。

她 2026-11-03 定“10 类全做，分四步迁”；Codex 第二轮评审的条件是第 1 步“仅建新表和登记元数据，不改旧记录、不接现有读写”。
所以这里只有：建表、登记 10 类对象类型和首版属性、建“她”这个对象。**不读也不写旧的 facts**，现有行为不变。
规则以 docs/design/ontology-contracts.md 为准。
"""

import json
from datetime import datetime, timezone

from . import tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS objects (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'merged', 'forgotten')),
  merged_into INTEGER REFERENCES objects(id), created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS object_types (object_id INTEGER NOT NULL REFERENCES objects(id), type TEXT NOT NULL,
  PRIMARY KEY (object_id, type));
CREATE TABLE IF NOT EXISTS aliases (alias TEXT NOT NULL, object_id INTEGER NOT NULL REFERENCES objects(id), source TEXT,
  PRIMARY KEY (alias, object_id));
CREATE TABLE IF NOT EXISTS situation_roles (situation_id INTEGER NOT NULL REFERENCES objects(id),
  object_id INTEGER NOT NULL REFERENCES objects(id), role TEXT NOT NULL, PRIMARY KEY (situation_id, object_id, role));
CREATE TABLE IF NOT EXISTS properties (key TEXT PRIMARY KEY, name TEXT NOT NULL,
  value_kind TEXT NOT NULL CHECK (value_kind IN ('text', 'number', 'date', 'object')),
  cardinality TEXT NOT NULL CHECK (cardinality IN ('one', 'many')),
  unit TEXT, min_value REAL, max_value REAL, applies_to TEXT NOT NULL,
  sensitive TEXT, review_every TEXT NOT NULL DEFAULT 'on_use' CHECK (review_every IN ('never', 'quarter', 'on_use')),
  status TEXT NOT NULL DEFAULT 'trial' CHECK (status IN ('trial', 'active', 'retired')));
CREATE TABLE IF NOT EXISTS statements (id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_id INTEGER NOT NULL REFERENCES objects(id), property TEXT NOT NULL REFERENCES properties(key),
  value_text TEXT, value_num REAL, value_object INTEGER REFERENCES objects(id),
  no_value TEXT CHECK (no_value IN ('unknown', 'none', 'not_applicable', 'unasked')),
  context TEXT, kind TEXT NOT NULL, said_by TEXT NOT NULL,
  valid_from TEXT, valid_to TEXT, recorded_at TEXT NOT NULL, retracted_at TEXT,
  review TEXT NOT NULL DEFAULT 'candidate' CHECK (review IN ('candidate', 'accepted', 'disputed', 'retracted', 'forgotten')),
  superseded_by INTEGER REFERENCES statements(id),
  CHECK ((value_text IS NOT NULL) + (value_num IS NOT NULL) + (value_object IS NOT NULL) + (no_value IS NOT NULL) = 1));
CREATE TABLE IF NOT EXISTS statement_changes (id INTEGER PRIMARY KEY AUTOINCREMENT,
  statement_id INTEGER NOT NULL REFERENCES statements(id), field TEXT NOT NULL, old TEXT, new TEXT, at TEXT NOT NULL, action_id INTEGER);
CREATE TABLE IF NOT EXISTS statement_sources (statement_id INTEGER NOT NULL REFERENCES statements(id),
  evidence_id INTEGER, from_statement INTEGER REFERENCES statements(id),
  role TEXT NOT NULL CHECK (role IN ('supports', 'contradicts', 'quotes', 'derived')),
  source_unknown INTEGER NOT NULL DEFAULT 0,
  CHECK (evidence_id IS NOT NULL OR from_statement IS NOT NULL OR source_unknown = 1));
CREATE TABLE IF NOT EXISTS action_log (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, target TEXT NOT NULL,
  basis TEXT NOT NULL, action_key TEXT NOT NULL UNIQUE, actor TEXT NOT NULL, at TEXT NOT NULL,
  result TEXT NOT NULL CHECK (result IN ('done', 'rejected', 'pending_confirm', 'failed')));
CREATE TABLE IF NOT EXISTS object_refs (ref TEXT PRIMARY KEY, object_id INTEGER NOT NULL REFERENCES objects(id));
CREATE TABLE IF NOT EXISTS fact_map (fact_id INTEGER NOT NULL, statement_id INTEGER NOT NULL REFERENCES statements(id),
  PRIMARY KEY (fact_id, statement_id));
"""

# 第 4 步：说法也带权限（从旧 facts 同步）：可见级别、读取名单、她锁定的
STATEMENT_COLUMNS = (("visibility", "TEXT"), ("read_by", "TEXT"), ("locked", "INTEGER NOT NULL DEFAULT 0"))

# 10 类对象（ontology-structure.md 2.1 节，她 11-03 定全做）
OBJECT_TYPES = {"self": "她", "person": "人", "place": "地点", "org": "组织", "event": "事件", "situation": "情境",
                "goal": "目标链", "habit": "习惯", "decision": "决定", "topic": "话题"}

# 首版属性（试验状态）：先登记最常用、能力题首批要用的几个，其余按真实缺口长（ontology-structure.md 第 6 节）
# (key, 名字, 值类型, 单值/多值, 单位, 能用在哪些类型, 敏感标签, 多久复核)
PROPERTIES = (
    ("lives_in", "住在哪个城市", "object", "one", None, ["self", "person"], None, "quarter"),
    ("located_in", "位于", "object", "one", None, ["org", "place", "event"], None, "never"),
    ("situation_start", "情境开始日", "date", "one", None, ["situation"], None, "never"),
    ("situation_end", "情境结束日", "date", "one", None, ["situation"], None, "on_use"),
    ("relation_to_her", "和她的关系", "text", "many", None, ["person", "org"], "relationships", "on_use"),
    ("preference", "偏好", "text", "many", None, ["self"], None, "on_use"),
    ("wake_time", "起床时间", "text", "one", None, ["self"], None, "on_use"),
    ("birthday", "生日", "date", "one", None, ["self", "person"], "body", "never"),
    ("body_weight", "体重", "number", "one", "kg", ["self"], "body", "on_use"),
    ("weekly_spend", "每周开销", "number", "one", "GBP", ["self"], "resources", "on_use"),
    # 1.2 文件整理要用的最小属性（她的经历）：值写成一句话（学校、专业、起止），多值
    ("studied_at", "就读", "text", "many", None, ["self", "person"], None, "never"),
    ("worked_at", "任职", "text", "many", None, ["self", "person"], None, "quarter"),
    ("project", "做过的项目", "text", "many", None, ["self"], None, "quarter"),
    ("skill", "技能", "text", "many", None, ["self"], None, "on_use"),
    # 1.5 行为记录每周整理（lifebase.write_behavior）：只描述，值是一句带样本数的话
    ("work_start", "开始用电脑的时间", "text", "one", None, ["self"], None, "on_use"),
    ("sleep_late", "晚上最后用电脑的时间", "text", "one", None, ["self"], None, "on_use"),
    ("outings_per_week", "每周出门天数", "text", "one", None, ["self"], None, "on_use"),
    ("calendar_per_week", "每周日历安排数", "text", "one", None, ["self"], None, "on_use"),
    ("workbench_edits", "工作台上亲手改的次数", "text", "one", None, ["self"], None, "on_use"),
    # 1.6：她对系统输出的反应（次数）、收藏里推出来的可能感兴趣
    ("reaction_prep", "对替她准备的事的反应", "text", "one", None, ["self"], None, "on_use"),
    ("reaction_diary", "对日记初稿的反应", "text", "one", None, ["self"], None, "on_use"),
    ("reaction_mail", "对邮件草稿的反应", "text", "one", None, ["self"], None, "on_use"),
    ("reaction_plan", "对计划里排的事的反应", "text", "one", None, ["self"], None, "on_use"),
    ("interest", "可能感兴趣的话题", "text", "many", None, ["self"], None, "on_use"),
    # 1.6：收到的邮件里关于她的有效信息（lifebase.write_mail_facts）：值是一句带日期和机构的话
    ("application_status", "申请和办理进度", "text", "many", None, ["self"], "resources", "on_use"),
    ("account_status", "账户和服务开通", "text", "many", None, ["self"], "resources", "on_use"),
    ("appointment", "预约和订单", "text", "many", None, ["self"], None, "on_use"),
)
BACKGROUND = ("studied_at", "worked_at", "project", "skill")
# 迁移第 2 步（她 11-03 同意）：旧记忆只分了 10 个方面，没有细属性，先给每个方面配一个“总的”属性（多值），
# 同一类攒多了、要细分时再拆出具体属性。敏感标签沿用 NEVER_PUBLIC 那几个方面。
DIM_PROPERTY = "dim:{}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db) -> dict:
    """建表、登记属性、建“她”。可以重复跑，不会重复登记。返回这次新加了什么。"""
    if tx.once(db, "ontology"):
        db.conn.executescript(SCHEMA)
        have = {r[1] for r in db.conn.execute("PRAGMA table_info(statements)")}
        for col, ddl in STATEMENT_COLUMNS:
            if col not in have:
                db.conn.execute(f"ALTER TABLE statements ADD COLUMN {col} {ddl}")
    added = {"properties": 0, "self": False}
    for key, name, kind, card, unit, applies, sensitive, review in PROPERTIES:
        added["properties"] += db.conn.execute(
            "INSERT OR IGNORE INTO properties (key, name, value_kind, cardinality, unit, applies_to, sensitive, review_every) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (key, name, kind, card, unit, json.dumps(applies), sensitive, review)).rowcount
    from . import lifebase
    for dim in lifebase.DIMENSIONS:
        added["properties"] += db.conn.execute(
            "INSERT OR IGNORE INTO properties (key, name, value_kind, cardinality, applies_to, sensitive, review_every) "
            "VALUES (?, ?, 'text', 'many', '[\"self\"]', ?, 'on_use')",
            (DIM_PROPERTY.format(dim), lifebase.DIM_NAMES.get(dim, dim) + "（总）", dim if dim in lifebase.NEVER_PUBLIC else None)).rowcount
    if self_id(db) is None:
        cur = db.conn.execute("INSERT INTO objects (name, created_at) VALUES ('她', ?)", (_now(),))
        db.conn.execute("INSERT INTO object_types (object_id, type) VALUES (?, 'self')", (cur.lastrowid,))
        added["self"] = True
    db.conn.execute("INSERT OR IGNORE INTO object_refs (ref, object_id) VALUES ('self', ?)", (self_id(db),))
    tx.commit(db)
    return added


def self_id(db):
    r = db.conn.execute("SELECT o.id FROM objects o JOIN object_types t ON t.object_id = o.id WHERE t.type = 'self' "
                        "AND o.status = 'active' ORDER BY o.id LIMIT 1").fetchone()
    return r[0] if r else None


def object_for_ref(db, ref: str):
    """旧数据里的主语（'self' / 'person:<人物卡编号>'）对应的对象；人物卡第一次用到时建一个“人”对象（一张人物卡一个，不合并）。
    不提交：跟着调用方的事务走。"""
    try:
        r = db.conn.execute("SELECT object_id FROM object_refs WHERE ref = ?", (ref,)).fetchone()
    except Exception:
        return None  # 新表还没建（老的测试库）：不同步，第 3 步迁移时补
    if r:
        return r[0]
    if not ref.startswith("person:"):
        return None
    pid = int(ref.split(":", 1)[1])
    try:
        row = db.conn.execute("SELECT name FROM people WHERE id = ?", (pid,)).fetchone()
    except Exception:
        row = None
    cur = db.conn.execute("INSERT INTO objects (name, created_at) VALUES (?, ?)", (row[0] if row else f"人物 #{pid}", _now()))
    db.conn.execute("INSERT INTO object_types (object_id, type) VALUES (?, 'person')", (cur.lastrowid,))
    db.conn.execute("INSERT INTO object_refs (ref, object_id) VALUES (?, ?)", (ref, cur.lastrowid))
    if row:
        db.conn.execute("INSERT OR IGNORE INTO aliases (alias, object_id, source) VALUES (?, ?, 'people')", (row[0], cur.lastrowid))
    return cur.lastrowid
