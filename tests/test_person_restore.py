"""关系页“删掉的人”（4）：删掉的人聊天里再提到也不自动建卡；要恢复在“删掉的人”里点，卡按删的时候的样子回来。
“该联系谁”只用人物卡那套（决定 5）：领地页、属性分、小伴 聊天、Obsidian 看板都从 relations 取，老的 people_to_contact 下线。"""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import extract, locks, relations
from tests.test_workbench import MONDAY, make_demo

EMPTY = {"todos": [], "wishes": [], "facts": [], "events": [], "remember": [], "people": []}


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def card(self, name):
        return self.db.conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()

    def test_delete_list_restore_with_content(self):
        pid = relations.upsert(self.db, "小林", source="wechat", circle="挚友", relation="大学室友", wx_key="ab" * 8, wx_name="林林")
        relations.set_field(self.db, pid, "notes", "她改过的备注")
        self.assertTrue(relations.delete(self.db, pid)["ok"])
        gone = relations.deleted_people(self.db)
        self.assertEqual([(x["name"], x["relation"], x["kept"]) for x in gone], [("小林", "大学室友", True)])  # 微信名字那把锁不重复列
        # 聊天里再提到：不自动建卡
        extract.apply(self.db, {**EMPTY, "people": [{"name": "小林", "relation": "同学", "note": "x", "quote": "x"}]}, None, "ref", MONDAY)
        self.assertIsNone(self.card("小林"))
        self.assertIsNone(relations.upsert(self.db, "林林"))
        # 恢复：卡上的内容回来，删除锁都放开，之后自动入口又能更新这张卡
        out = relations.restore(self.db, gone[0]["id"])
        self.assertTrue(out["ok"], out)
        row = self.card("小林")
        self.assertEqual((row["circle"], row["relation"], row["notes"], row["wx_name"]), ("挚友", "大学室友", "她改过的备注", "林林"))
        self.assertEqual(relations.deleted_people(self.db), [])
        self.assertFalse(relations.restore(self.db, gone[0]["id"])["ok"])  # 点两次不出错
        self.assertEqual(relations.upsert(self.db, "小林", recent="在搬家"), row["id"])
        self.assertEqual(self.card("小林")["recent"], "在搬家")

    def test_old_delete_without_snapshot_restores_name(self):
        locks.lock(self.db, "person", "name:阿哲", "deleted", title="阿哲")  # 11-06 以前删的：锁里没存卡的内容
        gone = relations.deleted_people(self.db)
        self.assertEqual([(x["name"], x["kept"]) for x in gone], [("阿哲", False)])
        self.assertTrue(relations.restore(self.db, gone[0]["id"])["ok"])
        self.assertIsNotNone(self.card("阿哲"))

    def test_forgotten_evidence_not_brought_back(self):
        eid = 77
        pid = relations.upsert(self.db, "Mia", source="wechat", recent="刚换工作", relation="同事", source_ids=json.dumps([eid]),
                               organized_at="2026-11-01")
        relations.delete(self.db, pid)
        relations.forget_evidence(self.db, [eid])  # 删了以后她又忘掉了那份记录
        lock_id = relations.deleted_people(self.db)[0]["id"]
        snap = json.loads(self.db.conn.execute("SELECT value FROM manual_locks WHERE id = ?", (lock_id,)).fetchone()[0])
        self.assertIsNone(snap["recent"])
        relations.restore(self.db, lock_id)
        self.assertIsNone(self.card("Mia")["recent"])

    def test_workbench_page_and_action(self):
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        pid = relations.upsert(self.db, "Leo", source="wechat", relation="健身房认识的")
        relations.delete(self.db, pid)
        wb = Workbench(self.store, Settings(Path(self.tmp.name) / "s.json"))
        page = wb.relations_page()
        self.assertEqual([x["name"] for x in page["deleted"]], ["Leo"])
        self.assertTrue(wb.act("person.restore", {"id": page["deleted"][0]["id"]})["ok"])
        self.assertEqual([x["name"] for x in wb.relations_page()["people"]], ["Leo"])


class ContactOnlyFromCardsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.db = self.store.db
        relations.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_default_interval_counts_everywhere(self):
        """只设了重要程度、没手设间隔的人（3 级默认 14 天），领地页和属性分都算“该联系了”（老的只看手设间隔，漏掉她们）。"""
        from ethan import attributes, lifelog, territory
        day = MONDAY
        relations.upsert(self.db, "母上大人", source="wechat", importance=3, last_contact=(day - timedelta(days=20)).isoformat())
        relations.upsert(self.db, "小雨", source="wechat", importance=2, last_contact=(day - timedelta(days=3)).isoformat())
        cs = relations.contact_status(self.db, day, "me_page")
        self.assertEqual((cs["tracked"], [x["name"] for x in cs["contact"]]), (2, ["母上大人"]))
        lifelog.add(self.db, "contact", day, who="小雨")
        items = {i["name"]: i for i in attributes.social(self.db, day - timedelta(days=29), day)}
        self.assertEqual(items["联系重要的人"]["actual"], "1/2 人按时联系")
        rows = {r["name"]: r for r in territory.health(self.db, "social", day, True)}
        self.assertEqual(rows["联系重要的人"]["actual"], "1 个该联系了")
        self.assertIn("母上大人", rows["联系重要的人"]["note"])

    def test_ethan_context_has_no_old_list(self):
        ctx = self.store.load_context(date(2026, 11, 6))
        self.assertNotIn("people_to_contact", ctx)
        self.assertIn("relations", ctx)


if __name__ == "__main__":
    unittest.main()
