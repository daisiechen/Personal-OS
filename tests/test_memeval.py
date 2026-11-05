import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import lifebase, memeval, memsearch, memusers
from ethan.autodeploy import Deployer
from ethan.memory import MemoryStore
from tests.test_autodeploy import FakeServer


class Router:
    """回答按 answer 给；检验员看回答里有没有 right 的关键词。"""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append(purpose)
        if purpose == "memeval-judge":
            content = messages[0]["content"]
            ok = "Islington" in content.split("回答：")[1].split("【正确说法】")[0]
            return SimpleNamespace(text=json.dumps({"ok": ok, "why": "x"}))
        return SimpleNamespace(text=self.answer)


class MemEvalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:1", "2026-10-30", "她说她住在 Camden")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_ticket_becomes_items_by_layer(self):
        lifebase.add_fact(self.db, "resources", "她住在 Camden 公寓", [self.ev])
        t = memusers.open_ticket(self.db, "她住在 Islington 公寓", "她住在 Camden", quote="我住 Islington 啊")
        tasks = [r["task"] for r in self.db.conn.execute("SELECT task FROM eval_items WHERE ticket_id = ?", (t["id"],))]
        self.assertEqual(tasks, ["fact_ops", "answer"])  # 记错了：每晚对照那步 + 回答
        self.assertEqual(memeval.add_for_ticket(self.db, t["id"]), [])  # 同一张单只出一次
        lifebase.add_fact(self.db, "body", "她对花生过敏", [self.ev])
        nf = memusers.open_ticket(self.db, "她对花生过敏", "她什么都能吃")
        self.assertEqual([r["task"] for r in self.db.conn.execute("SELECT task FROM eval_items WHERE ticket_id = ?", (nf["id"],))],
                         ["retrieval", "answer"])
        untold = memusers.open_ticket(self.db, "她对芒果过敏")
        self.assertFalse(self.db.conn.execute("SELECT 1 FROM eval_items WHERE ticket_id = ?", (untold["id"],)).fetchone())

    def test_run_finds_regressions_and_gate_keeps_baseline(self):
        lifebase.add_fact(self.db, "resources", "她住在 Camden 公寓", [self.ev])
        memusers.open_ticket(self.db, "她住在 Islington 公寓", "她住在 Camden")
        self.db.conn.execute("UPDATE eval_items SET active = 0 WHERE task != 'answer'")
        got = memeval.run(self.db, Router("你住 Islington 呀"), "weekly", self.store)
        self.assertEqual((got["total"], got["passed"], got["regressions"]), (1, 1, []))
        bad = memeval.run(self.db, Router("你不是住 Camden 吗"), "gate", self.store)
        self.assertEqual(len(bad["regressions"]), 1)  # 以前过、这次没过：退步
        again = memeval.run(self.db, Router("你不是住 Camden 吗"), "gate", self.store)
        self.assertEqual(len(again["regressions"]), 1)  # 闸门拦下的那次不改基线，下次照样拦
        summary = memeval.weekly_summary(self.db)
        self.assertEqual((summary["纠错数"], summary["检验题通过率"]), (1, 1.0))

    def test_gate_retries_a_flaky_item(self):
        """2026-11-01：照片那题一次判错就把整批上线退回了。闸门上没过的再跑两次，有一次过就不算退步；真退步照样拦。"""
        lifebase.add_fact(self.db, "resources", "她住在 Camden 公寓", [self.ev])
        memusers.open_ticket(self.db, "她住在 Islington 公寓", "她住在 Camden")
        self.db.conn.execute("UPDATE eval_items SET active = 0 WHERE task != 'answer'")
        memeval.run(self.db, Router("你住 Islington 呀"), "weekly", self.store)

        class Flaky(Router):
            def complete(self, system, messages, purpose="chat", images=()):
                if purpose != "memeval-judge":
                    self.n = getattr(self, "n", 0) + 1
                    self.answer = "你不是住 Camden 吗" if self.n == 1 else "你住 Islington 呀"
                return super().complete(system, messages, purpose, images)
        self.assertEqual(memeval.run(self.db, Flaky(""), "gate", self.store)["regressions"], [])
        self.assertEqual(len(memeval.run(self.db, Router("你不是住 Camden 吗"), "gate", self.store)["regressions"]), 1)

    def test_retrieval_item_checks_the_key(self):
        fid = lifebase.add_fact(self.db, "body", "她对花生过敏", [self.ev])
        memsearch.refresh(self.db)
        memsearch.search(self.db, ["晚饭吃什么"], user="ethan")  # 那一轮没查出这条
        t = memusers.open_ticket(self.db, "她对花生过敏", "她什么都能吃")
        item = dict(self.db.conn.execute("SELECT * FROM eval_items WHERE ticket_id = ? AND task = 'retrieval'", (t["id"],)).fetchone())
        self.assertEqual(json.loads(item["input"])["queries"], ["晚饭吃什么"])
        ok, note = memeval.run_item(self.db, Router(""), item)
        self.assertFalse(ok)  # 这道题记下的就是“那句话查不出这条”，查记忆改好了它才会过
        self.assertIn(f"fact:{fid}", note)
        self.assertEqual(json.loads(item["expect"])["key"], f"fact:{fid}")

    def test_deploy_gate_runs_only_when_memory_code_changed(self):
        with tempfile.TemporaryDirectory() as d:
            mk = lambda: Deployer(Path("/repo"), Path(d), run=server, sleep=lambda s: None, unit_target=Path("/etc/systemd/system"))
            server = FakeServer(units="ethan/cards.py\n")
            mk().once()
            self.assertFalse(any("ethan.memeval" in " ".join(c) for c in server.calls))
            server = FakeServer(units="ethan/prompts/fact_ops.md\n")
            mk().once()
            self.assertTrue(any("ethan.memeval" in " ".join(c) for c in server.calls))
            server = FakeServer(units="ethan/memsearch.py\n", fail_on="ethan.memeval")
            (Path(d) / "deploy-status.json").unlink()
            record = mk().once()
            self.assertEqual((record["result"], record["step"]), ("失败，已退回原版本", "跑记忆检验题"))


if __name__ == "__main__":
    unittest.main()
