"""工作台的 HTTP 服务（Python 标准库，不加依赖）。

  GET  /                      页面（静态 HTML/CSS/JS，在 static/ 里）
  POST /api/auth/webapp       Telegram 小程序登录：正文 {"initData": "..."}
  GET  /login?t=<一次性令牌>   Safari 登录链接（小伴 在 Telegram 里发给她）
  GET  /d/<设备令牌>           设备链接：直接给页面并登录，加到主屏幕后每次点开都从这里进（没有 Telegram 的 iPad 用）
  GET  /api/state             主界面要的全部数据
  GET  /api/quests            任务页
  GET  /api/team              团队页（agent 成绩单和任务看板）
  GET  /api/map               领地地图；GET /api/territory?k=body 领地页（k=waste 是荒原）
  GET  /api/brain?tab=new|all|archived|trash&q=&kind=&k=  第二大脑
  POST /api/act               她的操作：{"action": "habit.check", ...}
  GET  /img/avatar/day|night  两张头像；/img/card/<日期> 今日卡
除了页面和登录，其余都要会话令牌（cookie eo_session，或请求头 Authorization: Bearer）。

运行：python3 -m ethan.workbench.server --data <数据目录>（systemd：ethan2-workbench）
"""

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from . import VERSION, auth
from .api import Workbench

STATIC = Path(__file__).parent / "static"
MAX_BODY = 64 * 1024
COOKIE = "eo_session"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; script-src 'self' https://telegram.org; connect-src 'self'; "
       "frame-ancestors https://web.telegram.org https://*.telegram.org")


class App:
    def __init__(self, workbench: Workbench, bot_token: str, data_root: Path):
        self.wb, self.bot_token, self.root = workbench, bot_token, Path(data_root)
        self.key = auth.derive_key(bot_token)
        self.logins = auth.LoginTokens(self.key)

    @property
    def owner(self):
        return self.wb.settings.get("telegram_owner_chat_id")

    @property
    def device_ver(self) -> int:
        return int(self.wb.settings.get("workbench_device_ver") or 1)

    def authed(self, headers: dict) -> bool:
        tokens = []  # 请求头和 cookie 有一个对就行（页面里存着的旧令牌过期了，不该挡住刚由设备链接种下的 cookie）
        authz = headers.get("Authorization") or headers.get("authorization") or ""
        if authz.startswith("Bearer "):
            tokens.append(authz[7:])
        cookie = SimpleCookie(headers.get("Cookie") or headers.get("cookie") or "")
        if COOKIE in cookie:
            tokens.append(cookie[COOKIE].value)
        return any(auth.check_session(self.key, t, self.owner) for t in tokens)

    def session_cookie(self, token: str) -> str:
        return f"{COOKIE}={token}; Path=/; Max-Age={auth.SESSION_DAYS * 86400}; HttpOnly; Secure; SameSite=Lax"

    def handle(self, method: str, url: str, headers: dict, body: bytes) -> tuple:
        """返回 (状态码, 响应头字典, 正文 bytes)。"""
        parts = urlsplit(url)
        path, query = parts.path, parse_qs(parts.query)
        if method == "GET" and path in ("/", "/index.html"):
            return self.index()
        m = re.fullmatch(r"/d/([\w.\-]+)", path)
        if method == "GET" and m:
            if self.owner is None or not auth.check_device_token(self.key, m.group(1), self.device_ver):
                return 401, {"Content-Type": "text/html; charset=utf-8"}, "这个设备链接已经作废。请在手机 Telegram 里给 小伴 发“设备链接”拿新的。".encode()
            status, headers, body = self.index()
            return status, {**headers, "Set-Cookie": self.session_cookie(auth.make_session(self.key, self.owner))}, body
        if method == "GET" and path.startswith("/static/"):
            status, headers, body = self.static(path[len("/static/"):])
            if status == 200 and query.get("v") == [VERSION]:
                headers = {**headers, "Cache-Control": "public, max-age=31536000, immutable"}
            return status, headers, body
        if method == "GET" and path == "/health":
            return self.json(200, {"ok": True})
        if method == "POST" and path == "/api/auth/webapp":
            data = self.body_json(body)
            user = auth.verify_init_data(data.get("initData", ""), self.bot_token)
            if not user or self.owner is None or user.get("id") != self.owner:
                return self.json(401, {"ok": False, "error": "不是她的 Telegram 账号"})
            token = auth.make_session(self.key, self.owner)
            return self.json(200, {"ok": True, "token": token}, {"Set-Cookie": self.session_cookie(token)})
        if method == "GET" and path == "/login":
            if self.owner is None or not self.logins.redeem((query.get("t") or [""])[0]):
                return 401, {"Content-Type": "text/html; charset=utf-8"}, "登录链接过期或已用过，请在 Telegram 里重新发“工作台”。".encode()
            token = auth.make_session(self.key, self.owner)
            return 302, {"Location": f"/?v={VERSION}", "Set-Cookie": self.session_cookie(token)}, b""
        if method == "POST" and path == "/api/clientlog":  # 前端报告走到哪一步（排查手机上的问题用），只记日志
            data = self.body_json(body)
            print(f"[workbench] 前端：{str(data.get('step', ''))[:80]} {str(data.get('info', ''))[:400]}", flush=True)
            return self.json(200, {"ok": True})
        if path.startswith("/img/") and query.get("s"):
            headers = {"Authorization": "Bearer " + query["s"][0]}  # 图片标签带不了请求头
        if not self.authed(headers):
            return self.json(401, {"ok": False, "error": "需要登录"})
        if method == "GET" and path == "/api/state":
            return self.json(200, self.wb.state())
        if method == "GET" and path == "/api/quests":
            return self.json(200, self.wb.quests_page())
        if method == "GET" and path == "/api/team":
            return self.json(200, self.wb.team_page())
        if method == "GET" and path == "/api/map":
            return self.json(200, self.wb.map_page())
        if method == "GET" and path == "/api/review":
            return self.json(200, self.wb.review_page())
        if method == "GET" and path == "/api/monzo/start":  # 设置页“去授权”：跳到 Monzo 登录
            from .. import finance
            try:
                return 302, {"Location": finance.auth_link(self.root)}, b""
            except ValueError as e:
                return 200, {"Content-Type": "text/plain; charset=utf-8"}, str(e).encode()
        if method == "GET" and path == "/api/settings":
            return self.json(200, self.wb.settings_page())
        if method == "GET" and path == "/api/cards":
            return self.json(200, self.wb.cards_page())
        if method == "GET" and path == "/api/ach":
            return self.json(200, self.wb.ach_page())
        if method == "GET" and path == "/api/brain":
            g = lambda k: (query.get(k) or [""])[0]
            return self.json(200, self.wb.brain_page(g("tab") or "all", g("q"), g("kind"), g("k")))
        if method == "GET" and path == "/api/ethanday":
            return self.json(200, self.wb.ethan_days())
        if method == "GET" and path == "/api/territory":
            try:
                return self.json(200, self.wb.territory_page((query.get("k") or [""])[0]))
            except ValueError as e:
                return self.json(404, {"ok": False, "error": str(e)})
        if method == "POST" and path == "/api/act":
            data = self.body_json(body)
            return self.json(200, self.wb.act(str(data.pop("action", "")), data))
        m = re.fullmatch(r"/img/avatar/(day|night)", path)
        if method == "GET" and m:
            return self.file(self.root / "avatars" / f"avatar-{m.group(1)}.jpg")
        m = re.fullmatch(r"/img/card/(\d{4}-\d{2}-\d{2})", path)
        if method == "GET" and m:
            return self.file(self.root / "cards" / f"{m.group(1)}.jpg")
        return self.json(404, {"ok": False, "error": "not found"})

    @staticmethod
    def body_json(body: bytes) -> dict:
        try:
            data = json.loads(body or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def json(status: int, payload: dict, extra: dict = None) -> tuple:
        return status, {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", **(extra or {})}, \
            json.dumps(payload, ensure_ascii=False).encode()

    def index(self) -> tuple:
        status, headers, body = self.static("index.html")
        for name in ("app.css", "app.js", "moves.js"):  # 静态文件带版本号，内容变了地址就变，绕开缓存
            body = body.replace(f"/static/{name}\"".encode(), f"/static/{name}?v={VERSION}\"".encode())
        return status, {**headers, "Cache-Control": "no-store"}, body

    def static(self, name: str) -> tuple:
        path = (STATIC / name).resolve()
        if STATIC.resolve() not in path.parents or not path.is_file():
            return self.json(404, {"ok": False, "error": "not found"})
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        return 200, {"Content-Type": ctype, "Cache-Control": "no-store", "Content-Security-Policy": CSP}, path.read_bytes()

    def file(self, path: Path) -> tuple:
        if not path.is_file():
            return self.json(404, {"ok": False, "error": "not found"})
        return 200, {"Content-Type": "image/jpeg", "Cache-Control": "private, max-age=3600"}, path.read_bytes()


def make_handler(app: App):
    lock = threading.Lock()  # 每个连接一个线程收发，处理请求（用数据库）时一次只一个

    class Handler(BaseHTTPRequestHandler):
        timeout = 20  # 连上不说话的连接 20 秒就断，不占着

        def _go(self, method, head=False):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                status, headers, body = app.json(413, {"ok": False, "error": "too large"})
            else:
                try:
                    data = self.rfile.read(length)
                    with lock:
                        status, headers, body = app.handle(method, self.path, dict(self.headers), data)
                except Exception as e:  # 不把异常细节给外面，只记日志
                    print(f"[workbench] {method} {urlsplit(self.path).path} 出错：{type(e).__name__} {e}", flush=True)
                    status, headers, body = app.json(500, {"ok": False, "error": "服务器出错了"})
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if not head:
                self.wfile.write(body)

        def do_GET(self):
            self._go("GET")

        def do_HEAD(self):  # 监控和 curl -I 用
            self._go("GET", head=True)

        def do_POST(self):
            self._go("POST")

        def log_message(self, fmt, *args):  # 不记查询串（登录令牌在里面）
            print(f"[workbench] {self.command} {urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}", flush=True)

    return Handler


class CardRunner:
    """现画今日卡：另起进程跑 python3 -m ethan.cards（要半分钟，不能卡住服务）。"""

    def __init__(self, data: Path, module: str = "ethan.cards", extra=()):
        self.data, self.proc, self.module, self.extra = Path(data), None, module, list(extra)

    def busy(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def __call__(self) -> bool:
        if self.busy():
            return False
        self.proc = subprocess.Popen([sys.executable, "-m", self.module, "--data", str(self.data), *self.extra],
                                     cwd=Path(__file__).resolve().parents[2])
        return True


def build(data: Path, env=os.environ) -> App:
    from ..icloud import build_calendar
    from ..core.router import Settings
    from ..memory import MemoryStore
    from ..travel import places

    settings = Settings(Path(data) / "settings.json")
    store = MemoryStore(data)
    return App(Workbench(store, settings, calendar=build_calendar(env), places=places(settings), background=True,
                         card_runner=CardRunner(data), replan_runner=CardRunner(data, "ethan.planner", ["--force"])),
               env.get("TELEGRAM_BOT_TOKEN", ""), data)


def main():
    parser = argparse.ArgumentParser(description="Earth Online 工作台")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8788)
    args = parser.parse_args()
    app = build(args.data)
    if not app.bot_token:
        raise SystemExit("缺少 TELEGRAM_BOT_TOKEN")
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
