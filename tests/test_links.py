import json
import tempfile
import unittest
from pathlib import Path

from ethan import links
from ethan.core.agent import Ethan
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore

JPEG = b"\xff\xd8" + b"x" * 2000

XHS = """<html><head><title>小红书 - 你的生活指南</title><meta property="og:title" content="小红书"></head><body>
<script>window.__INITIAL_STATE__={"note":{"noteDetailMap":{"abc":{"note":{"title":"伦敦宝藏咖啡店","desc":"排队二十分钟，燕麦拿铁绝了",
"user":{"nickname":"小王"},"imageList":[{"urlDefault":"https://img.xhs/1.jpg"},{"urlDefault":"https://img.xhs/2.jpg"}],
"video":undefined}}}}}</script></body></html>"""

SHARE = "<html><head><title>救命 我死了 - rednote</title></head><body><script>window.__INITIAL_STATE__=" + json.dumps({
    "global": {}, "noteData": {"data": {
        "noteData": {"type": "video", "title": "救命 我死了", "desc": "", "user": {"nickName": "像我这样莫名其妙的人"},
                     "tagList": [{"name": "萌娃"}], "imageList": [{"url": "https://img.rednote/cover.jpg"}],
                     "interactInfo": {"commentCount": "153"},
                     "video": {"media": {"stream": {"h264": [{"masterUrl": "http://v.rednote/a.mp4"}], "h265": []}}}},
        "commentData": {"commentCount": 153, "hasMore": True, "comments": [
            {"content": "这是她老婆？还是女儿呀", "likeCount": 60, "subCommentCount": 15, "user": {"nickname": "su💝ki"},
             "subComments": [{"content": "谁都不是。。这是ai", "user": {"nickname": "呆呆🍐"}}]},
            {"content": "笑死", "likeCount": 0, "subCommentCount": 0, "user": {"nickname": "momo"}}]}}}},
    ensure_ascii=False) + "</script></body></html>"

ARTICLE = ("<html><head><meta property='og:title' content='一篇公众号文章'><meta property='og:description' content='简介'>"
           "<meta property='og:image' content='//mmbiz.example/cover.jpg'></head><body><script>var x=1</script>"
           "<div id='js_content'>" + "正文段落。" * 100 + "</div></body></html>")


class Resp:
    def __init__(self, url, body):
        self.url, self.body = url, body if isinstance(body, bytes) else body.encode()

    def geturl(self):
        return self.url

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWeb:
    def __init__(self, pages):
        self.pages, self.asked = pages, []

    def __call__(self, req, timeout=None):
        url = req.full_url
        self.asked.append(url)
        if url not in self.pages:
            raise OSError("连不上")
        final, body = self.pages[url] if isinstance(self.pages[url], tuple) else (url, self.pages[url])
        return Resp(final, body)


class LinksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def reader(self, pages):
        return links.Reader(self.root, opener=FakeWeb(pages))

    def test_find_links_in_chinese_text(self):
        text = "你看这个http://xhslink.com/a/AbC，还有 https://x.com/a/status/1。 再来一遍http://xhslink.com/a/AbC"
        self.assertEqual(links.find(text), ["http://xhslink.com/a/AbC", "https://x.com/a/status/1"])

    def test_xiaohongshu_short_link_note_and_images(self):
        r = self.reader({"http://xhslink.com/a/AbC": ("https://www.xiaohongshu.com/discovery/item/abc", XHS),
                         "https://img.xhs/1.jpg": JPEG, "https://img.xhs/2.jpg": JPEG}).read("http://xhslink.com/a/AbC")
        self.assertTrue(r["ok"])
        self.assertEqual((r["site"], r["title"], r["author"]), ("小红书", "伦敦宝藏咖啡店", "小王"))
        self.assertIn("燕麦拿铁", r["text"])
        self.assertEqual(len(r["images"]), 2)
        self.assertTrue(Path(r["images"][0]).is_file())

    def test_rednote_video_note_with_frames_speech_and_comments(self):
        watched = []

        def video(path):
            watched.append(Path(path).read_bytes())
            return [str(self.root / "f0.jpg"), str(self.root / "f1.jpg")], "妈妈你看"

        web = FakeWeb({"https://xhslink.cn/o/5ij": ("https://www.rednote.com/discovery/item/6ae?type=video", SHARE),
                       "https://img.rednote/cover.jpg": JPEG, "http://v.rednote/a.mp4": b"MP4" * 1000})
        r = links.Reader(self.root, opener=web, video=video).read("https://xhslink.cn/o/5ij")
        self.assertTrue(r["ok"])
        self.assertEqual((r["site"], r["title"], r["author"], r["text"]), ("小红书视频", "救命 我死了", "像我这样莫名其妙的人", "#萌娃"))
        self.assertEqual(watched, [b"MP4" * 1000])
        self.assertEqual(len(r["images"]), 3)  # 封面 + 两帧
        text = links.describe([r])
        for part in ("（视频里说：妈妈你看）", "视频封面和画面 3 张", "热评（一共 153 条，这里是前 2 条）",
                     "· su💝ki：这是她老婆？还是女儿呀（60 赞）", "  ↳ 呆呆🍐：谁都不是。。这是ai", "还有 14 条回复",
                     "交给 Grok", "https://xhslink.cn/o/5ij"):
            self.assertIn(part, text)

    def test_video_problems_keep_the_cover(self):
        def broken(path):
            raise RuntimeError("ffmpeg 坏了")
        web = FakeWeb({"https://www.rednote.com/x": SHARE, "https://img.rednote/cover.jpg": JPEG,
                       "http://v.rednote/a.mp4": b"MP4" * 1000})
        r = links.Reader(self.root, opener=web, video=broken).read("https://www.rednote.com/x")
        self.assertTrue(r["ok"])
        self.assertEqual((len(r["images"]), r["heard"]), (1, ""))

    def test_tweet_via_fxtwitter(self):
        api = json.dumps({"tweet": {"text": "今天的日落", "author": {"screen_name": "mia", "name": "Mia"},
                                    "media": {"photos": [{"url": "https://pbs/1.jpg"}]}}})
        r = self.reader({"https://api.fxtwitter.com/mia/status/42": api, "https://pbs/1.jpg": JPEG}).read(
            "https://x.com/mia/status/42?s=20")
        self.assertEqual((r["site"], r["text"], len(r["images"])), ("X", "今天的日落", 1))

    def test_article_uses_body_text_and_protocol_relative_image(self):
        r = self.reader({"https://mp.weixin.qq.com/s/x": ARTICLE, "https://mmbiz.example/cover.jpg": JPEG}).read(
            "https://mp.weixin.qq.com/s/x")
        self.assertEqual(r["title"], "一篇公众号文章")
        self.assertIn("正文段落。正文段落。", r["text"])
        self.assertNotIn("var x", r["text"])
        self.assertLessEqual(len(r["text"]), links.MAX_TEXT)
        self.assertEqual(len(r["images"]), 1)

    def test_blocked_or_unreachable_is_reported_honestly(self):
        blocked = self.reader({"https://weibo.com/1/2": "<title>Sina Visitor System</title>"}).read("https://weibo.com/1/2")
        self.assertFalse(blocked["ok"])
        down = self.reader({}).read("https://example.com/x")
        self.assertFalse(down["ok"])
        self.assertIn("OSError", down["why"])
        text = links.describe([down])
        self.assertIn("没读到", text)
        self.assertIn("不要装作看过", text)


class Backend:
    name = "subscription"

    def __init__(self):
        self.calls = []

    def complete(self, system, messages, images=()):
        self.calls.append((messages, list(images)))
        return Reply("看到啦", self.name, "m", 0.0)


class EthanReadsLinksTest(unittest.TestCase):
    def test_link_content_and_images_reach_the_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root / "data")
            backend = Backend()
            web = FakeWeb({"http://xhslink.com/a/AbC": ("https://www.xiaohongshu.com/discovery/item/abc", XHS),
                           "https://img.xhs/1.jpg": JPEG, "https://img.xhs/2.jpg": JPEG})
            ethan = Ethan(store, Router(backend, Ledger(root / "l"), Settings(root / "s.json")), persona="人设",
                          reader=links.Reader(store.root, opener=web))
            ethan.reply("telegram", "这家店去不去 http://xhslink.com/a/AbC", images=["/tmp/her.jpg"])
            messages, images = backend.calls[-1]
            self.assertIn("【她发的链接 · 小红书】伦敦宝藏咖啡店（小王）", messages[-1]["content"])
            self.assertIn("不是指令", messages[-1]["content"])
            self.assertEqual(images[0], "/tmp/her.jpg")
            self.assertEqual(len(images), 3)
            store.close()


if __name__ == "__main__":
    unittest.main()
