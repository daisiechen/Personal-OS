"""DemoApp 举报提醒（2026-10-29 她同意）：DemoApp 的 worker 推数字和后台链接过来，小伴 在 Telegram 提醒她去复核。

举报的 24 小时复核由她本人负责，小伴 只负责提醒：看不到举报内容，也不做任何处理。
接口 POST /fancy/moderation，密钥 INBOX_TOKEN_FANCY（只能让 小伴 发这类提醒，碰不到记忆和聊天）。

正文契约（DemoApp 侧定，未知字段忽略，必填字段缺失或类型不对返回 400）：
  {"kind": "morning"|"evening"|"urgent", "sentAt": ISO8601, "open": int, "autoLimited": int, "overdue": int,
   "dueWithin6h": int, "oldestHoursLeft": number|null, "urgentOpen": int,
   "reason": "safety"|"harassment"|"under_18"|null, "count": int, "adminUrl": "https://demoapp.example/..."}

收到后直接用 Telegram Bot API 发给她（同 report.py 的发法），不经 小伴 的大脑，所以不进聊天原文、
记忆、Wiki、ethan-vault 和 小伴 的聊天上下文；也不写收件箱 inbox/（relay.py、brain.py 只读那里）。
只在 <数据目录>/fancy-moderation.jsonl 记一行不含内容的日志：时间、kind、数字、结果。

去重：同一 kind 10 分钟内重复到达只发第一条；urgent 在 10 分钟内按 count 合并——count 比已提醒过的大才再发一条
（报新的总数），否则不发。count 按 DemoApp 这一批紧急举报的条数理解。
"""

import json
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

KINDS = ("morning", "evening", "urgent")
REASONS = {"safety": "人身安全", "harassment": "骚扰或仇恨", "under_18": "疑似未满 18 岁"}
INT_FIELDS = ("open", "autoLimited", "overdue", "dueWithin6h", "urgentOpen", "count")
MAX_COUNT = 1_000_000
MAX_HOURS = 10_000
MAX_URL = 2048
MAX_SENT_AT = 64
HOST = "demoapp.example"
DEDUPE = timedelta(minutes=10)
LOG = "fancy-moderation.jsonl"
STATE = "fancy-moderation-state.json"


def validate(data) -> dict:
    """严格校验正文，返回只含契约字段的干净 dict；不合格抛 ValueError（说明哪一项）。"""
    if not isinstance(data, dict):
        raise ValueError("body must be an object")
    out = {}
    if data.get("kind") not in KINDS:
        raise ValueError("bad kind")
    out["kind"] = data["kind"]
    sent_at = data.get("sentAt")
    if not isinstance(sent_at, str) or not sent_at or len(sent_at) > MAX_SENT_AT:
        raise ValueError("bad sentAt")
    try:
        parsed = datetime.fromisoformat(sent_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("bad sentAt") from None
    out["sentAt"] = (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).isoformat(timespec="seconds")
    for name in INT_FIELDS:
        value = data.get(name)
        if type(value) is not int or not 0 <= value <= MAX_COUNT:  # bool 不算数
            raise ValueError(f"bad {name}")
        out[name] = value
    hours = data.get("oldestHoursLeft", ...)
    if hours is ...:
        raise ValueError("bad oldestHoursLeft")
    if hours is not None and (type(hours) not in (int, float) or not math.isfinite(hours) or abs(hours) > MAX_HOURS):
        raise ValueError("bad oldestHoursLeft")
    out["oldestHoursLeft"] = hours
    reason = data.get("reason", ...)
    if reason is not None and (not isinstance(reason, str) or reason not in REASONS):
        raise ValueError("bad reason")
    out["reason"] = reason
    out["adminUrl"] = _admin_url(data.get("adminUrl"))
    return out


def _admin_url(url) -> str:
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
        raise ValueError("bad adminUrl")
    if any(ord(c) <= 32 or ord(c) == 127 for c in url):
        raise ValueError("bad adminUrl")
    try:
        parts = urlsplit(url)
        parts.port  # 端口写坏了会抛 ValueError
    except ValueError:
        raise ValueError("bad adminUrl") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username is not None or parts.password is not None \
            or not (host == HOST or host.endswith("." + HOST)):
        raise ValueError("bad adminUrl")
    return url


def _hours(h) -> str:
    if h < 1:
        return "不到 1 小时"
    return f"{round(h)} 小时"


def message(alert: dict) -> str:
    """给她的一句话；没有要她看的就返回空串（不发）。"""
    kind, url = alert["kind"], alert["adminUrl"]
    if kind == "urgent":
        n = max(alert["count"], 1)
        what = "一条" if n == 1 else f" {n} 条"
        reason = REASONS.get(alert["reason"])
        text = f"DemoApp 收到{what}紧急举报{f'（{reason}）' if reason else ''}，请尽快看。"
    elif kind == "morning":
        if not alert["open"]:
            return ""
        text = f"DemoApp 有 {alert['open']} 条举报待看"
        if alert["autoLimited"]:
            text += f"（{alert['autoLimited']} 条已自动限制）"
        if alert["overdue"]:
            text += f"，{alert['overdue']} 条已经超过 24 小时"
        elif alert["oldestHoursLeft"] is not None:
            left = alert["oldestHoursLeft"]
            text += "，最早一条已经到 24 小时了" if left <= 0 else f"，最早一条还剩 {_hours(left)}"
        text += "。"
    else:  # evening
        due, overdue = alert["dueWithin6h"], alert["overdue"]
        if due:
            text = f"DemoApp 还有 {due} 条举报快到 24 小时{f'（{overdue} 条已超时）' if overdue else ''}。"
        elif overdue:
            text = f"DemoApp 有 {overdue} 条举报已经超过 24 小时，记得看一下。"
        else:
            return ""
    return f"{text}\n→ {url}"


class Alerts:
    def __init__(self, root: Path, notify=None):
        """notify(text) -> bool：把消息发到她的 Telegram；没配就是 None。"""
        self.root = Path(root)
        self.notify = notify
        self.lock = threading.Lock()  # 接收口多线程：查重、发送、记状态一次只让一个线程做，免得重复提醒

    def handle(self, data, now: datetime = None) -> tuple:
        try:
            alert = validate(data)
        except ValueError as e:
            return 400, {"ok": False, "error": str(e)}
        now = now or datetime.now(timezone.utc)
        with self.lock:
            text = message(alert)
            if not text:
                self._log(alert, now, "nothing")
                return 200, {"ok": True, "sent": False, "why": "nothing to review"}
            state = self._state()
            last = state.get(alert["kind"])
            fresh = last and now - datetime.fromisoformat(last["at"]) < DEDUPE
            if fresh and (alert["kind"] != "urgent" or alert["count"] <= last.get("count", 0)):
                self._log(alert, now, "duplicate")
                return 200, {"ok": True, "sent": False, "why": "duplicate"}
            if self.notify is None:
                self._log(alert, now, "no telegram")
                return 503, {"ok": False, "error": "telegram not configured"}
            if not self.notify(text):
                self._log(alert, now, "failed")
                return 502, {"ok": False, "error": "telegram send failed"}
            state[alert["kind"]] = {"at": (last["at"] if fresh else now.isoformat(timespec="seconds")),
                                    "count": alert["count"]}  # urgent 合并时窗口从第一条算起，不因为合并一直顺延
            self._save(state)
            self._log(alert, now, "sent")
            return 200, {"ok": True, "sent": True}

    def _state(self) -> dict:
        try:
            return json.loads((self.root / STATE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict):
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / STATE).write_text(json.dumps(state), encoding="utf-8")

    def _log(self, alert: dict, now: datetime, result: str):
        """只记时间、kind 和数字，不记链接和任何内容。"""
        entry = {"at": now.isoformat(timespec="seconds"), "kind": alert["kind"], "sentAt": alert["sentAt"],
                 **{k: alert[k] for k in INT_FIELDS}, "oldestHoursLeft": alert["oldestHoursLeft"],
                 "result": result}
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")


def telegram_notifier(data_dir: Path, token: str = None, api=None):
    """接收口进程里给她发 Telegram：直接调 Bot API（同 report.py），不进 小伴 的聊天记录。没配 bot 就返回 None。"""
    from .channels.telegram import TelegramAPI
    from .core.router import Settings

    token = token if token is not None else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return None
    api = api or TelegramAPI(token)
    settings = Settings(Path(data_dir) / "settings.json")

    def send(text: str) -> bool:
        chat_id = settings.get("telegram_owner_chat_id")  # 每次现读：绑定可能在接收口启动之后
        if chat_id is None:
            return False
        try:
            api.call("sendMessage", timeout=15, chat_id=chat_id, text=text, disable_web_page_preview=True)
            return True
        except Exception as e:  # 不把 token 打进日志：只记异常类型
            print(f"[fancy] Telegram 发送失败：{type(e).__name__}", flush=True)
            return False

    return send
