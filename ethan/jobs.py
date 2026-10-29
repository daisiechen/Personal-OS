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
# 任务名 → (中文名, 多久该跑一次)。超过两倍间隔没跑就算出问题
JOBS = {
    "planner": ("排计划（定时任务）", timedelta(days=1)),
    "english": ("英语备课", timedelta(days=1)),
    "backup": ("加密备份", timedelta(days=1)),
    "vault": ("看板同步", timedelta(hours=1)),
    "proactive": ("主动消息", timedelta(minutes=10)),
    "weekly": ("周报", timedelta(days=7)),
    "prep": ("准备员", timedelta(hours=14)),  # 白天每 2 小时，夜里停 10 小时
    "brain": ("大脑整理", timedelta(minutes=30)),
    "memory": ("每晚记忆整理", timedelta(days=1)),
    "money": ("Monzo 同步", timedelta(hours=1)),
}


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
        runs[name] = {"started": started.isoformat(timespec="seconds"), "finished": finished.isoformat(timespec="seconds"),
                      "ok": ok, "tail": tail[-400:]}
        save(data, runs)


def run(data: Path, name: str, cmd: list, runner=subprocess.run) -> int:
    started = datetime.now(timezone.utc)
    proc = runner(cmd, capture_output=True, text=True)
    out = (proc.stdout or "") + (proc.stderr or "")
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    lines = [l for l in out.strip().splitlines() if l.strip()]
    record(data, name, proc.returncode == 0, started, datetime.now(timezone.utc), "\n".join(lines[-3:]))
    return proc.returncode


def health(data: Path, now: datetime = None) -> list:
    """设置页体检用：每个定时任务一行 {name, ok, detail}。"""
    now = now or datetime.now(timezone.utc)
    runs = load(data)
    out = []
    for key, (label, every) in JOBS.items():
        r = runs.get(key)
        if not r:
            out.append({"name": label, "ok": False, "detail": "还没有运行记录"})
            continue
        at = datetime.fromisoformat(r["finished"])
        late = now - at > every * 2
        when = at.astimezone(timezone.utc).strftime("%m-%d %H:%M UTC")
        if not r["ok"]:
            detail = f"{when} 失败：{(r.get('tail') or '').splitlines()[-1][:80] if r.get('tail') else '没有输出'}"
        elif late:
            detail = f"上次 {when}，该跑没跑"
        else:
            detail = f"最近一次 {when}"
        out.append({"name": label, "ok": r["ok"] and not late, "detail": detail})
    return out


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
