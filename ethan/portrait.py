"""画像（docs/design/digital-life-spec.md 第 3 节、4.2 节，2026-10-30 她认可）。

三件事：
1. 每晚 fact_ops：每晚整理出的“今天新知道的”，逐条对照库里相关的旧事实，由模型提议 新增 / 更新 / 作废 / 已知，代码校验后执行。
   更新 = 旧的填失效时间和取代者，再新记一条；作废只填失效时间。什么都不删。
2. 画像摘要 profile_blocks：每个维度一段，小伴 每轮聊天都带着。事实不多时直接按重要度列出（不调模型）；
   超过篇幅才让模型浓缩，浓缩结果必须用到全部重要度 ≥ 4 的事实、篇幅合格，不然退回直接列出。每次改写都存进 profile_history。
3. 每周复盘（周日夜里随每晚整理跑）：
   - 代码：180 天没再证实的事实降可信度（×0.8，最低 0.3）；
   - 模型：找互相矛盾的两条（代码只允许作废较早的那条）、给 [待定] 和放错维度的事实归维度、挑 3–5 个确认问题；
   - 确认问题进工作台“待你定”（mepage.add_question，2026-10-31 两套合一），小伴 聊天时也从那里挑着问。
     问过或 7 天没问就过期。
运行：python3 -m ethan.portrait --data <数据目录> [--weekly] [--blocks]
"""

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import lifebase, llm
from .clock import her_today

BLOCK_BUDGET = 600  # 每个维度摘要的字数上限
STALE_DAYS = 180

DIM = {"type": "string", "enum": list(lifebase.DIMENSIONS)}
OPS_SCHEMA = {"type": "object", "required": ["ops"], "properties": {"ops": {"type": "array", "maxItems": 60, "items": {
    "type": "object", "required": ["op", "from"], "properties": {
        "op": {"type": "string", "enum": ["ADD", "UPDATE", "INVALIDATE", "NOOP"]}, "from": {"type": "string"},
        "fact_id": {"type": ["integer", "null"]}, "dimension": {"type": ["string", "null"], "enum": list(lifebase.DIMENSIONS) + [None]},
        "statement": {"type": ["string", "null"], "maxLength": 80}, "valid_from": {"type": ["string", "null"]},
        "reason": {"type": ["string", "null"], "maxLength": 80}}}}}}
BLOCK_SCHEMA = {"type": "object", "required": ["summary", "used_ids"], "properties": {
    "summary": {"type": "string"}, "used_ids": {"type": "array", "items": {"type": "integer"}}}}
WEEKLY_SCHEMA = {"type": "object", "required": ["contradictions", "reclassify", "questions"], "properties": {
    "contradictions": {"type": "array", "maxItems": 20, "items": {"type": "object", "required": ["old_id", "new_id"], "properties": {
        "old_id": {"type": "integer"}, "new_id": {"type": "integer"}, "reason": {"type": ["string", "null"], "maxLength": 80}}}},
    "reclassify": {"type": "array", "maxItems": 50, "items": {"type": "object", "required": ["fact_id", "dimension"], "properties": {
        "fact_id": {"type": "integer"}, "dimension": DIM}}},
    "questions": {"type": "array", "maxItems": 5, "items": {"type": "object", "required": ["question"], "properties": {
        "fact_id": {"type": ["integer", "null"]}, "question": {"type": "string", "maxLength": 60}}}}}}
QUESTIONS_SQL = """CREATE TABLE IF NOT EXISTS profile_questions (
    id INTEGER PRIMARY KEY, fact_id INTEGER, question TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL, closed_at TEXT)"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    lifebase.ensure(db)
    llm.ensure(db)
    db.conn.execute(QUESTIONS_SQL)


def _fact(db, fid):
    r = db.conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
    return dict(r) if r else None


def _current(f) -> bool:
    return bool(f) and f["valid_to"] is None and f["status"] != "invalid"


# ---------- 1. 每晚 fact_ops ----------
def related(db, statements: list, per: int = 5) -> list:
    """每条新说法在库里最像的几条旧事实（检索 v2，只要仍成立的）。"""
    from . import memsearch
    memsearch.refresh(db)
    ids = []
    for s in statements:
        for r in memsearch.search(db, [s], "chat", limit=per, kinds=("fact",), log=False, user="system"):
            fid = int(r["key"].split(":")[1])
            if fid not in ids:
                ids.append(fid)
    return [f for f in (_fact(db, i) for i in ids) if _current(f)]


def ops_input(new: list, old: list, day: date) -> str:
    a = "\n".join(f"n{i + 1}. {s}" for i, s in enumerate(new))
    b = "\n".join(f"{f['id']}. [{f['dimension']}] {f['statement']}（{(f['valid_from'] or f['recorded_at'] or '')[:10]}）" for f in old) or "（没有相关的）"
    return f"今天：{day.isoformat()}\n\n【今天新知道的】\n{a}\n\n【库里已有的】\n{b}"


def check_ops(data: dict, new: list, old_ids: set) -> list:
    errors = []
    for i, op in enumerate(data["ops"]):
        if op["from"] not in {f"n{k + 1}" for k in range(len(new))}:
            errors.append(f"ops[{i}].from 不是今天新知道的编号")
        if op["op"] in ("UPDATE", "INVALIDATE", "NOOP") and op.get("fact_id") not in old_ids:
            errors.append(f"ops[{i}].fact_id 不在库里已有的里")
        if op["op"] in ("ADD", "UPDATE") and not (op.get("dimension") and (op.get("statement") or "").strip()):
            errors.append(f"ops[{i}] 新增和更新要有 dimension 和 statement")
        if op.get("valid_from"):
            try:
                date.fromisoformat(op["valid_from"][:10])
            except ValueError:
                errors.append(f"ops[{i}].valid_from 不是日期")
    return errors


def propose_ops(router, db, new: list, old: list, day: date, ref: str = ""):
    version, system = llm.load_prompt("fact_ops")
    ids = {f["id"] for f in old}
    return llm.structured(router, db, "fact_ops", system, ops_input(new, old, day), OPS_SCHEMA,
                          check=lambda d: check_ops(d, new, ids), version=version, input_ref=ref, purpose="fact-ops")


def apply_ops(db, data: dict, evidence_id: int, day: date, context: str = None, props: dict = None, kinds: dict = None) -> dict:
    got = {"ADD": 0, "UPDATE": 0, "INVALIDATE": 0, "NOOP": 0}
    now = _now()
    for op in data["ops"]:
        kind, old = op["op"], _fact(db, op.get("fact_id")) if op.get("fact_id") else None
        if kind != "ADD" and not _current(old):
            continue  # 前面的操作已经改过它
        since = (op.get("valid_from") or day.isoformat())[:10]
        if kind == "NOOP":
            ids = sorted(set(json.loads(old["evidence_ids"])) | {evidence_id})
            db.conn.execute("UPDATE facts SET last_confirmed_at = ?, evidence_ids = ? WHERE id = ?", (now, json.dumps(ids), old["id"]))
        elif kind == "INVALIDATE":
            lifebase.invalidate(db, old["id"], since)
        else:
            subject = old["subject"] if old else "self"
            new_id = lifebase.add_fact(db, op["dimension"], op["statement"], [evidence_id], subject=subject, valid_from=since,
                                       importance=old["importance"] if old else 3,
                                       visibility=old["visibility"] if old and subject == "self" else None, context=context,
                                       prop=(props or {}).get(op.get("from")) or (old["prop"] if old and "prop" in old.keys() else None),
                                       kind=(kinds or {}).get(op.get("from")) or "fact")
            if kind == "UPDATE":
                lifebase.supersede(db, old["id"], new_id, since)
        got[kind] += 1
    db.conn.commit()
    return got


def nightly(store, router, day: date, statements: list, summary: str = "") -> dict:
    """每晚整理拿到的“新知道的”（一句一条）交给 fact_ops，再重写有变化的画像摘要。"""
    db = store.db
    ensure(db)
    statements = [s for s in dict.fromkeys(x.strip() for x in statements) if s][:60]
    got = {}
    if statements:
        ev = lifebase.try_evidence(db, "chat", f"journal:{day.isoformat()}", day.isoformat(),
                                   (summary + "\n" if summary else "") + "\n".join(statements), path=f"wiki/journal/{day.isoformat()}.md")
        data = propose_ops(router, db, statements, related(db, statements), day, f"journal:{day.isoformat()}") if ev else None
        got = {"skipped": "她要求删掉过这天的整理"} if ev is None else apply_ops(db, data, ev, day) if data else {"failed": "两次都没通过校验"}
    got["blocks"] = rebuild_blocks(db, router)
    return got


# ---------- 2. 画像摘要 ----------
def plain_block(facts: list) -> str:
    return "\n".join(f"- {f['statement']}" for f in facts)


def _block_facts(db, dim: str) -> list:
    """这个维度现在成立的、关于她本人的事实，重要的、新的在前。待定的也先放进来（不丢），每周复盘再归位。
    别人的事不进她的画像摘要（在人物卡里）。"""
    rows = [f for f in lifebase.current_facts(db, dim) if f["dimension"] == dim and f["subject"] == "self"
            and f.get("kind") != "hypothesis" and not f.get("read_by")]  # 画像大家都读：假设和限定了读者的不进（ontology.md 6.1 节）
    return sorted(rows, key=lambda f: (-f["importance"], -(f["confidence"] or 0), f["valid_from"] or "", f["id"]))


def condense_block(router, db, dim: str, facts: list, budget: int = BLOCK_BUDGET):
    if router is None:
        return None
    version, system = llm.load_prompt("profile_block")
    ids = {f["id"] for f in facts}
    must = {f["id"] for f in facts if f["importance"] >= 4}

    def check(d):
        errors = []
        if len(d["summary"]) > budget * 1.2:
            errors.append(f"摘要太长（{len(d['summary'])} 字，上限 {budget}）")
        if set(d["used_ids"]) - ids:
            errors.append(f"used_ids 里有不存在的编号 {sorted(set(d['used_ids']) - ids)}")
        if must - set(d["used_ids"]):
            errors.append(f"重要的事实没用上：{sorted(must - set(d['used_ids']))}")
        return errors
    text = "\n".join(f"{f['id']}. （重要度 {f['importance']}，{(f['valid_from'] or '')[:10]}）{f['statement']}" for f in facts)
    data = llm.structured(router, db, "profile_block", system.replace("{budget}", str(budget)),
                          f"维度：{lifebase.DIM_NAMES[dim]}\n\n{text}", BLOCK_SCHEMA, check=check, version=version,
                          input_ref=dim, purpose="profile-block")
    return data["summary"].strip() if data else None


def rebuild_blocks(db, router, force: bool = False) -> int:
    """事实有变化的维度重写摘要，返回改了几个维度。"""
    ensure(db)
    changed = 0
    for dim in lifebase.DIMENSIONS:
        facts = _block_facts(db, dim)
        ids = [f["id"] for f in facts]
        row = db.conn.execute("SELECT * FROM profile_blocks WHERE dimension = ?", (dim,)).fetchone()
        if row and not force and json.loads(row["fact_ids"]) == ids:
            continue
        if not facts and not row:
            continue
        summary = plain_block(facts)
        if len(summary) > BLOCK_BUDGET:
            summary = condense_block(router, db, dim, facts) or summary  # 浓缩没过检查就照列，宁长不丢
        version = (row["version"] if row else 0) + 1
        db.conn.execute("INSERT OR REPLACE INTO profile_blocks (dimension, summary, fact_ids, version, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (dim, summary, json.dumps(ids), version, _now()))
        db.conn.execute("INSERT INTO profile_history (dimension, version, summary, at) VALUES (?, ?, ?, ?)", (dim, version, summary, _now()))
        changed += 1
    db.conn.commit()
    return changed


def chat_text(db, user: str = None, skip=()) -> str:
    """给使用者看的画像：按维度分小节。没有就空。user 必须在登记表里（ethan/memusers.py），每次读都记一笔。"""
    from . import memsearch, memusers
    who = memusers.need(user, "portrait")  # 只登记了读某几项的（日记只读写作规范）不能读画像
    if lifebase.on_hold(db) and not who.get("all"):
        return ""  # 记忆暂停中
    if memusers.external(user):
        return _outside_text(db, user, skip)
    try:
        blocks = [dict(r) for r in db.conn.execute("SELECT dimension, summary, fact_ids FROM profile_blocks") if r["summary"].strip()]
    except Exception:
        return ""
    rows = {}
    for b in blocks:  # 摘要继承它用到的每条的权限：有一条这个使用者不能读、或已经删了 / 作废了，这段不用摘要，改用它能读的逐条（Codex 评审 11-03）
        ids = json.loads(b["fact_ids"] or "[]")
        facts = {r["id"]: dict(r) for r in db.conn.execute(
            f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
        if not ids:
            continue  # 说不清用到了哪几条的摘要，不给（等重做）
        ok = lambda f: (f["status"] not in ("deleted", "invalid", "suspended") and f["valid_to"] is None
                        and memusers.may_read(user, f.get("kind"), f.get("read_by"), f["dimension"]))
        if len(facts) == len(ids) and all(ok(f) for f in facts.values()):
            rows[b["dimension"]] = b["summary"]
        else:
            keep = [f for f in facts.values() if ok(f)]
            if keep:
                rows[b["dimension"]] = plain_block(keep)
    try:
        memsearch.ensure(db)
        db.conn.execute("INSERT INTO retrieval_log (at, scene, round, queries, keys, latency_ms, user) VALUES (?, 'portrait', 0, '[\"画像\"]', ?, 0, ?)",
                        (_now(), json.dumps([f"profile:{d}" for d in rows]), user))
        db.conn.commit()
    except Exception:
        pass
    return "\n\n".join(f"#### {lifebase.DIM_NAMES[d]}\n{rows[d]}" for d in lifebase.DIMENSIONS if d in rows and d not in skip)


def _outside_text(db, user: str, skip=()) -> str:
    """对外的（合作方、公开两档）：不给摘要（摘要混着不能说的），只按对外开关逐条给“可以说”的（lifebase.disclose，ontology-plan 3.3）。"""
    rows = {}
    for f in db.conn.execute("SELECT * FROM facts WHERE subject = 'self' AND valid_to IS NULL AND status = 'active' "
                             "ORDER BY importance DESC, id DESC").fetchall():
        if f["dimension"] in skip or lifebase.disclose(db, user, f) != "allow":
            continue
        rows.setdefault(f["dimension"], []).append(dict(f))
    try:
        from . import memsearch
        memsearch.ensure(db)
        db.conn.execute("INSERT INTO retrieval_log (at, scene, round, queries, keys, latency_ms, user) VALUES (?, 'portrait', 0, '[\"画像\"]', ?, 0, ?)",
                        (_now(), json.dumps([f"fact:{f['id']}" for fs in rows.values() for f in fs]), user))
        db.conn.commit()
    except Exception:
        pass
    return "\n\n".join(f"#### {lifebase.DIM_NAMES[d]}\n{plain_block(rows[d][:12])}" for d in lifebase.DIMENSIONS if d in rows)


# ---------- 3. 每周复盘 ----------
def decay(db, today: date) -> int:
    cut = (today - timedelta(days=STALE_DAYS)).isoformat()
    cur = db.conn.execute("UPDATE facts SET confidence = MAX(0.3, confidence * 0.8) WHERE valid_to IS NULL AND status NOT IN ('invalid', 'deleted') AND locked = 0 "
                          "AND COALESCE(last_confirmed_at, recorded_at) < ? AND confidence > 0.3", (cut,))
    return cur.rowcount


def weekly_input(facts: list) -> str:
    out = []
    for dim in lifebase.DIMENSIONS:
        rows = [f for f in facts if f["dimension"] == dim]
        if rows:
            out.append(f"## {lifebase.DIM_NAMES[dim]}（{dim}）\n" + "\n".join(
                f"{f['id']}. {'[待定] ' if f['status'] == 'candidate' else ''}{f['statement']}（{(f['valid_from'] or f['recorded_at'] or '')[:10]}，可信度 {f['confidence']:.1f}）"
                for f in rows))
    return "\n\n".join(out)


def _when(f) -> str:
    return f["valid_from"] or f["recorded_at"] or ""


def weekly(store, router, today: date = None) -> dict:
    db = store.db
    ensure(db)
    today = today or her_today()
    got = {"decayed": decay(db, today), "invalidated": 0, "reclassified": 0, "questions": 0}
    from . import mepage
    got["expired"] = mepage.expire_questions(db, today)
    facts = [f for f in lifebase.current_facts(db) if f["subject"] == "self"]
    if facts and router is not None:
        byid = {f["id"]: f for f in facts}

        def check(d):
            errors = [f"contradictions 里 {c['old_id']} 或 {c['new_id']} 不在清单里" for c in d["contradictions"]
                      if c["old_id"] not in byid or c["new_id"] not in byid or c["old_id"] == c["new_id"]]
            errors += [f"reclassify 的 {r['fact_id']} 不在清单里" for r in d["reclassify"] if r["fact_id"] not in byid]
            errors += [f"questions 的 fact_id {q['fact_id']} 不在清单里" for q in d["questions"] if q.get("fact_id") and q["fact_id"] not in byid]
            return errors
        version, system = llm.load_prompt("weekly_review")
        data = llm.structured(router, db, "weekly_review", system, weekly_input(facts), WEEKLY_SCHEMA, check=check,
                              version=version, input_ref=today.isoformat(), purpose="profile-weekly")
        if data:
            for c in data["contradictions"]:
                old, new = byid[c["old_id"]], byid[c["new_id"]]
                if _when(old) > _when(new):  # 代码保证作废的是较早的那条
                    old, new = new, old
                if _current(_fact(db, old["id"])) and lifebase.supersede(db, old["id"], new["id"], today.isoformat()):
                    got["invalidated"] += 1  # 她锁定的不动，记成问题（lifebase.supersede）
            for r in data["reclassify"]:
                db.conn.execute("UPDATE facts SET dimension = ?, also_dims = '[]', status = 'active' WHERE id = ? AND locked = 0",
                                (r["dimension"], r["fact_id"]))
                got["reclassified"] += 1
            for q in data["questions"]:  # 进“待你定”：工作台和 小伴 聊天问的是同一份（2026-10-31）
                if mepage.add_question(db, q["question"], q.get("fact_id"), commit=False):
                    got["questions"] += 1
        else:
            got["failed"] = "两次都没通过校验"
    db.conn.commit()
    got["blocks"] = rebuild_blocks(db, router)
    return got


def main():
    parser = argparse.ArgumentParser(description="画像：每周复盘、重写摘要")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--weekly", action="store_true")
    parser.add_argument("--blocks", action="store_true", help="全部维度重写摘要")
    args = parser.parse_args()
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        router = build_router(args.data, 50.0)
        if args.weekly:
            print(json.dumps(weekly(store, router), ensure_ascii=False), flush=True)
        else:
            print(f"改写了 {rebuild_blocks(store.db, router, force=args.blocks)} 个维度", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
