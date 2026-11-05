"""问她本人的事时，她的说法排在 agent 转述前面（11-04 服务器实测：搜“她读过哪些学校”，前 3 条都是 Grok 转述的学校清单）。
关键词、语义两路都测；语义用假向量模型（同 test_semantic.Fake）。"""

import tempfile
import unittest
from pathlib import Path

from ethan import lifebase, memsearch
from ethan.memory import MemoryStore
from tests.test_semantic import Fake

GROK_LIST = ("【来源：调研任务】进修计划学校清单：伦敦大学学院 UCL 博士项目、帝国理工学院博士、剑桥大学博士、牛津大学博士、"
             "伦敦政治经济学院博士、爱丁堡大学博士、曼彻斯特大学博士，申请学校按截止日期排好，读博士的学校都列在这里")


class AboutHerRankTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._old = memsearch._EMBEDDER
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        self.grok = lifebase.add_evidence(self.db, "grok", "grok:1030-phd", "2026-10-30", GROK_LIST)
        cv = lifebase.add_evidence(self.db, "file", "file:cv", "2026-11-03", "简历")
        self.school = lifebase.add_fact(self.db, "abilities", "在某理工大学读软件工程本科", [cv],
                                        source_kind="her_provided", prop="studied_at")
        self.db.conn.commit()
        memsearch.refresh(self.db)

    def tearDown(self):
        memsearch._EMBEDDER = self._old
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def keys(self, q):
        return [r["key"] for r in memsearch.search(self.db, [q], user="ethan", log=False)]

    def test_keyword_her_fact_first(self):
        self.assertFalse(memsearch.semantic_on(self.db))
        keys = self.keys("她读过哪些学校")
        self.assertEqual(keys[0], f"fact:{self.school}")
        self.assertIn(f"ev:{self.grok}", keys)  # 转述只是往后排，不是不给

    def test_semantic_her_fact_first(self):
        memsearch._EMBEDDER = Fake()
        self.assertTrue(memsearch.set_semantic(self.db, True)["semantic"])
        self.assertTrue(memsearch.similar_keys(self.db, "她读过哪些学校"))  # 语义这一路确实在出结果
        keys = self.keys("她读过哪些学校")
        self.assertEqual(keys[0], f"fact:{self.school}")
        self.assertIn(f"ev:{self.grok}", keys)

    def test_not_about_her_no_reweight(self):
        self.assertFalse(memsearch.about_her(["进修计划截止日期"]))
        self.assertTrue(memsearch.about_her(["Mia 的习惯"]))
        self.assertEqual(self.keys("进修计划截止日期")[0], f"ev:{self.grok}")


if __name__ == "__main__":
    unittest.main()
