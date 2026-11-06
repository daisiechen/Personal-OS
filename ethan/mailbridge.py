"""她 Mac 上的邮件程序（服务器那半在 ethan/mail.py）。

每 10 分钟（launchd）：
1. 用 AppleScript 读“邮件”App 统一收件箱里最近 3 天收到、还没处理过的邮件：发件人、主题、时间、正文开头 3000 字。
2. 发到 https://ethan.example.com/cloud/mail（钥匙同 Claude 报到钩子，读 ~/.claude/settings.json，不另外要她录）。
3. 服务器判好的，照办：不需要她看的，移进该账户下的“小伴整理/<类别>”文件夹并标已读；需要她看的，留在收件箱、不动。
   还没判完的，下一轮再问。
只移动和标已读：不删、不发、不回。每次移动记进 ~/mail-cleanup/moves-auto.csv（原文件夹、新文件夹、原来是否已读），能撤回。

装（她在 Mac 上跑一次；第一次运行时 macOS 会问“允许控制‘邮件’吗”，点允许）：
    python3 -m ethan.mailbridge --install
先看看会做什么、不真的移动：python3 -m ethan.mailbridge --once --dry-run
撤回最近的自动移动：python3 -m ethan.mailbridge --undo
"""

import argparse
import csv
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://ethan.example.com/cloud/mail"
STATE = Path.home() / ".ethan-mail.json"
LOG = Path.home() / "mail-cleanup" / "moves-auto.csv"
PLIST = Path.home() / "Library" / "LaunchAgents" / "me.mia.ethan-mail.plist"
DAYS = 3
US, RS = "\x1f", "\x1e"

LIST_SCRIPT = """on run argv
  set cutoff to (current date) - ((item 1 of argv) as integer) * days
  set known to item 2 of argv
  set US to ASCII character 31
  set RS to ASCII character 30
  set out to ""
  tell application "Mail"
    set msgs to (messages of inbox whose date received > cutoff)
    repeat with m in msgs
      set mid to message id of m
      set acc to name of account of mailbox of m
      if known does not contain ("|" & mid & "|") then
        set c to ""
        try
          set c to content of m
          if length of c > 3000 then set c to text 1 thru 3000 of c
        end try
        set out to out & mid & US & acc & US & (sender of m) & US & (subject of m) & US & ((date received of m) as «class isot» as string) & US & ((read status of m) as string) & US & c & RS
      end if
    end repeat
  end tell
  return out
end run"""

MOVE_SCRIPT = """on run argv
  set mid to item 1 of argv
  set accName to item 2 of argv
  set boxName to item 3 of argv
  set markRead to (item 4 of argv) is "1"
  tell application "Mail"
    set acc to account accName
    set m to first message of inbox whose message id is mid
    set wasRead to read status of m
    set fromBox to name of mailbox of m
    try
      set target to mailbox boxName of acc
    on error
      set target to make new mailbox with properties {name:boxName} at acc
    end try
    if markRead then set read status of m to true
    move m to target
  end tell
  return fromBox & "|" & (wasRead as string)
end run"""

UNDO_SCRIPT = """on run argv
  set mid to item 1 of argv
  set accName to item 2 of argv
  set boxName to item 3 of argv
  set backTo to item 4 of argv
  set wasRead to (item 5 of argv) is "true"
  tell application "Mail"
    set acc to account accName
    set m to first message of mailbox boxName of acc whose message id is mid
    set read status of m to wasRead
    move m to mailbox backTo of acc
  end tell
end run"""


def osa(script: str, args, run=subprocess.run) -> str:
    proc = run(["osascript", "-", *map(str, args)], input=script, capture_output=True, text=True, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300])
    return proc.stdout.rstrip("\n")


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {"done": [], "pending": []}


def save_state(state: dict):
    state["done"] = state["done"][-5000:]  # 只要记得最近的就够（只看 3 天内的邮件）
    STATE.write_text(json.dumps(state, ensure_ascii=False))


def parse_list(out: str) -> list:
    items = []
    for rec in out.split(RS):
        f = rec.split(US)
        if len(f) < 7 or not f[0].strip():
            continue
        items.append({"id": f[0].strip(), "account": f[1], "sender": f[2], "subject": f[3], "received_at": f[4],
                      "was_read": f[5] == "true", "body": US.join(f[6:])[:3000]})
    return items


def token() -> str:
    from .worktime import token as t
    return t()


def post(payload: dict, opener=urllib.request.urlopen) -> dict:
    req = urllib.request.Request(URL, data=json.dumps(payload, ensure_ascii=False).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token()}"})
    with opener(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def log_move(row: list):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG.exists()
    with LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["key", "account", "message_id", "from", "to", "was_read", "at"])
        w.writerow(row)


def once(dry_run: bool = False, run=subprocess.run, opener=urllib.request.urlopen) -> str:
    state = load_state()
    ids = [k[len("msgid:"):] for k in state["done"] + state["pending"]]
    known = "|" + "|".join(ids + [f"<{i}>" for i in ids]) + "|"  # “邮件”App 给的 Message-ID 有时带尖括号
    found = parse_list(osa(LIST_SCRIPT, [DAYS, known], run))
    by_key = {"msgid:" + i["id"].strip("<>"): i for i in found}
    where = state.setdefault("where", {})  # key → [账户, Message-ID]，还在判的下一轮要用
    for k, i in by_key.items():
        where[k] = [i["account"], i["id"]]
    reply = post({"items": [{k: v for k, v in i.items() if k != "was_read"} for i in found], "ask": state["pending"]}, opener)
    moved = kept = 0
    for a in reply.get("actions", []):
        key = a["key"]
        if key not in where:
            continue
        account, mid = where[key]
        if not a.get("keep"):
            if dry_run:
                print(f"会移到 {a['folder']}{'，标已读' if a.get('mark_read') else ''}：{by_key.get(key, {}).get('subject', key)}")
            else:
                try:
                    fr, _, was_read = osa(MOVE_SCRIPT, [mid, account, a["folder"], "1" if a.get("mark_read") else "0"], run).partition("|")
                    log_move([key, account, mid, fr, a["folder"], was_read, datetime.now(timezone.utc).isoformat(timespec="seconds")])
                except RuntimeError as e:  # 邮件已被她移走或删掉：跳过
                    print(f"没移成：{key}：{e}", file=sys.stderr)
            moved += 1
        else:
            kept += 1
        if not dry_run:
            state["done"].append(key)
            if key in state["pending"]:
                state["pending"].remove(key)
            where.pop(key, None)
    if not dry_run:
        done = set(state["done"])
        state["pending"] = sorted((set(state["pending"]) | set(reply.get("pending", []))) - done)
        save_state(state)
    return f"新邮件 {len(found)} 封；移走 {moved} 封，留给她看 {kept} 封，还在判 {len(reply.get('pending', []))} 封" + ("（演练，没真的移动）" if dry_run else "")


def undo(hours: float = 24, run=subprocess.run) -> str:
    """把最近 hours 小时里自动移走的邮件放回原处，恢复原来的已读状态。"""
    if not LOG.exists():
        return "没有可撤回的"
    rows = list(csv.DictReader(LOG.open(encoding="utf-8")))
    cut = datetime.now(timezone.utc).timestamp() - hours * 3600
    n = 0
    for r in reversed(rows):
        if datetime.fromisoformat(r["at"]).timestamp() < cut:
            continue
        try:
            osa(UNDO_SCRIPT, [r["message_id"], r["account"], r["to"], r["from"], r["was_read"]], run)
            n += 1
        except RuntimeError as e:
            print(f"没放回：{r['key']}：{e}", file=sys.stderr)
    return f"放回了 {n} 封"


def install() -> str:
    repo = Path(__file__).resolve().parents[1]
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>me.mia.ethan-mail</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>ethan.mailbridge</string><string>--once</string></array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>StartInterval</key><integer>600</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>/tmp/ethan-mail.log</string>
  <key>StandardErrorPath</key><string>/tmp/ethan-mail.log</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    return f"装好了：每 10 分钟看一次新邮件。日志在 /tmp/ethan-mail.log。停掉：launchctl unload {PLIST}"


def main():
    p = argparse.ArgumentParser(description="Mac 上的邮件程序")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--install", action="store_true")
    p.add_argument("--undo", action="store_true")
    a = p.parse_args()
    if a.install:
        print(install())
    elif a.undo:
        print(undo())
    else:
        print(once(dry_run=a.dry_run))


if __name__ == "__main__":
    main()
