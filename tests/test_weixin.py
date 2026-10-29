import base64
import json
import queue
import tempfile
import unittest
from ethan.core.agent import TIMESTAMP

from pathlib import Path

from ethan.channels.weixin import (RELOAD_CHECK, ILinkAPI, SessionExpired, WeixinChannel, aes_ecb_decrypt, aes_ecb_encrypt,
                                   load_creds, login, parse_aes_key, save_creds)
from ethan.core.agent import Ethan
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore

KEY = bytes(range(16))


class Resp:
    def __init__(self, data):
        self.data = data if isinstance(data, bytes) else json.dumps(data).encode()

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout):
        self.requests.append(req)
        return Resp(self.responses.pop(0))


class FakeAPI:
    def __init__(self, files=None):
        self.posts = []
        self.files = files or {}
        self.token, self.base = "old", "https://old"

    def post(self, path, timeout=15, **body):
        self.posts.append((path, body))
        return {"typing_ticket": "t"} if path == "getconfig" else {}

    def download(self, url):
        return self.files[url]

    def upload(self, url, data):
        self.uploads = getattr(self, "uploads", []) + [(url, data)]
        return "dl-param"

    def sent(self):
        return [b["msg"]["item_list"][0]["text_item"]["text"] for p, b in self.posts
                if p == "sendmessage" and "text_item" in b["msg"]["item_list"][0]]


class Backend:
    name = "subscription"

    def __init__(self, text="在呢\n想我了？"):
        self.text, self.seen = text, []

    def complete(self, system, messages, images=()):
        self.seen.append((messages, list(images)))
        return Reply(self.text, self.name, "m", 0.0)


def wx(text=None, token="ctx1", sender="me@im.wechat", items=None):
    items = items or [{"type": 1, "text_item": {"text": text}}]
    return {"message_type": 1, "from_user_id": sender, "context_token": token, "item_list": items}


class CryptoTest(unittest.TestCase):
    def test_aes_key_formats(self):
        self.assertEqual(parse_aes_key(KEY.hex()), KEY)
        self.assertEqual(parse_aes_key(base64.b64encode(KEY).decode()), KEY)
        self.assertEqual(parse_aes_key(base64.b64encode(KEY.hex().encode()).decode()), KEY)
        with self.assertRaises(ValueError):
            parse_aes_key(base64.b64encode(b"short").decode())

    def test_openssl_roundtrip(self):
        enc = aes_ecb_encrypt(b"hello img", KEY)
        self.assertEqual(len(enc), 16)  # PKCS7 补齐到 16 字节
        self.assertEqual(aes_ecb_decrypt(enc, KEY), b"hello img")


class ApiTest(unittest.TestCase):
    def test_headers_and_session_expired(self):
        opener = Opener({"ret": 0, "msgs": []}, {"ret": -14})
        api = ILinkAPI("tok", opener=opener)
        api.post("getupdates", get_updates_buf="")
        req = opener.requests[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(req.get_header("Authorizationtype"), "ilink_bot_token")
        self.assertIn("base_info", json.loads(req.data))
        with self.assertRaises(SessionExpired):
            api.post("getupdates", get_updates_buf="")

    def test_login_follows_redirect_and_saves_creds(self):
        opener = Opener({"qrcode": "q1", "qrcode_img_content": "https://qr"},
                        {"status": "wait"}, {"status": "scaned_but_redirect", "redirect_host": "node2"},
                        {"status": "confirmed", "bot_token": "tok", "baseurl": "https://b",
                         "ilink_bot_id": "bot@im.bot", "ilink_user_id": "me@im.wechat"})
        shown = []
        creds = login(ILinkAPI(opener=opener), show=shown.append, sleep=lambda s: None)
        self.assertEqual(creds, {"token": "tok", "base": "https://b", "bot_id": "bot@im.bot",
                                 "owner": "me@im.wechat"})
        self.assertIn("https://qr", shown[0])
        self.assertTrue(opener.requests[-1].full_url.startswith("https://node2/"))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "c" / "weixin.json"
            save_creds(creds, path)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(load_creds(path)["token"], "tok")

    def test_login_refreshes_expired_qr(self):
        opener = Opener({"qrcode": "q1", "qrcode_img_content": "https://qr1"}, {"status": "expired"},
                        {"qrcode": "q2", "qrcode_img_content": "https://qr2"},
                        {"status": "confirmed", "bot_token": "t", "ilink_user_id": "me"})
        shown = []
        creds = login(ILinkAPI(opener=opener), show=shown.append, sleep=lambda s: None)
        self.assertEqual(creds["owner"], "me")
        self.assertIn("https://qr2", shown[-1])
        self.assertIn("qrcode=q2", opener.requests[-1].full_url)


class ChannelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.backend = Backend()
        router = Router(self.backend, Ledger(root / "ledger"), Settings(root / "settings.json"))
        self.ethan = Ethan(self.store, router, persona="人设")
        self.api = FakeAPI()
        self.cred_path = root / "weixin.json"
        self.ch = WeixinChannel(self.api, self.ethan, owner="me@im.wechat", media_dir=root / "media",
                                sleep=lambda s: None, decrypt=lambda data, key: b"jpg:" + data, quiet=0.01,
                                cred_path=self.cred_path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_burst_gets_one_reply_with_latest_context_token(self):
        inbox = queue.Queue()
        for m in (wx("在吗", "c1"), wx("好累", "c2")):
            inbox.put(m)
        self.ch.poll_once(inbox)
        self.assertEqual(len(self.backend.seen), 1)
        self.assertEqual(TIMESTAMP.sub("", self.backend.seen[0][0][-1]["content"]), "在吗\n好累")
        self.assertEqual(self.api.sent(), ["在呢", "想我了？"])
        tokens = {b["msg"]["context_token"] for p, b in self.api.posts if p == "sendmessage"}
        self.assertEqual(tokens, {"c2"})
        self.assertIn("sendtyping", [p for p, _ in self.api.posts])
        self.assertEqual(self.ch.state.get("last_context")["token"], "c2")

    def test_fetch_ignores_strangers_and_bot_echo_and_saves_cursor(self):
        self.api.post = lambda path, timeout=15, **b: {"get_updates_buf": "cur2", "msgs": [
            wx("hi"), wx("陌生人", sender="x@im.wechat"), {**wx("自己"), "message_type": 2}]}
        self.assertEqual(len(self.ch.fetch()), 1)
        self.assertEqual(self.ch.state.get("cursor"), "cur2")

    def test_voice_uses_wechat_transcript_and_voice_tag_becomes_text(self):
        self.backend.text = "[VOICE]晚安小宝[/VOICE]"
        inbox = queue.Queue()
        inbox.put(wx(items=[{"type": 3, "voice_item": {"text": "我要睡了"}}]))
        self.ch.poll_once(inbox)
        self.assertEqual(TIMESTAMP.sub("", self.backend.seen[0][0][-1]["content"]), "【语音】我要睡了")
        self.assertEqual(self.api.sent(), ["晚安小宝"])

    def test_image_downloaded_and_decrypted(self):
        url = "https://cdn/full"
        self.api.files[url] = b"enc"
        inbox = queue.Queue()
        inbox.put(wx(items=[{"type": 2, "image_item": {"aeskey": KEY.hex(), "media": {"full_url": url}}}]))
        self.ch.poll_once(inbox)
        messages, images = self.backend.seen[0]
        self.assertEqual(TIMESTAMP.sub("", messages[-1]["content"]), "【图片】")
        self.assertEqual(Path(images[0]).read_bytes(), b"jpg:enc")

    def test_broken_image_does_not_break_chat(self):
        inbox = queue.Queue()
        inbox.put(wx(items=[{"type": 2, "image_item": {"media": {}}}, {"type": 1, "text_item": {"text": "看"}}]))
        self.ch.poll_once(inbox)
        self.assertEqual(TIMESTAMP.sub("", self.backend.seen[0][0][-1]["content"]), "【图片没收到】 看")

    def test_pdf_word_and_unsupported_files(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", "<w:p><w:r><w:t>简历正文</w:t></w:r></w:p>")
        self.api.files.update({"https://cdn/pdf": b"%PDF", "https://cdn/doc": b"x"})
        self.ch.decrypt = lambda data, key: buf.getvalue() if data == b"x" else data
        inbox = queue.Queue()
        inbox.put(wx(items=[
            {"type": 4, "file_item": {"file_name": "流水.pdf", "len": "4", "media": {"full_url": "https://cdn/pdf", "aes_key": KEY.hex()}}},
            {"type": 4, "file_item": {"file_name": "简历.docx", "media": {"full_url": "https://cdn/doc", "aes_key": KEY.hex()}}},
            {"type": 4, "file_item": {"file_name": "账单.xlsx", "media": {}}},
            {"type": 4, "file_item": {"file_name": "大.pdf", "len": str(30 * 1024 * 1024), "media": {}}}]))
        self.ch.poll_once(inbox)
        messages, images = self.backend.seen[0]
        text = messages[-1]["content"]
        self.assertTrue(images[0].endswith(".pdf"))
        for part in ("【PDF 文件 流水.pdf", "【文件 简历.docx】\n简历正文", "账单.xlsx，这种格式暂时打不开", "大.pdf 超过 20MB"):
            self.assertIn(part, text)

    def test_video_frames_go_to_the_model(self):
        self.api.files["https://cdn/v"] = b"mp4"
        self.ch.video = lambda path: ([str(path) + "-f0.jpg"], "")
        inbox = queue.Queue()
        inbox.put(wx(items=[{"type": 5, "video_item": {"media": {"full_url": "https://cdn/v", "aes_key": KEY.hex()}}}]))
        self.ch.poll_once(inbox)
        messages, images = self.backend.seen[0]
        self.assertIn("【视频】（截了几帧画面）", messages[-1]["content"])
        self.assertTrue(images[0].endswith(".mp4-f0.jpg"))

    def test_photo_reply_is_uploaded_and_sent_as_image(self):
        from ethan import photos

        class Maker:
            name, model = "openrouter", "m"

            def make(self, prompt):
                return b"PNGDATA"
        self.ethan.photographer = photos.Photographer(self.store.root, [Maker()])
        self.backend.text = "给你看\n[照片 她要的 窗外的雨]"
        self.ch.encrypt = lambda data, key: b"enc:" + data
        self.api.post = lambda path, timeout=15, **body: (self.api.posts.append((path, body)) or
                                                          ({"upload_param": "up/p"} if path == "getuploadurl" else {}))
        inbox = queue.Queue()
        inbox.put(wx("发张照片", "c9"))
        self.ch.poll_once(inbox)
        self.assertEqual(self.api.sent()[0], "给你看")
        up = next(b for p, b in self.api.posts if p == "getuploadurl")
        self.assertEqual((up["media_type"], up["rawsize"], up["filesize"], up["to_user_id"]), (1, 7, 11, "me@im.wechat"))
        self.assertEqual(self.api.uploads[0], (f"https://novac2c.cdn.weixin.qq.com/c2c/upload?encrypted_query_param=up%2Fp&filekey={up['filekey']}", b"enc:PNGDATA"))
        image = [b["msg"] for p, b in self.api.posts if p == "sendmessage"][-1]
        item = image["item_list"][0]
        self.assertEqual((item["type"], image["context_token"]), (2, "c9"))
        self.assertEqual(item["image_item"]["media"]["encrypt_query_param"], "dl-param")
        self.assertEqual(base64.b64decode(item["image_item"]["media"]["aes_key"]).decode(), up["aeskey"])
        self.assertEqual(item["image_item"]["mid_size"], 11)

    def test_rescan_picks_up_new_creds_without_restart(self):
        def post(path, timeout=15, **body):
            if self.api.token == "old":
                raise SessionExpired("失效")
            return {"msgs": [wx("扫好了")]}
        self.api.post = post
        save_creds({"token": "old", "owner": "me@im.wechat"}, self.cred_path)
        inbox = queue.Queue()
        self.assertEqual(self.ch.receive_once(inbox), RELOAD_CHECK)  # 还没重新扫码，稍后再看
        self.assertTrue(self.ch.expired)
        save_creds({"token": "new", "base": "https://new/", "owner": "me@im.wechat"}, self.cred_path)
        self.assertEqual(self.ch.receive_once(inbox), 0)  # 换上新 token，马上接着收
        self.assertEqual(self.ch.receive_once(inbox), 0)
        self.assertEqual((self.api.token, self.api.base), ("new", "https://new"))
        self.assertFalse(self.ch.expired)
        self.assertEqual(inbox.get_nowait()["item_list"][0]["text_item"]["text"], "扫好了")

    def test_reply_retries_once_with_new_creds(self):
        posts = []
        def post(path, timeout=15, **body):
            if self.api.token == "old":
                raise SessionExpired("失效")
            posts.append(body["msg"]["item_list"][0]["text_item"]["text"])
            return {}
        self.api.post = post
        save_creds({"token": "new", "owner": "me@im.wechat"}, self.cred_path)
        self.ch.send_text("c1", "在呢")
        self.assertEqual(posts, ["在呢"])

    def test_expired_without_new_creds_still_raises(self):
        def post(path, timeout=15, **body):
            raise SessionExpired("失效")
        self.api.post = post
        save_creds({"token": "old", "owner": "me@im.wechat"}, self.cred_path)
        with self.assertRaises(SessionExpired):
            self.ch.send_text("c1", "在呢")


if __name__ == "__main__":
    unittest.main()
