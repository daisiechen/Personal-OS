"""定时任务留痕（2026-10-28 全面检查：后台任务默默坏掉没人知道）。

每个定时任务的 systemd 都经这里启动：照常跑原来的命令，跑完把结果（开始、结束、成功没有、最后几行输出）
记进 <数据目录>/jobs.json；设置页“系统体检”读它，超过该跑的时间没跑、或上次失败，就标红。
命令：python3 -m ethan.jobs --data <数据目录> --name <任务名> -- <原来的命令…>
"""

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

FILE = "jobs.json"
SEEN = "jobs-seen.json"  # 每个任务第一次被体检看到的时间：从没跑过的，过了该跑的时间也要报（2026-11-04 统一巡检）
# 任务名 → (中文名, 多久该跑一次, 定时器里那一行)。定时器那一行和 deploy/systemd/ethan2-<任务名>.timer 里的一字不差
# （tests/test_jobs.py 对着查），体检按它算“下次什么时候跑”和“是不是超时没跑”。没写时区的 OnCalendar 按服务器时区 UTC。
JOBS = {
    "planner": ("排计划（定时任务）", timedelta(days=1), "OnCalendar=*-*-* 06:30:00 Europe/London"),
    "english": ("英语备课", timedelta(days=1), "OnCalendar=*-*-* 06:45:00 Europe/London"),
    "backup": ("加密备份", timedelta(days=1), "OnCalendar=*-*-* 03:30:00"),
    "vault": ("看板同步", timedelta(hours=1), "OnCalendar=hourly"),
    "proactive": ("主动消息", timedelta(minutes=10), "OnCalendar=*:0/10"),
    "weekly": ("周报", timedelta(days=7), "OnCalendar=Sun *-*-* 20:00:00 Europe/London"),
    "prep": ("准备员", timedelta(hours=14), "OnCalendar=*-*-* 08,10,12,14,16,18,20,22:20:00 Europe/London"),  # 白天每 2 小时，夜里停 10 小时
    "brain": ("大脑整理", timedelta(minutes=30), "OnUnitInactiveSec=30min"),
    "memory": ("每晚记忆整理", timedelta(days=1), "OnCalendar=*-*-* 03:00:00 Europe/London"),
    "money": ("Monzo 同步", timedelta(hours=1), "OnUnitInactiveSec=1h"),
    "evals": ("题集评测", timedelta(days=7), "OnCalendar=Sat *-*-* 15:00:00 Europe/London"),
    "mail": ("收邮件", timedelta(minutes=10), "OnUnitInactiveSec=10min"),  # 不及格退出码 1，巡检会看到（2026-10-30 执行保障）
    # 2026-11-04 统一巡检补登记：以前没经过留痕，挂了没人知道（system-review-20261030 第三节“告警有盲区”）
    "backlog": ("大批整理（微信对话、人物卡）", timedelta(minutes=10), "OnUnitInactiveSec=10min"),  # 没活马上退出；整理中能跑好几个小时
    "codex": ("Codex 自动领活", timedelta(minutes=5), "OnUnitInactiveSec=5min"),
}
# 跑了多久还没结束算卡住；None = 不按时长判断（大批整理一跑几个小时，看的是 ethan/syscheck.py 的整理进度）
STUCK = {"backlog": None, "codex": timedelta(hours=4, minutes=30)}
STUCK_DEFAULT = timedelta(hours=3)
LONDON = "Europe/London"


def load(data: Path) -> dict:
    try:
        return json.loads((Path(data) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save(data: Path, runs: dict):
    path = Path(data) / FILE
    tmp = path.with_name(f"{path.stem}.{os.getpid()}.tmp")  # 每个进程自己的临时文件，两个定时任务同时收尾不会互相踩
    tmp.write_text(json.dumps(runs, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record(data: Path, name: str, ok: bool, started: datetime, finished: datetime, tail: str = ""):
    import fcntl
    Path(data).mkdir(parents=True, exist_ok=True)
    with open(Path(data) / (FILE + ".lock"), "w") as lock:  # 读了再写要加锁，不然同一时刻收尾的会丢一条（2026-10-29 自查）
        fcntl.flock(lock, fcntl.LOCK_EX)
        runs = load(data)
        keep = {k: v for k, v in (runs.get(name) or {}).items() if k in ("progress_at", "progress")}  # 心跳留着（收件口起的后台也写它）
        runs[name] = {"started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds"),
                      "ok": ok, "tail": tail[-400:], **keep}
        save(data, runs)


def mark_start(data: Path, name: str, started: datetime):
    """开跑先记一笔“在跑”：跑几个小时的（大批整理、Codex）体检显示在跑，不误报“该跑没跑”；进程被杀了留下的标记按 STUCK 判卡住。"""
    import fcntl
    Path(data).mkdir(parents=True, exist_ok=True)
    with open(Path(data) / (FILE + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runs = load(data)
        runs.setdefault(name, {})["running_since"] = started.isoformat(timespec="seconds")
        save(data, runs)


def progress(data: Path, name: str, note: str, at: datetime = None):
    """长任务的心跳（Codex 评审 P2 第 10 条，11-05）：最近一次进展的时间和一句话，体检在“在跑”那一行显示。"""
    import fcntl
    Path(data).mkdir(parents=True, exist_ok=True)
    with open(Path(data) / (FILE + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        runs = load(data)
        runs.setdefault(name, {}).update(progress_at=(at or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                                         progress=str(note)[:200])
        save(data, runs)


def run(data: Path, name: str, cmd: list, runner=subprocess.run) -> int:
    started = datetime.now(timezone.utc)
    try:
        mark_start(data, name, started)
    except OSError:
        pass
    proc = runner(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    lines = [l for l in out.strip().splitlines() if l.strip()]
    flagged = [l for l in lines if l.rstrip().endswith("✗")]  # 题集评测把不及格的那项标 ✗，可能不在最后几行（2026-11-02 巡检）
    record(data, name, proc.returncode == 0, started, datetime.now(timezone.utc), "\n".join(flagged[-2:] + lines[-3:]))
    return proc.returncode


def _zone(name: str):
    from zoneinfo import ZoneInfo
    return ZoneInfo(name) if name else timezone.utc


def _span(text: str) -> timedelta:
    n = int("".join(ch for ch in text if ch.isdigit()))
    unit = text.lstrip("0123456789").strip()
    return {"s": timedelta(seconds=n), "sec": timedelta(seconds=n), "min": timedelta(minutes=n), "m": timedelta(minutes=n),
            "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


DAYS = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4, "Sat": 5, "Sun": 6}


def next_run(spec: str, after: datetime, started: datetime = None) -> datetime:
    """按定时器那一行算 after 之后的下一次（只认这套定时器里用到的写法）。OnUnitInactiveSec 从上次结束算，OnUnitActiveSec 从上次开始算。"""
    key, _, val = spec.partition("=")
    if key == "OnUnitInactiveSec":
        return after + _span(val)
    if key == "OnUnitActiveSec":
        return (started or after) + _span(val)
    val = val.strip()
    if val == "hourly":
        return after.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    if val.startswith("*:0/"):  # 每 N 分钟（整点对齐）
        step = int(val[4:])
        base = after.replace(second=0, microsecond=0)
        return base + timedelta(minutes=step - base.minute % step)
    parts = val.split()
    days = None
    if parts[0] in DAYS:
        days = {DAYS[parts[0]]}
        parts = parts[1:]
    clock, zone = parts[1], _zone(parts[2] if len(parts) > 2 else "")
    hh, mm, ss = clock.split(":")
    hours = sorted(int(h) for h in hh.split(","))
    local = after.astimezone(zone)
    for add in range(0, 9):
        d = (local + timedelta(days=add)).date()
        if days is not None and d.weekday() not in days:
            continue
        for h in hours:
            at = datetime(d.year, d.month, d.day, h, int(mm), int(ss), tzinfo=zone)
            if at > after:
                return at.astimezone(timezone.utc)
    return after + timedelta(days=7)


WEEK_ZH = {"Mon": "周一", "Tue": "周二", "Wed": "周三", "Thu": "周四", "Fri": "周五", "Sat": "周六", "Sun": "周日"}


def every_text(spec: str) -> str:
    """定时器那一行写成大白话（设置页显示）。"""
    key, _, val = spec.partition("=")
    if key in ("OnUnitInactiveSec", "OnUnitActiveSec"):
        span = _span(val)
        mins = int(span.total_seconds() // 60)
        t = f"{mins // 60} 小时" if mins % 60 == 0 and mins >= 60 else f"{mins} 分钟"
        return f"跑完隔 {t}" if key == "OnUnitInactiveSec" else f"每 {t}"
    if val == "hourly":
        return "每小时"
    if val.startswith("*:0/"):
        return f"每 {val[4:]} 分钟"
    parts = val.split()
    day = WEEK_ZH.get(parts[0], "") if parts[0] in DAYS else ""
    if day:
        parts = parts[1:]
    hh, mm, _ = parts[1].split(":")
    zone = "" if len(parts) > 2 else " UTC"
    if "," in hh:
        hs = hh.split(",")
        return f"{hs[0]}–{hs[-1]} 点每 2 小时（:{mm}）" if len(hs) > 2 else f"每天 {'、'.join(h + ':' + mm for h in hs)}"
    return f"{'每' + day if day else '每天'} {int(hh):02d}:{mm}{zone}"


def _fmt(t: datetime) -> str:
    return t.astimezone(_zone(LONDON)).strftime("%m-%d %H:%M")


def _seen(data: Path, now: datetime) -> dict:
    """第一次看到每个任务的时间（从没跑过的过了该跑的时间才报，免得新加的任务一上线就红）。"""
    path = Path(data) / SEEN
    try:
        seen = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        seen = {}
    new = {k: now.isoformat(timespec="seconds") for k in JOBS if k not in seen}
    if new:
        seen.update(new)
        try:
            path.write_text(json.dumps(seen, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return seen


def overview(data: Path, now: datetime = None) -> list:
    """设置页“系统体检”和巡检用：每个定时任务一行。
    {key, name, ok, state（ok / running / late / failed / stuck / never / waiting）, detail, last, next, running_since}。
    时间都按伦敦时间写（她看的），判断按 UTC 算。"""
    now = now or datetime.now(timezone.utc)
    runs = load(data)
    seen = _seen(data, now)
    out = []
    for key, (label, every, spec) in JOBS.items():
        r = runs.get(key) or {}
        grace = max(min(every, timedelta(hours=2)), timedelta(minutes=10))
        row = {"key": key, "name": label, "ok": True, "state": "ok", "detail": "", "last": None, "next": None,
               "running_since": r.get("running_since"), "every": every_text(spec)}
        fin = datetime.fromisoformat(r["finished"]) if r.get("finished") else None
        st = datetime.fromisoformat(r["started"]) if r.get("started") else None
        run_since = datetime.fromisoformat(r["running_since"]) if r.get("running_since") else None
        running = run_since is not None and (st is None or run_since > st)
        if fin:
            row["last"] = _fmt(fin)
            nxt = next_run(spec, fin, st)
            row["next"] = _fmt(nxt)
        if running:
            limit = STUCK.get(key, STUCK_DEFAULT)
            if limit is not None and now - run_since > limit:
                row.update(ok=False, state="stuck", detail=f"从 {_fmt(run_since)} 开始跑，{int((now - run_since).total_seconds() // 3600)} 小时还没结束（可能卡住或被打断了）")
            else:
                row.update(state="running", detail=f"在跑（从 {_fmt(run_since)} 起）")
            if r.get("progress_at"):
                row["detail"] += f"，最近一次进展 {_fmt(datetime.fromisoformat(r['progress_at']))}：{r.get('progress') or ''}"
            out.append(row)
            continue
        if not fin:
            first = datetime.fromisoformat(seen.get(key, now.isoformat()))
            due = next_run(spec, first) + grace
            row.update(ok=now <= due, state="waiting" if now <= due else "never",
                       detail="还没有运行记录" if now <= due else f"从 {_fmt(first)} 起一直没有运行记录，定时器可能没开")
            row["next"] = _fmt(next_run(spec, now)) if now <= due else None
            out.append(row)
            continue
        late = now > next_run(spec, fin, st) + grace
        when = _fmt(fin)
        if not r.get("ok"):
            tail = (r.get("tail") or "").splitlines()
            line = next((l for l in tail if l.rstrip().endswith("✗") or l.strip().startswith("✗")), None)
            # 找不到标 ✗ 的那行，就不拿最后一行顶替（10-31 那次显示“通过 100%”却算失败，她看不懂）
            if line is None and key != "evals":
                line = tail[-1] if tail else "没有输出"  # 别的任务最后一行就是报错
            row.update(ok=False, state="failed",
                       detail=f"{when} 失败：{line.strip()[:80]}" if line else f"{when} 失败（这次的记录看不出是哪一项，下次运行会写清）")
        elif late:
            row.update(ok=False, state="late", detail=f"上次 {when}，该在 {row['next']} 跑，该跑没跑")
        else:
            row["detail"] = f"最近一次 {when}"
        out.append(row)
    return out


def health(data: Path, now: datetime = None) -> list:
    """兼容老的体检格式：每个定时任务一行 {name, ok, detail}。"""
    return [{"name": r["name"], "ok": r["ok"], "detail": r["detail"]} for r in overview(data, now)]


def main():
    parser = argparse.ArgumentParser(description="跑一个定时任务并留痕")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("cmd", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    cmd = args.cmd[1:] if args.cmd[:1] == ["--"] else args.cmd
    sys.exit(run(args.data, args.name, cmd))


if __name__ == "__main__":
    main()
