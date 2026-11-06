"""其他会话里的访谈进记忆（ontology 总方案 1.1）：算她的原话、注明会话和场合、每晚提取、删了不再收。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import interviews, lifebase, onto_query
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

os.environ.setdefault("ETHAN_NO_KICK", "1")  # 测试里不真起后台整理进程

TEXT = "问：你大学在哪读的？\n答：我本科在上海读的，学的是金融，2016 年毕业。\n问：为什么后来转行？\n答：我不喜欢天天对着报表，我更想做自己的产品。"


class Router:
    def __init__(self, *answers):
        self.answers = list(answers)

    def complete(self, system, messages, purpose="chat", images=()):
        a = self.answers.pop(0)
        return SimpleNamespace(text=json.dumps(a, ensure_ascii=False))


class InterviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def send(self, **kw):
        payload = {"session": "2- 个人主页", "scene": "聊过往的访谈", "text": TEXT, **kw}
        return interviews.receive(self.store, payload, now=datetime(2026, 11, 4, tzinfo=timezone.utc))

    def test_receive_as_her_words_private_and_idempotent(self):
        code, out = self.send()
        self.assertEqual(code, 200)
        ev = self.db.conn.execute("SELECT * FROM evidence WHERE id = ?", (out["evidence"],)).fetchone()
        self.assertEqual((ev["source"], ev["visibility"]), ("interview", "private"))
        self.assertIn("会话：2- 个人主页", ev["text"])
        self.assertEqual(lifebase.source_kind_for(self.db, [ev["id"]]), "her_words")  # 算原话
        self.assertEqual(self.send()[1]["evidence"], out["evidence"])  # 同一份交两次不重复
        self.assertEqual(interviews.receive(self.store, {"text": "x"})[0], 400)

    def test_digest_extracts_with_context_and_is_queryable(self):
        self.send()
        router = Router({"facts": [
            {"statement": "本科在上海读金融，2016 年毕业", "dimension": "abilities", "quote": "我本科在上海读的，学的是金融，2016 年毕业"},
            {"statement": "不喜欢天天对着报表，更想做自己的产品", "dimension": "preferences", "quote": "我不喜欢天天对着报表"}]},
            {"ops": [{"op": "ADD", "from": "n1", "dimension": "abilities", "statement": "本科在上海读金融，2016 年毕业"},
                     {"op": "ADD", "from": "n2", "dimension": "preferences", "statement": "不喜欢天天对着报表，更想做自己的产品"}]})
        got = interviews.digest(self.store, router)
        self.assertEqual((got["interviews"], got["facts"], got["failed"]), (1, 2, 0))
        f = self.db.conn.execute("SELECT context, source_kind FROM facts WHERE statement LIKE '%报表%'").fetchone()
        self.assertEqual(f["source_kind"], "her_words")
        self.assertIn("2- 个人主页", f["context"])
        from datetime import date
        pref = onto_query.preferences(self.db, "ethan", "报表", today=date(2026, 11, 5))
        self.assertEqual(pref["items"][0]["said_by"], "her_words")  # 新结构里查得到、出处是她的原话
        self.assertEqual(interviews.digest(self.store, Router())["interviews"], 0)  # 整理过的不再整理

    def test_handwritten_diary_and_wish_become_her_words(self):
        from ethan import cards, diary
        diary.save(self.db, "2026-11-02", "今天去游泳了，我还是更喜欢早上运动。", source="her")
        diary.save(self.db, "2026-11-03", "系统写的初稿", source="auto")  # 系统写的不算她的话
        self.db.conn.execute(cards.WISH_SCHEMA)
        self.db.conn.execute("INSERT INTO card_wishes (text, created_at) VALUES ('想画一张在海边跑步的', '2026-11-03T08:00:00+00:00')")
        lifebase.backfill_handwritten(self.db)
        refs = {r[0] for r in self.db.conn.execute("SELECT source_ref FROM evidence WHERE source = 'workbench'")}
        self.assertEqual(len([r for r in refs if r.startswith("diary:")]), 1)
        self.assertIn("wish:1", refs)
        lifebase.backfill_handwritten(self.db)  # 重跑不重复
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM evidence WHERE source = 'workbench'").fetchone()[0], 2)
        router = Router({"facts": [{"statement": "更喜欢早上运动", "dimension": "rhythms", "quote": "我还是更喜欢早上运动"}]},
                        {"ops": [{"op": "ADD", "from": "n1", "dimension": "rhythms", "statement": "更喜欢早上运动"}]},
                        {"facts": []})
        got = interviews.digest(self.store, router)
        self.assertEqual((got["interviews"], got["facts"], got["failed"]), (2, 1, 0))
        f = self.db.conn.execute("SELECT context, source_kind FROM facts WHERE statement = '更喜欢早上运动'").fetchone()
        self.assertEqual(f["source_kind"], "her_words")
        self.assertIn("2026-11-02 的日记", f["context"])

    def test_quote_must_be_her_words(self):
        self.send()
        bad = {"facts": [{"statement": "在北京读书", "dimension": "abilities", "quote": "我在北京读书"}]}
        got = interviews.digest(self.store, Router(bad, bad))
        self.assertEqual(got["failed"], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)

    def test_failed_digest_retried_then_given_up(self):
        """Codex 评审 11-05：没整理成的不能记成整理过；下次再试，试满 3 次才放弃，巡检看得到。"""
        from ethan import checkup
        self.send()
        bad = {"facts": [{"statement": "在北京读书", "dimension": "abilities", "quote": "我在北京读书"}]}
        for n in (1, 2, 3):
            self.assertEqual(len(interviews.pending(self.db)), 1, n)  # 没成的还在待整理里
            self.assertEqual(interviews.digest(self.store, Router(bad, bad))["failed"], 1)
            self.assertEqual(self.db.conn.execute("SELECT ok, tries FROM interview_digest").fetchone()[:], (0, n))
        self.assertEqual(interviews.pending(self.db), [])  # 试满了
        self.assertEqual(interviews.given_up(self.db), 1)
        got = checkup.problems(self.root, self.db, services=False, meminfo=self.root / "没有")
        self.assertTrue(any(p["what"] == "大批整理" and "放弃" in p["detail"] for p in got))

    def test_model_error_counts_as_a_try(self):
        class Down:
            def complete(self, *a, **kw):
                raise TimeoutError("额度满了")
        self.send()
        got = interviews.digest(self.store, Down())
        self.assertEqual((got["interviews"], got["failed"]), (1, 1))
        self.assertEqual(len(interviews.pending(self.db)), 1)
        ok = {"facts": []}
        self.assertEqual(interviews.digest(self.store, Router(ok))["failed"], 0)  # 下次好了就整理成
        self.assertEqual(self.db.conn.execute("SELECT ok, tries FROM interview_digest").fetchone()[:], (1, 2))
        self.assertEqual(interviews.pending(self.db), [])

    def test_retry_failed_digests_migration(self):
        import importlib.util
        self.send()
        _, out = interviews.receive(self.store, {"session": "a", "scene": "b", "text": "问：x\n答：我喜欢游泳"})
        root = Path(__file__).resolve().parent.parent
        # 老表（没有 ok / tries 两列），一份错标成整理过（failed），一份真整理过
        self.db.conn.execute("CREATE TABLE interview_digest (evidence_id INTEGER PRIMARY KEY, at TEXT NOT NULL, result TEXT)")
        first = self.db.conn.execute("SELECT MIN(id) FROM evidence WHERE source = 'interview'").fetchone()[0]
        self.db.conn.execute("INSERT INTO interview_digest VALUES (?, 'x', ?)", (first, json.dumps({"failed": "两次都没通过校验"})))
        self.db.conn.execute("INSERT INTO interview_digest VALUES (?, 'x', ?)", (out["evidence"], json.dumps({"ADD": 1, "UPDATE": 0})))
        self.db.conn.commit()
        # 老代码写的“ok 默认 1 但结果带 failed”现在就当没成（上线时还在跑的旧进程会这样写），迁移照样把 tries 清零
        self.assertEqual([e["id"] for e in interviews.pending(self.db)], [first])
        spec = importlib.util.spec_from_file_location("m", root / "migrations" / "20261105_retry_failed_digests.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.run(self.store), "1 份改回待重试")
        self.assertEqual([e["id"] for e in interviews.pending(self.db)], [first])
        self.assertEqual(mod.run(self.store), "0 份改回待重试")  # 再跑不重复

    def test_forgotten_interview_not_received_again(self):
        _, out = self.send()
        lifebase.forget(self.db, [out["evidence"]], root=self.root)
        self.assertIn("skipped", self.send()[1])

    def test_file_handed_over_in_a_session(self):
        payload = {"kind": "file", "session": "开发会话", "name": "简历.pdf", "text": "教育经历：2014-2018 某大学"}
        code, out = interviews.receive(self.store, payload)
        ev = self.db.conn.execute("SELECT source, path FROM evidence WHERE id = ?", (out["evidence"],)).fetchone()
        self.assertEqual((ev["source"], ev["path"]), ("file", "handoff/简历.pdf"))
        self.assertEqual(lifebase.source_kind_for(self.db, [out["evidence"]]), "her_provided")  # 她提供的资料，不算原话
        self.assertIn(out["evidence"], [e["id"] for e in interviews.files_pending(self.db, 10)])  # 和别的文件一样整理
        self.assertEqual(interviews.receive(self.store, payload)[1]["evidence"], out["evidence"])

    def test_fix_resume_handoff_migration(self):
        import importlib.util
        root = Path(__file__).resolve().parent.parent
        spec = importlib.util.spec_from_file_location("m", root / "migrations" / "20261103_fix_resume_handoff.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIn("没动", mod.run(self.store))  # 对不上就不动
        self.db.conn.execute("INSERT INTO evidence (id, source, source_ref, occurred_at, recorded_at, text, participants, involves_others, visibility) "
                             "VALUES (2514, 'interview', 'interview:x', 'x', 'x', '【访谈】会话：开发会话（个人管理工作台）；场合：没写；时间：x\n教育背景……', '[]', 0, 'private')")
        self.assertIn("改成", mod.run(self.store))
        r = self.db.conn.execute("SELECT source, text FROM evidence WHERE id = 2514").fetchone()
        self.assertEqual((r["source"], r["text"]), ("file", "教育背景……"))
        self.assertEqual(interviews.pending(self.db), [])

    def test_endpoint_needs_cloud_token(self):
        inbox = Inbox(self.store, {"cloud": "secret"})
        body = json.dumps({"session": "2- 个人主页", "scene": "访谈", "text": TEXT}).encode()
        self.assertEqual(inbox.handle("/cloud/interview", {}, body)[0], 401)
        code, out = inbox.handle("/cloud/interview", {"Authorization": "Bearer secret"}, body)
        self.assertEqual((code, out["ok"]), (200, True))


TX = ("〔2026-11-02〕\n【小林】你最近是不是又熬夜了，脸色好差\n【她】是啊，这周每天两点睡，在赶 进修 的论文\n"
      "【小林】那周六我们去 Hampstead 爬山吧，说好了\n【小林】我下个月搬去曼彻斯特\n〔2026-11-03〕\n【她】好呀\n【Grok】建议你每天 1500 千卡、每周三次力量训练")


class TranscriptTest(unittest.TestCase):
    """整段对话：三种来源分对、原话按说话人校验、后台一次整理完。"""
    setUp, tearDown, send = InterviewTest.setUp, InterviewTest.tearDown, InterviewTest.send

    def send_tx(self, **kw):
        payload = {"session": "微信 · 小林", "scene": "她的微信聊天（导出）", "text": TX, "format": "transcript", **kw}
        return interviews.receive(self.store, payload, now=datetime(2026, 11, 4, tzinfo=timezone.utc))

    def test_receive_marks_format_and_kicks(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {"ETHAN_NO_KICK": ""}), mock.patch.object(interviews, "kick_digest_all") as kick:
            code, out = self.send_tx(agent="Grok")
            self.assertEqual(code, 200)
            kick.assert_called_once_with(self.store.root)
            self.send()  # 旧的问/答格式不起后台
            self.assertEqual(kick.call_count, 1)
        first = self.db.conn.execute("SELECT text FROM evidence WHERE id = ?", (out["evidence"],)).fetchone()[0].split("\n", 1)[0]
        self.assertIn("；格式：整段对话；agent：Grok", first)
        self.assertTrue(first.startswith("【访谈】会话：微信 · 小林；"))  # 人物卡按这个前缀找
        # 没写 format 也认得出（旧的交接命令不用改）
        code, out2 = interviews.receive(self.store, {"session": "微信 · 小林", "scene": "x", "text": "【她】我想学游泳\n【小林】好"})
        self.assertIn(interviews.TX_MARK, self.db.conn.execute("SELECT text FROM evidence WHERE id = ?", (out2["evidence"],)).fetchone()[0])
        self.assertEqual(interviews.receive(self.store, {"session": "a", "text": "b", "format": "xml"})[0], 400)

    def test_kick_spawns_background(self):
        calls = []
        self.assertTrue(interviews.kick_digest_all("/data", spawn=lambda cmd, **kw: calls.append(cmd)))
        self.assertEqual(calls[0][-3:], ["--data", "/data", "--digest-all"])

    def test_parse_and_pieces(self):
        turns = interviews.parse_transcript(TX)
        self.assertEqual([(t["who"], t["day"]) for t in turns][:2], [("小林", "2026-11-02"), ("她", "2026-11-02")])
        self.assertEqual(turns[-1], {"who": "Grok", "text": "建议你每天 1500 千卡、每周三次力量训练", "day": "2026-11-03"})
        pieces = interviews._pieces(TX * 30, size=500)
        self.assertGreater(len(pieces), 2)
        self.assertTrue(all(p.startswith("〔") for p in pieces))  # 每段开头都有日期
        self.assertEqual(interviews.source_of("她"), "her_words")
        self.assertEqual(interviews.source_of("Grok"), "agent_relay")
        self.assertEqual(interviews.source_of("小助手", "小助手"), "agent_relay")
        self.assertEqual(interviews.source_of("小林"), "other_person")

    def test_digest_three_sources(self):
        self.send_tx()
        facts = {"facts": [
            {"speaker": "她", "statement": "2026-11 这周每天两点睡，在赶 毕业论文", "dimension": "rhythms", "quote": "这周每天两点睡"},
            {"speaker": "小林", "statement": "小林说她最近熬夜、脸色差", "dimension": "body", "quote": "你最近是不是又熬夜了"},
            {"speaker": "小林", "statement": "小林和她约了周六去 Hampstead 爬山", "dimension": "relationships", "quote": "周六我们去 Hampstead 爬山吧"},
            {"speaker": "Grok", "statement": "Grok 给她的减脂方案：每天 1500 千卡、每周三次力量", "dimension": "body", "quote": "每天 1500 千卡"}]}
        ops = {"ops": [{"op": "ADD", "from": "n1", "dimension": "rhythms", "statement": "2026-11 这周每天两点睡，在赶 毕业论文"}]}
        got = interviews.digest(self.store, Router(facts, ops))
        self.assertEqual((got["interviews"], got["facts"], got["others"], got["failed"]), (1, 1, 3, 0))
        rows = {r["statement"]: dict(r) for r in self.db.conn.execute("SELECT statement, kind, source_kind, context, visibility, valid_from FROM facts")}
        her = rows["2026-11 这周每天两点睡，在赶 毕业论文"]
        self.assertEqual(her["source_kind"], "her_words")
        self.assertIn("微信 · 小林", her["context"])
        lin = rows["小林说她最近熬夜、脸色差"]
        self.assertEqual((lin["kind"], lin["source_kind"], lin["visibility"]), ("fact", "other_person", "private"))
        self.assertEqual(lin["context"], "小林在微信里说的（2026-11-02）")
        grok = rows["Grok 给她的减脂方案：每天 1500 千卡、每周三次力量"]
        self.assertEqual((grok["kind"], grok["source_kind"], grok["visibility"]), ("hypothesis", "agent_relay", "private"))
        self.assertIn("只当资料", grok["context"])
        self.assertEqual(grok["valid_from"][:10], "2026-11-03")
        from datetime import date
        said = {i["value"] for i in onto_query.preferences(self.db, "ethan", "", today=date(2026, 11, 5))["items"]}
        self.assertNotIn(grok["statement"], said)  # 转述不当她的偏好
        self.assertEqual(interviews.digest(self.store, Router())["interviews"], 0)

    def test_quote_checked_by_speaker(self):
        self.send_tx()
        wrong_speaker = {"facts": [{"speaker": "她", "statement": "最近熬夜", "dimension": "body", "quote": "你最近是不是又熬夜了"}]}
        unnamed = {"facts": [{"speaker": "小林", "statement": "她最近熬夜", "dimension": "body", "quote": "你最近是不是又熬夜了"}]}
        errs = interviews.check_transcript(wrong_speaker, interviews.parse_transcript(TX))
        self.assertTrue(any("不在【她】说的话里" in e for e in errs))
        self.assertTrue(any("要写明是小林说的" in e for e in interviews.check_transcript(unnamed, interviews.parse_transcript(TX))))
        self.assertTrue(interviews.check_transcript({"facts": [{"speaker": "老王", "statement": "老王说", "dimension": "body", "quote": "x"}]},
                                                    interviews.parse_transcript(TX)))
        got = interviews.digest(self.store, Router(wrong_speaker, unnamed))
        self.assertEqual(got["failed"], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)

    def test_digest_all_and_nightly_lock(self):
        self.send_tx()
        self.send()
        router = Router({"facts": []}, {"facts": []})
        self.assertEqual(interviews.digest_all(self.store, router)["interviews"], 2)
        self.assertEqual(interviews.pending(self.db), [])
        self.send_tx(text=TX + "\n【她】我下周想去游泳")
        held = interviews.lock(self.root)
        try:
            self.assertEqual(interviews.nightly(self.store, Router()), {"skipped": "后台正在整理"})  # 后台在整理就不碰
        finally:
            held.close()
        self.assertEqual(interviews.nightly(self.store, Router({"facts": []}))["interviews"], 1)

    def test_failed_piece_retried_alone_and_not_twice_in_one_run(self):
        self.send_tx()
        wrong = {"facts": [{"speaker": "她", "statement": "最近熬夜", "dimension": "body", "quote": "你最近是不是又熬夜了"}]}
        got = interviews.digest_all(self.store, Router(wrong, wrong))  # 一轮里没成的不连着再试（Router 再要答案会 IndexError）
        self.assertEqual((got["interviews"], got["failed"]), (1, 1))
        r = json.loads(self.db.conn.execute("SELECT result FROM interview_digest").fetchone()[0])
        self.assertEqual(r["failed_pieces"], [0])
        self.assertEqual(len(interviews.pending(self.db)), 1)
        got = interviews.digest(self.store, Router({"facts": []}))
        self.assertEqual(got["failed"], 0)
        self.assertEqual(interviews.pending(self.db), [])

    def test_endpoint_passes_format(self):
        inbox = Inbox(self.store, {"cloud": "secret"})
        body = json.dumps({"session": "微信 · 小林", "scene": "x", "text": TX, "format": "transcript"}).encode()
        code, out = inbox.handle("/cloud/interview", {"Authorization": "Bearer secret"}, body)
        self.assertEqual(code, 200)
        self.assertIn(interviews.TX_MARK, self.db.conn.execute("SELECT text FROM evidence WHERE id = ?", (out["evidence"],)).fetchone()[0])

    def test_eval_cases_score(self):
        from ethan import evals
        cases = {c["id"]: c for c in evals.load("transcript_facts")}
        self.assertGreaterEqual(len(cases), 2)
        for c in cases.values():
            turns = interviews.parse_transcript(c["input"]["text"])
            self.assertTrue(turns)
            for w in c["expect"].get("sources", []):
                self.assertIn(w["speaker"], {t["who"] for t in turns})


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
