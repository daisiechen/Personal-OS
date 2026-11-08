"""她本人的工作时长（事业领地的健康线：每天不超过 8 小时，超过黄，超过 10 小时红，见 docs/design/territories.md）。

怎么算：Mac 键鼠活跃时间 + 她在 Claude 会话里发消息的时间；agent 自己在跑不算。
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
from . import clock

SLOT_MIN = 5
URL = "https://ethan.example.com/cloud/activity"
SCHEMA = "CREATE TABLE IF NOT EXISTS work_slots (slot TEXT PRIMARY KEY, source TEXT NOT NULL);"
LIMIT_OK, LIMIT_BAD = 8.0, 10.0
PENDING = Path.home() / ".ethan-worktime.json"  # 没发出去的格子先攒在这
PLIST = Path.home() / "Library" / "LaunchAgents" / "me.mia.ethan-worktime.plist"


def slot_of(t: datetime) -> str:
    """按本地时间取 5 分钟格（工作日按本地算）。"""
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


DAY_START = f"{clock.DAY_START_HOUR:02d}:00"  # 一天从早上 5 点算起：凌晨 0–5 点还算前一晚（不该算进今天；常量在 clock）


def day_range(day: date) -> tuple:
    """这一天的格子范围：当天 05:00（含）到第二天 05:00（不含）。"""
    return f"{day.isoformat()}T{DAY_START}", f"{(day + timedelta(days=1)).isoformat()}T{DAY_START}"


def hours(db, day: date) -> float:
    ensure(db)
    n = db.conn.execute("SELECT COUNT(*) FROM work_slots WHERE slot >= ? AND slot < ?", day_range(day)).fetchone()[0]
    return round(n * SLOT_MIN / 60, 1)


def has_data(db) -> bool:
    ensure(db)
    return db.conn.execute("SELECT 1 FROM work_slots LIMIT 1").fetchone() is not None


def light(h: float) -> str:
    return "ok" if h <= LIMIT_OK else "warn" if h <= LIMIT_BAD else "bad"


def week_row(db, day: date) -> dict:
    """健康线一行：今天几小时、本周日均、本周最长那天；灯按本周最差那天算。"""
    start = clock.week_start(day)
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
    """Codex 桌面 App 在沙箱里跑报到脚本，连不了网（日志：curl 退出码 6），脚本就把报到存进 ~/.codex/ethan_spool.jsonl，
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


CURFEW_URL = "https://ethan.example.com/cloud/curfew"
CURFEW_STATE = Path.home() / ".ethan-curfew.json"
CURFEW_EVERY = 15 * 60  # 11 点后还在用：每 15 分钟再弹一次（弹全屏提醒，不锁屏）
FULLSCREEN = r"""
ObjC.import('Cocoa');
var app = $.NSApplication.sharedApplication;
app.setActivationPolicy(1);
var f = $.NSScreen.mainScreen.frame;
var w = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(f, 0, 2, false);
w.setLevel(1000); w.setOpaque(false); w.setIgnoresMouseEvents(false);
w.setBackgroundColor($.NSColor.colorWithCalibratedRedGreenBlueAlpha(0.07, 0.08, 0.17, 0.95));
var W = Math.min(f.size.width - 160, 1100), X = (f.size.width - W) / 2;
function label(text, size, y, h, r, g, b) {  // 自动换行、居中，宽度限在屏幕中间（截图：一行太长显示不全）
  var t = $.NSTextField.wrappingLabelWithString(text);
  t.setFont($.NSFont.boldSystemFontOfSize(size)); t.setTextColor($.NSColor.colorWithCalibratedRedGreenBlueAlpha(r, g, b, 1));
  t.setAlignment(2); t.setFrame($.NSMakeRect(X, y, W, h));
  w.contentView.addSubview(t);
}
label(__TITLE__, 44, f.size.height / 2 + 40, 70, 0.89, 0.75, 0.42);
label(__MSG__, 22, f.size.height / 2 - 60, 90, 0.95, 0.93, 0.88);
label("点任意处关掉。还在用的话，15 分钟后会再提醒。", 16, f.size.height / 2 - 120, 30, 0.67, 0.66, 0.8);
app.activateIgnoringOtherApps(true); w.makeKeyAndOrderFront(null);
var end = Date.now() + 60000;
while (Date.now() < end) {
  var e = app.nextEventMatchingMaskUntilDateInModeDequeue(0xFFFFFFFF, $.NSDate.dateWithTimeIntervalSinceNow(0.2), $.NSDefaultRunLoopMode, true);
  if (e && !e.isNil()) { var ty = e.type; if (ty == 1 || ty == 10 || ty == 3) break; app.sendEvent(e); }
}
w.orderOut(null);
"""


def curfew_check(active: bool, now: float = None, opener=urllib.request.urlopen, run=subprocess.run) -> str:
    """问服务器现在该不该收工：9 点、10 点弹一条通知；11 点后还在用就弹全屏提醒（每 15 分钟一次），不锁屏。"""
    import time as _t
    now = now or _t.time()
    if not active:
        return ""
    req = urllib.request.Request(CURFEW_URL, headers={"User-Agent": "ethan-worktime", "Authorization": f"Bearer {token()}"})
    try:
        with opener(req, timeout=10) as r:
            c = json.loads(r.read().decode("utf-8"))
    except Exception:
        return ""
    try:
        st = json.loads(CURFEW_STATE.read_text())
    except (OSError, ValueError):
        st = {}
    phase = c.get("phase", "ok")
    night = clock.work_day(datetime.now(_tz())).isoformat()  # 按她的时区、凌晨 5 点分天（以前用 Mac 本地时间，写法违规）
    done = ""
    if phase in ("remind", "persuade") and st.get(phase) != night:
        text = {"remind": "9 点了，开始收尾吧，别再开新的了。", "persuade": "10 点了，今天到这儿吧，去做点工作以外的事。"}[phase]
        run(["osascript", "-e", f'display notification "{text}" with title "小伴"'], capture_output=True, timeout=20)
        st[phase] = night
        done = "通知"
    elif c.get("stop") and now - float(st.get("full_at", 0)) >= CURFEW_EVERY:
        full = c.get("message") or "已经 11 点了，今天到这儿。"
        head, _, rest = full.partition("。")  # 第一句做大标题，其余换行小字（一行太长显示不全）
        title, msg = json.dumps(head + "。", ensure_ascii=False), json.dumps(rest.strip(), ensure_ascii=False)
        out = run(["osascript", "-l", "JavaScript", "-e", FULLSCREEN.replace("__TITLE__", title).replace("__MSG__", msg)],
                  capture_output=True, timeout=75)
        if getattr(out, "returncode", 0) != 0:  # 全屏没弹出来就退回系统提醒框
            run(["osascript", "-e", f"display alert \"小伴\" message {json.dumps(full, ensure_ascii=False)}"], capture_output=True, timeout=75)
        st["full_at"] = now
        done = "全屏提醒"
    CURFEW_STATE.write_text(json.dumps(st))
    return done


def once(now: datetime = None, idle=idle_seconds, opener=urllib.request.urlopen) -> str:
    """看一眼：这 5 分钟动过键鼠就记一格；连同之前没发出去的一起发。"""
    now = now or datetime.now(timezone.utc)
    try:
        pending = json.loads(PENDING.read_text())
    except (OSError, ValueError):
        pending = []
    active = idle() < SLOT_MIN * 60
    if active:
        pending.append(slot_of(now))
    pending = sorted(set(pending))[-2000:]
    if pending and send(pending, opener):
        pending = []
    PENDING.write_text(json.dumps(pending))
    try:
        codex = flush_codex(opener)
    except Exception:
        codex = 0
    try:
        curfew = curfew_check(active, opener=opener)
    except Exception:
        curfew = ""
    return f"待发 {len(pending)} 格" + (f"，替 Codex 报到 {codex} 条" if codex else "") + (f"，{curfew}" if curfew else "")


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
    codex = install_codex_guard()
    return f"装好了：{PLIST}，每 5 分钟记一次（11 点后还在用会弹全屏提醒）。{codex}不想记了：launchctl unload {PLIST}"


CODEX_GUARD = """# ethan-curfew 开始：晚上 11 点后开新的 codex 先问一句（python3 -m ethan.worktime --install 装的）
# 她原来给 codex 设了别名：先记下别名的内容再取消，不然 zsh 报 defining function based on alias
__ethan_codex_alias="${aliases[codex]:-${BASH_ALIASES[codex]:-}}"
unalias codex 2>/dev/null
function codex {
  local t r
  t=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.claude/settings.json")))["env"]["ETHAN_HOOK_TOKEN"])' 2>/dev/null)
  if [ -n "$t" ]; then
    r=$(curl -s -m 4 -H "Authorization: Bearer $t" https://ethan.example.com/cloud/curfew)
    if printf '%s' "$r" | python3 -c 'import json,sys;sys.exit(0 if json.load(sys.stdin).get("stop") else 1)' 2>/dev/null; then
      printf '%s' "$r" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("message",""))'
      return 1
    fi
  fi
  if [ -n "$__ethan_codex_alias" ]; then
    eval "$__ethan_codex_alias \\"\\$@\\""
  else
    command codex "$@"
  fi
}
# ethan-curfew 结束
"""


def install_codex_guard(rc: Path = None) -> str:
    rc = rc or Path.home() / ".zshrc"
    old = rc.read_text(encoding="utf-8") if rc.exists() else ""
    old = re.sub(r"# ethan-curfew 开始.*?# ethan-curfew 结束\n", "", old, flags=re.S)
    rc.write_text(old.rstrip("\n") + "\n\n" + CODEX_GUARD, encoding="utf-8")
    return f"终端里的 codex 也装好了 11 点检查（{rc}，新开的终端窗口生效）。"


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
