import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import photos
from ethan.channels.telegram import TelegramChannel
from ethan.core.agent import Ethan, build_system_prompt, parse_reply
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from tests.test_telegram import FakeAPI

DAY = date(2026, 11, 2)
PNG = b"\x89PNG" + b"x" * 100


class Maker:
    def __init__(self, name, fail=False):
        self.name, self.model, self.fail, self.prompts = name, "m", fail, []

    def make(self, prompt):
        self.prompts.append(prompt)
        if self.fail:
            raise OSError("403 Forbidden")
        return PNG


class PhotoTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_markers_are_parsed_and_hidden(self):
        r = parse_reply("日出好看吧\n[照片 山脊上的橘粉色日出][照片 她要的 刚泡好的手冲][发图 https://a.b/c.jpg]")
        self.assertEqual(r["bubbles"], ["日出好看吧"])
        self.assertEqual(r["photos"], [(False, "山脊上的橘粉色日出"), (True, "刚泡好的手冲")])
        self.assertEqual(r["image_urls"], ["https://a.b/c.jpg"])

    def test_three_self_photos_a_day_but_hers_unlimited_and_no_face(self):
        maker = Maker("gemini")
        cam = photos.Photographer(self.root, [maker])
        files, _ = cam.take([(False, f"风景{n}") for n in range(5)], DAY)
        self.assertEqual(len(files), 3)
        files, _ = cam.take([(False, "又一张")] + [(True, f"她要的{n}") for n in range(4)], DAY)
        self.assertEqual(len(files), 4)
        self.assertTrue(Path(files[0]).read_bytes().startswith(b"\x89PNG"))
        self.assertIn("不出现任何人脸", maker.prompts[0])
        self.assertEqual(len(cam.take([(False, "第二天")], date(2026, 11, 3))[0]), 1)

    def test_falls_back_to_next_service_and_records_cost(self):
        ledger = Ledger(self.root / "ledger")
        cam = photos.Photographer(self.root, [Maker("gemini", fail=True), Maker("openrouter")], ledger)
        files, notes = cam.take([(True, "晚饭")], DAY)
        self.assertEqual((len(files), notes), (1, []))
        self.assertAlmostEqual(ledger.month_spend("openrouter"), photos.EST_COST)

    def test_failure_is_told_only_when_she_asked(self):
        cam = photos.Photographer(self.root, [Maker("gemini", fail=True)])
        self.assertEqual(cam.take([(False, "日出")], DAY), ([], []))
        files, notes = cam.take([(True, "日出")], DAY)
        self.assertEqual(files, [])
        self.assertIn("照片没生成出来", notes[0])
        self.assertIn("403", notes[0])

    def test_build_needs_a_key(self):
        self.assertIsNone(photos.build(self.root, env={}))
        cam = photos.build(self.root, env={"GEMINI_API_KEY": "k", "OPENROUTER_API_KEY": "o"})
        self.assertEqual([m.name for m in cam.makers], ["openrouter", "gemini"])

    def test_openrouter_asks_grok_for_an_image_only(self):
        import base64, json
        sent = []

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            sent.append(json.loads(req.data))
            url = "data:image/jpeg;base64," + base64.b64encode(PNG).decode()
            return Resp(json.dumps({"choices": [{"message": {"images": [{"image_url": {"url": url}}]}}]}).encode())

        self.assertEqual(photos.OpenRouter("k", opener=opener).make("雨夜"), PNG)
        self.assertEqual((sent[0]["model"], sent[0]["modalities"]), ("x-ai/grok-imagine-image-2.0", ["image"]))

    def test_prompt_mentions_photos_only_when_possible(self):
        self.assertNotIn("[照片", build_system_prompt("人设", {"today": "2026-11-02"}))
        self.assertIn("[照片 她要的", build_system_prompt("人设", {"today": "2026-11-02"}, can_photo=True))


class Backend:
    name = "subscription"

    def __init__(self, text):
        self.text = text

    def complete(self, system, messages, images=()):
        return Reply(self.text, self.name, "m", 0.0)


class TelegramPhotoTest(unittest.TestCase):
    def test_reply_photo_is_generated_and_uploaded_after_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root / "data")
            router = Router(Backend("给你看\n[照片 她要的 窗外的雨]"), Ledger(root / "l"), Settings(root / "s.json"))
            ethan = Ethan(store, router, persona="人设", photographer=photos.Photographer(store.root, [Maker("gemini")]))
            api = FakeAPI([])
            uploads = []
            api.upload = lambda method, field, name, data, **kw: uploads.append((method, field, data[:4]))
            ch = TelegramChannel(api, ethan, sleep=lambda s: None)
            ch.respond_batch(42, [{"text": "想看看你那边", "message_id": 1}])
            self.assertEqual([p["text"] for m, p in api.calls if m == "sendMessage"], ["给你看"])
            self.assertEqual(uploads, [("sendPhoto", "photo", b"\x89PNG")])
            store.close()

    def test_without_photographer_she_is_told(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root / "data")
            router = Router(Backend("好\n[照片 她要的 窗外]"), Ledger(root / "l"), Settings(root / "s.json"))
            result = Ethan(store, router, persona="人设").reply("weixin", "发张照片")
            self.assertIn("【系统提醒】这边还发不了照片", result["bubbles"])
            self.assertEqual(result["photo_files"], [])
            store.close()


if __name__ == "__main__":
    unittest.main()
