"""打扰登记表写“每件只问一次”的几类（约撞时间、计划撞了、路线排不开、准备员问要不要做、惊喜玩了没有、打卡提醒），
发出去要记进 nudge_once：以前只记在按天的状态文件里，第二天会对同一件再问一次（修盘点 bug）。
只修“重复问”；“没回就挂在首页”她还没拍板，不在这里。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ethan import events, nudges, proactive
from ethan.memory import MemoryStore
from tests.test_review_p2b import Backend, Channel, london

# 用 told() 判断、登记表写“每个 / 每件 / 每对 1 次”或“每天 / 每两天最多 1 次”的键
ONCE_KINDS = ("clash:1", "plan-clash:2026-10-23:5:午饭", "route:2026-10-23:abc", "prep-ask:3",
              "redeem-check:1:2026-10-20:1", "checkin-ask:2026-10-23")


class NudgeOnceReplayTest(unittest.TestCase):
    def setUp(self):
        from ethan.core.agent import Ethan
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router, Settings
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        self.ethan = Ethan(self.store, Router(Backend(), Ledger(root / "ledger"), Settings(root / "settings.json")), persona="人设")
        self.channel = Channel()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_clash_asked_once_across_days(self):
        """整天回放：周五下午问过“约撞了”，她没回；周六同一时间不再问同一个约。"""
        eid = events.add(self.db, "2099-01-02", "15:00", "17:00", "The Devonshire 午餐", "Denman St", note="来自邮件")
        self.db.conn.execute("UPDATE future_events SET conflict = ? WHERE id = ?", ("伦敦最火Sunday Roast", eid))
        self.db.conn.commit()
        others = {c.__name__: (lambda *a: None) for c in nudges.checks() if c.__name__ != "_clash"}
        with mock.patch.multiple(nudges, **others):
            first = proactive.tick(self.ethan, self.channel, now=london(13, 0))
            self.assertIn(f"clash:{eid}：已发送", first)
            second = proactive.tick(self.ethan, self.channel, now=london(13, 0).replace(day=24))
        self.assertNotIn(f"clash:{eid}", second)
        self.assertTrue(nudges.told(self.db, f"clash:{eid}"))

    def test_registry_once_kinds_are_marked(self):
        for key in ONCE_KINDS:
            self.assertTrue(key.startswith(proactive.ONCE_PREFIXES), key)


if __name__ == "__main__":
    unittest.main()
