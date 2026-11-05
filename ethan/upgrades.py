"""闭环跑起来（ontology-plan 阶段 4）：升级记录（4.1）和能力题自动收候选（4.2）。

4.1 升级记录：每次升级（接了一个来源、拆了属性、修了检索、改了规则）记一条，带前后两周的数字：
    说法条数（关于她、还算数的）、按题占比（按题答 / (按题答 + 兜底检索)）、答不上次数（能力题返回缺失）、纠错数。
    前两周的数字记的时候就算；后两周的留空，满两周后每周复盘开场时自动补（fill）。
    “我”页 metrics 的 recent_upgrades 给最近 3 条；没起作用的规则（后两周没变好）在“我”页上看得出来，降级由开发会话按这个定。
4.2 候选题：同类“查不到”2 周内重复 3 次 → 记一道候选题（question_candidates），月度复盘时她过一遍。
    两种“查不到”：① 兜底检索（不经题函数）一条都没找到——按问句的字片段归类（重合一半以上算同类）；
    ② 某道能力题返回缺失（unknown）——按题归类。都不算系统维护自己查的。
只记数字和问句，不动她的记忆；候选题收不收由她定，收了再由开发会话做成题函数（capability-questions.md）。
"""

import json
from datetime import date, datetime, timedelta, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS upgrades (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, what TEXT NOT NULL, kind TEXT NOT NULL, note TEXT,
    before TEXT NOT NULL, after TEXT, filled_at TEXT, created_at TEXT NOT NULL, UNIQUE (at, what));
CREATE TABLE IF NOT EXISTS question_candidates (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, toks TEXT NOT NULL DEFAULT '[]', samples TEXT NOT NULL DEFAULT '[]',
    n INTEGER NOT NULL, first_at TEXT NOT NULL, last_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL, decided_at TEXT, note TEXT);
"""
KINDS = {"source": "接来源", "property": "拆属性", "retrieval": "修检索", "rule": "规则"}
WINDOW = 14  # 天：前两周、后两周
CAND_WINDOW, CAND_MIN, SAME = 14, 3, 0.5  # 2 周内同类 3 次；字片段重合一半以上算同类
INTERNAL = ("system",)
NUMBERS = (("statements", "说法条数"), ("share", "按题占比"), ("missed", "答不上次数"), ("corrections", "纠错数"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.executescript(SCHEMA)


def _iso(d: date) -> str:
    return datetime.combine(d, datetime.min.time(), timezone.utc).isoformat()


def _count(db, sql, args) -> int:
    try:
        return db.conn.execute(sql, args).fetchone()[0] or 0
    except Exception:  # 表还没建：当 0
        return 0


def snapshot(db, start: date, end: date) -> dict:
    """[start, end) 这段时间的四个数。说法条数是 end 那一刻还算数的关于她的说法。"""
    a, b = _iso(start), _iso(end)
    from . import ontology
    try:
        sid = ontology.self_id(db)
    except Exception:
        sid = None
    statements = _count(db, "SELECT COUNT(*) FROM statements WHERE subject_id = ? AND review IN ('accepted', 'disputed') AND recorded_at < ? "
                            "AND (retracted_at IS NULL OR retracted_at >= ?)", (sid, b, b)) if sid else 0
    by_q = _count(db, "SELECT COUNT(*) FROM function_log WHERE at >= ? AND at < ? AND user NOT IN ('system')", (a, b))
    fallback = _count(db, "SELECT COUNT(*) FROM retrieval_log WHERE at >= ? AND at < ? AND COALESCE(user, '') != 'system' AND via IS NULL", (a, b))
    missed = _count(db, "SELECT COUNT(*) FROM function_log WHERE at >= ? AND at < ? AND user NOT IN ('system') AND status IN ('unknown', 'unavailable')", (a, b))
    fixes = _count(db, "SELECT COUNT(*) FROM corrections WHERE at >= ? AND at < ? AND undone_at IS NULL", (a, b))
    return {"from": start.isoformat(), "to": end.isoformat(), "statements": statements,
            "share": round(by_q / (by_q + fallback), 2) if by_q + fallback else None, "by_question": by_q, "fallback": fallback,
            "missed": missed, "corrections": fixes}


def log_upgrade(db, at, what: str, kind: str, note: str = "", today: date = None) -> int:
    """记一次升级。at：哪天上线的（date 或 'YYYY-MM-DD'）；kind：source / property / retrieval / rule。
    同一天同一件只记一次（返回已有的编号）。前两周的数字现在算；后两周满了再补（today 已过两周就当场补上）。"""
    if kind not in KINDS:
        raise ValueError(f"kind 只能是 {'、'.join(KINDS)}")
    ensure(db)
    day = at if isinstance(at, date) else date.fromisoformat(str(at)[:10])
    have = db.conn.execute("SELECT id FROM upgrades WHERE at = ? AND what = ?", (day.isoformat(), what)).fetchone()
    if have:
        return have[0]
    before = snapshot(db, day - timedelta(days=WINDOW), day)
    cur = db.conn.execute("INSERT INTO upgrades (at, what, kind, note, before, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (day.isoformat(), what, kind, note, json.dumps(before, ensure_ascii=False), _now()))
    db.conn.commit()
    if today:
        fill(db, today)
    return cur.lastrowid


def fill(db, today: date) -> int:
    """满两周的升级补上后两周的数字（每周复盘开场时调）。返回补了几条。"""
    ensure(db)
    n = 0
    for r in db.conn.execute("SELECT id, at FROM upgrades WHERE after IS NULL").fetchall():
        day = date.fromisoformat(r["at"])
        if day + timedelta(days=WINDOW) <= today:
            after = snapshot(db, day, day + timedelta(days=WINDOW))
            db.conn.execute("UPDATE upgrades SET after = ?, filled_at = ? WHERE id = ?", (json.dumps(after, ensure_ascii=False), _now(), r["id"]))
            n += 1
    if n:
        db.conn.commit()
    return n


def recent(db, n: int = 3) -> list:
    """“我”页“最近升级”：最近几次升级和前后数字（后两周没满的 after 是 None）。"""
    try:
        rows = db.conn.execute("SELECT * FROM upgrades ORDER BY at DESC, id DESC LIMIT ?", (n,)).fetchall()
    except Exception:
        return []
    out = []
    for r in rows:
        before = json.loads(r["before"])
        after = json.loads(r["after"]) if r["after"] else None
        out.append({"id": r["id"], "at": r["at"], "what": r["what"], "kind": KINDS.get(r["kind"], r["kind"]), "note": r["note"] or "",
                    "before": before, "after": after,
                    "numbers": [{"key": k, "name": name, "before": before.get(k), "after": after.get(k) if after else None} for k, name in NUMBERS]})
    return out


# ---------- 4.2 候选题 ----------
def _toks(queries) -> set:
    from .memsearch import tokens
    return set(tokens(" ".join(q for q in queries if q), query=True))


def _same(a: set, b: set) -> bool:
    return bool(a and b) and len(a & b) / min(len(a), len(b)) >= SAME


def collect(db, today: date) -> dict:
    """看最近 2 周的“查不到”，同类 3 次以上的记成候选题（已有的更新次数和最近时间）。返回 {新增, 更新}。"""
    ensure(db)
    since, until = _iso(today - timedelta(days=CAND_WINDOW)), _iso(today + timedelta(days=1))
    groups = []  # [(toks, [(at, 问句)])]
    try:
        rows = db.conn.execute("SELECT at, queries FROM retrieval_log WHERE at >= ? AND at < ? AND via IS NULL AND keys = '[]' "
                               "AND COALESCE(user, '') NOT IN ('system') ORDER BY at", (since, until)).fetchall()
    except Exception:
        rows = []
    for r in rows:
        qs = [q for q in json.loads(r["queries"] or "[]") if isinstance(q, str)]
        t = _toks(qs)
        if not t:
            continue
        g = next((g for g in groups if _same(g[0], t)), None)
        if g is None:
            groups.append((t, [(r["at"], "；".join(qs)[:80])]))
        else:
            g[1].append((r["at"], "；".join(qs)[:80]))
    found = [("fallback", t, hits) for t, hits in groups if len(hits) >= CAND_MIN]
    try:
        from . import onto_query
        for r in db.conn.execute("SELECT fn, COUNT(*) n, MIN(at) a, MAX(at) b FROM function_log WHERE at >= ? AND at < ? AND status = 'unknown' "
                                 "AND user NOT IN ('system') GROUP BY fn HAVING COUNT(*) >= ?", (since, until, CAND_MIN)):
            q = onto_query.QUESTION_OF.get(r["fn"])
            found.append(("missing", {f"q{q or r['fn']}"}, [(r["a"], f"第 {q} 题（{r['fn']}）答不上")] * (r["n"] - 1) + [(r["b"], f"第 {q} 题（{r['fn']}）答不上")]))
    except Exception:
        pass
    got = {"added": 0, "updated": 0}
    have = [dict(r) for r in db.conn.execute("SELECT * FROM question_candidates")]
    for kind, toks, hits in found:
        samples = list(dict.fromkeys(s for _, s in hits))[:3]
        first, last = min(a for a, _ in hits), max(a for a, _ in hits)
        old = next((h for h in have if h["kind"] == kind and _same(set(json.loads(h["toks"])), toks)), None)
        if old:
            if old["status"] != "open" or last <= old["last_at"]:
                continue  # 她定过的不再翻出来；没有新的不重复记
            db.conn.execute("UPDATE question_candidates SET n = ?, last_at = ?, samples = ? WHERE id = ?",
                            (max(old["n"], len(hits)), last, json.dumps(samples, ensure_ascii=False), old["id"]))
            got["updated"] += 1
        else:
            key = ("fb:" if kind == "fallback" else "") + "|".join(sorted(toks))[:120]
            db.conn.execute("INSERT OR IGNORE INTO question_candidates (key, kind, toks, samples, n, first_at, last_at, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (key, kind, json.dumps(sorted(toks), ensure_ascii=False),
                                                                json.dumps(samples, ensure_ascii=False), len(hits), first, last, _now()))
            got["added"] += 1
    db.conn.commit()
    return got


def candidates(db, status: str = "open") -> list:
    try:
        rows = db.conn.execute("SELECT * FROM question_candidates WHERE status = ? ORDER BY n DESC, last_at DESC", (status,)).fetchall()
    except Exception:
        return []
    return [{"id": r["id"], "kind": "兜底检索查不到" if r["kind"] == "fallback" else "能力题答不上", "n": r["n"],
             "samples": json.loads(r["samples"]), "last": r["last_at"][:10]} for r in rows]


def candidates_text(db) -> str:
    """月度复盘“候选题”一节的底稿：每道一行，带次数和例句。没有就照说。"""
    items = candidates(db)
    if not items:
        return "这个月没有攒够次数的候选题（同类查不到 2 周内 3 次才算）。"
    return "\n".join(f"#{c['id']} {c['kind']} {c['n']} 次（最近 {c['last']}）：" + " / ".join(c["samples"]) for c in items[:8])


def decide(db, cid: int, status: str, note: str = "") -> bool:
    """她过候选题：accepted（收进能力题，交开发会话做函数）/ rejected（不要）。"""
    if status not in ("accepted", "rejected"):
        raise ValueError("status 只能是 accepted / rejected")
    ensure(db)
    n = db.conn.execute("UPDATE question_candidates SET status = ?, decided_at = ?, note = ? WHERE id = ? AND status = 'open'",
                        (status, _now(), note, int(cid))).rowcount
    db.conn.commit()
    return bool(n)


def main():
    import argparse
    from pathlib import Path
    from .clock import her_today
    from .memory import MemoryStore
    p = argparse.ArgumentParser(description="升级记录和候选题（ontology-plan 4.1、4.2）")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--fill", action="store_true", help="补满两周的升级的后两周数字")
    p.add_argument("--collect", action="store_true", help="收一遍候选题")
    p.add_argument("--decide", nargs=2, metavar=("编号", "accepted/rejected"))
    a = p.parse_args()
    store = MemoryStore(a.data)
    try:
        today = her_today()
        if a.fill:
            print(json.dumps({"filled": fill(store.db, today)}), flush=True)
        if a.collect:
            print(json.dumps(collect(store.db, today)), flush=True)
        if a.decide:
            print(json.dumps({"ok": decide(store.db, int(a.decide[0]), a.decide[1], "命令行")}), flush=True)
        print(json.dumps({"recent_upgrades": recent(store.db), "candidates": candidates(store.db)}, ensure_ascii=False, indent=1), flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
