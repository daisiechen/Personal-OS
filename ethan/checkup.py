"""系统巡检（系统出问题开发会话主动找她，不要等她或 小伴 发现）。

服务器这边只汇总“哪里不对”，不修东西：上线失败、数据改动出错、定时任务失败或该跑没跑、任务自己报的错（job_runs，
比如写日历失败、计划用了兜底）、常驻服务没在跑。云端的定时巡检会话每 3 小时取一次 GET /cloud/health，
有问题就修（明显的 bug 修完直接上线），再推送到她手机上的 Claude App。没问题什么都不发。
"""

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import jobs

SERVICES = ("ethan2-inbox", "ethan2-telegram", "ethan2-weixin", "ethan2-workbench")  # 常驻的；定时的看 jobs.json
RECENT = timedelta(hours=26)


def _service_down(name: str, runner=subprocess.run) -> str:
    try:
        r = runner(["systemctl", "is-active", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""  # 看不了 systemd（比如测试机）就不报
    state = (r.stdout or "").strip()
    return "" if state in ("active", "activating", "reloading") else state or "未知"


def problems(root, db=None, now: datetime = None, runner=subprocess.run, services: bool = True, meminfo: Path = Path("/proc/meminfo")) -> list:
    """返回 [{"what": 哪里, "detail": 怎么了}]；空列表就是一切正常。"""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    out = []
    try:
        dep = json.loads((root / "deploy-status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        dep = {}
    if dep and not str(dep.get("result", "")).startswith("已上线"):
        out.append({"what": "自动上线", "detail": f"{dep.get('to', '')} {dep.get('result', '')}：{dep.get('step', '')} {str(dep.get('detail', ''))[-300:]}"})
    if "出错" in str(dep.get("migrations", "")) or "数据改动出错" in str(dep.get("result", "")):
        out.append({"what": "数据改动", "detail": f"{dep.get('to', '')} {dep.get('result', '')}：{str(dep.get('migrations', ''))[-300:]}"})
    for j in jobs.health(root, now):  # 从没跑过的：新加的等过了该跑的时间才报（jobs.overview 的 never）
        if not j["ok"]:
            out.append({"what": j["name"], "detail": j["detail"]})
    try:  # 自动上线心跳、挂掉的测试名、大批整理卡住、内存太低（统一巡检，ethan/syscheck.py）
        from . import syscheck
        out += syscheck.problems(root, db, now, meminfo)
    except Exception as e:
        out.append({"what": "系统体检", "detail": f"没查成：{type(e).__name__} {str(e)[:200]}"})
    if db is not None:
        since = (now - RECENT).isoformat()
        rows = db.conn.execute("SELECT job, finished_at, error FROM job_runs WHERE id IN (SELECT MAX(id) FROM job_runs GROUP BY job) "
                               "AND error IS NOT NULL AND error != '' AND finished_at >= ?", (since,)).fetchall()
        for r in rows:
            out.append({"what": f"任务 {r['job']}", "detail": f"{r['finished_at']} {str(r['error'])[:300]}"})
    if db is not None:
        try:  # 连续登不上的邮箱（126）
            from . import imapmail
            for f in imapmail.failing(db):
                out.append({"what": f"邮箱 {f['account']}", "detail": f"从 {f['failed_since']} 起连续 {f['fail_count']} 次登不上：{f['last_error']}。"
                                                                     "重新生成授权码后马上用 --add 录进去"})
        except Exception:
            pass
    if db is not None:
        try:  # 永远要成立的检查里只读库、不费事的几条（docs/design/system-rules.md 第 7 节）
            from . import invariants
            from .clock import her_today
            for name, bad in (("编号不重用", invariants.ids_not_reused(db)), ("她改过的没被改回", invariants.locks_respected(db, her_today())),
                              ("状态合法", invariants.states_valid(db)),
                              ("推断没写成事实", invariants.inference_not_fact(db))):
                for b in bad[:5]:
                    out.append({"what": f"检查：{name}", "detail": b})
        except Exception as e:
            out.append({"what": "检查：永远要成立的规则", "detail": f"没跑成：{type(e).__name__} {str(e)[:200]}"})
    if db is not None:
        try:  # 来源断档：每个数据源按自己的频率判断（ontology-plan 1.10，lifebase.source_health）
            from . import lifebase
            for b in lifebase.stale_sources(db, now):
                out.append({"what": "数据来源", "detail": b})
        except Exception as e:
            out.append({"what": "数据来源", "detail": f"没查成：{type(e).__name__} {str(e)[:200]}"})
    if db is not None:
        try:  # 互相矛盾的地方（第 24 题，ontology-plan 3.1）：只报出来，不改她的数据
            from . import onto_query
            from .clock import her_today
            # “小伴 答应了不在待办里”是按说法猜的，会误报，不进巡检告警（巡检会推到她手机），只留在函数结果里给开发会话看
            sure = [c for c in onto_query.contradictions(db, "patrol", her_today())["items"] if c["kind"] != "promise_not_todo"]
            for c in sure[:5]:
                out.append({"what": "检查：互相矛盾", "detail": f"{c['detail']}：{c.get('title') or c.get('decisions')}"})
        except Exception as e:
            out.append({"what": "检查：互相矛盾", "detail": f"没查成：{type(e).__name__} {str(e)[:200]}"})
    for name in SERVICES if services else ():
        state = _service_down(name, runner)
        if state:
            out.append({"what": f"服务 {name}", "detail": f"没在跑（{state}）"})
    return out
