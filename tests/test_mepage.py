import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import lifebase, memsearch, memusers, mepage
from ethan.memory import MemoryStore


class Router:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, system, messages, purpose="chat", images=()):
        return SimpleNamespace(text=self.replies.pop(0))


class MePageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        mepage.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:1", "2026-10-30", "她说的")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_conflict_with_locked_goes_to_pending_and_pick_rules(self):
        mine = lifebase.add_fact(self.db, "rhythms", "每周练 5 次，周末休息", [self.ev], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (mine,))
        guess = lifebase.add_fact(self.db, "rhythms", "每周练 4 次", [self.ev])
        self.assertFalse(lifebase.supersede(self.db, mine, guess))  # 锁定的不能自己改，进“待你定”
        items = mepage.pending_items(self.db)
        self.assertEqual(len(items), 1)
        self.assertEqual([o["mine"] for o in items[0]["options"]], [True, False])  # 标出哪个是她说过的
        self.assertEqual(mepage.pending_count(self.db), 1)
        out = mepage.resolve(self.db, items[0]["id"], "pick", pick=0)  # 选她说过的：系统推的作废
        self.assertTrue(out["ok"])
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (guess,)).fetchone()[0], "invalid")
        self.assertEqual(mepage.pending_count(self.db), 0)
        self.assertFalse(mepage.resolve(self.db, items[0]["id"], "pick", pick=0)["ok"])
        self.assertEqual(out["undo"]["action"], "me.reopen")  # 点错了能撤销：恢复原样、回到待你定
        mepage.reopen(self.db, items[0]["id"])
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (guess,)).fetchone()[0], "candidate")
        self.assertEqual(mepage.pending_count(self.db), 1)

    def test_picking_the_new_one_keeps_hers_as_history(self):
        mine = lifebase.add_fact(self.db, "preferences", "不吃香菜", [self.ev], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (mine,))
        new = lifebase.add_fact(self.db, "preferences", "现在开始吃香菜了", [self.ev])
        lifebase.supersede(self.db, mine, new)
        pid = mepage.pending_items(self.db)[0]["id"]
        mepage.resolve(self.db, pid, "pick", pick=1)  # 习惯变了：旧的不是错，留作历史
        old = self.db.conn.execute("SELECT status, valid_to FROM facts WHERE id = ?", (mine,)).fetchone()
        self.assertEqual(old["status"], "active")
        self.assertIsNotNone(old["valid_to"])
        hist = mepage.all_memory(self.db, history=True)["items"]
        self.assertIn("不吃香菜", [x["text"] for x in hist])

    def test_own_text_with_validity_and_both_true(self):
        a = lifebase.add_fact(self.db, "rhythms", "每周练 5 次", [self.ev], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (a,))
        b = lifebase.add_fact(self.db, "rhythms", "每周练 3 次", [self.ev])
        lifebase.supersede(self.db, a, b)
        pid = mepage.pending_items(self.db)[0]["id"]
        mepage.resolve(self.db, pid, "both", ranges=[{"since": "2026-10-26", "until": "2026-12-31"}, {"since": "2027-01-01"}])
        rows = {r["id"]: dict(r) for r in self.db.conn.execute("SELECT id, until, valid_from, locked FROM facts")}
        self.assertEqual((rows[a]["until"], rows[b]["valid_from"], rows[b]["locked"]), ("2026-12-31", "2027-01-01", 1))
        # 自己写的：长期带有效期
        c = lifebase.add_fact(self.db, "preferences", "喜欢早上出门", [self.ev])
        for _ in range(3):
            self.db.conn.execute("INSERT INTO retrieval_log (at, scene, round, queries, keys, user) VALUES (datetime('now'), 'chat', 1, '[]', ?, 'ethan')",
                                 (json.dumps([f"fact:{c}"]),))
        self.assertEqual(mepage.refresh(self.db)["confirm"], 1)  # 常被用到、没确认过
        pid = next(p["id"] for p in mepage.pending_items(self.db) if p["kind"] == "confirm")
        mepage.resolve(self.db, pid, "own", text="赛季里早上出门，平时随意", since="2026-10-26", until="2026-12-31")
        new = self.db.conn.execute("SELECT * FROM facts WHERE statement = '赛季里早上出门，平时随意'").fetchone()
        self.assertEqual((new["locked"], new["until"]), (1, "2026-12-31"))
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (c,)).fetchone()[0], "invalid")

    def test_expiry_asks_again(self):
        f = lifebase.add_fact(self.db, "rhythms", "这个赛季每周练 5 次", [self.ev], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET locked = 1, until = '2026-12-31' WHERE id = ?", (f,))
        self.assertEqual(mepage.refresh(self.db, today=date(2027, 1, 1))["expire"], 1)
        pid = mepage.pending_items(self.db)[0]["id"]
        mepage.resolve(self.db, pid, "no")  # 不算了：到期留作历史，不是错
        r = self.db.conn.execute("SELECT status, valid_to FROM facts WHERE id = ?", (f,)).fetchone()
        self.assertEqual(r["status"], "active")
        self.assertIsNotNone(r["valid_to"])
        g = lifebase.add_fact(self.db, "resources", "在伦敦交换", [self.ev], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET until_cond = 'city:London' WHERE id = ?", (g,))
        self.assertEqual(mepage.refresh(self.db, city="Shenzhen")["expire"], 1)

    def test_ask_and_verdict(self):
        fid = lifebase.add_fact(self.db, "rhythms", "每周练 5 次", [self.ev], confidence=1.0)
        memsearch.refresh(self.db)
        got = mepage.ask(self.db, Router(["一周 5 次", "我不知道"]), "我每周练几次")
        self.assertEqual([a["user"] for a in got["answers"]], ["ethan", "twin"])
        self.assertIn(f"fact:{fid}", got["answers"][0]["keys"])
        self.assertEqual(got["answers"][1]["keys"], [])  # Days只能看公开的，私密的查不到
        out = mepage.verdict(self.db, got["id"], {"ethan": "ok", "twin": "bad"})
        self.assertEqual(len(out["tickets"]), 1)  # 说错的那个开一张纠错单
        t = self.db.conn.execute("SELECT user, entry, right FROM corrections").fetchone()
        self.assertEqual(tuple(t), ("twin", "ask", "一周 5 次"))
        self.assertEqual(mepage.recent_questions(self.db), ["我每周练几次"])
        both_bad = mepage.ask(self.db, Router(["4 次", "6 次"]), "我每周练几次")
        self.assertFalse(mepage.verdict(self.db, both_bad["id"], {"ethan": "bad", "twin": "bad"})["ok"])  # 都不对要写一句
        self.assertEqual(len(mepage.verdict(self.db, both_bad["id"], {"ethan": "bad", "twin": "bad"}, text="现在每周 3 次", kind="changed")["tickets"]), 2)
        self.assertEqual(mepage.compare(Router(['{"conflict": true, "why": "次数不同"}']), "5 次", "3 次")["conflict"], True)

    def test_undo_ticket_restores(self):
        old = lifebase.add_fact(self.db, "preferences", "她住在 Camden 公寓", [self.ev])
        t = memusers.open_ticket(self.db, "她住在 Greenwich 公寓", "她住在 Camden")
        self.assertTrue(mepage.undo_ticket(self.db, t["id"])["ok"])
        self.assertEqual(self.db.conn.execute("SELECT status, valid_to FROM facts WHERE id = ?", (old,)).fetchone()[0], "active")
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (t["fact_id"],)).fetchone()[0], "invalid")
        self.assertEqual(mepage.page(self.db)["tickets"], [])

    def test_undo_ticket_when_clock_ticks_between_fact_and_ticket(self):
        """服务器慢时新说法和纠错单不在同一秒（上线测试没过）：照样作废新记的那条。"""
        lifebase.add_fact(self.db, "preferences", "她住在 Camden 公寓", [self.ev])
        t = memusers.open_ticket(self.db, "她住在 Greenwich 公寓", "她住在 Camden")
        self.db.conn.execute("UPDATE facts SET recorded_at = '2026-11-04T10:00:00+00:00' WHERE id = ?", (t["fact_id"],))
        self.db.conn.execute("UPDATE corrections SET at = '2026-11-04T10:00:01+00:00' WHERE id = ?", (t["id"],))
        self.assertTrue(mepage.undo_ticket(self.db, t["id"])["ok"])
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (t["fact_id"],)).fetchone()[0], "invalid")

    def test_page_orders_users_and_counts_only_real_errors(self):
        memusers.open_ticket(self.db, "现在吃香菜", "不吃香菜", kind="changed")  # 变了：不算错
        p = mepage.page(self.db)
        self.assertEqual([u["user"] for u in p["users"]], ["ethan", "twin", "site", "os"])  # 按实体（不要把每个任务都放上来；11-01：Days 是聊天沟通的，和做文案的个人主页分开）
        self.assertIn("计划器", p["users"][3]["parts"])
        self.assertEqual(p["wrong"], 0)
        self.assertEqual(p["tickets"][0]["kind"], "changed")

    def test_open_question_without_fact(self):
        """画像想问的、没有现成说法的：只能自己写；没理就两周后过期；同一句不重复进。"""
        pid = mepage.add_question(self.db, "你现在每周练几次？")
        self.assertEqual(mepage.add_question(self.db, "你现在每周练几次？"), 0)
        self.assertFalse(mepage.resolve(self.db, pid, "yes")["ok"])
        self.assertTrue(mepage.resolve(self.db, pid, "own", text="现在每周 3 次")["ok"])
        self.assertEqual(self.db.conn.execute("SELECT locked FROM facts WHERE statement = '现在每周 3 次'").fetchone()[0], 1)
        old = mepage.add_question(self.db, "你还在学游泳吗？")
        self.db.conn.execute("UPDATE pending SET created_at = '2026-01-01' WHERE id = ?", (old,))
        self.assertEqual(mepage.expire_questions(self.db, date(2026, 10, 31)), 1)
        self.assertEqual(mepage.pending_count(self.db), 0)

    def test_all_memory_tabs(self):
        """全部记忆四个标签——关于你、发生过的事、人（收藏和资料是原来的大脑页）；不再只给 300 条。"""
        for i in range(120):
            lifebase.add_fact(self.db, "preferences", f"喜欢第 {i} 种东西", [self.ev])
        got = mepage.all_memory(self.db, limit=100)
        self.assertEqual((len(got["items"]), got["more"]), (100, True))
        self.assertEqual(len(mepage.all_memory(self.db, limit=200)["items"]), 120)
        self.store.wiki.write("journal/2026-10-30", "记忆", "# 2026-10-30 的记忆\n\n她去攀岩，手很酸。\n\n心情：开心")
        lifebase.add_event(self.db, "2026-10-31", "错过 Hidden London", [self.ev], "manual", summary="出门晚了没赶上")
        days = mepage.days(self.db, self.store.wiki)["items"]
        self.assertEqual([d["day"] for d in days], ["2026-10-31", "2026-10-30"])
        self.assertEqual((days[0]["events"][0]["title"], days[1]["journal"]), ("错过 Hidden London", "她去攀岩，手很酸。"))
        self.assertEqual([d["day"] for d in mepage.days(self.db, self.store.wiki, "攀岩")["items"]], ["2026-10-30"])
        self.db.upsert_person("Amy", circle="某商学院 同学", last_contact="2026-10-28")
        self.assertEqual(mepage.people(self.db, "某商学院")["items"][0]["name"], "Amy")

    def test_yes_with_new_dates(self):
        """她在待你定里改了时间，点“对”要一起存。"""
        fid = lifebase.add_fact(self.db, "resources", "家里没有秤", [self.ev])
        pid = mepage.add_question(self.db, "家里还是没有秤吗？", fid)
        self.assertTrue(mepage.resolve(self.db, pid, "yes", since="2026-10-20", until="2026-11-15")["ok"])
        f = self.db.conn.execute("SELECT valid_from, until, locked FROM facts WHERE id = ?", (fid,)).fetchone()
        self.assertEqual(tuple(f), ("2026-10-20", "2026-11-15", 1))

    def test_chat_marks(self):
        a = lifebase.add_fact(self.db, "rhythms", "每周练 5 次", [self.ev], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (a,))
        b = lifebase.add_fact(self.db, "rhythms", "每周练 4 次", [self.ev])
        lifebase.supersede(self.db, a, b)
        pid = mepage.pending_items(self.db)[0]["id"]
        self.assertEqual(mepage.strip(f"好的[待定 #{pid} 选 1]"), "好的")
        mepage.note_chat(self.db, f"记下了[待定 #{pid} 选 1]")
        self.assertEqual(mepage.pending_count(self.db), 0)  # 聊天里定了，这里同样少一件



class AllMemorySearchTest(unittest.TestCase):
    """在“全部记忆”里搜“某商学院”“读过的学校”都是“没有”。"""

    def test_case_insensitive_synonyms_and_source_label(self):
        import tempfile
        from pathlib import Path
        from ethan import lifebase, mepage
        from ethan.memory import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            db = store.db
            lifebase.ensure(db)
            ev = lifebase.add_evidence(db, "file", "file:x", "2026-11-03", "简历", path="handoff/简历.pdf")
            lifebase.add_fact(db, "abilities", "在某大学读统计学本科（2014-2018）", [ev], context="在她发来的文件《简历》里写的")
            lifebase.add_fact(db, "resources", "在 某商学院 交换", [ev])
            db.conn.commit()
            texts = lambda q: [x["text"] for x in mepage.all_memory(db, q)["items"]]
            self.assertEqual(texts("某商学院"), ["在 某商学院 交换"])
            got = mepage.all_memory(db, "某商学院")
            self.assertEqual((got["total"], [(x["key"], x["n"]) for x in got["dims"]]), (1, [("resources", 1)]))  # 标签数跟着搜索结果
            self.assertIn("在某大学读统计学本科（2014-2018）", texts("读过的学校"))
            item = mepage.all_memory(db, "本科")["items"][0]
            self.assertEqual(item["said"], "你给的资料")
            self.assertIn("简历", item["context"])
            store.close()


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
