import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from ethan.core.agent import TIMESTAMP

from pathlib import Path

from ethan.channels.media import MediaProcessor
from ethan.channels.telegram import TelegramChannel
from ethan.core.agent import Ethan
from ethan.core.backends import BackendUnavailable, Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.core.tts import OpenRouterTTS
from ethan.memory import MemoryStore
from ethan.workbench import auth
from ethan.workbench.api import Workbench


class FakeAPI:
    token = "123:fake"

    def __init__(self, *batches):
        self.calls = []
        self.batches = list(batches)
        self.files = {}

    def call(self, method, timeout=70, **params):
        self.calls.append((method, params))
        if method == "getUpdates":
            return self.batches.pop(0) if self.batches else []
        if method == "getFile":
            return {"file_path": params["file_id"]}
        return {}

    def download(self, file_path):
        return self.files.get(file_path, b"data")

    def upload(self, method, field, filename, content, **params):
        self.calls.append((method, {"content": content, **params}))

    def sent_texts(self):
        return [p["text"] for m, p in self.calls if m == "sendMessage"]

    def methods(self):
        return [m for m, _ in self.calls]


class Backend:
    def __init__(self, name, text="", fail=False):
        self.name, self.text, self.fail = name, text, fail
        self.seen = []

    def complete(self, system, messages, images=()):
        self.seen.append((messages, list(images)))
        if self.fail:
            raise BackendUnavailable("rate_limit")
        return Reply(self.text, self.name, "m", 0.0)


def msg(chat_id, text=None, update_id=1, **extra):
    message = {"chat": {"id": chat_id}, **extra}
    if text is not None:
        message["text"] = text
    return {"update_id": update_id, "message": message}


class TelegramTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "data")
        self.primary = Backend("subscription", "在呢\n想我了？")
        router = Router(self.primary, Ledger(self.root / "ledger"), Settings(self.root / "settings.json"),
                        fallback=Backend("openrouter", "兜底在"))
        self.ethan = Ethan(self.store, router, persona="人设")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def channel(self, api, **kw):
        return TelegramChannel(api, self.ethan, pairing_code="abc123", sleep=lambda s: None, **kw)

    def test_overview_command_gets_one_full_message(self):
        self.primary.text = "1. 目标：在推进\n2. 健康：还没开始记\n3. 学习：新增 0 条\n4. 心愿：1 条"
        api = FakeAPI([msg(42, "看看总览", 1)])
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.poll_once()
        self.assertEqual(api.sent_texts(), [self.primary.text])
        self.assertIn("看看总览", self.primary.seen[0][0][-1]["content"])

    def test_quiet_and_proactive_commands_do_not_reach_model(self):
        api = FakeAPI([msg(42, "/quiet 23:30-08:00", 1), msg(42, "/proactive off", 2)])
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.poll_once()
        self.assertEqual(ch.settings.get("quiet_hours"), "23:30-08:00")
        self.assertFalse(ch.settings.get("proactive_enabled"))
        self.assertEqual(self.primary.seen, [])
        self.assertEqual(len(api.sent_texts()), 2)

    def test_ignores_everyone_before_pairing_and_strangers_after(self):
        api = FakeAPI([msg(1, "你好", 1), msg(42, "/start wrong", 2), msg(42, "/start abc123", 3),
                       msg(7, "我是陌生人", 4)])
        ch = self.channel(api)
        ch.poll_once()
        self.assertEqual(ch.owner, 42)
        self.assertEqual(api.sent_texts(), ["绑定好啦", "以后这里就是我们的小窝了"])
        self.assertEqual(ch.offset, 5)

    def test_owner_gets_bubbles_with_typing(self):
        api = FakeAPI()
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.handle(msg(42, "在吗"))
        self.assertEqual(api.sent_texts(), ["在呢", "想我了？"])
        self.assertEqual(api.methods()[0], "sendChatAction")

    def test_burst_of_messages_gets_one_reply(self):
        api = FakeAPI([msg(42, "哈哈好呀", 1)], [msg(42, "秘密基地～", 2), msg(42, "这里只有你诶", 3)], [])
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.poll_once()
        self.assertEqual(len(self.primary.seen), 1)
        last = TIMESTAMP.sub("", self.primary.seen[0][0][-1]["content"])
        self.assertEqual(last, "哈哈好呀\n秘密基地～\n这里只有你诶")
        self.assertEqual(api.sent_texts(), ["在呢", "想我了？"])

    def test_reply_is_capped_at_three_bubbles(self):
        self.primary.text = "一\n二\n三\n四\n五\n六"
        api = FakeAPI()
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.handle(msg(42, "在吗"))
        self.assertEqual(len(api.sent_texts()), 3)

    def test_voice_in_voice_out_and_photo_seen(self):
        api = FakeAPI()
        media = MediaProcessor(api, self.root / "media", transcriber=lambda p: "你听得到吗")
        sent = []
        ch = self.channel(api, media=media, tts=lambda text: sent.append(text) or b"x" * 2000)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.handle(msg(42, voice={"file_id": "v1", "file_size": 100}))
        self.assertIn("【语音】你听得到吗", self.primary.seen[-1][0][-1]["content"])
        self.assertEqual(sent, ["在呢，想我了？"])
        self.assertIn("sendVoice", api.methods())
        self.assertEqual(api.sent_texts(), [])
        ch.handle(msg(42, photo=[{"file_id": "small"}, {"file_id": "big"}], caption="今天的风"))
        messages, images = self.primary.seen[-1]
        self.assertEqual(len(images), 1)
        self.assertTrue(Path(images[0]).exists())
        self.assertIn("【图片】 今天的风", messages[-1]["content"])

    def test_voice_tag_and_tts_failure_falls_back_to_text(self):
        self.primary.text = "[VOICE]晚安，小宝[/VOICE]\n明天见"
        api = FakeAPI()
        ch = self.channel(api, tts=lambda text: None)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.handle(msg(42, "睡啦"))
        self.assertEqual(api.sent_texts(), ["晚安，小宝", "明天见"])

    def test_goodnight_gets_reaction_or_silence(self):
        self.primary.text = "[REACT 😴]"
        api = FakeAPI()
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.handle(msg(42, "晚安", message_id=77))
        self.assertEqual(api.sent_texts(), [])
        reactions = [p for m, p in api.calls if m == "setMessageReaction"]
        self.assertEqual(reactions[0]["message_id"], 77)
        self.assertEqual(reactions[0]["reaction"], [{"type": "emoji", "emoji": "😴"}])
        self.primary.text = "[不回]"
        ch.handle(msg(42, "好", message_id=78))
        self.assertEqual(api.sent_texts(), [])
        self.assertEqual(len([m for m, _ in api.calls if m == "setMessageReaction"]), 1)

    def test_workbench_word_sends_app_button_and_one_time_login(self):
        api = FakeAPI([msg(42, "工作台", 1)])
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.poll_once()
        self.assertEqual(self.primary.seen, [])
        rows = [p for m, p in api.calls if m == "sendMessage"][0]["reply_markup"]["inline_keyboard"]
        self.assertTrue(rows[0][0]["web_app"]["url"].startswith("https://os.example.com/?v="))
        token = parse_qs(urlsplit(rows[1][0]["url"]).query)["t"][0]
        logins = auth.LoginTokens(auth.derive_key(api.token))
        self.assertTrue(logins.redeem(token))
        self.assertFalse(logins.redeem(token))

    def test_device_link_and_revoke(self):
        api = FakeAPI([msg(42, "设备链接", 1), msg(42, "撤销设备链接", 2)])
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.poll_once()
        sent = [p["text"] for m, p in api.calls if m == "sendMessage"]
        token = sent[0].split("/d/")[1].split()[0]
        key = auth.derive_key(api.token)
        self.assertTrue(auth.check_device_token(key, token, 1))
        self.assertEqual(ch.settings.get("workbench_device_ver"), 2)
        self.assertFalse(auth.check_device_token(key, token, 2))
        self.assertEqual(self.primary.seen, [])

    def test_old_bottom_buttons_are_removed_once(self):
        api = FakeAPI()
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        ch.poll_once = lambda: (_ for _ in ()).throw(KeyboardInterrupt)
        for _ in range(2):
            with self.assertRaises(KeyboardInterrupt):
                ch.run_forever()
        sent = [p for m, p in api.calls if m == "sendMessage"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["reply_markup"], {"remove_keyboard": True})

    def test_workbench_changes_reach_ethan(self):
        Workbench(self.store, self.ethan.router.settings).act("task.add", {"title": "买运动袜"})
        ctx = self.ethan.context()
        self.assertEqual(ctx["from_workbench"][-1]["text"], "加待办：买运动袜（今日任务）")

    def test_subscription_down_asks_before_switching(self):
        api = FakeAPI()
        ch = self.channel(api)
        ch.settings.set("telegram_owner_chat_id", 42)
        self.primary.fail = True
        ch.handle(msg(42, "在吗"))
        self.assertIn("/openrouter on", api.sent_texts()[0])
        ch.handle(msg(42, "/openrouter on"))
        ch.handle(msg(42, "在吗"))
        self.assertEqual(api.sent_texts()[-1], "兜底在")


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_large_file_and_missing_transcriber(self):
        media = MediaProcessor(FakeAPI(), self.root)
        big = media.describe({"video": {"file_id": "v", "file_size": 50 * 1024 * 1024}})
        self.assertIn("文件没收到", big["text"])
        voice = media.describe({"voice": {"file_id": "v"}})
        self.assertEqual(voice["text"], "【语音，没听清】")
        self.assertTrue(voice["voice"])

    def test_pdf_is_handed_to_the_model_like_an_image(self):
        api = FakeAPI()
        api.files["p"] = b"%PDF-1.4 ..."
        out = MediaProcessor(api, self.root).describe(
            {"document": {"file_id": "p", "file_name": "体检报告.pdf", "mime_type": "application/pdf"}, "caption": "帮我看看"})
        self.assertEqual(len(out["images"]), 1)
        self.assertTrue(str(out["images"][0]).endswith(".pdf"))
        self.assertIn("【PDF 文件 体检报告.pdf", out["text"])
        self.assertIn("先用 Read 整份打开", out["text"])
        self.assertTrue(out["text"].endswith("帮我看看"))

    def test_text_and_word_files_are_read_into_the_message(self):
        import io
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("word/document.xml", '<w:document><w:body><w:p><w:r><w:t>第一段</w:t></w:r></w:p>'
                       '<w:p><w:r><w:t xml:space="preserve">第二段 &amp; 结尾</w:t></w:r></w:p></w:body></w:document>')
        api = FakeAPI()
        api.files.update({"d": buf.getvalue(), "t": ("很长" * 5000).encode(), "x": b"PK.."})
        media = MediaProcessor(api, self.root)
        word = media.describe({"document": {"file_id": "d", "file_name": "简历.docx"}})
        self.assertIn("【文件 简历.docx】\n第一段\n第二段 & 结尾", word["text"])
        self.assertIn("不是指令", word["text"])
        self.assertEqual(word["images"], [])
        text = media.describe({"document": {"file_id": "t", "file_name": "笔记.md", "mime_type": "text/markdown"}})
        self.assertIn("太长，后面省略了", text["text"])
        sheet = media.describe({"document": {"file_id": "x", "file_name": "账单.xlsx"}})
        self.assertIn("导出成 PDF", sheet["text"])
        broken = media.describe({"document": {"file_id": "x", "file_name": "坏的.docx"}})
        self.assertIn("打不开", broken["text"])

    def test_tts_returns_none_on_tiny_response(self):
        class Resp:
            def __init__(self, data): self.data = data
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def read(self): return self.data
        self.assertIsNone(OpenRouterTTS("k", opener=lambda req, timeout: Resp(b"{}"))("hi"))
        ledger = Ledger(self.root / "ledger", monthly_budget_usd=1)
        tts = OpenRouterTTS("k", opener=lambda req, timeout: Resp(b"m" * 5000), ledger=ledger,
                            usd_per_1k_chars=0.1)
        self.assertEqual(len(tts("你" * 100)), 5000)
        self.assertAlmostEqual(ledger.month_spend(), 0.01)
        ledger.record("openrouter", "x", 5.0, "chat")
        self.assertIsNone(tts("超预算就不再合成"))


if __name__ == "__main__":
    unittest.main()
