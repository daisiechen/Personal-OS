"""权益与保障（docs/design/digital-life-spec.md 第 2.7 节、第 9 节第 7 步）。

会员和等级（航司、酒店、银行、信用卡、零售、订阅）和保险，各一张表。界面等“我”页定下来再做（先不动），
现在先做到：
- 记下来：她发截图、邮件或者跟 小伴 说，小伴 在回复里写 [会员 …] / [保险 …]（键=值），代码校验后新增或更新；
  卡号、会员号、保单号只留后四位，给了整串也只存后四位。
- 用起来：小伴 每轮看得到她有哪些会员、等级、积分和保险（订机票酒店、出门、生病时用）。
- 到期提醒：积分过期、等级到期、续费、保险续保，提前 30 天、7 天、1 天各提醒一次（白天，经主动消息的免打扰规则）。
保级进度和薅羊毛等这块数据多了再做（规格第 9 节第 7 步后半）。
"""

import json
import re
from datetime import date, datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS memberships (
    id INTEGER PRIMARY KEY, program TEXT NOT NULL UNIQUE, category TEXT NOT NULL DEFAULT 'other', tier TEXT,
    member_no_last4 TEXT, points REAL, points_expire_at TEXT, tier_expire_at TEXT, tier_progress REAL, tier_target REAL,
    renew_at TEXT, annual_fee REAL, currency TEXT, benefits TEXT, status TEXT NOT NULL DEFAULT 'active',
    evidence_ids TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policies (
    id INTEGER PRIMARY KEY, type TEXT NOT NULL, insurer TEXT NOT NULL, policy_no_last4 TEXT, coverage TEXT,
    sum_insured TEXT, excess TEXT, premium REAL, premium_period TEXT, currency TEXT, start_at TEXT, renew_at TEXT,
    auto_renew INTEGER, status TEXT NOT NULL DEFAULT 'active', evidence_ids TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL);
"""
CATEGORIES = ("airline", "hotel", "bank", "card", "retail", "subscription", "other")
POLICY_TYPES = ("health", "travel", "contents", "life", "critical_illness", "car", "phone", "other")
STATUSES = ("active", "lapsed", "cancelled")
MARK = re.compile(r"\[(会员|保险)\s+([^\]]+)\]")
PAIR = re.compile(r"(\S+?)=(“[^”]*”|\"[^\"]*\"|\S+)")
# 标记里的中文键 → 表字段
M_KEYS = {"项目": "program", "类别": "category", "等级": "tier", "尾号": "member_no_last4", "积分": "points",
          "积分到期": "points_expire_at", "等级到期": "tier_expire_at", "保级进度": "tier_progress", "保级门槛": "tier_target",
          "续费": "renew_at", "年费": "annual_fee", "币种": "currency", "权益": "benefits", "状态": "status"}
P_KEYS = {"类型": "type", "公司": "insurer", "尾号": "policy_no_last4", "保什么": "coverage", "保额": "sum_insured",
          "免赔": "excess", "保费": "premium", "周期": "premium_period", "币种": "currency", "生效": "start_at",
          "续保": "renew_at", "自动续保": "auto_renew", "状态": "status"}
DATES = {"points_expire_at", "tier_expire_at", "renew_at", "start_at"}
NUMBERS = {"points", "tier_progress", "tier_target", "annual_fee", "premium"}
LAST4 = {"member_no_last4", "policy_no_last4"}
AHEAD = (30, 7, 1)  # 提前几天提醒
WHAT = {"points_expire_at": "积分过期", "tier_expire_at": "等级到期", "renew_at": "续费"}


def ensure(db):
    db.conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean(field, value):
    """校验一个值；不合格抛 ValueError。"""
    value = value.strip().strip("“”\"")
    if field in DATES:
        return date.fromisoformat(value).isoformat()
    if field in NUMBERS:
        return float(value.replace(",", ""))
    if field in LAST4:
        digits = re.sub(r"\D", "", value)
        if len(digits) < 4:
            raise ValueError("尾号要 4 位数字")
        return digits[-4:]  # 整串也只留后四位
    if field == "category" and value not in CATEGORIES:
        raise ValueError(f"类别只能是 {CATEGORIES}")
    if field == "type" and value not in POLICY_TYPES:
        raise ValueError(f"保险类型只能是 {POLICY_TYPES}")
    if field == "status" and value not in STATUSES:
        raise ValueError(f"状态只能是 {STATUSES}")
    if field == "auto_renew":
        return 1 if value in ("是", "yes", "true", "1", "自动") else 0
    return value[:300]


def parse(body: str, keys: dict) -> tuple:
    """“键=值 键=值” → (字段字典, 错误列表)。"""
    fields, errors = {}, []
    for k, v in PAIR.findall(body):
        if k not in keys:
            errors.append(f"不认识的键：{k}")
            continue
        try:
            fields[keys[k]] = _clean(keys[k], v)
        except ValueError as e:
            errors.append(f"{k}：{e}")
    return fields, errors


def _upsert(db, table, fields, match_sql, match_args, evidence_id=None):
    row = db.conn.execute(f"SELECT id, evidence_ids FROM {table} WHERE {match_sql}", match_args).fetchone()
    if row:
        if evidence_id:
            fields["evidence_ids"] = json.dumps(sorted(set(json.loads(row["evidence_ids"])) | {evidence_id}))
        sets = ", ".join(f"{k} = ?" for k in fields)
        db.conn.execute(f"UPDATE {table} SET {sets}, updated_at = ? WHERE id = ?", (*fields.values(), _now(), row["id"]))
        return row["id"], "updated"
    fields["evidence_ids"] = json.dumps([evidence_id] if evidence_id else [])
    cols = ", ".join(fields) + ", updated_at"
    cur = db.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({', '.join('?' * (len(fields) + 1))})", (*fields.values(), _now()))
    return cur.lastrowid, "added"


def note(db, text: str, evidence_id: int = None) -> list:
    """小伴 回复里的 [会员 …] / [保险 …]。返回 [(类别, 名字, added|updated|错误说明)]。"""
    ensure(db)
    out = []
    for kind, body in MARK.findall(text):
        if kind == "会员":
            fields, errors = parse(body, M_KEYS)
            if not fields.get("program"):
                out.append(("会员", "?", "缺 项目=")); continue
            _, how = _upsert(db, "memberships", fields, "program = ?", (fields["program"],), evidence_id)
            out.append(("会员", fields["program"], how + (f"（没记的：{'；'.join(errors)}）" if errors else "")))
        else:
            fields, errors = parse(body, P_KEYS)
            if not fields.get("insurer") or not fields.get("type"):
                out.append(("保险", fields.get("insurer", "?"), "缺 公司= 或 类型=")); continue
            match = "insurer = ? AND type = ? AND COALESCE(policy_no_last4, '') = ?"
            _, how = _upsert(db, "policies", fields, match, (fields["insurer"], fields["type"], fields.get("policy_no_last4", "")), evidence_id)
            out.append(("保险", fields["insurer"], how + (f"（没记的：{'；'.join(errors)}）" if errors else "")))
    db.conn.commit()
    return out


def strip(text: str) -> str:
    return MARK.sub("", text)


def active(db) -> tuple:
    ensure(db)
    m = [dict(r) for r in db.conn.execute("SELECT * FROM memberships WHERE status = 'active' ORDER BY category, program")]
    p = [dict(r) for r in db.conn.execute("SELECT * FROM policies WHERE status = 'active' ORDER BY type, insurer")]
    return m, p


def brief(db) -> str:
    """给 小伴 每轮看的一小段。"""
    m, p = active(db)
    lines = []
    for r in m:
        bits = [r["tier"] or "", f"积分 {r['points']:g}" if r["points"] is not None else "",
                f"积分 {r['points_expire_at']} 过期" if r["points_expire_at"] else "", f"等级到 {r['tier_expire_at']}" if r["tier_expire_at"] else "",
                f"保级 {r['tier_progress']:g}/{r['tier_target']:g}" if r["tier_progress"] is not None and r["tier_target"] else "",
                f"{r['renew_at']} 续费" if r["renew_at"] else "", r["benefits"] or ""]
        lines.append(f"- {r['program']}（{r['category']}）：" + "，".join(b for b in bits if b))
    for r in p:
        bits = [r["coverage"] or "", f"保额 {r['sum_insured']}" if r["sum_insured"] else "", f"免赔 {r['excess']}" if r["excess"] else "",
                f"{r['renew_at']} 续保" + ("（自动）" if r["auto_renew"] else "") if r["renew_at"] else ""]
        lines.append(f"- 保险 {r['insurer']}（{r['type']}）：" + "，".join(b for b in bits if b))
    return "\n".join(lines)


def due(db, today: date) -> list:
    """到期提醒：[(提醒键, 一句说明)]，按最近的排。每个到期日在提前 30 / 7 / 1 天各出现一次（键里带档位，提过就不再提）。"""
    m, p = active(db)
    out = []
    items = [(f"m{r['id']}", r["program"], f, r[f]) for r in m for f in WHAT if r[f]]
    items += [(f"p{r['id']}", f"{r['insurer']} 的{r['type']}保险", "renew_at", r["renew_at"]) for r in p if r["renew_at"] and not r["auto_renew"]]
    for rid, name, field, when in items:
        left = (date.fromisoformat(when) - today).days
        if not 0 <= left <= max(AHEAD):
            continue
        step = min(a for a in AHEAD if left <= a)  # 落在哪一档：30 天内、7 天内、1 天内
        what = "续保" if rid.startswith("p") else WHAT[field]
        out.append((left, f"perk:{rid}:{field}:{when}:{step}", f"{name}：{when} {what}（还有 {left} 天）"))
    return [(k, t) for _, k, t in sorted(out)]


PROMPT = ("她的会员、等级、积分和保险由你记：她发截图、邮件或者说到时，在回复末尾写 [会员 项目=名字 类别=airline/hotel/bank/card/retail/subscription/other "
          "等级=… 积分=数字 积分到期=YYYY-MM-DD 等级到期=YYYY-MM-DD 保级进度=数字 保级门槛=数字 续费=YYYY-MM-DD 年费=数字 币种=GBP 权益=“要点” 尾号=后四位]，"
          "或 [保险 类型=health/travel/contents/life/critical_illness/car/phone/other 公司=名字 保什么=“要点” 保额=… 免赔=… 保费=数字 周期=month/year "
          "生效=YYYY-MM-DD 续保=YYYY-MM-DD 自动续保=是/否 尾号=后四位]（只写知道的键，值里有空格用引号）；已经记过的同一项目再写就是更新。"
          "卡号、会员号、保单号只写后四位，完整号码不要写进回复。订机票酒店、出门、看病时，想想她有哪些等级和权益能用上。")
