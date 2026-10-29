"""给 Claude Code 装“会话报到”钩子：会话开始、她发话、一轮做完、结束时，往接收口报一声，团队页实时显示（2026-10-28 她要）。

她是 Max 个人套餐，没有账户层面的统一设置，所以分三处装（见 docs/design/agent-team.md）：
  Mac：      python3 -m ethan.setup_hook --where mac [--ssh 用户@服务器]   写进 ~/.claude/settings.json，Mac 上所有会话都报到
  服务器：    python3 -m ethan.setup_hook --where server --from-env ~/.config/ethan/ethan.env
  云端仓库：  python3 -m ethan.setup_hook --repo <仓库目录>   写进仓库的 .claude/settings.json（不含密钥，只在云端会话里生效）
  Codex：    python3 -m ethan.setup_hook --where codex --ssh my-server（my-server 是她 Mac 上 ~/.ssh/config 里服务器的名字）   写 ~/.codex/ethan_notify.py 和 config.toml 的 notify
钥匙用 INBOX_TOKEN_CLOUD（云端环境里本来就有 ETHAN_CLOUD_TOKEN）；也认只能报到的 INBOX_TOKEN_HOOK。
报到只发钩子自带的那几项（会话编号、项目目录、她的话、最后一段回复），服务器只留几十个字，不存对话内容。
钩子在后台跑（async），连不上就算了，不影响会话。
"""

import argparse
import getpass
import json
import subprocess
import sys
from pathlib import Path

URL = "https://ethan.example.com/cloud/agent/hook"
MARK = "ethan-hook"  # 用来认出自己装的钩子，重装时替换不重复
EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")


def command(where: str) -> str:
    guard = '[ "$CLAUDE_CODE_REMOTE" = true ] || exit 0; ' if where == "cloud" else ""
    return (f'{guard}t="${{ETHAN_HOOK_TOKEN:-$ETHAN_CLOUD_TOKEN}}"; [ -n "$t" ] || exit 0; '
            f'curl -s -m 5 -A {MARK} -H "Authorization: Bearer $t" -H "Content-Type: application/json" '
            f'--data-binary @- "{URL}?where={where}" >/dev/null 2>&1 || true')


def install(settings_path: Path, where: str, token: str = None) -> dict:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        groups = [g for g in hooks.get(event, [])
                  if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        groups.append({"hooks": [{"type": "command", "command": command(where), "async": True}]})
        hooks[event] = groups
    if token:
        settings.setdefault("env", {})["ETHAN_HOOK_TOKEN"] = token
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if token:
        settings_path.chmod(0o600)  # 里面有钥匙
    return settings


def token_from_env(env_file: Path) -> str:
    for line in env_file.read_text(encoding="utf-8").splitlines():
        if line.startswith("INBOX_TOKEN_CLOUD="):
            return line.split("=", 1)[1].strip()
    raise SystemExit(f"{env_file} 里没有 INBOX_TOKEN_CLOUD")


def token_from_ssh(host: str) -> str:
    out = subprocess.run(["ssh", host, "grep '^INBOX_TOKEN_CLOUD=' /home/ethan/.config/ethan/ethan.env 2>/dev/null || "
                                       "sudo -n -u ethan grep '^INBOX_TOKEN_CLOUD=' /home/ethan/.config/ethan/ethan.env"],
                         capture_output=True, text=True, timeout=30)
    if out.returncode != 0 or "=" not in out.stdout:
        raise SystemExit("ssh 没取到钥匙：" + (out.stderr.strip()[:200] or "没有输出"))
    return out.stdout.split("=", 1)[1].strip()


# Codex（2026-10-29 她要所有 agent 都汇总到团队页）：Codex 没有 Claude 那样的钩子，只有 notify——每轮做完调一次外部程序，
# 把一段 JSON 当参数传进去。装一个小脚本转发到 /cloud/agent/report，记成“Codex 会话 · 一轮做完等你看”。
REPORT_URL = "https://ethan.example.com/cloud/agent/report"
CODEX_SCRIPT = r'''#!/usr/bin/env python3
# ethan-hook：Codex 每轮做完报到给 小伴 的团队页（python3 -m ethan.setup_hook --where codex 装的，重装会覆盖）
# 每次被调用都在 ethan_notify.log 记一行（不记钥匙、不记对话全文），查“为什么没报到”用（2026-10-29 桌面 App 一直没报到）
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent
def log(line):
    try:
        f = here / "ethan_notify.log"
        old = f.read_text(encoding="utf-8").splitlines()[-199:] if f.exists() else []
        f.write_text("\n".join(old + [time.strftime("%m-%d %H:%M:%S ") + line]) + "\n", encoding="utf-8")
    except Exception:
        pass
ev = None
for a in reversed(sys.argv[1:]):  # 不同版本参数个数不一样：取最后一个能读成 JSON 的
    try:
        ev = json.loads(a)
        break
    except ValueError:
        continue
if not isinstance(ev, dict):
    log(f"被调用了，但没有 JSON 参数：{len(sys.argv) - 1} 个参数 {[x[:30] for x in sys.argv[1:]]}")
    sys.exit(0)
kind = str(ev.get("type", ""))
log(f"被调用：type={kind} keys={sorted(ev)[:12]}")
token = (here / "ethan_token").read_text().strip() if (here / "ethan_token").exists() else ""
if not token:
    log("没有钥匙文件，没报")
    sys.exit(0)
msgs = ev.get("input-messages") or ev.get("input_messages") or [""]
tid = ev.get("thread-id") or ev.get("thread_id") or ev.get("turn-id") or ev.get("turn_id") or "codex"
where = (here / "ethan_where").read_text().strip() if (here / "ethan_where").exists() else "mac"
body = {"agent": "codex", "id": str(tid), "where": where,
        "project": Path(ev.get("cwd") or "").name, "title": " ".join(str(msgs[0] if msgs else "").split())[:60],
        "summary": " ".join(str(ev.get("last-assistant-message") or ev.get("last_assistant_message") or "").split())[:120],
        "state": "review_ready"}
try:
    out = subprocess.run(["/usr/bin/curl", "-s", "-m", "8", "-o", "/dev/null", "-w", "%{http_code}", "-A", "ethan-hook",
                          "-H", "Authorization: Bearer " + token, "-H", "Content-Type: application/json",
                          "--data-binary", json.dumps(body, ensure_ascii=False), "__URL__"],
                         capture_output=True, text=True, timeout=12)
    log(f"报到结果：HTTP {out.stdout or '无'} curl 退出码 {out.returncode} {out.stderr.strip()[:80]}")
    ok = out.stdout.strip() == "200"
except Exception as e:
    log(f"报到没发出去：{type(e).__name__} {str(e)[:80]}")
    ok = False
if not ok:  # 桌面 App 的沙箱不让联网：先存着，Mac 上的工作时长小程序每 5 分钟在沙箱外替它发（ethan/worktime.py flush_codex）
    try:
        with open(here / "ethan_spool.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(body, ensure_ascii=False) + "\n")
        log("已存进 ethan_spool.jsonl，等 5 分钟内补发")
    except Exception as e:
        log(f"存不下来：{type(e).__name__}")
'''
# 她原来就有的 notify 照样跑（装的时候存在 ethan_notify_next.json），放在最前面，不管报到成不成都先跑
CODEX_CHAIN = r'''import json as _j, subprocess as _s, sys as _y
from pathlib import Path as _P
_n = _P(__file__).with_name("ethan_notify_next.json")
if _n.exists() and len(_y.argv) > 1:
    try:
        _s.Popen(_j.loads(_n.read_text()) + [_y.argv[1]])
    except Exception:
        pass
'''.replace("__URL__", REPORT_URL)


WATCH_LABEL = "me.mia.ethan-codex-watch"


def schedule_watch(home: Path, run=subprocess.run) -> str:
    """每分钟跑一次 ~/.codex/ethan_watch.py：Mac 用 launchd，Linux 用 cron。"""
    script = home / ".codex" / "ethan_watch.py"
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (sys.executable or "python3")
    if sys.platform == "darwin":
        plist = home / "Library" / "LaunchAgents" / f"{WATCH_LABEL}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{WATCH_LABEL}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>{script}</string></array>
  <key>StartInterval</key><integer>60</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
""")
        run(["launchctl", "unload", str(plist)], capture_output=True)
        run(["launchctl", "load", str(plist)], capture_output=True)
        return "launchd，每分钟一次"
    cur = run(["crontab", "-l"], capture_output=True, text=True)
    lines = [l for l in (cur.stdout or "").splitlines() if "ethan_watch.py" not in l]
    lines.append(f"* * * * * {py} {script} >/dev/null 2>&1")
    run(["crontab", "-"], input="\n".join(lines) + "\n", text=True, capture_output=True)
    return "cron，每分钟一次"


def install_codex(home: Path, token: str, where: str = "mac", schedule: bool = False) -> Path:
    """写 ~/.codex/ethan_notify.py 和钥匙文件，在 config.toml 顶上加 notify；已有别的 notify 就不动，返回 None 让她自己合。"""
    d = home / ".codex"
    d.mkdir(parents=True, exist_ok=True)
    script = d / "ethan_notify.py"
    head, rest = CODEX_SCRIPT.split("\n", 2)[:2], CODEX_SCRIPT.split("\n", 2)[2]
    script.write_text("\n".join(head) + "\n" + CODEX_CHAIN + rest, encoding="utf-8")
    key = d / "ethan_token"
    key.write_text(token + "\n", encoding="utf-8")
    key.chmod(0o600)
    (d / "ethan_where").write_text(where + "\n", encoding="utf-8")
    (d / "ethan_watch.py").write_text((Path(__file__).with_name("codex_watch.py")).read_text(encoding="utf-8"), encoding="utf-8")
    if schedule:
        schedule_watch(home)
    cfg = d / "config.toml"
    text = cfg.read_text(encoding="utf-8") if cfg.exists() else ""
    py = "/usr/bin/python3" if Path("/usr/bin/python3").exists() else (sys.executable or "python3")  # 桌面 App 的 PATH 很短，写绝对路径
    line = f'notify = ["{py}", "{script}"]'
    lines = [l for l in text.splitlines() if not (l.startswith("notify") and "ethan_notify" in l)]
    other = [l for l in lines if l.strip().startswith("notify")]
    if other:  # 她原来有别的 notify（2026-10-29 她 Mac 上就有）：存下来让我们的脚本接着调它，再换成我们的
        try:
            prev = json.loads(other[0].split("=", 1)[1].strip())
            assert isinstance(prev, list) and all(isinstance(x, str) for x in prev)
        except (ValueError, AssertionError, IndexError):
            return None  # 写法认不出（多行、单引号），不冒险改
        (d / "ethan_notify_next.json").write_text(json.dumps(prev, ensure_ascii=False), encoding="utf-8")
        (d / "config.toml.bak-ethan").write_text(text, encoding="utf-8")
        lines = [l for l in lines if l is not other[0]]
    cfg.write_text("\n".join([line] + lines) + "\n", encoding="utf-8")  # notify 要放在所有 [表] 前面
    return cfg


def main():
    parser = argparse.ArgumentParser(description="装会话报到钩子")
    parser.add_argument("--where", choices=("mac", "server", "codex"))
    parser.add_argument("--repo", type=Path, help="写进这个仓库的 .claude/settings.json（云端会话用）")
    parser.add_argument("--from-env", type=Path, help="服务器：从 ethan.env 读钥匙")
    parser.add_argument("--ssh", help="Mac：经 ssh 从服务器读钥匙，例如 ethan@服务器")
    parser.add_argument("--at", choices=("mac", "seoul", "server"), help="Codex 装在哪台机器上（团队页卡片上显示），默认 Mac 是 mac、Linux 是 seoul")
    args = parser.parse_args()
    if args.repo:
        install(args.repo / ".claude" / "settings.json", "cloud")
        print(f"已写进 {args.repo / '.claude' / 'settings.json'}，提交并合进默认分支后，这个仓库的云端会话就会报到。")
        return
    if not args.where:
        raise SystemExit("要么 --repo，要么 --where mac|server")
    if args.from_env:
        token = token_from_env(args.from_env.expanduser())
    elif args.ssh:
        token = token_from_ssh(args.ssh)
    else:
        token = getpass.getpass("粘贴云端钥匙（Claude 云端环境设置里 ETHAN_CLOUD_TOKEN 的值，屏幕不显示）: ").strip()
    if not token:
        raise SystemExit("没有钥匙，没装")
    if args.where == "codex":
        at = args.at or ("mac" if sys.platform == "darwin" else "seoul")
        cfg = install_codex(Path.home(), token, where=at, schedule=True)
        print(f"装好了：{cfg}（原来的 notify 照样会跑，改之前的备份在 config.toml.bak-ethan）。"
              "另外每分钟看一眼 Codex 的会话记录，在干、做完都会实时报到团队页。" if cfg else
              "~/.codex/config.toml 里原来的 notify 写法认不出，没改；把那一行原样发给 Claude 会话处理。")
        return
    path = Path.home() / ".claude" / "settings.json"
    install(path, args.where, token)
    print(f"装好了：{path}。新开的 Claude 会话都会报到，团队页“Claude 会话”一栏实时显示。")


if __name__ == "__main__":
    main()
