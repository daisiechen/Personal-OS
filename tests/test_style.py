"""写作规范和 skill 第一版（docs/design/style-and-skills.md，她 11-05：“提前做”）：
原料只取她写的、出处校验、确认前不注入 / 确认后注入、增量不覆盖她确认的、skill 草稿只进候选区、批准后生效、纠正攒够提议。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import desk, diary, evals, lifebase, memusers, mepage, onto_query, skills, style
from ethan.memory import MemoryStore

SYSTEM_DRAFT = "今天真的是非常充实的一天呢，哈哈。\n下午去图书馆看了很久的书，收获满满。\n晚上吃了火锅。"
HER_EDIT = "今天挺满的。\n下午去图书馆看了很久的书，收获满满。\n晚上和小林吃火锅，挺开心。"


class Model:
    """假模型：按 purpose 回固定的东西，记下收到的提示词。"""

    def __init__(self, rules=None, text="写好的一段"):
        self.rules, self.text, self.calls = rules or [], text, []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append({"system": system, "user": messages[0]["content"], "purpose": purpose})
        out = json.dumps({"rules": self.rules}, ensure_ascii=False) if purpose == "style-guide" else self.text
        return SimpleNamespace(text=out, model="m", backend="b", cost_usd=0)


GOOD = {"category": "avoid", "audience": "all", "rule": "不用“哈哈”和句末的“呢”", "words": ["哈哈", "呢"],
        "quotes": ["今天挺满的。", "今天真的是非常充实的一天呢，哈哈。"], "replaces": None}
SHORT = {"category": "length", "audience": "all", "rule": "句子短，一句只说一件事", "words": [],
         "quotes": ["今天挺满的。", "晚上和小林吃火锅，挺开心。"], "replaces": None}


class StyleBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        diary.ensure(self.db)
        diary.save(self.db, "2026-11-01", SYSTEM_DRAFT, source="ethan")
        diary.save(self.db, "2026-11-01", HER_EDIT)  # 她在系统草稿上改了 → her_edited
        diary.save(self.db, "2026-11-02", "系统自己写的一整篇，她没改。", source="ethan")
        diary.save(self.db, "2026-11-03", "早上跑了 5 公里。腿有点酸。")  # 她从空白写的
        lifebase.add_evidence(self.db, "sent", "sent:m1", "2026-11-01", "主题：周五见\n好的，周五下午三点见，我带 deck。")
        lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:0", "2026-11-01T10:00", "mia：今天想早点睡，困死了")
        lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:1", "2026-11-01T10:01", "ethan：好呀，早点休息，晚安")
        lifebase.backfill_handwritten(self.db)  # 每晚把她写的日记登记成证据（草稿说法挂在这些证据上）
        lifebase.add_evidence(self.db, "email", "mail:x", "2026-11-01", "别人发给她的邮件：您好，附件是合同。")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class MaterialsTest(StyleBase):
    def test_only_her_writing_goes_in(self):
        diary.mark(self.db, "2026-11-02", "系统自己写的一整篇，她没改。")
        nid = diary.annotate(self.db, "2026-11-02", "系统自己写的一整篇，她没改。", "写得不好，不像我")
        diary.annotate(self.db, "2026-11-02", "系统自己写的一整篇，她没改。", "时间记错了，是周二")
        items = style.materials(self.db)
        text = " ".join(i["text"] + (i.get("before") or "") for i in items)
        kinds = {i["kind"] for i in items}
        self.assertIn("我带 deck", text)  # 她发出的邮件
        self.assertIn("早上跑了 5 公里", text)  # 她自己写的日记
        self.assertIn("今天想早点睡", text)  # 聊天里她的话
        self.assertNotIn("早点休息，晚安", text)  # 小伴 说的不算
        self.assertNotIn("附件是合同", text)  # 别人的邮件不算
        pairs = [i for i in items if i["kind"] == "pair"]
        self.assertEqual(len(pairs), 2)  # 只有她改过的两句，没改的那句（图书馆）不算她的
        self.assertTrue(all("图书馆" not in i["text"] for i in items if i["kind"] in ("pair", "added", "diary")))
        self.assertIn("liked", kinds)  # 荧光笔
        disliked = [i for i in items if i["kind"] == "disliked"]
        self.assertEqual([i["ref"] for i in disliked], [f"disliked:{nid}"])  # 只收“写得不好”的，记错事实的批注不算
        self.assertFalse(any(i["kind"] == "diary" and "她没改" in i["text"] for i in items))  # 系统写的整篇不进

    def test_only_style_and_system_may_read_materials(self):
        self.assertEqual(onto_query.style_materials(self.db, "ethan")["status"], "unknown")
        self.assertEqual(onto_query.style_materials(self.db, "style")["status"], "answer")


class QuoteCheckTest(StyleBase):
    def test_quotes_must_be_verbatim_and_hers(self):
        items = style.materials(self.db)
        self.assertEqual(style.check_rule(GOOD, items), [])
        made_up = {**GOOD, "quotes": ["今天挺满的。", "我从来不说哈哈"]}
        self.assertTrue(any("不在原料里" in e for e in style.check_rule(made_up, items)))
        one = {**GOOD, "quotes": ["今天挺满的。"]}
        self.assertTrue(style.check_rule(one, items))
        diary.mark(self.db, "2026-11-02", "系统自己写的一整篇，她没改。")
        items = style.materials(self.db)
        only_liked = {**GOOD, "quotes": ["系统自己写的一整篇", "她没改"]}
        self.assertTrue(any("她写的或她改的" in e for e in style.check_rule(only_liked, items)))

    def test_model_output_filtered(self):
        bad = {**SHORT, "rule": "编出来的", "quotes": ["根本没有这句", "也没有这句"]}
        got = style.induce(Model([GOOD, SHORT, bad]), self.db, style.materials(self.db), [])
        self.assertEqual([r["rule"] for r in got], [GOOD["rule"], SHORT["rule"]])


class ConfirmFlowTest(StyleBase):
    def _draft(self, rules=(GOOD, SHORT)):
        return style.draft(self.db, Model(list(rules)), force=True)

    def test_draft_not_injected_until_confirmed(self):
        got = self._draft()
        self.assertEqual(got["added"], 2)
        drafts = style.rules(self.db, "draft")
        self.assertTrue(drafts[0]["strength"] >= 1)  # 改前→改后当证据
        self.assertEqual(style.style_block(self.db, "diary"), "")  # 草稿不注入
        self.assertEqual(diary.with_style(self.db, diary.PROMPT), diary.PROMPT)
        # 草稿也是说法：系统的假设、候选
        sid = drafts[0]["statement_id"]
        row = self.db.conn.execute("SELECT kind, review, property FROM statements WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(tuple(row), ("hypothesis", "candidate", "writing_style"))
        # 待你定里一条
        p = [x for x in mepage.pending_items(self.db) if x["kind"] == "style"]
        self.assertEqual(len(p), 1)
        self.assertIn("你的写作规范草稿", p[0]["question"])
        self.assertEqual(len(p[0]["options"]), 2)
        # 逐条：对一条、改一条
        rid = {r["rule"]: r["id"] for r in drafts}
        self.assertTrue(mepage.resolve(self.db, p[0]["id"], "yes", pick=rid[GOOD["rule"]])["ok"])
        block = style.style_block(self.db, "diary")
        self.assertIn("不用“哈哈”", block)
        self.assertNotIn("句子短", block)  # 另一条还没定
        self.assertIn("她确认过的写作规范（", diary.with_style(self.db, diary.PROMPT))
        self.assertTrue(mepage.resolve(self.db, p[0]["id"], "own", pick=rid[SHORT["rule"]], text="句子要短")["ok"])
        self.assertIn("句子要短", style.style_block(self.db, "mail"))
        self.assertFalse([x for x in mepage.pending_items(self.db) if x["kind"] == "style"])  # 全定完就关掉
        # 确认的是她的偏好、锁住
        w = onto_query.writing_style(self.db, "mail")
        self.assertEqual(w["status"], "answer")
        self.assertTrue(all(i["said_by"] == "her_confirmation" for i in w["items"]))
        sids = [i["statement"] for i in w["items"]]
        self.assertTrue(all(self.db.conn.execute("SELECT locked FROM statements WHERE id = ?", (s,)).fetchone()[0] for s in sids))
        self.assertEqual(lifebase.reconcile_statements(self.db), [])
        # 不混进偏好那道题
        self.assertFalse(any("哈哈" in (i["value"] or "") for i in onto_query.preferences(self.db, "planner")["items"]))

    def test_reject_never_comes_back(self):
        self._draft([GOOD])
        p = [x for x in mepage.pending_items(self.db) if x["kind"] == "style"][0]
        mepage.resolve(self.db, p["id"], "no", pick=style.rules(self.db, "draft")[0]["id"])
        self.assertEqual(style.style_block(self.db, "mail"), "")
        diary.save(self.db, "2026-11-05", "又写了一篇新的。挺好。")
        got = style.draft(self.db, Model([{**GOOD, "quotes": ["又写了一篇新的。", "挺好。"]}]), force=True)
        self.assertEqual(got.get("added", 0), 0)
        self.assertEqual(got["skipped"], 1)

    def test_incremental_does_not_touch_confirmed(self):
        self._draft([GOOD])
        rid = style.rules(self.db, "draft")[0]["id"]
        style.confirm(self.db, rid, "不用“哈哈”")
        before = style.rules(self.db, "confirmed")
        # 没有新原料：跳过
        self.assertIn("skipped", style.draft(self.db, Model([SHORT])))
        # 新原料来了：只看新的，同一条（像她确认过的）不重复、不改她那条
        diary.save(self.db, "2026-11-05", "今天下雨。没出门。")
        got = style.draft(self.db, Model([{**GOOD, "rule": "不用哈哈", "quotes": ["今天下雨。", "没出门。"]}]), force=True)
        self.assertEqual(got["skipped"], 1)
        self.assertEqual(style.rules(self.db, "confirmed"), before)
        model = Model([])
        style.draft(self.db, model, force=True)
        self.assertFalse(model.calls)  # 用过的原料不再给模型

    def test_skip_when_not_enough(self):
        self.store.close()
        self.store = MemoryStore(self.root / "empty")
        self.db = self.store.db
        self.assertIn("skipped", style.draft(self.db, Model([GOOD])))


class ReadersTest(StyleBase):
    def _confirm(self):
        style.draft(self.db, Model([GOOD]), force=True)
        style.confirm(self.db, style.rules(self.db, "draft")[0]["id"])

    def test_diary_only_reads_writing_style(self):
        self._confirm()
        self.assertEqual(onto_query.writing_style(self.db, "diary")["status"], "answer")
        self.assertEqual(onto_query.preferences(self.db, "diary")["status"], "unknown")
        self.assertEqual(onto_query.last_said(self.db, "diary", "跑步")["status"], "unknown")
        from ethan import memsearch, portrait
        with self.assertRaises(ValueError):
            memsearch.search(self.db, ["跑步"], user="diary")
        with self.assertRaises(ValueError):
            portrait.chat_text(self.db, "diary")
        self.assertEqual(memusers.USERS["diary"]["memory_only"], ["writing_style"])

    def test_dais_gets_rules_without_quotes(self):
        self._confirm()
        w = onto_query.writing_style(self.db, "twin")
        self.assertEqual(w["status"], "answer")
        self.assertNotIn("quotes", w["items"][0])
        block = style.style_block(self.db, "twin", quotes=False)
        self.assertNotIn("今天挺满的", block)
        from ethan import publicsite
        publicsite.ensure_triage(self.db)
        self.assertIn("哈哈", publicsite._style(self.db))

    def test_injected_into_diary_mail_and_card(self):
        self._confirm()
        m = Model(text="今天挺满的。")
        from datetime import date, datetime, timezone
        self.store.raw.append("telegram", "mia", "今天去跑步了", at=datetime(2026, 11, 4, 9, tzinfo=timezone.utc))
        diary.write(self.store, m, date(2026, 11, 4), force=True)
        self.assertIn("不用“哈哈”", m.calls[-1]["system"])
        self.assertIn("不用“哈哈”", desk._mail_how(self.db))
        from ethan import relations
        m2 = Model(text=json.dumps({"relation": "", "circle": "", "how_met": "", "topics": [], "recent": "", "dates": [], "open_loops": [], "draft": ""}))
        relations.card_from_text(m2, self.db, "小林", {}, "【她】你好")
        self.assertIn("不用“哈哈”", m2.calls[0]["system"])


class SkillsTest(StyleBase):
    def test_draft_goes_to_candidates_and_approval_activates(self):
        self.assertEqual(skills.prompt_for(self.db, "mail_reply"), "")
        p = skills.draft_skill(self.db, "mail_reply")
        self.assertEqual(p, self.root / "skills" / "candidates" / "mail_reply.md")
        text = p.read_text(encoding="utf-8")
        for part in ("## 1. 通用方法", "## 2. 她的做法", "## 3. 能做到哪一步", "只能起草"):
            self.assertIn(part, text)
        self.assertFalse((self.root / "skills" / "active" / "mail_reply.md").exists())
        self.assertEqual(skills.prompt_for(self.db, "mail_reply"), "")  # 没批准不生效
        self.assertEqual(desk._mail_how(self.db), "")
        item = [x for x in mepage.pending_items(self.db) if x["kind"] == "skill"][0]
        self.assertIn("新 skill 草稿：替你回邮件", item["question"])
        self.assertTrue(mepage.resolve(self.db, item["id"], "own", text="给教授的信用英文")["ok"])
        self.assertTrue((self.root / "skills" / "active" / "mail_reply.md").exists())
        self.assertFalse(p.exists())
        how = desk._mail_how(self.db)
        self.assertIn("替你回邮件", how)
        self.assertIn("给教授的信用英文", how)
        self.assertNotIn("mail_reply.md", str(list(Path(__file__).resolve().parent.parent.glob("skills/**/*.md"))))  # 不进仓库

    def test_reject_draft(self):
        skills.draft_skill(self.db, "mail_reply")
        item = [x for x in mepage.pending_items(self.db) if x["kind"] == "skill"][0]
        mepage.resolve(self.db, item["id"], "no")
        self.assertFalse((self.root / "skills" / "active" / "mail_reply.md").exists())
        self.assertEqual(skills.state(self.db, "mail_reply")["status"], "rejected")

    def test_enough_corrections_only_propose(self):
        from ethan import runlog
        runlog.ensure(self.db)
        for n in range(2):
            self.db.conn.execute("INSERT INTO run_log (key, at, actor, kind, title, reaction, reaction_at) VALUES (?, ?, 'ethan', 'mail', 't', 'edited', ?)",
                                 (f"mail:{n}", "2099-01-01", "2099-01-01"))
        self.assertEqual(skills.propose(self.db), [])  # 两次不够
        self.db.conn.execute("INSERT INTO run_log (key, at, actor, kind, title, reaction, reaction_at) VALUES ('prep:9', '2099-01-01', 'ethan', 'mail', 't', 'revised', '2099-01-01')")
        self.assertEqual(skills.propose(self.db), ["mail_reply"])
        self.assertEqual(skills.propose(self.db), [])  # 提议过不重复
        self.assertFalse((self.root / "skills").exists())  # 只是提议，没写草稿
        item = [x for x in mepage.pending_items(self.db) if x["kind"] == "skill"][0]
        self.assertIn("要不要开一个 skill", item["question"])
        mepage.resolve(self.db, item["id"], "yes")
        self.assertTrue((self.root / "skills" / "candidates" / "mail_reply.md").exists())  # 她说要才起草，仍是候选
        self.assertFalse((self.root / "skills" / "active" / "mail_reply.md").exists())


class EvalTest(StyleBase):
    def test_metrics(self):
        self.assertEqual(style.banned_hits("哈哈哈哈", ["哈哈"]), 2)
        self.assertGreater(style.similarity("今天挺满的", "今天挺满的。"), style.similarity("非常充实", "今天挺满的。"))
        self.assertEqual(style.used_hits("sync 一下", ["sync", "deck"]), 0.5)

    def test_offline_eval_writes_numbers(self):
        style.draft(self.db, Model([GOOD]), force=True)
        style.confirm(self.db, style.rules(self.db, "draft")[0]["id"])
        lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:5", "2026-11-01T12:00", "mia：去了图书馆")
        self.store.raw.append("telegram", "mia", "去了图书馆")
        res = style.offline_eval(self.store, Model(text="今天挺满的。晚上和小林吃火锅，挺开心。"), n=3)
        self.assertIn("similarity_after", res)
        self.assertTrue(list((self.root / "evals").glob("style-offline-*.json")))

    def test_style_eval_set_registered_and_scored_by_code(self):
        self.assertIn("style", evals.TASKS)
        cases = evals.load("style")
        self.assertGreaterEqual(len(cases), 3)
        ok, _ = evals._score_style({"rules": [GOOD], "text": "今天挺满的。挺开心。", "afters": ["今天挺满的。"],
                                    "befores": ["今天真的是非常非常充实的一天呢，哈哈"]}, cases[0]["expect"])
        self.assertTrue(ok)
        bad, note = evals._score_style({"rules": [GOOD], "text": "今天真的是非常非常充实的一天呢，哈哈！", "afters": ["今天挺满的。"],
                                        "befores": ["今天真的是非常非常充实的一天呢，哈哈"]}, cases[0]["expect"])
        self.assertFalse(bad)
        self.assertIn("禁用词", note)


if __name__ == "__main__":
    unittest.main()
