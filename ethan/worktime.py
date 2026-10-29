"""她本人的工作时长（事业领地的健康线：每天不超过 8 小时，超过黄，超过 10 小时红，见 docs/design/territories.md）。

怎么算（2026-10-27 她定）：Mac 键鼠活跃时间 + 她在 Claude 会话里发消息的时间；agent 自己在跑不算。
- Mac 上每 5 分钟看一次键鼠空闲了多久（ioreg 的 HIDIdleTime），这 5 分钟里动过就算一格，攒着发给接收口。
- Claude 会话报到钩子里她发话（UserPromptSubmit，where=mac）也算一格。
- 服务器按 5 分钟一格去重存进 work_slots，一格 = 5 分钟。

Mac 上装（每 5 分钟跑一次，launchd）：python3 -m ethan.worktime --install
不装也行，手动看一次：python3 -m ethan.worktime --once
钥匙：读 ~/.claude/settings.json 里装钩子时写进去的 ETHAN_HOOK_TOKEN，不另外要她录。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SLOT_MIN = 5
URL = "https://ethan.example.com/cloud/activity"
SCHEMA = "CREATE TABLE IF NOT EXISTS work_slots (slot TEXT PRIMARY KEY, source TEXT NOT NULL);"
LIMIT_OK, LIMIT_BAD = 8.0, 10.0
PENDING = Path.home() / ".ethan-worktime.json"  # 没发出去的格子先攒在这
PLIST = Path.home() / "Library" / "LaunchAgents" / "me.mia.ethan-worktime.plist"


def slot_of(t: datetime) -> str:
    """按伦敦本地时间取 5 分钟格（她在伦敦，工作日按本地算）。"""
    t = t.astimezone(_tz())
    return t.replace(minute=t.minute - t.minute % SLOT_MIN, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


def _tz():
    try:
        from .clock import LOCAL
        return LOCAL
    except Exception:  # 没有时区库就用 UTC
        return timezone.utc


# ---------- 服务器这边 ----------
def ensure(db):
    db.conn.executescript(SCHEMA)


def record(db, slots, source: str) -> int:
    ensure(db)
    n = 0
    for s in slots:
        s = str(s)[:16]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
            n += db.conn.execute("INSERT OR IGNORE INTO work_slots (slot, source) VALUES (?, ?)", (s, source[:20])).rowcount
    db.conn.commit()
    return n


def hours(db, day: date) -> float:
    ensure(db)
    n = db.conn.execute("SELECT COUNT(*) FROM work_slots WHERE slot LIKE ?", (day.isoformat() + "%",)).fetchone()[0]
    return round(n * SLOT_MIN / 60, 1)


def has_data(db) -> bool:
    ensure(db)
    return db.conn.execute("SELECT 1 FROM work_slots LIMIT 1").fetchone() is not None


def light(h: float) -> str:
    return "ok" if h <= LIMIT_OK else "warn" if h <= LIMIT_BAD else "bad"


def week_row(db, day: date) -> dict:
    """健康线一行：今天几小时、本周日均、本周最长那天；灯按本周最差那天算。"""
    start = day - timedelta(days=day.weekday())
    days = [start + timedelta(days=i) for i in range(day.weekday() + 1)]
    hs = [hours(db, d) for d in days]
    worked = [h for h in hs if h > 0]
    avg = round(sum(worked) / len(worked), 1) if worked else 0
    worst = max(hs) if hs else 0
    return {"today": hs[-1], "avg": avg, "max": worst, "light": light(worst)}


# ---------- Mac 这边 ----------
def idle_seconds() -> float:
    out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=10).stdout
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m.group(1)) / 1e9 if m else 1e9


def token() -> str:
    t = os.environ.get("ETHAN_HOOK_TOKEN") or os.environ.get("ETHAN_CLOUD_TOKEN")
    if t:
        return t
    try:
        return json.loads((Path.home() / ".claude" / "settings.json").read_text())["env"]["ETHAN_HOOK_TOKEN"]
    except (OSError, ValueError, KeyError):
        return ""


def send(slots: list, opener=urllib.request.urlopen) -> bool:
    req = urllib.request.Request(URL, method="POST", data=json.dumps({"slots": slots, "source": "mac"}).encode(),
                                 headers={"Content-Type": "application/json", "User-Agent": "ethan-worktime",
                                          "Authorization": f"Bearer {token()}"})
    try:
        with opener(req, timeout=15) as r:
            return getattr(r, "status", 200) < 300
    except Exception:
        return False


CODEX_SPOOL = Path.home() / ".codex" / "ethan_spool.jsonl"
REPORT_URL = "https://ethan.example.com/cloud/agent/report"


def flush_codex(opener=urllib.request.urlopen, spool: Path = None) -> int:
    """Codex 桌面 App 在沙箱里跑报到脚本，连不了网（2026-10-29 日志：curl 退出码 6），脚本就把报到存进 ~/.codex/ethan_spool.jsonl，
    这里（每 5 分钟、在沙箱外）替它发出去；发成功的删掉，没发成功的留着下次。返回发出去几条。"""
    spool = spool or CODEX_SPOOL
    try:
        lines = [l for l in spool.read_text(encoding="utf-8").split("\n") if l.strip()]
    except OSError:
        return 0
    key = spool.with_name("ethan_token")
    tok = key.read_text().strip() if key.exists() else token()
    left, sent = [], 0
    for line in lines:
        req = urllib.request.Request(REPORT_URL, method="POST", data=line.encode("utf-8"),
                                     headers={"Content-Type": "application/json", "User-Agent": "ethan-hook",
                                              "Authorization": f"Bearer {tok}"})
        try:
            with opener(req, timeout=15) as r:
                ok = getattr(r, "status", 200) < 300
        except Exception:
            ok = False
        if ok:
            sent += 1
        elif len(left) < 500:
            left.append(line)
    spool.write_text("".join(l + "\n" for l in left), encoding="utf-8")
    return sent


def once(now: datetime = None, idle=idle_seconds, opener=urllib.request.urlopen) -> str:
    """看一眼：这 5 分钟动过键鼠就记一格；连同之前没发出去的一起发。"""
    now = now or datetime.now(timezone.utc)
    try:
        pending = json.loads(PENDING.read_text())
    except (OSError, ValueError):
        pending = []
    if idle() < SLOT_MIN * 60:
        pending.append(slot_of(now))
    pending = sorted(set(pending))[-2000:]
    if pending and send(pending, opener):
        pending = []
    PENDING.write_text(json.dumps(pending))
    try:
        codex = flush_codex(opener)
    except Exception:
        codex = 0
    return f"待发 {len(pending)} 格" + (f"，替 Codex 报到 {codex} 条" if codex else "")


def install() -> str:
    repo = Path(__file__).resolve().parents[1]
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>me.mia.ethan-worktime</string>
  <key>ProgramArguments</key><array><string>{sys.executable}</string><string>-m</string><string>ethan.worktime</string><string>--once</string></array>
  <key>WorkingDirectory</key><string>{repo}</string>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardErrorPath</key><string>/tmp/ethan-worktime.log</string>
</dict></plist>
""")
    subprocess.run(["launchctl", "unload", str(PLIST)], capture_output=True)
    subprocess.run(["launchctl", "load", str(PLIST)], check=True)
    return f"装好了：{PLIST}，每 5 分钟记一次。不想记了：launchctl unload {PLIST}"


def copy_token(run=subprocess.run) -> str:
    t = token()
    if not t:
        return "这台 Mac 上没找到钥匙：先装会话报到钩子（python3 -m ethan.setup_hook --where mac --ssh my-server）"
    run(["pbcopy"], input=t, text=True, check=True)
    return "钥匙已复制到剪贴板（没有显示出来），用“通用剪贴板”粘到 iPhone 快捷指令里，粘完随便复制点别的盖掉。"


def main():
    p = argparse.ArgumentParser(description="她本人的工作时长（Mac 键鼠活跃）")
    p.add_argument("--once", action="store_true")
    p.add_argument("--install", action="store_true")
    p.add_argument("--copy-token", action="store_true", help="把钥匙复制到剪贴板（给 iPhone 快捷指令用），不打印出来")
    a = p.parse_args()
    if a.copy_token:
        print(copy_token())
    else:
        print(install() if a.install else once())


if __name__ == "__main__":
    main()
