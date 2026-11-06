"""skill 第一版：从 ontology 长出来的“她的那部分”（ontology.md 1.4，docs/design/style-and-skills.md）。

一个 skill 三块：
- 通用方法：这类事一般怎么做（写在下面 SKILLS 里，来自专业知识，不从她的记忆来）；
- 她的做法：偏好（第 4 题）、规矩（第 5 题）、要求和禁忌（第 12 题）、她确认过的写作规范——按题函数从记忆取，
  以 skill 自己的使用者身份（mail 等）读，权限照登记表；
- 能做到哪一步：授权（第 13 题）；没有授权 = 只能起草，等她确认。

草稿放 <数据目录>/skills/candidates/<名字>.md（不进仓库），“待你定”里一条“新 skill 草稿：…”，她批准才移到 skills/active/，
用的时候（比如邮件起草）读 active 的通用方法，“她的做法”和授权**当场重新按题查**（文件里那份只是给她审的快照，她的偏好变了跟着变）。
纠正攒够了：
- 落在已有 skill 管的事上（按 SKILLS 里登记的 scope 匹配：runlog 的种类、纠错单的 entry）→ 在这个 skill 上迭代：
  4 周里攒够 PROPOSE_AT 次没用过的纠正，就起草新版本（只改“她的做法”那块，每处改动附上是哪次纠正），
  待你定里一条“替你回邮件 v2：改了 N 处”，能看改前改后；她批准才替换，旧版进 skills/history/，能一键退回上一版。
- 落在没有 skill 管的一类事上 → 攒够 PROPOSE_AT 次才“提议”开新 skill（进待你定），不自动生效。
"""

import json
import re
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
        # 它管的事类：运行记录里 kind=mail 的（邮件草稿：她改过再发 / 要改）、纠错单里 entry=mail 的（起草邮件时纠正的）
        "scope": {"label": "回邮件 / 起草邮件", "runlog": ("mail",), "entries": ("mail",)},
    },
}
# 她的纠正攒够了可以提议的事（runlog 的种类、纠错单的使用者）→ 提议开的 skill；没写通用方法的只记“她想要”，等开发会话补
IDEAS = {"mail": ("mail_reply", "替你回邮件"), "prep": ("prep", "替你准备事"), "diary": ("diary", "写你的日记"), "plan": ("plan", "排你的计划")}
PROPOSE_AT = 3  # 同类纠正几次提议一次
PROPOSE_WEEKS = 4
SCHEMA = """CREATE TABLE IF NOT EXISTS skill_state (name TEXT PRIMARY KEY, status TEXT NOT NULL, note TEXT, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS skill_fixes (ref TEXT PRIMARY KEY, skill TEXT NOT NULL, version INTEGER NOT NULL, at TEXT NOT NULL)"""
# skill_fixes：哪次纠正已经用进了哪个版本（用过的不再算，不重复起草）
SECTION = "## 2. 她的做法（偏好和口径）"
FIXES = "### 她纠正过的（按这些改）"
# status：proposed 提议了 / wanted 她要、等写通用方法 / declined 她不要 / candidate 草稿等她看 / active 批准了在用 / rejected 草稿不要


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    if tx.once(db, "skills"):
        db.conn.executescript(SCHEMA)
        if "version" not in {c[1] for c in db.conn.execute("PRAGMA table_info(skill_state)")}:  # 在用的是第几版
            db.conn.execute("ALTER TABLE skill_state ADD COLUMN version INTEGER")


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


def _set(db, name: str, status: str, note: str = None, version: int = None):
    ensure(db)
    if version is None:
        version = (state(db, name) or {}).get("version")
    db.conn.execute("INSERT OR REPLACE INTO skill_state (name, status, note, updated_at, version) VALUES (?, ?, ?, ?, ?)",
                    (name, status, note, _now(), version))
    tx.commit(db)


# ---------- 版本：文件第一行 “# skill：替你回邮件（mail_reply）v2” ----------
VER = re.compile(r"^# skill：[^\n]*?v(\d+)\s*$", re.M)
STATUS_LINE = re.compile(r"^(草稿|修订草稿|已批准) · .*$", re.M)


def version_of(text: str) -> int:
    m = VER.search(text or "")
    return int(m.group(1)) if m else 1


def _files(root, name: str, where: str) -> list:
    d = Path(root) / "skills" / where
    return sorted(f for f in d.glob(f"{name}*.md") if f.stem == name or f.stem.startswith(name + "-")) if d.exists() else []


def next_version(root, name: str) -> int:
    """在用的和留底的里最大的版本 + 1（退回过、不要过的版本号不再复用也没关系，只要比在用的大）。"""
    vs = [version_of(f.read_text(encoding="utf-8")) for w in ("active", "history") for f in _files(root, name, w)]
    return max(vs) + 1 if vs else 1


def _park(src: Path, root, name: str, where: str, ver: int) -> Path:
    """移进 history / rejected 留底：<名字>-v<版本>-<时间>.md（重名就加后缀）。"""
    stamp = _now()[:19].replace(":", "")
    dst = path(root, f"{name}-v{ver}-{stamp}", where)
    dst.parent.mkdir(parents=True, exist_ok=True)
    n = 2
    while dst.exists():
        dst, n = path(root, f"{name}-v{ver}-{stamp}-{n}", where), n + 1
    shutil.move(str(src), str(dst))
    return dst


# ---------- 她的做法：按题从记忆取 ----------
def her_way(db, name: str) -> dict:
    """skill 用之前要查的（只读，按 skill 自己的使用者身份）：偏好、规矩、要求和禁忌、写作规范、授权。"""
    sk = SKILLS[name]
    user, topic = sk["user"], sk["topic"]
    from . import style
    fresh = lambda g: onto_query.recent_only(db, g)  # 只用最近两年的（驳回第一版：用了十年前邮件里的内容）
    got = {"preferences": onto_query.lines(fresh(onto_query.preferences(db, user, topic))),
           "rules": onto_query.lines(fresh(onto_query.standing_rules(db, user, topic))),
           "requirements": onto_query.lines(fresh(onto_query.prep_requirements(db, user, sk["task"]))),
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


def render(db, name: str, way: dict = None, version: int = 1) -> str:
    sk = SKILLS[name]
    way = way or her_way(db, name)
    sec = lambda title, body: f"### {title}\n{body.strip() if body and body.strip() else '（还没有她说过的）'}\n"
    return "\n".join([
        f"# skill：{sk['name']}（{name}）v{version}", "",
        f"草稿 · {_now()[:10]} 生成 · 批准前不生效。用的时候“她的做法”和“授权”会按题重新查最新的，这里是给你审的快照。", "",
        "## 1. 通用方法", "\n".join(f"- {x}" for x in sk["method"]), "",
        SECTION,
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
    p.write_text(render(db, name, version=next_version(root, name)), encoding="utf-8")
    st = state(db, name)
    if not st or st["status"] != "active":  # 已经在用的，重新起草不改它的状态（批准了才换）
        _set(db, name, "candidate", (st or {}).get("note"))
    _pending(db, "draft", name, f"新 skill 草稿：{SKILLS[name]['name']}", body=p.read_text(encoding="utf-8"), root=str(root))
    return p


def _pending(db, kind: str, name: str, question: str, body: str = "", root: str = "", why: str = "", extra: dict = None) -> int:
    from . import mepage
    mepage.ensure(db)
    extra = extra or {}
    for r in db.conn.execute("SELECT id, options FROM pending WHERE kind = 'skill' AND status = 'open'").fetchall():
        o = (json.loads(r["options"]) or [{}])[0]
        if o.get("skill") == name and o.get("step") == kind:
            db.conn.execute("UPDATE pending SET question = ?, options = ? WHERE id = ?",
                            (question, json.dumps([{**o, **extra, "body": body[:8000], "text": question}], ensure_ascii=False), r["id"]))
            tx.commit(db)
            return r["id"]
    opt = {"skill": name, "step": kind, "text": question, "body": body[:8000], "root": root, "why": why,
           "src": why or ("批准后邮件起草就按它来；“她的做法”每次用都重新查最新的" if kind == "draft" else ""), **extra}
    pid = db.conn.execute("INSERT INTO pending (kind, type, question, options, fact_ids, users, uses, created_at) VALUES ('skill', 'long', ?, ?, '[]', ?, 0, ?)",
                          (question, json.dumps([opt], ensure_ascii=False), json.dumps([SKILLS.get(name, {}).get("user", "system")]), _now())).lastrowid
    tx.commit(db)
    return pid


def approve(db, name: str, root=None, note: str = "") -> dict:
    """批准：候选移到 active（原来在用的移进 history 留底，能退回）；她写的补充要求原样附在后面。"""
    root = Path(root) if root else data_root(db)
    cand = path(root, name)
    if not cand.exists():
        return {"ok": False, "msg": "草稿不见了，再生成一次"}
    act = path(root, name, "active")
    act.parent.mkdir(parents=True, exist_ok=True)
    text = cand.read_text(encoding="utf-8")
    ver, old = version_of(text), None
    if act.exists():
        old = version_of(act.read_text(encoding="utf-8"))
        _park(act, root, name, "history", old)
    text = STATUS_LINE.sub(f"已批准 · {_now()[:10]} 批准 v{ver} · 用的时候“她的做法”和“授权”会按题重新查最新的；“她纠正过的”是她改过的地方，照着做。",
                           text, count=1)
    if note.strip():
        text += f"\n## 她补充的要求（原话，{_now()[:10]}）\n{note.strip()}\n"
    act.write_text(text, encoding="utf-8")
    cand.unlink()
    _set(db, name, "active", note.strip() or (state(db, name) or {}).get("note"), version=ver)
    if old:
        return {"ok": True, "msg": f"换成 v{ver} 了：{SKILLS[name]['name']}；不合适可以在待你定最下面“在用的 skill”里退回 v{old}"}
    return {"ok": True, "msg": f"批准了：{SKILLS[name]['name']}，以后就按它来"}


def reject_draft(db, name: str, root=None) -> dict:
    """草稿 / 新版本不要：移进 rejected 留底。已经在用的照旧用原来那版。"""
    root = Path(root) if root else data_root(db)
    cand = path(root, name)
    if cand.exists():
        _park(cand, root, name, "rejected", version_of(cand.read_text(encoding="utf-8")))
    act = path(root, name, "active")
    if act.exists():
        cur = version_of(act.read_text(encoding="utf-8"))
        _set(db, name, "active", version=cur)
        return {"ok": True, "msg": f"好，这版不用，还按 v{cur} 来"}
    _set(db, name, "rejected")
    return {"ok": True, "msg": "好，这份草稿不用"}


def rollback(db, name: str, root=None) -> dict:
    """一键退回上一版：现在用的移进 history，history 里比它旧的最新一版放回 active。"""
    root = Path(root) if root else data_root(db)
    act = path(root, name, "active")
    if not act.exists():
        return {"ok": False, "msg": "这个 skill 还没在用"}
    cur = version_of(act.read_text(encoding="utf-8"))
    older = [(version_of(f.read_text(encoding="utf-8")), f.stat().st_mtime, f) for f in _files(root, name, "history")]
    older = [x for x in older if x[0] < cur]
    if not older:
        return {"ok": False, "msg": f"v{cur} 已经是最早的一版，没有可以退回的"}
    prev, _, f = max(older, key=lambda x: (x[0], x[1]))
    _park(act, root, name, "history", cur)
    shutil.move(str(f), str(act))
    _set(db, name, "active", version=prev)
    return {"ok": True, "msg": f"退回 v{prev} 了：{SKILLS[name]['name']}以后按 v{prev} 来（v{cur} 留在 history 里）"}


def listing(db, root=None) -> list:
    """在用的 skill：第几版、能不能退回（给待你定最下面那一栏）。"""
    try:
        root = Path(root) if root else data_root(db)
    except ValueError:
        return []
    out = []
    for name, sk in SKILLS.items():
        act = path(root, name, "active")
        if not act.exists():
            continue
        cur = version_of(act.read_text(encoding="utf-8"))
        older = [version_of(f.read_text(encoding="utf-8")) for f in _files(root, name, "history")]
        older = [v for v in older if v < cur]
        out.append({"skill": name, "title": sk["name"], "version": cur, "back_to": max(older) if older else None,
                    "scope": sk.get("scope", {}).get("label", "")})
    return out


def resolve(db, r, action: str, text: str = "") -> dict:
    """待你定里那一条：草稿 / 新版本 yes 批准 / own 加上她的要求再批准 / no 不要；提议 yes 要（有通用方法的马上起草）/ no 不要。"""
    opt = (json.loads(r["options"]) or [{}])[0]
    name, root = opt.get("skill"), opt.get("root") or None
    if opt.get("step") in ("draft", "revise"):
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


# ---------- 纠正攒够了：落在已有 skill 上 → 迭代新版本；没有 skill 管 → 提议开新的 ----------
RUN_REACT = {"edited": "她自己改了", "revised": "她要改"}


def correction_records(db, weeks: int = PROPOSE_WEEKS) -> list:
    """最近几周她的每一次纠正：纠错单（kind=wrong、没撤销）+ 运行记录里她自己改了 / 要改的。
    每条：ref（唯一）、kind（纠错单是使用者，运行记录是种类，对应 IDEAS）、entry（纠错单从哪进来的）、text（改成什么）、from（哪次）。"""
    from . import runlog
    since = (datetime.now(timezone.utc) - timedelta(days=7 * weeks)).isoformat(timespec="seconds")
    out = []
    try:
        for r in db.conn.execute("SELECT id, at, user, entry, wrong, right FROM corrections WHERE at >= ? AND kind = 'wrong' AND undone_at IS NULL "
                                 "ORDER BY id", (since,)):
            out.append({"ref": f"ticket:{r['id']}", "src": "ticket", "kind": r["user"], "entry": r["entry"], "at": r["at"][:10],
                        "text": (f"不是“{r['wrong']}”，是：{r['right']}" if r["wrong"] else r["right"]),
                        "from": f"{r['at'][:10]} 纠错单 #{r['id']}"})
    except Exception:
        pass
    try:
        for r in db.conn.execute("SELECT key, kind, title, reaction, note, COALESCE(reaction_at, at) t FROM run_log "
                                 "WHERE reaction IN ('edited', 'revised') AND COALESCE(reaction_at, at) >= ? ORDER BY t, key", (since[:10],)):
            how = RUN_REACT[r["reaction"]]
            out.append({"ref": f"run:{r['key']}", "src": "run", "kind": r["kind"], "entry": None, "at": (r["t"] or "")[:10],
                        "text": (r["note"] or "").strip() or f"{how}：{r['title']}",
                        "from": f"{(r['t'] or '')[:10]} {runlog.KINDS.get(r['kind'], r['kind'])}「{r['title']}」{how}"})
    except Exception:
        pass
    return out


def corrections_by_kind(db, weeks: int = PROPOSE_WEEKS) -> dict:
    """最近几周同一类事上她的纠正次数：纠错单（按使用者）+ 运行记录里她自己改了 / 要改的（按种类）。"""
    out = {}
    for r in correction_records(db, weeks):
        out[r["kind"]] = out.get(r["kind"], 0) + 1
    return out


def owner(db, rec: dict):
    """这次纠正落在哪个在用的 skill 管的事上（按 SKILLS 里的 scope）；没有就 None。"""
    for name, sk in SKILLS.items():
        sc = sk.get("scope") or {}
        st = state(db, name)
        if not st or st["status"] != "active":
            continue
        if (rec["src"] == "run" and rec["kind"] in sc.get("runlog", ())) or (rec["src"] == "ticket" and rec["entry"] in sc.get("entries", ())):
            return name
    return None


def _section(text: str) -> str:
    a = text.find(SECTION)
    if a < 0:
        return ""
    b = text.find("\n## ", a + len(SECTION))
    return text[a:b if b >= 0 else len(text)].strip("\n")


def revise_text(old: str, name: str, recs: list, ver: int, cur: int):
    """在 old 上起草新版本：只改“她的做法”那块——每次纠正加一条（附上是哪次纠正），说过的不重复。返回 (新全文, 改动)。"""
    from .core.agent import find_similar
    sec = _section(old)
    if not sec:
        return old, []
    have = [re.sub(r"（v\d+ 新加，依据：.*?）$", "", l[2:]) for l in sec.splitlines() if l.startswith("- ")]
    changes = []
    for r in recs:
        line = " ".join((r["text"] or "").split())[:200]
        if not line or find_similar(line, have + [c["text"] for c in changes]):
            continue
        changes.append({"text": line, "from": r["from"], "ref": r["ref"]})
    if not changes:
        return old, []
    add = "\n".join(f"- {c['text']}（v{ver} 新加，依据：{c['from']}）" for c in changes)
    new_sec = sec + "\n" + add if FIXES in sec else sec + "\n\n" + FIXES + "\n" + add
    text = old.replace(sec, new_sec, 1)
    sk = SKILLS[name]
    text = re.sub(r"\A# skill：.*$", f"# skill：{sk['name']}（{name}）v{ver}", text, count=1, flags=re.M)
    text = STATUS_LINE.sub(f"修订草稿 · {_now()[:10]} 生成 · 在 v{cur} 上改了 {len(changes)} 处（只改“她的做法”）· 批准前不生效，现在用的还是 v{cur}。",
                           text, count=1)
    return text, changes


def draft_revision(db, name: str, recs: list, root=None):
    """落在在用 skill 上的纠正攒够了：起草新版本放候选区，待你定里一条“替你回邮件 v2：改了 N 处”。有一版在等她看就先不起草。"""
    root = Path(root) if root else data_root(db)
    act, cand = path(root, name, "active"), path(root, name)
    if not act.exists() or cand.exists():
        return None
    old = act.read_text(encoding="utf-8")
    cur, ver = version_of(old), next_version(root, name)
    new, changes = revise_text(old, name, recs, ver, cur)
    ensure(db)
    db.conn.executemany("INSERT OR IGNORE INTO skill_fixes (ref, skill, version, at) VALUES (?, ?, ?, ?)",
                        [(r["ref"], name, ver if changes else cur, _now()) for r in recs])  # 和已有的重复的也记成用过，不再反复触发
    tx.commit(db)
    if not changes:
        return None
    cand.parent.mkdir(parents=True, exist_ok=True)
    cand.write_text(new, encoding="utf-8")
    sk = SKILLS[name]
    _pending(db, "revise", name, f"{sk['name']} v{ver}：改了 {len(changes)} 处", body=new, root=str(root),
             why=f"最近 {PROPOSE_WEEKS} 周{sk['scope']['label']}上你纠正 / 改了 {len(recs)} 次，只改了“她的做法”那块；批准才替换 v{cur}，换了也能退回",
             extra={"version": ver, "from_version": cur, "changes": changes, "before": _section(old), "after": _section(new)})
    return cand


def propose(db) -> list:
    """纠正攒够 PROPOSE_AT 次（PROPOSE_WEEKS 周内）：
    - 落在在用 skill 管的事上的（没用进过版本的）→ 起草这个 skill 的新版本（draft_revision），不开新 skill；
    - 没有 skill 管的一类事 → 还没有这个 skill（在用 / 草稿 / 提议过 / 她不要的都不再提）就在待你定里提议一条。只提议，不生效。
    返回起草了新版本或提议了的 skill 名字。"""
    ensure(db)
    used = {r[0] for r in db.conn.execute("SELECT ref FROM skill_fixes")}
    owned, loose = {}, {}
    for rec in correction_records(db):
        name = owner(db, rec)
        if name:
            if rec["ref"] not in used:
                owned.setdefault(name, []).append(rec)
        else:
            loose[rec["kind"]] = loose.get(rec["kind"], 0) + 1
    out = []
    for name, recs in owned.items():
        if len(recs) >= PROPOSE_AT:
            try:
                if draft_revision(db, name, recs):
                    out.append(name)
            except ValueError:  # 内存数据库没有数据目录
                pass
    for kind, n in loose.items():
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


def fixes_of(text: str) -> list:
    """在用版本里“她纠正过的”那几条（去掉“v2 新加，依据……”的尾巴）。"""
    sec = _section(text)
    if FIXES not in sec:
        return []
    part = sec[sec.index(FIXES) + len(FIXES):]
    return [re.sub(r"（v\d+ 新加，依据：.*?）$", "", l[2:]).strip() for l in part.splitlines() if l.startswith("- ")]


def notes_of(text: str) -> list:
    return [m.strip() for m in re.findall(r"^## 她补充的要求（原话，[^）]*）\n(.*?)(?=\n## |\Z)", text, flags=re.M | re.S) if m.strip()]


def prompt_for(db, name: str, root=None) -> str:
    """邮件起草等用：批准了的 skill → 通用方法 + 当场重新查的“她的做法”和授权 + 在用版本里她纠正过的 + 她批准时补充的原话。没批准就空。"""
    try:
        text = active_text(db, name, root)
    except ValueError:
        return ""
    if not text:
        return ""
    sk, way = SKILLS[name], her_way(db, name)
    st = state(db, name) or {}
    parts = [f"按她批准的 skill「{sk['name']}」v{version_of(text)} 来做：", "通用方法：\n" + "\n".join(f"- {x}" for x in sk["method"])]
    for title, key in (("她的偏好", "preferences"), ("她定的规矩", "rules"), ("她说过的要求和禁忌", "requirements")):
        if way[key]:
            parts.append(f"{title}：\n{way[key]}")
    if way["style"]:
        parts.append(way["style"])
    fx = fixes_of(text)
    if fx:
        parts.append("她纠正过的（照这些改）：\n" + "\n".join(f"- {x}" for x in fx))
    notes = notes_of(text) or ([st["note"]] if st.get("note") else [])
    if notes:
        parts.append("她批准时补充的：" + "；".join(notes))
    parts.append(f"能做到哪一步：{way['authorization']}")
    return "\n\n".join(parts)
