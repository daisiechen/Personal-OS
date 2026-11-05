"""聊天里即时抽取（docs/design/digital-life-spec.md 第 3 节、4.1 节，2026-10-30 她认可）。

她每发一条消息，小伴 回完话后在后台跑一次：模型按固定格式提议“记下哪些待办、心愿、事实、事件、要记住的话、提到的人”，
代码校验（原话必须真在她这条消息里、编号必须存在、日期和取值合法），再查重、执行。小伴 回复里的 [待办 …] 标记照旧，
这里是兜底和补充：先跑的是标记，这里查重时能看到它们，不会重复记。

不删任何东西：
- 她说做完了 → 只记“她说做完了”（claimed_done_at），不勾，勾不勾仍按现有规则。
- 她说不做了 → 只记 status = drop_asked，不删；删要她在工作台点。

开关（<数据目录>/extract-mode 文件的内容）：
- 没有文件或写 off：不跑；
- shadow：只调模型、留痕（llm_calls），不写库，用来上线前看真实效果；
- on：照上面执行。
题集 tests/evals/extract_realtime.jsonl 过了（精确率 ≥ 90%、召回率 ≥ 80%）才改成 on。

运行：python3 -m ethan.extract --data <数据目录> --ref raw:<日期>:<行号>
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import lifebase
from .clock import her_today

OWNERS = ("mia", "ethan", "grok", "claude", "codex")
WISH_CATS = ("place", "try", "have", "become")
MODES = ("off", "shadow", "on")
MIN_CHARS = 4  # 比这短（“好”“哈哈”“嗯嗯”）不跑


def _quoted(item_props: dict, required: list) -> dict:
    return {"type": "object", "required": required + ["quote"],
            "properties": {**item_props, "quote": {"type": "string", "maxLength": 200}}}


DIM = {"type": "string", "enum": list(lifebase.DIMENSIONS)}
SCHEMA = {"type": "object", "required": ["todos", "wishes", "facts", "events", "remember", "people"], "properties": {
    "todos": {"type": "array", "maxItems": 8, "items": _quoted({
        "op": {"type": "string", "enum": ["add", "update", "drop", "claim_done"]},
        "match_id": {"type": ["integer", "null"]}, "title": {"type": ["string", "null"], "maxLength": 40},
        "due": {"type": ["string", "null"]}, "owner": {"type": ["string", "null"], "enum": list(OWNERS) + [None]}}, ["op"])},
    "wishes": {"type": "array", "maxItems": 5, "items": _quoted({
        "op": {"type": "string", "enum": ["add", "mention", "drop"]}, "match_id": {"type": ["integer", "null"]},
        "text": {"type": ["string", "null"], "maxLength": 60},
        "category": {"type": ["string", "null"], "enum": list(WISH_CATS) + [None]}}, ["op"])},
    "facts": {"type": "array", "maxItems": 8, "items": _quoted({
        "dimension": DIM, "also_dims": {"type": "array", "maxItems": 3, "items": DIM},
        "subject": {"type": "string", "enum": ["self", "other"]}, "statement": {"type": "string", "maxLength": 80}},
        ["dimension", "statement"])},
    "events": {"type": "array", "maxItems": 5, "items": _quoted({
        "title": {"type": "string", "maxLength": 40}, "occurred_at": {"type": "string"},
        "people": {"type": "array", "items": {"type": "string"}}, "dims": {"type": "array", "items": DIM},
        "emotion": {"type": ["string", "null"], "enum": list(lifebase.EMOTIONS) + [None]},
        "intensity": {"type": ["integer", "null"], "minimum": 1, "maximum": 5}}, ["title", "occurred_at"])},
    "remember": {"type": "array", "maxItems": 5, "items": _quoted({
        "dimension": DIM, "statement": {"type": "string", "maxLength": 80}}, ["dimension", "statement"])},
    "people": {"type": "array", "maxItems": 5, "items": _quoted({
        "name": {"type": "string", "maxLength": 20}, "relation": {"type": ["string", "null"], "maxLength": 20},
        "note": {"type": ["string", "null"], "maxLength": 100}}, ["name"])},
}}
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}")


def mode(root) -> str:
    p = Path(root) / "extract-mode"
    m = p.read_text(encoding="utf-8").strip() if p.is_file() else "off"
    return m if m in MODES else "off"


# ---------- 校验（代码，模型说了不算） ----------
def check(data: dict, text: str, today: date, task_ids=(), wish_ids=()) -> list:
    """返回错误列表；有错就带着错误让模型重试一次（llm.structured）。"""
    from .llm import quote_in
    errors = []
    for kind in ("todos", "wishes", "facts", "events", "remember", "people"):
        for i, item in enumerate(data.get(kind) or []):
            if not quote_in(item.get("quote", ""), text):
                errors.append(f"{kind}[{i}] 的 quote 不在她这条消息里")
    for i, t in enumerate(data.get("todos") or []):
        if t["op"] == "add" and not (t.get("title") or "").strip():
            errors.append(f"todos[{i}] 新增要有 title")
        if t["op"] != "add" and t.get("match_id") not in task_ids:
            errors.append(f"todos[{i}] 的 match_id 不在待办清单里")
        if t.get("due"):
            try:
                d = date.fromisoformat(t["due"])
                if d < today - timedelta(days=365):
                    errors.append(f"todos[{i}] 的 due 早于一年前")
            except ValueError:
                errors.append(f"todos[{i}] 的 due 不是 YYYY-MM-DD")
    for i, w in enumerate(data.get("wishes") or []):
        if w["op"] == "add" and not (w.get("text") or "").strip():
            errors.append(f"wishes[{i}] 新增要有 text")
        if w["op"] != "add" and w.get("match_id") not in wish_ids:
            errors.append(f"wishes[{i}] 的 match_id 不在心愿清单里")
    for i, e in enumerate(data.get("events") or []):
        if not DAY.match(e.get("occurred_at") or ""):
            errors.append(f"events[{i}] 的 occurred_at 要以 YYYY-MM-DD 开头")
    return errors


# ---------- 输入 ----------
def locate(store, text: str) -> str:
    """她刚发的这条在原始对话里的编号 raw:<日期>:<行号>（和 lifebase 补登记用的一样，证据不重复）。"""
    for path in sorted(Path(store.raw.root).glob("*.jsonl"), reverse=True)[:2]:
        lines = path.read_text(encoding="utf-8").split("\n")
        for i in range(len(lines) - 1, -1, -1):
            if not lines[i].strip():
                continue
            r = json.loads(lines[i])
            if r.get("role") == "mia" and r.get("text") == text:
                return f"raw:{path.stem}:{i}"
    return None


def load(store, ref: str) -> tuple:
    """(她这条, 前面几句上下文)。"""
    _, day, line = ref.split(":")
    rows = [json.loads(l) for l in (Path(store.raw.root) / f"{day}.jsonl").read_text(encoding="utf-8").split("\n") if l.strip()]
    i = int(line)
    before = [r for r in rows[max(0, i - 6):i] if r.get("role") in ("mia", "ethan")]
    return rows[i], before


def prompt_input(msg: dict, before: list, tasks: list, wishes: list, today: date) -> str:
    who = {"mia": "她", "ethan": "小伴"}
    ctx = "\n".join(f"{who[r['role']]}：{r['text'][:300]}" for r in before) or "（无）"
    t = "\n".join(f"#{x['id']} {x['title']}" + (f"（{x['due']}）" if x.get("due") else "") for x in tasks) or "（无）"
    w = "\n".join(f"#{x['id']} {x['quote']}" for x in wishes) or "（无）"
    return (f"今天：{today.isoformat()}（{'一二三四五六日'[today.weekday()]}）\n\n前面的对话：\n{ctx}\n\n"
            f"没做完的待办：\n{t}\n\n心愿清单：\n{w}\n\n【她这条消息】\n{msg['text']}")


def propose(router, db, text: str, prompt_text: str, today: date, tasks: list, wishes: list, ref: str = "") -> dict:
    from . import llm
    version, system = llm.load_prompt("extract_realtime")
    tids, wids = {t["id"] for t in tasks}, {w["id"] for w in wishes}
    return llm.structured(router, db, "extract_realtime", system, prompt_text, SCHEMA,
                          check=lambda d: check(d, text, today, tids, wids), version=version, input_ref=ref,
                          purpose="extract-realtime")


# ---------- 执行（查重 + 写库） ----------
def apply(db, data: dict, evidence_id: int, ref: str, today: date) -> dict:
    from .core.agent import find_similar
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    got = {k: 0 for k in ("todo_add", "todo_update", "todo_claim", "todo_drop", "wish_add", "wish_mention",
                          "fact_add", "fact_seen", "event_add", "person")}
    open_tasks = {t["title"]: t for t in db.open_tasks()}
    for t in data.get("todos") or []:
        if t["op"] == "add":
            title = t["title"].strip()[:200]
            same = find_similar(title, list(open_tasks))
            if same:  # 已经有了（多半是 小伴 的标记刚记的）：只补日期
                if t.get("due") and not open_tasks[same].get("due"):
                    db.conn.execute("UPDATE tasks SET due = ? WHERE id = ?", (t["due"], open_tasks[same]["id"]))
                continue
            from . import locks
            if locks.she_deleted(db, title) or locks.renamed_from(db, title):  # 她删掉过 / 改过名字的同一件事，不再从聊天里记一条（docs/design/system-rules.md 第 1 节）
                continue
            owner = t.get("owner") or "mia"
            from . import naming
            title = naming.title(title)  # 标题式（docs/design/ui-spec.md 第 5 节）
            tid = db.add_task(title, due=t.get("due"), promised_by_ethan=owner != "mia")
            db.conn.execute("UPDATE tasks SET owner = ?, source_type = 'chat', source_ref = ?, status = 'open' WHERE id = ?",
                            (owner, ref, tid))
            open_tasks[title] = {"id": tid, "title": title, "due": t.get("due")}
            got["todo_add"] += 1
        elif t["op"] == "update":
            db.conn.execute("UPDATE tasks SET title = COALESCE(?, title), due = COALESCE(?, due) WHERE id = ? AND done = 0",
                            ((t.get("title") or "").strip()[:200] or None, t.get("due"), t["match_id"]))
            got["todo_update"] += 1
        elif t["op"] == "claim_done":
            db.conn.execute("UPDATE tasks SET claimed_done_at = ? WHERE id = ? AND done = 0", (now, t["match_id"]))
            got["todo_claim"] += 1
        else:  # drop：只标记，不删
            db.conn.execute("UPDATE tasks SET status = 'drop_asked' WHERE id = ? AND done = 0", (t["match_id"],))
            got["todo_drop"] += 1
    wishes = {w["quote"]: w["id"] for w in db.open_wishes()}
    for w in data.get("wishes") or []:
        wid = w.get("match_id")
        if w["op"] == "add":
            same = find_similar(w["text"], list(wishes))
            if not same:
                wid = db.add_wish(w["text"].strip(), context=w["quote"])
                db.conn.execute("UPDATE wishes SET category = ?, quotes = ?, last_mentioned_at = ? WHERE id = ?",
                                (w.get("category"), json.dumps([w["quote"]], ensure_ascii=False), now, wid))
                wishes[w["text"].strip()] = wid
                got["wish_add"] += 1
                continue
            wid = wishes[same]
        if w["op"] in ("add", "mention"):
            row = db.conn.execute("SELECT quotes FROM wishes WHERE id = ?", (wid,)).fetchone()
            quotes = (json.loads(row["quotes"] or "[]") + [w["quote"]])[-10:]
            db.conn.execute("UPDATE wishes SET mention_count = mention_count + 1, last_mentioned_at = ?, quotes = ? WHERE id = ?",
                            (now, json.dumps(quotes, ensure_ascii=False), wid))
            got["wish_mention"] += 1
        # drop：心愿不自动关，下次每周体检时问她
    people = {}
    for p in data.get("people") or []:
        name = p["name"].strip()
        row = db.conn.execute("SELECT id, circle, notes FROM people WHERE name = ?", (name,)).fetchone()
        fields = {}
        if p.get("relation") and not (row and row["circle"]):
            fields["circle"] = p["relation"]
        if p.get("note"):
            fields["notes"] = ((row["notes"] + "\n") if row and row["notes"] else "") + f"（{today.isoformat()}）{p['note']}"
        people[name] = db.upsert_person(name, **fields)
        got["person"] += 1
    facts = [(f, 3, 0.6, ()) for f in data.get("facts") or []] + [(r, 5, 0.9, ("remember",)) for r in data.get("remember") or []]
    for f, importance, conf, tags in facts:
        subject = "self"
        if f.get("subject") == "other":
            named = [pid for name, pid in people.items() if name in f["statement"] or name in f["quote"]]
            if not named:
                continue  # 别人的事对不上人就不记
            subject = f"person:{named[0]}"
        current = [r for r in lifebase.current_facts(db, f["dimension"]) if r["subject"] == subject]
        same = find_similar(f["statement"], [r["statement"] for r in current])
        if same:  # 已经知道：记一次证实，不另记
            row = next(r for r in current if r["statement"] == same)
            ids = json.loads(row["evidence_ids"])
            db.conn.execute("UPDATE facts SET last_confirmed_at = ?, evidence_ids = ?, importance = MAX(importance, ?) WHERE id = ?",
                            (now, json.dumps(sorted(set(ids) | {evidence_id})), importance, row["id"]))
            got["fact_seen"] += 1
            continue
        lifebase.add_fact(db, f["dimension"], f["statement"], [evidence_id], subject=subject, also_dims=f.get("also_dims") or (),
                          tags=tags, confidence=conf, importance=importance, valid_from=today.isoformat())
        got["fact_add"] += 1
    for e in data.get("events") or []:
        day = e["occurred_at"][:10]
        same_day = [r["title"] for r in db.conn.execute("SELECT title FROM events WHERE substr(occurred_at, 1, 10) = ?", (day,))]
        if find_similar(e["title"], same_day):
            continue
        lifebase.add_event(db, e["occurred_at"], e["title"], [evidence_id], "realtime", people=e.get("people") or (),
                           dims=e.get("dims") or (), emotion=e.get("emotion"), intensity=e.get("intensity"))
        got["event_add"] += 1
    db.conn.commit()
    return got


def run(store, router, ref: str, today: date = None, force_mode: str = None) -> dict:
    m = force_mode or mode(store.root)
    if m == "off":
        return {"skipped": "关着"}
    db = store.db
    lifebase.ensure(db)
    msg, before = load(store, ref)
    text = msg.get("text") or ""
    if len(re.sub(r"\s", "", text)) < MIN_CHARS:
        return {"skipped": "太短"}
    if lifebase.is_forgotten(db, "chat", ref):
        return {"skipped": "她要求删掉过这条"}
    today = today or date.fromisoformat((msg.get("at") or "")[:10] or her_today().isoformat())
    tasks, wishes = db.open_tasks(), db.open_wishes()
    data = propose(router, db, text, prompt_input(msg, before, tasks, wishes, today), today, tasks, wishes, ref)
    if data is None:
        return {"failed": "两次都没通过校验"}
    if m == "shadow":
        return {"shadow": {k: len(v) for k, v in data.items()}}
    ev = lifebase.add_evidence(db, "chat", ref, msg.get("at") or today.isoformat(), f"mia：{text}", path=f"raw/{ref.split(':')[1]}.jsonl")
    return apply(db, data, ev, ref, today)


def kick(root, ref: str, spawn=subprocess.Popen) -> bool:
    """小伴 回完话：后台抽取她这条，不拖慢回复。开关关着就不起进程。"""
    if not ref or mode(root) == "off":
        return False
    try:
        spawn([sys.executable, "-m", "ethan.extract", "--data", str(root), "--ref", ref],
              cwd=str(Path(__file__).resolve().parent.parent), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              start_new_session=True)
        return True
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser(description="聊天里即时抽取")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--mode", choices=MODES)
    args = parser.parse_args()
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        print(json.dumps(run(store, build_router(args.data, 50.0), args.ref, force_mode=args.mode), ensure_ascii=False), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
