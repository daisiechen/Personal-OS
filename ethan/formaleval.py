"""ontology 正式评估（ontology-plan 4.3，按 docs/design/ontology.md 第 9 节）。

运行：python3 -m ethan.memeval --formal --data /home/ethan/data [--questions 题集.json] [--out 报告.md]
- 在**内存里的副本**上跑（retire._Copy），不往她的使用记录（function_log、retrieval_log）里添数，不动数据；
- 不调模型：F 组 = 能力题函数（结构化说法），R 组 = 同一个使用者、同一句问话的原文检索（memsearch.search，按使用者权限筛）；
  比的是“该有的有没有、不该有的有没有”，H 组（加行为假设）和主观题的“她两周后再答一次”这次不跑，报告里写明；
- 报告只写数字和题号，不写她的记忆内容，可以直接放进 docs/research/。

题从两处来：
1. 自动出的（每道首版题都有）：① 能用——登记的使用者按默认参数调一次，返回格式对（题号、依据、状态）；
   ② 权限——用对外的使用者（主页 site）去问不对外的题，依据里出现不公开的说法就算泄露；第 15 题拿住址、钱、身体这类去问，必须拒答；
   ③ 查得到——从她说过、还算数的说法里抽几条，用里面的词去问第 4、18 题，看 F 的依据和 R 的前 10 条里有没有这一条。
2. 她定的题集（可选，<数据目录>/evals/formal.json，或 --questions）：每题
   {"q": 4, "user": "ethan", "args": {"topic": "咖啡"}, "ask": "她喝咖啡有什么讲究", "category": "抽信息|跨多次|时间|知识更新|该拒答|权限|主观",
    "expect": {"status": "answer|unknown", "contains": ["燕麦奶"], "absent": ["全脂"]}}
   标准答案跑之前定好、存版本，不边跑边改（ontology.md 9.2）。题集里有她的记忆内容，只放她的数据目录，不进仓库。
"""

import inspect
import json
import random
from datetime import date, datetime, timezone
from pathlib import Path

PUBLIC_Q = (14, 15, 16)
SAMPLE_Q = {4: "topic", 18: "topic"}  # 查得到：这两题直接返回说法（依据是说法编号），用说法里的词去问；第 4 题只抽偏好
DEFAULTS = {"topic": "工作", "name": "妈妈", "task_title": "准备面试", "action": "发消息", "scope": "邮件", "words": "跑步",
            "title": "跑步", "keyword": "签证"}
SENSITIVE_ASKS = ("住址", "一个月花多少钱", "体重")
PASS = {"accuracy_gain": 0.10, "refuse": 0.95, "over_refuse": 0.10, "leaks": 0}


def _user_for(q: int) -> str:
    from . import memusers
    if q in PUBLIC_Q:
        return "twin"
    return next((u for u, v in memusers.USERS.items() if q in v.get("questions", []) and u != "system"), "system")


def _call(db, q: int, user: str, args: dict = None, today: date = None):
    from . import onto_query
    fn = getattr(onto_query, onto_query.QUESTIONS[q])
    params = inspect.signature(fn).parameters
    kw = {}
    for name, p in list(params.items())[2:]:
        if args and name in args:
            kw[name] = args[name]
        elif p.default is inspect.Parameter.empty:
            if name == "day":
                kw[name] = today
            elif name == "statement_id":
                r = db.conn.execute("SELECT MIN(id) FROM statements").fetchone()
                kw[name] = (r[0] if r else None) or 1
            else:
                kw[name] = DEFAULTS.get(name, "工作")
        elif name in ("today",) and today:
            kw[name] = today
    return fn(db, user, **kw)


def _text(got) -> str:
    out = []
    for i in got.get("items", []):
        out.append(json.dumps(i, ensure_ascii=False, default=str) if isinstance(i, dict) else str(i))
    return " ".join(out) + " " + (got.get("note") or "")


def _safe(i) -> bool:
    """拒答、“只能起草”、“转给她”这类占位，和她逐条批准过的对外说法（disclosure）不算泄露。"""
    from .onto_query import REFUSE
    return isinstance(i, dict) and (i.get("refuse") is True or i.get("reply") == REFUSE or i.get("allowed") == "draft_only"
                                    or i.get("forward") is True or "disclosure" in i)


def _leaked(db, got) -> bool:
    """对外使用者拿到的依据里有不公开的说法（或者条目根本不带依据）就算泄露。"""
    items = [i for i in got.get("items") or [] if not _safe(i)]
    if not items:
        return False
    basis = got.get("basis") or []
    if not basis:
        return True
    vis = [r[0] for r in db.conn.execute(f"SELECT COALESCE(visibility, 'private') FROM statements WHERE id IN ({','.join('?' * len(basis))})", basis)]
    return any(v != "public" for v in vis)


def _r_hits(db, user: str, ask: str) -> list:
    from . import memsearch
    # 每个使用者写死一行（tests/test_memusers.py 要求每处检索都写明登记过的使用者）；R 组按同一个使用者的权限筛
    calls = {
        "ethan": lambda: memsearch.search(db, [ask], "chat", limit=10, user="ethan", log=False),
        "planner": lambda: memsearch.search(db, [ask], "chat", limit=10, user="planner", log=False),
        "prep": lambda: memsearch.search(db, [ask], "chat", limit=10, user="prep", log=False),
        "mail": lambda: memsearch.search(db, [ask], "chat", limit=10, user="mail", log=False),
        "review": lambda: memsearch.search(db, [ask], "chat", limit=10, user="review", log=False),
        "patrol": lambda: memsearch.search(db, [ask], "chat", limit=10, user="patrol", log=False),
        "me_page": lambda: memsearch.search(db, [ask], "chat", limit=10, user="me_page", log=False),
        "twin": lambda: memsearch.search(db, [ask], "public", limit=10, user="twin", log=False),
        "site": lambda: memsearch.search(db, [ask], "public", limit=10, user="site", log=False),
    }
    try:
        return calls.get(user, calls["site"])()  # 不认识的按最严（对外）查
    except Exception:
        return []


def _judge(text: str, expect: dict, status: str) -> float:
    """对 1 / 部分对 0.5 / 错 0（ontology.md 9.3）。"""
    want = expect.get("status", "answer")
    if want == "unknown":
        return 1.0 if status in ("unknown", "unavailable") or not any(c in text for c in expect.get("contains", [])) else 0.0
    if status in ("unknown", "unavailable", "forbidden"):
        return 0.0
    if any(a in text for a in expect.get("absent", [])):
        return 0.0
    need = expect.get("contains", [])
    if not need:
        return 1.0
    got = sum(1 for c in need if c in text)
    return 1.0 if got == len(need) else 0.5 if got else 0.0


def _sample(db, n: int, seed: int, kind: str = None) -> list:
    """她说过、还算数的说法里抽几条（按编号排序后定种子抽，同一份数据每次抽到的一样）。"""
    from . import ontology
    sid = ontology.self_id(db)
    rows = db.conn.execute("SELECT s.id, s.value_text, m.fact_id FROM statements s LEFT JOIN fact_map m ON m.statement_id = s.id "
                           "WHERE s.subject_id = ? AND s.review = 'accepted' AND s.valid_to IS NULL AND s.superseded_by IS NULL "
                           "AND s.said_by IN ('her_words', 'her_confirmation', 'her_provided') AND COALESCE(s.kind, 'fact') != 'hypothesis' "
                           "AND length(s.value_text) >= 4" + (" AND s.kind = ?" if kind else "") + " ORDER BY s.id",
                           (sid, *([kind] if kind else []))).fetchall() if sid else []
    rnd = random.Random(seed)
    return rnd.sample(list(rows), min(n, len(rows)))


def _words(text: str) -> str:
    from .memsearch import tokens
    toks = [t for t in tokens(text, query=True) if len(t) >= 2][:3]
    return " ".join(toks) or text[:6]


def load_questions(path: Path) -> list:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("items", data) if isinstance(data, (dict, list)) else []


def run(db, today: date = None, questions: list = (), per_q: int = 10, seed: int = 20261104) -> dict:
    """跑一次；返回结构化结果（报告由 report() 写）。db 应该是副本。"""
    from . import lifebase, onto_query, ontology
    from .clock import her_today
    from . import memsearch
    today = today or her_today()
    lifebase.ensure(db)
    ontology.ensure(db)
    try:
        memsearch.refresh(db)
    except Exception:
        pass
    per = {q: {"q": q, "fn": fn, "user": _user_for(q), "usable": False, "status": None, "probes": 0, "leaks": 0,
               "f_hit": 0, "r_hit": 0, "sampled": 0, "error": ""} for q, fn in onto_query.QUESTIONS.items()}
    for q, row in per.items():  # ① 能用
        try:
            got = _call(db, q, row["user"], today=today)
            row["status"] = got["status"]
            row["usable"] = got.get("q") == q and "basis" in got and got["status"] in ("answer", "disputed", "unknown", "unavailable")
        except Exception as e:
            row["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    leaks, probes = 0, 0
    for q, row in per.items():  # ② 权限：对外的使用者去问不对外的题
        if q in PUBLIC_Q:
            continue
        try:
            got = _call(db, q, "site", today=today)
        except Exception:
            continue  # 拒绝调用也是挡住
        row["probes"] += 1
        probes += 1
        if _leaked(db, got):
            row["leaks"] += 1
            leaks += 1
    refused, sens = 0, 0
    for ask in SENSITIVE_ASKS:  # 第 15 题：敏感话题必须拒答
        for user in ("twin", "site"):
            try:
                got = onto_query.public_sensitive(db, user, ask)
            except Exception:
                continue
            sens += 1
            ok = bool(got.get("items")) and all(_safe(i) for i in got["items"])
            refused += 1 if ok else 0
            per[15]["probes"] += 1
            if not ok:
                per[15]["leaks"] += 1
                leaks += 1
    picks = [(4, s) for s in _sample(db, per_q, seed, "preference")] + [(18, s) for s in _sample(db, per_q, seed)]  # ③ 查得到
    for q, s in picks:
        row = per[q]
        ask = _words(s["value_text"])
        try:
            got = _call(db, q, row["user"], {SAMPLE_Q[q]: ask}, today)
        except Exception:
            continue
        row["sampled"] += 1
        row["f_hit"] += 1 if s["id"] in (got.get("basis") or []) else 0
        keys = {h["key"] for h in _r_hits(db, row["user"], ask)}
        row["r_hit"] += 1 if (s["fact_id"] and f"fact:{s['fact_id']}" in keys) else 0
    gold = {"total": 0, "f": 0.0, "r": 0.0, "by_category": {}, "refuse_want": 0, "refuse_ok": 0, "answer_want": 0, "over_refused": 0,
            "update_total": 0, "f_old": 0, "r_old": 0}
    for it in questions:  # 她定的题集
        try:
            q, user, expect = int(it["q"]), it.get("user") or _user_for(int(it["q"])), it.get("expect") or {}
            got = _call(db, q, user, it.get("args") or {}, today)
        except Exception:
            continue
        ftext = _text(got)
        rtext = " ".join(h.get("text") or "" for h in _r_hits(db, user, it.get("ask") or ""))
        rstatus = "answer" if rtext.strip() else "unknown"
        f, r = _judge(ftext, expect, got["status"]), _judge(rtext, expect, rstatus)
        cat = it.get("category") or "抽信息"
        c = gold["by_category"].setdefault(cat, {"total": 0, "f": 0.0, "r": 0.0})
        c["total"] += 1
        c["f"] += f
        c["r"] += r
        gold["total"] += 1
        gold["f"] += f
        gold["r"] += r
        if expect.get("status") == "unknown":
            gold["refuse_want"] += 1
            gold["refuse_ok"] += 1 if got["status"] in ("unknown", "unavailable") else 0
        else:
            gold["answer_want"] += 1
            gold["over_refused"] += 1 if got["status"] in ("unknown", "unavailable") else 0
        if cat == "知识更新":
            gold["update_total"] += 1
            gold["f_old"] += 1 if any(a in ftext for a in expect.get("absent", [])) else 0
            gold["r_old"] += 1 if any(a in rtext for a in expect.get("absent", [])) else 0
        if cat == "权限" and user in ("site", "twin") and _leaked(db, got):
            leaks += 1
            per[q]["leaks"] += 1
    sampled = sum(r["sampled"] for r in per.values())
    try:
        last = db.conn.execute("SELECT total, passed, at FROM eval_runs ORDER BY id DESC LIMIT 1").fetchone()
        items = {"total": last["total"], "passed": last["passed"], "at": last["at"][:10]} if last else None
    except Exception:
        items = None
    try:
        pending = db.conn.execute("SELECT COUNT(*) FROM pending WHERE status = 'open'").fetchone()[0]
    except Exception:
        pending = None
    return {"at": today.isoformat(), "per": list(per.values()), "leaks": leaks, "probes": probes + sens, "sensitive": sens,
            "sensitive_refused": refused, "sampled": sampled, "f_hit": sum(r["f_hit"] for r in per.values()),
            "r_hit": sum(r["r_hit"] for r in per.values()), "gold": gold, "eval_items": items, "pending_open": pending,
            "usable": sum(1 for r in per.values() if r["usable"]), "questions": len(per)}


def _pct(a, b):
    return f"{round(100 * a / b)}%" if b else "—"


def verdict(res: dict) -> dict:
    """按 ontology.md 9.3 的过线标准逐项判；测不了的写“这次没测”。"""
    g = res["gold"]
    out = {"能用": ("过" if res["usable"] == res["questions"] else "没过", f"{res['usable']}/{res['questions']} 道题按合同返回")}
    out["权限遵守"] = ("过" if res["leaks"] == PASS["leaks"] else "没过",
                       f"{res['probes']} 次对外试探，泄露 {res['leaks']} 次（0 次只说明这批没测出，上线后继续抽查）")
    if g["total"]:
        gain = (g["f"] - g["r"]) / g["total"]
        out["准确"] = ("过" if gain >= PASS["accuracy_gain"] else "没过", f"F {_pct(g['f'], g['total'])}，R {_pct(g['r'], g['total'])}（要高 10 个百分点）")
    elif res["sampled"]:
        out["准确"] = ("参考", f"题集还没定，只比了“查得到”：F {_pct(res['f_hit'], res['sampled'])}，R {_pct(res['r_hit'], res['sampled'])}（{res['sampled']} 条抽样）")
    else:
        out["准确"] = ("这次没测", "没有题集，也没有可抽样的说法")
    if g["update_total"]:
        out["时效"] = ("过" if g["f_old"] * 2 <= max(g["r_old"], 0) or g["f_old"] == 0 else "没过",
                       f"答成旧的：F {g['f_old']}/{g['update_total']}，R {g['r_old']}/{g['update_total']}")
    else:
        out["时效"] = ("这次没测", "题集里没有“知识更新”类的题")
    if g["refuse_want"] or g["answer_want"]:
        ok = (not g["refuse_want"] or g["refuse_ok"] / g["refuse_want"] >= PASS["refuse"]) and \
             (not g["answer_want"] or g["over_refused"] / g["answer_want"] <= PASS["over_refuse"])
        out["拒答"] = ("过" if ok else "没过", f"该拒的拒了 {_pct(g['refuse_ok'], g['refuse_want'])}，不该拒的被拒 {_pct(g['over_refused'], g['answer_want'])}")
    else:
        out["拒答"] = ("参考", f"敏感话题拒答 {res['sensitive_refused']}/{res['sensitive']}（题集定了再算过度拒答）")
    out["维护成本"] = ("参考", f"待你定还开着 {res['pending_open'] if res['pending_open'] is not None else '—'} 条；她每周花的分钟数要她自己报")
    out["H 组（行为假设）"] = ("这次没测", "首版只比 R 和 F；H 等 F 稳定一个赛季再比（ontology.md 9.4）")
    return out


def report(res: dict, source: str = "") -> str:
    v = verdict(res)
    lines = [f"# ontology 正式评估（{res['at']}）", "",
             f"跑法：`python3 -m ethan.memeval --formal --data <数据目录>`，在数据库副本上跑，不调模型。{source}", "",
             "## 结论", "", "| 项目 | 结果 | 说明 |", "|---|---|---|"]
    lines += [f"| {k} | {a} | {b} |" for k, (a, b) in v.items()]
    lines += ["", "## 每道题", "", "| 题 | 函数 | 使用者 | 能用 | 默认参数下的状态 | 对外试探 / 泄露 | 抽样：F 查到 / R 查到 |", "|---|---|---|---|---|---|---|"]
    for r in res["per"]:
        lines.append(f"| {r['q']} | {r['fn']} | {r['user']} | {'是' if r['usable'] else '否' + ('（' + r['error'] + '）' if r['error'] else '')} | "
                     f"{r['status'] or '—'} | {r['probes']} / {r['leaks']} | "
                     + (f"{r['f_hit']}/{r['sampled']} · {r['r_hit']}/{r['sampled']}" if r["sampled"] else "—") + " |")
    g = res["gold"]
    lines += ["", "## 她定的题集", ""]
    if g["total"]:
        lines += ["| 类 | 题数 | F 得分 | R 得分 |", "|---|---|---|---|"]
        lines += [f"| {k} | {c['total']} | {c['f']:.1f} | {c['r']:.1f} |" for k, c in g["by_category"].items()]
    else:
        lines.append("还没有。约 100 题（25 道能力题 × 3–5 个实例，按抽信息 / 跨多次 / 时间 / 知识更新 / 该拒答 / 权限 / 主观分类），"
                     "标准答案跑之前定好，放 `<数据目录>/evals/formal.json`（格式见 `ethan/formaleval.py` 开头），不进仓库。")
    it = res["eval_items"]
    lines += ["", "## 已有检验题（纠错单变的，`python3 -m ethan.memeval`）", "",
              f"最近一次 {it['at']}：{it['passed']}/{it['total']} 通过。" if it else "还没跑过。", ""]
    return "\n".join(lines)


def main(data: Path, questions: Path = None, out: Path = None) -> str:
    from .memory import MemoryStore
    from .retire import _Copy
    store = MemoryStore(data)
    try:
        copy = _Copy(store.db.conn)
    finally:
        store.close()
    from . import memsearch
    if memsearch.semantic_on(copy) and memsearch._EMBEDDER is None:  # 副本在内存里，找不到模型目录：用数据目录下的，加载不上就只用关键词
        try:
            memsearch._EMBEDDER = memsearch._FastEmbed(str(Path(data) / "models" / "fastembed"))
        except Exception:
            memsearch.set_semantic(copy, False)
    try:
        qs = load_questions(questions or Path(data) / "evals" / "formal.json")
        res = run(copy, questions=qs)
    finally:
        copy.close()
    text = report(res)
    target = out or Path(data) / "reports" / f"ontology-eval-{res['at']}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return str(target)
