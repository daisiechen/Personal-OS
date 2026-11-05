"""ontology 3.4（旧结构下线体检）、4.1（升级记录）、4.2（候选题）、4.3（正式评估）。"""

import importlib.util
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import formaleval, lifebase, memsearch, memusers, mepage, onto_query, retire, upgrades, weekreview
from ethan.core.router import Settings
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 11, 4)


def iso(d, h=10):
    return datetime.combine(d, datetime.min.time(), timezone.utc).replace(hour=h).isoformat(timespec="seconds")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name) / "data"
        self.store = MemoryStore(self.data)
        self.db = self.store.db
        lifebase.ensure(self.db)
        mepage.ensure(self.db)
        memusers.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:x:1", "2026-11-01", "mia：随便聊聊")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fact(self, text, dim="preferences", kind="preference", vis="private", at=None):
        fid = lifebase.add_fact(self.db, dim, text, [self.ev], kind=kind, source_kind="her_words", visibility=vis)
        if at:
            self.db.conn.execute("UPDATE facts SET recorded_at = ? WHERE id = ?", (at, fid))
            self.db.conn.execute("UPDATE statements SET recorded_at = ? WHERE id = (SELECT statement_id FROM fact_map WHERE fact_id = ?)", (at, fid))
        self.db.conn.commit()
        return fid


class RetireTest(Base):
    def test_static_list_matches_code(self):
        self.assertEqual(retire.unlisted(), [])
        for f in retire.scan():
            self.assertIn(f, retire.LEGACY)

    def test_new_direct_read_is_caught(self):
        fake = Path(self.tmp.name) / "repo"
        (fake / "ethan").mkdir(parents=True)
        (fake / "ethan" / "newthing.py").write_text('db.conn.execute("SELECT * FROM facts WHERE id = ?")\n', encoding="utf-8")
        self.assertTrue(any("newthing.py" in x for x in retire.unlisted(fake)))
        (fake / "ethan" / "newthing.py").write_text('db.conn.execute("SELECT * FROM facts_backup")\n', encoding="utf-8")
        self.assertEqual(retire.unlisted(fake), [])  # 别的表不算

    def test_runtime_counter_by_module(self):
        lifebase.current_facts(self.db)
        mepage.metrics(self.db, DAY)
        got = retire.runtime(self.data)
        self.assertIn("ethan/lifebase.py", got["modules"])
        self.assertGreater(got["modules"]["ethan/lifebase.py"]["read"], 0)
        self.assertTrue((self.data / retire.TRACE_FILE).exists())

    def test_source_health_new_equals_old_and_fallback(self):
        self.fact("咖啡要燕麦奶", at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        new = [s["facts_this_month"] for s in lifebase.source_health(self.db)]
        self.assertEqual(sum(new), 1)
        retire.set_fallback(self.db, True)
        self.assertTrue(retire.fallback(self.db))
        self.assertEqual([s["facts_this_month"] for s in lifebase.source_health(self.db)], new)
        retire.set_fallback(self.db, False)
        self.assertFalse(retire.fallback(self.db))

    def test_check_and_drill_do_not_touch_data(self):
        self.fact("喜欢靠窗")
        onto_query.switch(self.db, "planner", True)
        before = self.db.conn.execute("SELECT on_at FROM onto_switch WHERE user = 'planner'").fetchone()[0]
        rep = retire.check(self.db, self.data)
        self.assertEqual(rep["reconcile_diffs"], 0)
        self.assertEqual(rep["unlisted"], [])
        self.assertTrue(rep["drill"]["ok"], rep["drill"])
        self.assertEqual(rep["drill"]["users"][0]["user"], "planner")
        self.assertFalse(rep["ready_to_freeze"])  # 还有模块在读旧表
        self.assertIn("问她", rep["plan"])
        self.assertTrue(onto_query.reading_new(self.db, "planner"))  # 演练在副本上：真库开关没动
        self.assertEqual(self.db.conn.execute("SELECT on_at FROM onto_switch WHERE user = 'planner'").fetchone()[0], before)
        self.assertFalse(retire.fallback(self.db))
        self.assertIsNotNone(self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'facts'").fetchone())  # 不删表


class UpgradesTest(Base):
    def log_q(self, at, status="answer", fn="preferences", user="ethan"):
        onto_query._out(self.db, fn, user, status)
        self.db.conn.execute("UPDATE function_log SET at = ? WHERE id = (SELECT MAX(id) FROM function_log)", (at,))
        self.db.conn.commit()

    def fallback_search(self, at, q, keys="[]"):
        self.db.conn.execute("INSERT INTO retrieval_log (at, scene, round, queries, keys, user, via) VALUES (?, 'chat', 1, ?, ?, 'ethan', NULL)",
                             (at, json.dumps([q], ensure_ascii=False), keys))
        self.db.conn.commit()

    def test_log_before_after_and_metrics(self):
        self.fact("喜欢靠窗", at=iso(DAY - timedelta(days=3)))
        self.log_q(iso(DAY - timedelta(days=2)))
        self.log_q(iso(DAY - timedelta(days=2)), "unknown")
        self.fallback_search(iso(DAY - timedelta(days=1)), "她去过哪")
        uid = mepage.log_upgrade(self.db, DAY, "接微信", "source")
        self.assertEqual(mepage.log_upgrade(self.db, DAY, "接微信", "source"), uid)  # 同一件只记一次
        with self.assertRaises(ValueError):
            mepage.log_upgrade(self.db, DAY, "乱写", "whatever")
        up = upgrades.recent(self.db)[0]
        self.assertEqual((up["before"]["statements"], up["before"]["missed"], up["before"]["share"]), (1, 1, 0.67))
        self.assertIsNone(up["after"])
        self.assertEqual(upgrades.fill(self.db, DAY + timedelta(days=13)), 0)  # 没满两周不补
        self.fact("不吃香菜", at=iso(DAY + timedelta(days=3)))
        self.assertEqual(upgrades.fill(self.db, DAY + timedelta(days=14)), 1)
        up = upgrades.recent(self.db)[0]
        self.assertEqual((up["after"]["statements"], up["after"]["missed"]), (2, 0))
        m = mepage.metrics(self.db, DAY + timedelta(days=14))
        self.assertEqual(m["recent_upgrades"][0]["what"], "接微信")
        self.assertEqual([n["name"] for n in m["recent_upgrades"][0]["numbers"]], ["说法条数", "按题占比", "答不上次数", "纠错数"])

    def test_recent_at_most_three(self):
        for i in range(5):
            mepage.log_upgrade(self.db, DAY - timedelta(days=i), f"升级 {i}", "rule")
        self.assertEqual([u["what"] for u in mepage.metrics(self.db, DAY)["recent_upgrades"]], ["升级 0", "升级 1", "升级 2"])

    def test_seed_migration_only_bookkeeping(self):
        spec = importlib.util.spec_from_file_location("m", ROOT / "migrations" / "20261104_upgrades_seed.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        facts = self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        self.assertIn("5", m.run(self.store))
        m.run(self.store)  # 重复跑不重复记
        rows = self.db.conn.execute("SELECT at, kind, after FROM upgrades").fetchall()
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["at"] for r in rows}, {"2026-11-04"})
        self.assertEqual({r["kind"] for r in rows}, {"source", "property", "rule", "retrieval"})
        self.assertTrue(all(r["after"] is None for r in rows))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], facts)

    def test_frontend_shows_recent_upgrades(self):
        app = (ROOT / "ethan/workbench/static/app.js").read_text(encoding="utf-8")
        board = app[app.index("function meBoard("):app.index("function mePage()")]
        self.assertIn("recent_upgrades", board)
        self.assertIn("最近升级", board)
        self.assertIn(".slice(0, 3)", board)

    # ---- 4.2 候选题 ----
    def test_same_kind_missing_three_times_in_two_weeks(self):
        self.fallback_search(iso(DAY - timedelta(days=20)), "她养过什么宠物")  # 超过 2 周：不算
        self.fallback_search(iso(DAY - timedelta(days=9)), "她养过什么宠物")
        self.fallback_search(iso(DAY - timedelta(days=5)), "她养过宠物吗")
        self.assertEqual(upgrades.collect(self.db, DAY)["added"], 0)  # 2 周内只有 2 次
        self.fallback_search(iso(DAY - timedelta(days=1)), "宠物 养过")
        self.fallback_search(iso(DAY - timedelta(days=1)), "她的车是什么牌子")  # 别的类
        self.fallback_search(iso(DAY - timedelta(days=1)), "她养过什么宠物", keys='["fact:1"]')  # 查到了：不算
        self.assertEqual(upgrades.collect(self.db, DAY), {"added": 1, "updated": 0})
        c = upgrades.candidates(self.db)
        self.assertEqual((len(c), c[0]["n"], c[0]["kind"]), (1, 3, "兜底检索查不到"))
        self.assertEqual(upgrades.collect(self.db, DAY), {"added": 0, "updated": 0})  # 没有新的不重复记
        self.fallback_search(iso(DAY), "她养过什么宠物")
        self.assertEqual(upgrades.collect(self.db, DAY + timedelta(days=1))["updated"], 1)

    def test_question_missing_and_system_ignored(self):
        for i in range(3):
            self.log_q(iso(DAY - timedelta(days=i)), "unknown", fn="weekly_spend")
            self.log_q(iso(DAY - timedelta(days=i)), "unknown", fn="person", user="system")  # 系统维护自己查的不算
        upgrades.collect(self.db, DAY)
        c = upgrades.candidates(self.db)
        self.assertEqual(len(c), 1)
        self.assertIn("第 26 题", c[0]["samples"][0])

    def test_monthly_review_lists_and_she_decides(self):
        for i in range(3):
            self.fallback_search(iso(DAY - timedelta(days=i)), "她养过什么宠物")
        settings = Settings(Path(self.tmp.name) / "settings.json")
        week = weekreview.opener_prompt("人设", self.store, settings, date(2026, 11, 8), ["week"])
        self.assertNotIn("候选题", week)  # 平时只收不列
        self.assertEqual(len(upgrades.candidates(self.db)), 1)  # 每周开场顺手收了
        p = weekreview.opener_prompt("人设", self.store, settings, date(2026, 11, 29), ["week", "month"])
        self.assertIn("候选题", p)
        self.assertIn("宠物", p)
        cid = upgrades.candidates(self.db)[0]["id"]
        weekreview.open_review(self.db, date(2026, 11, 29), ["week", "month"])
        weekreview.note(self.db, f"[复盘记录 这个月不错 | 要点 | 候选题 #{cid} 收]", date(2026, 11, 29))
        self.assertEqual(upgrades.candidates(self.db), [])
        self.assertEqual(upgrades.candidates(self.db, "accepted")[0]["id"], cid)


class FormalEvalTest(Base):
    def test_report_and_no_leak_and_does_not_touch_logs(self):
        self.fact("咖啡要加燕麦奶")
        self.fact("下雨天不出门跑步")
        self.fact("在伦敦读商学院", dim="abilities", kind="fact")
        self.db.conn.commit()
        logs = self.db.conn.execute("SELECT COUNT(*) FROM function_log").fetchone()[0] if \
            self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'function_log'").fetchone() else 0
        qfile = self.data / "evals" / "formal.json"
        qfile.parent.mkdir(parents=True)
        qfile.write_text(json.dumps({"items": [
            {"q": 4, "user": "ethan", "args": {"topic": "咖啡"}, "ask": "她喝咖啡", "category": "抽信息", "expect": {"contains": ["燕麦奶"]}},
            {"q": 4, "user": "planner", "args": {"topic": "宠物"}, "ask": "她养宠物吗", "category": "该拒答", "expect": {"status": "unknown", "contains": ["猫"]}},
            {"q": 4, "user": "site", "args": {"topic": "咖啡"}, "ask": "她喝咖啡", "category": "权限", "expect": {"status": "unknown", "contains": ["燕麦奶"]}},
        ]}, ensure_ascii=False), encoding="utf-8")
        out = Path(self.tmp.name) / "report.md"
        self.assertEqual(formaleval.main(self.data, out=out), str(out))
        text = out.read_text(encoding="utf-8")
        self.assertIn("| 权限遵守 | 过 |", text)
        self.assertIn("| 能用 | 过 |", text)
        self.assertIn("| 拒答 | 过 |", text)
        self.assertIn("| 抽信息 | 1 |", text)
        self.assertNotIn("燕麦奶", text)  # 报告只写数字和题号，不写她的记忆
        for q in onto_query.QUESTIONS:
            self.assertIn(f"| {q} | {onto_query.QUESTIONS[q]} |", text)
        now = self.db.conn.execute("SELECT COUNT(*) FROM function_log").fetchone()[0] if \
            self.db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'function_log'").fetchone() else 0
        self.assertEqual(now, logs)  # 在副本上跑：真库的使用记录没多

    def test_leak_detector(self):
        private = self.fact("咖啡要加燕麦奶")
        sid = self.db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (private,)).fetchone()[0]
        self.assertTrue(formaleval._leaked(self.db, {"items": [{"value": "x"}], "basis": [sid]}))
        self.assertTrue(formaleval._leaked(self.db, {"items": [{"value": "x"}], "basis": []}))
        self.assertFalse(formaleval._leaked(self.db, {"items": [{"reply": onto_query.REFUSE, "refuse": True}], "basis": []}))
        self.assertFalse(formaleval._leaked(self.db, {"items": [], "basis": []}))

    def test_research_doc_has_command(self):
        doc = (ROOT / "docs/research/2026-11-ontology-eval.md").read_text(encoding="utf-8")
        self.assertIn("python3 -m ethan.memeval --formal --data /home/ethan/data", doc)
        self.assertIn("## 结论", doc)


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
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
