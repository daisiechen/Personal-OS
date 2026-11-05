import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import evals, extract, lifebase
from ethan.memory import MemoryStore

TODAY = date(2026, 10, 30)
EMPTY = {"todos": [], "wishes": [], "facts": [], "events": [], "remember": [], "people": []}


class FakeRouter:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append(messages[0]["content"])
        return SimpleNamespace(text=json.dumps(self.answers.pop(0), ensure_ascii=False))


def answer(**kw):
    return {**EMPTY, **kw}


class ExtractTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def say(self, text):
        self.store.raw.append("telegram", "ethan", "在呢")
        self.store.raw.append("telegram", "mia", text)
        self.store.raw.append("telegram", "ethan", "好")
        return extract.locate(self.store, text)

    def on(self, m="on"):
        (self.root / "extract-mode").write_text(m)

    def test_off_by_default(self):
        ref = self.say("明天要去银行激活卡")
        self.assertEqual(extract.mode(self.root), "off")
        self.assertFalse(extract.kick(self.root, ref, spawn=lambda *a, **k: self.fail("不该起进程")))
        self.assertIn("skipped", extract.run(self.store, FakeRouter(), ref))

    def test_locate_and_load(self):
        ref = self.say("明天要去银行激活卡")
        self.assertRegex(ref, r"^raw:\d{4}-\d{2}-\d{2}:1$")
        msg, before = extract.load(self.store, ref)
        self.assertEqual(msg["text"], "明天要去银行激活卡")
        self.assertEqual([r["text"] for r in before], ["在呢"])

    def test_check_rejects_made_up_quotes_ids_and_dates(self):
        data = answer(todos=[{"op": "update", "match_id": 99, "quote": "激活卡"},
                             {"op": "add", "title": "激活卡", "due": "明天", "quote": "去银行激活"},
                             {"op": "add", "title": "买菜", "quote": "去超市买菜"}])
        errs = extract.check(data, "明天要去银行激活卡", TODAY, task_ids={1})
        self.assertTrue(any("match_id" in e for e in errs))
        self.assertTrue(any("due" in e for e in errs))
        self.assertTrue(any("todos[2] 的 quote" in e for e in errs))

    def test_retry_then_apply_todos(self):
        self.on()
        old = self.db.add_task("给学校发邮件要学生证明")
        rent = self.db.add_task("交 Q4 房租")
        notes = self.db.add_task("整理课程笔记")
        text = "明天要去银行激活卡，学生证明邮件也要发，房租交了，笔记不弄了"
        ref = self.say(text)
        bad = answer(todos=[{"op": "add", "title": "激活卡", "quote": "编的话"}])
        good = answer(todos=[
            {"op": "add", "title": "去银行激活卡", "due": "2026-10-31", "owner": "mia", "quote": "明天要去银行激活卡"},
            {"op": "add", "title": "发邮件要学生证明", "due": "2026-11-02", "quote": "学生证明邮件也要发"},
            {"op": "claim_done", "match_id": rent, "quote": "房租交了"},
            {"op": "drop", "match_id": notes, "quote": "笔记不弄了"}])
        router = FakeRouter(bad, good)
        got = extract.run(self.store, router, ref, today=TODAY)
        self.assertEqual(len(router.calls), 2)
        self.assertIn("没通过检查", router.calls[1])
        self.assertEqual(got["todo_add"], 1)  # 学生证明那条已经有了，只补日期
        rows = {r["title"]: dict(r) for r in self.db.conn.execute("SELECT * FROM tasks")}
        self.assertEqual(rows["去银行激活卡"]["due"], "2026-10-31")
        self.assertEqual(rows["去银行激活卡"]["source_ref"], ref)
        self.assertEqual(rows["给学校发邮件要学生证明"]["due"], "2026-11-02")
        self.assertEqual(rows["交 Q4 房租"]["done"], 0)  # 只记她说做完了，不勾
        self.assertIsNotNone(rows["交 Q4 房租"]["claimed_done_at"])
        self.assertEqual(rows["整理课程笔记"]["status"], "drop_asked")  # 不删
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 4)
        calls = self.db.conn.execute("SELECT valid FROM llm_calls WHERE task = 'extract_realtime' ORDER BY id").fetchall()
        self.assertEqual([c[0] for c in calls], [0, 1])
        self.assertEqual(self.db.conn.execute("SELECT source_ref FROM evidence").fetchone()[0], ref)
        self.assertIsNotNone(old)

    def test_ethan_owner_and_wishes(self):
        self.on()
        wid = self.db.add_wish("去芬兰看极光")
        text = "你帮我查下去爱丁堡的火车票，又想去芬兰了，还好想去京都看红叶"
        ref = self.say(text)
        got = extract.run(self.store, FakeRouter(answer(
            todos=[{"op": "add", "title": "查伦敦到爱丁堡火车票", "owner": "ethan", "quote": "你帮我查下去爱丁堡的火车票"}],
            wishes=[{"op": "mention", "match_id": wid, "quote": "又想去芬兰了"},
                    {"op": "add", "text": "去京都看红叶", "category": "place", "quote": "好想去京都看红叶"},
                    {"op": "add", "text": "去芬兰看极光", "category": "place", "quote": "想去芬兰"}])), ref, today=TODAY)
        self.assertEqual((got["wish_add"], got["wish_mention"]), (1, 2))
        t = self.db.conn.execute("SELECT owner, promised_by_ethan FROM tasks").fetchone()
        self.assertEqual((t["owner"], t["promised_by_ethan"]), ("ethan", 1))
        w = self.db.conn.execute("SELECT mention_count, quotes FROM wishes WHERE id = ?", (wid,)).fetchone()
        self.assertEqual(w["mention_count"], 3)
        self.assertEqual(len(json.loads(w["quotes"])), 2)
        kyoto = self.db.conn.execute("SELECT category FROM wishes WHERE quote = '去京都看红叶'").fetchone()
        self.assertEqual(kyoto["category"], "place")

    def test_facts_remember_people_events(self):
        self.on()
        text = "记住我对青霉素过敏。我不吃香菜。我妈最近在学瑜伽。今天 presentation 讲得超顺"
        ref = self.say(text)
        data = answer(
            facts=[{"dimension": "preferences", "statement": "她不吃香菜", "quote": "我不吃香菜"},
                   {"dimension": "rhythms", "subject": "other", "statement": "妈妈在学瑜伽", "quote": "我妈最近在学瑜伽"},
                   {"dimension": "values", "subject": "other", "statement": "小林很自律", "quote": "今天 presentation 讲得超顺"}],
            remember=[{"dimension": "body", "statement": "她对青霉素过敏", "quote": "记住我对青霉素过敏"}],
            people=[{"name": "妈", "relation": "妈妈", "note": "在学瑜伽", "quote": "我妈最近在学瑜伽"}],
            events=[{"title": "presentation 讲得很顺", "occurred_at": "2026-10-30", "emotion": "proud", "intensity": 4,
                     "dims": ["abilities"], "quote": "今天 presentation 讲得超顺"}])
        got = extract.run(self.store, FakeRouter(data, data), ref, today=TODAY)
        self.assertEqual((got["fact_add"], got["event_add"], got["person"]), (3, 1, 1))  # “小林很自律”对不上提到的人，不记
        facts = {r["statement"]: r for r in lifebase.current_facts(self.db)}
        self.assertEqual(facts["她对青霉素过敏"]["importance"], 5)
        self.assertIn("remember", facts["她对青霉素过敏"]["tags"])
        self.assertTrue(facts["妈妈在学瑜伽"]["subject"].startswith("person:"))
        self.assertEqual(facts["妈妈在学瑜伽"]["visibility"], "private")
        self.assertIn("瑜伽", self.db.conn.execute("SELECT notes FROM people WHERE name = '妈'").fetchone()[0])
        # 同一句再说一遍：事实只记证实，事件不重复
        ref2 = self.say(text + "！")
        again = extract.run(self.store, FakeRouter(data), ref2, today=TODAY)
        self.assertEqual((again["fact_add"], again["fact_seen"], again["event_add"]), (0, 3, 0))
        ev = json.loads(lifebase.current_facts(self.db, "preferences")[0]["evidence_ids"])
        self.assertEqual(len(ev), 2)

    def test_shadow_writes_nothing_and_short_skipped(self):
        self.on("shadow")
        ref = self.say("明天要去银行激活卡")
        got = extract.run(self.store, FakeRouter(answer(todos=[{"op": "add", "title": "激活卡", "quote": "去银行激活卡"}])), ref, today=TODAY)
        self.assertEqual(got["shadow"]["todos"], 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM evidence").fetchone()[0], 0)
        self.assertEqual(extract.run(self.store, FakeRouter(), self.say("嗯嗯"), today=TODAY), {"skipped": "太短"})

    def test_agent_kicks_only_when_on(self):
        from ethan.core.agent import Ethan
        router = SimpleNamespace(complete=lambda *a, **k: SimpleNamespace(text="好", backend="x", model="m", cost_usd=0),
                                 settings=None)
        ethan = Ethan(self.store, router, persona="人设")
        kicked = []
        ethan.extract_kick = lambda root, ref: kicked.append(ref)
        ethan.reply("cli", "明天要去银行激活卡", today=TODAY)
        self.assertEqual(kicked, [])
        self.on()
        ethan.reply("cli", "后天要交论文", today=TODAY)
        self.assertEqual(len(kicked), 1)
        self.assertEqual(extract.load(self.store, kicked[0])[0]["text"], "后天要交论文")


class ExtractEvalTest(unittest.TestCase):
    def test_scorer(self):
        cases = {c["id"]: c for c in evals.load("extract_realtime")}
        self.assertGreaterEqual(len(cases), 30)
        score = evals._score_extract_realtime
        ok = {"todos": [{"op": "add", "title": "去银行激活卡", "due": "2026-10-31", "owner": "mia"}], "wishes": []}
        self.assertTrue(score(ok, cases["todo-add-due"]["expect"])[0])
        self.assertFalse(score({**ok, "wishes": [{"op": "add", "text": "x"}]}, cases["todo-add-due"]["expect"])[0])
        self.assertFalse(score({"todos": []}, cases["todo-add-due"]["expect"])[0])
        self.assertTrue(score(EMPTY, cases["chat-none"]["expect"])[0])
        self.assertFalse(score({**EMPTY, "facts": [{"dimension": "values", "statement": "x"}]}, cases["chat-none"]["expect"])[0])
        self.assertTrue(score({"todos": [{"op": "update", "match_id": 12}]}, cases["todo-dup"]["expect"])[0])
        self.assertFalse(score({"todos": [{"op": "add", "title": "发邮件"}]}, cases["todo-dup"]["expect"])[0])
        pref = {"facts": [{"dimension": "body", "statement": "她不吃香菜"}], "todos": [], "wishes": []}
        self.assertTrue(score(pref, cases["fact-pref"]["expect"])[0])

    def test_runner_offline(self):
        case = next(c for c in evals.load("extract_realtime") if c["id"] == "todo-claim")
        router = FakeRouter(answer(todos=[{"op": "claim_done", "match_id": 15, "quote": "房租我已经交掉了"}]))
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            out = evals._extract_realtime(router, store.db, case)
            store.close()
        self.assertTrue(evals._score_extract_realtime(out, case["expect"])[0])


if __name__ == "__main__":
    unittest.main()
