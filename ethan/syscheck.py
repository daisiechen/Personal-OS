"""系统体检一屏看全。

设置页“系统体检”和每 3 小时的巡检（ethan/checkup.py）看同一份：
- 定时任务：每个任务的中文名、上次、结果、下次、超时没跑（ethan/jobs.py 的 JOBS，和 deploy/systemd 的定时器一一对应）；
- 自动上线：最近一次部署（提交号、结果、时间、挂掉的测试名）和每 2 分钟的检查心跳（ethan/autodeploy.py）；
- 后台大批整理：微信对话、人物卡整理到哪了，从数据库现算；有活却 2 小时没进展算卡住；
- 主动消息里的几样（聊聊、周五周末推荐、派 Grok 查本周新闻 / 周末去哪）和她 Mac 上的导出（服务器只看最近一次收到）；
- 订阅额度：最近 5 小时用了多少、门槛多少、后台整理是不是在让路（ethan/bgwork.py budget）；
- 来源健康（lifebase.source_health）、服务器可用内存（/proc/meminfo）、红点自检（ethan/invariants.py）；
- Agent-Box 每晚备份（Agent-Box 仓库 docs/mia/design.md 第 3 节）：洛杉矶每晚拉下的备份仓库里各机器的 status.json，连续两晚没备上进巡检。
只读：不改她的数据。时间按本地时间写给她看。
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import jobs
from . import clock

MEM_LOW, MEM_CRITICAL = 300, 200  # 可用内存低于 300MB 标黄，低于 200MB 进巡检（2G 的机器，11-04 曾被会话进程吃满）
STUCK_AFTER = timedelta(hours=2)  # 大批整理有活却 2 小时没进展
CHECK_LATE = timedelta(minutes=15)  # 自动上线每 2 分钟看一次 main，15 分钟没看算停了
BACKUP_DIR_NAME = "agent-box-backup"  # 在数据目录旁边（洛杉矶 /home/ethan/agent-box-backup），ethan 用户每晚 04:10 拉一次（Agent-Box deploy/backup）
# 机器 → 该有的备份种类和最长间隔：两台服务器每晚一次，过 50 小时 = 连续两晚没备上；Mac 常合着，放宽到 3 天
BACKUPS = {"seoul": ("db", timedelta(hours=50)), "la": ("homes", timedelta(hours=50)), "mac": ("homes", timedelta(hours=74))}


def _t(v):
    """各处存的时间格式不一（ISO 带时区、SQLite 的 'YYYY-MM-DD HH:MM:SS' UTC），统一成带时区的 datetime。"""
    if not v:
        return None
    s = str(v).replace("Z", "+00:00")
    if len(s) > 10 and s[10] == " ":
        s = s[:10] + "T" + s[11:]
    try:
        t = datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _fmt(t) -> str:
    t = _t(t) if not isinstance(t, datetime) else t
    if not t:
        return ""
    return t.astimezone(clock.her_tz()).strftime("%m-%d %H:%M")


def _one(db, sql, args=()):
    try:
        r = db.conn.execute(sql, args).fetchone()
    except Exception:
        return None
    return r[0] if r else None


# ---------- 自动上线 ----------
def deploy_info(root, now: datetime = None) -> dict:
    from .autodeploy import CHECK, STATUS, failed_tests
    now = now or datetime.now(timezone.utc)
    root = Path(root)
    try:
        dep = json.loads((root / STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dep = {}
    try:
        chk = json.loads((root / CHECK).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        chk = {}
    result = str(dep.get("result", ""))
    tests = dep.get("failed_tests") or (failed_tests(str(dep.get("detail", ""))) if dep.get("step") == "跑测试" else [])
    checked = _t(chk.get("checked_at"))
    return {"has": bool(dep), "commit": str(dep.get("to", ""))[:7], "subject": dep.get("subject", ""), "result": result,
            "ok": bool(dep) and result.startswith("已上线") and "出错" not in result, "at": _fmt(dep.get("at")), "step": dep.get("step", ""),
            "detail": str(dep.get("detail", ""))[-300:], "failed_tests": tests,
            "migrations": str(dep.get("migrations", ""))[-300:], "migrations_ok": "出错" not in str(dep.get("migrations", "")),
            "checked": _fmt(checked), "check_error": chk.get("error", ""),
            # 心跳文件是这次统一巡检才加的：还没有就不报（上线后 2 分钟就有了）
            "check_ok": not chk or (checked is not None and now - checked <= CHECK_LATE and not chk.get("error"))}


def backup_info(root, now: datetime = None, base: Path = None) -> list:
    """Agent-Box 每晚备份各机器最近一次。备份仓库没在这台机器上就返回空（不报）。"""
    now = now or datetime.now(timezone.utc)
    base = Path(base) if base else Path(root).resolve().parent / BACKUP_DIR_NAME
    if not (base / ".git").exists():
        return []
    out = []
    for name, (kind, limit) in BACKUPS.items():
        try:
            st = json.loads((base / name / "status.json").read_text(encoding="utf-8")).get(kind) or {}
        except (OSError, ValueError):
            st = {}
        at = _t(st.get("time"))
        out.append({"name": name, "kind": kind, "at": _fmt(at), "bytes": st.get("bytes"),
                    "ok": at is not None and now - at <= limit})
    return out


# ---------- 服务器内存 ----------
def memory_info(path: Path = Path("/proc/meminfo")) -> dict:
    """可用内存（MemAvailable）。读不到（不是 Linux）返回 None。"""
    try:
        rows = dict(line.split(":", 1) for line in Path(path).read_text().splitlines() if ":" in line)
        mb = lambda k: int(rows[k].strip().split()[0]) // 1024
        avail, total = mb("MemAvailable"), mb("MemTotal")
    except (OSError, KeyError, ValueError, IndexError):
        return None
    swap_total = mb("SwapTotal") if "SwapTotal" in rows else 0
    swap_used = swap_total - (mb("SwapFree") if "SwapFree" in rows else 0)
    level = "critical" if avail < MEM_CRITICAL else "low" if avail < MEM_LOW else "ok"
    return {"available_mb": avail, "total_mb": total, "swap_used_mb": swap_used, "swap_total_mb": swap_total, "level": level}


# ---------- 后台大批整理 ----------
WX = "text LIKE '【访谈】会话：微信%'"


def backlog(db, now: datetime = None) -> dict:
    """微信对话、整段对话、人物卡整理到哪了（ethan2-backlog 每 10 分钟接着整理，ethan/interviews.py、ethan/relations.py）。"""
    from . import interviews, lifebase, relations
    now = now or datetime.now(timezone.utc)
    lifebase.ensure(db)
    interviews.ensure_digest(db)
    relations.ensure(db)
    done_ids = interviews.DONE_SQL
    live = "source = 'interview' AND deleted_at IS NULL AND text IS NOT NULL"
    wx_total = _one(db, f"SELECT COUNT(*) FROM evidence WHERE {live} AND {WX}") or 0
    wx_done = _one(db, f"SELECT COUNT(*) FROM evidence WHERE {live} AND {WX} AND id IN ({done_ids})") or 0
    pending = interviews.pending(db)
    last_digest = _t(_one(db, "SELECT MAX(at) FROM interview_digest"))
    oldest = _t(_one(db, "SELECT recorded_at FROM evidence WHERE id = ?", (pending[0]["id"],))) if pending else None  # 最早没整理的那份什么时候到的
    wechat_people = "wx_name IS NOT NULL"
    todo_sql = f"{wechat_people} AND (organized_at IS NULL OR substr(organized_at, 1, 10) < last_contact)"  # 同 relations.candidates
    p_total = _one(db, f"SELECT COUNT(*) FROM people WHERE {wechat_people}") or 0
    p_done = _one(db, f"SELECT COUNT(*) FROM people WHERE {wechat_people} AND organized_at IS NOT NULL") or 0
    p_todo = _one(db, f"SELECT COUNT(*) FROM people WHERE {todo_sql}") or 0
    try:  # 只有统计、还没有聊天记录可整理的人不算“没整理”：等她 Mac 上交过来（11-08 的 CHEN. 让体检一直报人物卡卡住）
        from . import relations
        waiting = sum(1 for r in db.conn.execute(f"SELECT wx_name, wx_key FROM people WHERE {todo_sql}").fetchall()
                      if not relations._records(db, r["wx_name"], r["wx_key"])[0].strip())
        p_todo -= waiting
    except Exception:
        pass
    last_people = _t(_one(db, "SELECT MAX(organized_at) FROM people"))
    people_since = _t(_one(db, f"SELECT MIN(stats_at) FROM people WHERE {todo_sql}"))

    def stuck(left, last, since):
        if not left:
            return False
        anchor = max([t for t in (last, since) if t], default=None)
        return anchor is not None and now - anchor > STUCK_AFTER

    talks_stuck = stuck(len(pending), last_digest, oldest)
    # 人物卡在整段对话整理完之后才轮到（同一个定时任务里先后跑），对话还没整理完时不算人物卡卡住
    people_stuck = not pending and stuck(p_todo, last_people, people_since)
    return {"wechat": {"done": wx_done, "total": wx_total, "left": wx_total - wx_done},
            "talks_left": len(pending), "last_digest": _fmt(last_digest), "talks_stuck": talks_stuck,
            "talks_given_up": interviews.given_up(db),  # 试满 3 次都没整理成的（Codex 评审 11-05）
            "people": {"done": p_done, "total": p_total, "left": p_todo}, "last_people": _fmt(last_people), "people_stuck": people_stuck}


# ---------- 订阅额度（5 小时窗口） ----------
def quota(root, now: datetime = None) -> dict:
    """订阅最近 5 小时的用量和后台是否在让路（和 bgwork.PatientRouter 用同一条规则）。没有账本就当 0。"""
    from . import bgwork
    from .core.ledger import Ledger
    from .core.router import Settings
    root = Path(root)
    settings = Settings(root / "settings.json")
    if not (root / "ledger").is_dir():
        from .core import quota as q
        block = bgwork.paid_block(root, now)
        return {"calls": 0, "used_usd": 0.0, "rate_per_hour": 0.0, "projected_usd": 0.0, "tokens": {}, "tokens_known": 0,
                "by_purpose": {}, "hours": 5, "limit_usd": bgwork.budget_limit(settings), "util": q.windows(root, now),
                "util_max": bgwork.UTIL_MAX, "paid_block": block, "yielding": bool(block)}
    out = bgwork.budget(Ledger(root / "ledger"), settings, now)
    if out.get("paid_block"):  # 体检显示本地时间
        out["paid_block"] = {**out["paid_block"], "until_local": _fmt(out["paid_block"].get("until"))}
    return out


# ---------- 主动消息里的几样、派活、Mac 上的导出 ----------
def feeds(db, now: datetime = None) -> list:
    """{name, last, detail, ok}：这些不是单独的定时任务（在主动消息里，或在她 Mac 上），单列出来才看得到有没有在动。"""
    now = now or datetime.now(timezone.utc)
    out = []
    local = now.astimezone(clock.her_tz())
    week = clock.week_start(local.date()).isoformat()

    chat = _one(db, "SELECT MAX(at) FROM nudge_once WHERE key LIKE 'lesson-chat:%'")
    out.append({"name": "聊聊（知识点 + 补缺口）", "last": _fmt(chat), "ok": True,
                "detail": "每天 15:00–20:30 她没在聊时来一次；今天讲过问过就不发" if chat else "还没发过（每天 15:00–20:30 她没在聊时来一次）"})

    wk = None
    try:
        r = db.conn.execute("SELECT * FROM weekend_recs ORDER BY week DESC LIMIT 1").fetchone()
        wk = dict(r) if r else None
    except Exception:
        pass
    fri = local.date() + timedelta(days=4 - local.weekday()) if local.weekday() <= 4 else local.date() - timedelta(days=local.weekday() - 4)
    this_week = wk if wk and wk.get("week") == fri.isoformat() else None
    late_fri = local.date() == fri and local.hour >= 21 or local.date() > fri
    # 发出去记在主动消息的发送记录里（键 weekend:周五），weekend_recs.sent_at 没人写（11-07 体检误报“这周五过了还没发”，其实 11-06 12:32 发了）
    try:
        from . import nudges
        sent = (this_week or {}).get("sent_at") or nudges.sent_at(db, f"weekend:{fri.isoformat()}")
    except Exception:
        sent = (this_week or {}).get("sent_at")
    if sent:
        d, ok = f"这周的发了（{_fmt(sent)}）", True
    elif late_fri:
        d, ok = "这周五过了还没发", False
    else:
        d, ok = f"周五 12:00–20:30 发；{('Grok 已在查' if this_week and this_week.get('asked_at') and not this_week.get('got_at') else 'Grok 查回来了' if this_week and this_week.get('got_at') else '还没派 Grok')}", True
    out.append({"name": "周五“这周末可以去”", "last": _fmt(sent or (wk or {}).get("sent_at")), "detail": d, "ok": ok})

    news = None
    try:
        r = db.conn.execute("SELECT * FROM lesson_news WHERE week = ?", (week,)).fetchone()
        news = dict(r) if r else None
    except Exception:
        pass
    asked = _t((news or {}).get("asked_at"))
    if not news or not asked:
        d, ok = "这周还没派（每周派一次，派了才有本周新闻）", True
    elif news.get("got_at"):
        d, ok = f"{_fmt(asked)} 派的，{_fmt(news['got_at'])} 回了", True
    else:
        ok = now - asked <= timedelta(days=2)
        d = f"{_fmt(asked)} 派的，还没回" + ("" if ok else "（超过 2 天）")
    out.append({"name": "派 Grok 查本周新闻（知识点用）", "last": _fmt(asked), "detail": d, "ok": ok})

    for name, sql in (("微信导出（她 Mac 上，每周日）", f"SELECT MAX(recorded_at) FROM evidence WHERE source = 'interview' AND {WX}"),
                      ("微信私聊统计（人物卡用）", "SELECT MAX(stats_at) FROM people"),
                      ("Codex 导出（她 Mac 上，每周日）", "SELECT MAX(recorded_at) FROM evidence WHERE source = 'interview' "
                                                       "AND instr(substr(text, 1, 300), '；agent：Codex') > 0"),
                      ("Grok Bot App 导出（每月她点一次）", "SELECT MAX(at) FROM grok_files WHERE is_grok = 1")):
        last = _one(db, sql)
        out.append({"name": name, "last": _fmt(last), "ok": True, "detail": f"最近一次收到 {_fmt(last)}" if last else "还没收到过"})
    return out


# ---------- 合在一起 ----------
def _invariants(wb, day) -> list:
    from . import invariants
    try:
        got = invariants.run_all(wb, day)
    except Exception as e:
        return [{"name": "红点自检", "ok": False, "items": [f"没跑成：{type(e).__name__} {str(e)[:120]}"]}]
    return [{"name": k, "ok": not v, "items": v[:5]} for k, v in got.items()]


def overview(root, db, now: datetime = None, wb=None, meminfo: Path = Path("/proc/meminfo"), services: bool = True) -> dict:
    """设置页“系统体检”一屏的全部内容。"""
    from . import checkup, lifebase
    from .clock import her_today
    now = now or datetime.now(timezone.utc)
    out = {"jobs": jobs.overview(root, now), "deploy": deploy_info(root, now), "memory": memory_info(meminfo)}
    for key, fn in (("backlog", lambda: backlog(db, now)), ("quota", lambda: quota(root, now)), ("feeds", lambda: feeds(db, now)), ("sources", lambda: lifebase.source_health(db, now, cached=True))):
        try:
            out[key] = fn()
        except Exception as e:
            out[key] = None
            out.setdefault("errors", []).append(f"{key}：{type(e).__name__} {str(e)[:120]}")
    out["checks"] = _invariants(wb, her_today()) if wb is not None else []
    # 巡检多看的：任务自己报的错、常驻服务停了、邮箱登不上、互相矛盾（只取这里没单独列的）
    shown = {j["name"] for j in out["jobs"]} | {"自动上线", "数据改动", "数据来源", "服务器内存", "自动上线检查", "大批整理"}
    out["other"] = [p for p in checkup.problems(root, db, now, services=services) if p["what"] not in shown and not p["what"].startswith("检查：")]
    try:  # 等着办：系统的到了没人收、等太久还没到的（ethan/followups.py）；只在体检页列，不进巡检推送
        from . import followups
        out["other"] += followups.problems(db, her_today())
    except Exception as e:
        out["other"].append({"what": "等着办", "detail": f"没查成：{type(e).__name__} {str(e)[:120]}"})
    bad = [j for j in out["jobs"] if not j["ok"]]
    n = len(bad) + (0 if out["deploy"]["ok"] or not out["deploy"]["has"] else 1) + (0 if out["deploy"]["check_ok"] else 1)
    n += sum(1 for s in out.get("sources") or [] if s["state"] == "stale")
    n += 1 if (out.get("backlog") or {}).get("talks_stuck") or (out.get("backlog") or {}).get("people_stuck") else 0
    n += 1 if (out.get("backlog") or {}).get("talks_given_up") else 0
    n += 1 if (out["memory"] or {}).get("level") == "critical" else 0
    n += sum(1 for c in out["checks"] if not c["ok"]) + len(out["other"]) + sum(1 for f in out.get("feeds") or [] if not f["ok"])
    out["bad"] = n
    out["at"] = _fmt(now)
    return out


def problems(root, db, now: datetime = None, meminfo: Path = Path("/proc/meminfo")) -> list:
    """巡检多查的几样（checkup.problems 调）：自动上线心跳、大批整理卡住、内存太低。[{what, detail}]"""
    now = now or datetime.now(timezone.utc)
    out = []
    dep = deploy_info(root, now)
    if not dep["check_ok"]:
        out.append({"what": "自动上线检查", "detail": dep["check_error"] or f"上次看 main 是 {dep['checked'] or '很久以前'}，定时器可能停了"})
    if dep["failed_tests"] and not dep["ok"]:
        out.append({"what": "自动上线", "detail": "没过的测试：" + "、".join(dep["failed_tests"])})
    if db is not None:
        try:
            from .interviews import MAX_TRIES
            b = backlog(db, now)
            if b["talks_stuck"]:
                out.append({"what": "大批整理", "detail": f"还有 {b['talks_left']} 份对话没整理，最近一次进展 {b['last_digest'] or '没有'}，超过 2 小时没动"})
            if b["talks_given_up"]:
                out.append({"what": "大批整理", "detail": f"有 {b['talks_given_up']} 份对话试了 {MAX_TRIES} 次都没整理成，放弃了"
                                                     "（interview_digest 里 ok = 0 的；修好原因后把 tries 清零就会重试）"})
            if b["people_stuck"]:
                out.append({"what": "大批整理", "detail": f"还有 {b['people']['left']} 张人物卡没整理，最近一次进展 {b['last_people'] or '没有'}，超过 2 小时没动"})
        except Exception as e:
            out.append({"what": "大批整理", "detail": f"没查成：{type(e).__name__} {str(e)[:200]}"})
    for b in backup_info(root, now):
        if not b["ok"]:
            out.append({"what": "Agent-Box 备份", "detail": f"{b['name']} 的{'数据库' if b['kind'] == 'db' else ' agent 目录'}最近一次备份是 {b['at'] or '从来没有'}，"
                                                     "连续两晚没备上（看那台机器的 agentbox-backup 定时任务日志）"})
    mem = memory_info(meminfo)
    if mem and mem["level"] == "critical":
        out.append({"what": "服务器内存", "detail": f"可用只剩 {mem['available_mb']}MB（共 {mem['total_mb']}MB，交换区用了 {mem['swap_used_mb']}MB）。"
                                                 "先看有没有没归档的服务器会话（每个常驻 70–195M）"})
    return out
