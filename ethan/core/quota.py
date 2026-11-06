"""订阅额度的“硬闸”：在用付费额度（extra usage / overage）或订阅额度用完时，后台大批整理一律不跑。

那天周额度用完后，后台整理接着跑，花的是她的付费额度。

- 每次调订阅（claude -p --output-format stream-json），Claude Code 会发一条 rate_limit_event：
  status（allowed / allowed_warning / rejected）、isUsingOverage、resetsAt（unix 秒）、rateLimitType、
  unifiedWindows（five_hour / seven_day 的 utilization 和 resetsAt）。2.1.x 实测有。
- 聊天和后台的调用都会看到这条：isUsingOverage 为真 → 记“到 resetsAt 之前后台不跑”；status 为 rejected（额度满）同样记；
  拿不到这条、但订阅报“额度用完”类错误（429 / usage limit / weekly limit）→ 到 resetsAt，没有就到下一个 UTC 9 点。
- 之后哪次调用看到 status 正常、没在用付费额度，就解除（额度提前恢复）。
- 聊天不看这个标记，照常回。
标记和最近一次额度情况都写在数据目录里（quota-block.json、quota-state.json），后台各入口开工前读。
"""

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

STATE = "quota-state.json"   # 最近一次 Claude 报的额度情况（体检显示）
BLOCK = "quota-block.json"   # 后台禁跑标记：{"until", "reason", "at"}
LIMIT_TEXT = re.compile(r"usage limit|weekly limit|limit reached|hit your limit|rate.?limit|\b429\b|out of (extra )?usage|"
                        r"usage credits|overage", re.I)
RESET_HOUR_UTC = 9


def _now(now=None):
    return now or datetime.now(timezone.utc)


def next_reset_guess(now: datetime = None) -> datetime:
    """拿不到 resetsAt 时的保守猜测：下一个 UTC 9 点。"""
    now = _now(now)
    t = now.replace(hour=RESET_HOUR_UTC, minute=0, second=0, microsecond=0)
    return t if t > now else t + timedelta(days=1)


def _ts(v):
    try:
        return datetime.fromtimestamp(float(v), timezone.utc) if v not in (None, "") else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _write(path: Path, data: dict):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def block(root, until: datetime, reason: str, now: datetime = None):
    now = _now(now)
    if until <= now:
        until = next_reset_guess(now)
    old = blocked(root, now)
    if old and datetime.fromisoformat(old["until"]) > until:
        until = datetime.fromisoformat(old["until"])  # 已经记了更晚的就不往前提
    _write(Path(root) / BLOCK, {"until": until.isoformat(timespec="seconds"), "reason": reason,
                                "at": now.isoformat(timespec="seconds")})


def clear(root):
    try:
        (Path(root) / BLOCK).unlink()
    except OSError:
        pass


def blocked(root, now: datetime = None):
    """还在禁跑期就返回 {"until", "reason", "at"}，否则 None。"""
    b = _read(Path(root) / BLOCK)
    if not isinstance(b, dict) or not b.get("until"):
        return None
    try:
        until = datetime.fromisoformat(b["until"])
    except ValueError:
        return None
    return b if until > _now(now) else None


def state(root):
    return _read(Path(root) / STATE)


def observe(root, info: dict, now: datetime = None):
    """记下一次调用里 Claude 报的额度情况，并按规则设 / 解除禁跑。info 是 rate_limit_event 的 rate_limit_info。"""
    if not isinstance(info, dict):
        return
    now = _now(now)
    try:
        _write(Path(root) / STATE, {**info, "seen_at": now.isoformat(timespec="seconds")})
        reset = _ts(info.get("resetsAt")) or next_reset_guess(now)
        if info.get("isUsingOverage"):
            block(root, reset, "在用付费额度（extra usage），订阅额度恢复前后台不跑", now)
        elif info.get("status") == "rejected":
            block(root, reset, f"订阅额度用完（{info.get('rateLimitType') or '额度'}），恢复前后台不跑", now)
        elif info.get("status") in ("allowed", "allowed_warning"):
            clear(root)
    except OSError:
        pass


def on_error(root, text: str, info: dict = None, now: datetime = None):
    """订阅调用失败：带了额度信息就按信息来；没带但报错像“额度用完”，到下一个 UTC 9 点前不跑。"""
    if isinstance(info, dict):
        observe(root, info, now)
        if info.get("status") == "rejected" or info.get("isUsingOverage"):
            return
    if LIMIT_TEXT.search(text or ""):
        try:
            block(root, next_reset_guess(now), f"订阅报额度用完：{(text or '')[:120]}", now)
        except OSError:
            pass


def windows(root, now: datetime = None) -> dict:
    """最近一次 Claude 报的各窗口用了几成（还没重置的才算）：{"five_hour": 0.36, "seven_day": 0.04}。"""
    s = state(root) or {}
    now = _now(now)
    out = {}
    for name, w in (s.get("unifiedWindows") or {}).items():
        if not isinstance(w, dict) or w.get("utilization") is None:
            continue
        reset = _ts(w.get("resetsAt"))
        if reset is None or reset > now:
            try:
                out[name] = float(w["utilization"])
            except (TypeError, ValueError):
                pass
    return out
