"""页面加载提速：全部记忆的“用过几次”一次读完、系统体检和设置页分开拉并复用 5 分钟。"""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import lifebase, memsearch, mepage
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import make_demo


class PageSpeedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_use_counts_same_as_one_by_one(self):
        lifebase.ensure(self.db)
        memsearch.ensure(self.db)
        ev = lifebase.add_evidence(self.db, "chat", "raw:s:1", "2026-11-01", "mia：随便聊聊")
        a = lifebase.add_fact(self.db, "rhythms", "她每天早上跑步。", [ev])
        b = lifebase.add_fact(self.db, "preferences", "她喜欢罗马。", [ev])
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for keys, user in (([f"fact:{a}", f"fact:{b}"], "ethan"), ([f"fact:{a}", f"fact:{a}"], "me"), ([f"fact:{b}"], "system"), ([f"fact:{a}1"], "ethan")):
            self.db.conn.execute("INSERT INTO retrieval_log (at, scene, round, queries, keys, latency_ms, user) VALUES (?, 't', 1, '[]', ?, 0, ?)",
                                 (now, json.dumps(keys), user))
        self.db.conn.commit()
        got = mepage._use_counts(self.db, 3650)
        for fid in (a, b):
            self.assertEqual(got.get(fid, 0), mepage._uses(self.db, fid, 3650)[0])
        self.assertEqual(got[a], 2)
        items = {x["id"]: x["uses"] for x in mepage.all_memory(self.db)["items"]}
        self.assertEqual(items[a], 2)
        self.assertEqual(items[b], 1)

    def test_system_check_apart_and_reused(self):
        self.assertNotIn("system", self.wb.settings_page())
        first = self.wb.system_page()
        self.assertIn("bad", first)
        self.assertIs(self.wb.system_page(), first)  # 5 分钟内直接用上次的
        self.assertIsNot(self.wb.system_page(fresh=True), first)


if __name__ == "__main__":
    unittest.main()
