"""她自己装的 agent 的记忆和聊天，每周导出一次（ontology-plan 1.8，数据源 S20；agent-network.md 第 3 节）。

现在只做 Codex（Mac 上的 ~/.codex）：
1. 原样存档：AGENTS.md、memories/、sessions/**/rollout-*.jsonl 里新增或改过的文件打成 tar.gz，
   放 ~/.config/ethan/agent-archive/（不进本仓库）。有密钥文件 ~/.config/ethan/agent-archive.key 就用
   ethan/backup.py 同样的 openssl 方式加密成 .tar.gz.enc；推到私有仓库还没做（见 docs/operations/agent-export.md）。
2. 整段对话（她 11-04：“难道别人跟我的对话不是我的记忆么！！！”）：会话记录里她（role=user）和 Codex（assistant）
   两边都交，每行标说话人【她】/【Codex】。她说的去掉 Codex 自动注入的块（环境信息、AGENTS.md 说明）、点头（不到 8 个字）、
   纯代码 / 命令 / 路径，长的粘贴截到 2000 字；Codex 说的多是代码，代码块写“［代码略］”、截到 500 字；都去掉邮箱电话。
   经访谈通道交给 小伴（cloud.CloudClient.interview，format=transcript）。哪些是关于她的、谁说的，由服务器整理时判断。
3. 进度：每个文件按 路径+大小+修改时间 记指纹，交成功才记（会话续写了只交新增的几段），
   记在 ~/.config/ethan/agentexport-state.json。没有钥匙就只生成文件、不发。

用法（在 Personal-OS 目录里）：
  python3 -m ethan.agentexport --codex --dry-run        只看有多少新的，不存档不发
  python3 -m ethan.agentexport --codex --send           存档 + 交给 小伴
  python3 -m ethan.agentexport --install-launchd        装每周日 20:13 自动跑（Mac）
钥匙：环境变量 ETHAN_CLOUD_TOKEN；launchd 里没有环境变量，就读 ~/.claude/settings.json 里 setup_hook 写的那把。
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from datetime import datetime
from pathlib import Path

from ethan import clock
from ethan.docquotes import CHUNK, _plain_len, redact

CONF = Path.home() / ".config" / "ethan"
STATE_FILE = CONF / "agentexport-state.json"
ARCHIVE_DIR = CONF / "agent-archive"
ARCHIVE_KEY = CONF / "agent-archive.key"
OUT_DIR = CONF / "agentexport-out"  # 没钥匙时生成的交接文件
LOG_FILE = CONF / "agentexport.log"
CODEX_HOME = Path.home() / ".codex"
LABEL = "com.ethan.agentexport"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SCENE = "她在 Codex 里说的（agent 记忆导出）"
MIN_CHARS = 8
MAX_CHARS = 2000
CUT_NOTE = "……（后面是粘贴的长内容，已截断）"
HER = "她"  # 整段对话里她的说话人标签（ethan/interviews.py 认这个）
AGENT_MAX = 500  # Codex 的回复多是代码，截到 500 字
LONG_NOTE = "……（太长，截断）"

# Codex 自动塞进“用户消息”的块，不是她说的
INJECTED = [re.compile(p, re.S) for p in (r"<environment_context>.*?(?:</environment_context>|\Z)",
                                          r"<user_instructions>.*?(?:</user_instructions>|\Z)",
                                          r"^\s*# AGENTS\.md instructions.*?(?:</INSTRUCTIONS>|\Z)")]
FENCE = re.compile(r"```.*?(?:```|\Z)", re.S)
CODE_NOTE = "［代码略］"
CJK = re.compile(r"[㐀-鿿]")
COMMANDS = ("git ", "python", "pip ", "npm ", "npx ", "yarn ", "cd ", "ls ", "cat ", "sudo ", "brew ", "curl ", "ssh ",
            "docker ", "rm ", "mv ", "cp ", "mkdir ", "export ", "source ", "$ ", "./", "~/", "/")
CODE_HINTS = re.compile(r"(^\s*(def|class|import|from|function|const|let|var|return)\b|[;{}]\s*$|=>|\(\)|==|Traceback|^\s*at )", re.M)


# ---------- 读会话记录 ----------
def _texts(content) -> list:
    if isinstance(content, str):
        return [content]
    out = []
    for c in content or []:
        if isinstance(c, dict) and isinstance(c.get("text"), str) and c.get("type", "input_text") in (
                "input_text", "output_text", "text"):
            out.append(c["text"])
    return out


def _message(obj) -> tuple:
    """一行事件 → (role, [文字])；认不出的返回 (None, [])。新版包在 response_item.payload 里，旧版直接是 message。"""
    if not isinstance(obj, dict):
        return None, []
    if obj.get("type") == "response_item" and isinstance(obj.get("payload"), dict):
        obj = obj["payload"]
    if obj.get("type") != "message" or obj.get("role") not in ("user", "assistant"):
        return None, []
    return obj["role"], _texts(obj.get("content"))


def strip_injected(text: str) -> str:
    for p in INJECTED:
        text = p.sub("", text)
    return text.strip()


def parse_turns(lines) -> list:
    """一个会话文件 → 两边的话 [{"role": "user" / "assistant", "text"}]（她的去掉了注入块，还没筛）。"""
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        role, texts = _message(obj)
        if role == "user":
            text = "\n".join(p for p in (strip_injected(t) for t in texts) if p).strip()
        else:
            text = "\n".join(texts).strip()
        if role and text:
            out.append({"role": role, "text": text})
    return out


def after_her(turns: list, k: int) -> int:
    """她说的第 k 段之后在两边的话里的位置（旧进度换算用）。"""
    if k <= 0:
        return 0
    seen = 0
    for i, m in enumerate(turns):
        if m["role"] == "user":
            seen += 1
            if seen == k:
                return i + 1
    return len(turns)


def looks_like_code(text: str) -> bool:
    """没有中文、看着是代码 / 命令 / 路径 / 日志的。"""
    if CJK.search(text):
        return False
    s = text.strip()
    if re.fullmatch(r"[\w~./\\:@+-]+", s):  # 一个路径、文件名、链接
        return True
    if s.lower().startswith(COMMANDS) and "\n" not in s:
        return True
    if CODE_HINTS.search(s):
        return True
    letters = sum(ch.isalpha() or ch.isspace() for ch in s)
    return letters / max(len(s), 1) < 0.75


def clean(text: str) -> str:
    """筛一段她说的：不像她说话的返回空串。"""
    text = FENCE.sub(CODE_NOTE, text).strip()
    if _plain_len(text.replace(CODE_NOTE, "")) < MIN_CHARS or looks_like_code(text.replace(CODE_NOTE, "")):
        return ""
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + CUT_NOTE
    return redact(text)


def session_date(path: Path, lines: list) -> str:
    m = re.search(r"rollout-(\d{4}-\d{2}-\d{2})", path.name)
    if m:
        return m.group(1)
    for line in lines[:5]:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        ts = obj.get("timestamp") or (obj.get("payload") or {}).get("timestamp") if isinstance(obj, dict) else None
        if isinstance(ts, str) and re.match(r"\d{4}-\d{2}-\d{2}", ts):
            return ts[:10]
    return clock.her_today(now=datetime.fromtimestamp(path.stat().st_mtime, clock.LOCAL)).isoformat()


def agent_said(text: str, limit: int = MAX_CHARS) -> str:
    """agent 说的一段：代码块略去，纯代码 / 日志不要，截到 limit 字，去掉邮箱电话。"""
    text = FENCE.sub(CODE_NOTE, text or "").strip()
    rest = text.replace(CODE_NOTE, "").strip()
    if not rest or (not CJK.search(rest) and (CODE_HINTS.search(rest) or
                                              sum(ch.isalpha() or ch.isspace() for ch in rest) / len(rest) < 0.75)):
        return ""  # 纯代码 / 日志（短的英文回答照留）
    if len(text) > limit:
        text = text[:limit] + LONG_NOTE
    return redact(text)


def transcript_lines(msgs: list, agent: str, agent_max: int = MAX_CHARS, her=lambda m: m["role"] == "user") -> list:
    """两边的话 → 一行一句、标说话人：【她】…、【Codex】…。她的按 clean 筛，agent 的按 agent_said 筛；筛掉的不出现。"""
    out = []
    for m in msgs:
        said = clean(m["text"]) if her(m) else agent_said(m["text"], agent_max)
        if said:
            out.append(f"【{HER if her(m) else agent}】{said}")
    return out


def pack_transcript(lines: list, header: str, size: int = CHUNK) -> list:
    """整段对话按字数分块，同一会话的块按顺序；每块开头都带这次会话的抬头行（〔……〕）。"""
    out, cur, n = [], [], len(header) + 1
    for ln in lines:
        if cur and n + len(ln) + 1 > size:
            out.append("\n".join([header] + cur))
            cur, n = [], len(header) + 1
        cur.append(ln)
        n += len(ln) + 1
    return out + (["\n".join([header] + cur)] if cur else [])


# ---------- 文件、指纹、进度 ----------
def codex_files(home: Path) -> dict:
    """要存档的：{"agents": [...], "memories": [...], "sessions": [...]}。不碰 auth.json、config.toml（有钥匙）。"""
    home = Path(home)
    agents = [home / "AGENTS.md"] if (home / "AGENTS.md").is_file() else []
    memories = sorted(p for p in (home / "memories").rglob("*") if p.is_file()) if (home / "memories").is_dir() else []
    sessions = []
    for sub in ("sessions", "archived_sessions"):
        if (home / sub).is_dir():
            sessions += sorted((home / sub).rglob("rollout-*.jsonl"))
    return {"agents": agents, "memories": memories, "sessions": sessions}


def signature(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{int(st.st_mtime)}"


def load_state(path: Path = STATE_FILE) -> dict:
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("sessions", {})
    st.setdefault("archived", {})
    return st


def save_state(st: dict, path: Path = STATE_FILE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")


def token(env=os.environ, settings: Path = None) -> str:
    t = env.get("ETHAN_CLOUD_TOKEN", "").strip()
    if t:
        return t
    try:  # launchd 跑的时候没有终端里的环境变量：用 setup_hook 写进 ~/.claude/settings.json 的那把
        e = json.loads(Path(settings or Path.home() / ".claude" / "settings.json").read_text(encoding="utf-8"))["env"]
        return (e.get("ETHAN_CLOUD_TOKEN") or e.get("ETHAN_HOOK_TOKEN") or "").strip()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""


# ---------- 存档 ----------
def archive(paths: list, home: Path, out_dir: Path, key: Path, now: datetime = None) -> str:
    """新增或改过的文件原样打包；有密钥就加密。返回生成的文件路径。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in paths:
            tar.add(str(p), arcname="codex/" + p.relative_to(home).as_posix())
    data = buf.getvalue()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o700)
    name = f"codex-{clock.her_now(now=now):%Y%m%d-%H%M}.tar.gz"
    if key and Path(key).is_file():
        try:
            from ethan.backup import encrypt
            data, name = encrypt(data, Path(key)), name + ".enc"
        except (RuntimeError, OSError):
            pass  # openssl 不能用就先留未加密的（只在她自己的 ~/.config 里）
    f = out_dir / name
    f.write_bytes(data)
    os.chmod(f, 0o600)
    return str(f)


# ---------- 一次导出 ----------
def run(home: Path = CODEX_HOME, mode: str = "dry-run", env=os.environ, state_path: Path = STATE_FILE,
        archive_dir: Path = ARCHIVE_DIR, key: Path = ARCHIVE_KEY, out_dir: Path = OUT_DIR, client=None,
        settings: Path = None, now: datetime = None) -> dict:
    home = Path(home)
    st = load_state(state_path)
    files = codex_files(home)
    every = files["agents"] + files["memories"] + files["sessions"]
    changed = [p for p in every if st["archived"].get(str(p)) != signature(p)]
    todo = []  # (路径, 指纹, 已交段数, 新的交接块, 会话名)
    for p in files["sessions"]:
        sig, old = signature(p), st["sessions"].get(str(p), {})
        if old.get("sig") == sig:
            continue
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        turns = parse_turns(lines)
        sent = old.get("sent", 0)
        if old and old.get("fmt") != "transcript":  # 旧进度数的是她说的段数：换成整段对话里的位置
            sent = after_her(turns, sent)
        sent = sent if sent <= len(turns) else 0
        date = session_date(p, lines)
        parts = pack_transcript(transcript_lines(turns[sent:], "Codex", AGENT_MAX), f"〔Codex 会话 {date}〕")
        todo.append((p, sig, len(turns), parts, f"Codex 会话（{date}）"))
    report = {"files": len(every), "changed": len(changed), "sessions": len(todo),
              "blocks": sum(len(t[3]) for t in todo), "archive": "", "sent": 0, "written": [], "errors": []}
    if mode == "dry-run":
        return report
    if changed:
        report["archive"] = archive(changed, home, archive_dir, key, now)
        for p in changed:
            st["archived"][str(p)] = signature(p)
        save_state(st, state_path)
    tok = token(env, settings)
    if not tok:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for p, _, _, parts, sess in todo:
            for k, part in enumerate(parts, 1):
                f = out_dir / f"{p.stem}-{k}.md"
                f.write_text(part, encoding="utf-8")
                report["written"].append(str(f))
        report["note"] = ("没有钥匙（ETHAN_CLOUD_TOKEN），只生成了文件、没有发；有钥匙后重跑 --send 就会交，"
                          "或者逐个用 python3 -m ethan.cloud interview --session \"Codex 会话（日期）\" "
                          f"--scene \"{SCENE}\" --file <文件> 交")
        return report
    if client is None:
        from ethan.cloud import CloudClient, DEFAULT_URL
        client = CloudClient(tok, env.get("ETHAN_URL", DEFAULT_URL))
    for p, sig, n, parts, sess in todo:
        ok = True
        for part in parts:
            res = client.interview(sess, SCENE, part, fmt="transcript", agent="Codex")
            if not res.get("ok"):
                ok = False
                report["errors"].append(f"{p.name}: {res.get('error', res)}")
                break
            report["sent"] += part.count("\n【她】")
        if ok:  # 一个会话全交成功才记，失败的下次整段重交（服务器对同样的内容不重复记）
            st["sessions"][str(p)] = {"sig": sig, "sent": n, "fmt": "transcript"}
            save_state(st, state_path)
    return report


# ---------- 每周自动跑 ----------
def plist_text(python: str, repo: Path, log: Path = LOG_FILE) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{python}</string><string>-m</string><string>ethan.agentexport</string><string>--codex</string><string>--send</string></array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>StartCalendarInterval</key><dict><key>Weekday</key><integer>0</integer><key>Hour</key><integer>20</integer><key>Minute</key><integer>13</integer></dict>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict></plist>
"""


def install_launchd(plist: Path = PLIST, run_cmd=subprocess.run, platform: str = sys.platform) -> str:
    plist = Path(plist)
    plist.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = plist_text(sys.executable, Path(__file__).resolve().parents[1])
    plist.write_text(text, encoding="utf-8")
    if platform != "darwin":
        return f"{text}\n写好了 {plist}；这台不是 Mac，没有 launchctl load。"
    run_cmd(["launchctl", "unload", str(plist)], capture_output=True)
    run_cmd(["launchctl", "load", str(plist)], check=True)
    return (f"{text}\n装好了：{plist}，每周日 20:13 导出一次 Codex 的记录，日志在 {LOG_FILE}。"
            f"\n不想要了：launchctl unload {plist}")


def main(argv=None, env=os.environ) -> int:
    p = argparse.ArgumentParser(description="导出 agent 的记忆和聊天（ontology-plan 1.8）")
    p.add_argument("--codex", action="store_true", help="导 Codex（~/.codex）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只看有多少新的，不存档不发")
    g.add_argument("--send", action="store_true", help="存档 + 交给 小伴（没钥匙只生成文件）")
    g.add_argument("--install-launchd", action="store_true", help="装每周日 20:13 自动跑（Mac）")
    p.add_argument("--codex-home", type=Path, default=CODEX_HOME)
    p.add_argument("--state-file", type=Path, default=STATE_FILE)
    args = p.parse_args(argv)
    if args.install_launchd:
        print(install_launchd())
        return 0
    if not args.codex:
        p.error("现在只支持 --codex")
    res = run(args.codex_home, "dry-run" if args.dry_run else "send", env, args.state_file)
    print(f"[{clock.stamp(clock.her_now())}] " + json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
