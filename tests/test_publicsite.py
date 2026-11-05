import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import publicsite
from ethan.core.agent import Ethan
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

SITE, CLOUD = "s" * 40, "c" * 40
NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def auth(t):
    return {"Authorization": f"Bearer {t}"}


def visit(vid="v1", at="2026-10-30T11:00:00Z", **kw):
    return json.dumps({"name": "Anna", "contact": "anna@example.com", "org": "", "intent": "合作", "message": "想聊聊品牌合作",
                       "page": "/", "lang": "zh", "at": at, "visitor_id": vid, **kw}, ensure_ascii=False).encode()


class PublicSiteTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        self.inbox = Inbox(self.store, {"grok": "g" * 40, "cloud": CLOUD, "site": SITE})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_visitor_stored_once_then_triaged_daily_with_cap(self):
        self.assertEqual(self.inbox.handle("/cloud/visitor", {}, visit(), NOW)[0], 401)
        self.assertEqual(self.inbox.handle("/cloud/visitor", auth(SITE), visit(), NOW), (200, {"ok": True}))
        self.assertEqual(self.inbox.handle("/cloud/visitor", auth(SITE), visit(), NOW)[1].get("duplicate"), True)  # 中转重送
        for i in range(5):
            self.inbox.handle("/cloud/visitor", auth(SITE), visit(f"v{i + 2}", message=f"合作邀约 {i}"), NOW)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)  # 来了先存着
        self.assertEqual(self.db.conn.execute("SELECT source FROM evidence").fetchone()[0], "visitor")
        ids = [r[0] for r in self.db.conn.execute("SELECT id FROM visitor_messages ORDER BY id")]
        answer = {"items": [{"id": ids[0], "tier": "fyi", "summary": "Anna 夸主页"}]
                  + [{"id": i, "tier": "reply", "priority": p, "summary": f"品牌合作 {p}"} for i, p in zip(ids[1:5], (2, 5, 4, 3))]
                  + [{"id": ids[5], "tier": "junk", "summary": "广告"}]}
        router = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text=json.dumps(answer, ensure_ascii=False)))
        self.assertIsNone(publicsite.triage_due(self.db, router, datetime(2026, 10, 30, 12, 0)))  # 傍晚才分
        got = publicsite.triage_due(self.db, router, datetime(2026, 10, 30, 18, 0))
        self.assertEqual(got, {"triaged": 6, "tasks": 3})
        titles = [r[0] for r in self.db.conn.execute("SELECT title FROM tasks ORDER BY id")]
        self.assertEqual(titles, ["回复 Anna（主页留言）：品牌合作 5", "回复 Anna（主页留言）：品牌合作 4", "回复 Anna（主页留言）：品牌合作 3"])
        self.assertIsNone(publicsite.triage_due(self.db, router, datetime(2026, 10, 30, 19, 0)))  # 一天一次
        from ethan import nudges
        key, text = nudges._mail(self.db, datetime(2026, 10, 30, 19, 0), {})
        self.assertIn("还有 1 条也要她回", text)
        self.assertIn("1 条不用她回", text)
        nudges.mark_told(self.db, [key])
        self.assertIsNone(nudges._mail(self.db, datetime(2026, 10, 30, 19, 10), {}))
        empty = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text='{"items": []}'))
        self.db.conn.execute("UPDATE tasks SET created_at = '2026-10-30T18:00:00'")  # 待办按真实时间建，测试里固定成“昨天”
        self.assertEqual(publicsite.triage_due(self.db, empty, datetime(2026, 10, 31, 18, 0)), {"triaged": 0, "tasks": 1})  # 排队的第二天进

    def test_other_intent_spam_and_limits(self):
        self.inbox.handle("/cloud/visitor", auth(SITE), visit("a", intent="其他", message="主页好看"), NOW)
        self.inbox.handle("/cloud/visitor", auth(SITE), visit("b", at="x", intent="瞎填", message="hi"), NOW)
        self.inbox.handle("/cloud/visitor", auth(SITE), visit("c", message="http://a http://b http://c http://d"), NOW)
        rows = {r["visitor_id"]: dict(r) for r in self.db.conn.execute("SELECT * FROM visitor_messages")}
        self.assertEqual((rows["a"]["status"], rows["b"]["intent"], rows["c"]["status"]), ("new", "其他", "spam"))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        for i in range(5):
            code = self.inbox.handle("/cloud/visitor", auth(SITE), visit("d", at=f"t{i}"), NOW)[0]
        self.assertEqual(code, 200)
        self.assertEqual(self.inbox.handle("/cloud/visitor", auth(SITE), visit("d", at="t9"), NOW)[0], 429)
        self.assertEqual(self.inbox.handle("/cloud/visitor", auth(SITE), b"not json", NOW)[0], 400)

    def test_honeypot_is_junk_and_agent_shown(self):
        """2026-10-31 主页会话：suspect=honeypot 进垃圾档不删；via_agent 显示“由 agent 代留”，照常分档。"""
        from ethan import desk
        self.inbox.handle("/cloud/visitor", auth(SITE), visit("bot", suspect="honeypot", message="cheap seo"), NOW)
        self.inbox.handle("/cloud/visitor", auth(SITE), visit("amy", via_agent="某某助手，替 Amy 联系", email="amy@example.com"), NOW)
        rows = {r["visitor_id"]: dict(r) for r in self.db.conn.execute("SELECT * FROM visitor_messages")}
        self.assertEqual((rows["bot"]["status"], rows["bot"]["tier"], rows["bot"]["suspect"]), ("spam", "junk", "honeypot"))
        self.assertEqual((rows["amy"]["status"], rows["amy"]["via_agent"]), ("new", "某某助手，替 Amy 联系"))
        seen = []
        answer = {"items": [{"id": rows["amy"]["id"], "tier": "fyi", "summary": "Amy 想约聊"}]}

        def complete(system, messages, purpose="chat", images=()):
            seen.append(messages[0]["content"])
            return SimpleNamespace(text=json.dumps(answer, ensure_ascii=False))
        self.assertEqual(desk.items(self.db), [])  # 分档前工作台不显示（2026-10-31 她定）
        publicsite.triage(self.db, SimpleNamespace(complete=complete), "2026-10-30")
        self.assertIn("由 AI agent「某某助手，替 Amy 联系」代留", seen[0])
        self.assertNotIn("cheap seo", seen[0])  # 机器人那条不送去分档
        self.db.conn.execute("UPDATE visitor_messages SET draft = '你好 Amy' WHERE visitor_id = 'amy'")
        got = [x for x in desk.items(self.db) if x["kind"] == "visitor"]
        self.assertEqual([x["via_agent"] for x in got], ["某某助手，替 Amy 联系"])

    def test_life_feed_only_after_she_says_yes(self):
        reply = "要不要放主页？[主页动态 她 第一次攀岩｜今天第一次去攀岩，手酸但很开心。][主页动态 我 陪她过关｜今天她拿下了新手关。]"
        self.assertEqual(publicsite.note_reply(self.db, reply), ["feed-draft", "feed-draft"])
        self.assertEqual(publicsite.strip(reply), "要不要放主页？")
        self.assertEqual(self.inbox.handle_get("/cloud/public/feed?lang=zh", auth(SITE), NOW), (200, {"ok": True, "items": []}))
        self.assertFalse(publicsite.note_her(self.db, "等一下"))
        self.assertTrue(publicsite.note_her(self.db, "可以"))
        code, got = self.inbox.handle_get("/cloud/public/feed?lang=zh", auth(SITE), NOW)
        self.assertEqual([(x["who"], x["title"], x["lang"]) for x in got["items"]], [("ethan", "陪她过关", "zh"), ("mia", "第一次攀岩", "zh")])
        self.assertEqual(len(self.inbox.handle_get("/cloud/public/feed?lang=en", auth(SITE), NOW)[1]["items"]), 2)  # 没有英文的就都给
        self.assertEqual(self.inbox.handle_get("/cloud/public/feed", {}, NOW)[0], 401)

    def test_site_key_only_opens_three_doors(self):
        self.assertEqual(self.inbox.handle_get("/cloud/health", auth(SITE), NOW)[0], 401)
        self.assertEqual(self.inbox.handle_get("/cloud/grok/results", auth(SITE), NOW)[0], 401)
        self.assertEqual(self.inbox.handle("/cloud/grok", auth(SITE), b'{"task": "x"}', NOW)[0], 401)
        code, body = self.inbox.handle_get("/cloud/profile", auth(SITE), NOW)
        self.assertEqual(code, 200)
        self.assertEqual(set(body), {"ok", "city", "birthday"})
        self.assertIsInstance(body["birthday"], bool)
        self.assertIn("source", self.inbox.handle_get("/cloud/profile", auth(CLOUD), NOW)[1])
        self.assertEqual(self.inbox.handle_get("/cloud/public/now", auth(SITE), NOW), (200, {"ok": True, "text": None, "at": None, "city": None}))

    def test_now_needs_her_yes(self):
        answers = ["要不要放主页？[主页近况 在伦敦读书，顺便做一个叫 DemoApp 的小产品]", "好，发上去了", "收到 [主页近况 撤下]"]

        def complete(system, messages, purpose="chat", images=()):
            return SimpleNamespace(text=answers.pop(0), backend="x", model="m", cost_usd=0)
        ethan = Ethan(self.store, SimpleNamespace(complete=complete, settings=None), persona="人设")
        out = ethan.reply("cli", "最近在做 DemoApp", today=date(2026, 10, 30))
        self.assertEqual(out["bubbles"], ["要不要放主页？"])
        self.assertIsNone(publicsite.public_now(self.db)["text"])  # 只是草稿
        self.assertFalse(publicsite.note_her(self.db, "等等我想想"))
        ethan.reply("cli", "好", today=date(2026, 10, 30))
        self.assertEqual(publicsite.public_now(self.db)["text"], "在伦敦读书，顺便做一个叫 DemoApp 的小产品")
        ethan.reply("cli", "撤掉吧", today=date(2026, 10, 30))
        self.assertIsNone(publicsite.public_now(self.db)["text"])


if __name__ == "__main__":
    unittest.main()
