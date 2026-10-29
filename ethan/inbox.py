"""接收口：外部只能往这里送数据，送来的内容只当信息，不当指令。

只监听服务器本机 127.0.0.1:8787，经 Cloudflare Tunnel 对外暴露为 https://ethan.example.com，服务器不开新端口。
目前收两类（2026-10-26 设计第 9.7 节）：
  POST /grok/result   Grok Bot 办完 小伴 交代的事，回报结果
  POST /grok/memory   Grok 里的那个 小伴 和她聊出的新事、讲过的新故事
请求头 Authorization: Bearer <INBOX_TOKEN_GROK>；正文是 JSON（取 text / result / content / message / task 字段）或纯文本，最大 64KB。

Claude 云端开发会话登不上服务器，经这里找 Grok（2026-10-27 她同意），用另一把密钥 INBOX_TOKEN_CLOUD：
  POST /cloud/grok           正文 {"task": "..."}，转交 Grok Bot，每天最多 CLOUD_DAILY_LIMIT 条
  GET  /cloud/grok/results   Grok 回报里第一行带【Claude 会话】的那些（可加 ?since=ISO 时间），不给别的回报
这把密钥漏了，别人最多能给 Grok 派活、读这类回报，碰不到记忆和聊天。客户端见 ethan/cloud.py。

Agent 任务队列（2026-10-28，见 ethan/agents.py），同样用 INBOX_TOKEN_CLOUD：
  POST /cloud/agent/tasks               派活 {"agent","title","spec","verify","size","project"}
  GET  /cloud/agent/tasks?status=&agent= 列表（不含改动内容）
  GET  /cloud/agent/tasks/<编号>         全部内容，含 Codex 的改动（patch）和日志
  POST /cloud/agent/tasks/<编号>/review  审查 {"decision": "accept"|"reject", "note"}
  POST /cloud/agent/tasks/<编号>/retry   失败的活重新排队，不记返工
  POST /cloud/agent/done                记一件已做完的活（Claude 自己的活等）
  POST /cloud/agent/start               自己开工一件活，马上显示“在干” {"agent","title","size","project"}
  POST /cloud/agent/tasks/<编号>/finish  自己的活收尾 {"status": "accepted"|"review", "note"}
  GET  /cloud/agent/scoreboard          最近 7 天的成绩单
  POST /cloud/agent/report              任何 agent 自己报到 {"agent","id","title","state","summary","where"}（Codex 的 notify 钩子等）
  POST /cloud/agent/hook?where=mac|server|cloud  Claude Code 钩子报到（会话开始、她发话、一轮做完、结束），
                                        正文是钩子原样的 JSON；密钥 INBOX_TOKEN_HOOK（只能报到）或 INBOX_TOKEN_CLOUD
  POST /cloud/activity                  她本人的工作时长：Mac 键鼠活跃的 5 分钟格 {"slots": ["2026-10-28T10:05"], "source": "mac"}（ethan/worktime.py）
  POST /cloud/place                     iPhone 定位：出门 / 到家 {"event": "leave"|"arrive", "place": "home"}（ethan/places_log.py）
  POST /cloud/agent/sessions            她 Claude 账号里各会话的快照，整份覆盖 {"sessions": [{"title","state","where","updated_at","summary"}]}
  GET  /cloud/maps/<文件名>               领地地图底图（<数据目录>/maps/ 下的 jpg/png），给云端会话取图做设计稿

DemoApp 举报提醒（2026-10-29 她同意，见 ethan/fancy_moderation.py），用单独的密钥 INBOX_TOKEN_FANCY：
  POST /fancy/moderation   DemoApp 的 worker 推只含数字和后台链接的 JSON，小伴 在 Telegram 提醒她去复核
这把密钥漏了，别人最多能让 小伴 给她发这类提醒，碰不到记忆和聊天；这类消息不进收件箱、Wiki 和聊天上下文。

收到的内容：原样记进 <数据目录>/inbox/<日期>.jsonl，再追加到 Wiki 的 from-grok 页（随看板同步到 ethan-vault），
服务器上的 小伴 聊天时能看到最近几条。服务器 小伴 是唯一写记忆的一方。

运行：python3 -m ethan.inbox --data <数据目录>
"""

import argparse
import base64
import hmac
import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import agents, english, fancy_moderation, finance, places_log, worktime
from .grok import LOG_FILE, build_grok
from .memory import MemoryStore

ROUTES = {"/grok/result": ("grok", "Grok Bot 办完"), "/grok/memory": ("grok", "Grok 里的 小伴")}
MAX_BODY = 64 * 1024
HOOK_MAX_BODY = 1024 * 1024  # 钩子原样发来，一轮做完的那条带着最后一段回复，可能很长；只取前面几个字
MAX_TEXT = 2000
SAVED_MARK = "【收藏日报】"  # Grok 每天 4 点整理她的点赞收藏（docs/design/saved-feed.md）：整条留着，进第二大脑，不进 Wiki
PREP_MARK = "【准备 #"  # 准备员托 Grok 查的（ethan/prep.py）：整条留着，不进 Wiki
ENGLISH_MARK = english.MARK  # Grok 陪练英语的回报（ethan/english.py）：记打卡和词句，不进 Wiki
PAGE = "from-grok"
PAGE_TITLE = "Grok 那边的回报"
CLOUD_MARK = "【Claude 会话】"
CLOUD_CHANNEL = "claude-session"
CLOUD_DAILY_LIMIT = 30
CLOUD_REPORT_TO = ("这是 Claude 开发会话交代的事，不是 Mia 在聊天里说的。办完把结果回报到 小伴 的收件箱 "
                   f"https://ethan.example.com/grok/result（用原来的收件箱密钥），回报正文第一行写“{CLOUD_MARK}”；"
                   "不用告诉 Mia，除非要她付钱、登录或做决定。")


class Inbox:
    def __init__(self, store: MemoryStore, tokens: dict, grok=None, notify=None):
        """tokens：{来源: 密钥}，grok 给 Grok 回报用，cloud 给 Claude 云端会话用，fancy 给 DemoApp 举报提醒用；
        grok 是 GrokWebhook（没配就是 None）；notify(text) -> bool 给她发 Telegram（DemoApp 提醒用，没配就是 None）。"""
        self.store = store
        self.fancy = fancy_moderation.Alerts(store.root, notify)
        self.tokens = {k: v for k, v in tokens.items() if v}
        self.grok = grok
        self.dir = Path(store.root) / "inbox"
        self.wiki_lock = threading.Lock()
        self.db_lock = threading.Lock()  # 接收口多线程，数据库一次只让一个线程用

    def _authorized(self, source: str, headers: dict) -> bool:
        token = self.tokens.get(source)
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        return bool(token) and hmac.compare_digest(auth.encode(), f"Bearer {token}".encode())

    def handle(self, path: str, headers: dict, body: bytes, now: datetime = None) -> tuple:
        """返回 (HTTP 状态码, 回给对方的 JSON)。"""
        if path == "/fancy/moderation":
            return self._fancy(headers, body, now)
        if path == "/cloud/grok":
            return self._cloud_task(headers, body, now or datetime.now(timezone.utc))
        if urlsplit(path).path in ("/cloud/activity", "/cloud/place", "/cloud/spend"):
            return self._device(urlsplit(path).path, headers, body, now)
        if urlsplit(path).path == "/cloud/agent/hook":
            return self._hook(parse_qs(urlsplit(path).query), headers, body, now)
        if path.startswith("/cloud/agent/"):
            return self._agent("POST", path, {}, headers, body)
        if path not in ROUTES:
            return 404, {"ok": False, "error": "not found"}
        source, label = ROUTES[path]
        if not self._authorized(source, headers):
            return 401, {"ok": False, "error": "unauthorized"}
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "error": "too large"}
        text = extract_text(body, MAX_BODY)  # 先整条取出来再看标记：JSON 里中文可能是 \u 转义，看原始字节认不出
        if not any(m in text[:200] for m in (SAVED_MARK, PREP_MARK, ENGLISH_MARK, CLOUD_MARK)):  # 给开发会话的调研也常常很长
            text = text[:MAX_TEXT]
        if not text:
            return 400, {"ok": False, "error": "empty"}
        if path == "/grok/result":
            text = order_marks(text)
        now = now or datetime.now(timezone.utc)
        self._record(path, label, text, now)
        if path == "/grok/result" and text.startswith(ENGLISH_MARK):
            try:
                with self.db_lock:
                    got = english.ingest(self.store.db, text, now)
                return 200, {"ok": True, "english": got}
            except ValueError as e:  # 格式不对也先存档了，回一句让 Grok 知道
                return 200, {"ok": True, "english": None, "error": f"英语陪练回报没读懂：{e}"}
        return 200, {"ok": True}

    def handle_get(self, url: str, headers: dict, now: datetime = None) -> tuple:
        parts = urlsplit(url)
        if parts.path == "/cloud/monzo/callback":  # Monzo 授权完跳回来（state 在 finance 里核对）
            q = parse_qs(parts.query)
            try:
                msg = finance.callback(self.store.root, (q.get("code") or [""])[0], (q.get("state") or [""])[0])
                return 200, {"ok": True, "msg": msg}
            except Exception as e:
                return 400, {"ok": False, "msg": str(e)[:200]}
        if parts.path == "/health":
            return 200, {"ok": True}
        if parts.path.startswith("/cloud/agent/"):
            return self._agent("GET", parts.path, parse_qs(parts.query), headers, b"")
        m = re.fullmatch(r"/cloud/maps/([A-Za-z0-9._-]+\.(?:jpg|png))", parts.path)
        if m:
            if not self._authorized("cloud", headers):
                return 401, {"ok": False, "error": "unauthorized"}
            path = Path(self.store.root) / "maps" / m.group(1)
            if not path.is_file():
                return 404, {"ok": False, "error": "not found"}
            return 200, {"ok": True, "name": m.group(1), "base64": base64.b64encode(path.read_bytes()).decode()}
        if parts.path != "/cloud/grok/results":
            return 404, {"ok": False, "error": "not found"}
        if not self._authorized("cloud", headers):
            return 401, {"ok": False, "error": "unauthorized"}
        since = (parse_qs(parts.query).get("since") or [""])[0]
        try:
            since_at = datetime.fromisoformat(since) if since else None
        except ValueError:
            return 400, {"ok": False, "error": "bad since"}
        if since_at and since_at.tzinfo is None:
            since_at = since_at.replace(tzinfo=timezone.utc)
        entries = [e for e in recent(self.store.root, hours=24 * 7, limit=500, now=now)
                   if e.get("path") == "/grok/result" and e["text"].startswith(CLOUD_MARK)
                   and (since_at is None or datetime.fromisoformat(e["at"]) > since_at)]
        return 200, {"ok": True, "results": [{"at": e["at"], "text": e["text"]} for e in entries[-20:]]}

    def _fancy(self, headers: dict, body: bytes, now: datetime = None) -> tuple:
        """DemoApp 举报提醒：只认 INBOX_TOKEN_FANCY，别的钥匙都不行；不记进收件箱。"""
        if not self._authorized("fancy", headers):
            return 401, {"ok": False, "error": "unauthorized"}
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "error": "too large"}
        try:
            data = json.loads(body)
        except ValueError:
            return 400, {"ok": False, "error": "bad json"}
        return self.fancy.handle(data, now)

    def _device(self, path: str, headers: dict, body: bytes, now: datetime = None) -> tuple:
        """她的 Mac 和 iPhone 报数据：钥匙同会话报到钩子。"""
        if not (self._authorized("hook", headers) or self._authorized("cloud", headers)):
            return 401, {"ok": False, "error": "unauthorized"}
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "error": "too large"}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {"ok": False, "error": "bad json"}
        if not isinstance(data, dict):
            return 400, {"ok": False, "error": "bad json"}
        with self.db_lock:
            if path == "/cloud/spend":  # iPhone 快捷指令转发的招行消费短信（ethan/finance.py）
                text = data.get("text") or data.get("sms") or ""
                if not str(text).strip():
                    return 400, {"ok": False, "error": "empty"}
                return 200, finance.record_cmb(self.store.db, str(text), now, str(data.get("sender") or ""))
            if path == "/cloud/activity":
                return 200, {"ok": True, "saved": worktime.record(self.store.db, list(data.get("slots") or [])[:2000],
                                                                 str(data.get("source", "mac")))}
            try:
                return 200, {"ok": True, **places_log.record(self.store.db, str(data.get("event", "")),
                                                            str(data.get("place", "home")), now)}
            except ValueError as e:
                return 400, {"ok": False, "error": str(e)}

    def _hook(self, query: dict, headers: dict, body: bytes, now: datetime = None) -> tuple:
        if not (self._authorized("hook", headers) or self._authorized("cloud", headers)):
            return 401, {"ok": False, "error": "unauthorized"}
        if len(body) > HOOK_MAX_BODY:
            return 413, {"ok": False, "error": "too large"}
        try:
            event = json.loads(body or b"{}")
        except ValueError:
            return 400, {"ok": False, "error": "bad json"}
        if not isinstance(event, dict) or not event.get("session_id"):
            return 400, {"ok": False, "error": "no session_id"}
        where = (query.get("where") or ["cloud"])[0]
        with self.db_lock:
            agents.record_hook(self.store.root, event, where, now)
            if where == "mac" and event.get("hook_event_name") == "UserPromptSubmit":  # 她在 Mac 上跟 Claude 说话也算工作时间
                worktime.record(self.store.db, [worktime.slot_of(now or datetime.now(timezone.utc))], "claude")
        return 200, {"ok": True}

    def _agent(self, method: str, path: str, query: dict, headers: dict, body: bytes) -> tuple:
        if not self._authorized("cloud", headers):
            return 401, {"ok": False, "error": "unauthorized"}
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "error": "too large"}
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return 400, {"ok": False, "error": "bad json"}
        if not isinstance(data, dict):
            return 400, {"ok": False, "error": "bad json"}
        q = {k: v[0] for k, v in query.items()}
        db = self.store.db
        m = re.fullmatch(r"/cloud/agent/tasks/(\d+)(/review|/retry|/finish)?", path)
        try:
            with self.db_lock:
                if method == "POST" and path == "/cloud/agent/tasks":
                    tid = agents.add(db, str(data.get("agent", "codex")), str(data.get("title", "")),
                                     spec=str(data.get("spec", "")), verify=str(data.get("verify", "")),
                                     size=int(data.get("size", 1)), project=str(data.get("project", "")))
                    return 200, {"ok": True, "id": tid}
                if method == "POST" and path == "/cloud/agent/done":
                    tid = agents.record_done(db, str(data.get("agent", "claude")), str(data.get("title", "")),
                                             int(data.get("size", 1)), project=str(data.get("project", "")),
                                             tokens_in=int(data.get("tokens_in", 0)),
                                             tokens_out=int(data.get("tokens_out", 0)),
                                             cost_usd=float(data.get("cost_usd", 0)),
                                             work_seconds=float(data.get("work_seconds", 0)),
                                             reworks=int(data.get("reworks", 0)), note=str(data.get("note", "")))
                    return 200, {"ok": True, "id": tid}
                if method == "GET" and path == "/cloud/agent/tasks":
                    return 200, {"ok": True, "tasks": agents.listing(db, q.get("status"), q.get("agent"))}
                if method == "POST" and path == "/cloud/agent/sessions":
                    n = agents.save_sessions(self.store.root, list(data.get("sessions") or []))
                    return 200, {"ok": True, "saved": n}
                if method == "GET" and path == "/cloud/agent/sessions":  # 查哪些会话报到过（排查用）
                    return 200, {"ok": True, **agents.load_sessions(self.store.root)}
                if method == "POST" and path == "/cloud/agent/report":
                    return 200, {"ok": True, "key": agents.record_report(self.store.root, data)}
                if method == "GET" and path == "/cloud/agent/scoreboard":
                    return 200, {"ok": True, "scoreboard": agents.scoreboard(db)}
                if m and method == "GET" and not m.group(2):
                    task = agents.get(db, int(m.group(1)))
                    return (200, {"ok": True, "task": task}) if task else (404, {"ok": False, "error": "not found"})
                if method == "POST" and path == "/cloud/agent/start":
                    tid = agents.start(db, str(data.get("agent", "claude")), str(data.get("title", "")),
                                       size=int(data.get("size", 1)), project=str(data.get("project", "")),
                                       spec=str(data.get("spec", "")))
                    return 200, {"ok": True, "id": tid}
                if m and method == "POST" and m.group(2) == "/finish":
                    task = agents.finish(db, int(m.group(1)), str(data.get("status", "accepted")),
                                         str(data.get("note", "")))
                    return 200, {"ok": True, "task": {k: task[k] for k in agents.LIST_FIELDS}}
                if m and method == "POST" and m.group(2) == "/retry":
                    task = agents.retry(db, int(m.group(1)))
                    return 200, {"ok": True, "task": {k: task[k] for k in agents.LIST_FIELDS}}
                if m and method == "POST" and m.group(2):
                    task = agents.review(db, int(m.group(1)), str(data.get("decision", "")), str(data.get("note", "")))
                    return 200, {"ok": True, "task": {k: task[k] for k in agents.LIST_FIELDS}}
        except (ValueError, TypeError) as e:
            return 400, {"ok": False, "error": str(e)}
        return 404, {"ok": False, "error": "not found"}

    def _cloud_task(self, headers: dict, body: bytes, now: datetime) -> tuple:
        if not self._authorized("cloud", headers):
            return 401, {"ok": False, "error": "unauthorized"}
        if len(body) > MAX_BODY:
            return 413, {"ok": False, "error": "too large"}
        task = extract_text(body)
        if not task:
            return 400, {"ok": False, "error": "empty"}
        if self.grok is None:
            return 503, {"ok": False, "error": "grok not configured"}
        if self._cloud_sent_today(now) >= CLOUD_DAILY_LIMIT:
            return 429, {"ok": False, "error": "daily limit"}
        if not self.grok(task, channel=CLOUD_CHANNEL, report_to=CLOUD_REPORT_TO):
            return 502, {"ok": False, "error": "grok webhook failed"}
        return 200, {"ok": True}

    def _cloud_sent_today(self, now: datetime) -> int:
        path = Path(self.store.root) / LOG_FILE
        if not path.exists():
            return 0
        today = now.date().isoformat()
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l.strip()]
        return sum(1 for r in rows if r.get("channel") == CLOUD_CHANNEL and r.get("at", "").startswith(today))

    def _record(self, path: str, label: str, text: str, now: datetime):
        self.dir.mkdir(parents=True, exist_ok=True)
        entry = {"at": now.isoformat(timespec="seconds"), "path": path, "label": label, "text": text}
        with (self.dir / f"{now.date().isoformat()}.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        if text.startswith((CLOUD_MARK, SAVED_MARK, PREP_MARK, ENGLISH_MARK)):  # 开发会话的事、收藏日报、准备员托查的、英语陪练回报都不进 Wiki 和 ethan-vault
            return
        with self.wiki_lock:  # 读了再写，并发回报会丢行
            wiki = self.store.wiki
            body = wiki.read(PAGE)["body"].rstrip() if wiki.exists(PAGE) else ""
            line = f"- {now:%Y-%m-%d %H:%M} UTC [{label}] {' '.join(text.split())}"
            wiki.write(PAGE, PAGE_TITLE, (body + "\n" + line).strip() + "\n")


LEAD_MARK = re.compile(r"^\s*(【[^】\n]{1,40}】)")


def order_marks(text: str) -> str:
    """Grok 回报开头的记号排成固定顺序：先是类型（【收藏日报】【准备 #12】【开工】…），再【回执 G…】，最后【来源：…】。
    （2026-10-29 自查：要它第一行写来源以后，来源一旦写在最前面，后面按开头认类型的都会认不出）"""
    marks, rest = [], text
    while True:
        m = LEAD_MARK.match(rest)
        if not m:
            break
        marks.append(m.group(1))
        rest = rest[m.end():]
    if len(marks) < 2:
        return text
    rank = lambda k: 2 if k.startswith("【来源") else 1 if k.startswith("【回执") else 0
    return "".join(sorted(marks, key=rank)) + rest  # sorted 稳定，同类保持原来先后


def extract_text(body: bytes, limit: int = MAX_TEXT) -> str:
    raw = body.decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        return raw[:limit]
    if isinstance(data, dict):
        for key in ("text", "result", "content", "message", "task"):
            if isinstance(data.get(key), str) and data[key].strip():
                return data[key].strip()[:limit]
    return json.dumps(data, ensure_ascii=False)[:limit]


def recent(root: Path, hours: int = 72, limit: int = 8, now: datetime = None) -> list:
    """最近几条回报，给聊天上下文用。"""
    now = now or datetime.now(timezone.utc)
    folder = Path(root) / "inbox"
    entries = []
    for day in range(hours // 24 + 1, -1, -1):
        path = folder / f"{(now - timedelta(days=day)).date().isoformat()}.jsonl"
        if path.exists():
            entries += [json.loads(l) for l in path.read_text(encoding="utf-8").split("\n") if l]
    cutoff = now - timedelta(hours=hours)
    entries = [e for e in entries if datetime.fromisoformat(e["at"]) >= cutoff]
    return entries[-limit:]


def make_handler(inbox: Inbox):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            status, payload = inbox.handle_get(self.path, dict(self.headers))
            self._reply(status, payload)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > (HOOK_MAX_BODY if self.path.startswith("/cloud/agent/hook") else MAX_BODY):
                self._reply(413, {"ok": False, "error": "too large"})
                return
            status, payload = inbox.handle(self.path, dict(self.headers), self.rfile.read(length))
            self._reply(status, payload)

        def log_message(self, fmt, *args):  # 只记方法、路径和状态，不记请求头里的密钥
            print(f"[inbox] {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}", flush=True)

    return Handler


def main():
    parser = argparse.ArgumentParser(description="小伴 的接收口")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    tokens = {"grok": os.environ.get("INBOX_TOKEN_GROK", ""), "cloud": os.environ.get("INBOX_TOKEN_CLOUD", ""),
              "hook": os.environ.get("INBOX_TOKEN_HOOK", ""), "fancy": os.environ.get("INBOX_TOKEN_FANCY", "")}
    inbox = Inbox(MemoryStore(args.data), tokens, grok=build_grok(log_dir=args.data),
                  notify=fancy_moderation.telegram_notifier(args.data))
    if not inbox.tokens.get("grok"):
        raise SystemExit("缺少 INBOX_TOKEN_GROK，请运行 python3 -m ethan.setup_secrets")
    ThreadingHTTPServer((args.host, args.port), make_handler(inbox)).serve_forever()


if __name__ == "__main__":
    main()
