"""借鉴清单剩下三条（docs/research/borrow-2026-11.md）：
A 临时状态到期（第 6 条）、B “我”页每条下面列意思相近的（第 8 条）、C 见面前提醒（第 10 条）。"""

import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import lifebase, memsearch, meetprep, memusers, mepage, nudge_registry, nudges, onto_query, portrait
from ethan.clock import LOCAL, her_today
from ethan.core.agent import build_system_prompt
from ethan.memory import MemoryStore

REPO = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:0", "2026-11-01T09:00:00", "mia：随便聊聊")
        self.today = her_today()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def stmt(self, fid):
        return self.db.conn.execute("SELECT s.* FROM statements s JOIN fact_map m ON m.statement_id = s.id WHERE m.fact_id = ?",
                                    (fid,)).fetchone()


# ---------------- A 临时状态到期 ----------------
class TempUntilTest(Base):
    def test_rule_dates(self):
        fri = date(2026, 10, 30)
        self.assertEqual(lifebase.temp_until("她这周在生病", fri), "2026-11-01")  # 到这周日
        self.assertEqual(lifebase.temp_until("她这几天在巴黎出差", fri), "2026-11-02")
        self.assertEqual(lifebase.temp_until("她这个月在减脂", fri), "2026-10-31")
        self.assertEqual(lifebase.temp_until("她今天很累", "2026-10-30T09:00:00"), "2026-10-30")
        self.assertEqual(lifebase.temp_until("她最近在减脂", fri), "2026-11-13")  # 自己判断——只说“最近”的算两周
        self.assertEqual(lifebase.temp_until("她2025年3月说最近几天在赶论文", "2025-03-10"), "2025-03-13")  # 这几天：按说的那天
        self.assertEqual(lifebase.temp_until("她2025年3月在赶论文", "2025-03-10"), "2025-03-31")  # 只写到月：到月底
        self.assertIsNone(lifebase.temp_until("她2025年10月起不喝奶茶", "2025-10-01"))  # 从那时起：长期的
        self.assertIsNone(lifebase.temp_until("她2016年硕士毕业", None))  # 背景
        self.assertIsNone(lifebase.temp_until("她住在伦敦", fri))
        self.assertTrue(lifebase.expired("2026-10-29", "2026-10-30"))
        self.assertFalse(lifebase.expired("2026-10-30", "2026-10-30"))  # 当天还算
        self.assertFalse(lifebase.expired(None, "2026-10-30"))

    def test_expired_is_not_current_anywhere_but_not_deleted(self):
        past = (self.today - timedelta(days=2)).isoformat()
        sick = lifebase.add_fact(self.db, "body", "她这周在生病", [self.ev], until=past, importance=4)
        home = lifebase.add_fact(self.db, "body", "她膝盖有旧伤", [self.ev])
        self.db.conn.commit()
        self.assertEqual(self.stmt(sick)["until"], past)  # 双写到说法
        self.assertEqual([f["id"] for f in lifebase.current_facts(self.db, "body")], [home])
        self.assertIn(sick, [f["id"] for f in lifebase.current_facts(self.db, "body", include_expired=True)])
        row = self.db.conn.execute("SELECT valid_to, status FROM facts WHERE id = ?", (sick,)).fetchone()
        self.assertEqual(tuple(row), (None, "active"))  # 不删、不自动结束
        self.assertEqual(lifebase.reconcile_statements(self.db), [])
        # 画像：小伴 读到的里没有到期的
        portrait.rebuild_blocks(self.db, None)
        text = portrait.chat_text(self.db, "ethan")
        self.assertIn("膝盖", text)
        self.assertNotIn("生病", text)
        # 题函数：按“现在”算的不给（11-06 起统一走 lifebase.fact_state / MEM_VIEWS）
        self.assertFalse(lifebase.mem_ok("now", self.stmt(sick), self.today.isoformat()))
        self.assertTrue(lifebase.mem_ok("now", self.stmt(home), self.today.isoformat()))
        self.assertEqual(lifebase.fact_state(self.stmt(sick), self.today.isoformat()), "expired")
        # 检索：还查得到，但写明到期了
        memsearch.refresh(self.db)
        hit = [r for r in memsearch.search(self.db, ["生病"], user="ethan", log=False) if r["key"] == f"fact:{sick}"]
        self.assertTrue(hit and "到期了" in hit[0]["text"])

    def test_expired_states_for_ethan_prompt_only_inside(self):
        past = (self.today - timedelta(days=1)).isoformat()
        lifebase.add_fact(self.db, "rhythms", "她这几天在巴黎出差", [self.ev], until=past)
        lifebase.add_fact(self.db, "rhythms", "她这周在赶论文", [self.ev], until=(self.today + timedelta(days=3)).isoformat())
        self.db.conn.commit()
        lines = onto_query.expired_lines(self.db, "ethan")
        self.assertIn("巴黎出差", lines)
        self.assertNotIn("赶论文", lines)  # 没到期的照旧是现在
        self.assertEqual(onto_query.expired_states(self.db, "twin")["items"], [])  # 对外拿不到
        prompt = build_system_prompt("我是 小伴", {"today": self.today.isoformat(), "expired": lines})
        self.assertIn("已经到期的临时状态", prompt)

    def test_fact_ops_writes_until(self):
        day = date(2026, 10, 30)
        data = {"ops": [{"from": "n1", "op": "ADD", "dimension": "body", "statement": "她这周在生病", "until": None},
                        {"from": "n2", "op": "ADD", "dimension": "rhythms", "statement": "她下周三前都在巴黎", "until": "2026-11-04"},
                        {"from": "n3", "op": "ADD", "dimension": "preferences", "statement": "她喜欢克莱因蓝"}]}
        self.assertEqual(portrait.check_ops({"ops": [{"from": "n1", "op": "ADD", "dimension": "body", "statement": "x",
                                                      "valid_from": "2026-10-30", "until": "2026-10-01"}]}, ["x"], set()),
                         ["ops[0].until 早于 valid_from"])
        portrait.apply_ops(self.db, data, self.ev, day)
        got = {r["statement"]: r["until"] for r in self.db.conn.execute("SELECT statement, until FROM facts")}
        self.assertEqual(got, {"她这周在生病": "2026-11-01", "她下周三前都在巴黎": "2026-11-04", "她喜欢克莱因蓝": None})

    def test_prompt_version_and_eval_cases(self):
        self.assertTrue((REPO / "ethan/prompts/fact_ops.md").read_text(encoding="utf-8").startswith("version: 2"))
        from ethan import evals
        cases = {c["id"]: c for c in evals.load("fact_ops")}
        for k in ("add-temp-sick", "add-temp-trip", "update-move-no-until"):
            self.assertIn(k, cases)
        ok, _ = evals._score_fact_ops({"ops": [{"from": "n1", "op": "ADD", "dimension": "body", "until": "2026-11-01"}]},
                                      cases["add-temp-sick"]["expect"])
        self.assertTrue(ok)
        ok, note = evals._score_fact_ops({"ops": [{"from": "n1", "op": "UPDATE", "fact_id": 4, "dimension": "resources", "until": "2026-11-01"}]},
                                         cases["update-move-no-until"]["expect"])
        self.assertFalse(ok)
        self.assertIn("until", note)

    def test_nightly_asks_her_about_expired(self):
        lifebase.add_fact(self.db, "body", "她这周在生病", [self.ev], until=(self.today - timedelta(days=1)).isoformat())
        self.db.conn.commit()
        self.assertEqual(mepage.refresh(self.db, today=self.today)["expire"], 1)

    def test_long_past_expiry_not_asked(self):
        """11-07：早就过去的旧事按到期处理，不进待你定问她；两周内刚到期的才问。"""
        lifebase.add_fact(self.db, "rhythms", "她2025年3月在赶论文", [self.ev], until="2025-03-31")
        lifebase.add_fact(self.db, "body", "她这周在生病", [self.ev], until=(self.today - timedelta(days=2)).isoformat())
        self.db.conn.commit()
        self.assertEqual(mepage.refresh(self.db, today=self.today)["expire"], 1)

    def test_migration_backfills_only_unlocked_temp_states(self):
        from ethan import locks
        a = lifebase.add_fact(self.db, "body", "她这周在生病", [self.ev], valid_from="2026-10-30")
        b = lifebase.add_fact(self.db, "body", "她这几天在出差", [self.ev], valid_from="2026-10-30", locked=True)
        c = lifebase.add_fact(self.db, "rhythms", "她这个月在减脂", [self.ev], valid_from="2026-10-30")
        d = lifebase.add_fact(self.db, "rhythms", "她最近在减脂", [self.ev], valid_from="2026-10-30")
        self.db.conn.commit()
        locks.lock(self.db, "fact", c, "until")  # 她在工作台改过的不动
        spec = importlib.util.spec_from_file_location("m", REPO / "migrations/20261105_temp_states_until.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn("原话", mod.__doc__)
        self.assertIn("2 条", mod.run(self.store))  # 11-07 起“最近”的也补（两周）
        got = {r["id"]: r["until"] for r in self.db.conn.execute("SELECT id, until FROM facts")}
        self.assertEqual((got[a], got[b], got[c], got[d]), ("2026-11-01", None, None, "2026-11-13"))
        self.assertEqual(self.stmt(a)["until"], "2026-11-01")


# ---------------- B 每条下面意思相近的 ----------------
class SimilarTest(Base):
    def vec(self, fid, v):
        self.db.conn.execute(memsearch.VEC_SCHEMA)
        n = sum(x * x for x in v) ** 0.5
        self.db.conn.execute("INSERT OR REPLACE INTO mem_vec (key, hash, vec) VALUES (?, 'h', ?)",
                             (f"fact:{fid}", memsearch._pack([x / n for x in v])))

    def test_each_item_lists_similar_ones_for_her_only(self):
        a = lifebase.add_fact(self.db, "rhythms", "她通常一点睡", [self.ev])
        b = lifebase.add_fact(self.db, "rhythms", "她一般凌晨一点才睡", [self.ev], locked=True)
        c = lifebase.add_fact(self.db, "rhythms", "她十二点前睡", [self.ev])
        lifebase.supersede(self.db, c, a)  # 旧的也列出来，标“过时了”
        d = lifebase.add_fact(self.db, "preferences", "她不吃香菜", [self.ev])
        self.db.conn.commit()
        memsearch.refresh(self.db)
        self.vec(a, [1, 0.1, 0]), self.vec(b, [1, 0.12, 0]), self.vec(c, [0.9, 0.3, 0]), self.vec(d, [0, 0, 1])
        items = {x["id"]: x for x in mepage.all_memory(self.db)["items"]}
        sims = items[a]["similar"]
        self.assertEqual([s["id"] for s in sims], [b, c])  # 最像的在前，不像的（香菜）不列
        self.assertTrue(sims[0]["locked"])
        self.assertEqual(sims[1]["past"], "过时了")
        self.assertEqual(items[d]["similar"], [])
        self.assertEqual(self.db.conn.execute("SELECT user FROM retrieval_log WHERE scene = 'similar'").fetchone()[0], "me_similar")

    def test_only_registered_users_and_no_vectors_is_empty(self):
        with self.assertRaises(ValueError):
            memsearch.similar_to(self.db, ["fact:1"], user="newcomer")
        self.assertEqual(memsearch.similar_to(self.db, ["fact:1"], user="me_similar"), {})
        self.assertIn("me_similar", memusers.USERS)

    def test_frontend_shows_similar(self):
        js = (REPO / "ethan/workbench/static/app.js").read_text(encoding="utf-8")
        self.assertIn("意思相近的还有", js)
        self.assertIn(".mesim", (REPO / "ethan/workbench/static/app.css").read_text(encoding="utf-8"))


# ---------------- C 见面前提醒 ----------------
class FakeCal:
    def __init__(self, days):
        self.days = days

    def busy(self, day):
        return self.days.get(day, [])


class MeetTest(Base):
    def setUp(self):
        super().setUp()
        from ethan import relations
        relations.ensure(self.db)
        self.leo = self.db.upsert_person("Leo", circle="同学", recent="刚换了工作去做咨询", last_contact="2026-10-20",
                                         open_loops=json.dumps([{"kind": "they_asked", "text": "问她要不要一起去滑雪", "date": "2026-10-20"}],
                                                               ensure_ascii=False))
        self.db.upsert_person("小林", circle="挚友", recent="在准备搬家")
        self.db.upsert_person("小林子", circle="其他")
        self.db.upsert_person("Al")  # 名字太短不认
        self.db.upsert_person("签证代办王")  # 像商家的不认
        self.db.conn.commit()
        self.day = date(2026, 11, 10)

    def test_match_names(self):
        self.assertEqual([n for _, n in meetprep.match(self.db, "和 Leo 吃晚饭")], ["Leo"])
        self.assertEqual(meetprep.match(self.db, "Leonardo exhibition"), [])  # 英文按整词
        self.assertEqual([n for _, n in meetprep.match(self.db, "小林子生日聚会")], ["小林子"])  # 长名字包住的短名字不算
        self.assertEqual(meetprep.match(self.db, "Al 的课"), [])
        self.assertEqual(meetprep.match(self.db, "找签证代办王递材料"), [])

    def test_evening_before_then_not_again(self):
        cal = FakeCal({self.day + timedelta(days=1): [("19:00", "21:00", "和 Leo 吃晚饭", {})]})
        now = datetime(2026, 11, 10, 21, 0, tzinfo=LOCAL)
        self.assertEqual(meetprep.scan(self.db, cal, self.day, now=now.astimezone(timezone.utc)), 1)
        got = nudges.candidates(self.db, now, {})
        key, text = next((k, t) for k, t in got if k.startswith("meet:"))
        self.assertEqual(key, f"meet:2026-11-11:19:00:{self.leo}")
        self.assertTrue(nudge_registry.registered(key))
        for want in ("明天 19:00", "Leo", "刚换了工作去做咨询", "滑雪"):
            self.assertIn(want, text)
        nudges.mark_told(self.db, [key])
        later = datetime(2026, 11, 11, 17, 0, tzinfo=LOCAL)  # 第二天见面前也不再提
        self.assertIsNone(meetprep.due(self.db, later, {}))

    def test_same_day_before_window_and_quiet_otherwise(self):
        cal = FakeCal({self.day: [("19:00", "21:00", "和小林看电影", {})]})
        meetprep.scan(self.db, cal, self.day, force=True)
        self.assertIsNone(meetprep.due(self.db, datetime(2026, 11, 10, 15, 0, tzinfo=LOCAL), {}))  # 太早
        key, text = meetprep.due(self.db, datetime(2026, 11, 10, 17, 0, tzinfo=LOCAL), {})
        self.assertIn("今天 19:00", text)
        self.assertIn("在准备搬家", text)
        self.assertIsNone(meetprep.due(self.db, datetime(2026, 11, 10, 18, 30, tzinfo=LOCAL), {}))  # 只差半小时，不提了

    def test_rescan_drops_moved_and_unreadable_keeps(self):
        cal = FakeCal({self.day: [("19:00", "21:00", "和小林看电影", {})]})
        meetprep.scan(self.db, cal, self.day, force=True)
        meetprep.scan(self.db, FakeCal({}), self.day, force=True)  # 她删了
        self.assertEqual(meetprep.upcoming(self.db, self.day), [])

        class Broken:
            def busy(self, day):
                raise OSError("断了")
        meetprep.scan(self.db, cal, self.day, force=True)
        meetprep.scan(self.db, Broken(), self.day, force=True)
        self.assertEqual(len(meetprep.upcoming(self.db, self.day)), 1)  # 读不到不等于没约

    def test_registered_and_private(self):
        self.assertIn("heads_up", memusers.USERS)
        self.assertEqual(memusers.tier_of("heads_up"), "internal")
        self.assertEqual(onto_query.person(self.db, "twin", "Leo")["items"], [])  # 人物卡对外一律拿不到
        self.assertEqual(meetprep.brief(self.db, "没这个人", self.day), "")
        lifebase.hold(self.db, "测试")  # 记忆暂停中不提
        self.assertEqual(meetprep.brief(self.db, "Leo", self.day), "")


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
