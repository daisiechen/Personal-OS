"""下个赛季：拆副本草稿、开季、这一季的结算（tasks-and-schedule.md 第 7 节；第 1 件）。

她之前定的：“每个赛季结算的时候……我们一起把下个赛季的副本按照我们的框架拆分一下”；“复盘只在聊天里聊吧，工作台上只看结果，就是汇总和草稿”。

怎么运作：
1. 赛季最后一个周日的复盘聊完（[复盘记录 …] 记下、这次带“赛季”层），后台让 小伴 起草下个赛季：
   名字和起止日、每个副本（目标、截止、管哪些事、接着打哪个老副本）、3–4 个里程碑、每个里程碑的阶段任务（频次、引用哪项日常周常、阶段末复盘）、
   以及哪些老副本暂停。依据：赛季页、这季的副本和里程碑、每周复盘记录、复盘那几天的聊天。
2. 草稿在工作台赛季页“下季草稿”里看，不在页面上改：要改跟 小伴 说，他写 [下季草稿 改 她的意见]，后台按意见重写。
3. 她点“按这个开下个赛季”（点两次确认）：先把这一季存一份结算（往季回顾里一季一张卡），再建副本、里程碑、阶段任务，
   草稿里没接着打的老副本改成暂停（不删，能在副本候选里找回），赛季定义换成新的。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

from . import season, stages

SCHEMA = """
CREATE TABLE IF NOT EXISTS season_drafts (
    id INTEGER PRIMARY KEY, season TEXT NOT NULL, body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
    feedback TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS season_archive (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL, line TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL);
"""
MARK = re.compile(r"\[下季草稿\s+改\s+([^\]]+)\]")
CADENCES = ("once", "daily", "weekly", "per_milestone")
DOMAINS = ("身体", "事业", "财务", "社交", "学习", "学术", "生活体验")
PROMPT = """你是 Mia 的 AI 伙伴 小伴。这个赛季快结束了，你们刚聊完赛季复盘。现在按你们的框架起草下个赛季，给她在工作台上看。
框架：领域（身体、事业、财务、社交、学习、学术、生活体验）→ 副本（这个赛季的目标：目标、截止日、管哪些事）→ 3–4 个里程碑（可检查的阶段结果，带截止日）
→ 每个里程碑的阶段任务（频次：once 一次 / daily 每天 / weekly 每周 N 次 / per_milestone 每阶段一次；可以引用一项日常周常；每个里程碑最后一件是复盘）。
规矩：
- 以复盘聊天里她说的为准（保留、停、新开、硬线）；她没提的，按赛季页和这季的数据提建议。
- 副本少而精，通常 3–5 个；接着打的老副本写 from_goal（老副本编号）。
- 里程碑截止日落在新赛季起止日之内，最后一个就是副本截止日。
- 阶段任务写到能直接排进待办的程度，一件一句。
- 交给 agent 的项目类副本，阶段任务只写要她本人做的（验收、审、定、复盘）。
只回一个 JSON：
{"name": "赛季名，像“2027 春季 · 城市篇”", "start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
 "summary": "两三句：这个赛季想怎么过、和上一季比改了什么",
 "rule": "一条硬线",
 "goals": [{"title": "副本名", "domain": "七个领域之一", "due": "YYYY-MM-DD", "scope": "管哪些事", "from_goal": null 或老副本编号, "agent": false,
            "milestones": [{"title": "M1 ……", "due": "YYYY-MM-DD", "tasks": [{"title": "……", "cadence": "once", "per_week": null, "habit": null 或日常周常名字, "review": false}]}]}],
 "pause": [老副本编号]}"""


def ensure(db):
    db.conn.executescript(SCHEMA)
    if "scope" not in {r[1] for r in db.conn.execute("PRAGMA table_info(goals)")}:  # 管哪些事（migrations/20261102_goal_scope_and_tasks.py 加的）
        db.conn.execute("ALTER TABLE goals ADD COLUMN scope TEXT")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def draft(db, name: str = None):
    """当前这一季的草稿（还没开季的）：{"id", "body", "status", "updated_at"} 或 None。"""
    ensure(db)
    q = "SELECT * FROM season_drafts WHERE status = 'draft'" + (" AND season = ?" if name else "") + " ORDER BY id DESC LIMIT 1"
    r = db.conn.execute(q, (name,) if name else ()).fetchone()
    return {"id": r["id"], "body": json.loads(r["body"]), "status": r["status"], "feedback": r["feedback"], "updated_at": r["updated_at"]} if r else None


def facts(store, settings, day: date) -> dict:
    db = store.db
    s, start = season.current(settings)
    page = season.page(db, s, start, day)
    goals = []
    for g in db.conn.execute("SELECT * FROM goals WHERE level = 'year' AND status IN ('active', 'paused') ORDER BY id"):
        ms = [f"{m['title']}（{(m['due'] or '')[5:]}，{ {'open': '进行中', 'done': '达成', 'missed': '没达成'}[m['status']] }）"
              for m in stages.milestones(db, g["id"])]
        goals.append({"编号": g["id"], "副本": g["title"], "领域": g["domain"], "状态": g["status"], "交给 agent": bool(g["handled_by"]),
                      "管哪些事": g["scope"] if "scope" in g.keys() else None, "里程碑": ms})
    from . import quests, weekreview
    lines = []
    for d in range(4):
        for r in store.raw.read_day((day - timedelta(days=d)).isoformat())[-120:]:
            if r.get("role") in ("mia", "ethan") and r.get("text"):
                lines.append(f"{r['role']}：{' '.join(str(r['text']).split())[:300]}")
    return {"这一季": s["name"], "起止": f"{start.isoformat()} – {s['end']}", "小伴 对这季的结论": page.get("line"),
            "七块领域": [f"{d['name']} 开季 {d.get('start')} → 现在 {d.get('now')}：{d.get('why') or ''}" for d in page.get("domains", [])],
            "下季建议": page.get("next"), "副本": goals,
            "日常周常": [h["title"] for h in quests.habits(db)],
            "每周复盘": [f"{r['day']}：{r['summary']}；调整：{'、'.join(r['changes'])}" for r in weekreview.recent(db, 16) if r["summary"]],
            "最近的聊天（赛季复盘在这里）": lines[-250:]}


def _clean(body: dict, season_end: date) -> dict:
    """把模型写的草稿收拾干净：日期、频次、领域不对的改掉或去掉。"""
    def d(x, default):
        try:
            return date.fromisoformat(str(x)).isoformat()
        except ValueError:
            return default
    start = d(body.get("start"), (season_end + timedelta(days=1)).isoformat())
    end = d(body.get("end"), (date.fromisoformat(start) + timedelta(days=90)).isoformat())
    goals = []
    for g in body.get("goals") or []:
        title = " ".join(str(g.get("title") or "").split())[:60]
        if not title:
            continue
        ms = []
        for m in g.get("milestones") or []:
            mt = " ".join(str(m.get("title") or "").split())[:60]
            if not mt:
                continue
            tasks = []
            for t in m.get("tasks") or []:
                tt = " ".join(str(t.get("title") or "").split())[:80]
                if tt:
                    cad = t.get("cadence") if t.get("cadence") in CADENCES else "once"
                    per = t.get("per_week") if cad == "weekly" and str(t.get("per_week") or "").isdigit() else None
                    tasks.append({"title": tt, "cadence": cad, "per_week": int(per) if per else (1 if cad == "weekly" else None),
                                  "habit": (str(t["habit"]).strip() or None) if t.get("habit") else None, "review": bool(t.get("review"))})
            if not any(t["review"] for t in tasks) and not g.get("agent"):
                tasks.append({"title": f"{mt.split(' ')[0]} 复盘", "cadence": "once", "per_week": None, "habit": None, "review": True})
            ms.append({"title": mt, "due": d(m.get("due"), end), "tasks": tasks})
        goals.append({"title": title, "domain": g.get("domain") if g.get("domain") in DOMAINS else None, "due": d(g.get("due"), end),
                      "scope": str(g.get("scope") or "")[:200], "from_goal": int(g["from_goal"]) if str(g.get("from_goal") or "").isdigit() else None,
                      "agent": bool(g.get("agent")), "milestones": ms})
    return {"name": str(body.get("name") or "下个赛季")[:40], "start": start, "end": end, "summary": str(body.get("summary") or "")[:300],
            "rule": str(body.get("rule") or "")[:120], "goals": goals,
            "pause": [int(x) for x in body.get("pause") or [] if str(x).isdigit()]}


def write(store, router, day: date, feedback: str = None) -> dict:
    """起草（或按她的意见重写）下个赛季，存进 season_drafts。"""
    db = store.db
    ensure(db)
    s, _ = season.current(router.settings)
    f = facts(store, router.settings, day)
    old = draft(db, s["name"])
    ask = "（起草下个赛季）"
    if old and feedback:
        ask = "上一版草稿：\n" + json.dumps(old["body"], ensure_ascii=False) + "\n\n她的意见：" + feedback + "\n（按她的意见改，没提的地方别动）"
    reply = router.complete(PROMPT + "\n\n### 事实\n" + json.dumps(f, ensure_ascii=False, indent=1, default=str),
                            [{"role": "user", "content": ask}], purpose="next-season")
    text = reply.text
    body = _clean(json.loads(text[text.find("{"): text.rfind("}") + 1]), date.fromisoformat(s["end"]))
    if old:
        db.conn.execute("UPDATE season_drafts SET body = ?, feedback = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(body, ensure_ascii=False), feedback or old.get("feedback"), _now(), old["id"]))
    else:
        cur = db.conn.execute("INSERT INTO season_drafts (season, body, created_at, updated_at) VALUES (?, ?, ?, ?)",
                              (s["name"], json.dumps(body, ensure_ascii=False), _now(), _now()))
        from . import transitions
        transitions.record(db, "season_draft", cur.lastrowid, None, "draft", {})
    db.conn.commit()
    return body


def note(db, text: str) -> list:
    """小伴 在聊天里写了 [下季草稿 改 她的意见]：记下，返回意见（由调用的地方去后台重写）。"""
    return [m.strip() for m in MARK.findall(text) if m.strip()]


def strip(text: str) -> str:
    return MARK.sub("", text)


def chat_text(d: dict) -> str:
    b = d["body"]
    return ("### 下个赛季草稿（你写好的，在工作台赛季页“下季草稿”里给她看）\n"
            f"{b['name']}（{b['start']} – {b['end']}）：" + "；".join(g["title"] for g in b["goals"])
            + "。她要改什么，照她的话在回复末尾写 [下季草稿 改 她的意见]，后台会按意见重写；她点了“按这个开下个赛季”才会建好。")


def kick(root, feedback: str = None, spawn=None) -> bool:
    """后台起草 / 重写（不卡住聊天）。"""
    from .bgwork import kick as _kick
    return bool(_kick(root, "next-season", ["--feedback", feedback] if feedback else [], spawn))


# ---------- 结算和开季 ----------
def settle(db, settings, day: date) -> int:
    """把这一季存一份结算（往季回顾里一季一张卡）。"""
    ensure(db)
    s, start = season.current(settings)
    if db.conn.execute("SELECT 1 FROM season_archive WHERE name = ?", (s["name"],)).fetchone():
        return 0
    page = season.page(db, s, start, day)
    from . import weekreview
    bosses = []
    for g in db.conn.execute("SELECT * FROM goals WHERE level = 'year' AND status = 'active' ORDER BY id"):
        ms = stages.milestones(db, g["id"])
        bosses.append({"title": g["title"], "done": sum(m["status"] == "done" for m in ms), "total": len(ms)})
    body = {"domains": [{"name": d["name"], "start": d.get("start"), "now": d.get("now"), "why": d.get("why")} for d in page.get("domains", [])],
            "bosses": bosses, "next": page.get("next"), "reviews": [r for r in weekreview.recent(db, 20) if r["summary"]]}
    cur = db.conn.execute("INSERT INTO season_archive (name, start, end, line, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                          (s["name"], start.isoformat(), s["end"], page.get("line"), json.dumps(body, ensure_ascii=False), _now()))
    db.conn.commit()
    return cur.lastrowid


def open_next(db, settings, day: date) -> dict:
    """她点了“按这个开下个赛季”：结算这一季、建副本、里程碑、阶段任务，没接着打的老副本暂停，换赛季定义。"""
    from . import quests, transitions
    s, _ = season.current(settings)
    d = draft(db, s["name"])
    if not d:
        raise ValueError("还没有下个赛季的草稿")
    b = d["body"]
    ensure(db)
    settle(db, settings, day)
    habits = {h["title"]: h["id"] for h in quests.habits(db)}
    keep, made = set(), []
    for g in b["goals"]:
        if g.get("from_goal") and db.conn.execute("SELECT 1 FROM goals WHERE id = ?", (g["from_goal"],)).fetchone():
            gid = g["from_goal"]
            old_st = db.conn.execute("SELECT status FROM goals WHERE id = ?", (gid,)).fetchone()[0]
            if old_st != "active":
                transitions.record(db, "goal", gid, old_st, "active", {"by": "开季"})
            db.conn.execute("UPDATE goals SET status = 'active', due = ?, domain = COALESCE(?, domain), scope = ? WHERE id = ?",
                            (g["due"], g["domain"], g["scope"] or None, gid))
            # 老副本还开着的里程碑：上个赛季的，算没达成收掉，换成新的
            db.conn.execute("UPDATE milestones SET status = 'missed', closed_at = ? WHERE goal_id = ? AND status = 'open'", (_now(), gid))
            keep.add(gid)
        else:
            gid = db.add_goal(g["title"], "year", status="active", due=g["due"], domain=g["domain"])
            transitions.record(db, "goal", gid, None, "active", {"by": "开季"})
            if g["scope"]:
                db.conn.execute("UPDATE goals SET scope = ? WHERE id = ?", (g["scope"], gid))
            if g.get("agent"):
                db.conn.execute("UPDATE goals SET handled_by = 'agent' WHERE id = ?", (gid,))
        for m in g["milestones"]:
            mid = stages.add_milestone(db, gid, m["title"], m["due"])
            for t in m["tasks"]:
                hid = next((v for k, v in habits.items() if t.get("habit") and (k == t["habit"] or t["habit"] in k)), None)
                stages.add_task(db, mid, t["title"], t["cadence"], t.get("per_week"), habit_id=hid, review=t["review"])
        made.append(gid)
    paused = []
    for r in db.conn.execute("SELECT id FROM goals WHERE level = 'year' AND status = 'active'").fetchall():
        if r["id"] not in keep and r["id"] not in made:
            db.conn.execute("UPDATE goals SET status = 'paused' WHERE id = ?", (r["id"],))
            transitions.record(db, "goal", r["id"], "active", "paused", {"by": "开季"})
            paused.append(r["id"])
    db.conn.execute("UPDATE season_drafts SET status = 'opened', updated_at = ? WHERE id = ?", (_now(), d["id"]))
    transitions.record(db, "season_draft", d["id"], "draft", "opened", {"made": made, "paused": paused})
    db.conn.commit()
    settings.set("season", {"name": b["name"], "start": b["start"], "end": b["end"]})
    return {"made": made, "paused": paused, "season": b["name"]}


def archive(db) -> list:
    """往季回顾：一季一张卡。"""
    ensure(db)
    out = []
    for r in db.conn.execute("SELECT * FROM season_archive ORDER BY end DESC"):
        body = json.loads(r["body"])
        out.append({"name": r["name"], "start": r["start"], "end": r["end"], "line": r["line"] or "",
                    "bosses": body.get("bosses", []), "domains": body.get("domains", [])})
    return out


def main():
    import argparse
    from pathlib import Path
    from .cli import build_router
    from .clock import her_now
    from .memory import MemoryStore
    p = argparse.ArgumentParser(description="让 小伴 起草（或按意见重写）下个赛季")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--feedback")
    a = p.parse_args()
    store = MemoryStore(a.data)
    try:
        router = build_router(a.data, 50.0)
        body = write(store, router, her_now(router.settings).date(), a.feedback)
        print(f"{body['name']}：{len(body['goals'])} 个副本")
    finally:
        store.close()


if __name__ == "__main__":
    main()
