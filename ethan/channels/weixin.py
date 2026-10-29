"""微信入口：通过微信官方 ClawBot（iLink Bot API）接入 Mia 的个人微信，只服务她一个人。

登录（一次性，Mia 用手机微信扫码）：
  python3 -m ethan.channels.weixin login
  打印二维码链接，在电脑上打开后用微信扫一扫并确认。凭据存进 ~/.config/ethan/weixin.json（600）。
运行：python3 -m ethan.channels.weixin run --data <数据目录>

协议要点（依据 @tencent-weixin/openclaw-weixin 2.4.6 的公开分析，非正式稳定接口）：
- getupdates 长轮询约 35 秒，游标 get_updates_buf 持久化
- 回复必须带她那条消息的 context_token；社区报告她约 24 小时没说话后 token 失效、每个 token 约 10 条下行，
  官方没有文档；媒体转述 ClawBot“不支持 AI 主动推送”（2026-10-29 调研）。所以主动问候仍走 Telegram，微信只负责聊天
- 语音自带微信转写文字（voice_item.text）；图片、文件、视频在 CDN 上用 AES-128-ECB 加密，下载后用 openssl 解密
- 发图片（2026-10-27 她要微信也能收发文件、照片和链接）：getuploadurl 拿上传参数 → 加密后 POST 到 CDN，
  响应头 x-encrypted-param 是下载参数 → sendmessage 发 image_item（aes_key 是 16 字节密钥的 hex 再 base64）。
  依据 openclaw-weixin 2.4.6 的 src/cdn/upload.ts 和 src/messaging/send.ts
- 文件：PDF、文字文件、Word 和 Telegram 一样处理（见 media.py）；视频截几帧画面（微信进程不载语音转写模型，省内存）
- ret/errcode 为 -14 表示登录失效，需要重新扫码。重新扫码后服务自动读取新凭据，不用重启
"""

import argparse
import base64
import hashlib
import json
import os
import queue
import random
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from .. import links
from ..core.agent import Ethan
from ..grok import build_grok
from ..photos import build as build_photos
from .media import MAX_DOWNLOAD, MediaProcessor, document_suffix, file_kind, read_document, unsupported
from ..core.router import AllBackendsFailed, BudgetExceeded, NeedsSwitchApproval, Settings
from ..core.splitter import typing_delay
from ..memory import MemoryStore

BASE = "https://ilinkai.weixin.qq.com"
CDN = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "2.4.6"
CLIENT_VERSION = str((2 << 16) | (4 << 8) | 6)
BOT_AGENT = "Ethan/2.0"
CRED_PATH = Path.home() / ".config" / "ethan" / "weixin.json"
QUIET = 3
MAX_WAIT = 20
RELOAD_CHECK = 30  # 登录失效后每隔多少秒看一次她有没有重新扫码


class SessionExpired(RuntimeError):
    """-14：登录失效，需要 Mia 重新扫码。"""


class ILinkAPI:
    def __init__(self, token: str = None, base: str = BASE, opener=urllib.request.urlopen):
        self.token = token
        self.base = base.rstrip("/")
        self.opener = opener

    def _headers(self, post=True) -> dict:
        h = {"iLink-App-Id": "bot", "iLink-App-ClientVersion": CLIENT_VERSION}
        if post:
            uin = str(random.getrandbits(32))
            h.update({"Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                      "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode()})
            if self.token:
                h["Authorization"] = f"Bearer {self.token}"
        return h

    def post(self, path: str, timeout: int = 15, **body) -> dict:
        body["base_info"] = {"channel_version": CHANNEL_VERSION, "bot_agent": BOT_AGENT}
        req = urllib.request.Request(f"{self.base}/ilink/bot/{path}", headers=self._headers(),
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"))
        with self.opener(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode("utf-8") or "{}")
        code = result.get("ret") or result.get("errcode") or 0
        if code == -14:
            raise SessionExpired("微信登录失效，需要重新扫码")
        if code != 0:
            raise RuntimeError(f"iLink {path} 失败：ret={code} {result.get('errmsg', '')}")
        return result

    def get(self, path: str, timeout: int = 35, **params) -> dict:
        url = f"{self.base}/ilink/bot/{path}?{urllib.parse.urlencode(params)}"
        with self.opener(urllib.request.Request(url, headers=self._headers(post=False)), timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")

    def download(self, url: str) -> bytes:
        with self.opener(urllib.request.Request(url), timeout=60) as resp:
            return resp.read()

    def upload(self, url: str, data: bytes) -> str:
        """把加密好的文件 POST 到 CDN，返回下载参数（响应头 x-encrypted-param）。"""
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/octet-stream"})
        with self.opener(req, timeout=60) as resp:
            param = resp.headers.get("x-encrypted-param")
        if not param:
            raise RuntimeError("CDN 上传没返回下载参数")
        return param


def parse_aes_key(value: str) -> bytes:
    """兼容三种编码：hex 字符串、base64(16 字节)、base64(32 位 hex)。"""
    if len(value) == 32:
        try:
            return bytes.fromhex(value)
        except ValueError:
            pass
    raw = base64.b64decode(value)
    if len(raw) == 32:
        return bytes.fromhex(raw.decode("ascii"))
    if len(raw) != 16:
        raise ValueError("AES key 长度不对")
    return raw


def aes_ecb_decrypt(data: bytes, key: bytes, runner=subprocess.run) -> bytes:
    proc = runner(["openssl", "enc", "-d", "-aes-128-ecb", "-K", key.hex()],
                  input=data, capture_output=True)
    if proc.returncode != 0:
        raise ValueError("解密失败")
    return proc.stdout


def aes_ecb_encrypt(data: bytes, key: bytes, runner=subprocess.run) -> bytes:
    proc = runner(["openssl", "enc", "-aes-128-ecb", "-K", key.hex()], input=data, capture_output=True)
    if proc.returncode != 0:
        raise ValueError("加密失败")
    return proc.stdout


def load_creds(path: Path = CRED_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).exists() else {}


def save_creds(creds: dict, path: Path = CRED_PATH):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        path.write_text(json.dumps(creds, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
    finally:
        os.umask(old)


def login(api: ILinkAPI, show=print, sleep=time.sleep, ask=input, max_polls: int = 300,
          refreshes: int = 3) -> dict:
    """扫码登录，返回 {"token", "base", "bot_id", "owner"}。二维码过期会自动换新的，最多 refreshes 次。"""
    def new_qr():
        qr = api.post("get_bot_qrcode?bot_type=3", local_token_list=[])
        show(f"请在电脑上打开这个链接，用手机微信扫码并确认：\n{qr['qrcode_img_content']}")
        return api, {"qrcode": qr["qrcode"]}

    poll_api, params = new_qr()
    for _ in range(max_polls):
        status = poll_api.get("get_qrcode_status", **params)
        state = status.get("status")
        if state == "confirmed":
            return {"token": status["bot_token"], "base": status.get("baseurl") or BASE,
                    "bot_id": status.get("ilink_bot_id"), "owner": status.get("ilink_user_id")}
        if state == "scaned_but_redirect":
            poll_api = ILinkAPI(base=f"https://{status['redirect_host']}", opener=api.opener)
        elif state == "need_verifycode":
            params["verify_code"] = ask("手机上显示的配对码：").strip()
        elif state == "expired" and refreshes > 0:
            refreshes -= 1
            show("二维码过期了，换一个新的：")
            poll_api, params = new_qr()
        elif state in ("expired", "verify_code_blocked"):
            raise RuntimeError("二维码过期或配对码错误次数过多，请重新运行登录")
        sleep(1)
    raise RuntimeError("等待扫码超时，请重新运行登录")


class WeixinChannel:
    def __init__(self, api: ILinkAPI, ethan: Ethan, owner: str, media_dir: Path, sleep=time.sleep,
                 decrypt=aes_ecb_decrypt, quiet: int = QUIET, max_wait: int = MAX_WAIT,
                 cred_path: Path = None, encrypt=aes_ecb_encrypt, video=None):
        """video(路径) -> (画面图片路径, 说的话)：处理她发的视频；默认只截帧不转写。"""
        self.api = api
        self.encrypt = encrypt
        self.video = video or MediaProcessor(None, media_dir).video_digest
        self.cred_path = cred_path  # 登录失效时从这里重读凭据；None 表示不重读
        self.ethan = ethan
        # 游标等状态单独存一个文件，不和 Telegram 进程抢写 settings.json
        self.state = Settings(Path(ethan.store.root) / "weixin-state.json")
        self.owner = owner
        self.media_dir = Path(media_dir)
        self.sleep = sleep
        self.decrypt = decrypt
        self.quiet = quiet
        self.max_wait = max_wait
        self.cursor = self.state.get("cursor", "")
        self.failures = 0
        self.expired = False

    def reload_creds(self) -> bool:
        """她重新扫码后凭据文件里是新 token：换上新 token，返回 True。"""
        if self.cred_path is None:
            return False
        creds = load_creds(self.cred_path)
        if not creds.get("token") or creds["token"] == self.api.token:
            return False
        self.api.token = creds["token"]
        self.api.base = (creds.get("base") or self.api.base).rstrip("/")
        self.owner = creds.get("owner") or self.owner
        self.expired = False
        print("[weixin] 已换用重新扫码后的凭据", flush=True)
        return True

    # 收消息
    def fetch(self) -> list:
        result = self.api.post("getupdates", timeout=40, get_updates_buf=self.cursor)
        if result.get("get_updates_buf"):
            self.cursor = result["get_updates_buf"]
            self.state.set("cursor", self.cursor)
        return [m for m in result.get("msgs") or []
                if m.get("message_type") == 1 and m.get("from_user_id") == self.owner]

    def download_media(self, item: dict, suffix: str = ".jpg") -> Path:
        """图片、文件、视频共用：从 CDN 下载并解密。"""
        media = item.get("media") or {}
        url = media.get("full_url") or f"{CDN}/download?encrypted_query_param=" + urllib.parse.quote(
            media["encrypt_query_param"], safe="")
        key = parse_aes_key(item.get("aeskey") or media["aes_key"])
        day = self.media_dir / time.strftime("%Y-%m-%d")
        day.mkdir(parents=True, exist_ok=True)
        path = day / f"wx-{time.strftime('%H%M%S')}-{secrets.token_hex(3)}{suffix}"
        path.write_bytes(self.decrypt(self.api.download(url), key))
        return path

    def download_image(self, item: dict) -> Path:
        return self.download_media(item, ".jpg")

    def file(self, item: dict, notes: list) -> list:
        name = item.get("file_name") or "文件"
        kind = file_kind({"file_name": name})
        if kind == "other":
            notes.append(unsupported(name))
            return []
        if int(item.get("len") or 0) > MAX_DOWNLOAD:
            notes.append(f"【文件 {name} 超过 20MB，没收下】")
            return []
        return read_document(self.download_media(item, document_suffix(kind, name)), name, kind, notes)

    def describe(self, msg: dict) -> dict:
        notes, images, voice = [], [], False
        for item in msg.get("item_list") or []:
            kind = item.get("type")
            try:
                if kind == 1:
                    notes.append((item.get("text_item") or {}).get("text", "").strip())
                elif kind == 3:
                    voice = True
                    heard = (item.get("voice_item") or {}).get("text", "").strip()
                    notes.append(f"【语音】{heard}" if heard else "【语音，没听清】")
                elif kind == 2:
                    images.append(self.download_image(item.get("image_item") or {}))
                    notes.append("【图片】")
                elif kind == 5:
                    frames, heard = self.video(self.download_media(item.get("video_item") or {}, ".mp4"))
                    images += frames
                    notes.append("【视频】" + (f"（里面说：{heard}）" if heard else "（截了几帧画面）"))
                elif kind == 4:
                    images += self.file(item.get("file_item") or {}, notes)
            except Exception as e:  # 下载解密失败不影响整轮对话
                print(f"[weixin] 处理附件失败：{e}", flush=True)
                notes.append({2: "【图片没收到】", 4: "【文件没收到】", 5: "【视频没收到】"}.get(kind, "【附件没收到】"))
        return {"text": " ".join(n for n in notes if n), "images": images, "voice": voice}

    # 发消息
    def typing(self, context_token: str, on: bool):
        try:
            ticket = self.api.post("getconfig", timeout=10, ilink_user_id=self.owner,
                                   context_token=context_token).get("typing_ticket")
            if ticket:
                self.api.post("sendtyping", timeout=10, ilink_user_id=self.owner,
                              typing_ticket=ticket, status=1 if on else 2)
        except Exception as e:  # “正在输入”只是锦上添花
            print(f"[weixin] typing 失败：{e}", flush=True)

    def send_text(self, context_token: str, text: str):
        def send():
            self.api.post("sendmessage", msg={
                "from_user_id": "", "to_user_id": self.owner,
                "client_id": f"ethan-{secrets.token_hex(8)}", "message_type": 2, "message_state": 2,
                "context_token": context_token, "item_list": [{"type": 1, "text_item": {"text": text}}],
            })
        try:
            send()
        except SessionExpired:
            if not self.reload_creds():
                raise
            send()  # 刚重新扫码，用新凭据再发一次

    def send_image(self, context_token: str, data: bytes):
        """上传到微信 CDN 再发图片消息。"""
        key, filekey = secrets.token_bytes(16), secrets.token_hex(16)
        cipher = self.encrypt(data, key)
        up = self.api.post("getuploadurl", filekey=filekey, media_type=1, to_user_id=self.owner, rawsize=len(data),
                           rawfilemd5=hashlib.md5(data).hexdigest(), filesize=len(cipher), no_need_thumb=True,
                           aeskey=key.hex())
        url = (up.get("upload_full_url") or "").strip() or (
            f"{CDN}/upload?encrypted_query_param={urllib.parse.quote(up['upload_param'], safe='')}"
            f"&filekey={filekey}")
        param = self.api.upload(url, cipher)
        self.api.post("sendmessage", msg={
            "from_user_id": "", "to_user_id": self.owner,
            "client_id": f"ethan-{secrets.token_hex(8)}", "message_type": 2, "message_state": 2,
            "context_token": context_token, "item_list": [{"type": 2, "image_item": {
                "media": {"encrypt_query_param": param, "aes_key": base64.b64encode(key.hex().encode()).decode(),
                          "encrypt_type": 1},
                "mid_size": len(cipher)}}],
        })

    def send_photos(self, context_token: str, files: list, urls: list):
        """小伴 生成的照片和他转发的网上图片。发不出去就用文字说一声。"""
        for source in list(files) + list(urls):
            try:
                if str(source).startswith("http"):
                    data = self.api.download(source)
                else:
                    data = Path(source).read_bytes()
                self.send_image(context_token, data)
            except Exception as e:
                print(f"[weixin] 发图片失败：{e}", flush=True)
                self.send_text(context_token, "【系统提醒】有张图片没发出去")

    def send_bubbles(self, context_token: str, bubbles: list):
        for bubble in bubbles:
            self.sleep(typing_delay(bubble))
            self.send_text(context_token, bubble)

    def respond_batch(self, messages: list):
        token = messages[-1].get("context_token", "")
        self.state.set("last_context", {"token": token, "at": time.time()})
        parts = [self.describe(m) for m in messages]
        text = "\n".join(p["text"] for p in parts if p["text"])
        images = [str(i) for p in parts for i in p["images"]]
        if not text:
            return
        self.typing(token, True)
        try:
            reply = self.ethan.reply("weixin", text, images=images)
        except NeedsSwitchApproval as e:
            self.send_text(token, f"【系统提醒】Claude 订阅暂时用不了（{str(e)[:80]}）。"
                                  "在 Telegram 发送 /openrouter on 可切到 OpenRouter。")
            return
        except (BudgetExceeded, AllBackendsFailed) as e:
            self.send_text(token, f"【系统提醒】暂时联系不上模型：{str(e)[:120]}")
            return
        finally:
            self.typing(token, False)
        # 微信里暂不发语音，[VOICE] 的内容改成文字发
        self.send_bubbles(token, reply["voice"] + reply["bubbles"])
        self.send_photos(token, reply.get("photo_files", ()), reply.get("image_urls", ()))

    def poll_once(self, inbox: "queue.Queue"):
        """取一段消息：拿到第一条后，等她停下 quiet 秒（最多 max_wait 秒）再一起回。"""
        pending = [inbox.get()]
        waited = 0
        while waited < self.max_wait:
            try:
                pending.append(inbox.get(timeout=self.quiet))
            except queue.Empty:
                break
            waited += self.quiet
        self.respond_batch(pending)

    def receive_once(self, inbox: "queue.Queue") -> int:
        """长轮询一次，消息放进 inbox。返回下一轮前要等的秒数。"""
        try:
            for msg in self.fetch():
                inbox.put(msg)
            self.failures = 0
            return 0
        except SessionExpired:
            if self.reload_creds():
                return 0
            if not self.expired:  # 只提示一次，之后每 RELOAD_CHECK 秒悄悄看一次有没有新凭据
                self.expired = True
                print("[weixin] 登录失效，请 Mia 重新扫码：python3 -m ethan.channels.weixin login", flush=True)
            return RELOAD_CHECK
        except Exception as e:  # 网络抖动等
            self.failures += 1
            print(f"[weixin] {e}", flush=True)
            return 30 if self.failures >= 3 else 2

    def receive_forever(self, inbox: "queue.Queue"):
        """后台线程：长轮询收消息放进 inbox，回复不用等长轮询返回。"""
        while True:
            wait = self.receive_once(inbox)
            if wait:
                self.sleep(wait)

    def run_forever(self):
        inbox = queue.Queue()
        threading.Thread(target=self.receive_forever, args=(inbox,), daemon=True).start()
        while True:
            try:
                self.poll_once(inbox)
            except Exception as e:
                print(f"[weixin] 回复失败：{e}", flush=True)


def main():
    from ..cli import build_router

    parser = argparse.ArgumentParser(description="小伴 的微信入口（ClawBot）")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="扫码登录")
    run = sub.add_parser("run", help="运行")
    run.add_argument("--data", type=Path, required=True)
    run.add_argument("--budget", type=float, default=50.0)
    args = parser.parse_args()
    if args.cmd == "login":
        creds = login(ILinkAPI())
        save_creds(creds)
        print("登录成功，凭据已保存。服务会在半分钟内自动换用新凭据，不用重启")
        return
    creds = load_creds()
    if not creds.get("token"):
        raise SystemExit("还没登录，请先运行 python3 -m ethan.channels.weixin login")
    store = MemoryStore(args.data)
    api = ILinkAPI(creds["token"], creds.get("base") or BASE)
    try:
        api.post("msg/notifystart", timeout=10)
    except SessionExpired:
        raise SystemExit("微信登录失效，请重新扫码登录")
    except Exception as e:
        print(f"[weixin] notifystart 失败：{e}", flush=True)
    router = build_router(args.data, args.budget)
    video = MediaProcessor(None, args.data / "media").video_digest  # 只截帧，不另载语音转写模型
    ethan = Ethan(store, router, delegate=build_grok(args.data), photographer=build_photos(args.data, router.ledger),
                  reader=links.Reader(args.data, video=video))
    channel = WeixinChannel(api, ethan, owner=creds["owner"], media_dir=args.data / "media",
                            cred_path=CRED_PATH, video=video)
    channel.run_forever()


if __name__ == "__main__":
    main()
