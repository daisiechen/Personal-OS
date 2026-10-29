"""Agent 任务队列和成绩单（2026-10-28 她同意，替代 Mia-Agent-OS 里 Codex 那套 Task Board 的重 PRD）。

谁派活、谁干活、谁验收都记在同一张表 agent_tasks 里，工作台“团队”页和成绩单都从这里算：
  queued 排队 → running 在干 → review 等审查 → accepted 通过 / 打回重新 queued（返工 +1）
               → failed 干不了（报错、验收命令跑不过、超时，由系统判定）
               → dropped 她叫停（“不干了”，比如 DemoApp 不让 Codex 做了），不算失败、不进成绩单
Codex 的活由 ethan/codex_runner.py 定时领；Claude、Grok、小伴 的活也可以直接记一笔已完成的，一起算分。

打分（她同意的算法）：派活时定大小 1/2/3/5/8 点（干活的人自己不定）。每件通过的活四项 0–1 分：
  质量 40%：一次通过 1，每返工一次减半；成本 20% 和速度 20%：同大小的活的中位数 ÷ 这件，夹在 0.5–1.5 再除以 1.5；
  她的评价 20%：👍 1，没评 0.5，👎 0。单件得分 = 大小 × 加权和。
每个 agent 每周两个数：产出分 = 单件得分之和；效率分 = 产出分 ÷ 通过的总大小 × 100。
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

SIZES = (1, 2, 3, 5, 8)
AGENTS = ("codex", "claude", "grok", "ethan")
WEIGHTS = {"quality": 0.4, "cost": 0.2, "speed": 0.2, "rating": 0.2}

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    id INTEGER PRIMARY KEY,
    agent TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL,
    spec TEXT NOT NULL DEFAULT '',
    verify TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'queued',
    reworks INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL DEFAULT 'claude',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    reviewed_at TEXT,
    retry_after TEXT,
    base_sha TEXT,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    work_seconds REAL NOT NULL DEFAULT 0,
    verify_ok INTEGER,
    patch TEXT NOT NULL DEFAULT '',
    log TEXT NOT NULL DEFAULT '',
    review_note TEXT NOT NULL DEFAULT '',
    rating INTEGER
);
"""
LIST_FIELDS = ("id", "agent", "project", "title", "size", "status", "reworks", "created_at", "finished_at",
               "reviewed_at", "verify_ok", "tokens_in", "tokens_out", "cost_usd", "work_seconds", "rating")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure(db):
    db.conn.executescript(SCHEMA)


def add(db, agent: str, title: str, spec: str = "", verify: str = "", size: int = 1, project: str = "",
        repo: str = "", created_by: str = "claude") -> int:
    ensure(db)
    if agent not in AGENTS:
        raise ValueError(f"不认识的 agent：{agent}")
    if size not in SIZES:
        raise ValueError(f"大小只能是 {SIZES}")
    if not title.strip():
        raise ValueError("缺标题")
    cur = db.conn.execute(
        "INSERT INTO agent_tasks (agent, project, title, spec, verify, repo, size, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (agent, project, title.strip(), spec, verify, repo, size, created_by, now()))
    db.conn.commit()
    return cur.lastrowid


def record_done(db, agent: str, title: str, size: int, project: str = "", tokens_in: int = 0, tokens_out: int = 0,
                cost_usd: float = 0, work_seconds: float = 0, reworks: int = 0, note: str = "") -> int:
    """直接记一件已经做完、验收过的活（Claude、Grok、小伴 自己的活用）。"""
    tid = add(db, agent, title, size=size, project=project, created_by=agent)
    t = now()
    db.conn.execute("UPDATE agent_tasks SET status = 'accepted', started_at = ?, finished_at = ?, reviewed_at = ?, "
                    "tokens_in = ?, tokens_out = ?, cost_usd = ?, work_seconds = ?, reworks = ?, review_note = ? "
                    "WHERE id = ?", (t, t, t, tokens_in, tokens_out, cost_usd, work_seconds, reworks, note, tid))
    db.conn.commit()
    return tid


def start(db, agent: str, title: str, size: int = 1, project: str = "", spec: str = "") -> int:
    """Claude、Grok、小伴 自己开工的活：直接记成“在干”，工作台团队页马上看得到（2026-10-28 她说我在干活看板上却没显示）。"""
    tid = add(db, agent, title, spec=spec, size=size, project=project, created_by=agent)
    update(db, tid, status="running", started_at=now())
    return tid


def finish(db, tid: int, status: str = "accepted", note: str = "", tokens_in: int = 0, tokens_out: int = 0,
           cost_usd: float = 0) -> dict:
    """自己的活做完：accepted（测试过了、已上线）或 review（等她看，比如设计稿）；用时从开工算起。"""
    t = get(db, tid)
    if t is None:
        raise ValueError("没有这件活")
    if t["status"] not in ("running", "review"):
        raise ValueError(f"这件活现在是 {t['status']}，不能收尾")
    if status not in ("accepted", "review"):
        raise ValueError("收尾状态只能是 accepted 或 review")
    t_now = now()
    began = datetime.fromisoformat(t["started_at"]) if t["started_at"] else datetime.now(timezone.utc)
    fields = dict(status=status, finished_at=t["finished_at"] or t_now, review_note=note or t["review_note"],
                  tokens_in=t["tokens_in"] + tokens_in, tokens_out=t["tokens_out"] + tokens_out,
                  cost_usd=t["cost_usd"] + cost_usd)
    if t["status"] == "running":
        fields["work_seconds"] = (datetime.now(timezone.utc) - began).total_seconds()
    if status == "accepted":
        fields["reviewed_at"] = t_now
    update(db, tid, **fields)
    return get(db, tid)


def get(db, tid: int) -> dict:
    ensure(db)
    r = db.conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (tid,)).fetchone()
    return dict(r) if r else None


def listing(db, status: str = None, agent: str = None, limit: int = 50) -> list:
    ensure(db)
    where, args = [], []
    if status:
        where.append("status = ?")
        args.append(status)
    if agent:
        where.append("agent = ?")
        args.append(agent)
    sql = f"SELECT {', '.join(LIST_FIELDS)} FROM agent_tasks" + (" WHERE " + " AND ".join(where) if where else "")
    return [dict(r) for r in db.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit))]


def next_queued(db, agent: str, at: str = None):
    """下一件可以开工的活（额度用完时设了 retry_after 的，到点才领）。"""
    ensure(db)
    at = at or now()
    r = db.conn.execute("SELECT * FROM agent_tasks WHERE agent = ? AND status = 'queued' "
                        "AND (retry_after IS NULL OR retry_after <= ?) ORDER BY id LIMIT 1", (agent, at)).fetchone()
    return dict(r) if r else None


def update(db, tid: int, **fields):
    sets = ", ".join(f"{k} = ?" for k in fields)
    db.conn.execute(f"UPDATE agent_tasks SET {sets} WHERE id = ?", (*fields.values(), tid))
    db.conn.commit()


def review(db, tid: int, decision: str, note: str = "") -> dict:
    """审查结果：accept 通过；reject 打回重做（返工 +1，审查意见附在要求后面）。"""
    t = get(db, tid)
    if t is None:
        raise ValueError("没有这件活")
    if t["status"] not in ("review", "failed"):
        raise ValueError(f"这件活现在是 {t['status']}，不能审查")
    if decision == "accept":
        update(db, tid, status="accepted", reviewed_at=now(), review_note=note)
    elif decision == "reject":
        spec = t["spec"] + (f"\n\n【第 {t['reworks'] + 1} 次返工，审查意见】{note}" if note else "")
        update(db, tid, status="queued", reworks=t["reworks"] + 1, spec=spec, review_note=note, patch="",
               verify_ok=None, retry_after=None)
    else:
        raise ValueError("decision 只能是 accept 或 reject")
    return get(db, tid)


def retry(db, tid: int) -> dict:
    """失败的活重新排队，不记返工（环境出错、要求写得不清楚这种不怪干活的人时用）。"""
    t = get(db, tid)
    if t is None:
        raise ValueError("没有这件活")
    if t["status"] != "failed":
        raise ValueError(f"这件活现在是 {t['status']}，不用重试")
    update(db, tid, status="queued", retry_after=None, patch="", verify_ok=None, started_at=None, finished_at=None)
    return get(db, tid)


def drop(db, tid: int, note: str = "") -> dict:
    """她叫停一件活（2026-10-29：失败只由系统判，她主动不要的叫“放弃”，不扣分）。"""
    t = get(db, tid)
    if t is None:
        raise ValueError("没有这件活")
    if t["status"] in ("accepted", "dropped"):
        raise ValueError("这件活已经结束了")
    update(db, tid, status="dropped", finished_at=now(), review_note=note or "她叫停了")
    return get(db, tid)


def report_title(text: str) -> str:
    """Grok 回报的标题（2026-10-29 她说团队卡片写得太随便、看不出是哪件活）：取第一行像样的话，去掉 Markdown 和记号。"""
    for line in str(text or "").splitlines():
        line = re.sub(r"^[#>*\-\s]+|\*\*|【[^】]*】", "", line).strip(" ：:")
        if len(line) >= 4:
            return line[:40] + ("…" if len(line) > 40 else "")
    return "Grok 回报"


def grok_live(root, hours: int = 48) -> dict:
    """Grok 的活：小伴 派出去的（grok-tasks.jsonl）和 Grok 回报到收件箱的（/grok/result）按先后配对，
    没等到回报的算“在干”；Grok 自己回报、没人派的（比如收藏日报、她在 Grok 里直接交代的）算“刚做完”。"""
    root = Path(root)
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    sent, back = [], []
    try:
        for line in (root / "grok-tasks.jsonl").read_text(encoding="utf-8").split("\n"):  # 只按 \n 分：正文里的 U+2028 不是换行
            if not line.strip():
                continue
            e = json.loads(line)
            if e.get("ok") and e.get("at", "") >= since:
                sent.append(e)
    except (OSError, ValueError):
        pass
    for p in sorted((root / "inbox").glob("*.jsonl")):
        if p.stem < since[:10]:
            continue
        for line in p.read_text(encoding="utf-8").split("\n"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("path") == "/grok/result" and e.get("at", "") >= since:
                back.append(e)
    sent.sort(key=lambda e: e["at"]), back.sort(key=lambda e: e.get("at", ""))
    # 按记号配对（2026-10-29 她说 Grok 在干活看板上没显示：以前谁先回来就算哪件做完，收藏日报一来就把在干的抵掉了）：
    # 【回执 G…】对 小伴 派的那件，【准备 #编号】对准备员那件；没记号的旧任务才按先后配，收藏日报、英语陪练、开发会话的回报不抵。
    waiting, done, started = list(sent), [], []
    own = ("【收藏日报】", "【英语陪练】", "【Claude 会话】", "【惊喜灵感", "【连通测试")
    for b in back:
        text = str(b.get("text", "")).strip()
        if text.startswith("【开工】"):  # Grok 自己报的开工（她在 Grok 里直接交代的活）
            started.append({"title": " ".join(text[4:].split())[:60] or "Grok 在干活", "at": b.get("at", "")})
            continue
        ref = re.search(r"【回执\s*(G\d+)】", text[:80])
        prep_m = re.match(r"【准备 #\d+】", text)
        if ref:
            match = next((s for s in waiting if s.get("ref") == ref.group(1)), None)
        elif prep_m:
            match = next((s for s in waiting if str(s.get("task", "")).startswith(prep_m.group(0))), None)
        elif text.startswith(own):
            match = None
        else:
            match = next((s for s in waiting if not s.get("ref") and not str(s.get("task", "")).startswith("【准备 #")
                          and s["at"] <= b.get("at", "")), None)
        if match:
            waiting.remove(match)
        elif not (ref or prep_m or text.startswith(own)):
            started = started[1:]  # 没配上派出去的活，就是她直接交代的那件做完了（先开工的先完）
        body = re.sub(r"^\s*(【回执\s*G\d+】|【来源[:：][^】]*】|\s)+", "", text)
        done.append({"title": report_title(body), "at": b.get("at", ""), "asked": bool(match),
                     "task": str(match.get("task", ""))[:200] if match else "", "text": body[:3000]})
    cut = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat(timespec="seconds")
    waiting += [{"task": s["title"], "at": s["at"], "self": True} for s in started if s["at"] >= cut]
    title = lambda s: " ".join(str(s.get("task", "")).split())[:60]
    return {"running": [{"title": title(s), "at": s["at"], "task": str(s.get("task", ""))[:800], "self": bool(s.get("self"))} for s in waiting],
            "done": list(reversed(done))[:10]}


SELF_STALE_HOURS = 12  # Claude、Grok、小伴 自己开工的活，开工 12 小时还没收尾就算没干成（2026-10-29 自查：以前会一直挂“在干”）


def expire_self_started(db, at: datetime = None) -> list:
    ensure(db)
    cut = ((at or datetime.now(timezone.utc)) - timedelta(hours=SELF_STALE_HOURS)).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT id FROM agent_tasks WHERE agent != 'codex' AND status = 'running' AND started_at < ?", (cut,)).fetchall()
    for r in rows:
        update(db, r["id"], status="failed", finished_at=now(),
               review_note=f"开工 {SELF_STALE_HOURS} 小时没收尾，系统判没干成")
    return [r["id"] for r in rows]


DEFAULT_RATING, RATE_DAYS = 3, 7  # 2026-10-29 她定：1–5 分，通过 7 天还没打分的默认 3 分


def rate(db, tid: int, rating: int):
    if rating not in range(0, 6):
        raise ValueError("评分只能是 1–5，0 是清掉")
    update(db, tid, rating=rating or None)


def auto_rate(db, now: datetime = None) -> int:
    """通过满 7 天她还没打分的，记默认 3 分（进归档）；返回记了几件。"""
    ensure(db)
    cut = ((now or datetime.now(timezone.utc)) - timedelta(days=RATE_DAYS)).isoformat(timespec="seconds")
    n = db.conn.execute("UPDATE agent_tasks SET rating = ? WHERE status = 'accepted' AND rating IS NULL "
                        "AND COALESCE(reviewed_at, finished_at) < ?", (DEFAULT_RATING, cut)).rowcount
    db.conn.commit()
    return n


# ---------- 成绩单 ----------
def _ratio(base: float, value: float) -> float:
    if not value or not base:  # 没有数据就按和中位数持平算
        return 1 / 1.5
    return min(1.5, max(0.5, base / value)) / 1.5


def task_score(task: dict, peers: list) -> dict:
    """peers：同大小、已通过的活（含它自己），用来算成本和速度的中位数。"""
    costs = [p["cost_usd"] or (p["tokens_in"] + p["tokens_out"]) for p in peers]
    times = [p["work_seconds"] for p in peers]
    cost = task["cost_usd"] or (task["tokens_in"] + task["tokens_out"])
    parts = {
        "quality": 0.5 ** task["reworks"],
        "cost": _ratio(median(costs) if costs else 0, cost),
        "speed": _ratio(median(times) if times else 0, task["work_seconds"]),
        "rating": 0.5 if not task["rating"] else (min(5, max(1, task["rating"])) - 1) / 4,  # 1–5 分折成 0–1，没打分算中间
    }
    score = task["size"] * sum(WEIGHTS[k] * v for k, v in parts.items())
    return {"parts": parts, "score": round(score, 2)}


def scoreboard(db, since: datetime = None, until: datetime = None) -> list:
    """每个 agent 在这段时间（按通过时间）的产出分和效率分，默认最近 7 天。"""
    ensure(db)
    until = until or datetime.now(timezone.utc)
    since = since or until - timedelta(days=7)
    done = [dict(r) for r in db.conn.execute(
        "SELECT * FROM agent_tasks WHERE status = 'accepted' AND reviewed_at >= ? AND reviewed_at <= ?",
        (since.isoformat(timespec="seconds"), until.isoformat(timespec="seconds")))]
    allp = [dict(r) for r in db.conn.execute("SELECT * FROM agent_tasks WHERE status = 'accepted'")]
    out = []
    for agent in AGENTS:
        mine = [t for t in done if t["agent"] == agent]
        rows = [task_score(t, [p for p in allp if p["size"] == t["size"]]) for t in mine]
        size = sum(t["size"] for t in mine)
        output = round(sum(r["score"] for r in rows), 2)
        out.append({
            "agent": agent, "done": len(mine), "size": size, "output": output,
            "efficiency": round(100 * output / size) if size else None,
            "first_pass": round(100 * sum(1 for t in mine if t["reworks"] == 0) / len(mine)) if mine else None,
            "reworks": sum(t["reworks"] for t in mine),
            "cost_usd": round(sum(t["cost_usd"] for t in mine), 2),
            "tokens": sum(t["tokens_in"] + t["tokens_out"] for t in mine),
            "hours": round(sum(t["work_seconds"] for t in mine) / 3600, 1),
            "failed": db.conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE agent = ? AND status = 'failed' "
                                      "AND finished_at >= ?", (agent, since.isoformat(timespec="seconds"))).fetchone()[0],
            "open": db.conn.execute("SELECT COUNT(*) FROM agent_tasks WHERE agent = ? AND status IN "
                                    "('queued', 'running', 'review')", (agent,)).fetchone()[0],
        })
    return out


# ---------- Claude 会话快照（2026-10-28 她要团队页也看到 Claude 里别的会话） ----------
# 两个来源：① 钩子实时报到（2026-10-28 她选的，Mac、服务器、各仓库的云端会话都装了 Claude Code 钩子），按会话编号记在
# claude-hooks.json；② 云端会话手动读一次会话列表推来的快照（claude-sessions.json，整份覆盖）。有钩子数据就只显示钩子的。
SESSIONS_FILE = "claude-sessions.json"
SESSION_STATES = {"working": "在干", "blocked": "等你", "review_ready": "等你看", "completed": "空闲", "failed": "出错",
                  "idle": "空闲"}
SESSION_WHERE = {"mac": "Mac", "cloud": "云端", "server": "服务器", "seoul": "另一台服务器"}
HOOKS_FILE = "claude-hooks.json"
HOOK_STATE = {"SessionStart": "completed", "UserPromptSubmit": "working", "Stop": "completed", "StopFailure": "failed",
              "SessionEnd": "ended"}
HOOK_KEEP_DAYS = 3


def record_hook(root, event: dict, where: str, now: datetime = None) -> None:
    """一条钩子事件更新一个会话：标题取项目名 + 她这个会话里说的第一句话；只存几十个字，不存对话内容。"""
    now = now or datetime.now(timezone.utc)
    path = Path(root) / HOOKS_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    sid = str(event["session_id"])[:80]
    name = str(event.get("hook_event_name", ""))
    s = data.get(sid) or {"project": _project(str(event.get("cwd") or "")), "first": ""}
    if name == "UserPromptSubmit" and not s.get("first"):
        text = re.sub(r"<[^>]*>", " ", str(event.get("prompt", "")))  # 别的会话发来的消息带着标签，去掉
        s["first"] = " ".join(text.split())[:40]
    if name == "Stop" and event.get("last_assistant_message"):
        s["summary"] = " ".join(str(event["last_assistant_message"]).split())[:120]
    if name in HOOK_STATE:
        s["state"] = HOOK_STATE[name]
    s["where"] = where if where in SESSION_WHERE else "cloud"
    s["updated_at"] = now.isoformat(timespec="seconds")
    data[sid] = s
    cutoff = (now - timedelta(days=HOOK_KEEP_DAYS)).isoformat(timespec="seconds")
    data = {k: v for k, v in data.items() if v.get("updated_at", "") >= cutoff}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


REPORT_AGENTS = {"claude": "Claude", "codex": "Codex", "grok": "Grok", "chatgpt": "ChatGPT", "gemini": "Gemini", "other": "其他"}


def record_report(root, data: dict, now: datetime = None) -> str:
    """别的 agent 自己报到（2026-10-29 她要所有 agent、会话、任务都汇总到团队页）：
    {"agent": "codex", "id": "会话或任务编号", "title": "在干什么", "state": "working|blocked|review_ready|completed|failed|ended",
     "summary": "一句话", "where": "mac|cloud|server"}。和 Claude 钩子记在同一个文件，同一个编号再报就是更新。"""
    now = now or datetime.now(timezone.utc)
    agent = str(data.get("agent") or "other").lower()
    agent = agent if agent in REPORT_AGENTS else "other"
    rid = str(data.get("id") or "").strip()[:80]
    if not rid:
        raise ValueError("缺 id")
    path = Path(root) / HOOKS_FILE
    try:
        hooks = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        hooks = {}
    key = f"{agent}:{rid}"
    s = hooks.get(key) or {"project": "", "first": ""}
    s["agent"] = agent
    if data.get("title"):
        s["first"] = " ".join(re.sub(r"<[^>]*>", " ", str(data["title"])).split())[:60]
    if data.get("project"):
        s["project"] = str(data["project"])[:40]
    if data.get("summary"):
        s["summary"] = " ".join(str(data["summary"]).split())[:120]
    state = str(data.get("state") or "working")
    s["state"] = state if state in SESSION_STATES or state == "ended" else "working"
    s["where"] = data.get("where") if data.get("where") in SESSION_WHERE else s.get("where", "cloud")
    s["updated_at"] = now.isoformat(timespec="seconds")
    hooks[key] = s
    cutoff = (now - timedelta(days=HOOK_KEEP_DAYS)).isoformat(timespec="seconds")
    hooks = {k: v for k, v in hooks.items() if v.get("updated_at", "") >= cutoff}
    path.write_text(json.dumps(hooks, ensure_ascii=False), encoding="utf-8")
    return key


def _project(cwd: str) -> str:
    """项目名取仓库目录名；在 .claude/worktrees/<分支> 这类工作树里，取工作树上面那层的仓库名。"""
    parts = [p for p in Path(cwd).parts if p not in ("/", "")]
    for i, p in enumerate(parts):
        if p in (".claude", "worktrees", ".worktrees") and i:
            return parts[i - 1][:40]
    name = parts[-1] if parts else ""
    return "" if name.startswith(("bridge-", "cse_")) else name[:40]


def _hook_sessions(root, now: datetime = None) -> list:
    now = now or datetime.now(timezone.utc)
    try:
        data = json.loads((Path(root) / HOOKS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for s in data.values():
        if s.get("state") == "ended":
            continue
        title = s.get("project") or ("" if s.get("agent") else "会话")
        if s.get("first"):
            title = (title + " · " if title else "") + s["first"]
        state = s.get("state", "idle")
        if state == "review_ready" and s.get("updated_at", "") < (now - timedelta(hours=12)).isoformat():
            state = "idle"  # 一轮做完等她看，半天没动就当空闲，不老挂在看板上
        if state == "working" and s.get("updated_at", "") < (now - timedelta(hours=6)).isoformat():
            state = "idle"  # 6 小时没动静的“在干”多半是会话断了没报结束
        out.append({"title": title or "会话", "state": state, "where": s.get("where", "cloud"), "agent": s.get("agent", "claude"),
                    "updated_at": s.get("updated_at", ""), "summary": s.get("summary", "")})
    return out


def save_sessions(root, sessions: list, now: datetime = None) -> int:
    rows = []
    for s in sessions[:60]:
        if not isinstance(s, dict) or not s.get("title"):
            continue
        rows.append({"title": str(s["title"])[:80],
                     "state": s.get("state") if s.get("state") in SESSION_STATES else "idle",
                     "where": s.get("where") if s.get("where") in SESSION_WHERE else "cloud",
                     "updated_at": str(s.get("updated_at", ""))[:32], "summary": str(s.get("summary", ""))[:200]})
    at = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")
    path = Path(root) / SESSIONS_FILE
    path.write_text(json.dumps({"at": at, "sessions": rows}, ensure_ascii=False), encoding="utf-8")
    return len(rows)


def load_sessions(root, now: datetime = None) -> dict:
    hooked = _hook_sessions(root, now)
    if hooked:
        data = {"at": max(s["updated_at"] for s in hooked), "live": True, "sessions": hooked}
    else:
        try:
            data = json.loads((Path(root) / SESSIONS_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"at": None, "sessions": []}
    for s in data.get("sessions", []):
        s["state_label"] = SESSION_STATES.get(s["state"], "空闲")
        s["where_label"] = SESSION_WHERE.get(s["where"], "云端")
    order = {"working": 0, "blocked": 1, "review_ready": 1, "failed": 2}
    data["sessions"].sort(key=lambda s: s["updated_at"], reverse=True)  # 在干的排最前，其余按最近活动
    data["sessions"].sort(key=lambda s: order.get(s["state"], 3))
    return data
