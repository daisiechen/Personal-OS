import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from ethan import nudges, perks
from ethan.core.agent import Ethan, parse_reply
from ethan.memory import MemoryStore

TODAY = date(2026, 10, 30)


class PerksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_marks_add_update_and_keep_only_last4(self):
        got = perks.note(self.db, "记下了 [会员 项目=英航常旅客 类别=airline 等级=银卡 积分=12,000 积分到期=2027-03-01 尾号=12345678 权益=“免费选座 优先登机”]")
        self.assertEqual(got, [("会员", "英航常旅客", "added")])
        perks.note(self.db, "[会员 项目=英航常旅客 积分=15000 等级到期=2026-11-20 保级进度=480 保级门槛=600 类别=飞机]")
        r = dict(self.db.conn.execute("SELECT * FROM memberships").fetchone())
        self.assertEqual((r["tier"], r["points"], r["member_no_last4"], r["category"]), ("银卡", 15000.0, "5678", "airline"))
        self.assertEqual(r["benefits"], "免费选座 优先登机")
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM memberships").fetchone()[0], 1)
        bad = perks.note(self.db, "[保险 公司=AXA 类型=travel 续保=明年 尾号=12]")
        self.assertIn("没记的", bad[0][2])
        self.assertIsNone(self.db.conn.execute("SELECT renew_at FROM policies").fetchone()[0])
        self.assertIn("缺", perks.note(self.db, "[保险 保额=100]")[0][2])
        self.assertIn("英航常旅客（airline）：银卡，积分 15000", perks.brief(self.db))

    def test_due_windows_once_each(self):
        perks.note(self.db, "[会员 项目=万豪 类别=hotel 等级到期=2026-11-25] [保险 公司=AXA 类型=travel 续保=2026-11-05 自动续保=否]"
                            "[保险 公司=Aviva 类型=contents 续保=2026-11-02 自动续保=是]")
        keys = [k for k, _ in perks.due(self.db, TODAY)]
        self.assertEqual(len(keys), 2)  # 自动续保的不提醒
        self.assertTrue(keys[0].endswith(":7"))  # AXA 6 天后：7 天档，排最前
        self.assertTrue(keys[1].endswith(":30"))
        self.assertEqual(perks.due(self.db, date(2026, 10, 1)), [])  # 还早

    def test_nudge_daytime_and_told(self):
        perks.note(self.db, "[保险 公司=AXA 类型=travel 续保=2026-11-05]")
        self.assertIsNone(nudges._perks(self.db, datetime(2026, 10, 30, 8, 0), {}))
        key, text = nudges._perks(self.db, datetime(2026, 10, 30, 11, 0), {})
        self.assertIn("AXA", text)
        nudges.mark_told(self.db, [key])
        self.assertIsNone(nudges._perks(self.db, datetime(2026, 10, 30, 11, 0), {}))

    def test_agent_records_and_strips(self):
        def complete(system, messages, purpose="chat", images=()):
            self.assertIn("她的会员、积分和保险", system)
            return SimpleNamespace(text="收到，银卡哦 [会员 项目=英航常旅客 类别=airline 等级=银卡]", backend="x", model="m", cost_usd=0)
        out = Ethan(self.store, SimpleNamespace(complete=complete, settings=None), persona="人设").reply("cli", "我英航是银卡", today=TODAY)
        self.assertEqual(out["bubbles"], ["收到，银卡哦"])
        self.assertEqual(self.db.conn.execute("SELECT tier FROM memberships").fetchone()[0], "银卡")
        self.assertEqual(parse_reply("好 [保险 公司=AXA 类型=travel]")["bubbles"], ["好"])


if __name__ == "__main__":
    unittest.main()
