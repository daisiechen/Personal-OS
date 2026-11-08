"""她的写作规范（ontology.md 3.2 “语言习惯”，docs/design/style-and-skills.md）。

- 原料只取她自己写的（onto_query.style_materials，使用者 style）：她发出的邮件、她自己写的日记、她改系统草稿时的改前→改后、
  荧光笔标的句子（她喜欢的）、批注说“写得不好 / 不像我”的句子（她不喜欢的）、聊天里她的原话（抽样）。系统写的、她没改的不进。
- 归纳：模型按提示词 prompts/style_guide.md 输出结构化的规则，每条带 2–3 句出处，代码校验出处一字不差在原料里，
  且至少一句是她写的或她改的（只有“喜欢 / 不喜欢”撑着的不要）；改前→改后的对比算最强证据（strength）。
- 存：规则表 style_rules（分类、对象、词、出处）+ 说法（属性 writing_style）：草稿是假设、候选；她在“待你定”里逐条
  对 / 不对 / 改，对了、改了的记成她确认的偏好并锁住（locks：style:<编号>），不对的作废、以后不再提。
- 增量：每次只看没用过的新原料（style_seen），确认过的、她说不对的不动、不重复；原料不足就跳过。每周日夜里自动跑一次（nightly）。
- 迭代（——迭代，规范也一样）：
  她最近写的和某条确认过的规则不一样了（模型在 replaces 里指那条），每次算一次信号（style_signals）；4 周里攒够 REVISE_AT 句，
  出一条这条规则的“修订草稿”（revises 指原来那条），不另开新规则；她点对才替换（原来那条记“被取代”），不对就还按原来的。
- 用：只有确认过的才注入日记、邮件起草、人物卡“可以发的一句”、Days 对外回复的提示词（style_block）；草稿不注入。

命令：
  python3 -m ethan.style --data <数据目录> --draft          生成 / 增量更新草稿（--force 原料少也跑）
  python3 -m ethan.style --data <数据目录> --show           只读：列出现在的规范
  python3 -m ethan.style --data <数据目录> --eval 5         离线检验：最近 5 篇她改过的日记，用规范前后各生成一次，比和她改后版本的接近度
"""

import argparse
import difflib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import lifebase, locks, onto_query, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS style_rules (id INTEGER PRIMARY KEY, category TEXT NOT NULL, audience TEXT NOT NULL DEFAULT 'all',
  rule TEXT NOT NULL, words TEXT NOT NULL DEFAULT '[]', quotes TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '[]',
  strength INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'draft', fact_id INTEGER, statement_id INTEGER, her_text TEXT,
  replaced_by INTEGER, created_at TEXT NOT NULL, decided_at TEXT);
CREATE TABLE IF NOT EXISTS style_seen (ref TEXT PRIMARY KEY, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS style_signals (rule_id INTEGER NOT NULL, ref TEXT NOT NULL, text TEXT, at TEXT NOT NULL, used_by INTEGER,
  PRIMARY KEY (rule_id, ref));
"""
REVISE_AT = 3  # 她写的和确认过的某条不一样，攒够几句出修订草稿
REVISE_WEEKS = 4
CATEGORIES = {"length": "句子长短", "tone": "语气", "words": "常用词和口头禅", "avoid": "不用的词", "punctuation": "标点和 emoji",
              "mix": "中英混用", "opening": "开头", "closing": "结尾", "audience": "对不同对象", "other": "其他"}
AUDIENCES = {"all": "对谁都一样", "friend": "对朋友", "elder": "对长辈", "work": "工作上"}
# 用在哪
SCENES = {"all": "通用", "mail": "邮件", "diary": "日记", "site": "Days 回留言", "chat": "和朋友聊天"}
REF_SCENE = {"sent": "mail", "mailpair": "mail", "diary": "diary", "pair": "diary", "liked": "diary", "disliked": "diary", "chat": "chat"}
USER_SCENE = {"mail": "mail", "diary": "diary", "twin": "site", "relations": "chat"}  # 哪个使用者用哪个场景的规范


def scene_of(quotes: list) -> str:
    """按出处定用在哪：出处都来自同一种（都是她发的邮件、都是日记……）就是那一种；几处都有就是通用。"""
    got = {REF_SCENE.get(str(q.get("ref") or "").split(":")[0]) for q in quotes or []} - {None}
    return got.pop() if len(got) == 1 else "all"
STATUS = ("draft", "confirmed", "rejected", "replaced")
HER_KINDS = ("mail", "diary", "pair", "added", "deleted", "chat")  # 她写的或她改的；liked / disliked 是系统写的句子，只能当旁证
EDIT_KINDS = ("pair", "added", "deleted")  # 改前→改后：最强信号
TAG = {"pair": "改", "added": "加", "deleted": "删", "mail": "写", "diary": "写", "chat": "聊", "liked": "喜欢", "disliked": "不喜欢"}
FROM = {"pair": "你改草稿时", "added": "你加的", "deleted": "你删掉的系统句子", "mail": "你发的邮件", "diary": "你写的日记",
        "chat": "聊天里", "liked": "你标的荧光笔", "disliked": "你批注不好的"}
MIN_SCORE = 6  # 新原料不够这个分（改的算 3、其余算 1）就跳过
MAX_INPUT = 14000
OUT_SCHEMA = {"type": "object", "required": ["rules"], "properties": {"rules": {"type": "array", "maxItems": 20, "items": {
    "type": "object", "required": ["category", "audience", "rule", "quotes"], "properties": {
        "category": {"type": "string", "enum": list(CATEGORIES)}, "audience": {"type": "string", "enum": list(AUDIENCES)},
        "rule": {"type": "string", "maxLength": 60}, "words": {"type": "array", "maxItems": 10, "items": {"type": "string", "maxLength": 20}},
        "quotes": {"type": "array", "maxItems": 3, "items": {"type": "string", "maxLength": 300}},
        "replaces": {"type": ["integer", "null"]}}}}}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    if tx.once(db, "style"):
        db.conn.executescript(SCHEMA)
        have = {c[1] for c in db.conn.execute("PRAGMA table_info(style_rules)")}
        if "revises" not in have:  # 修订草稿：改的是哪条确认过的
            db.conn.execute("ALTER TABLE style_rules ADD COLUMN revises INTEGER")
        if "scene" not in have:  # 用在哪：老规则按出处补
            db.conn.execute("ALTER TABLE style_rules ADD COLUMN scene TEXT NOT NULL DEFAULT 'all'")
            for r in db.conn.execute("SELECT id, quotes FROM style_rules").fetchall():
                db.conn.execute("UPDATE style_rules SET scene = ? WHERE id = ?", (scene_of(json.loads(r["quotes"] or "[]")), r["id"]))
            db.conn.commit()


def rules(db, status=None) -> list:
    ensure(db)
    sts = [status] if isinstance(status, str) else list(status or STATUS)
    rows = db.conn.execute(f"SELECT * FROM style_rules WHERE status IN ({','.join('?' * len(sts))}) ORDER BY strength DESC, id", sts)
    out = []
    for r in rows:
        d = dict(r)
        d["words"], d["quotes"], d["evidence"] = json.loads(d["words"]), json.loads(d["quotes"]), json.loads(d["evidence"])
        out.append(d)
    return out


# ---------- 原料 ----------
def materials(db, chat_limit: int = 80) -> list:
    got = onto_query.style_materials(db, "style", chat_limit=chat_limit)
    return got["items"] if got["status"] == "answer" else []


def fresh(db, items: list) -> list:
    ensure(db)
    seen = {r[0] for r in db.conn.execute("SELECT ref FROM style_seen")}
    return [i for i in items if i["ref"] not in seen]


def score(items: list) -> int:
    return sum(3 if i["kind"] in EDIT_KINDS else 1 for i in items)


def build_input(items: list, existing: list) -> str:
    """原料按可信度排：改前→改后在最前（最强信号）。"""
    order = {k: n for n, k in enumerate(("pair", "deleted", "added", "mail", "diary", "chat", "liked", "disliked"))}
    lines = []
    for i in sorted(items, key=lambda x: order.get(x["kind"], 9)):
        if i["kind"] == "pair":
            lines.append(f"【改】改前：{i['before']}\n　　改后：{i['text']}")
        elif i["kind"] == "disliked":
            lines.append(f"【不喜欢】{i['text']}（她的批注：{i.get('note', '')}）")
        elif i["kind"] == "mail":
            lines.append(f"【写】（邮件{('：' + i['subject']) if i.get('subject') else ''}）{i['text']}")
        else:
            lines.append(f"【{TAG[i['kind']]}】{i['text']}")
    text = "\n".join(lines)[:MAX_INPUT]
    groups = {"确认过的（不要重复；她最近的写法明显和某条不一样了，才写一条修订版，replaces 写它的编号）": "confirmed", "草稿（可以改进，replaces 写编号）": "draft", "她说不对的（不要再提）": "rejected"}
    ex = []
    for title, st in groups.items():
        rs = [r for r in existing if r["status"] == st]
        if rs:
            ex.append(f"{title}：\n" + "\n".join(f"- #{r['id']} [{r['category']}/{r['audience']}] {r['her_text'] or r['rule']}" for r in rs))
    return "## 原料\n" + text + "\n\n## 现有的规范\n" + ("\n".join(ex) or "（还没有）")


def _locate(quote: str, items: list):
    """出处在哪条原料里（一字不差，忽略空白和标点差别）；改前那句也算（那是她改掉的写法）。"""
    from .llm import quote_in
    for i in items:
        if quote_in(quote, i["text"]):
            return i, "after"
        if i.get("before") and quote_in(quote, i["before"]):
            return i, "before"
    return None, None


def check_rule(rule: dict, items: list, drafts=()) -> list:
    errs = []
    qs = [q for q in rule.get("quotes") or [] if (q or "").strip()]
    if not 2 <= len(qs) <= 3:
        errs.append("要 2–3 句出处")
    found = [_locate(q, items) for q in qs]
    if any(i is None for i, _ in found):
        errs.append("出处不在原料里：" + "；".join(q[:20] for q, (i, _) in zip(qs, found) if i is None))
    if found and not any(i is not None and i["kind"] in HER_KINDS for i, _ in found):
        errs.append("至少要有一句是她写的或她改的")
    if any(lifebase.CONTACT.search(q) for q in qs) or lifebase.CONTACT.search(rule.get("rule") or ""):
        errs.append("出处里有联系方式或住址")
    if rule.get("replaces") is not None and rule["replaces"] not in {d["id"] for d in drafts}:
        errs.append(f"replaces #{rule['replaces']} 不是草稿或确认过的")
    return errs


def induce(router, db, items: list, existing: list) -> list:
    """模型归纳 → 校验过的规则（不合格的条目丢掉；超过一半不合格就让模型重来一次）。"""
    from . import llm
    version, system = llm.load_prompt("style_guide")
    drafts = [r for r in existing if r["status"] in ("draft", "confirmed")]  # 改进草稿，或者修订确认过的（攒够才出草稿）

    def check(data):
        bad = [f"第 {n + 1} 条：{'、'.join(e)}" for n, r in enumerate(data.get("rules") or []) if (e := check_rule(r, items, drafts))]
        return bad if bad and len(bad) * 2 > len(data.get("rules") or []) else []
    data = llm.structured(router, db, "style_guide", system, build_input(items, existing), OUT_SCHEMA, check=check, version=version,
                          input_ref="style", purpose="style-guide")
    return [r for r in (data or {}).get("rules") or [] if not check_rule(r, items, drafts)]


# ---------- 存 ----------
def _statement_of(db, fact_id):
    r = db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (fact_id,)).fetchone() if fact_id else None
    return r[0] if r else None


def _context(category: str, audience: str) -> str:
    return f"写作规范 · {CATEGORIES.get(category, category)}" + ("" if audience == "all" else f" · {AUDIENCES.get(audience, audience)}")


def apply(db, new_rules: list, items: list, used: list = None) -> dict:
    """新规则写成草稿：和确认过的、她说不对的说的是同一件事的丢掉（不覆盖、不重提）；改进草稿的把旧草稿标“被取代”。
    用过的原料记进 style_seen（used 不传就是 items 全部），下次只看新的。"""
    from .core.agent import find_similar
    ensure(db)
    existing = rules(db)
    done = {"added": 0, "replaced": 0, "skipped": 0, "signals": 0, "revisions": 0}
    with tx.atomic(db):
        for r in new_rules:
            text = " ".join(r["rule"].split())[:60]
            target = next((x for x in existing if x["status"] == "confirmed" and x["id"] == r.get("replaces")), None)
            if target is not None:  # 她最近写的和这条确认过的不一样了：记信号，攒够才出修订草稿
                got = _signal(db, target, r, text, items, existing)
                done["signals"] += got != "same"
                done["revisions"] += got == "revision"
                done["skipped"] += got == "same"
                continue
            settled = [x["her_text"] or x["rule"] for x in existing if x["status"] in ("confirmed", "rejected")]
            if find_similar(text, settled):
                done["skipped"] += 1
                continue
            old = next((x for x in existing if x["status"] == "draft" and (x["id"] == r.get("replaces") or find_similar(text, [x["rule"]]))), None)
            if old and locks.locked(db, "style", old["id"], "rule"):
                done["skipped"] += 1
                continue
            cur, fid = _insert_draft(db, r, text, items)
            if old:
                db.conn.execute("UPDATE style_rules SET status = 'replaced', replaced_by = ?, decided_at = ? WHERE id = ?", (cur.lastrowid, _now(), old["id"]))
                if old["fact_id"] and fid:
                    lifebase.supersede(db, old["fact_id"], fid, force=True)
                elif old["fact_id"]:
                    lifebase.invalidate(db, old["fact_id"], force=True)
                done["replaced"] += 1
                existing = [x for x in existing if x["id"] != old["id"]]
            else:
                done["added"] += 1
            existing.append({"id": cur.lastrowid, "status": "draft", "rule": text, "her_text": None})
        db.conn.executemany("INSERT OR IGNORE INTO style_seen (ref, at) VALUES (?, ?)", [(i["ref"], _now()) for i in (items if used is None else used)])
    refresh_pending(db)
    return done


def draft(db, router, force: bool = False) -> dict:
    """增量生成草稿：只看新原料；不够就跳过（force 照跑）。确认过的不动。"""
    items = materials(db)
    new = fresh(db, items)
    if not new or (score(new) < MIN_SCORE and not force):
        return {"skipped": f"新原料不够（{len(new)} 条，分 {score(new)} < {MIN_SCORE}）", "new": len(new)}
    got = induce(router, db, new, rules(db))
    if not got:
        return {"skipped": "模型没给出合格的规则", "new": len(new)}  # 原料不记成用过，下次再试
    return {"new": len(new), **apply(db, got, new)}


def _insert_draft(db, r: dict, text: str, items: list, revises: int = None):
    """一条草稿写进规则表（草稿也是一条说法：系统的假设、候选，只有 小伴 能看到；她认了才是偏好）。返回 (游标, 说法编号)。"""
    quotes, evidence, strength = [], [], 0
    for q in r["quotes"][:3]:
        i, side = _locate(q, items)
        quotes.append({"text": q, "kind": i["kind"], "side": side, "ref": i["ref"]})
        strength += i["kind"] in EDIT_KINDS
        if i.get("evidence") and i["evidence"] not in evidence:
            evidence.append(i["evidence"])
    fid = None
    if evidence:
        fid = lifebase.add_fact(db, "expression", text, evidence, confidence=0.5, importance=3, tags=("writing_style",),
                                status="candidate", kind="hypothesis", source_kind="system_inference", prop="writing_style",
                                context=_context(r["category"], r["audience"]))
    scene = r.get("scene") or scene_of(quotes)  # 修订草稿沿用原来那条的；新的按出处定
    cur = db.conn.execute("INSERT INTO style_rules (category, audience, rule, words, quotes, evidence, strength, fact_id, statement_id, created_at, revises, scene) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (r["category"], r["audience"], text, json.dumps(r.get("words") or [], ensure_ascii=False),
                           json.dumps(quotes, ensure_ascii=False), json.dumps(evidence), strength, fid, _statement_of(db, fid), _now(), revises, scene))
    return cur, fid


def _signal(db, target: dict, r: dict, text: str, items: list, existing: list) -> str:
    """她最近写的和确认过的 target 不一样：她写的 / 改的那几句各记一次信号；4 周里没用过的攒够 REVISE_AT 句、
    这条还没有在等她看的修订草稿，就出一条修订草稿（revises = target）。返回 same（说的是同一件事，不算）/ signal / revision。"""
    from datetime import timedelta
    from .core.agent import find_similar
    if find_similar(text, [target["her_text"] or target["rule"]]):
        return "same"
    for q in r["quotes"][:3]:
        i, _ = _locate(q, items)
        if i is not None and i["kind"] in HER_KINDS:
            db.conn.execute("INSERT OR IGNORE INTO style_signals (rule_id, ref, text, at) VALUES (?, ?, ?, ?)", (target["id"], i["ref"], text, _now()))
    since = (datetime.now(timezone.utc) - timedelta(days=7 * REVISE_WEEKS)).isoformat(timespec="seconds")
    n = db.conn.execute("SELECT COUNT(*) FROM style_signals WHERE rule_id = ? AND used_by IS NULL AND at >= ?", (target["id"], since)).fetchone()[0]
    if n < REVISE_AT or any(x["status"] == "draft" and x.get("revises") == target["id"] for x in existing):
        return "signal"
    cur, _ = _insert_draft(db, {**r, "category": target["category"], "audience": target["audience"], "scene": target.get("scene") or "all"}, text, items, revises=target["id"])
    db.conn.execute("UPDATE style_signals SET used_by = ? WHERE rule_id = ? AND used_by IS NULL", (cur.lastrowid, target["id"]))
    existing.append({"id": cur.lastrowid, "status": "draft", "rule": text, "her_text": None, "revises": target["id"]})
    return "revision"


# ---------- 待你定：一条“你的写作规范草稿”，里面逐条 对 / 不对 / 改 ----------
QUESTION = "你的写作规范草稿"


def _option(r: dict, db=None) -> dict:
    out = {"rule_id": r["id"], "text": r["her_text"] or r["rule"], "category": CATEGORIES.get(r["category"], r["category"]),
           "scene": r.get("scene") or "all", "confirmed": r["status"] == "confirmed",
           "audience": AUDIENCES.get(r["audience"], r["audience"]), "words": r["words"],
           "quotes": [{"text": q["text"], "from": FROM.get(q["kind"], "") + ("（改前）" if q.get("side") == "before" else "")} for q in r["quotes"]],
           "src": f"依据 {len(r['quotes'])} 句" + (f"，其中 {r['strength']} 句是你改草稿时的" if r["strength"] else "")}
    if r.get("revises") and db is not None:  # 修订草稿：原来生效的那条
        old = db.conn.execute("SELECT rule, her_text FROM style_rules WHERE id = ?", (r["revises"],)).fetchone()
        if old:
            out["revises"] = {"id": r["revises"], "text": old["her_text"] or old["rule"]}
            n = db.conn.execute("SELECT COUNT(*) FROM style_signals WHERE used_by = ?", (r["id"],)).fetchone()[0]
            out["src"] = f"修订：你最近 {n} 次写的和原来那条不一样；点对就替换原来那条，不对就还按原来的 · " + out["src"]
    return out


def refresh_pending(db) -> int:
    """草稿有没定的：待你定里保持一条（选项跟着草稿变）；全定完了就关掉。返回那一条的编号（没有就 0）。"""
    from . import mepage
    mepage.ensure(db)
    drafts = rules(db, "draft")
    row = db.conn.execute("SELECT id FROM pending WHERE kind = 'style' AND status = 'open' ORDER BY id DESC LIMIT 1").fetchone()
    if not drafts:
        if row:
            db.conn.execute("UPDATE pending SET status = 'done', resolved_at = ?, resolution = ? WHERE id = ?",
                            (_now(), json.dumps({"action": "all_done"}), row[0]))
            tx.commit(db)
        return 0
    n_ok = len(rules(db, "confirmed"))
    n_rev = sum(1 for r in drafts if r.get("revises"))
    q = f"{QUESTION}：{len(drafts)} 条等你看" + (f"（其中修订 {n_rev} 条）" if n_rev else "") + (f"（已生效 {n_ok} 条）" if n_ok else "")
    opts = json.dumps([_option(r, db) for r in drafts] + [_option(r, db) for r in rules(db, "confirmed")], ensure_ascii=False)  # 已生效的也列，能改用在哪
    if row:
        db.conn.execute("UPDATE pending SET question = ?, options = ? WHERE id = ?", (q, opts, row[0]))
        pid = row[0]
    else:
        pid = db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES ('style', 'long', ?, ?, '[]', ?, 0, ?)",
                              (q, opts, json.dumps(["style"]), _now())).lastrowid
    tx.commit(db)
    return pid


def confirm(db, rule_id: int, text: str = None) -> dict:
    """她点“对”（或写了一句改过的）：记成她确认的偏好、锁住；草稿那条说法被取代。"""
    ensure(db)
    r = next((x for x in rules(db, "draft") if x["id"] == int(rule_id)), None)
    if r is None:
        return {"ok": False, "msg": "这条已经定过了"}
    final = " ".join((text or r["rule"]).split())[:60]
    if not final:
        return {"ok": False, "msg": "写一句规则"}
    with tx.atomic(db):
        how = "改成" if text and final != r["rule"] else "确认了"
        ev = lifebase.add_evidence(db, "correction", f"style:{r['id']}:{_now()}", _now(), f"她在待你定里{how}写作规范：{final}")
        fid = lifebase.add_fact(db, "expression", final, [ev] + r["evidence"], confidence=1.0, importance=4, tags=("writing_style", "her_correction"),
                                kind="preference", source_kind="her_confirmation", prop="writing_style",
                                context=_context(r["category"], r["audience"]), locked=True)
        if r["fact_id"]:
            lifebase.supersede(db, r["fact_id"], fid, force=True)
        db.conn.execute("UPDATE style_rules SET status = 'confirmed', fact_id = ?, statement_id = ?, her_text = ?, decided_at = ? WHERE id = ?",
                        (fid, _statement_of(db, fid), final if final != r["rule"] else None, _now(), r["id"]))
        old = next((x for x in rules(db, "confirmed") if x["id"] == r.get("revises")), None)
        if old:  # 修订草稿她认了：原来那条被取代（留着能查），以后按新的
            db.conn.execute("UPDATE style_rules SET status = 'replaced', replaced_by = ?, decided_at = ? WHERE id = ?", (r["id"], _now(), old["id"]))
            if old["fact_id"]:
                lifebase.supersede(db, old["fact_id"], fid, force=True)
        locks.lock(db, "style", r["id"], "rule", value=final, note=f"她在待你定里{how}")
    refresh_pending(db)
    return {"ok": True, "msg": f"记下了，以后按这条写：{final}"}


def reject(db, rule_id: int) -> dict:
    """她点“不对”：草稿作废，以后不再提同样的（也锁住，自动流程不会再写回来）。"""
    ensure(db)
    r = next((x for x in rules(db, "draft") if x["id"] == int(rule_id)), None)
    if r is None:
        return {"ok": False, "msg": "这条已经定过了"}
    with tx.atomic(db):
        if r["fact_id"]:
            lifebase.invalidate(db, r["fact_id"], force=True)
        db.conn.execute("UPDATE style_rules SET status = 'rejected', decided_at = ? WHERE id = ?", (_now(), r["id"]))
        locks.lock(db, "style", r["id"], "rule", value="rejected", note="她在待你定里说不对")
    refresh_pending(db)
    return {"ok": True, "msg": "好，这条不算，以后不再提"}


def resolve(db, pending_row, action: str, pick=None, text: str = "") -> dict:
    """mepage.resolve 转过来的：pick 是规则编号；yes 对 / no 不对 / own 改成她写的。"""
    if pick is None:
        return {"ok": False, "msg": "选一条"}
    if action == "yes":
        return confirm(db, int(pick))
    if action == "no":
        return reject(db, int(pick))
    if action == "own":
        if not (text or "").strip():
            return {"ok": False, "msg": "写一句改好的"}
        return confirm(db, int(pick), text)
    if action == "scene":  # 改用在哪
        if text not in SCENES:
            return {"ok": False, "msg": "用在哪只能选：" + "、".join(SCENES.values())}
        ensure(db)
        db.conn.execute("UPDATE style_rules SET scene = ? WHERE id = ?", (text, int(pick)))
        db.conn.commit()
        refresh_pending(db)
        return {"ok": True, "msg": f"这条以后只用在：{SCENES[text]}" if text != "all" else "这条以后哪里都用", "log": f"写作规范 #{pick} 用在 {SCENES[text]}"}
    return {"ok": False, "msg": "不认识的操作"}


# ---------- 用：只注入确认过的 ----------
def style_block(db, user: str, audience: str = None, quotes: bool = True) -> str:
    """给提示词：她确认过的写作规范（经 onto_query.writing_style，按使用者把关）。没有确认过的就空——草稿不注入。"""
    try:
        got = onto_query.writing_style(db, user, audience=audience, scene=USER_SCENE.get(user))
    except Exception:
        return ""
    if got["status"] != "answer":
        return ""
    lines = []
    for i in got["items"]:
        line = f"- {i['value']}"
        if i["category"] == "avoid" and i["words"]:
            line += f"（不用：{'、'.join(i['words'])}）"
        if i["audience"] != "all":
            line += f"（{AUDIENCES.get(i['audience'], i['audience'])}）"
        if quotes and i.get("quotes"):
            line += "\n  她写过的：" + " / ".join(f"「{q[:60]}」" for q in i["quotes"][:2])
        lines.append(line)
    return "她确认过的写作规范（只管怎么说，不管说什么；照着写）：\n" + "\n".join(lines)


def banned_words(db, statuses=("confirmed",)) -> list:
    """不用的词（avoid 类规则里写的）。"""
    out = []
    for r in rules(db, list(statuses)):
        if r["category"] == "avoid":
            out += [w for w in r["words"] if w and w not in out]
    return out


# ---------- 检验：能用代码判的指标（不靠模型打分） ----------
def similarity(a: str, b: str) -> float:
    norm = lambda s: re.sub(r"\s+", "", s or "")
    return round(difflib.SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio(), 3)


def banned_hits(text: str, words) -> int:
    return sum((text or "").count(w) for w in words if w)


def used_hits(text: str, words) -> float:
    """她常用的词在文字里出现了几成（没登记常用词就 None）。"""
    words = [w for w in words if w]
    return round(sum(1 for w in words if w in (text or "")) / len(words), 3) if words else None


def _block_from_rules(rs: list) -> str:
    if not rs:
        return ""
    return "她的写作规范（只管怎么说，不管说什么；照着写）：\n" + "\n".join(
        f"- {r['her_text'] or r['rule']}" + (f"（不用：{'、'.join(r['words'])}）" if r["category"] == "avoid" and r["words"] else "") for r in rs)


def offline_eval(store, router, n: int = 5, use_drafts: bool = False) -> dict:
    """服务器上跑：她最近 n 篇改过的日记（her_edited），用规范前、后各生成一次，比和她改后版本的接近度。
    只读她的数据：生成的不存，结果写 <数据目录>/evals/style-offline-<日期>.json。"""
    from datetime import date
    from . import diary
    db = store.db
    diary.ensure(db)
    rs = rules(db, ["confirmed", "draft"] if use_drafts else ["confirmed"])
    block = _block_from_rules(rs)
    banned = [w for r in rs if r["category"] == "avoid" for w in r["words"]]
    common = [w for r in rs if r["category"] in ("words", "mix") for w in r["words"]]
    days = [r[0] for r in db.conn.execute("SELECT day FROM diary WHERE source = ? AND draft_text IS NOT NULL ORDER BY day DESC LIMIT ?", (diary.EDITED, n))]
    rows = []
    for d in days:
        her = diary.get(db, d)["body"]
        m = diary.materials(store, date.fromisoformat(d))
        if not m:
            continue
        user = f"日期：{d}\n\n{m}"
        out = {}
        for name, system in (("before", diary.PROMPT), ("after", diary.PROMPT + ("\n\n" + block if block else ""))):
            text = router.complete(system, [{"role": "user", "content": user}], purpose="style-eval").text.strip()
            out[name] = {"similarity": similarity(text, her), "banned": banned_hits(text, banned), "common": used_hits(text, common), "chars": len(text)}
        out["draft"] = {"similarity": similarity(diary.get(db, d)["draft_text"], her), "banned": banned_hits(diary.get(db, d)["draft_text"], banned)}
        rows.append({"day": d, **out})
    avg = lambda k, f: round(sum(r[k][f] for r in rows) / len(rows), 3) if rows else None
    res = {"at": _now(), "days": len(rows), "rules": len(rs), "use_drafts": use_drafts, "banned_words": banned,
           "similarity_before": avg("before", "similarity"), "similarity_after": avg("after", "similarity"),
           "banned_before": sum(r["before"]["banned"] for r in rows), "banned_after": sum(r["after"]["banned"] for r in rows), "rows": rows}
    out_dir = Path(store.root) / "evals"
    out_dir.mkdir(exist_ok=True)
    (out_dir / f"style-offline-{res['at'][:10]}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return res


def weekly(store, router) -> dict:
    """每周日夜里（ethan/nightly.py）：增量更新草稿（原料不足就跳过），再看要不要提议新 skill 草稿。"""
    out = {"style": draft(store.db, router)}
    try:
        from . import skills
        out["skills"] = skills.propose(store.db)
    except Exception as e:
        out["skills"] = f"没看成：{type(e).__name__} {str(e)[:80]}"
    return out


def main():
    p = argparse.ArgumentParser(description="她的写作规范")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--draft", action="store_true", help="生成 / 增量更新草稿")
    p.add_argument("--force", action="store_true", help="原料少也跑")
    p.add_argument("--show", action="store_true", help="只读：列出现在的规范")
    p.add_argument("--eval", type=int, metavar="N", help="离线检验：最近 N 篇她改过的日记")
    p.add_argument("--use-drafts", action="store_true", help="离线检验时连草稿一起用（看草稿有没有用）")
    a = p.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        if a.draft or a.eval:
            from .cli import build_router
            router = build_router(a.data, 50.0)
        if a.draft:
            print(f"写作规范草稿：{draft(store.db, router, force=a.force)}", flush=True)
        if a.eval:
            res = offline_eval(store, router, a.eval, a.use_drafts)
            print(f"离线检验 {res['days']} 篇（规范 {res['rules']} 条{'，含草稿' if a.use_drafts else ''}）：和她改后版本的相似度 "
                  f"{res['similarity_before']} → {res['similarity_after']}；禁用词 {res['banned_before']} → {res['banned_after']} 次", flush=True)
        if a.show or not (a.draft or a.eval):
            for st in ("confirmed", "draft", "rejected"):
                rs = rules(store.db, st)
                print(f"{ {'confirmed': '已生效', 'draft': '草稿', 'rejected': '她说不对'}[st]} {len(rs)} 条")
                for r in rs:
                    print(f"  #{r['id']} [{CATEGORIES.get(r['category'])}/{AUDIENCES.get(r['audience'])}] {r['her_text'] or r['rule']}"
                          + (f"  （依据 {len(r['quotes'])} 句，改草稿的 {r['strength']} 句）" if st == "draft" else ""))
    finally:
        store.close()


if __name__ == "__main__":
    main()
