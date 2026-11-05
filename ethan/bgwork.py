"""后台大批整理（微信初始化这种上千次调用）时给 小伴 让路（她 11-04：“按建议来”）。

- 她正在和 小伴 聊（最近 QUIET 分钟里有她的消息）：先停，等她不聊了再接着整理；
- 订阅额度满了、模型出错：不切备用模型（备用每月 50 美元上限，不能被后台烧掉），歇 BACKOFF 分钟再试，
  一直不行才算这一份失败。
"""

import time
from datetime import datetime, timedelta, timezone

QUIET = 10        # 她最近几分钟说过话就让路
BACKOFF = 15      # 出错后歇几分钟
MAX_WAITS = 12    # 一次调用最多歇几回（约 3 小时），再不行算失败
CHECK = 60        # 让路时每隔几秒看一眼


def chatting(store, now: datetime = None, minutes: int = QUIET) -> bool:
    """她最近 minutes 分钟里有没有给 小伴 发过消息。"""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(minutes=minutes)).isoformat()
    try:
        for r in reversed(store.raw.recent(30)):
            if r.get("role") == "mia" and str(r.get("at", "")) >= since[:len(str(r.get("at", "")))]:
                return True
    except Exception:
        return False
    return False


class PatientRouter:
    """包一层：只用订阅（不碰备用），她在聊就等，出错就歇一会儿再试。"""

    def __init__(self, router, store, sleep=time.sleep, now=lambda: datetime.now(timezone.utc)):
        self.router, self.store, self.sleep, self.now = router, store, sleep, now
        self.waited = 0

    def __getattr__(self, name):
        return getattr(self.router, name)

    def _yield(self):
        for _ in range(180):  # 最多等 3 小时她聊完
            if not chatting(self.store, self.now()):
                return
            self.sleep(CHECK)

    def complete(self, system, messages, purpose="chat", images=()):
        last = None
        for _ in range(MAX_WAITS + 1):
            self._yield()
            try:
                if getattr(self.router, "primary", None) is not None:
                    return self.router._call(self.router.primary, system, messages, purpose, images)
                return self.router.complete(system, messages, purpose, images)
            except Exception as e:  # 额度满、超时：歇一会儿
                last = e
                self.waited += 1
                self.sleep(BACKOFF * 60)
        raise last


def patient(router, store):
    return PatientRouter(router, store)


def retry_locked(fn, tries: int = 6, wait: int = 60, sleep=time.sleep):
    """数据库被锁住（部署时备份、迁移正在写）就等一会儿再整轮重跑；别的错照常抛（11-05 部署那一刻 backlog 撞锁失败）。"""
    import sqlite3
    for i in range(tries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or i == tries - 1:
                raise
            sleep(wait)


# ---------- 后台整理的生命周期（Codex 评审 P2 第 10 条，11-05）：日志、锁、有限批、心跳 ----------
LOG_DIR = "logs"
LOG_MAX = 1_000_000  # 一个日志超过约 1MB 就挪成 .log.1（只留一份旧的）
JOB = "backlog"      # 心跳记在 jobs.json 的哪个任务下（体检“大批整理”那一行）


def log_file(root, name: str):
    """打开 <数据目录>/logs/<name>.log（追加）；超过 LOG_MAX 先滚动。返回打开的文件，用完 close。"""
    import os
    from pathlib import Path
    d = Path(root) / LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.log"
    try:
        if path.stat().st_size > LOG_MAX:
            os.replace(path, path.with_name(path.name + ".1"))
    except OSError:
        pass
    f = open(path, "a", encoding="utf-8")
    f.write(f"\n== {datetime.now(timezone.utc).isoformat(timespec='seconds')} 启动 ==\n")
    f.flush()
    return f


def spawn_logged(cmd: list, root, name: str, spawn=None) -> bool:
    """另起后台进程跑 cmd，输出和报错都追加进 logs/<name>.log（以前丢进 /dev/null，失败了没处查）。"""
    import subprocess
    from pathlib import Path
    spawn = spawn or subprocess.Popen
    try:
        log = log_file(root, name)
    except OSError:
        log = None
    try:
        spawn(cmd, cwd=str(Path(__file__).resolve().parent.parent), stdout=log or subprocess.DEVNULL,
              stderr=subprocess.STDOUT if log else subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False
    finally:
        if log:
            log.close()  # 子进程拿着自己的一份，父进程这边关掉


def try_lock(root, name: str):
    """拿到文件锁返回打开的锁文件（用完 close），别人拿着返回 None。"""
    import fcntl
    from pathlib import Path
    f = open(Path(root) / name, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except OSError:
        f.close()
        return None


def deadline(minutes: float, clock=time.monotonic):
    """有限批：返回 stop()，过了 minutes 分钟就为真；minutes 为 0 / None 时永远为假（不设上限）。"""
    if not minutes:
        return lambda: False
    end = clock() + minutes * 60
    return lambda: clock() >= end


def heartbeat(root, note: str):
    """整理有进展就记一笔进 jobs.json（体检“大批整理”那一行显示“最近一次进展”）；写不进去不影响整理。"""
    from . import jobs
    try:
        jobs.progress(root, JOB, note)
    except OSError:
        pass
