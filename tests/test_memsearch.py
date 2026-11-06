import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import evals, lifebase, memsearch
from ethan.core.agent import Ethan, build_system_prompt, parse_reply
from ethan.memory import MemoryStore


class MemsearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.src = lifebase.add_evidence(self.db, "chat", "t:0", "2026-10-30", "mia：测试")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_tokens(self):
        self.assertEqual(memsearch.tokens("膝盖疼 Pret A"), ["膝盖", "盖疼", "pret"])
        self.assertNotIn("什么", memsearch.tokens("我对什么过敏", query=True))

    def test_eval_set_passes_offline(self):
        res = evals.run("retrieval", None, None)
        self.assertTrue(res["passed"], [r for r in res["rows"] if not r["pass"]])

    def test_refresh_is_incremental_and_tracks_changes(self):
        fid = lifebase.add_fact(self.db, "body", "她膝盖有旧伤", [self.src])
        lifebase.add_evidence(self.db, "chat", "t:1", "2026-10-30", "ethan：那就别跑太远")
        self.assertGreater(memsearch.refresh(self.db), 0)
        self.assertEqual(memsearch.refresh(self.db), 0)
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM mem_docs WHERE body LIKE '%别跑太远%'").fetchone())  # 只收她说的
        self.assertEqual([r["key"] for r in memsearch.search(self.db, ["膝盖"], user="ethan")], [f"fact:{fid}"])
        lifebase.invalidate(self.db, fid)
        self.assertEqual(memsearch.refresh(self.db), 1)
        self.assertEqual(memsearch.search(self.db, ["膝盖"], user="ethan"), [])
        log = self.db.conn.execute("SELECT scene, keys FROM retrieval_log ORDER BY id").fetchall()
        self.assertEqual([json.loads(r["keys"]) for r in log], [[f"fact:{fid}"], []])

    def test_long_file_snippet(self):
        body = "开头 " * 300 + "这里写着 iCloud 每月 6 镑" + " 结尾" * 300
        cut = memsearch.snippet(body, {"icloud"})
        self.assertIn("iCloud", cut)
        self.assertLessEqual(len(cut), memsearch.SNIPPET + 2)


class LookupTest(unittest.TestCase):
    def test_agent_looks_up_then_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            lifebase.ensure(store.db)
            src = lifebase.add_evidence(store.db, "chat", "t:0", "2025-12-20", "mia：芬兰")
            lifebase.add_event(store.db, "2025-12-20", "和小林去芬兰看极光", [src], "manual")
            answers = ["[查 芬兰 极光]", "去年 12 月 20 号，和小林一起"]
            systems = []

            def complete(system, messages, purpose="chat", images=()):
                systems.append(system)
                return SimpleNamespace(text=answers.pop(0), backend="x", model="m", cost_usd=0)
            ethan = Ethan(store, SimpleNamespace(complete=complete, settings=None), persona="人设")
            out = ethan.reply("cli", "我们那次旅行是啥时候来着", today=date(2026, 10, 30))
            self.assertEqual(out["bubbles"], ["去年 12 月 20 号，和小林一起"])
            self.assertIn("### 查记忆", systems[0])
            self.assertIn("你刚才要查的「芬兰 极光」", systems[1])
            self.assertIn("和小林去芬兰看极光", systems[1])
            self.assertEqual([r["role"] for r in store.raw.recent(5)], ["mia", "ethan"])
            self.assertEqual(store.raw.recent(1)[0]["text"], "去年 12 月 20 号，和小林一起")
            rounds = [r[0] for r in store.db.conn.execute("SELECT round FROM retrieval_log WHERE scene != 'portrait' ORDER BY id")]
            self.assertEqual(rounds, [1, 2])
            store.close()

    def test_agent_searches_her_mailbox(self):  # “订单号也在我邮箱里自己去找”
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            answers = ["[查邮件 ltmuseum]", "找到了，订单号 778899"]
            systems, searched = [], []

            def complete(system, messages, purpose="chat", images=()):
                systems.append(system)
                return SimpleNamespace(text=answers.pop(0), backend="x", model="m", cost_usd=0)
            ethan = Ethan(store, SimpleNamespace(complete=complete, settings=None), persona="人设")
            ethan.mail_search = lambda words: searched.append(words) or [
                {"account": "Gmail", "folder": "x", "sender": "LTM", "subject": "Your order 778899", "at": "2026-10-20", "text": "Order 778899"}]
            out = ethan.reply("cli", "订单号在我邮箱里你自己找", today=date(2026, 10, 31))
            self.assertEqual(out["bubbles"], ["找到了，订单号 778899"])
            self.assertEqual(searched, [["ltmuseum"]])
            self.assertIn("### 查她的邮箱", systems[0])
            self.assertIn("Your order 778899", systems[1])
            self.assertEqual(parse_reply("好 [查邮件 ltmuseum]")["bubbles"], ["好"])
            store.close()

    def test_lookup_mark_stripped_and_not_offered_outside_chat(self):
        self.assertEqual(parse_reply("好的 [查 芬兰]")["bubbles"], ["好的"])
        self.assertNotIn("### 查记忆", build_system_prompt("人设", {"today": "2026-10-30"}))


if __name__ == "__main__":
    unittest.main()
