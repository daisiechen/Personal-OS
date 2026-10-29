import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from ethan import surprises, achievements, quests
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import MONDAY, make_demo


class AchievementsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def by_key(self):
        return {i["key"]: i for i in achievements.compute(self.db, self.root, MONDAY)["items"]}

    def test_counts_from_real_data(self):
        h = quests.habit_by_title(self.db, "训练")
        for i in range(52):
            quests.check_habit(self.db, h["id"], MONDAY - timedelta(days=i))
        self.db.conn.execute("INSERT INTO body_measures (day, weight, bodyfat, at) VALUES ('2026-11-01', 55, 24.5, 'x')")
        (self.root / "explore-map.json").write_text(json.dumps({"sheets": {
            "亚洲": [{"国家": f"国{i}", "状态": "已结束"} for i in range(21)],
            "城市徒步": [{"路线": f"线{i}", "状态": "已结束"} for i in range(3)],
            "Goal": [{"Goal": "三山五岳", "状态": "已结束"}, {"Goal": "南极企鹅", "状态": "想去"},
                     {"Goal": "56 个民族文化和节日", "状态": ""}]}}, ensure_ascii=False))
        a = self.by_key()
        self.assertEqual((a["train"]["value"], a["train"]["tier"], a["train"]["next"]), (52, "铜", 150))
        self.assertEqual((a["bodyfat"]["tier"], a["bodyfat"]["next"]), ("铜", 22))
        self.assertEqual((a["countries"]["tier"], a["hikes"]["value"]), ("铜", 3))
        self.assertTrue(a["goal:三山五岳"]["done"])
        self.assertNotIn("goal:56 个民族文化和节日", a)  # 她删掉的
        self.assertTrue(a["first_weigh"]["done"])
        self.assertFalse(a["outing"]["source"])  # 没数据来源的标等记录

    def test_pages(self):
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        page = wb.ach_page()
        self.assertEqual([l["desc"] for l in page["legend"]], ["公司上市", "结婚", "生娃"])
        self.assertTrue(all(i.get("desc") for i in page["items"]))  # 每条都有说明
        r = page["rewards"]
        self.assertEqual(r["total"], len(surprises.PLAYS))
        self.assertTrue(all("name" not in p for p in r["plays"] if not p["unlocked"]))  # 没解锁的不露名字
        self.assertIsInstance(wb.cards_page()["cards"], list)


class SurpriseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_each_achievement_unlocks_next_play_of_its_tier_and_ethan_reveals_once(self):
        surprises.sync(self.db, [])  # 上线那一刻什么都没有：基线是 0
        items = [{"done": True}, {"tier": "银"}]  # 一个“第一次”（铜）+ 一个银级（也算一个铜）
        plays = surprises.sync(self.db, items)
        got = [p["key"] for p in plays if p["unlocked"]]
        bronze = [p["key"] for p in surprises.PLAYS if p["tier"] == "铜"][:2]
        silver = [p["key"] for p in surprises.PLAYS if p["tier"] == "银"][:1]
        self.assertEqual(got, bronze + silver)
        page = surprises.page(plays)
        self.assertEqual(page["unlocked"], 3)
        self.assertTrue(all("name" not in p and "note" not in p for p in page["plays"] if not p["unlocked"]))
        self.assertTrue(all("name" not in p for p in page["plays"] if p.get("waiting")))  # 等 小伴 揭晓的也先不露
        for _ in range(3):  # 一次只揭晓一个
            ethan = surprises.for_ethan(self.db)
            self.assertEqual(sum(p["new"] for p in ethan), 1)
            surprises.mark_announced(self.db, [p["key"] for p in ethan if p["new"]])
        self.assertFalse(any(p["new"] for p in surprises.for_ethan(self.db)))
        self.assertEqual(len(surprises.for_ethan(self.db)), 3)
        self.assertEqual(len([p for p in surprises.sync(self.db, []) if p["unlocked"]]), 3)  # 解锁了就不收回
        # 小伴 揭晓过的，工作台弹一次就不再弹
        self.assertEqual(len(surprises.reveals(self.db)), 3)
        from ethan import achievements
        achievements.mark_shown(self.db, ["play:" + p["key"] for p in surprises.reveals(self.db)])
        self.assertEqual(surprises.reveals(self.db), [])

    def test_first_sync_gives_only_one_for_old_achievements(self):
        old = [{"done": True}] * 5 + [{"tier": "银"}]  # 上线前就点亮的一堆
        got = [p for p in surprises.sync(self.db, old) if p["unlocked"]]
        self.assertEqual(len(got), 1)
        got = [p for p in surprises.sync(self.db, old + [{"done": True}]) if p["unlocked"]]
        self.assertEqual(len(got), 2)  # 之后拿一个解一个

    def test_wishlist_items_are_not_rewards(self):
        names = " ".join(p["name"] + p["how"] for p in surprises.PLAYS)
        for wish in ("搓个脸", "写首歌", "我们的故事", "戒指", "摄像头"):
            self.assertNotIn(wish, names)

    def test_prompt_reveals_new_play(self):
        from ethan.core.agent import build_system_prompt
        surprises.sync(self.db, [{"done": True}])
        ctx = {**self.store.load_context(today=MONDAY), "plays": surprises.for_ethan(self.db)}
        system = build_system_prompt("persona", ctx)
        self.assertIn(surprises.PLAYS[0]["name"], system)
        self.assertIn("揭晓", system)
        self.assertNotIn(surprises.PLAYS[-1]["name"], system)  # 没解锁的不进提示词

class CelebrateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_new_achievement_pops_once_and_hidden_ones_too(self):
        self.assertEqual(achievements.celebrate(self.db, self.root, MONDAY), [])  # 第一次：已有的不补弹
        h = quests.habit_by_title(self.db, "0 点前睡")
        for i in range(7):
            quests.check_habit(self.db, h["id"], MONDAY - timedelta(days=6 - i))
        got = self.wb.state(datetime.combine(MONDAY, datetime.min.time()).replace(hour=12))["celebrate"]
        hidden = [x for x in got if x["key"] == "hidden:sleep_week"]
        self.assertEqual(len(hidden), 1)
        self.assertTrue(hidden[0]["hidden"])
        self.assertTrue(self.wb.act("ach.seen", {"keys": [x["key"] for x in got]})["ok"])
        self.assertEqual(achievements.celebrate(self.db, self.root, MONDAY), [])  # 弹过就不再弹
        page = achievements.compute(self.db, self.root, MONDAY)
        self.assertIn("hidden:sleep_week", [i["key"] for i in page["items"]])  # 达成后成就页里也有了

if __name__ == "__main__":
    unittest.main()


class ShopNoticeTest(AchievementsTest):
    def test_affordable_reward_pops_once_and_again_after_redeem(self):
        """2026-10-29：小惊喜能拆了系统没提示。攒够了弹一次，拆过再攒够还会再弹。"""
        achievements.celebrate(self.db, self.root, MONDAY)  # 第一次把已有的记成弹过
        quests.award(self.db, 300, "测试", MONDAY)
        shop = [x for x in achievements.celebrate(self.db, self.root, MONDAY) if x.get("shop")]
        self.assertEqual(len(shop), 1)
        self.assertIn("小惊喜", shop[0]["name"])
        achievements.mark_shown(self.db, [shop[0]["key"]])
        self.assertFalse([x for x in achievements.celebrate(self.db, self.root, MONDAY) if x.get("shop")])
        rid = next(r["id"] for r in quests.rewards(self.db) if r["tier"] == "小")
        quests.redeem(self.db, rid, MONDAY)
        quests.award(self.db, 300, "测试", MONDAY)
        self.assertTrue([x for x in achievements.celebrate(self.db, self.root, MONDAY) if x.get("shop")])
