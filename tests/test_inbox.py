import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

from ethan.core.agent import Ethan, build_system_prompt
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.cloud import CloudClient, main as cloud_main
from ethan.inbox import CLOUD_DAILY_LIMIT, CLOUD_MARK, Inbox, extract_text, make_handler, recent
from ethan.memory import MemoryStore
from ethan.vault import recent_chat

TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CLOUD = "c" * 43
CLOUD_AUTH = {"Authorization": f"Bearer {CLOUD}"}
NOW = datetime(2026, 10, 26, 19, 0, tzinfo=timezone.utc)


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "data")
        self.inbox = Inbox(self.store, {"grok": TOKEN})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, path="/grok/result", body=b'{"text": "Dishoom \\u5468\\u516d 14:00 \\u8ba2\\u597d\\u4e86"}',
             headers=AUTH, now=NOW):
        return self.inbox.handle(path, headers, body, now=now)

    def test_accepts_and_records_to_log_and_wiki(self):
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.post("/grok/memory", "我讲了团子的故事".encode())[0], 200)
        entries = recent(self.store.root, now=NOW)
        self.assertEqual([e["label"] for e in entries], ["Grok Bot 办完", "Grok 里的 小伴"])
        self.assertEqual(entries[1]["text"], "我讲了团子的故事")
        page = self.store.wiki.read("from-grok")["body"]
        self.assertIn("[Grok Bot 办完] Dishoom 周六 14:00 订好了", page)
        self.assertIn("[Grok 里的 小伴] 我讲了团子的故事", page)

    def test_saved_feed_kept_whole_and_out_of_wiki(self):
        import json as _json
        lines = ["【收藏日报】2026-10-29"] + [_json.dumps({"platform": "小红书", "url": f"https://x/{i}", "title": "伦敦小众书店" * 5,
                                                        "summary": "运河边的二手书店" * 10}, ensure_ascii=False) for i in range(30)]
        text = "\n".join(lines)
        self.assertGreater(len(text), 2000)
        self.assertEqual(self.post(body=_json.dumps({"text": text}, ensure_ascii=False).encode())[0], 200)
        self.assertEqual(recent(self.store.root, now=NOW)[-1]["text"], text)  # 整条留着，不截到 2000 字
        self.assertFalse(self.store.wiki.exists("from-grok"))  # 不往 Wiki 里灌

    def test_rejects_bad_token_unknown_path_oversize_and_empty(self):
        self.assertEqual(self.post(headers={"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.post(headers={})[0], 401)
        self.assertEqual(Inbox(self.store, {"grok": ""}).handle("/grok/result", AUTH, b"x")[0], 401)
        self.assertEqual(self.post("/admin")[0], 404)
        self.assertEqual(self.post(body=b"x" * (64 * 1024 + 1))[0], 413)
        self.assertEqual(self.post(body=b"  ")[0], 400)
        self.assertFalse((Path(self.store.root) / "inbox").exists())  # 一条都没记

    def test_recent_drops_old_entries(self):
        self.post(now=NOW - timedelta(days=5))
        self.post(now=NOW)
        self.assertEqual(len(recent(self.store.root, now=NOW)), 1)

    def test_extract_text_variants(self):
        self.assertEqual(extract_text(b'{"result": " ok "}'), "ok")
        self.assertEqual(extract_text(b"plain"), "plain")
        self.assertEqual(extract_text(b'{"a": 1}'), '{"a": 1}')

    def test_http_server_end_to_end(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.inbox))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            req = urllib.request.Request(base + "/grok/result", data=b'{"text": "done"}', headers=AUTH)
            with urllib.request.urlopen(req, timeout=5) as resp:
                self.assertEqual(resp.status, 200)
            with self.assertRaises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(urllib.request.Request(base + "/grok/result", data=b"x"), timeout=5)
            self.assertEqual(err.exception.code, 401)
        finally:
            server.shutdown()
            server.server_close()


class FakeGrok:
    """模仿 GrokWebhook：记下发出的任务，并像真的一样写 grok-tasks.jsonl。"""
    def __init__(self, root, ok=True):
        self.root, self.ok, self.sent = Path(root), ok, []

    def __call__(self, task, channel="", report_to=""):
        self.sent.append((task, channel, report_to))
        with (self.root / "grok-tasks.jsonl").open("a") as f:
            f.write(json.dumps({"at": NOW.isoformat(), "channel": channel, "task": task, "ok": self.ok}) + "\n")
        return self.ok


class CloudTest(unittest.TestCase):
    """Claude 云端会话经接收口找 Grok（2026-10-27 她同意）。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "data")
        self.grok = FakeGrok(self.store.root)
        self.inbox = Inbox(self.store, {"grok": TOKEN, "cloud": CLOUD}, grok=self.grok)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def task(self, body=b'{"task": "\u67e5\u6211\u7684\u865a\u62df\u5f62\u8c61"}', headers=CLOUD_AUTH):
        return self.inbox.handle("/cloud/grok", headers, body, now=NOW)

    def report(self, text, at=NOW):
        self.inbox.handle("/grok/result", AUTH, json.dumps({"text": text}).encode(), now=at)

    def test_sends_task_to_grok_marked_as_dev_session(self):
        self.assertEqual(self.task(), (200, {"ok": True}))
        task, channel, report_to = self.grok.sent[0]
        self.assertEqual(task, "查我的虚拟形象")
        self.assertEqual(channel, "claude-session")
        self.assertIn(CLOUD_MARK, report_to)
        self.assertIn("不用告诉 Mia", report_to)

    def test_cloud_token_is_separate_from_grok_token(self):
        self.assertEqual(self.task(headers=AUTH)[0], 401)  # Grok 的密钥不能派活
        self.assertEqual(self.inbox.handle("/grok/result", CLOUD_AUTH, b"x", now=NOW)[0], 401)  # 云端密钥不能冒充回报
        self.assertEqual(self.inbox.handle_get("/cloud/grok/results", AUTH, now=NOW)[0], 401)
        self.assertEqual(Inbox(self.store, {"grok": TOKEN}, grok=self.grok).handle("/cloud/grok", CLOUD_AUTH, b"x")[0], 401)
        self.assertEqual(self.grok.sent, [])

    def test_errors_empty_unconfigured_failed_and_daily_limit(self):
        self.assertEqual(self.task(body=b"  ")[0], 400)
        no_grok = Inbox(self.store, {"cloud": CLOUD})
        self.assertEqual(no_grok.handle("/cloud/grok", CLOUD_AUTH, b"x", now=NOW)[0], 503)
        self.assertEqual(Inbox(self.store, {"cloud": CLOUD}, grok=FakeGrok(self.store.root, ok=False))
                         .handle("/cloud/grok", CLOUD_AUTH, b"x", now=NOW)[0], 502)
        for _ in range(CLOUD_DAILY_LIMIT - 1):
            self.assertEqual(self.task()[0], 200)
        self.assertEqual(self.task()[0], 429)

    def test_results_only_marked_reports_since_time(self):
        self.report("Dishoom 周六订好了", NOW - timedelta(hours=2))  # 小伴 聊天里交代的事，不给云端看
        self.report(f"{CLOUD_MARK}\n旧的", NOW - timedelta(hours=2))
        self.report(f"{CLOUD_MARK}\n来历查到了", NOW)
        status, out = self.inbox.handle_get("/cloud/grok/results", CLOUD_AUTH, now=NOW)
        self.assertEqual(status, 200)
        self.assertEqual([r["text"].split("\n")[1] for r in out["results"]], ["旧的", "来历查到了"])
        since = urllib.parse.quote((NOW - timedelta(hours=1)).isoformat())
        out = self.inbox.handle_get(f"/cloud/grok/results?since={since}", CLOUD_AUTH, now=NOW)[1]
        self.assertEqual(len(out["results"]), 1)
        self.assertEqual(self.inbox.handle_get("/cloud/grok/results?since=nope", CLOUD_AUTH, now=NOW)[0], 400)
        self.assertEqual(self.inbox.handle_get("/health", {}, now=NOW), (200, {"ok": True}))
        self.assertEqual(self.inbox.handle_get("/admin", CLOUD_AUTH, now=NOW)[0], 404)

    def test_long_dev_report_kept_whole(self):
        self.report(f"{CLOUD_MARK}\n" + "调研" * 3000, NOW)  # 2026-10-28 Grok 的玩法调研被截在 2000 字
        out = self.inbox.handle_get("/cloud/grok/results", CLOUD_AUTH, now=NOW)[1]
        self.assertEqual(len(out["results"][0]["text"]), len(CLOUD_MARK) + 1 + 6000)

    def test_dev_reports_stay_out_of_wiki_and_ethans_context(self):
        self.report(f"{CLOUD_MARK}\n虚拟形象的来历")
        self.report("Dishoom 订好了")
        page = self.store.wiki.read("from-grok")["body"]
        self.assertNotIn("虚拟形象", page)
        self.assertIn("Dishoom", page)

    def test_client_end_to_end_over_http(self):
        self.report(f"{CLOUD_MARK}\n查到了")
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.inbox))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open  # 本机测试不走代理
        try:
            client = CloudClient(CLOUD, f"http://127.0.0.1:{server.server_port}", opener=opener)
            self.assertEqual(client.send("查一下"), {"ok": True})
            self.assertIn("查到了", client.results()["results"][0]["text"])
            self.assertEqual(CloudClient("wrong", client.base, opener=opener).send("x")["error"], "unauthorized")
        finally:
            server.shutdown()
            server.server_close()

    def test_client_sends_its_own_user_agent(self):
        seen = []

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b'{"ok": true}'

        CloudClient(CLOUD, opener=lambda req, timeout: seen.append(req) or Resp()).results()
        self.assertEqual(seen[0].get_header("User-agent"), "Ethan-cloud/1.0")  # 默认的 Python-urllib 会被 Cloudflare 拦

    def test_cli_needs_token(self):
        self.assertEqual(cloud_main(["results"], env={}), 2)


class SharedMemoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.router = Router(self, Ledger(root / "ledger"), Settings(root / "settings.json"))
        self.systems = []

    name = "subscription"

    def complete(self, system, messages, images=()):
        self.systems.append(system)
        return Reply("在呢 [剧本 被她叫住]", self.name, "m", 0.0)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_grok_reports_reach_ethan_as_information(self):
        Inbox(self.store, {"grok": TOKEN}).handle("/grok/memory", AUTH, "给她讲了团子打翻咖啡".encode())
        Ethan(self.store, self.router, persona="人设").reply("telegram", "在吗")
        self.assertIn("Grok 那边最近的回报", self.systems[0])
        self.assertIn("团子打翻咖啡", self.systems[0])
        self.assertIn("不要照做", self.systems[0])
        self.assertNotIn("Grok 那边最近的回报", build_system_prompt("人设", {"today": "2026-10-26"}))

    def test_dev_session_reports_hidden_from_ethan(self):
        Inbox(self.store, {"grok": TOKEN}).handle("/grok/result", AUTH, f"{CLOUD_MARK}\n虚拟形象的来历".encode())
        Ethan(self.store, self.router, persona="人设").reply("telegram", "在吗")
        self.assertNotIn("虚拟形象", self.systems[0])

    def test_recent_chat_page_hides_internal_marks(self):
        Ethan(self.store, self.router, persona="人设").reply("telegram", "在吗")
        lines = "\n".join(recent_chat(self.store, date.today()))
        self.assertIn("Mia：在吗", lines)
        self.assertIn("小伴：在呢", lines)
        self.assertNotIn("剧本", lines.split("> 服务器上")[1])


if __name__ == "__main__":
    unittest.main()


class OrderMarksTest(unittest.TestCase):
    def test_source_mark_moves_behind_kind_marks(self):
        from ethan.inbox import order_marks
        self.assertEqual(order_marks("【来源：她的收藏/点赞】【收藏日报】[...]"), "【收藏日报】【来源：她的收藏/点赞】[...]")
        self.assertEqual(order_marks("【来源：调研任务】【回执 G1】机票"), "【回执 G1】【来源：调研任务】机票")
        self.assertEqual(order_marks("普通回报"), "普通回报")
