import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import evals, llm
from ethan.memory import MemoryStore

SCHEMA = {"type": "object", "required": ["kind", "items"], "properties": {
    "kind": {"type": "string", "enum": ["a", "b"]},
    "items": {"type": "array", "maxItems": 2, "items": {"type": "object", "required": ["quote"],
                                                         "properties": {"quote": {"type": "string", "maxLength": 20}}}}}}


class Seq:
    def __init__(self, *texts):
        self.texts, self.prompts = list(texts), []

    def complete(self, system, messages, purpose="chat", images=()):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(text=self.texts.pop(0), model="m", backend="b", cost_usd=0.0)


class StructuredTest(unittest.TestCase):
    """2026-10-30 执行保障：模型只出提议，代码校验格式和取值，引用的原话必须在原文里，不合格重试一次，每次留痕。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_validate_catches_types_enums_lengths(self):
        self.assertEqual(llm.validate({"kind": "a", "items": [{"quote": "好"}]}, SCHEMA), [])
        errs = llm.validate({"kind": "c", "items": [{"quote": "x" * 30}, {}, {}]}, SCHEMA)
        self.assertTrue(any("只能是" in e for e in errs))
        self.assertTrue(any("太长" in e for e in errs))
        self.assertTrue(any("太多" in e for e in errs))
        self.assertTrue(any("缺了" in e for e in errs))
        self.assertTrue(llm.validate({"kind": "a", "items": "no"}, SCHEMA))

    def test_retry_with_errors_then_log(self):
        source = "我下周三要交论文"
        check = lambda d: [f"引用不在原文里：{i['quote']}" for i in d["items"] if not llm.quote_in(i["quote"], source)]
        r = Seq('{"kind": "a", "items": [{"quote": "我下周四要交"}]}', '好的 {"kind": "a", "items": [{"quote": "下周三要交论文"}]}')
        got = llm.structured(r, self.store.db, "t", "系统", source, SCHEMA, check=check, version="3")
        self.assertEqual(got["items"][0]["quote"], "下周三要交论文")
        self.assertIn("没通过检查", r.prompts[1])
        rows = self.store.db.conn.execute("SELECT valid, attempt, prompt_version FROM llm_calls ORDER BY id").fetchall()
        self.assertEqual([(x["valid"], x["attempt"], x["prompt_version"]) for x in rows], [(0, 1, "3"), (1, 2, "3")])
        self.assertEqual(len(llm.recent_failures(self.store.db)), 1)

    def test_gives_up_after_two_bad_answers(self):
        self.assertIsNone(llm.structured(Seq("不是 JSON", '{"kind": "z", "items": []}'), self.store.db, "t", "s", "u", SCHEMA))

    def test_prompts_have_versions(self):
        version, text = llm.load_prompt("file_digest")
        self.assertNotEqual(version, "0")
        self.assertIn("doc_type", text)


class EvalSetsTest(unittest.TestCase):
    def test_every_eval_file_is_well_formed_and_registered(self):
        files = sorted(evals.EVALS.glob("*.jsonl"))
        self.assertTrue(files)
        for f in files:
            self.assertIn(f.stem, evals.TASKS, f"{f.name} 没有登记怎么跑、怎么判")
            ids = set()
            for case in evals.load(f.stem):
                self.assertTrue({"id", "input", "expect"} <= set(case), f"{f.name} 缺字段")
                self.assertNotIn(case["id"], ids)
                ids.add(case["id"])

    def test_runner_scores_with_a_fake_model(self):
        answers = {c["input"]["text"][:10]: c["expect"]["doc_type"] for c in evals.load("file_digest")}

        class R:
            def complete(self, system, messages, purpose="chat", images=()):
                text = messages[0]["content"].split("\n\n", 1)[1]
                return SimpleNamespace(text=json.dumps({"title": "招行流水 账单", "summary": "s", "doc_type": answers[text[:10]], "tags": []}),
                                       model="m", backend="b", cost_usd=0)
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            res = evals.run("file_digest", R(), store.db)
            store.close()
        self.assertTrue(res["passed"])


if __name__ == "__main__":
    unittest.main()
