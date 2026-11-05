"""已收文件整理成关于她的说法（ontology 总方案 1.2）：先判断是不是关于她、原文片段要对得上、失败重试、进度可查。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import interviews, lifebase, memsearch
from ethan.memory import MemoryStore

CV = "个人简历\n教育经历：2012-2016 上海财经大学 金融学 本科\n工作经历：2016-2019 在一家券商做行业研究\n技能：英语流利，会 Python\n自我评价：自驱力强"
REPORT = "2026 年英国新能源汽车市场报告\n第一章 市场规模……"


class Router:
    def __init__(self, *answers):
        self.answers = list(answers)

    def complete(self, system, messages, purpose="chat", images=()):
        a = self.answers.pop(0)
        return SimpleNamespace(text=json.dumps(a, ensure_ascii=False))


class FileDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.cv = lifebase.add_evidence(self.db, "file", "file:cv", "2026-10-25", CV, path="files/简历.pdf")
        self.rep = lifebase.add_evidence(self.db, "file", "file:report", "2026-10-26", REPORT, path="files/报告.pdf")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_cv_becomes_statements_report_skipped(self):
        router = Router(
            {"about_her": True, "facts": [
                {"statement": "在上海财经大学读金融学本科", "dimension": "abilities", "quote": "2012-2016 上海财经大学 金融学 本科",
                 "property": "studied_at", "when": "2012-2016"},
                {"statement": "在券商做行业研究", "dimension": "resources", "quote": "2016-2019 在一家券商做行业研究",
                 "property": "worked_at", "when": "2016-2019"},
                {"statement": "简历里自评自驱力强", "dimension": "temperament", "quote": "自驱力强", "kind": "self_report"}]},
            {"ops": [{"op": "ADD", "from": "n1", "dimension": "abilities", "statement": "在上海财经大学读金融学本科（2012-2016）"},
                     {"op": "ADD", "from": "n2", "dimension": "resources", "statement": "在券商做行业研究（2016-2019）"},
                     {"op": "ADD", "from": "n3", "dimension": "temperament", "statement": "简历里自评自驱力强"}]},
            {"about_her": False, "why": "市场报告，不是关于她的", "facts": []})
        got = interviews.digest_files(self.store, router, limit=10)
        self.assertEqual((got["files"], got["about_her"], got["facts"], got["failed"]), (2, 1, 3, 0))
        self.assertEqual(self.db.conn.execute("SELECT kind FROM facts WHERE statement LIKE '%自驱力%'").fetchone()[0], "self_report")  # 自评不当事实
        ctx = self.db.conn.execute("SELECT context, source_kind FROM facts WHERE statement LIKE '%上海财经%'").fetchone()
        self.assertIn("简历.pdf", ctx["context"])
        self.assertEqual(ctx["source_kind"], "her_provided")  # 她提供的资料，不自动算她的原话
        memsearch.refresh(self.db)
        self.assertTrue(memsearch.search(self.db, ["上海财经大学"], user="ethan", log=False))
        from ethan import onto_query
        bg = onto_query.background(self.db, "ethan")  # 能答“在哪读过书、做过什么”，按属性分好
        self.assertEqual([(i["property"], i["said_by"]) for i in bg["items"]],
                         [("studied_at", "her_provided"), ("worked_at", "her_provided")])
        twin = onto_query.background(self.db, "twin")  # 对外按开关（ontology-plan 3.3）：学历、职业经历公开档默认可以说
        self.assertEqual([i["property"] for i in twin["items"]], ["studied_at", "worked_at"])
        from ethan import lifebase
        lifebase.set_policy(self.db, "public", "career", "deny")  # 她关掉“职业经历”就只剩学历
        self.assertEqual([i["property"] for i in onto_query.background(self.db, "twin")["items"]], ["studied_at"])
        self.assertEqual(interviews.files_progress(self.db), {"files": 2, "done": 2, "about_her": 1, "facts": 3, "retrying": 0})
        self.assertEqual(interviews.digest_files(self.store, Router(), limit=10)["files"], 0)  # 整理过的不再整理
        self.assertGreater(interviews.FILES_PER_NIGHT, 0)  # 小批次验收过了，每晚自动整理打开（11-04）

    def test_quote_mismatch_fails_and_retries_then_gives_up(self):
        bad = {"about_her": True, "facts": [{"statement": "在北京读书", "dimension": "abilities", "quote": "北京大学"}]}
        for _ in range(interviews.MAX_TRIES):
            interviews.digest_files(self.store, Router(bad, bad), limit=1)
        self.assertEqual(self.db.conn.execute("SELECT tries FROM file_digest WHERE evidence_id = ?", (self.cv,)).fetchone()[0], 3)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0], 0)  # 对不上原文的不收
        nxt = interviews.files_pending(self.db, 10)
        self.assertEqual([e["id"] for e in nxt], [self.rep])  # 试满 3 次不再挑它

    def test_forgotten_file_not_digested(self):
        lifebase.forget(self.db, [self.cv])
        self.assertEqual([e["id"] for e in interviews.files_pending(self.db, 10)], [self.rep])

    def test_small_batch_by_ids(self):
        got = interviews.digest_files(self.store, Router({"about_her": False, "why": "报告", "facts": []}), limit=10, ids=[self.rep])
        self.assertEqual(got["files"], 1)
        self.assertEqual([e["id"] for e in interviews.files_pending(self.db, 10)], [self.cv])


if __name__ == "__main__":
    unittest.main()
