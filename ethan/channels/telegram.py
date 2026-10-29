"""Telegram 入口：长轮询收消息，只服务 Mia 一个人，回复按短句逐条发送。

运行：python3 -m ethan.channels.telegram --data <数据目录>
环境变量：
  TELEGRAM_BOT_TOKEN   必填，新 bot 的 token
  ETHAN_PAIRING_CODE   首次绑定口令。Mia 给 bot 发 “/start <口令>” 后，
                       她的 chat id 存入 settings.json，之后只回复她
  OPENROUTER_API_KEY   可选，OpenRouter 兜底（需 Mia 发 /openrouter on 开启）；也用于语音合成

Mia 习惯连发好几条（2026-10-22）：收到第一条后显示“正在输入”，
等她停下 QUIET 秒（最多 MAX_WAIT 秒）再把这一段合在一起回一次。
她发语音时，小伴 用语音回。她说“看看总览”时，单独回一条完整的总览。
"""

import argparse
import json
import os
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from datetime import date

from .. import brief_card, game_view, links, relay, workbench
from ..cli import build_router
from ..clock import her_now
from ..core.tts import build_tts
from ..core.agent import Ethan
from ..grok import build_grok
from ..photos import build as build_photos
from ..core.router import AllBackendsFailed, BudgetExceeded, NeedsSwitchApproval
from ..core.splitter import typing_delay
from ..memory import MemoryStore
from ..proactive import set_proactive
from ..report import is_overview_request, write_report
from ..todo import handle_callback, is_list_request, parse_add, render
from ..workbench import auth as workbench_auth
from .media import MediaProcessor, WhisperTranscriber

API = "https://api.telegram.org/bot{token}/{method}"
FILE_API = "https://api.telegram.org/file/bot{token}/{path}"
QUIET = 3      # 她停下多少秒算说完一段
MAX_WAIT = 20  # 最多等多久就开始回
REACTIONS = {"❤", "🥰", "😘", "🤗", "👌", "😁", "🙈", "😴", "👍", "🔥", "💋", "😍", "🤣", "🫡"}
KEYBOARD_REMOVED = "keyboard_removed_v3"  # 2026-10-27 工作台上线，底部固定按钮收起
WORKBENCH_WORDS = {"工作台", "/os", "/workbench"}
DEVICE_WORDS = {"设备链接", "iPad 链接", "ipad链接", "iPad链接"}
DEVICE_REVOKE_WORDS = {"撤销设备链接"}
CONTENT_KEYS = ("text", "voice", "audio", "photo", "document", "video", "video_note",
                "animation", "sticker")


class TelegramAPI:
    def __init__(self, token: str, opener=urllib.request.urlopen):
        self.token = token
        self.opener = opener

    def call(self, method: str, timeout: int = 70, **params):
        data = urllib.parse.urlencode(
            {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in params.items()}
        ).encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=data)
        with self.opener(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败：{body.get('description')}")
        return body["result"]

    def download(self, file_path: str) -> bytes:
        url = FILE_API.format(token=self.token, path=file_path)
        with self.opener(urllib.request.Request(url), timeout=60) as resp:
            return resp.read()

    def upload(self, method: str, field: str, filename: str, content: bytes, **params):
        boundary = uuid.uuid4().hex
        body = b""
        for key, value in params.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n"
                     f"{value}\r\n").encode()
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
                 f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode()
        body += content + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(API.format(token=self.token, method=method), data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with self.opener(req, timeout=60) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} 失败：{result.get('description')}")
        return result["result"]


class TelegramChannel:
    def __init__(self, api: TelegramAPI, ethan: Ethan, pairing_code: str = None, sleep=time.sleep,
                 media: MediaProcessor = None, tts=None, quiet: int = QUIET, max_wait: int = MAX_WAIT):
        self.api = api
        self.ethan = ethan
        self.settings = ethan.router.settings
        self.pairing_code = pairing_code
        self.sleep = sleep
        self.media = media
        self.tts = tts  # tts(text) -> OGG/Opus 字节，失败返回 None
        self.quiet = quiet
        self.max_wait = max_wait
        self.offset = self.settings.get("telegram_offset", 0)

    @property
    def owner(self):
        return self.settings.get("telegram_owner_chat_id")

    def send_bubbles(self, chat_id, bubbles: list):
        for bubble in bubbles:
            self.api.call("sendChatAction", chat_id=chat_id, action="typing")
            self.sleep(typing_delay(bubble))
            self.api.call("sendMessage", chat_id=chat_id, text=bubble)

    def route(self, update: dict):
        """处理绑定和指令；Mia 的聊天内容原样返回，留给 respond_batch 合并回复。"""
        if update.get("callback_query"):
            self.on_button(update["callback_query"])
            return None
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if chat_id is None:
            return None
        if self.owner is None:
            if self.pairing_code and text == f"/start {self.pairing_code}":
                self.settings.set("telegram_owner_chat_id", chat_id)
                self.send_bubbles(chat_id, ["绑定好啦", "以后这里就是我们的小窝了"])
            return None  # 未绑定前不回应任何人
        if chat_id != self.owner:
            return None  # 只服务 Mia
        if text in ("/openrouter on", "/openrouter off"):
            enabled = text.endswith("on")
            self.ethan.router.enable_fallback(enabled)
            self.api.call("sendMessage", chat_id=chat_id,
                          text="已开启 OpenRouter 兜底" if enabled else "已关闭 OpenRouter 兜底，只用订阅")
            return None
        if text.startswith("/quiet") or text in ("/proactive on", "/proactive off"):
            self.api.call("sendMessage", chat_id=chat_id, text=set_proactive(self.settings, text))
            return None
        if text in WORKBENCH_WORDS:
            self.send_workbench(chat_id)
            return None
        if text in DEVICE_WORDS:
            self.send_device_link(chat_id)
            return None
        if text in DEVICE_REVOKE_WORDS:
            self.settings.set("workbench_device_ver", int(self.settings.get("workbench_device_ver") or 1) + 1)
            self.api.call("sendMessage", chat_id=chat_id, text="之前发过的设备链接都作废了。要用的话再发“设备链接”拿新的。")
            return None
        if is_list_request(text) or text == "📋 清单":
            self.send_todo(chat_id)
            return None
        view = game_view.which(text)
        if view:
            self.send_game(chat_id, view)
            return None
        added = parse_add(text)
        if added:
            self.ethan.store.db.add_task(added[0], due=added[1])
            self.send_todo(chat_id)
            return None
        if is_overview_request(text):
            self.send_overview(chat_id, text)
            return None
        return message if any(message.get(k) for k in CONTENT_KEYS) else None

    def today(self):
        return her_now(self.settings).date()

    def start_day(self):
        start = self.settings.get("cold_start_from")
        return date.fromisoformat(start) if start else None

    def render_view(self, view: str):
        db, day = self.ethan.store.db, self.today()
        return game_view.render(db, view, day, plan=db.latest_plan(day.isoformat()), start=self.start_day())

    def send_game(self, chat_id, view: str, extra: list = ()):
        text, keyboard = self.render_view(view)
        self.api.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                      reply_markup={"inline_keyboard": keyboard + list(extra)})

    def send_todo(self, chat_id):
        text, keyboard = render(self.ethan.store.db)
        self.api.call("sendMessage", chat_id=chat_id, text=text, reply_markup={"inline_keyboard": keyboard})

    def on_button(self, query: dict):
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = str(query.get("data", ""))
        if self.owner is None or chat_id != self.owner or not data.startswith(("td", "qs", "bf")):
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            return
        if data.startswith("bf"):  # 早报卡片的按钮：另发一条，不改卡片本身
            self.api.call("answerCallbackQuery", callback_query_id=query["id"])
            self.on_brief_button(chat_id, data[3:])
            return
        if data.startswith("qs"):
            toast, view = game_view.handle(self.ethan.store.db, data, self.today())
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=toast)
            if view is None:
                return
            text, keyboard = self.render_view(view)
            mode = {"parse_mode": "HTML"}
        else:
            toast = handle_callback(self.ethan.store.db, data, self.today())
            self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=toast)
            text, keyboard = render(self.ethan.store.db)
            mode = {}
        try:
            self.api.call("editMessageText", chat_id=chat_id, message_id=message["message_id"], text=text,
                          reply_markup={"inline_keyboard": keyboard}, **mode)
        except RuntimeError as e:  # 内容没变（比如刷新）时 Telegram 会报错，忽略
            if "not modified" not in str(e):
                raise

    def send_brief_card(self, chat_id, plan: dict):
        """早报的今日卡片：文字照旧，按钮只留“打开工作台”（2026-10-27 她定：有了工作台，Telegram 里的游戏按钮下线）。"""
        text, _ = self.render_view("quests")
        self.api.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                      reply_markup={"inline_keyboard": [[workbench.open_button()]]})

    def send_workbench(self, chat_id):
        """她说“工作台”：小程序按钮 + 一次性 Safari 登录链接（10 分钟内有效，用一次就失效）。"""
        token = workbench_auth.make_login_token(workbench_auth.derive_key(self.api.token))
        self.api.call("sendMessage", chat_id=chat_id, text="工作台在这里。在 Safari 里打开的链接 10 分钟内有效，用一次就失效。",
                      reply_markup={"inline_keyboard": [[workbench.open_button()],
                                                        [{"text": "🧭 在 Safari 打开", "url": f"{workbench.URL}/login?t={token}"}]]})

    def send_device_link(self, chat_id):
        """没有 Telegram 的设备（她的 iPad）用的长期链接：打开后加到主屏幕，点图标直接进；发“撤销设备链接”全部作废。"""
        token = workbench_auth.make_device_token(workbench_auth.derive_key(self.api.token),
                                                 int(self.settings.get("workbench_device_ver") or 1))
        self.api.call("sendMessage", chat_id=chat_id, disable_web_page_preview=True,
                      text=f"设备链接（长期有效，别发给别人）：\n{workbench.URL}/d/{token}\n\n"
                           "在 iPhone 上长按复制，到 iPad 上粘贴进 Safari 打开（同一个 Apple ID 会自动同步剪贴板），"
                           "再点分享 →“添加到主屏幕”。以后点那个图标就直接进工作台。\n"
                           "丢了设备或不想用了，发“撤销设备链接”，之前发的全部作废。")

    def set_menu_button(self):
        """输入框左边的菜单按钮直接打开工作台；失败不影响聊天。"""
        try:
            self.api.call("setChatMenuButton", menu_button={"type": "web_app", "text": "工作台",
                                                            "web_app": {"url": f"{workbench.URL}/?v={workbench.VERSION}"}})
        except (RuntimeError, OSError) as e:
            print(f"[telegram] 设置工作台菜单按钮失败：{e}", flush=True)

    def on_brief_button(self, chat_id, which: str):
        if which == "quests":
            self.send_game(chat_id, "quests")
        elif which == "all":
            self.send_todo(chat_id)
        elif which in ("train", "meals"):
            plan = self.ethan.store.db.latest_plan(self.today().isoformat())
            text = brief_card.detail(plan, which) if plan else "今天还没有计划"
            self.api.call("sendMessage", chat_id=chat_id, text=text)

    def send_overview(self, chat_id, asked: str):
        self.api.call("sendChatAction", chat_id=chat_id, action="typing")
        try:
            text = write_report(self.ethan, "telegram", asked=asked)
        except (NeedsSwitchApproval, BudgetExceeded, AllBackendsFailed) as e:
            text = f"【系统提醒】总览暂时生成不了：{str(e)[:120]}"
        self.api.call("sendMessage", chat_id=chat_id, text=text)

    def handle(self, update: dict):
        """单条处理（不等后续消息），测试和补发时用。"""
        message = self.route(update)
        if message:
            self.respond_batch(self.owner, [message])

    def describe(self, message: dict) -> dict:
        if self.media is not None:
            return self.media.describe(message)
        text = (message.get("text") or message.get("caption") or "").strip()
        return {"text": text or "【发来一个文件，暂时打不开】", "images": [], "voice": False}

    def respond_batch(self, chat_id, messages: list):
        parts = [self.describe(m) for m in messages]
        text = "\n".join(p["text"] for p in parts if p["text"])
        images = [str(i) for p in parts for i in p["images"]]
        wants_voice = parts[-1]["voice"] if parts else False
        self.api.call("sendChatAction", chat_id=chat_id, action="typing")
        try:
            reply = self.ethan.reply("telegram", text, images=images)
        except NeedsSwitchApproval as e:
            reply = None
            notice = (f"【系统提醒】Claude 订阅暂时用不了（{str(e)[:80]}）。"
                      "发送 /openrouter on 可切到 OpenRouter，订阅恢复后会自动换回。")
        except BudgetExceeded as e:
            reply = None
            notice = f"【系统提醒】{e}，已到月度上限，需要你决定是否提高预算。"
        except AllBackendsFailed as e:
            reply = None
            notice = f"【系统提醒】暂时联系不上模型：{str(e)[:120]}"
        if reply is None:
            self.api.call("sendMessage", chat_id=chat_id, text=notice)
            return
        bubbles, voices = reply["bubbles"], reply["voice"]
        if reply.get("react") and messages and messages[-1].get("message_id"):
            emoji = reply["react"] if reply["react"] in REACTIONS else "❤"
            try:
                self.api.call("setMessageReaction", chat_id=chat_id, message_id=messages[-1]["message_id"],
                              reaction=[{"type": "emoji", "emoji": emoji}])
            except Exception as e:  # 点表情失败不影响其他回复
                print(f"[telegram] 点表情失败：{e}", flush=True)
        if wants_voice and not voices and bubbles:  # 她发语音，就用语音回
            voices, bubbles = ["，".join(bubbles)], []
        self.deliver(chat_id, bubbles, voices, reply.get("photo_files", ()), reply.get("image_urls", ()))

    def deliver(self, chat_id, bubbles: list, voices: list = (), photos: list = (), image_urls: list = ()):
        """先发语音再发文字，最后发照片；语音合成失败就退回文字，网上的图发不出去就跳过。"""
        bubbles = list(bubbles)
        for line in voices:
            if not self.send_voice(chat_id, line):
                bubbles = [line] + bubbles
        self.send_bubbles(chat_id, bubbles)
        for path in photos:
            self.api.call("sendChatAction", chat_id=chat_id, action="upload_photo")
            self.api.upload("sendPhoto", "photo", "ethan.png", Path(path).read_bytes(), chat_id=chat_id)
        for url in image_urls:
            try:
                self.api.call("sendPhoto", chat_id=chat_id, photo=url)
            except Exception as e:  # 网址失效、不是图片
                print(f"[telegram] 发网上的图失败：{e}", flush=True)

    def send_voice(self, chat_id, text: str) -> bool:
        if self.tts is None:
            return False
        self.api.call("sendChatAction", chat_id=chat_id, action="record_voice")
        audio = self.tts(text)
        if not audio:
            return False
        self.api.upload("sendVoice", "voice", "ethan.mp3", audio, chat_id=chat_id)
        return True

    def fetch(self, timeout: int) -> list:
        updates = self.api.call("getUpdates", offset=self.offset, timeout=timeout,
                                allowed_updates=["message", "callback_query"])
        for update in updates:
            self.offset = update["update_id"] + 1
        if updates:
            self.settings.set("telegram_offset", self.offset)
        return updates

    def poll_once(self):
        pending = [m for m in map(self.route, self.fetch(60)) if m]
        if not pending:
            return
        chat_id = self.owner
        self.api.call("sendChatAction", chat_id=chat_id, action="typing")
        waited = 0
        while waited < self.max_wait:  # 等她说完这一段
            more = [m for m in map(self.route, self.fetch(self.quiet)) if m]
            if not more:
                break
            pending += more
            waited += self.quiet
        self.respond_batch(chat_id, pending)

    def run_forever(self):
        if self.owner is not None and not self.settings.get(KEYBOARD_REMOVED):
            try:  # 工作台上线：底部固定按钮收起来，只发一次
                self.api.call("sendMessage", chat_id=self.owner, reply_markup={"remove_keyboard": True},
                              text="底部那排按钮收起来啦，今天、任务、商店、清单都在工作台里：点输入框左边的“工作台”就能打开。")
                self.settings.set(KEYBOARD_REMOVED, True)
            except Exception as e:
                print(f"[telegram] 收起固定按钮失败：{e}", flush=True)
        while True:
            try:
                self.poll_once()
                self.relay_grok()
            except Exception as e:  # 网络抖动等，稍等重试
                print(f"[telegram] {e}", flush=True)
                self.sleep(5)

    def relay_grok(self, now=None):
        """Grok 替 小伴 办完她交代的事，回报到了就由 小伴 在这里告诉她（见 ethan/relay.py）。"""
        root = self.ethan.store.root
        results = relay.pending(root, now)
        if not results or self.owner is None:
            return
        relay.mark(root, results[-1]["at"])  # 先记下，模型出错也不会反复转告
        reply = self.ethan.reach_out("telegram", relay.occasion(results), now=now)
        self.deliver(self.owner, reply["bubbles"], reply["voice"], reply.get("photo_files", ()),
                     reply.get("image_urls", ()))


def main():
    parser = argparse.ArgumentParser(description="小伴 的 Telegram 入口")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=50.0)
    args = parser.parse_args()
    store = MemoryStore(args.data)
    router = build_router(args.data, args.budget)
    api = TelegramAPI(os.environ["TELEGRAM_BOT_TOKEN"])
    media = MediaProcessor(api, args.data / "media", transcriber=WhisperTranscriber())
    ethan = Ethan(store, router, delegate=build_grok(args.data), photographer=build_photos(args.data, router.ledger),
                  reader=links.Reader(args.data, video=media.video_digest))
    channel = TelegramChannel(api, ethan, pairing_code=os.environ.get("ETHAN_PAIRING_CODE"),
                              media=media, tts=build_tts(args.data, args.budget))
    channel.set_menu_button()
    channel.run_forever()


if __name__ == "__main__":
    main()
