"""权限接进统一入口（docs/design/ontology.md 6.1 节）：读取与推理按使用者把关；她逐条批准的对外表达。"""

import json
import tempfile
import unittest
from pathlib import Path

from ethan import lifebase, memsearch, portrait
from ethan.memory import MemoryStore


class PermissionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：随便聊聊")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def keys(self, q, user, scene="chat"):
        memsearch.refresh(self.db)
        return [r["key"] for r in memsearch.search(self.db, [q], scene, user=user, log=False)]

    def test_hypothesis_only_for_ethan_and_her_page(self):
        h = lifebase.add_fact(self.db, "thinking", "她可能习惯拖到截止前才动手", [self.ev], kind="hypothesis", source_kind="system_inference")
        key = f"fact:{h}"
        self.assertIn(key, self.keys("截止", "ethan"))
        self.assertIn(key, self.keys("截止", "me_page"))
        for user in ("planner", "prep", "mail"):
            self.assertNotIn(key, self.keys("截止", user), user)
        with self.assertRaises(ValueError):  # 日记 11-05 起只登记了读写作规范，连检索都不能用
            self.keys("截止", "diary")
        memsearch.refresh(self.db)
        found = memsearch.search(self.db, ["截止"], user="ethan", log=False)
        self.assertIn("我的猜测", memsearch.lines(found))  # 小伴 看到的标着是猜测
        self.assertFalse(portrait._block_facts(self.db, "thinking"))  # 大家都读的画像里没有假设

    def test_read_by_list_limits_readers(self):
        f = lifebase.add_fact(self.db, "preferences", "看牙医前要先吃点甜的", [self.ev])
        self.db.conn.execute("UPDATE facts SET read_by = ? WHERE id = ?", (json.dumps([{"user": "ethan", "purpose": "聊天"}]), f))
        self.assertIn(f"fact:{f}", self.keys("牙医", "ethan"))
        self.assertNotIn(f"fact:{f}", self.keys("牙医", "planner"))

    def test_disclosure_exactly_one_sentence_until_revoked(self):
        body = lifebase.add_fact(self.db, "body", "最近在减脂，体脂目标二十", [self.ev])
        with self.assertRaises(ValueError):
            lifebase.approve_disclosure(self.db, "在减脂", quote="")  # 没有她的原话不行
        d = lifebase.approve_disclosure(self.db, "最近在规律训练", quote="主页可以说我在规律训练", dimension="body")
        got = self.keys("训练", "twin", "public")
        self.assertEqual(got, [f"disc:{d}"])
        self.assertNotIn(f"fact:{body}", self.keys("减脂", "twin", "public"))  # 同类的别的照样不说
        self.assertNotIn(f"disc:{d}", self.keys("训练", "planner"))
        lifebase.revoke_disclosure(self.db, d)
        self.assertEqual(self.keys("训练", "twin", "public"), [])
        e = lifebase.approve_disclosure(self.db, "在做创业项目", quote="可以公开创业", until="2026-11-02T00:00:00+00:00")
        self.assertNotIn(f"disc:{e}", self.keys("创业", "twin", "public"))  # 过期了

    def test_once_disclosure_used_up_and_standing_asks_renewal(self):
        with self.assertRaises(ValueError):
            lifebase.approve_disclosure(self.db, "在找实习", quote="这次可以跟他说", once=True)  # 这次的要写对谁
        d = lifebase.approve_disclosure(self.db, "在找实习", quote="这次可以跟 Leo 说我在找实习", once=True, to_whom="Leo")
        self.assertFalse(lifebase.disclosure_ok(self.db, d, "twin"))  # 不知道来问的是谁：不说
        self.assertFalse(lifebase.disclosure_ok(self.db, d, "twin", recipient="Mia"))
        self.assertTrue(lifebase.disclosure_ok(self.db, d, "twin", recipient="leo"))
        self.assertEqual(self.keys("实习", "twin", "public"), [])  # 通用访客检索里也拿不到
        memsearch.refresh(self.db)
        self.assertEqual(len(memsearch.search(self.db, ["实习"], "public", user="twin", log=False, recipient="Leo")), 1)
        lifebase.mark_disclosure_used(self.db, d)
        self.assertFalse(lifebase.disclosure_ok(self.db, d, "twin", recipient="Leo"))  # 说过一次就失效
        s = lifebase.approve_disclosure(self.db, "在做创业项目", quote="以后都可以说我在创业")
        self.assertEqual(lifebase.renewals_due(self.db), [])
        due = lifebase.renewals_due(self.db, now="2099-01-01T00:00:00+00:00")
        self.assertEqual([r["id"] for r in due], [s])  # 30 天后问一次要不要续
        lifebase.mark_disclosure_used(self.db, s)
        self.assertTrue(lifebase.disclosure_ok(self.db, s, "twin"))  # 长期的说了照样有效
        lifebase.renew_disclosure(self.db, s)
        self.assertEqual(lifebase.renewals_due(self.db), [])

    def test_public_users_never_get_never_public_dims(self):
        f = lifebase.add_fact(self.db, "body", "每周练五次", [self.ev])
        self.db.conn.execute("UPDATE facts SET visibility = 'public' WHERE id = ?", (f,))  # 万一被标成了公开
        self.assertNotIn(f"fact:{f}", self.keys("每周练五次", "twin", "public"))

    def test_once_disclosure_claim_is_atomic_and_refundable(self):
        d = lifebase.approve_disclosure(self.db, "在找实习", quote="这次可以跟 Leo 说", once=True, to_whom="Leo")
        self.assertIsNone(lifebase.claim_disclosure(self.db, d, "twin"))  # 没说对象是谁，领不到
        first = lifebase.claim_disclosure(self.db, d, "twin", recipient="Leo")
        self.assertIsNotNone(first)
        self.assertIsNone(lifebase.claim_disclosure(self.db, d, "twin", recipient="Leo"))  # 第二个请求领不到
        self.assertTrue(lifebase.unclaim_disclosure(self.db, d, first))  # 确认没发出去才退回
        second = lifebase.claim_disclosure(self.db, d, "twin", recipient="Leo")
        self.assertTrue(lifebase.confirm_disclosure_sent(self.db, d, second))
        self.assertFalse(lifebase.unclaim_disclosure(self.db, d, second))  # 发出去了就退不回
        s = lifebase.approve_disclosure(self.db, "在创业", quote="以后都可以说")
        self.assertIsNotNone(lifebase.claim_disclosure(self.db, s, "twin"))
        self.assertIsNotNone(lifebase.claim_disclosure(self.db, s, "twin"))  # 长期的每次都能说

    def test_profile_inherits_permissions_of_its_facts(self):
        f1 = lifebase.add_fact(self.db, "preferences", "喜欢靠窗的位子", [self.ev])
        f2 = lifebase.add_fact(self.db, "preferences", "看牙医前要吃甜的", [self.ev])
        self.db.conn.execute("INSERT OR REPLACE INTO profile_blocks (dimension, summary, fact_ids, version, updated_at) VALUES "
                             "('preferences', '喜欢靠窗；看牙医前要吃甜的', ?, 1, 'x')", (json.dumps([f1, f2]),))
        self.assertIn("看牙医前要吃甜的", portrait.chat_text(self.db, "planner"))
        self.db.conn.execute("UPDATE facts SET read_by = ? WHERE id = ?", (json.dumps([{"user": "ethan"}]), f2))  # 摘要做好以后才限定读者
        text = portrait.chat_text(self.db, "planner")
        self.assertIn("喜欢靠窗的位子", text)
        self.assertNotIn("牙医", text)  # 计划器拿不到，摘要不再整段给它
        self.assertIn("看牙医前要吃甜的", portrait.chat_text(self.db, "ethan"))
        self.assertEqual(portrait.chat_text(self.db, "twin"), "")  # 对外的不读画像
        self.db.conn.execute("INSERT OR REPLACE INTO profile_blocks (dimension, summary, fact_ids, version, updated_at) VALUES "
                             "('values', '说不清依据的旧摘要', '[]', 1, 'x')")
        self.assertNotIn("说不清依据", portrait.chat_text(self.db, "ethan"))  # 不知道用到哪几条的摘要不给


if __name__ == "__main__":
    unittest.main()
