"""skill 第一版：从 ontology 长出来的“她的那部分”（ontology.md 1.4，她 11-05：“提前做”；docs/design/style-and-skills.md）。

一个 skill 三块：
- 通用方法：这类事一般怎么做（写在下面 SKILLS 里，来自专业知识，不从她的记忆来）；
- 她的做法：偏好（第 4 题）、规矩（第 5 题）、要求和禁忌（第 12 题）、她确认过的写作规范——按题函数从记忆取，
  以 skill 自己的使用者身份（mail 等）读，权限照登记表；
- 能做到哪一步：授权（第 13 题）；没有授权 = 只能起草，等她确认。

草稿放 <数据目录>/skills/candidates/<名字>.md（不进仓库），“待你定”里一条“新 skill 草稿：…”，她批准才移到 skills/active/，
用的时候（比如邮件起草）读 active 的通用方法，“她的做法”和授权**当场重新按题查**（文件里那份只是给她审的快照，她的偏好变了跟着变）。
同一类事她的纠正攒够（4 周里同类纠错 + 改过 / 要改的 >= PROPOSE_AT 次），只“提议”开新 skill 草稿（进待你定），不自动生效。
"""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import onto_query, tx

SKILLS = {
    "mail_reply": {
        "name": "替你回邮件", "user": "mail", "topic": "邮件", "task": "回邮件", "audience": None,
        "grant": ("send", "邮件"),
        "method": [
            "先看清对方是谁、和她什么关系、这封要她做什么（确认、回答、约时间、推掉、只是知会）。",
            "用来信的语言回；先回应对方的问题或请求，再补必要的信息，最后写清下一步。",
            "只写她能确定的事：时间、价格、承诺不编，用 [待你填：…] 标出来。",
            "短：能三句说清的不写五句；不写客套长段、不写主题行。",
            "结尾署名 Mia；草稿只给她看，她点发送才发。",
        ],
    },
}
# 她的纠正攒够了可以提议的事（runlog 的种类、纠错单的使用者）→ 提议开的 skill；没写通用方法的只记“她想要”，等开发会话补
IDEAS = {"mail": ("mail_reply", "替你回邮件"), "prep": ("prep", "替你准备事"), "diary": ("diary", "写你的日记"), "plan": ("plan", "排你的计划")}
PROPOSE_AT = 3  # 同类纠正几次提议一次
PROPOSE_WEEKS = 4
SCHEMA = """CREATE TABLE IF NOT EXISTS skill_state (name TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL)"""
# status：proposed 提议了 / wanted 她要、等写通用方法 / declined 她不要 / candidate 草稿等她看 / active 批准了在用 / rejected 草稿不要


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    if tx.once(db, "skills"):
        db.conn.execute(SCHEMA)


def data_root(db) -> Path:
    """数据目录 = 数据库文件所在的目录。"""
    for r in db.conn.execute("PRAGMA database_list"):
        if r[1] == "main" and r[2]:
            return Path(r[2]).parent
    raise ValueError("内存里的数据库没有数据目录，传 root")


def path(root, name: str, where: str = "candidates") -> Path:
    return Path(root) / "skills" / where / f"{name}.md"


def state(db, name: str):
    ensure(db)
    r = db.conn.execute("SELECT * FROM skill_state WHERE name = ?", (name,)).fetchone()
    return dict(r) if r else None


def _set(db, name: str, status: str, note: str = None):
    ensure(db)
    db.conn.execute("INSERT OR REPLACE INTO skill_state (name, status, note, updated_at) VALUES (?, ?, ?, ?)", (name, status, note, _now()))
    tx.commit(db)


# ---------- 她的做法：按题从记忆取 ----------
def her_way(db, name: str) -> dict:
    """skill 用之前要查的（只读，按 skill 自己的使用者身份）：偏好、规矩、要求和禁忌、写作规范、授权。"""
    sk = SKILLS[name]
    user, topic = sk["user"], sk["topic"]
    from . import style
    got = {"preferences": onto_query.lines(onto_query.preferences(db, user, topic)),
           "rules": onto_query.lines(onto_query.standing_rules(db, user, topic)),
           "requirements": onto_query.lines(onto_query.prep_requirements(db, user, sk["task"])),
           "style": style.style_block(db, user, sk.get("audience"))}
    action, scope = sk["grant"]
    auth = onto_query.authorization(db, user, action, scope)
    if auth["status"] in ("answer", "disputed") and auth["items"] and "grant" in auth["items"][0]:
        i = auth["items"][0]
        got["authorization"] = f"她授权过：{action} · {i['scope']}" + ("，每次都要她确认" if i["each_confirm"] else "，不用每次确认") + \
                               (f"，到 {i['until']}" if i.get("until") else "") + (f"（原话：{i['quote']}）" if i.get("quote") else "")
    else:
        got["authorization"] = "没有授权记录：只能起草，等她看过、点发送"
    return got


def render(db, name: str, way: dict = None) -> str:
    sk = SKILLS[name]
    way = way or her_way(db, name)
    sec = lambda title, body: f"### {title}\n{body.strip() if body and body.strip() else '（还没有她说过的）'}\n"
    return "\n".join([
        f"# skill：{sk['name']}（{name}）", "",
        f"草稿 · {_now()[:10]} 生成 · 批准前不生效。用的时候“她的做法”和“授权”会按题重新查最新的，这里是给你审的快照。", "",
        "## 1. 通用方法", "\n".join(f"- {x}" for x in sk["method"]), "",
        "## 2. 她的做法（偏好和口径）",
        sec("偏好（第 4 题）", way["preferences"]), sec("规矩（第 5 题）", way["rules"]),
        sec("要求和禁忌（第 12 题）", way["requirements"]), sec("写作规范（她确认过的）", way["style"]),
        "## 3. 能做到哪一步（授权，第 13 题）", way["authorization"], "",
        "## 用之前要查", f"- onto_query.preferences(user={sk['user']}, topic={sk['topic']})",
        f"- onto_query.standing_rules(user={sk['user']}, topic={sk['topic']})", f"- onto_query.prep_requirements(user={sk['user']}, {sk['task']})",
        f"- onto_query.writing_style(user={sk['user']})", f"- onto_query.authorization(user={sk['user']}, {sk['grant'][0]}, {sk['grant'][1]})", ""])


def draft_skill(db, name: str, root=None) -> Path:
    """生成 skill 草稿：只写进候选区（<数据目录>/skills/candidates/），待你定里出一条，她批准才移到 active。已在用的不动。"""
    if name not in SKILLS:
        raise ValueError(f"还没有“{name}”的通用方法：先在 ethan/skills.py 的 SKILLS 里写")
    root = Path(root) if root else data_root(db)
    p = path(root, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(db, name), encoding="utf-8")
    st = state(db, name)
    _set(db, name, "candidate", (st or {}).get("note"))
    _pending(db, "draft", name, f"新 skill 草稿：{SKILLS[name]['name']}", body=p.read_text(encoding="utf-8"), root=str(root))
    return p


def _pending(db, kind: str, name: str, question: str, body: str = "", root: str = "", why: str = "") -> int:
    from . import mepage
    mepage.ensure(db)
    for r in db.conn.execute("SELECT id, options FROM pending WHERE kind = 'skill' AND status = 'open'").fetchall():
        o = (json.loads(r["options"]) or [{}])[0]
        if o.get("skill") == name and o.get("step") == kind:
            db.conn.execute("UPDATE pending SET options = ? WHERE id = ?",
                            (json.dumps([{**o, "body": body[:8000], "text": question}], ensure_ascii=False), r["id"]))
            tx.commit(db)
            return r["id"]
    opt = {"skill": name, "step": kind, "text": question, "body": body[:8000], "root": root, "why": why,
           "src": why or ("批准后邮件起草就按它来；“她的做法”每次用都重新查最新的" if kind == "draft" else "")}
    pid = db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES ('skill', 'long', ?, ?, '[]', ?, 0, ?)",
                          (question, json.dumps([opt], ensure_ascii=False), json.dumps([SKILLS.get(name, {}).get("user", "system")]), _now())).lastrowid
    tx.commit(db)
    return pid


def approve(db, name: str, root=None, note: str = "") -> dict:
    """批准：候选移到 active（原来在用的移进 history 留底）；她写的补充要求原样附在后面。"""
    root = Path(root) if root else data_root(db)
    cand = path(root, name)
    if not cand.exists():
        return {"ok": False, "msg": "草稿不见了，再生成一次"}
    act = path(root, name, "active")
    act.parent.mkdir(parents=True, exist_ok=True)
    if act.exists():
        hist = path(root, f"{name}-{_now()[:19].replace(':', '')}", "history")
        hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(act), str(hist))
    text = cand.read_text(encoding="utf-8").replace("草稿 · ", "已批准 · ", 1)
    if note.strip():
        text += f"\n## 她补充的要求（原话，{_now()[:10]}）\n{note.strip()}\n"
    act.write_text(text, encoding="utf-8")
    cand.unlink()
    _set(db, name, "active", note.strip() or None)
    return {"ok": True, "msg": f"批准了：{SKILLS[name]['name']}，以后就按它来"}


def reject_draft(db, name: str, root=None) -> dict:
    root = Path(root) if root else data_root(db)
    cand = path(root, name)
    if cand.exists():
        dead = path(root, f"{name}-{_now()[:19].replace(':', '')}", "rejected")
        dead.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(cand), str(dead))
    _set(db, name, "rejected")
    return {"ok": True, "msg": "好，这份草稿不用"}


def resolve(db, r, action: str, text: str = "") -> dict:
    """待你定里那一条：草稿 yes 批准 / own 加上她的要求再批准 / no 不要；提议 yes 要（有通用方法的马上起草）/ no 不要。"""
    opt = (json.loads(r["options"]) or [{}])[0]
    name, root = opt.get("skill"), opt.get("root") or None
    if opt.get("step") == "draft":
        if action == "yes":
            res = approve(db, name, root)
        elif action == "own":
            res = approve(db, name, root, text) if (text or "").strip() else {"ok": False, "msg": "写一句要求"}
        elif action == "no":
            res = reject_draft(db, name, root)
        else:
            res = {"ok": False, "msg": "不认识的操作"}
    else:
        if action in ("yes", "own"):
            if name in SKILLS:
                db.conn.execute("UPDATE pending SET status = 'done', resolved_at = ?, resolution = ? WHERE id = ?",
                                (_now(), json.dumps({"action": action}), r["id"]))
                tx.commit(db)
                draft_skill(db, name, root)
                return {"ok": True, "msg": f"好，草稿写好了：{SKILLS[name]['name']}，在待你定里看"}
            _set(db, name, "wanted", (text or "").strip() or None)
            res = {"ok": True, "msg": "记下了：这个 skill 的通用方法要开发会话来写，写好再给你看草稿"}
        elif action == "no":
            _set(db, name, "declined")
            res = {"ok": True, "msg": "好，不开"}
        else:
            res = {"ok": False, "msg": "不认识的操作"}
    if res.get("ok"):
        db.conn.execute("UPDATE pending SET status = 'done', resolved_at = ?, resolution = ? WHERE id = ?",
                        (_now(), json.dumps({"action": action, "text": text}, ensure_ascii=False), r["id"]))
        tx.commit(db)
    return res


# ---------- 纠正攒够了：提议开 skill ----------
def corrections_by_kind(db, weeks: int = PROPOSE_WEEKS) -> dict:
    """最近几周同一类事上她的纠正：纠错单（按使用者）+ 运行记录里她自己改了 / 要改的（按种类）。"""
    since = (datetime.now(timezone.utc) - timedelta(days=7 * weeks)).isoformat(timespec="seconds")
    out = {}
    try:
        for r in db.conn.execute("SELECT user, COUNT(*) n FROM corrections WHERE at >= ? AND kind = 'wrong' AND undone_at IS NULL GROUP BY user", (since,)):
            out[r["user"]] = out.get(r["user"], 0) + r["n"]
    except Exception:
        pass
    try:
        for r in db.conn.execute("SELECT kind, COUNT(*) n FROM run_log WHERE reaction IN ('edited', 'revised') "
                                 "AND COALESCE(reaction_at, at) >= ? GROUP BY kind", (since[:10],)):
            out[r["kind"]] = out.get(r["kind"], 0) + r["n"]
    except Exception:
        pass
    return out


def propose(db) -> list:
    """同类纠正攒够 PROPOSE_AT 次、还没有这个 skill（在用 / 草稿 / 提议过 / 她不要的都不再提）：待你定里提议一条。只提议，不生效。"""
    ensure(db)
    out = []
    for kind, n in corrections_by_kind(db).items():
        if kind not in IDEAS or n < PROPOSE_AT:
            continue
        name, title = IDEAS[kind]
        st = state(db, name)
        if st and st["status"] in ("active", "candidate", "proposed", "wanted", "declined"):
            continue
        _set(db, name, "proposed", f"最近 {PROPOSE_WEEKS} 周{title}上你纠正 / 改了 {n} 次")
        _pending(db, "idea", name, f"要不要开一个 skill：{title}", why=f"最近 {PROPOSE_WEEKS} 周这类事你纠正 / 改了 {n} 次；开了以后按你的做法来，先给你看草稿")
        out.append(name)
    return out


# ---------- 用 ----------
def active_text(db, name: str, root=None) -> str:
    root = Path(root) if root else data_root(db)
    p = path(root, name, "active")
    return p.read_text(encoding="utf-8") if p.exists() else ""


def prompt_for(db, name: str, root=None) -> str:
    """邮件起草等用：批准了的 skill → 通用方法 + 当场重新查的“她的做法”和授权 + 她批准时补充的原话。没批准就空。"""
    try:
        if not active_text(db, name, root):
            return ""
    except ValueError:
        return ""
    sk, way = SKILLS[name], her_way(db, name)
    st = state(db, name) or {}
    parts = [f"按她批准的 skill「{sk['name']}」来做：", "通用方法：\n" + "\n".join(f"- {x}" for x in sk["method"])]
    for title, key in (("她的偏好", "preferences"), ("她定的规矩", "rules"), ("她说过的要求和禁忌", "requirements")):
        if way[key]:
            parts.append(f"{title}：\n{way[key]}")
    if way["style"]:
        parts.append(way["style"])
    if st.get("note"):
        parts.append(f"她批准时补充的：{st['note']}")
    parts.append(f"能做到哪一步：{way['authorization']}")
    return "\n\n".join(parts)
