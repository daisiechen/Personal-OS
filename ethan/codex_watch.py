#!/usr/bin/env python3
"""Codex 实时报到（2026-10-29 她要 Codex 干活的状态实时更新，Mac 本地和另一台服务器上的都要）。

Codex 只在一轮做完时调 notify，而且桌面 App 在沙箱里跑它、连不了网，所以“在干”报不上来。
但 Codex 每一轮都会把过程实时写进 ~/.codex/sessions/年/月/日/rollout-*.jsonl。这个小程序在沙箱外每分钟看一眼：
正在写的、还没出现“这轮做完”的算在干，做完了的算等她看；状态变了、或者在干每 5 分钟，报一次到 小伴 的接收口。
顺手把 notify 脚本存下来没发出去的报到（ethan_spool.jsonl）也发掉。

只用标准库，装的时候整个文件复制到 ~/.codex/ethan_watch.py（python3 -m ethan.setup_hook --where codex 会装好，
Mac 用 launchd、Linux 用 cron，每分钟一次）。只报项目名、她第一句话的前 60 个字、最后一段回复的前 120 个字，不报对话全文。
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

URL = "https://ethan.example.com/cloud/agent/report"
HOME = Path(__file__).resolve().parent if Path(__file__).resolve().parent.name == ".codex" else Path.home() / ".codex"
RECENT = 6 * 3600  # 只看 6 小时内动过的会话
QUIET = 20 * 60  # 在干的会话 20 分钟没写东西，就当停了
BEAT = 5 * 60  # 在干时每 5 分钟报一次，团队页不会当它断了
START = ("task_started", "user_message", "turn_started")
END = ("task_complete", "turn_complete", "turn_aborted", "task_aborted", "error")


def _text(v) -> str:
    if isinstance(v, str):
        return v
    if isinstance(v, list):
        return " ".join(_text(x.get("text", "")) if isinstance(x, dict) else _text(x) for x in v)
    if isinstance(v, dict):
        return _text(v.get("text") or v.get("message") or v.get("content") or "")
    return ""


def _is_her(text: str) -> bool:
    """Codex 会把 AGENTS.md、环境说明当成“用户消息”塞进去，标题要跳过这些，取她真正说的第一句（2026-10-29）。"""
    t = text.strip()
    return bool(t) and not t.startswith(("<", "# AGENTS.md", "#AGENTS.md")) and "AGENTS.md instructions" not in t[:80]


def read_session(path: Path) -> dict:
    """从一份 rollout 记录里认出：编号、项目、第一句话、最后一段回复、现在是在干还是做完了。"""
    raw = path.read_bytes()
    lines = raw[:65536].decode("utf-8", "ignore").split("\n")
    if len(raw) > 65536:  # 大文件只看开头（会话信息、第一句话）和最后 400 KB（现在的状态）
        lines += raw[-409600:].decode("utf-8", "ignore").split("\n")[1:]
    info = {"id": path.stem.split("-")[-1] if "-" in path.stem else path.stem, "project": "", "title": "",
            "summary": "", "working": False}
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        kind, p = e.get("type"), e.get("payload") if isinstance(e.get("payload"), dict) else e
        if kind == "session_meta":
            info["id"] = str(p.get("id") or info["id"])
            info["project"] = Path(str(p.get("cwd") or "")).name
            continue
        sub = p.get("type")
        if kind == "event_msg" and sub in START:
            info["working"] = True
            if sub == "user_message" and not info["title"] and _is_her(_text(p.get("message"))):
                info["title"] = " ".join(_text(p.get("message")).split())[:60]
        elif kind == "event_msg" and sub in END:
            info["working"] = False
            last = _text(p.get("last_agent_message"))
            if last:
                info["summary"] = " ".join(last.split())[:120]
        elif kind == "event_msg" and sub == "agent_message":
            info["summary"] = " ".join(_text(p.get("message")).split())[:120]
        elif kind == "response_item" and p.get("type") == "message" and p.get("role") == "user" and not info["title"]:
            t = " ".join(_text(p.get("content")).split())
            if _is_her(t):  # 跳过 Codex 自己塞的环境说明和 AGENTS.md
                info["title"] = t[:60]
    return info


def post(body: dict, token: str, opener=urllib.request.urlopen) -> bool:
    req = urllib.request.Request(URL, method="POST", data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "User-Agent": "ethan-hook",
                                          "Authorization": f"Bearer {token}"})
    try:
        with opener(req, timeout=15) as r:
            return getattr(r, "status", 200) < 300
    except Exception:
        return False


def once(home: Path = None, now: float = None, opener=urllib.request.urlopen, say=lambda m: None) -> int:
    home = home or HOME
    now = now or time.time()
    key = home / "ethan_token"
    if not key.exists():
        say(f"没有钥匙文件 {key}")
        return 0
    token = key.read_text().strip()
    where = (home / "ethan_where").read_text().strip() if (home / "ethan_where").exists() else "mac"
    sent = 0
    spool = home / "ethan_spool.jsonl"  # notify 脚本在沙箱里发不出去存下的
    if spool.exists():
        left = [l for l in spool.read_text(encoding="utf-8").split("\n") if l.strip()]
        keep = []
        for l in left:
            try:
                body = json.loads(l)
            except ValueError:
                continue
            if post(body, token, opener):
                sent += 1
            else:
                keep.append(l)
        spool.write_text("".join(k + "\n" for k in keep[-500:]), encoding="utf-8")
    cache_path = home / "ethan_watch_state.json"
    try:
        cache = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        cache = {}
    # 默认看 ~/.codex/sessions；Codex 用了别的 CODEX_HOME 的（比如另一台服务器上的 app-server），把它的 sessions 目录
    # 一行一个写进 ~/.codex/ethan_sessions（2026-10-29 旧服务器上的两个 Codex 都不在默认位置）
    dirs = [home / "sessions"]
    extra = home / "ethan_sessions"
    if extra.exists():
        dirs += [Path(l.strip()).expanduser() for l in extra.read_text(encoding="utf-8").split("\n") if l.strip()]
    files = []
    for sessions in dirs:
        try:
            found = [f for f in sessions.rglob("rollout-*.jsonl") if now - f.stat().st_mtime < RECENT] if sessions.exists() else []
        except OSError as e:
            found = []
            say(f"会话目录 {sessions}：读不了（{type(e).__name__}），可能是权限")
        say(f"会话目录 {sessions}：{'在' if sessions.exists() else '不存在'}，6 小时内动过的记录 {len(found)} 份")
        files += found
    for f in files:
        try:
            s = read_session(f)
        except OSError:
            continue
        working = s["working"] and now - f.stat().st_mtime < QUIET
        state = "working" if working else "review_ready"
        old = cache.get(s["id"]) or {}
        if old.get("state") == state and not (working and now - old.get("at", 0) > BEAT):
            continue
        body = {"agent": "codex", "id": s["id"], "where": where, "project": s["project"], "title": s["title"],
                "summary": s["summary"], "state": state}
        ok = post(body, token, opener)
        say(f"{f.name}：{state} 「{s['title'][:20]}」 报到{'成功' if ok else '失败（连不上 ' + URL + ' 或钥匙不对）'}")
        if ok:
            cache[s["id"]] = {"state": state, "at": now}
            sent += 1
    cache = {k: v for k, v in cache.items() if now - v.get("at", 0) < 2 * RECENT}
    cache_path.write_text(json.dumps(cache))
    return sent


if __name__ == "__main__":
    try:
        n = once(say=print if "-v" in sys.argv else (lambda m: None))
        if "-v" in sys.argv:
            print(f"报了 {n} 条")
    except Exception as e:  # 定时跑的，出错不刷屏，下一分钟再来
        if "-v" in sys.argv:
            print(f"出错：{type(e).__name__} {e}")
