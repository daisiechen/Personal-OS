"""工作台“我”页（记忆对齐第三步，稿：docs/design/memory-alignment.md 第 9 节）。

页面主体：对得上的比例和每周趋势 → 使用者小卡（小伴、Days在前，其余按用得多少）→ 纠错单（跟着点开的卡变）。
右上角三个弹窗：待你定（红点数字）、问一问、全部记忆。

记忆分两种：长期（她一般是怎样的，带有效期：一直 / 从某天起 / 到某天 / 某个条件下）和那一次（某天发生了什么，记成事件）。
三种改法：错了（从来不是这样，旧的作废，算一次错）/ 变了（旧的到某天为止、留作历史，不算错）/ 都对只是时间不同（各带有效期）。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

from . import lifebase, memusers, tx, upgrades
from .clock import her_today
from . import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS pending (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'long', question TEXT NOT NULL, options TEXT NOT NULL,
    fact_ids TEXT NOT NULL DEFAULT '[]', users TEXT NOT NULL DEFAULT '[]', uses INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open', resolved_at TEXT, resolution TEXT);
CREATE TABLE IF NOT EXISTS confirmations (id INTEGER PRIMARY KEY, at TEXT NOT NULL, source TEXT NOT NULL, fact_id INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS ask_log (id INTEGER PRIMARY KEY, at TEXT NOT NULL, question TEXT NOT NULL, answers TEXT NOT NULL, verdict TEXT);
"""
PENDING_COLS = (("asked_at", "TEXT"), ("origin", "TEXT"), ("dim", "TEXT"), ("payload", "TEXT"))  # origin = gap：1.7 补缺口挑的题  # 小伴 在聊天里问过的时间：问过的三天内不再问（两套“问她”合一）
QUESTION_DAYS = 14  # 画像出的确认问题，她两周没理就过期（不算错也不算对）
ASK_GAP_DAYS = 3
SNAP_COLS = "valid_from, valid_to, superseded_by, status, locked, confidence, until, until_cond, last_confirmed_at"
ONE_OFF = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}月\d{1,2}日|\d{1,2}-\d{1,2} ?(早上|上午|中午|下午|晚上|凌晨)")
ASK_EXPIRED_DAYS = 14  # 到期两周以内的才在待你定里问一句还算不算数
CONFIRM_USES = 3  # 被用到这么多次、她还没确认过的，进“待你定”
ANSWER = ("你在替{who}回答 Mia 问的一个关于她自己的问题，用来检查{who}心里的她对不对。只按下面“记得的”回答，"
          "一两句话（问某天做了什么的可以三四句，按时间串起来，日记没写就说一声）；记得的里没有就说“我不知道”。不要编。\n\n记得的：\n{memory}")
ASK_TWIN = "这个我问问她，回头告诉你"  # 和 onto_query.ASK_REPLY 同一句
COMPARE = ("下面是对同一个问题的两个回答。它们说的是同一件事、只是详略不同，还是互相矛盾？只输出 JSON："
           '{"conflict": true 或 false, "why": "一句话"}')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    from . import memsearch
    memusers.ensure(db)
    if not tx.once(db, "mepage"):  # 建表、加列每个连接只做一次（我页加载还是慢）
        return
    memsearch.ensure(db)  # 算“用过几次”要 retrieval_log
    db.conn.executescript(SCHEMA)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(pending)")}
    for col, decl in PENDING_COLS:
        if col not in have:
            db.conn.execute(f"ALTER TABLE pending ADD COLUMN {col} {decl}")
    _move_old_questions(db)


def _move_old_questions(db):
    """画像每周复盘以前出的确认问题（profile_questions）挪进“待你定”，以后只有这一条线。"""
    try:
        rows = db.conn.execute("SELECT * FROM profile_questions WHERE status = 'open'").fetchall()
    except Exception:
        return
    for r in rows:
        add_question(db, r["question"], r["fact_id"], commit=False)
        db.conn.execute("UPDATE profile_questions SET status = 'moved', closed_at = ? WHERE id = ?", (_now(), r["id"]))
    if rows:
        db.conn.commit()


def _fact(db, fid):
    r = db.conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()
    return dict(r) if r else None


# 这条是谁说的（ontology-plan 1.2 节的归属表），“全部记忆”每条都写清楚，方便她验收（看到全写着“系统推出来的”）
SAID = {"her_words": "你说的", "her_confirmation": "你确认的", "her_provided": "你给的资料", "behavior": "行为记录",
        "other_person": "别人说的", "system_inference": "系统推的", "agent_relay": "转述"}


def _matches(q: str, text: str) -> bool:
    """全部记忆里的搜索（搜“某商学院”“读过的学校”都搜不到）：不分大小写；整句对得上、换过的说法对得上、
    或者一半以上的词对得上都算。意思相近但用词完全不同的，要等语义检索（ontology-plan 候补，提前）。"""
    from . import memsearch
    low = (text or "").lower()
    q = (q or "").strip().lower()
    if not q or q in low:
        return True
    if any(s.lower() in low for s in memsearch.expand(q)[1:]):
        return True
    toks = set(memsearch.tokens(q, query=True))
    return bool(toks) and sum(1 for t in toks if t in low) * 2 >= len(toks)


def _hers(f) -> bool:
    """这条是她自己说的 / 定的，还是系统推出来的。"""
    return bool(f and (f["locked"] or "her_correction" in (f["tags"] or "") or f["confidence"] >= 0.9))


def _uses(db, fid, days: int = 30) -> tuple:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT user FROM retrieval_log WHERE at >= ? AND keys LIKE ? AND COALESCE(user, '') != 'system'",
                           (since, f'%"fact:{fid}"%')).fetchall()
    return len(rows), sorted({r["user"] for r in rows if r["user"]})


def _use_counts(db, days: int = 30) -> dict:
    """每条记忆被用过几次，一次读完：{fact_id: 次数}。全部记忆一页一万多条，一条条查要 7 秒。
    和 _uses 同一个算法：一次检索里出现过就算一次，系统自己维护的不算。"""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out = {}
    for r in db.conn.execute("SELECT keys FROM retrieval_log WHERE at >= ? AND keys LIKE '%\"fact:%' AND COALESCE(user, '') != 'system'", (since,)):
        try:
            keys = set(json.loads(r["keys"] or "[]"))
        except ValueError:
            continue
        for k in keys:
            if isinstance(k, str) and k.startswith("fact:") and k[5:].isdigit():
                out[int(k[5:])] = out.get(int(k[5:]), 0) + 1
    return out


def _option(db, f) -> dict:
    n, users = _uses(db, f["id"])
    return {"text": f["statement"], "fact_id": f["id"], "mine": _hers(f), "at": (f["valid_from"] or f["recorded_at"] or "")[:10],
            "src": ("你说过的" if _hers(f) else "系统推出来的") + f" · {(f['recorded_at'] or '')[:10]}"}


# ---------- 待你定 ----------
def add_question(db, question: str, fact_id: int = None, commit: bool = True) -> int:
    """想问她的一件事（画像每周复盘出的确认问题、锁定的说法可能不成立了）。带事实就能点对/不对，不带就只能自己写。"""
    if "pending" not in {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}:
        ensure(db)
    question = (question or "").strip()
    f = _fact(db, fact_id) if fact_id else None
    if not question or db.conn.execute("SELECT 1 FROM pending WHERE status = 'open' AND kind = 'question' AND question = ?",
                                       (question,)).fetchone():
        return 0
    n, users = _uses(db, f["id"]) if f else (0, [])
    cur = db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES ('question', 'long', ?, ?, ?, ?, ?, ?)",
                          (question, json.dumps([_option(db, f)] if f else [], ensure_ascii=False), json.dumps([f["id"]] if f else []),
                           json.dumps(users), n, _now()))
    if commit:
        db.conn.commit()
    return cur.lastrowid


def expire_questions(db, today: date = None) -> int:
    ensure(db)
    cut = ((today or her_today()) - timedelta(days=QUESTION_DAYS)).isoformat()
    n = db.conn.execute("UPDATE pending SET status = 'expired', resolved_at = ? WHERE status = 'open' AND kind = 'question' AND created_at < ?",
                        (_now(), cut)).rowcount
    db.conn.commit()
    return n


def add_conflict(db, old_id: int, new_id: int) -> int:
    """新说法和她锁定的冲突（lifebase.supersede 拦下的）：两条都列出来让她定。"""
    ensure(db)
    old, new = _fact(db, old_id), _fact(db, new_id)
    if not old or not new:
        return 0
    if db.conn.execute("SELECT 1 FROM pending WHERE status = 'open' AND fact_ids = ?", (json.dumps([old_id, new_id]),)).fetchone():
        return 0
    n, users = _uses(db, old_id)
    cur = db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES (?, 'long', ?, ?, ?, ?, ?, ?)",
                          ("conflict", f"{lifebase.DIM_NAMES.get(old['dimension'], old['dimension'])}：两种说法对不上",
                           json.dumps([_option(db, old), _option(db, new)], ensure_ascii=False), json.dumps([old_id, new_id]),
                           json.dumps(users), n, _now()))
    db.conn.commit()
    return cur.lastrowid


def refresh(db, today: date = None, city: str = None) -> dict:
    """每晚：常被用到、她没确认过的，和到期的，进“待你定”。"""
    ensure(db)
    today = today or her_today()
    got = {"confirm": 0, "expire": 0}
    asked = {json.loads(r["fact_ids"])[0] for r in db.conn.execute("SELECT fact_ids FROM pending WHERE kind IN ('confirm', 'expire')")
             if json.loads(r["fact_ids"])}
    seen = [r[0] for r in db.conn.execute("SELECT f.statement FROM facts f WHERE f.locked = 1 OR f.id IN (SELECT fact_id FROM confirmations "
                                          "WHERE fact_id IS NOT NULL)")]
    for r in db.conn.execute("SELECT fact_ids FROM pending WHERE kind = 'confirm'"):
        ids = json.loads(r["fact_ids"] or "[]")
        if ids:
            f0 = _fact(db, ids[0])
            if f0:
                seen.append(f0["statement"] or "")
    for f in lifebase.current_facts(db, include_expired=True, today=today):  # 到期的读取时已经不算现在，这里要看到它们去问她
        if f["id"] in asked:
            continue
        expired = lifebase.fact_state(f, today) == "expired" or (  # “到期”只按 lifebase 一处判（决定 10）
            city and (f.get("until_cond") or "").startswith("city:") and f["until_cond"][5:].lower() != city.lower())
        if expired and f.get("until") and f["until"] < (today - timedelta(days=ASK_EXPIRED_DAYS)).isoformat():
            continue  # 早就过去的（按说法里的日子算到期的旧事）不问她，照到期处理
        if expired:
            n, users = _uses(db, f["id"])
            db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES ('expire', 'long', ?, ?, ?, ?, ?, ?)",
                            (f"到期了：这条还算数吗？（原定{('到 ' + f['until']) if f.get('until') else '在 ' + f['until_cond'][5:] + ' 期间'}）",
                             json.dumps([_option(db, f)], ensure_ascii=False), json.dumps([f["id"]]), json.dumps(users), n, _now()))
            got["expire"] += 1
            continue
        if _hers(f):
            continue
        if ONE_OFF.search(f["statement"] or ""):  # 某一天的事（“10-30 早上 8:43 才睡醒”）只是那天的记录，不当长期的去确认
            continue
        from .core.agent import similar_task
        if similar_task(f["statement"] or "", seen):  # 同一件事换个说法的，已经问过或她确认过就不再问
            continue
        n, users = _uses(db, f["id"])
        if n >= CONFIRM_USES:
            seen.append(f["statement"] or "")
            db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES ('confirm', 'long', ?, ?, ?, ?, ?, ?)",
                            ("常被用到，你还没确认过", json.dumps([_option(db, f)], ensure_ascii=False), json.dumps([f["id"]]),
                             json.dumps(users), n, _now()))
            got["confirm"] += 1
    db.conn.commit()
    return got


def add_disclosure_ask(db, user: str, question: str, category: str = None, draft: str = None, recipient: str = None,
                       statement_id: int = None) -> int:
    """对外使用者问到“问我”的类别（ontology-plan 3.3）：待你定里一条——谁问的、问了什么、拟答；她点“可以说”就记一次放行。
    同一个使用者、同一个问题还没定的，不重复建。"""
    ensure(db)
    who = memusers.check(user)
    question = (question or "").strip()[:200] or "（没说问什么）"
    asker = who["name"] + (f"替 {recipient} " if recipient else "")
    text = f"{asker}问：“{question}”"
    if db.conn.execute("SELECT 1 FROM pending WHERE status = 'open' AND kind = 'disclose' AND question = ?", (text,)).fetchone():
        return 0
    opt = {"text": (draft or "").strip(), "user": user, "tier": memusers.tier_of(user), "category": category,
           "category_name": lifebase.CATEGORY_NAMES.get(category, "合作邀约" if category is None else category),
           "recipient": recipient or "", "statement_id": statement_id, "asked": question,
           "src": f"{lifebase.TIER_NAMES[memusers.tier_of(user)]} · {lifebase.CATEGORY_NAMES.get(category, '合作邀约')}这一类现在是“问我”"}
    cur = db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) "
                          "VALUES ('disclose', 'long', ?, ?, '[]', ?, 0, ?)", (text, json.dumps([opt], ensure_ascii=False), json.dumps([user]), _now()))
    db.conn.commit()
    return cur.lastrowid


def _resolve_disclose(db, r, action: str, text: str = "") -> dict:
    """对外问她的那一条：yes = 这次可以说（对着来问的人；不知道是谁就是这一句长期放行）；always = 也把这一类改成“可以说”；
    own = 改一下再说（用她写的那句）；no = 不说。"""
    opt = (json.loads(r["options"]) or [{}])[0]
    say = (text or "").strip() if action == "own" else (opt.get("text") or "").strip()
    res = {"action": action, "text": say}
    if action in ("yes", "always", "own"):
        if not say:
            return {"ok": False, "msg": "写一句可以对外说的"}
        did = lifebase.approve_disclosure(db, say, f"她在待你定里点了可以说（{r['question']}）", to_users=[opt["user"]],
                                          dimension=lifebase.CATEGORY_DIM.get(opt.get("category")), once=bool(opt.get("recipient")),
                                          to_whom=opt.get("recipient") or None, statement_id=opt.get("statement_id"))
        res["disclosure"] = did
        msg = f"记下了：可以说“{say}”"
        if action == "always" and opt.get("category") in lifebase.CATEGORY_NAMES and opt.get("category") not in lifebase.LOCKED_DENY:
            lifebase.set_policy(db, opt["tier"], opt["category"], "allow")
            res["policy"] = [opt["tier"], opt["category"]]
            msg += f"；{lifebase.TIER_NAMES[opt['tier']]}档的“{opt['category_name']}”以后都可以说"
    elif action == "no":
        msg = "好，不说"
    else:
        return {"ok": False, "msg": "不认识的操作"}
    db.conn.execute("UPDATE pending SET status = 'done', resolved_at = ?, resolution = ? WHERE id = ?",
                    (_now(), json.dumps(res, ensure_ascii=False), r["id"]))
    db.conn.commit()
    return {"ok": True, "msg": msg}


def structure_seen(db) -> set:
    """提议过的规矩 / 在做的事 / 住处来自哪些说法（不管她点没点），不重复提（ethan/structure.py）。"""
    ensure(db)
    out = set()
    for r in db.conn.execute("SELECT fact_ids FROM pending WHERE kind = 'structure'"):
        out.update(json.loads(r["fact_ids"] or "[]"))
    return out


def add_structure(db, p: dict) -> bool:
    """进待你定一条：“这是你的规矩吗”“你现在在做这件事吗”“你住在这吗”。她点对才写进去（structure.apply）。"""
    from . import structure
    ensure(db)
    label = structure.TYPES[p["type"]]
    q = f"记成你的{label}？{p['text']}"
    if db.conn.execute("SELECT 1 FROM pending WHERE kind = 'structure' AND question LIKE ?", (q + "%",)).fetchone():
        return False  # 同一句已经问过（11-07 第一次整理出了三条“住在深圳”）
    extra = (f"（管：{p['scope']}）" if p["type"] == "rule" else
             f"（{p.get('start') or '?'} → {p.get('end') or '还没定'}）" if p["type"] == "situation" else "")
    db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, created_at, payload) VALUES ('structure', 'long', ?, ?, ?, ?, ?)",
                    (f"记成你的{label}？{p['text']}{extra}", json.dumps([{"text": p["text"], "src": f"你说过：{p['src']}"}], ensure_ascii=False),
                     json.dumps([p["fact"]]), _now(), json.dumps(p, ensure_ascii=False)))
    db.conn.commit()
    return True


def _resolve_structure(db, r, action: str, text: str = "", since: str = None, until: str = None) -> dict:
    from . import structure
    payload = json.loads(r["payload"] or "{}")
    if action == "no":
        db.conn.execute("UPDATE pending SET status = 'resolved', resolved_at = ?, resolution = 'no' WHERE id = ?", (_now(), r["id"]))
        db.conn.commit()
        return {"ok": True, "msg": "好，不记"}
    if action not in ("yes", "own") or (action == "own" and not (text or "").strip()):
        return {"ok": False, "msg": "点对，或者改一句再用"}
    got = structure.apply(db, payload, text if action == "own" else None, since, until)
    db.conn.execute("UPDATE pending SET status = 'resolved', resolved_at = ?, resolution = ? WHERE id = ?", (_now(), got[:200], r["id"]))
    db.conn.commit()
    return {"ok": True, "msg": f"记下了：{got}"}


def pending_items(db) -> list:
    ensure(db)
    rows = db.conn.execute("SELECT * FROM pending WHERE status = 'open' ORDER BY uses DESC, id").fetchall()
    names = {k: v["name"] for k, v in memusers.USERS.items()}
    return [{"id": r["id"], "kind": r["kind"], "type": r["type"], "question": r["question"], "options": json.loads(r["options"]),
             "users": [names.get(u, u) for u in json.loads(r["users"])], "uses": r["uses"],
             "gap": (r["origin"] if "origin" in r.keys() else None) == "gap", **_dates(r)} for r in rows]


def _dates(r) -> dict:
    """规矩 / 在做的事整理出来的起止日，待你定里先填上。"""
    if r["kind"] != "structure" or not r["payload"]:
        return {}
    p = json.loads(r["payload"])
    return {"since": p.get("start"), "until": p.get("end")}


def pending_count(db) -> int:
    try:
        ensure(db)
        # 小伴 补缺口的问题不算“待你定”（他在聊天里顺口问）
        return db.conn.execute("SELECT COUNT(*) FROM pending WHERE status = 'open' AND COALESCE(origin, '') != 'gap'").fetchone()[0]
    except Exception:
        return 0


def _snap(db, fids) -> dict:
    """撤销用的快照：这几条现在的有效期、状态、锁。"""
    return {str(fid): dict(db.conn.execute(f"SELECT {SNAP_COLS} FROM facts WHERE id = ?", (fid,)).fetchone() or {}) for fid in fids}


def _confirm(db, fid, source: str, note: str = ""):
    db.conn.execute("INSERT INTO confirmations (at, source, fact_id, note) VALUES (?, ?, ?, ?)", (_now(), source, fid, note[:200]))
    if fid:
        db.conn.execute("UPDATE facts SET locked = 1, confidence = 1.0, last_confirmed_at = ? WHERE id = ?", (_now(), fid))


def resolve(db, pid: int, action: str, pick: int = None, text: str = "", type_: str = "long", since: str = None,
            until: str = None, ranges: list = None) -> dict:
    """她在“待你定”里定了：
    pick：选第几个说法。选了她以前说过的 → 系统推出来的那条作废；选了新的、旧的是她说过的 → 旧的算“变了”，留作历史。
    yes / no：确认题（对 / 不对）；到期题（还算数 / 不算了）。
    own：都不对，自己写（长期带有效期，那一次记成事件）。
    both：都对，只是时间不同，ranges = [{since, until}]，按选项顺序。"""
    ensure(db)
    r = db.conn.execute("SELECT * FROM pending WHERE id = ? AND status = 'open'", (int(pid),)).fetchone()
    if r is None:
        return {"ok": False, "msg": "这件已经定过了"}
    if r["kind"] == "disclose":
        return _resolve_disclose(db, r, action, text)
    if r["kind"] == "style":  # 写作规范草稿：逐条对 / 不对 / 改（ethan/style.py，“提前做”），pick 是规则编号
        from . import style
        return style.resolve(db, r, action, pick, text)
    if r["kind"] == "structure":  # 规矩 / 在做的事 / 住处（ethan/structure.py）：点对才写进去
        return _resolve_structure(db, r, action, text, since, until)
    if r["kind"] == "skill":  # 新 skill 草稿 / 提议开 skill（ethan/skills.py）：批准才生效
        from . import skills
        return skills.resolve(db, r, action, text)
    opts, fids = json.loads(r["options"]), json.loads(r["fact_ids"])
    today = her_today().isoformat()
    confirm_from = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM confirmations").fetchone()[0]
    snap = _snap(db, fids)
    before_max = db.conn.execute("SELECT COALESCE(MAX(id), 0) FROM facts").fetchone()[0]
    if action == "pick":
        chosen = fids[int(pick)]
        if since or until:  # 选定的这条从哪天到哪天算（时间填在按钮上面）
            db.conn.execute("UPDATE facts SET valid_from = COALESCE(?, valid_from), until = ? WHERE id = ?", (since, until, chosen))
        _confirm(db, chosen, "pending")
        for fid in fids:
            if fid == chosen:
                continue
            f = _fact(db, fid)
            if _hers(f) and fid != chosen:  # 她以前说的，现在不一样了：到今天为止，留作历史
                lifebase.supersede(db, fid, chosen, today, force=True)
            else:  # 系统推错了
                lifebase.invalidate(db, fid, force=True)
        msg = f"已定：{opts[int(pick)]['text']}"
    elif action in ("yes", "no"):
        if not fids:
            return {"ok": False, "msg": "这件要你写一句"}
        fid = fids[0]
        if r["kind"] == "expire":
            if action == "yes":  # 还算数：去掉到期（她填了新的到期日就换成新的）
                db.conn.execute("UPDATE facts SET until = ?, until_cond = NULL WHERE id = ?", (until, fid))
                _confirm(db, fid, "pending", "到期了还算数")
            else:  # 不算了：到今天为止，留作历史（不是错）
                lifebase.supersede(db, fid, fid, today, force=True)
        elif action == "yes":
            if since or until:  # 对，但她改了时间（从哪天起 / 到哪天）
                db.conn.execute("UPDATE facts SET valid_from = COALESCE(?, valid_from), until = ? WHERE id = ?", (since, until, fid))
            _confirm(db, fid, "pending")
        else:
            lifebase.invalidate(db, fid, force=True)
        msg = "已定"
    elif action == "own":
        text = (text or "").strip()
        if not text:
            return {"ok": False, "msg": "写一句正确的"}
        first = _fact(db, fids[0]) if fids else None
        gap = "origin" in r.keys() and r["origin"] == "gap"
        if gap:  # 补缺口的题：她答的是她的原话（ontology-plan 1.7），问题照写，方便看出语境
            ev = lifebase.add_evidence(db, "chat", f"gap:{pid}:{_now()}", _now(), f"问：{r['question']}\n答：{text}")
        else:
            ev = lifebase.add_evidence(db, "correction", f"pending:{pid}:{_now()}", _now(), f"她在待你定里写：{text}")
        if type_ == "once":
            lifebase.add_event(db, since or today, text[:30], [ev], "manual", summary=text)
        else:
            dim = (first or {}).get("dimension") or (r["dim"] if gap and r["dim"] in lifebase.DIMENSIONS else None) or "preferences"
            fid = lifebase.add_fact(db, dim, text, [ev], confidence=1.0, importance=5,
                                    tags=("her_answer",) if gap else ("her_correction",), valid_from=since,
                                    context=f"小伴 问“{r['question']}”时她答的" if gap else None)
            db.conn.execute("UPDATE facts SET locked = 1, until = ? WHERE id = ?", (until, fid))
            for old in fids:
                f = _fact(db, old)
                if _hers(f):
                    lifebase.supersede(db, old, fid, since or today, force=True)
                else:
                    lifebase.invalidate(db, old, force=True)
        db.conn.execute("INSERT INTO confirmations (at, source, fact_id, note) VALUES (?, 'pending', NULL, ?)", (_now(), text[:200]))
        msg = f"已记下：{text}"
    elif action == "both":
        for fid, rg in zip(fids, ranges or []):
            db.conn.execute("UPDATE facts SET valid_from = COALESCE(?, valid_from), until = ?, locked = 1, confidence = 1.0 WHERE id = ?",
                            (rg.get("since"), rg.get("until"), fid))
            _confirm(db, fid, "pending", "都对，时间不同")
        msg = "两条都留着，各按时间算"
    else:
        return {"ok": False, "msg": "不认识的操作"}
    created = [x["id"] for x in db.conn.execute("SELECT id FROM facts WHERE id > ?", (before_max,))]
    # 撤销只撤这次自己的：记下操作后的样子和这次写的确认编号（Codex 评审 11-07 第 2 条：原来按“某个编号之后的全删”，会删到别的操作的确认）
    confirms = [x[0] for x in db.conn.execute("SELECT id FROM confirmations WHERE id > ?", (confirm_from,))]
    db.conn.execute("UPDATE pending SET status = 'done', resolved_at = ?, resolution = ? WHERE id = ?",
                    (_now(), json.dumps({"action": action, "pick": pick, "text": text, "type": type_, "since": since, "until": until,
                                         "snap": snap, "after": _snap(db, fids), "created": created, "confirms": confirms},
                                        ensure_ascii=False), int(pid)))
    db.conn.commit()
    return {"ok": True, "msg": msg, "undo": {"action": "me.reopen", "id": int(pid)}}


def reopen(db, pid: int) -> dict:
    """她点了“撤销”：这件定的作废，恢复原样，回到待你定。"""
    ensure(db)
    r = db.conn.execute("SELECT * FROM pending WHERE id = ? AND status = 'done'", (int(pid),)).fetchone()
    if r is None:
        return {"ok": False, "msg": "撤销不了"}
    res = json.loads(r["resolution"] or "{}")
    after = res.get("after")  # 旧记录没有：只按“删掉的、停用的不动”挡
    kept = 0
    for fid, row in (res.get("snap") or {}).items():
        # 删除优先于撤销、后来又改过的不动，都由 lifebase.undo_fact 判（Codex 评审 11-07 第 2 条）
        if row and not lifebase.undo_fact(db, int(fid), row, (after or {}).get(fid) if after is not None else None):
            kept += 1
    for fid in res.get("created") or []:
        lifebase.invalidate(db, fid, force=True)  # 撤销时新建的那几条作废（作废只走 lifebase 一个入口；删掉的不动）
    if "confirms" in res:
        ids = [int(i) for i in res["confirms"]]
        db.conn.execute(f"DELETE FROM confirmations WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)
    elif res.get("confirm_from"):  # 旧记录只有起点：只删这件待你定那一刻写的，别的操作写的留着
        db.conn.execute("DELETE FROM confirmations WHERE id > ? AND source = 'pending' AND at <= ?", (res["confirm_from"], r["resolved_at"]))
    db.conn.execute("UPDATE pending SET status = 'open', resolved_at = NULL, resolution = NULL WHERE id = ?", (int(pid),))
    db.conn.commit()
    return {"ok": True, "msg": "撤销了，回到待你定" + (f"（{kept} 条后来删过或改过，保持现在的样子）" if kept else "")}


# ---------- 问一问 ----------
def ask(db, router, question: str) -> dict:
    """同一个问题，小伴 和Days各会怎么答（只是问问，不发给任何人）。"""
    from . import memsearch
    ensure(db)
    question = (question or "").strip()[:200]
    if not question:
        return {"ok": False, "msg": "问一句"}
    memsearch.refresh(db)
    from . import askroute, onto_query
    jobs = []
    asked = []  # Days 按对外开关（ontology-plan 3.3）：“问我”的那几类只知道要先问她，不给内容
    # 和聊天一样按题分流（11-06：问一问“我昨天干嘛去了”只走了兜底检索，答“我不知道”）：命中能力题的，题函数的结果放在最前
    routed = askroute.route(db, question, user="ethan")
    via = f"q{routed['q']}" if routed else None
    day_ask = onto_query.day_question(question)  # 她哪天做了什么、去了哪是行程：对外一律“不方便说”，不查也不问模型
    for user, who in (("ethan", "小伴"), ("twin", "Days（她主页上的 AI，只能用对外开关里“可以说”的那部分）")):
        if user == "twin" and day_ask:
            onto_query.day_summary(db, "twin", day_ask[2])  # 只留一笔使用记录（没权限），不给内容
            found = []
        else:
            found = (memsearch.search(db, [question], "chat", user="ethan", via=via) if user == "ethan"
                     else memsearch.search(db, [question], "public", user="twin", asked=asked))
        jobs.append((user, who, found))
    ask_names = sorted({lifebase.CATEGORY_NAMES.get(a["category"], "") for a in asked} - {""})

    def memory(j):
        text = memsearch.lines(j[2]) or "（没有）"
        if j[0] == "ethan" and routed:
            text = "按题查到的（以这个为准）：\n" + askroute.prompt_text(routed) + "\n\n自由检索到的：\n" + text
        if j[0] == "twin" and ask_names:
            text += f"\n（还有“{'、'.join(ask_names)}”这类的，要先问她才能说：被问到时就说“{ASK_TWIN}”）"
        return text

    def answer(j):
        if j[0] == "twin" and day_ask:
            return onto_query.REFUSE
        return router.complete(ANSWER.format(who=j[1], memory=memory(j)), [{"role": "user", "content": question}], purpose="me-ask").text.strip()
    from concurrent.futures import ThreadPoolExecutor  # 两个一起问，省一半时间
    with ThreadPoolExecutor(2) as pool:
        texts = list(pool.map(answer, jobs))
    out = [{"user": user, "name": memusers.USERS[user]["name"], "answer": text[:400], "keys": [f["key"] for f in found][:5],
            "sources": ([f"按题查（第 {routed['q']} 题 {routed['name']}）"] if user == "ethan" and routed else [])
                       + [f"{f['label']}{'｜' + f['at'] if f['at'] else ''}：{f['text'][:60]}" for f in found[:2]],
            **({"ask": ask_names} if user == "twin" and ask_names else {})}
           for (user, who, found), text in zip(jobs, texts)]
    cur = db.conn.execute("INSERT INTO ask_log (at, question, answers) VALUES (?, ?, ?)", (_now(), question, json.dumps(out, ensure_ascii=False)))
    db.conn.commit()
    return {"ok": True, "id": cur.lastrowid, "question": question, "answers": out}


def recent_questions(db, n: int = 5) -> list:
    ensure(db)
    seen, out = set(), []
    for r in db.conn.execute("SELECT question FROM ask_log ORDER BY id DESC LIMIT 30"):
        if r["question"] not in seen:
            seen.add(r["question"])
            out.append(r["question"])
    return out[:n]


def compare(router, a: str, b: str) -> dict:
    """她两个都点了“对”：先看它们是不是真的矛盾。"""
    text = router.complete(COMPARE, [{"role": "user", "content": f"回答一：{a}\n回答二：{b}"}], purpose="me-compare").text
    try:
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
        return {"conflict": bool(data.get("conflict")), "why": str(data.get("why") or "")[:100]}
    except ValueError:
        return {"conflict": True, "why": "没判断出来，按矛盾处理"}


def verdict(db, ask_id: int, marks: dict, text: str = "", type_: str = "long", since: str = None, until: str = None,
            split: list = None, kind: str = "wrong") -> dict:
    """问一问之后她的判断。marks = {user: "ok" | "bad"}。
    - 有“对”的：那句就是正确说法，说错的各开一张纠错单；
    - 都“不对”：用她写的 text；
    - 两个都“对”但矛盾（前端先调 compare）：split = [{user, text, since, until}]，各带有效期留着。"""
    ensure(db)
    r = db.conn.execute("SELECT * FROM ask_log WHERE id = ?", (int(ask_id),)).fetchone()
    if r is None:
        return {"ok": False, "msg": "找不到这次提问"}
    answers = {a["user"]: a for a in json.loads(r["answers"])}
    tickets = []
    if split:
        for s in split:
            ev = lifebase.add_evidence(db, "correction", f"ask:{ask_id}:{s.get('user')}:{_now()}", _now(), f"她在问一问里说：{s['text']}")
            fid = lifebase.add_fact(db, "preferences", s["text"], [ev], confidence=1.0, importance=5, tags=("her_correction",),
                                    valid_from=s.get("since"))
            db.conn.execute("UPDATE facts SET locked = 1, until = ? WHERE id = ?", (s.get("until"), fid))
            _confirm(db, fid, "ask", "都对，时间不同")
        msg = "两条都留着，各按时间算"
    else:
        good = [u for u, m in marks.items() if m == "ok"]
        right = answers[good[0]]["answer"] if good else (text or "").strip()
        if not right:
            return {"ok": False, "msg": "写一句正确的"}
        for u in good:
            for k in answers[u]["keys"]:
                if k.startswith("fact:"):
                    _confirm(db, int(k.split(":")[1]), "ask")
        if not any(answers[u]["keys"] for u in good):
            db.conn.execute("INSERT INTO confirmations (at, source, note) VALUES (?, 'ask', ?)", (_now(), right[:200]))
        for u, m in marks.items():
            if m != "bad" or u not in answers:
                continue
            if type_ == "once" and not good:
                ev = lifebase.add_evidence(db, "correction", f"ask:{ask_id}:once:{_now()}", _now(), f"她在问一问里说：{right}")
                lifebase.add_event(db, since or her_today().isoformat(), right[:30], [ev], "manual", summary=right)
                continue
            tickets.append(memusers.open_ticket(db, right, answers[u]["answer"], user=u, entry="ask", quote=r["question"], kind=kind,
                                                since=since, until=until)["id"])
        msg = f"记下了，开了 {len(tickets)} 张纠错单" if tickets else "记下了"
    db.conn.execute("UPDATE ask_log SET verdict = ? WHERE id = ?", (json.dumps({"marks": marks, "text": text, "split": split}, ensure_ascii=False), int(ask_id)))
    db.conn.commit()
    return {"ok": True, "msg": msg, "tickets": tickets}


# ---------- 纠错单：撤销 ----------
def undo_ticket(db, tid: int) -> dict:
    """她点了“撤销这次纠正”：新记的那条作废，旧的那条恢复，这张单不再算数。"""
    ensure(db)
    t = db.conn.execute("SELECT * FROM corrections WHERE id = ? AND undone_at IS NULL", (int(tid),)).fetchone()
    if t is None:
        return {"ok": False, "msg": "这张已经撤销过了"}
    if t["fact_id"]:
        f = _fact(db, t["fact_id"])
        # 这张单自己新记的那条才作废（之前已有、只是被锁上的不动）。单子先记说法后写时间，跨秒时差一两秒，留 2 分钟余量
        opened = (datetime.fromisoformat(t["at"][:19]) - timedelta(minutes=2)).isoformat(timespec="seconds")
        if f and "her_correction" in (f["tags"] or "") and (f["recorded_at"] or "")[:19] >= opened:
            lifebase.invalidate(db, t["fact_id"], force=True)
    if t["wrong_fact_id"]:
        db.conn.execute("UPDATE facts SET valid_to = NULL, superseded_by = NULL, status = 'active' WHERE id = ?", (t["wrong_fact_id"],))
    db.conn.execute("UPDATE corrections SET undone_at = ? WHERE id = ?", (_now(), int(tid)))
    try:
        db.conn.execute("UPDATE eval_items SET active = 0 WHERE ticket_id = ?", (int(tid),))
    except Exception:
        pass
    db.conn.commit()
    return {"ok": True, "msg": "撤销了"}


# ---------- 页面数据 ----------
def _week_rate(db, start: date, end: date):
    a, b = start.isoformat(), end.isoformat()
    ok = db.conn.execute("SELECT COUNT(*) FROM confirmations WHERE at >= ? AND at < ?", (a, b)).fetchone()[0]
    bad = db.conn.execute("SELECT COUNT(*) FROM corrections WHERE at >= ? AND at < ? AND kind = 'wrong' AND undone_at IS NULL "
                          "AND about = 'her'", (a, b)).fetchone()[0]
    return {"confirmed": ok, "wrong": bad, "rate": round(ok / (ok + bad), 3) if ok + bad else None}


def page(db, today: date = None) -> dict:
    ensure(db)
    from . import memeval
    today = today or her_today()
    monday = clock.week_start(today)
    weeks = []
    for k in range(4, -1, -1):
        start = monday - timedelta(weeks=k)
        weeks.append({"week": start.isoformat()[5:], **_week_rate(db, start, start + timedelta(days=7))})
    this = weeks[-1]
    users = memusers.entity_stats(db)  # 按实体：小伴、个人主页、OS
    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    for u in users:  # 每个实体这周用得最多的记忆
        counts = {}
        marks = ",".join("?" * len(u["members"]))
        for row in db.conn.execute(f"SELECT keys FROM retrieval_log WHERE user IN ({marks}) AND at >= ?", (*u["members"], since)):
            for k in json.loads(row["keys"] or "[]"):
                if k.startswith("fact:"):
                    counts[k] = counts.get(k, 0) + 1
        top = []
        for k, n in sorted(counts.items(), key=lambda x: -x[1])[:4]:
            f = _fact(db, int(k.split(":")[1]))
            if f:
                top.append({"text": f["statement"], "n": n})
        u["top"] = top
        u["last_week"] = _user_week(db, u["members"], monday - timedelta(days=7), monday)
    tickets = []
    for t in db.conn.execute("SELECT * FROM corrections WHERE undone_at IS NULL ORDER BY id DESC LIMIT 60").fetchall():
        used = db.conn.execute("SELECT at FROM retrieval_log WHERE at >= ? AND keys LIKE ? AND COALESCE(user, '') != 'system' ORDER BY id",
                               (t["at"], f'%"fact:{t["fact_id"]}"%')).fetchall() if t["fact_id"] else []
        again = db.conn.execute("SELECT at FROM corrections WHERE recurrence_of = ? AND undone_at IS NULL", (t["id"],)).fetchall()
        ent = memusers.entity_of(t["user"])
        tickets.append({"id": t["id"], "at": t["at"][:10], "user": ent, "user_name": memusers.ENTITIES.get(ent, (t["user"],))[0],
                        "entry": {"chat": "聊天里", "ask": "问一问", "dev_session": "你发给开发会话", "pending": "待你定", "me_page": "全部记忆里改的"}.get(t["entry"], t["entry"]),
                        "layer": memusers.LAYERS.get(t["layer"], t["layer"]), "kind": t["kind"], "quote": t["quote"], "wrong": t["wrong"],
                        "right": t["right"], "used_after": len(used), "recurred": [a["at"][:10] for a in again]})
    summary = memeval.weekly_summary(db, today)
    all_wrong = db.conn.execute("SELECT COUNT(*) FROM corrections WHERE at >= ? AND kind = 'wrong' AND undone_at IS NULL",
                                (monday.isoformat(),)).fetchone()[0]  # 含 小伴 记错他自己的事（对得上的比例只算她的事）
    return {"corrections": all_wrong, "rate": this["rate"], "confirmed": this["confirmed"], "wrong": this["wrong"], "weeks": weeks, "users": users,
            "tickets": tickets, "pending": pending_count(db), "recurrences": sum(1 for t in tickets if t["recurred"] and t["at"] >= monday.isoformat()),
            "eval": {"rate": summary.get("检验题通过率"), "last": summary.get("上周通过率"), "total": summary.get("检验题数")},
            "asked": summary.get("她确认过的"), "recent_questions": recent_questions(db), "gaps": gap_week(db, today),
            "metrics": _metrics_safe(db, today)}


def _metrics_safe(db, today):
    try:  # 第一屏的体检单和星图（ontology-plan 2.2）；算不出来不挡整页
        m = metrics(db, today)
        return m
    except Exception as e:
        print(f"[mepage] 指标没算成：{type(e).__name__} {e}", flush=True)
        return None


def _user_week(db, users, start: date, end: date):
    users = [users] if isinstance(users, str) else list(users)
    a, b, marks = start.isoformat(), end.isoformat(), ",".join("?" * len(users))
    n = db.conn.execute(f"SELECT COUNT(*) FROM retrieval_log WHERE user IN ({marks}) AND round = 1 AND at >= ? AND at < ?", (*users, a, b)).fetchone()[0]
    bad = db.conn.execute(f"SELECT COUNT(*) FROM corrections WHERE user IN ({marks}) AND kind = 'wrong' AND undone_at IS NULL AND at >= ? AND at < ?",
                          (*users, a, b)).fetchone()[0]
    return round(1 - min(bad, n) / n, 3) if n else None


def verdict_fact(db, fid: int, action: str, root=None) -> dict:
    """“全部记忆”里一条的四个按钮（ontology-plan 2.2，me-page-v2.md 8.1）：
    你说的：对（确认、锁住）/ 删掉（她勾选确认后才调，永久删、同一来源不再导入）；“不对”“变了”走 fix_fact；
    系统推的（猜测）：像我（确认）/ 不像我（作废）/ 说不清（记一笔，不算确认也不算错）。"""
    ensure(db)
    f = _fact(db, fid)
    if not f:
        return {"ok": False, "msg": "找不到这条"}
    if action in ("yes", "like"):
        _confirm(db, f["id"], "me_all", "像我" if action == "like" else "")
        msg = "记下了：这条对"
    elif action == "unlike":
        lifebase.invalidate(db, f["id"], force=True)
        db.conn.execute("INSERT INTO confirmations (at, source, fact_id, note) VALUES (?, 'me_all_no', NULL, ?)", (_now(), f"fact:{f['id']} 不像我"))
        msg = "好，这条不算了"
    elif action == "unsure":
        db.conn.execute("INSERT INTO confirmations (at, source, fact_id, note) VALUES (?, 'me_all_unsure', NULL, ?)", (_now(), f"fact:{f['id']} 说不清"))
        msg = "记下了：你说不清，先放着"
    elif action == "forget":
        lifebase.forget(db, fact_ids=[f["id"]], root=root)
        msg = "删掉了，同一来源以后不会再记回来"
    else:
        return {"ok": False, "msg": "不认识的操作"}
    db.conn.commit()
    return {"ok": True, "msg": msg}


def all_memory(db, q: str = "", dim: str = "", history: bool = False, limit: int = 100, prop: str = "") -> dict:
    """全部记忆：按方面分，搜得到；🔒 是她定过的；默认只看现在成立的。"""
    ensure(db)
    rows = db.conn.execute("SELECT * FROM facts WHERE subject = 'self' ORDER BY importance DESC, id DESC").fetchall()
    counts, out = {}, []
    from . import memsearch
    near = {int(k.split(":")[1]) for k, _ in memsearch.similar_keys(db, q, top=60, prefix="fact:")} if q else set()  # 按意思也算（语义检索开着时）
    today = her_today()
    used = _use_counts(db, 3650)
    for f in rows:
        f = dict(f)
        state = lifebase.fact_state(f, today)  # 默认列现在的和到期的（标“到期了”，决定 10）；历史里列其余的（lifebase.MEM_VIEWS）
        if state not in lifebase.MEM_VIEWS["me_history" if history else "me_now"]["states"]:
            continue
        if q and f["id"] not in near and not _matches(q, " ".join(str(f.get(k) or "") for k in ("statement", "detail", "context"))):
            continue
        counts[f["dimension"]] = counts.get(f["dimension"], 0) + 1  # 标签上的数是搜索结果里各方面的条数
        if dim and f["dimension"] != dim:
            continue
        if prop and (f.get("prop") or "") != (prop if prop != "-" else ""):  # 星图往下钻：只看这个方面里的这一项（“-” 是没细分的）
            continue
        n = used.get(f["id"], 0)
        out.append({"id": f["id"], "text": f["statement"], "dim": f["dimension"], "dim_name": lifebase.DIM_NAMES.get(f["dimension"], f["dimension"]),
                    "locked": bool(f["locked"]), "mine": _hers(f), "from": (f["valid_from"] or f["recorded_at"] or "")[:10],
                    "to": (f["valid_to"] or "")[:10], "until": f.get("until"), "uses": n, "status": f["status"],
                    "state": state, "state_label": lifebase.MEM_LABEL[state],
                    "said": SAID.get(f.get("source_kind") or "", ""), "kind": f.get("kind"), "context": f.get("context") or "", "prop": f.get("prop") or ""})
    items = out[:limit]
    _attach_similar(db, items)
    return {"items": items, "more": len(out) > limit, "dims": [{"key": k, "name": lifebase.DIM_NAMES.get(k, k), "n": counts[k]}
                                         for k in lifebase.DIMENSIONS if k in counts], "total": sum(counts.values())}


SIMILAR_TOP = 3


def _attach_similar(db, items: list) -> None:
    """每条下面挂意思相近的几条（借鉴 8）：方便她一眼看出重复、矛盾。只是给她看，不合并、不改。
    只列关于她本人、没删没停用的；以前的（过时了、到期了）也列，标出来——矛盾常常就是一新一旧。"""
    from . import memsearch
    try:
        near = memsearch.similar_to(db, [f"fact:{x['id']}" for x in items], user="me_similar", top=SIMILAR_TOP + 2)
    except Exception as e:
        print(f"[mepage] 相近的没算出来：{type(e).__name__} {e}", flush=True)
        near = {}
    ids = {int(k.split(":")[1]) for v in near.values() for k, _ in v}
    today = her_today().isoformat()
    cond, args = lifebase.mem_where("me_similar", "facts", today)
    rows = {r["id"]: dict(r) for r in db.conn.execute(
        f"SELECT * FROM facts WHERE id IN ({','.join('?' * len(ids)) or 'NULL'}) AND subject = 'self' AND {cond}", (*sorted(ids), *args))}
    for x in items:
        sims = []
        for k, s in near.get(f"fact:{x['id']}", []):
            f = rows.get(int(k.split(":")[1]))
            if not f:
                continue
            st = lifebase.fact_state(f, today)
            past = "过时了" if st in ("past", "retracted") else lifebase.MEM_LABEL[st] if st in ("expired", "candidate") else ""
            sims.append({"id": f["id"], "text": f["statement"], "from": (f["valid_from"] or f["recorded_at"] or "")[:10],
                         "locked": bool(f["locked"]), "past": past, "sim": s})
        x["similar"] = sims[:SIMILAR_TOP]


def days(db, wiki, q: str = "", limit: int = 30) -> dict:
    """发生过的事：按天，每天的记忆日记开头一段 + 那天记下的事件，新的在前（“全部记忆”第二个标签）。"""
    import re as _re
    lifebase.ensure(db)
    by = {}
    for slug in wiki.pages():
        m = _re.fullmatch(r"journal/(\d{4}-\d{2}-\d{2})", slug)
        if m:
            body = wiki.read(slug)["body"]
            para = next((p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().startswith("#")), "")
            by.setdefault(m.group(1), {"day": m.group(1), "journal": para[:400], "events": []})["journal"] = para[:400]
    for e in db.conn.execute("SELECT occurred_at, title, summary FROM events ORDER BY occurred_at DESC LIMIT 2000"):
        d = (e["occurred_at"] or "")[:10]
        by.setdefault(d, {"day": d, "journal": "", "events": []})["events"].append({"title": e["title"], "summary": e["summary"] or ""})
    out = sorted(by.values(), key=lambda x: x["day"], reverse=True)
    if q:
        out = [x for x in out if q in x["journal"] or any(q in ev["title"] or q in ev["summary"] for ev in x["events"])]
    return {"items": out[:limit], "more": len(out) > limit, "total": len(out)}


def people(db, q: str = "", limit: int = 100) -> dict:
    """人：每个人一张卡（是谁、怎么认识、最近一次联系）。"""
    rows = [dict(r) for r in db.conn.execute("SELECT name, circle, how_met, notes, last_contact, birthday FROM people "
                                             "ORDER BY last_contact IS NULL, last_contact DESC, name")]
    if q:
        rows = [r for r in rows if any(q in (r[k] or "") for k in ("name", "circle", "how_met", "notes"))]
    for r in rows:
        r.pop("birthday", None)  # 页面上不显示别人的生日
    return {"items": rows[:limit], "more": len(rows) > limit, "total": len(rows)}


def fix_fact(db, fid: int, text: str, kind: str = "wrong", type_: str = "long", since: str = None, until: str = None) -> dict:
    """全部记忆里点“不对”：写对的那句，选是错了还是变了。"""
    f = _fact(db, fid)
    if f is None:
        return {"ok": False, "msg": "找不到这条"}
    t = memusers.open_ticket(db, text, f["statement"], user="ethan", entry="me_page", quote=text, dimension=f["dimension"],
                             kind=kind, since=since, until=until)
    # 她点的就是这一条：纠错单按文字找旧说法，找偏了（或这条她以前确认过、锁着）也要把这条换下来。
    # 锁只挡系统，不挡她自己
    new_id = t.get("fact_id")
    now = _fact(db, f["id"])
    if new_id and new_id != f["id"] and now and now["valid_to"] is None and now["status"] not in ("invalid", "deleted"):
        lifebase.supersede(db, f["id"], new_id, when=since, force=True)
        if kind == "wrong":
            lifebase.invalidate(db, f["id"], force=True)
        db.conn.commit()
    return {"ok": True, "msg": "改好了" if kind == "wrong" else "记下了，旧的留作历史", "ticket": t["id"]}


# ---------- 小伴 在聊天里问 ----------
CHAT_MARK = re.compile(r"\[待定\s*[#＃](\d+)\s+(选|写|问了)\s*([^\]]*)\]")


def chat_items(db, n: int = 2) -> list:
    """给 小伴 聊天用：最近三天问过的先不再问。"""
    items = pending_items(db)
    cut = (datetime.now(timezone.utc) - timedelta(days=ASK_GAP_DAYS)).isoformat(timespec="seconds")
    paused = gap_paused(db)  # 她说今天别问：补缺口的题今天不问（她自己要定的那些照常）
    fresh = {r["id"] for r in db.conn.execute("SELECT id FROM pending WHERE status = 'open' AND (asked_at IS NULL OR asked_at < ?) "
                                              "AND NOT (? AND COALESCE(origin, '') = 'gap')", (cut, 1 if paused else 0))}
    return [p for p in items if p["id"] in fresh][:n]


def note_chat(db, text: str) -> list:
    """小伴 回复里的 [待定 #编号 选 1] / [待定 #编号 写 她说的]：她在聊天里定了，这里同样少一件。"""
    out = []
    for pid, verb, rest in CHAT_MARK.findall(text or ""):
        try:
            if verb == "问了":  # 他问了、她还没答：三天内不再问
                db.conn.execute("UPDATE pending SET asked_at = ? WHERE id = ?", (_now(), int(pid)))
                db.conn.commit()
            elif verb == "选":
                item = next((p for p in pending_items(db) if p["id"] == int(pid)), None)
                if item and item["kind"] in ("confirm", "expire", "question") and rest.strip() in ("对", "还算数", "是"):
                    out.append(resolve(db, int(pid), "yes"))
                elif item and item["kind"] in ("confirm", "expire", "question") and rest.strip() in ("不对", "不算了", "否"):
                    out.append(resolve(db, int(pid), "no"))
                else:
                    out.append(resolve(db, int(pid), "pick", pick=int(rest.strip() or 1) - 1))
            else:
                out.append(resolve(db, int(pid), "own", text=rest.strip()))
        except Exception as e:
            print(f"[mepage] 聊天里的待定没记上：{type(e).__name__} {e}", flush=True)
    return out


def strip(text: str) -> str:
    return CHAT_MARK.sub("", text)



# ---------- 小伴 主动补缺口（ontology-plan 1.7） ----------
# 每天能挑的都挑（不设上限）：先看哪些能力题最近答不上，再看哪些方面空着、很薄。先查再问：她的聊天、文件里其实提过的，
# 不问她（交给每晚整理去关联，记一笔），真缺才问。题放进“待你定”，小伴 聊天时顺口问（三天内不重复，见 chat_items），
# 她挑着答；她说“今天别问”当天就停。她来聊天时问（打扰登记 gap-ask）；每天下午傍晚的“聊聊”主动带一题（nudges.py lesson-chat，11-04）。
GAP_DAILY, GAP_THIN = 20, 3  # 实际就是能挑的都挑（最多十几个）
GAP_STOP = re.compile(r"今天(先)?别问|别再问了|今天不想(答|回答)|不想被问")
GAP_FN = {  # 能力题答不上时，问她什么（只挑她自己答得上的；钱、作息这种看记录的不问她）
    "whereabouts_now": ("你现在平时住在哪个城市？最近常待的地方有哪些？", "住在 城市", "resources"),
    "background": ("你读过哪些学校、做过哪些工作？方便的话说说大概的时间。", "学校 读书 工作 公司", "abilities"),
    "fixed_schedule": ("你每周有哪些雷打不动的安排？比如上课、训练、固定的会。", "每周 固定 上课 训练", "rhythms"),
    "preferences": ("最近有什么特别喜欢或特别受不了的东西？吃的、用的、去的地方都行。", "喜欢 讨厌 受不了", "preferences"),
}
GAP_DIM = {  # 方面空着或很薄时问什么（关键词用来先查她有没有提过）
    "values": ("有什么事是你特别看重、怎么都不愿意妥协的？", "看重 原则 底线"),
    "temperament": ("你心情不好的时候一般会怎么样？什么能让你很快好起来？", "心情 难过 焦虑 开心"),
    "thinking": ("你做大决定的时候，一般是怎么想的？会先问谁吗？", "决定 纠结 选择"),
    "abilities": ("你最拿手的几件事是什么？有没有最近在学的？", "擅长 学会 技能"),
    "expression": ("你喜欢别人怎么跟你说话？直接一点还是委婉一点？", "说话 直接 委婉"),
    "preferences": ("最近有什么特别喜欢的吃的、地方或者东西？", "喜欢 爱吃 想去"),
    "body": ("最近身体怎么样？有没有要一直注意的地方？", "身体 过敏 疼 睡眠"),
    "relationships": ("最近最常联系的几个人是谁？", "朋友 家人 妈妈 爸爸"),
    "resources": ("最近手头在忙的大事有哪些？住、学习、工作上有什么变化吗？", "签证 学校 工作 房子"),
    "rhythms": ("你平时一般几点起、几点睡？周末和工作日差别大吗？", "起床 睡觉 周末"),
}
# 薄的方面不只问一句，给 小伴 一串接着问的（她答了上一个，下一轮接着问下一个）
GAP_FOLLOW = {
    "values": ["你做选择的时候，最不能让步的是什么？能举一件最近的事吗？", "有没有哪件事，你后来觉得当时坚持对了？",
               "你最佩服的人身上，有哪一点是你也想要的？", "什么样的事会让你觉得“这不对”，哪怕别人都觉得没什么？"],
    "thinking": ["最近一次纠结了很久的决定是什么？最后怎么定的？", "你一般先想清楚再做，还是先做起来再调？",
                 "做决定前你会找谁商量吗？还是更相信自己的判断？"],
    "expression": ["你说话更喜欢直接还是委婉？别人怎么跟你说话你最舒服？", "你生气或者不开心的时候，一般会说出来吗？"],
}
GAP_SCHEMA = """CREATE TABLE IF NOT EXISTS gap_log (id INTEGER PRIMARY KEY, day TEXT NOT NULL, key TEXT NOT NULL, question TEXT NOT NULL,
    outcome TEXT NOT NULL, pending_id INTEGER, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gap_pause (day TEXT PRIMARY KEY, at TEXT NOT NULL, quote TEXT)"""


def gap_note_her(db, text: str, today: date = None) -> bool:
    """她在聊天里说“今天别问”：当天不再挑题，已经挑的也不问。"""
    if not GAP_STOP.search(text or ""):
        return False
    db.conn.executescript(GAP_SCHEMA)
    db.conn.execute("INSERT OR REPLACE INTO gap_pause (day, at, quote) VALUES (?, ?, ?)",
                    ((today or her_today()).isoformat(), _now(), (text or "")[:100]))
    db.conn.commit()
    return True


def gap_paused(db, today: date = None) -> bool:
    db.conn.executescript(GAP_SCHEMA)
    return db.conn.execute("SELECT 1 FROM gap_pause WHERE day = ?", ((today or her_today()).isoformat(),)).fetchone() is not None


def _day_start_utc(today: date) -> str:
    """她那天 0 点（她的时区）对应的 UTC 时间，和 pending.asked_at 同一种写法。"""
    from .clock import her_tz
    start = datetime.combine(today, datetime.min.time(), her_tz())
    return start.astimezone(timezone.utc).isoformat(timespec="seconds")


def gap_asked_today(db, today: date = None) -> bool:
    """今天（她的日子）小伴 已经问过至少一个补缺口的题。"""
    ensure(db)
    return db.conn.execute("SELECT 1 FROM pending WHERE origin = 'gap' AND asked_at >= ? LIMIT 1",
                           (_day_start_utc(today or her_today()),)).fetchone() is not None


def top_gap(db, today: date = None):
    """主动“聊聊”要带的一题：挑题时排得最靠前（最近一天挑的、同一天里先挑的）、还开着、三天内没问过的。
    她说“今天别问”就没有。"""
    ensure(db)
    db.conn.executescript(GAP_SCHEMA)
    if gap_paused(db, today):
        return None
    cut = (datetime.now(timezone.utc) - timedelta(days=ASK_GAP_DAYS)).isoformat(timespec="seconds")
    r = db.conn.execute("SELECT p.id, p.question, p.dim FROM pending p LEFT JOIN gap_log g ON g.pending_id = p.id "
                        "WHERE p.status = 'open' AND p.origin = 'gap' AND (p.asked_at IS NULL OR p.asked_at < ?) "
                        "ORDER BY COALESCE(g.day, '') DESC, COALESCE(g.id, p.id) LIMIT 1", (cut,)).fetchone()
    return dict(r) if r else None


def mark_asked(db, pid: int):
    """和聊天里的 [待定 #编号 问了] 一样：记下问过的时间，三天内不再问。"""
    db.conn.execute("UPDATE pending SET asked_at = ? WHERE id = ?", (_now(), int(pid)))
    db.conn.commit()


def _thin_dims(db) -> list:
    counts = {d: 0 for d in lifebase.DIMENSIONS}
    cond, args = lifebase.mem_where("count", "facts")
    for r in db.conn.execute(f"SELECT dimension, COUNT(*) n FROM facts WHERE subject = 'self' AND {cond} GROUP BY dimension", args):
        if r["dimension"] in counts:
            counts[r["dimension"]] = r["n"]
    return [d for d, n in sorted(counts.items(), key=lambda x: x[1]) if n < GAP_THIN]


def _has_records(db, words: str) -> bool:
    """先查再问：她的聊天、文件、日记里有没有提过（不算已经整理成说法的）。只判断有没有，不读出内容。"""
    from . import memsearch
    try:
        hits = memsearch.search(db, [words], user="gaps", log=False)
    except Exception:
        return False
    return any(not str(h.get("key", "")).startswith("fact:") for h in hits)


def pick_gaps(db, today: date = None, n: int = GAP_DAILY) -> dict:
    """每天第一次跑时挑题；同一天重复跑不重复挑。返回 {问, 有记录没关联, 跳过原因}。"""
    ensure(db)
    db.conn.executescript(GAP_SCHEMA)
    day = (today or her_today()).isoformat()
    out = {"asked": [], "has_records": [], "skipped": ""}
    if gap_paused(db, today):
        out["skipped"] = "她说今天别问"
        return out
    if db.conn.execute("SELECT 1 FROM gap_log WHERE day = ?", (day,)).fetchone():
        out["skipped"] = "今天挑过了"
        return out
    open_n = db.conn.execute("SELECT COUNT(*) FROM pending WHERE status = 'open' AND origin = 'gap'").fetchone()[0]
    room = max(0, n - open_n)
    if not room:
        out["skipped"] = "上次的题她还没答完"
        db.conn.execute("INSERT INTO gap_log (day, key, question, outcome, at) VALUES (?, '-', '-', 'full', ?)", (day, _now()))
        db.conn.commit()
        return out
    since = (date.fromisoformat(day) - timedelta(days=7)).isoformat()
    recent = {r[0] for r in db.conn.execute("SELECT key FROM gap_log WHERE day >= ? AND outcome IN ('asked', 'has_records')", (since,))}
    cands = []
    try:
        for r in db.conn.execute("SELECT fn, COUNT(*) n FROM function_log WHERE status = 'unknown' AND at >= ? AND user != 'system' "
                                 "GROUP BY fn ORDER BY n DESC", (since,)):
            if r["fn"] in GAP_FN:
                q, words, dim = GAP_FN[r["fn"]]
                cands.append((f"fn:{r['fn']}", q, words, dim))
    except Exception:
        pass  # 还没有使用记录
    try:  # 和“我”页同一套厚薄（11-04：按条数挑一个都挑不出，“我”页却显示 3 个方面是薄的）
        order = {"empty": 0, "thin": 1, "stale": 2}
        weak = sorted([d for d in metrics(db, date.fromisoformat(day))["dims"] if d["state"] in order],
                      key=lambda d: (order[d["state"]], d["hers"]))
        thin = [d["dim"] for d in weak]
    except Exception:
        thin = _thin_dims(db)
    for d in thin:
        q, words = GAP_DIM[d]
        cands.append((f"dim:{d}", q, words, d))
    for key, q, words, dim in cands:
        if room <= 0:
            break
        if key in recent:
            continue
        if _has_records(db, words):  # 聊天里顺口提过不等于知道了（11-04 第一次跑：每个方面都“提过”，一题都没问）：
            out["has_records"].append(key)  # 只要这个方面整理出来的还很薄，照样问；记一笔，“我”页显示
        pid = add_question(db, q, commit=False)
        if not pid:
            continue
        db.conn.execute("UPDATE pending SET origin = 'gap', dim = ? WHERE id = ?", (dim, pid))
        db.conn.execute("INSERT INTO gap_log (day, key, question, outcome, pending_id, at) VALUES (?, ?, ?, 'asked', ?, ?)",
                        (day, key, q, pid, _now()))
        out["asked"].append(q)
        recent.add(key)
        room -= 1
        for fq in (GAP_FOLLOW.get(dim, []) if key.startswith("dim:") else []):  # 接着问的
            if room <= 0:
                break
            fid = add_question(db, fq, commit=False)
            if fid:
                db.conn.execute("UPDATE pending SET origin = 'gap', dim = ? WHERE id = ?", (dim, fid))
                db.conn.execute("INSERT INTO gap_log (day, key, question, outcome, pending_id, at) VALUES (?, ?, ?, 'asked', ?, ?)",
                                (day, key + ":more", fq, fid, _now()))
                out["asked"].append(fq)
                room -= 1
    if not out["asked"] and not out["has_records"]:
        db.conn.execute("INSERT INTO gap_log (day, key, question, outcome, at) VALUES (?, '-', '-', 'nothing', ?)", (day, _now()))
    db.conn.commit()
    return out


def gap_week(db, today: date = None) -> dict:
    """“我”页：这周 小伴 问了几个、她答了几个。"""
    ensure(db)
    db.conn.executescript(GAP_SCHEMA)
    day = today or her_today()
    monday = clock.week_start(day).isoformat()
    asked = db.conn.execute("SELECT COUNT(*) FROM pending WHERE origin = 'gap' AND asked_at >= ?", (monday,)).fetchone()[0]
    answered = db.conn.execute("SELECT COUNT(*) FROM pending WHERE origin = 'gap' AND status = 'done' AND resolved_at >= ?", (monday,)).fetchone()[0]
    found = 0  # 以前“旧记录里有就不问”，11-04 改成照样问，这个数不再有意义
    return {"asked": max(asked, answered), "answered": answered, "found_in_records": found, "paused_today": gap_paused(db, day)}


# ---------- “我”页后台指标（ontology-plan 2.1，me-page-v2.md 第 1、2 节；让和阶段 1 同步做） ----------
HER_KINDS = ("her_words", "her_confirmation", "her_provided")  # 厚薄只算她说的、她确认的、她给的资料
STALE_DAYS, THIN_N = 30, 3
REVIEW_DAYS = {"quarter": 90, "on_use": 180}
FN_DIM = {fn: v[2] for fn, v in GAP_FN.items()}  # 能力题答不上算哪个方面缺
SYSTEM_LAYERS = {"missed": ("沉淀（抽取）弱", "修抽取，加检验题"), "wrong": ("沉淀（抽取）弱", "修抽取，加检验题"),
                 "not_found": ("检索弱", "修检索、加同义说法"), "misused": ("使用弱", "修那个使用者的规则")}


def metrics(db, today: date = None) -> dict:
    """十个方面的厚 / 薄 / 旧 / 空，知识构成，这周纠错按层，升级建议（规则写死，不靠模型）。只读。"""
    ensure(db)
    day = today or her_today()
    now = datetime.combine(day, datetime.min.time(), timezone.utc)
    week = week_since(day)
    confirmed = {r[0] for r in db.conn.execute("SELECT fact_id FROM confirmations WHERE fact_id IS NOT NULL")}
    relay = {r[0] for r in db.conn.execute("SELECT id FROM evidence WHERE source IN ('grok', 'agent')")}
    review = {}
    try:
        review = {r[0]: r[1] for r in db.conn.execute("SELECT key, review_every FROM properties")}
    except Exception:
        pass
    cond, args = lifebase.mem_where("count", "facts", day)
    rows = db.conn.execute(f"SELECT * FROM facts WHERE subject = 'self' AND {cond}", args).fetchall()
    cols = rows[0].keys() if rows else []
    dims = {d: {"dim": d, "name": lifebase.DIM_NAMES[d], "n": 0, "hers": 0, "confirmed": 0, "last": None, "kinds": set(),
                "evidence": set(), "due": 0, "used": 0, "missed": 0, "props": {}} for d in lifebase.DIMENSIONS}
    pnames = {}
    try:
        pnames = {r[0]: r[1] for r in db.conn.execute("SELECT key, name FROM properties")}
    except Exception:
        pass
    compose, said = {}, {"hers": 0, "system": 0, "behavior": 0, "others": 0}
    for f in rows:
        d = dims.get(f["dimension"])
        if d is None:
            continue
        sk = f["source_kind"] if "source_kind" in cols else None
        d["n"] += 1
        d["kinds"].add(sk or "unknown")
        if sk in HER_KINDS:
            d["hers"] += 1
        if f["id"] in confirmed or ("locked" in cols and f["locked"]):
            d["confirmed"] += 1
        if not d["last"] or (f["recorded_at"] or "") > d["last"]:
            d["last"] = f["recorded_at"]
        for e in json.loads(f["evidence_ids"] or "[]"):
            if e not in relay:
                d["evidence"].add(e)
        prop = (f["prop"] if "prop" in cols else None) or f"dim:{f['dimension']}"
        pk = (f["prop"] if "prop" in cols else None) or "-"  # 星图往下钻：这个方面里按细属性分（没细分的归“-”）
        slot = d["props"].setdefault(pk, {"key": pk, "name": pnames.get(pk, "其他") if pk != "-" else "其他", "n": 0, "hers": 0})
        slot["n"] += 1
        slot["hers"] += 1 if sk in HER_KINDS else 0
        limit = REVIEW_DAYS.get(review.get(prop, "on_use"))
        seen = f["last_confirmed_at"] or f["recorded_at"] or ""
        if limit and seen and seen < (now - timedelta(days=limit)).isoformat():
            d["due"] += 1
        kind = f["kind"] if "kind" in cols else "fact"
        compose[kind or "fact"] = compose.get(kind or "fact", 0) + 1
        said[said_bucket(sk)] += 1
    logs = [[int(k[5:]) for k in json.loads(r["keys"] or "[]") if k.startswith("fact:") and k[5:].isdigit()]
            for r in db.conn.execute("SELECT keys FROM retrieval_log WHERE at >= ? AND COALESCE(user, '') != 'system'", (week,))]
    dim_of = {}  # 这周用到的记忆各属于哪个方面：一次查完，不再一条一条查（我页加载还是慢）
    wanted = sorted({i for ids in logs for i in ids})
    for i in range(0, len(wanted), 500):
        part = wanted[i:i + 500]
        dim_of.update(db.conn.execute(f"SELECT id, dimension FROM facts WHERE id IN ({','.join('?' * len(part))})", part).fetchall())
    for ids in logs:
        hit = {dim_of[i] for i in ids if dim_of.get(i) in dims}
        for dname in hit:  # 每次查询算一次，不按命中几条算
            dims[dname]["used"] += 1
    try:
        for r in db.conn.execute("SELECT fn, COUNT(*) n FROM function_log WHERE status = 'unknown' AND at >= ? AND user != 'system' GROUP BY fn", (week,)):
            if r["fn"] in FN_DIM:
                dims[FN_DIM[r["fn"]]]["missed"] += r["n"]
    except Exception:
        pass
    out_dims = []
    for d in dims.values():
        old = not d["last"] or d["last"] < (now - timedelta(days=STALE_DAYS)).isoformat()
        state = ("empty" if d["hers"] == 0 else "thin" if d["hers"] < THIN_N or len(d["kinds"]) <= 1
                 else "stale" if old or d["due"] >= 3 else "thick")
        out_dims.append({**d, "kinds": len(d["kinds"]), "evidence": len(d["evidence"]), "state": state,
                         "props": sorted(d["props"].values(), key=lambda x: -x["n"]),
                         "confirmed_rate": round(d["confirmed"] / d["n"], 2) if d["n"] else None})
    layers = week_layers(db, week)
    return {"dims": out_dims, "compose": compose, "said": said, "layers": layers, "routing": routing(db, week),
            "learned": week_learned(db, day), "suggestions": suggestions(db, out_dims, layers),
            "recent_upgrades": upgrades.recent(db, 3), "at": day.isoformat()}


def log_upgrade(db, at, what: str, kind: str, note: str = "", today: date = None) -> int:
    """ontology-plan 4.1：记一次升级（接来源 source / 拆属性 property / 修检索 retrieval / 规则 rule），前两周数字当场算。见 ethan/upgrades.py。"""
    return upgrades.log_upgrade(db, at, what, kind, note, today)


# ---------- “我”页和每周复盘共用的口径（ontology-plan 3.2：两边的数字由同一套函数算） ----------
SAID_NAMES = {"hers": "你说的", "others": "别人说的", "behavior": "行为记录", "system": "系统推的"}


def said_bucket(sk) -> str:
    """来源类型归成四类：她说的（含确认的、给的资料）/ 别人说的 / 行为记录 / 系统推的（其余，含转述）。"""
    return "hers" if sk in HER_KINDS else "behavior" if sk == "behavior" else "others" if sk == "other_person" else "system"


def week_since(day: date) -> str:
    """“这周”的起点：她今天 0 点（UTC）往前 7 天。“我”页 metrics 和每周复盘都用这个。"""
    return (datetime.combine(day, datetime.min.time(), timezone.utc) - timedelta(days=7)).isoformat()


def week_layers(db, since: str, until: str = None) -> dict:
    """纠错单按层计数（撤销的不算）。"""
    out = {}
    try:
        for r in db.conn.execute("SELECT layer, COUNT(*) n FROM corrections WHERE at >= ? AND at < ? AND undone_at IS NULL GROUP BY layer",
                                 (since, until or "9999")):
            out[r["layer"]] = r["n"]
    except Exception:
        pass
    return out


ANSWERED, MISSED = ("answer", "disputed"), ("unknown", "unavailable")  # 没权限（forbidden）不算答不上：是该挡的


def question_states(db, since: str, until: str) -> dict:
    """这段时间每道能力题的状态（function_log，不算系统维护自己的）：有一次答上 = 答得上；被问过但都没答上 = 答不上。"""
    from . import onto_query
    seen = {}
    try:
        rows = db.conn.execute("SELECT fn, status FROM function_log WHERE at >= ? AND at < ? AND user != 'system'", (since, until)).fetchall()
    except Exception:
        return {}
    for r in rows:
        q = onto_query.QUESTION_OF.get(r["fn"])
        if not q or r["status"] not in ANSWERED + MISSED:
            continue
        seen[q] = "answered" if r["status"] in ANSWERED or seen.get(q) == "answered" else "missed"
    return seen


def week_learned(db, day: date = None, top: int = 5) -> dict:
    """这周系统新认识了你什么（ontology-plan 3.2，周日复盘里一段，“我”页 metrics 里也带着，同一套数）：
    - new：这周新记下、现在还成立的说法，按方面、按来源（她说的 / 别人说的 / 行为 / 系统推的）各几条，挑最重要的几条原文；
    - corrected：这周被纠正的，按层（记错 / 变了）各几条，举例；
    - questions：哪几道能力题从上周“答不上”变成这周“答得上”（function_log 对比）；上周没记录就是“还没有对比数据”。"""
    day = day or her_today()
    since = week_since(day)
    last = (datetime.fromisoformat(since) - timedelta(days=7)).isoformat()
    cond, args = lifebase.mem_where("count", "facts", day)
    rows = db.conn.execute(f"SELECT * FROM facts WHERE subject = 'self' AND {cond} AND recorded_at >= ?", (*args, since)).fetchall()
    cols = rows[0].keys() if rows else []
    by_dim, by_source = {}, {k: 0 for k in SAID_NAMES}
    picks = []
    for f in rows:
        if f["dimension"] not in lifebase.DIM_NAMES:
            continue
        b = said_bucket(f["source_kind"] if "source_kind" in cols else None)
        by_source[b] += 1
        name = lifebase.DIM_NAMES[f["dimension"]]
        by_dim[name] = by_dim.get(name, 0) + 1
        picks.append(((b == "hers", f["importance"], f["recorded_at"]), {"text": f["statement"], "dim": name, "from": SAID_NAMES[b]}))
    picks.sort(key=lambda x: x[0], reverse=True)  # 她说的在前，再按重要程度、新旧
    layers = week_layers(db, since)
    examples = []
    try:
        for t in db.conn.execute("SELECT kind, wrong, right, layer FROM corrections WHERE at >= ? AND undone_at IS NULL ORDER BY id DESC LIMIT 3",
                                 (since,)):
            examples.append({"kind": "变了" if t["kind"] == "changed" else "记错", "wrong": t["wrong"] or "", "right": t["right"],
                             "layer": memusers.LAYERS.get(t["layer"], t["layer"])})
    except Exception:
        pass
    changed = layers.get("changed", 0)
    before, now = question_states(db, last, since), question_states(db, since, "9999")
    flips = sorted(q for q, st in now.items() if st == "answered" and before.get(q) == "missed")
    worse = sorted(q for q, st in now.items() if st == "missed" and before.get(q) == "answered")
    return {"since": since[:10], "to": day.isoformat(),
            "new": {"n": len(rows), "by_dim": dict(sorted(by_dim.items(), key=lambda x: -x[1])), "by_source": by_source,
                    "top": [p for _, p in picks[:top]]},
            "corrected": {"n": sum(layers.values()), "wrong": sum(layers.values()) - changed, "changed": changed,
                          "layers": {memusers.LAYERS.get(k, k): v for k, v in layers.items()}, "examples": examples},
            "questions": {"compared": bool(before), "now_answered": flips, "now_missed": worse,
                          "note": "" if before else "还没有对比数据（上周没有能力题的使用记录）"}}


def learned_text(w: dict) -> str:
    """给每周复盘的开场：这一段的文字底稿（数字原样用，小伴 换成自己的口吻）。"""
    n, c, q = w["new"], w["corrected"], w["questions"]
    src = "、".join(f"{SAID_NAMES[k]} {v} 条" for k, v in n["by_source"].items() if v) or "没有"
    dims = "、".join(f"{k} {v}" for k, v in list(n["by_dim"].items())[:5])
    out = [f"新记下的说法 {n['n']} 条（{src}）" + (f"；方面：{dims}" if dims else "")]
    out += [f"- {p['text']}（{p['dim']}，{p['from']}）" for p in n["top"]]
    out.append(f"这周被纠正 {c['n']} 次：记错 {c['wrong']}、变了 {c['changed']}" if c["n"] else "这周没有被纠正")
    out += [f"- {e['kind']}：{e['wrong'] or '（没说错的）'} → {e['right']}" for e in c["examples"]]
    if not q["compared"]:
        out.append(q["note"])
    else:
        name = lambda x: f"第 {x} 题"
        out.append("从答不上变成答得上：" + ("、".join(map(name, q["now_answered"])) if q["now_answered"] else "没有"))
        if q["now_missed"]:
            out.append("反过来上周答得上、这周答不上：" + "、".join(map(name, q["now_missed"])))
    return "\n".join(out)


def routing(db, since: str) -> dict:
    """这周按题答的 vs 兜底检索的（ontology-plan 3.1）：按题 = 能力题函数的调用（function_log），
    兜底 = 不经过题函数的自由检索（retrieval_log 里 via 为空的）。都不算系统维护自己的。"""
    by_q, fallback = {}, 0
    try:
        from . import onto_query
        for r in db.conn.execute("SELECT fn, COUNT(*) n FROM function_log WHERE at >= ? AND user != 'system' GROUP BY fn", (since,)):
            q = onto_query.QUESTION_OF.get(r["fn"])
            if q:
                by_q[q] = by_q.get(q, 0) + r["n"]
    except Exception:
        pass  # 还没有使用记录
    try:
        fallback = db.conn.execute("SELECT COUNT(*) FROM retrieval_log WHERE at >= ? AND COALESCE(user, '') != 'system' AND via IS NULL",
                                   (since,)).fetchone()[0]
    except Exception:
        fallback = 0
    answered = sum(by_q.values())
    total = answered + fallback
    return {"by_question": answered, "fallback": fallback, "share": round(answered / total, 2) if total else None, "by_q": by_q}


def suggestions(db, dims: list, layers: dict) -> list:
    """me-page-v2.md 第 1 节的规则表：看到什么信号 → 哪一环弱 → 谁做什么。最多 3 件，信号强的在前。"""
    out = []
    for d in dims:  # 被问得多、条数少 / 没确认：输入不够
        if (d["used"] + d["missed"]) >= 2 and d["state"] in ("empty", "thin", "stale"):
            out.append((d["used"] + d["missed"] * 2, {"who": "你", "weak": "输入不够",
                        "what": f"和 小伴 聊聊你的{d['name']}（这周被问到 {d['used'] + d['missed']} 次，你说过的只有 {d['hers']} 条）",
                        "watch": f"{d['name']}的条数和没查到次数"}))
    if layers.get("untold", 0) >= 2:
        out.append((layers["untold"] * 2, {"who": "你", "weak": "输入不够", "what": f"这周 {layers['untold']} 次纠正是“没告诉过”：挑一个方面多说一点",
                                           "watch": "“没告诉过”的次数"}))
    for layer, (weak, fix) in SYSTEM_LAYERS.items():
        if layers.get(layer, 0) >= 2:
            out.append((layers[layer] * 2, {"who": "系统", "weak": weak, "what": f"这周 {layers[layer]} 次纠正是“{memusers.LAYERS[layer]}”：{fix}",
                                            "watch": f"“{memusers.LAYERS[layer]}”的次数"}))
    due = sum(d["due"] for d in dims)
    if layers.get("outdated", 0) >= 1 or due >= 5:
        out.append((due + layers.get("outdated", 0) * 2, {"who": "你", "weak": "更新弱", "what": f"{due} 条好久没确认了，在“待你定”里过一遍",
                                                          "watch": "到期待确认的条数"}))
    try:
        for s in lifebase.stale_sources(db, cached=True):
            out.append((3, {"who": "你", "weak": "输入渠道", "what": s, "watch": "这个来源的最近一条"}))
    except Exception:
        pass
    if len(out) < 3:  # 信号不够时，空着的方面也提一句（分数最低，排在最后）
        for d in [d for d in dims if d["state"] == "empty"][:3 - len(out)]:
            out.append((0, {"who": "你", "weak": "输入不够", "what": f"你的{d['name']}还空着，有空跟 小伴 聊聊", "watch": f"{d['name']}的条数"}))
    return [x for _, x in sorted(out, key=lambda t: -t[0])][:3]


def main():
    import argparse
    from pathlib import Path
    p = argparse.ArgumentParser(description="“我”页后台指标（只读）")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--metrics", action="store_true")
    a = p.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        print(json.dumps(metrics(store.db), ensure_ascii=False, indent=1, default=str))
    finally:
        store.close()


if __name__ == "__main__":
    main()


# ---------- 删除管理和对外表达（ontology-plan 2.2 第二层） ----------
def manage(db) -> dict:
    """“我”页第二层：删过什么（只给日期、方面、条数，内容已经清掉）、先停用的条目、她批准对外说的话。"""
    ensure(db)
    dels = []
    for r in db.conn.execute("SELECT * FROM deletion_requests ORDER BY id DESC LIMIT 60").fetchall():
        scope, rep = json.loads(r["scope"] or "{}"), json.loads(r["report"] or "{}")
        fids = (rep.get("facts_deleted") or []) + (scope.get("facts") or [])
        dims = sorted({x[0] for x in db.conn.execute(f"SELECT dimension FROM facts WHERE id IN ({','.join('?' * len(fids)) or 'NULL'})", fids)})
        evs = scope.get("evidence") or []
        allowed = 0
        for e in db.conn.execute(f"SELECT source, source_ref FROM evidence WHERE id IN ({','.join('?' * len(evs)) or 'NULL'})", evs).fetchall():
            fp = lifebase.fingerprint(e["source"], e["source_ref"])
            row = db.conn.execute("SELECT allow_reimport FROM forgotten WHERE fp = ?", (fp,)).fetchone()
            allowed += 1 if row and row[0] else 0
        dels.append({"id": r["id"], "at": r["at"][:16].replace("T", " "), "dims": [lifebase.DIM_NAMES.get(d, d) for d in dims],
                     "facts": len(set(rep.get("facts_deleted") or [])), "sources": len(evs), "suspended": len(rep.get("facts_suspended") or []),
                     "reimport": bool(evs) and allowed == len(evs)})
    paused = [{"id": f["id"], "text": f["statement"], "dim": lifebase.DIM_NAMES.get(f["dimension"], f["dimension"])}
              for f in db.conn.execute("SELECT id, statement, dimension FROM facts WHERE status = 'suspended' ORDER BY id DESC LIMIT 60")]
    now = _now()
    discl = []
    for d in lifebase.disclosures(db):
        users = json.loads(d.get("to_users") or "[]")
        state = ("撤回了" if d.get("revoked_at") else "说过了（这次的）" if d.get("once") and d.get("used_at")
                 else "到期了" if d.get("until") and d["until"] < now[:10] else "有效")
        discl.append({"id": d["id"], "text": d["text"], "quote": d["quote"], "to": "、".join({"twin": "Days", "site": "主页"}.get(u, u) for u in users),
                      "once": bool(d.get("once")), "to_whom": d.get("to_whom") or "", "until": d.get("until") or "",
                      "renew_at": (d.get("renew_at") or "")[:10], "state": state})
    for d in lifebase.disclosures(db, mode="deny"):  # 逐条挡住的
        discl.append({"id": d["id"], "text": d["text"], "quote": d["quote"], "to": "、".join(memusers.USERS.get(u, {}).get("name", u)
                                                                                         for u in json.loads(d.get("to_users") or "[]")),
                      "once": False, "to_whom": "", "until": "", "renew_at": "", "deny": True,
                      "state": "撤回了" if d.get("revoked_at") else "有效"})
    return {"deletions": dels, "suspended": paused, "disclosures": discl, "policy": lifebase.policy(db),
            "policy_note": "自己人（小伴、计划器、准备员这些）照内部规则看；下面两档管往外说。新接进来的只要登记是哪一档，就按那一档的开关。"}


def set_policy(db, tier: str, category: str, mode: str) -> dict:
    """“我”页对外开关：她点了一格。"""
    try:
        lifebase.set_policy(db, tier, category, mode)
    except ValueError as e:
        return {"ok": False, "msg": str(e)}
    return {"ok": True, "msg": f"{lifebase.TIER_NAMES[tier]}档的“{lifebase.CATEGORY_NAMES[category]}”改成了“{lifebase.MODE_NAMES[mode]}”"}


def allow_reimport_request(db, req_id: int, root=None) -> dict:
    """她选“允许以后重新记”：这次删掉的来源放开，她再发一次 小伴 就会重新记（删掉的内容本身放不回来）。"""
    ensure(db)
    r = db.conn.execute("SELECT scope FROM deletion_requests WHERE id = ?", (int(req_id),)).fetchone()
    if r is None:
        return {"ok": False, "msg": "找不到这次删除"}
    evs = json.loads(r["scope"] or "{}").get("evidence") or []
    n = 0
    for e in db.conn.execute(f"SELECT source, source_ref FROM evidence WHERE id IN ({','.join('?' * len(evs)) or 'NULL'})", evs).fetchall():
        n += 1 if lifebase.allow_reimport(db, e["source"], e["source_ref"], root) else 0
    return {"ok": True, "msg": f"放开了 {n} 个来源：你再发一次，小伴 会重新记" if n else "这次删的是单条记忆，没有要放开的来源"}


def keep_suspended(db, fid: int) -> dict:
    """先停用的条目，她看过说“留着”：恢复成有效（剩下的依据）。"""
    ensure(db)
    n = db.conn.execute("UPDATE facts SET status = 'active' WHERE id = ? AND status = 'suspended'", (int(fid),)).rowcount
    if n:
        _confirm(db, int(fid), "me_manage", "停用后她说留着")
    db.conn.commit()
    return {"ok": bool(n), "msg": "留着了" if n else "这条已经不是停用的了"}



def skip_question(db, pid: int) -> dict:
    """小伴 补缺口的问题，她不想答：收起，一周内不再问同一个方面（gap_log 里已经记着挑过）。"""
    ensure(db)
    n = db.conn.execute("UPDATE pending SET status = 'skipped', resolved_at = ? WHERE id = ? AND status = 'open'", (_now(), int(pid))).rowcount
    db.conn.commit()
    return {"ok": bool(n), "msg": "好，不问这个了"}
