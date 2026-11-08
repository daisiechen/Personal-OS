import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

from ethan import quests, season1, territory
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import MONDAY, make_demo

WED = MONDAY + timedelta(days=2)
NOON_WED = datetime.combine(WED, datetime.min.time()).replace(hour=12)


class TerritoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        season1.apply(self.store)  # 七块领地的名字和新副本
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def check(self, title, *days):
        h = quests.habit_by_title(self.db, title)
        for d in days:
            quests.check_habit(self.db, h["id"], d)

    def rows(self, key, day=WED, active=True):
        return {r["name"]: r for r in territory.health(self.db, key, day, active)}

    def test_paced_by_days_so_far(self):
        # 今天不算欠账：周一一次没做也不红；也不说“达标”，算“还没到时候”（本周 0 次怎么是达标）
        self.assertEqual(self.rows("body", MONDAY)["训练"]["light"], "none")
        # 周四要求到周三（3 天）：副本 5 次 → 2.1 次；保底 4 次 → 1.7 次
        thu = MONDAY + timedelta(days=3)
        self.check("训练", MONDAY, MONDAY + timedelta(days=1), WED)
        self.assertEqual(self.rows("body", thu)["训练"]["light"], "ok")
        self.db.conn.execute("DELETE FROM habit_log")
        self.check("训练", MONDAY, thu)  # 今天做的也算进去
        self.assertEqual(self.rows("body", thu)["训练"]["light"], "warn")  # 2 次：副本落后、保底守住
        self.db.conn.execute("DELETE FROM habit_log")
        self.check("训练", WED)
        self.assertEqual(self.rows("body", thu)["训练"]["light"], "bad")
        self.assertEqual(self.rows("body")["每天出门"]["light"], "none")  # 还没接定位

    def test_weight_trend(self):
        for i in range(10):
            d = WED - timedelta(days=9 - i)
            self.db.conn.execute("INSERT INTO body_measures (day, weight, at) VALUES (?, ?, 'x')",
                                 (d.isoformat(), 60.0 - 0.07 * i))
        self.db.conn.commit()
        r = self.rows("body")["体重（7 天平均）"]
        self.assertEqual(r["light"], "ok")
        self.assertTrue(r["actual"].startswith("降"))

    def test_worst_ignores_grey(self):
        self.assertEqual(territory.worst(["none", "ok", "warn"]), "warn")
        self.assertEqual(territory.worst(["none", "none"]), "none")
        self.assertEqual(territory.worst(["ok", "bad"]), "bad")

    def test_guess(self):
        self.assertEqual(territory.guess("准备签证办理材料"), "life")
        self.assertEqual(territory.guess("还信用卡：5000"), "money")
        self.assertEqual(territory.guess("约年度体检"), "body")
        self.assertEqual(territory.guess("冷启动：财务（小伴 带你做）"), "money")
        self.assertIsNone(territory.guess("买袜子"))

    def test_map_and_territory_pages(self):
        m = self.wb.map_page(NOON_WED)
        by = {t["key"]: t for t in m["territories"]}
        self.assertEqual(list(by), [k for k, _, _ in territory.TERRITORIES])
        self.assertEqual(by["body"]["light"], "bad")
        self.assertIn("★ 减脂塑形", by["body"]["line"])
        self.assertEqual(by["money"]["light"], "none")
        self.assertEqual(by["study"]["line"], "英语陪练（Grok） 0 次")  # 没副本的领地显示亮着的那项
        page = self.wb.territory_page("body", NOON_WED)
        self.assertTrue(page["bosses"][0]["main"])
        self.assertIn("训练", [h["title"] for h in page["habits"]])
        self.assertIn("weights", page)
        with self.assertRaises(ValueError):
            self.wb.territory_page("nope")

    def test_goal_up_reorders_bosses(self):
        key = next(k for k, _, _ in territory.TERRITORIES if len(self.wb.territory_page(k, NOON_WED)["bosses"]) >= 2)
        before = [b["goal_id"] for b in self.wb.territory_page(key, NOON_WED)["bosses"]]
        r = self.wb.act("goal.up", {"id": before[1]}, now=NOON_WED)
        self.assertTrue(r["ok"])
        after = [b["goal_id"] for b in self.wb.territory_page(key, NOON_WED)["bosses"]]
        self.assertEqual(after[:2], [before[1], before[0]])
        order = [g for g in quests.main_quests(self.db) if g in before[:2]]  # 主线（排计划用）也跟着这个顺序
        if len(order) == 2:
            self.assertEqual(order, [before[1], before[0]])
        self.assertEqual(self.wb.act("goal.up", {"id": before[1]}, now=NOON_WED)["msg"], "已经在最前面了")
        self.assertFalse(self.wb.act("goal.up", {"id": 99999}, now=NOON_WED)["ok"])

    def test_waste_add_and_assign(self):
        r = self.wb.act("task.add", {"title": "买袜子", "when": "later"}, now=NOON_WED)
        visa = self.wb.act("task.add", {"title": "准备芬兰签证材料", "when": "later"}, now=NOON_WED)
        waste = {t["title"]: t for t in self.wb.territory_page("waste", NOON_WED)["tasks"]}
        self.assertIn("买袜子", waste)  # 猜不出就在荒原
        self.assertNotIn("准备芬兰签证材料", waste)  # 猜出来了直接归到生活体验
        self.assertIn("准备芬兰签证材料", [t["title"] for t in self.wb.territory_page("life", NOON_WED)["tasks"]])
        self.assertTrue(self.wb.act("task.domain", {"id": r["id"], "key": "money"}, now=NOON_WED)["ok"])
        self.assertNotIn("买袜子", [t["title"] for t in self.wb.territory_page("waste", NOON_WED)["tasks"]])
        self.assertFalse(self.wb.act("task.domain", {"id": visa["id"], "key": "mars"}, now=NOON_WED)["ok"])
        self.wb.act("task.add", {"title": "随便一件", "when": "later", "territory": "waste"}, now=NOON_WED)
        self.assertIn("随便一件", [t["title"] for t in self.wb.territory_page("waste", NOON_WED)["tasks"]])

    def test_explore_stats(self):
        (self.root / "explore-map.json").write_text(json.dumps({"sheets": {
            "亚洲": [{"国家": "日本", "状态": "已结束"}, {"国家": "日本", "状态": "已结束"}, {"国家": "韩国"}],
            "欧洲": [{"国家": "西班牙", "状态": "已结束"}], "中国": [{"城市": "安庆", "状态": "已结束"}],
            "Goal": [{"Goal": "三山五岳", "状态": "已结束"}, {"Goal": "草原骑射", "状态": "进行中"}]}}, ensure_ascii=False))
        ex = territory.explore_stats(self.root)
        self.assertEqual((ex["countries"], ex["china"], ex["goals_done"], ex["goals_doing"]), (2, 1, 1, 1))
        self.assertIn("点亮 2 国", {t["key"]: t for t in self.wb.map_page(NOON_WED)["territories"]}["life"]["line"])

class LifePageTest(unittest.TestCase):
    """看生活体验领域页：“出门做工作以外的事那一行……不要换行”“我实现的那个心愿是啥呀？我咋不知道？”
    “每周新体验的那个图去掉”“探索地图放在副本和资料中间”“本周的健康线跟你最上面的那个进度条那个模块是不是重复了呀？”"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import FakeCalendar, NOON, make_demo
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.wb = Workbench(self.store, Settings(Path(self.tmp.name) / "settings.json"), calendar=FakeCalendar())
        self.now = NOON

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_life_page_keeps_the_shared_skeleton(self):
        """七个领域详情页的排版和框架保持一致，没让把生活体验的“这个领域”和健康线合在一起。"""
        t = self.wb.territory_page("life", self.now)
        self.assertNotIn("merged", t)
        self.assertTrue(t["rows"])  # 健康线单独一块
        self.assertTrue(all("week" not in i for i in t["score"]["items"]))
        self.assertEqual([c["title"] for c in t["charts"]], ["每周出门天数"])  # 每周新体验的图去掉了
        self.assertIsNone(t["explore"])  # 没上传探索地图：给 None（页面写怎么加），不再显示 undefined

    def test_done_wish_shows_which_and_can_go_back(self):
        from ethan import attributes
        db = self.store.db
        wid = db.add_wish("一起去看一次北极光")
        db.close_wish(wid, "done")  # Telegram 待办清单“我们的心愿”那栏点了 ✓
        t = self.wb.territory_page("life", self.now)
        self.assertEqual([(w["id"], w["quote"]) for w in t["wishes_done"]], [(wid, "一起去看一次北极光")])
        item = next(i for i in t["score"]["items"] if i["name"] == "实现心愿")
        self.assertEqual(item["actual"], "90 天实现 1 个")  # 页面上的数和点开看到的是同一批
        r = self.wb.act("wish.reopen", {"id": wid}, self.now)
        self.assertTrue(r["ok"])
        self.assertEqual(self.wb.territory_page("life", self.now)["wishes_done"], [])
        self.assertEqual(db.open_wishes()[0]["id"], wid)
        done_at = r["undo"]["done_at"]
        self.assertTrue(self.wb.act("wish.redone", {"id": wid, "done_at": done_at}, self.now)["ok"])  # 撤销：照原来的日子记回
        self.assertEqual(db.conn.execute("SELECT done_at FROM wishes WHERE id = ?", (wid,)).fetchone()[0], done_at)
        self.assertFalse(self.wb.act("wish.reopen", {"id": 99999}, self.now)["ok"])


if __name__ == "__main__":
    unittest.main()
