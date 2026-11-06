"""给 Claude Code 装“会话报到”钩子：会话开始、她发话、一轮做完、结束时，往接收口报一声，团队页实时显示。

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
import os
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


CURFEW_URL = "https://ethan.example.com/cloud/curfew"
CURFEW_MARK = "ethan-curfew"


def curfew_command(where: str) -> str:
    """晚上 11 点后她给 Claude Code 发新指令时先问服务器，过点又没放行就挡回；连不上服务器就放行。
    云端：定时任务唤醒会话的消息和“定时巡检”直接放行（授权：“授权，你搞吧”，docs/operations/devices.md）。"""
    guard = ('[ "$CLAUDE_CODE_REMOTE" = true ] || exit 0; case "$(cat)" in *queued-remote-notifications*|*定时巡检*) exit 0;; esac; '
             if where == "cloud" else "")
    return (f'{guard}t="${{ETHAN_HOOK_TOKEN:-$ETHAN_CLOUD_TOKEN}}"; [ -n "$t" ] || exit 0; '
            f'r=$(curl -s -m 4 -A {CURFEW_MARK} -H "Authorization: Bearer $t" "{CURFEW_URL}") || exit 0; '
            "printf '%s' \"$r\" | python3 -c 'import json,sys\n"
            "try:\n d=json.load(sys.stdin)\nexcept Exception:\n sys.exit(0)\n"
            "if d.get(\"stop\"):\n print(d.get(\"message\") or \"已经 11 点了，今天到这儿。\", file=sys.stderr); sys.exit(2)'")


def install(settings_path: Path, where: str, token: str = None) -> dict:
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        settings = {}
    hooks = settings.setdefault("hooks", {})
    for event in EVENTS:
        groups = [g for g in hooks.get(event, [])
                  if not any(MARK in h.get("command", "") for h in g.get("hooks", []))]
        groups = [g for g in groups if not any(CURFEW_MARK in h.get("command", "") for h in g.get("hooks", []))]
        groups.append({"hooks": [{"type": "command", "command": command(where), "async": True}]})
        if event == "UserPromptSubmit":  # 收工钩子：同步跑，过点挡回（退出码 2）
            groups.append({"hooks": [{"type": "command", "command": curfew_command(where), "timeout": 8}]})
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


# Codex（所有 agent 都汇总到团队页）：Codex 没有 Claude 那样的钩子，只有 notify——每轮做完调一次外部程序，
# 把一段 JSON 当参数传进去。装一个小脚本转发到 /cloud/agent/report，记成“Codex 会话 · 一轮做完等你看”。
REPORT_URL = "https://ethan.example.com/cloud/agent/report"
CODEX_SCRIPT = r'''#!/usr/bin/env python3
# ethan-hook：Codex 每轮做完报到给 小伴 的团队页（python3 -m ethan.setup_hook --where codex 装的，重装会覆盖）
# 每次被调用都在 ethan_notify.log 记一行（不记钥匙、不记对话全文），查“为什么没报到”用（桌面 App 一直没报到）
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
    if other:  # 她原来有别的 notify（Mac 上就有）：存下来让我们的脚本接着调它，再换成我们的
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


NOTE_BEGIN, NOTE_END = "<!-- ethan-memory-note: begin -->", "<!-- ethan-memory-note: end -->"
MEMORY_NOTE = NOTE_BEGIN + """
## Mia 本人的话要交给 小伴 的记忆（所有会话都照这个做）

在任何会话里，她说了**关于她自己**的话（经历、过往、处境、习惯、喜好、看法、做决定的方式），或者直接给了你她的文件（简历、自我介绍、测评报告……），一个话题聊完时交给 小伴 的记忆：
- 只交她的原话：问题照写，回答可以去掉口头语，不改意思、不总结、不加你的推测；对系统设计、代码的要求不交（那是一次决定，不是她的性格）。
- 去掉电话、邮箱、住址、证件号、银行卡号。这些是私密内容，只进 小伴 的记忆，不写进任何仓库。
- 交法（钥匙在环境变量 ETHAN_CLOUD_TOKEN 或 ETHAN_HOOK_TOKEN；返回 401 说明这把钥匙只能报到，告诉她要换云端钥匙）：
  curl -s -A Ethan-cloud/1.0 -H "Authorization: Bearer ${ETHAN_CLOUD_TOKEN:-$ETHAN_HOOK_TOKEN}" -H "Content-Type: application/json" \
    --data-binary @正文.json https://ethan.example.com/cloud/interview
  正文：她说的话 {"session": "会话名", "scene": "聊了什么", "text": "问题和她的原话"}；她给的文件 {"kind": "file", "session": "会话名", "name": "文件名", "text": "文件的文字"}
- 临时文件用完删掉。完整说明：Personal-OS 仓库 docs/operations/interview-handoff.md。
""" + NOTE_END


def install_memory_note(path: Path) -> Path:
    """把上面这段写进全局说明文件（Claude Code 的 ~/.claude/CLAUDE.md、Codex 的 ~/.codex/AGENTS.md）：
    这台机器上所有会话开工都会读到，不用她每开一个会话贴一次。重装时替换自己那段，不动她写的别的内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if NOTE_BEGIN in text and NOTE_END in text:
        head, _, rest = text.partition(NOTE_BEGIN)
        tail = rest.partition(NOTE_END)[2]
        text = head.rstrip("\n") + ("\n\n" if head.strip() else "") + MEMORY_NOTE + tail
    else:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + MEMORY_NOTE + "\n"
    path.write_text(text, encoding="utf-8")
    return path


# ---------- 云端环境的启动脚本（docs/operations/cloud-curfew.md） ----------
# 云端会话常从上层目录（/home/user）开，读不到仓库里的 .claude/settings.json，收工钩子就不生效（统一巡检查出）。
# 在 claude.ai 云端环境的“启动脚本”里把收工钩子写进 ~/.claude/settings.json（用户级，不管从哪个目录开都读）。
# 脚本里不放钥匙：钩子跑的时候才读环境里已有的 ETHAN_CLOUD_TOKEN（启动脚本本身读不到环境变量和密钥）。
CLOUD_USER_SCRIPT = """# 小伴 收工钩子（python3 -m ethan.setup_hook --where cloud-user 生成，docs/operations/cloud-curfew.md）
# 11 点后她发的新指令挡回；定时任务唤醒和“定时巡检”放行。不含钥匙，钩子运行时读 ETHAN_CLOUD_TOKEN。
mkdir -p "$HOME/.claude"
python3 - <<'ETHAN_CURFEW_PY' || true
import json, os
p = os.path.expanduser("~/.claude/settings.json")
try:
    s = json.load(open(p, encoding="utf-8"))
except Exception:
    s = {}
cmd = __CMD__
hooks = s.setdefault("hooks", {})
groups = [g for g in hooks.get("UserPromptSubmit", []) if not any("ethan-curfew" in h.get("command", "") for h in g.get("hooks", []))]
groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 8}]})
hooks["UserPromptSubmit"] = groups
json.dump(s, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("小伴 收工钩子已写进", p)
ETHAN_CURFEW_PY
"""


def cloud_user_script() -> str:
    return CLOUD_USER_SCRIPT.replace("__CMD__", json.dumps(curfew_command("cloud"), ensure_ascii=False))


def check(paths: list) -> list:
    """哪几个设置文件里有收工钩子（服务器会话、云端会话自查用）：[(路径, 有没有, 只在云端生效吗)]。"""
    out = []
    for p in paths:
        try:
            s = json.loads(Path(p).expanduser().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            out.append((str(p), False, False))
            continue
        cmds = [h.get("command", "") for g in (s.get("hooks") or {}).get("UserPromptSubmit", []) for h in g.get("hooks", [])]
        hit = [c for c in cmds if CURFEW_MARK in c]
        out.append((str(p), bool(hit), bool(hit) and all('CLAUDE_CODE_REMOTE" = true' in c for c in hit)))
    return out


def main():
    parser = argparse.ArgumentParser(description="装会话报到钩子")
    parser.add_argument("--where", choices=("mac", "server", "codex", "cloud-user"),
                        help="cloud-user：只打印云端环境“启动脚本”要粘的那段（收工钩子写进 ~/.claude/settings.json，不含钥匙）")
    parser.add_argument("--check", action="store_true", help="看收工钩子装在哪：~/.claude/settings.json 和 --repo 仓库的 .claude/settings.json")
    parser.add_argument("--repo", type=Path, help="写进这个仓库的 .claude/settings.json（云端会话用）")
    parser.add_argument("--from-env", type=Path, help="服务器：从 ethan.env 读钥匙")
    parser.add_argument("--ssh", help="Mac：经 ssh 从服务器读钥匙，例如 ethan@服务器")
    parser.add_argument("--at", choices=("mac", "seoul", "server"), help="Codex 装在哪台机器上（团队页卡片上显示），默认 Mac 是 mac、Linux 是 seoul")
    parser.add_argument("--memory-note", action="store_true", help="只写全局说明：她的话交给 小伴 的记忆（~/.claude/CLAUDE.md、~/.codex/AGENTS.md），不要钥匙")
    args = parser.parse_args()
    if args.where == "cloud-user":
        print(cloud_user_script(), end="")
        return
    if args.check:
        paths = [Path.home() / ".claude" / "settings.json"] + ([args.repo / ".claude" / "settings.json"] if args.repo else [])
        found = False
        for path, has, cloud_only in check(paths):
            found |= has
            print(f"{path}：{'有收工钩子' + ('（只在云端会话生效）' if cloud_only else '') if has else '没有收工钩子'}")
        print(f"CLAUDE_CODE_REMOTE={os.environ.get('CLAUDE_CODE_REMOTE', '')!r}；钥匙 {'有' if os.environ.get('ETHAN_HOOK_TOKEN') or os.environ.get('ETHAN_CLOUD_TOKEN') else '没有'}"
              "（钥匙只看有没有，不打印）")
        sys.exit(0 if found else 1)
    if args.memory_note:
        done = [install_memory_note(Path.home() / ".claude" / "CLAUDE.md"), install_memory_note(Path.home() / ".codex" / "AGENTS.md")]
        print("写好了：" + "、".join(str(p) for p in done) + "。这台机器上新开的 Claude Code 和 Codex 会话都会照这个做。")
        return
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
        install_memory_note(Path.home() / ".codex" / "AGENTS.md")
        print(f"装好了：{cfg}（原来的 notify 照样会跑，改之前的备份在 config.toml.bak-ethan）。"
              "另外每分钟看一眼 Codex 的会话记录，在干、做完都会实时报到团队页。" if cfg else
              "~/.codex/config.toml 里原来的 notify 写法认不出，没改；把那一行原样发给 Claude 会话处理。")
        return
    path = Path.home() / ".claude" / "settings.json"
    install(path, args.where, token)
    install_memory_note(Path.home() / ".claude" / "CLAUDE.md")
    print(f"装好了：{path}。新开的 Claude 会话都会报到，团队页“Claude 会话”一栏实时显示；全局说明也写进了 ~/.claude/CLAUDE.md。")


if __name__ == "__main__":
    main()
