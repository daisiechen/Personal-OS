"""后台大批整理（微信初始化这种上千次调用）时给 小伴 让路。

- 她正在和 小伴 聊（最近 QUIET 分钟里有她的消息）：先停，等她不聊了再接着整理；
- 订阅额度满了、模型出错：不切备用模型（备用每月 50 美元上限，不能被后台烧掉），歇 BACKOFF 分钟再试，
  一直不行才算这一份失败。
- 额度预算（borrow-2026-11 第 4 条）：订阅最近 5 小时的用量“预计”要超过门槛（ledger.window 的 projected_usd），
  后台先歇着等窗口滑过去，不是撞到上限才歇；聊天不走这里，不受影响。门槛见 budget_limit。
  Claude 自己报的 5 小时 / 7 天用量（utilization）到 UTIL_MAX 也让路。
- 硬闸：在用付费额度（extra usage）或订阅额度用完时，
  后台一律不跑，直到订阅额度恢复（ethan/core/quota.py），不受 MAX_YIELD 限制：PatientRouter 抛 QuotaBlocked，
  各入口（interviews --digest-all、relations --organize-all、codex_runner）开工前 gate() 查到就直接退出。
"""

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

QUIET = 10        # 她最近几分钟说过话就让路
BACKOFF = 15      # 出错后歇几分钟；额度快到门槛时也是每 15 分钟看一眼
MAX_WAITS = 12    # 一次调用最多歇几回（约 3 小时），再不行算失败
CHECK = 60        # 让路时每隔几秒看一眼
MAX_YIELD = 6 * 3600  # 一次调用开工前最多让路这么久（秒）；5 小时窗口滑完还不够，就照常做，不卡死
# 订阅 5 小时窗口里“按 API 价折算的美元”（claude -p 的 total_cost_usd）预计到这个数，后台就先让路。
# 她是 Pro 订阅、官方不公布上限，先取保守值；看几天体检里的实际用量和撞墙点再调（settings.json 的 bg_window_usd，
# 或环境变量 ETHAN_BG_WINDOW_USD；设成 0 关掉按额度让路）。
BUDGET_USD = 5.0
BUDGET_KEY = "bg_window_usd"
UTIL_MAX = 0.8          # Claude 报的 5 小时或 7 天窗口用到八成，后台也让路（settings.json 的 bg_util_max 可改，设 0 关掉）
UTIL_KEY = "bg_util_max"


from .core.router import QuotaBlocked  # noqa: E402,F401  在用付费额度或订阅额度用完：后台这一轮别再调模型，干净地停（定义挪到 router，Router 自己也抛）


def _data_dir(router):
    d = getattr(router, "data_dir", None)
    if d is not None:
        return d
    settings = getattr(router, "settings", None)
    return Path(settings.path).parent if settings is not None and getattr(settings, "path", None) else None


def paid_block(root, now: datetime = None):
    """后台禁跑标记还在就返回 {"until", "reason"}，否则 None；读不了当没有。"""
    if root is None:
        return None
    from .core import quota
    try:
        return quota.blocked(root, now)
    except Exception:
        return None


def gate(root, name: str, now: datetime = None) -> bool:
    """后台入口开工前问一句：在禁跑期就打一行日志、返回 True（调用方直接退出）。"""
    b = paid_block(root, now)
    if b:
        print(f"[{name}] {b['reason']}；到 {b['until']}（UTC）前不跑", flush=True)
        return True
    return False


def stop_or_blocked(root, stop=None, now=None):
    """有限批的 stop 再加一条：禁跑标记出现就停（每份开始前问）。"""
    now = now or (lambda: datetime.now(timezone.utc))
    return lambda: bool(paid_block(root, now())) or bool(stop and stop())


def budget_limit(settings=None) -> float:
    """门槛：settings.json 的 bg_window_usd 优先，其次环境变量 ETHAN_BG_WINDOW_USD，都没有用 BUDGET_USD。"""
    for v in ((settings.get(BUDGET_KEY) if settings is not None else None), os.environ.get("ETHAN_BG_WINDOW_USD")):
        if v not in (None, ""):
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return BUDGET_USD


def _setting(settings, key, default) -> float:
    try:
        v = settings.get(key) if settings is not None else None
        return default if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return default


def budget(ledger, settings=None, now: datetime = None) -> dict:
    """订阅 5 小时窗口的用量 + 门槛 + 后台该不该让路（体检和 PatientRouter 看同一份）。"""
    from .core import quota
    w = ledger.window(now=now)
    limit = budget_limit(settings)
    root = Path(settings.path).parent if settings is not None and getattr(settings, "path", None) else Path(ledger.root).parent
    util_max = _setting(settings, UTIL_KEY, UTIL_MAX)
    util = quota.windows(root, now)
    # 现在已经到门槛，或者照最近一小时的速度一小时后会到，或者 Claude 报的用量到了八成，都让路
    # 有 Claude 自己报的用量就只看它；按 API 价折算的美元只在拿不到时兜底（11-07：$5 是 11-05 先拍的保守值，小伴 平时聊天就在 $4.9 上下，
    # 后台从 11-05 上午起一直让路、一条没整理，而 Claude 报的 5 小时 / 7 天用量只有 20% / 27%）
    over_usd = not util and limit > 0 and max(w["used_usd"], w["projected_usd"]) >= limit
    over_util = util_max > 0 and any(v >= util_max for v in util.values())
    block = paid_block(root, now)
    w.update({"limit_usd": limit, "util": util, "util_max": util_max, "paid_block": block,
              "yielding": bool(over_usd or over_util or block)})
    return w


def over_budget(router, now: datetime = None) -> bool:
    ledger = getattr(router, "ledger", None)
    if ledger is None or not hasattr(ledger, "window"):
        return False
    try:
        return budget(ledger, getattr(router, "settings", None), now)["yielding"]
    except Exception:  # 账本读不了不挡后台（以前也没有这道检查）
        return False


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
        self.budget_waits = 0  # 因为额度快到门槛歇过几回

    def __getattr__(self, name):
        return getattr(self.router, name)

    stop = None  # 这一轮的时限（deadline() 返回的函数）；设了就不歇过时限，到点抛 QuotaBlocked 让出位置，下一轮接着做

    def _nap(self, seconds):
        """歇一会儿；这一轮到时限了就不歇，直接让出来（11-06：让路时一歇 5 小时，30 分钟的时限没管住，还占着整理的位置）。"""
        if self.stop is not None and self.stop():
            raise QuotaBlocked("这一轮到时限了，先让出来，下一轮接着整理")
        self.sleep(seconds)
        if self.stop is not None and self.stop():
            raise QuotaBlocked("这一轮到时限了，先让出来，下一轮接着整理")

    def _yield(self):
        """她在聊：每分钟看一眼；额度预计要超门槛：每 15 分钟看一眼。合计最多 MAX_YIELD 秒，到了照常做。"""
        spent = 0
        while spent < MAX_YIELD:
            if paid_block(_data_dir(self.router), self.now()):
                return  # 交给 _check_paid 抛 QuotaBlocked
            if chatting(self.store, self.now()):
                step = CHECK
            elif over_budget(self.router, self.now()):
                step = BACKOFF * 60
                self.budget_waits += 1
            else:
                return
            self._nap(step)
            spent += step

    def _check_paid(self):
        b = paid_block(_data_dir(self.router), self.now())
        if b:
            raise QuotaBlocked(f"{b['reason']}；到 {b['until']}（UTC）")

    def complete(self, system, messages, purpose="chat", images=()):
        last = None
        for _ in range(MAX_WAITS + 1):
            self._check_paid()  # 硬闸：在用付费额度 / 额度用完，不等不试，直接停
            self._yield()
            self._check_paid()
            try:
                if getattr(self.router, "primary", None) is not None:
                    return self.router._call(self.router.primary, system, messages, purpose, images)
                return self.router.complete(system, messages, purpose, images)
            except Exception as e:  # 额度满、超时：歇一会儿
                last = e
                self._check_paid()  # 这次报错刚记下额度用完：别歇了再试，停
                self.waited += 1
                self._nap(BACKOFF * 60)
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


def spawn_logged(cmd: list, root, name: str, spawn=None, keep: bool = False):
    """另起后台进程跑 cmd，输出和报错都追加进 logs/<name>.log（以前丢进 /dev/null，失败了没处查）。
    keep=True 时起成了返回进程对象（工作台要看它跑完没有），否则返回 True；起不了返回 False。新代码走 kick()。"""
    import subprocess
    from pathlib import Path
    spawn = spawn or subprocess.Popen
    try:
        log = log_file(root, name)
    except OSError:
        log = None
    try:
        proc = spawn(cmd, cwd=str(Path(__file__).resolve().parent.parent), stdout=log or subprocess.DEVNULL,
                     stderr=subprocess.STDOUT if log else subprocess.DEVNULL, start_new_session=True)
        return proc if keep else True
    except OSError:
        return False
    finally:
        if log:
            log.close()  # 子进程拿着自己的一份，父进程这边关掉


def kick(root, name: str, args=(), spawn=None, now: datetime = None):
    """唯一的起后台进程入口（决定 7）：python3 -m <模块> --data <root> <args…>，名字在 bgjobs.KICKS 登记。
    - 停跑期（在用付费额度 / 额度用完）且这一名字用到的模型调用按表该停：不起；表里写了 REPLAY 的记下参数，恢复后 catchup 照原样再起；
    - 登记了锁（N 分钟）的：锁文件 <root>/<名字>.kick 在 N 分钟内就不重起（日记补写、赛季结论、catchup）；
    - 输出和报错都进 logs/<名字>.log（以前 12 种起法里 10 种丢进 /dev/null）。
    起了返回进程（测试里换掉的 spawn 返回 None 时返回 True），没起返回 False。"""
    import sys
    from . import bgjobs
    k = bgjobs.KICKS[name]  # 没登记的名字直接报错（守卫测试也查）
    args = [str(a) for a in args]
    stop = next((p for p in k.purposes if bgjobs.blocked(root, p, now)), None)
    if stop:
        if any(bgjobs.job(p).catchup == bgjobs.REPLAY for p in k.purposes):
            bgjobs.note_skipped(root, replay=[name, *args], now=now)
        else:
            bgjobs.note_skipped(root, stop, now=now)
        print(f"[{name}] 停跑期（付费额度），先不起，恢复后补", flush=True)
        return False
    if k.lock_minutes:
        lock = Path(root) / f"{name}.kick"
        try:
            if lock.exists() and time.time() - lock.stat().st_mtime < k.lock_minutes * 60:
                return False
            lock.write_text((now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
        except OSError:
            return False
    got = spawn_logged([sys.executable, "-m", k.module, "--data", str(root), *args], root, name, spawn, keep=True)
    return False if got is False else (True if got is None else got)


def release(root, name: str):
    """跑完了把 kick 的锁去掉（catchup 这种跑完就该允许下一次的）。"""
    try:
        (Path(root) / f"{name}.kick").unlink()
    except OSError:
        pass


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
