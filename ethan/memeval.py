"""记忆对齐第二步：错题回流、每周一圈、上线闸门、周报（docs/design/memory-alignment.md 第 5 节，她 2026-10-31 同意）。

1. 每张纠错单按错在哪一层自动变成一道检验题，存在她自己的数据库里（eval_items），不进代码仓库：
   - 没查到 → 查记忆：当时那句话应该查出这条锁定的事实；
   - 没记住 → 即时抽取：她说过的那句原话，应该记下这条事实；
   - 记错了 / 过时了 → 每晚对照：新说法和旧说法放一起，旧的应该被改掉；
   - 用错了 → 回答：记忆里有这条，回答里得用上，不能再说错的；
   - 他自己的事 → 回答：问 小伴 他自己的这件事，得和记录对得上；
   - 没告诉过 → 不出题，算“这方面还缺资料”。
2. 每周日夜里（每晚整理顺带）跑一圈，按层算通过率，和上周比；结果进周日周报的一行。
3. 上线闸门：自动部署发现这次改了记忆相关的代码或提示词，单元测试过了以后再跑一遍全部检验题；
   以前通过的题这次没过就算退步，整个版本退回（ethan/autodeploy.py）。模型连不上时不拦（记一笔），免得订阅额度用完就上不了线。
跑题用的是她的 Claude 订阅额度，不另外花钱。
运行：python3 -m ethan.memeval --data <数据目录> [--gate]
ontology 正式评估（ontology-plan 4.3）：python3 -m ethan.memeval --formal --data <数据目录>（见 ethan/formaleval.py）
"""

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from .clock import her_today

SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_items (
    id INTEGER PRIMARY KEY, ticket_id INTEGER, layer TEXT NOT NULL, task TEXT NOT NULL, input TEXT NOT NULL, expect TEXT NOT NULL,
    created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, last_pass INTEGER, last_note TEXT, last_at TEXT);
CREATE TABLE IF NOT EXISTS eval_runs (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, total INTEGER NOT NULL, passed INTEGER NOT NULL,
    by_layer TEXT NOT NULL, failed TEXT NOT NULL, regressions TEXT NOT NULL DEFAULT '[]', skipped INTEGER NOT NULL DEFAULT 0);
"""
# 改了这些就要过闸门
MEMORY_PATHS = ("ethan/memsearch.py", "ethan/extract.py", "ethan/portrait.py", "ethan/lifebase.py", "ethan/memusers.py",
                "ethan/memeval.py", "ethan/life.py", "ethan/core/agent.py", "ethan/prompts/")
JUDGE = ("你是检验员。下面有一个问题、一段回答，以及关于这件事的【正确说法】和【错误说法】。"
         "回答里的意思和正确说法一致、没有再说错误说法，就算对；没提到这件事也算不对。只输出 JSON："
         '{"ok": true 或 false, "why": "一句话"}')
ANSWER = ("你是 小伴，Mia 的男朋友。下面是你记得的相关的事，按它们回答她的问题，一两句话，口语。\n\n你记得的：\n{memory}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.executescript(SCHEMA)


# ---------- 1. 纠错单 → 检验题 ----------
def add_for_ticket(db, ticket_id: int) -> list:
    """按纠错单那一层出题；返回新题编号。同一张单只出一次。"""
    ensure(db)
    if db.conn.execute("SELECT 1 FROM eval_items WHERE ticket_id = ?", (ticket_id,)).fetchone():
        return []
    t = db.conn.execute("SELECT * FROM corrections WHERE id = ?", (ticket_id,)).fetchone()
    if t is None:
        return []
    question = (t["quote"] or "").strip() or (f"{t['wrong']}，对吗？" if t["wrong"] else t["right"])
    items = []
    if t["layer"] == "not_found" and t["fact_id"]:
        last = db.conn.execute("SELECT queries FROM retrieval_log WHERE user = ? ORDER BY id DESC LIMIT 1", (t["user"],)).fetchone()
        queries = json.loads(last["queries"]) if last else [question]
        items.append(("retrieval", {"queries": queries[:3]}, {"key": f"fact:{t['fact_id']}"}))
    elif t["layer"] == "missed":
        items.append(("extract", {"text": question}, {"statement": t["right"]}))
    elif t["layer"] in ("wrong", "outdated"):
        items.append(("fact_ops", {"new": t["right"], "old": t["wrong"] or "", "dimension": t["dimension"] or "preferences"},
                      {"change_old": True}))
    if t["layer"] in ("misused", "wrong", "outdated", "self") or (t["layer"] == "not_found" and t["fact_id"]):
        # 不管错在哪层，最后都看回答：带着对的记忆，回答不能再说错
        about = "self" if t["layer"] == "self" else "her"
        items.append(("answer", {"question": question, "about": about}, {"right": t["right"], "wrong": t["wrong"] or ""}))
    ids = []
    for task, inp, expect in items:
        cur = db.conn.execute("INSERT INTO eval_items (ticket_id, layer, task, input, expect, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                              (ticket_id, t["layer"], task, json.dumps(inp, ensure_ascii=False), json.dumps(expect, ensure_ascii=False), _now()))
        ids.append(cur.lastrowid)
    db.conn.commit()
    return ids


def backfill(db) -> int:
    """以前开的纠错单补出题（第一次跑时）。"""
    ensure(db)
    n = 0
    for r in db.conn.execute("SELECT id FROM corrections WHERE id NOT IN (SELECT ticket_id FROM eval_items WHERE ticket_id IS NOT NULL)").fetchall():
        n += len(add_for_ticket(db, r["id"]))
    return n


# ---------- 2. 跑题 ----------
def _judge(router, question: str, answer: str, expect: dict) -> tuple:
    text = router.complete(JUDGE, [{"role": "user", "content": f"今天是 {her_today().isoformat()}（回答里的“昨天”“前天”按这个算）\n"
                                                               f"问题：{question}\n回答：{answer}\n【正确说法】{expect['right']}\n"
                                                                   f"【错误说法】{expect.get('wrong') or '（无）'}"}], purpose="memeval-judge").text
    start = text.find("{")
    data, _ = json.JSONDecoder().raw_decode(text[start:]) if start >= 0 else ({}, 0)  # 只取第一段 JSON（判分模型有时多吐一段）
    return bool(data.get("ok")), str(data.get("why") or "")[:120]


def _memory_for(db, store, inp: dict) -> str:
    if inp.get("about") == "self" and store is not None and store.wiki.exists("ethan-life/self-corrections"):
        return store.wiki.read("ethan-life/self-corrections")["body"][-1500:]
    from . import memsearch
    memsearch.refresh(db)
    return memsearch.lines(memsearch.search(db, [inp["question"]], "chat", user="system", log=False))


def run_item(db, router, item: dict, store=None) -> tuple:
    """(通过?, 说明)。"""
    from .core.agent import find_similar
    inp, expect = json.loads(item["input"]), json.loads(item["expect"])
    if item["task"] == "retrieval":
        from . import memsearch
        memsearch.refresh(db)
        keys = [r["key"] for r in memsearch.search(db, inp["queries"], "chat", user="system", log=False)]
        return expect["key"] in keys, "" if expect["key"] in keys else f"没查出 {expect['key']}"
    if item["task"] == "extract":
        from . import extract
        today = her_today()
        text = extract.prompt_input({"role": "mia", "text": inp["text"]}, [], [], [], today)
        data = extract.propose(router, db, inp["text"], text, today, [], [], f"eval:{item['id']}") or {}
        said = [f["statement"] for k in ("facts", "remember") for f in data.get(k) or []]
        ok = bool(find_similar(expect["statement"], said))
        return ok, "" if ok else f"没记下：{expect['statement']}"
    if item["task"] == "fact_ops":
        from . import portrait
        old = [{"id": 1, "dimension": inp["dimension"], "statement": inp["old"], "valid_from": "2026-01-01", "recorded_at": "2026-01-01"}]
        data = portrait.propose_ops(router, db, [inp["new"]], old, her_today(), ref=f"eval:{item['id']}") or {"ops": []}
        ok = any(op["op"] in ("UPDATE", "INVALIDATE") and op.get("fact_id") == 1 for op in data["ops"])
        return ok, "" if ok else "旧说法没被改掉"
    if item["task"] == "answer":
        memory = _memory_for(db, store, inp)
        answer = router.complete(ANSWER.format(memory=memory or "（没有）"), [{"role": "user", "content": inp["question"]}],
                                 purpose="memeval-answer").text.strip()
        return _judge(router, inp["question"], answer, expect)
    return False, f"不认识的题型 {item['task']}"


def run(db, router, kind: str = "weekly", store=None) -> dict:
    """跑全部检验题；返回 {total, passed, by_layer, failed, regressions, skipped}。"""
    from .core.backends import BackendUnavailable
    from .core.router import AllBackendsFailed, NeedsSwitchApproval
    ensure(db)
    backfill(db)
    items = [dict(r) for r in db.conn.execute("SELECT * FROM eval_items WHERE active = 1 ORDER BY id")]
    by_layer, failed, regressions, skipped, results = {}, [], [], 0, []
    for it in items:
        try:
            ok, note = run_item(db, router, it, store)
        except (BackendUnavailable, AllBackendsFailed, NeedsSwitchApproval):  # 模型连不上：这题不算
            skipped += 1
            continue
        except Exception as e:
            ok, note = False, f"{type(e).__name__}: {str(e)[:100]}"
        if not ok and kind == "gate" and it["last_pass"] == 1:
            # 闸门上以前过、这次没过：模型回答和判分都有随机性，再跑两次，有一次过就不算退步
            # （2026-11-01 照片那题回答基本对、判分说“含糊”，把整批上线退回了；真退步会三次都不过）
            for _ in range(2):
                try:
                    ok, note2 = run_item(db, router, it, store)
                except Exception:
                    continue
                if ok:
                    break
        layer = by_layer.setdefault(it["layer"], {"total": 0, "passed": 0})  # 重跑之后再计分（11-05：重跑过了却按没过算）
        layer["total"] += 1
        layer["passed"] += 1 if ok else 0
        if not ok:
            failed.append({"id": it["id"], "layer": it["layer"], "task": it["task"], "note": note})
            if it["last_pass"] == 1:
                regressions.append(it["id"])
        results.append((1 if ok else 0, note, _now(), it["id"]))
    if not (kind == "gate" and regressions):  # 闸门拦下的那次不记成新基线，免得退回以后下次就看不出退步
        db.conn.executemany("UPDATE eval_items SET last_pass = ?, last_note = ?, last_at = ? WHERE id = ?", results)
    total = sum(v["total"] for v in by_layer.values())
    passed = sum(v["passed"] for v in by_layer.values())
    db.conn.execute("INSERT INTO eval_runs (at, kind, total, passed, by_layer, failed, regressions, skipped) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (_now(), kind, total, passed, json.dumps(by_layer, ensure_ascii=False), json.dumps(failed, ensure_ascii=False),
                     json.dumps(regressions), skipped))
    db.conn.commit()
    return {"total": total, "passed": passed, "by_layer": by_layer, "failed": failed, "regressions": regressions, "skipped": skipped}


def touches_memory(changed: list) -> bool:
    return any(f.startswith(MEMORY_PATHS) for f in changed)


# ---------- 3. 周报那一行 ----------
def weekly_summary(db, today: date = None) -> dict:
    """周日周报里的一行：对齐率、纠错数（按层）、复发率、题集通过率和变化、小伴 问了几个确认问题。"""
    from . import memusers
    ensure(db)
    memusers.ensure(db)
    today = today or her_today()
    since = (today - timedelta(days=7)).isoformat()
    fixes = db.conn.execute("SELECT layer, recurrence_of FROM corrections WHERE at >= ? AND kind = 'wrong' AND undone_at IS NULL",
                            (since,)).fetchall()
    try:  # 她确认过的（待你定里定的、聊天里答的；2026-10-31 两套“问她”合一后只有这一处）
        confirmed = db.conn.execute("SELECT COUNT(*) FROM confirmations WHERE at >= ?", (since,)).fetchone()[0]
    except Exception:
        confirmed = 0
    runs = db.conn.execute("SELECT total, passed, at FROM eval_runs WHERE kind = 'weekly' ORDER BY id DESC LIMIT 2").fetchall()
    rate = lambda r: round(r["passed"] / r["total"], 3) if r and r["total"] else None
    layers = {}
    for f in fixes:
        name = memusers.LAYERS[f["layer"]]
        layers[name] = layers.get(name, 0) + 1
    return {"纠错数": len(fixes), "按层": layers,
            "复发率": round(sum(1 for f in fixes if f["recurrence_of"]) / len(fixes), 3) if fixes else 0.0,
            "对齐率": round(confirmed / (confirmed + len(fixes)), 3) if (confirmed + len(fixes)) else None,
            "她确认过的": confirmed,
            "检验题通过率": rate(runs[0]) if runs else None, "上周通过率": rate(runs[1]) if len(runs) > 1 else None,
            "检验题数": runs[0]["total"] if runs else 0}


def main():
    p = argparse.ArgumentParser(description="跑记忆检验题")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--gate", action="store_true", help="上线闸门：以前过的题这次没过就退出码 1")
    p.add_argument("--formal", action="store_true", help="ontology 正式评估（ontology-plan 4.3），在副本上跑、不调模型，写一份报告")
    p.add_argument("--questions", type=Path, help="--formal 用的题集（默认 <数据目录>/evals/formal.json）")
    p.add_argument("--out", type=Path, help="--formal 报告写到哪（默认 <数据目录>/reports/ontology-eval-日期.md）")
    a = p.parse_args()
    if a.formal:
        from . import formaleval
        print(formaleval.main(a.data, a.questions, a.out), flush=True)
        return
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        got = run(store.db, build_router(a.data, 50.0), "gate" if a.gate else "weekly", store)
        print(json.dumps({k: got[k] for k in ("total", "passed", "by_layer", "regressions", "skipped")}, ensure_ascii=False), flush=True)
        for f in got["failed"]:
            print(f"  ✗ #{f['id']}（{f['layer']}/{f['task']}）{f['note']}", flush=True)
    finally:
        store.close()
    if a.gate and got["regressions"]:
        print(f"退步：{got['regressions']} 以前通过、这次没过", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
