import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import game_view, quests
from ethan.memory import MemoryStore

MON = date(2026, 11, 2)


class QuestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        quests.seed(self.db)
        self.goal = self.db.add_goal("创业拿到第一个订单", "year", status="active")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def all_dailies(self, day, skip=()):
        for h in quests.habits(self.db):
            if quests.due_today(h, day) and h["title"] not in skip:
                quests.check_habit(self.db, h["id"], day)

    def test_seed_is_idempotent_and_matches_confirmed_list(self):
        quests.seed(self.db)
        titles = [h["title"] for h in quests.habits(self.db)]
        self.assertEqual(len(titles), 7)
        self.assertEqual([r["cost"] for r in quests.rewards(self.db)], [300, 800, 2000])
        self.assertFalse(quests.due_today(quests.habit_by_title(self.db, "训练"), date(2026, 11, 1)))  # 周日不练

    def test_hard_step_and_undo_task(self):
        tid = quests.add_breakdown(self.db, self.goal, [{"title": "第一次给客户发消息", "size": "S"}])[0]
        self.db.conn.execute("UPDATE tasks SET hard = 1 WHERE id = ?", (tid,))
        self.assertEqual(quests.complete_task(self.db, tid, MON), 60)  # 小 40 × 1.5
        self.assertEqual(quests.uncomplete_task(self.db, tid), 60)
        self.assertEqual(quests.totals(self.db)["earned"], 0)
        self.assertEqual(quests.complete_task(self.db, tid, MON), 60)  # 撤销后能再完成

    def test_easy_version_half_points_and_undo_takes_back_full_day(self):
        h = quests.habit_by_title(self.db, "睡前拉伸")
        self.all_dailies(MON, skip=("睡前拉伸 5 分钟",))
        before = quests.totals(self.db)["earned"]
        self.assertEqual(quests.check_habit(self.db, h["id"], MON, easy=True), 5 + 20)  # 一半分 + 满勤
        self.assertTrue(quests.full_day(self.db, MON))
        self.assertEqual(quests.uncheck_habit(self.db, h["id"], MON), 25)  # 满勤奖励也收回
        self.assertFalse(quests.full_day(self.db, MON))
        self.assertEqual(quests.totals(self.db)["earned"], before)
        self.assertEqual(quests.uncheck_habit(self.db, h["id"], MON), 0)

    def test_rest_day_needs_two_and_once_a_week(self):
        self.assertEqual(quests.take_rest_day(self.db, MON)[0], True)
        self.assertFalse(quests.take_rest_day(self.db, MON + timedelta(days=1))[0])  # 一周一次
        for title in ("训练", "0 点前睡"):
            quests.check_habit(self.db, quests.habit_by_title(self.db, title)["id"], MON)
        self.assertTrue(quests.full_day(self.db, MON))
        self.assertTrue(quests.take_rest_day(self.db, MON + timedelta(days=7))[0])  # 下周又有

    def test_levels_have_no_cap(self):
        quests.award(self.db, 30000, "测试", MON)
        t = quests.totals(self.db)
        self.assertGreater(t["level"], 10)
        self.assertGreater(t["next_level_at"], 30000)
        self.assertEqual(t["title"], quests.TITLES[-1])

    def test_main_and_side_points_once_and_ethans_none(self):
        ids = quests.add_breakdown(self.db, self.goal, [{"title": "列名单", "size": "S"},
                                                        {"title": "签第一单", "size": "L", "milestone": True}])
        self.assertEqual(quests.complete_task(self.db, ids[0], MON), 40)
        self.assertEqual(quests.complete_task(self.db, ids[0], MON), 0)  # 不重复给分
        self.assertEqual(quests.complete_task(self.db, ids[1], MON), 500)  # 大 200 + 阶段节点 300
        side = self.db.add_task("买牛奶")
        self.assertEqual(quests.complete_task(self.db, side, MON), 20)
        his = self.db.add_task("帮她查 physio", promised_by_ethan=True)
        self.assertEqual(quests.complete_task(self.db, his, MON), 0)
        self.assertEqual(quests.totals(self.db)["balance"], 560)

    def test_drafts_and_approval(self):
        quests.add_breakdown(self.db, self.goal, [{"title": "a", "size": "M"}])
        self.assertTrue(quests.main_quests(self.db)[self.goal][0]["draft"])
        self.assertEqual(quests.approve_breakdown(self.db, self.goal), 1)
        self.assertFalse(quests.main_quests(self.db)[self.goal][0]["draft"])

    def test_full_day_bonus_streak_freeze_and_milestones(self):
        for i in range(7):
            self.all_dailies(MON + timedelta(days=i), skip=("0 点前睡",) if i == 2 else ())
        s = quests.streak(self.db, MON + timedelta(days=6))
        self.assertEqual((s["days"], s["freezes_used"]), (6, 1))  # 周三漏了一项，用补签卡，不断
        for i in range(7, 9):
            self.all_dailies(MON + timedelta(days=i))
        self.assertEqual(quests.streak(self.db, MON + timedelta(days=8))["days"], 8)
        reasons = [r["reason"] for r in self.db.conn.execute("SELECT reason FROM points WHERE delta > 0")]
        self.assertIn("连续满勤 7 天", reasons)
        self.assertEqual(reasons.count("日常满勤"), 8)
        self.assertEqual(quests.check_habit(self.db, 1, MON), 0)  # 同一天不重复

    def test_weekly_habit_and_streak_breaks_without_freeze(self):
        weekly = quests.habit_by_title(self.db, "每周 1 次新体验")
        self.assertEqual(quests.check_habit(self.db, weekly["id"], MON), 50)
        self.assertEqual(quests.check_habit(self.db, weekly["id"], MON + timedelta(days=1)), quests.SIDE_POINTS)  # 超额每次 +20
        self.assertEqual(quests.uncheck_habit(self.db, weekly["id"], MON + timedelta(days=1)), quests.SIDE_POINTS)
        self.all_dailies(MON)
        self.all_dailies(MON + timedelta(days=3))  # 中间漏两天，一周只有一张补签卡
        self.assertEqual(quests.streak(self.db, MON + timedelta(days=3))["days"], 1)

    def test_redeem_needs_balance_and_delivery(self):
        small = quests.rewards(self.db)[0]
        ok, msg = quests.redeem(self.db, small["id"], MON)
        self.assertFalse(ok)
        self.assertIn("还差 300 分", msg)
        quests.award(self.db, 320, "测试", MON)
        ok, _ = quests.redeem(self.db, small["id"], MON)
        self.assertTrue(ok)
        self.assertEqual(quests.totals(self.db)["balance"], 20)
        self.assertEqual(quests.totals(self.db)["earned"], 320)  # 兑换不减经验
        pending = quests.pending_redemptions(self.db)
        self.assertEqual(len(pending), 1)
        self.assertTrue(quests.deliver(self.db, pending[0]["id"], "写了一封信"))
        self.assertFalse(quests.pending_redemptions(self.db))

    def test_levels(self):
        self.assertEqual(quests.totals(self.db)["level"], 1)
        quests.award(self.db, 100, "x", MON)
        self.assertEqual((quests.totals(self.db)["level"], quests.totals(self.db)["title"]), (2, "起步的人"))
        quests.award(self.db, 200, "x", MON)
        self.assertEqual(quests.totals(self.db)["level"], 3)

    def test_habit_add_drop(self):
        quests.add_habit(self.db, "每天喝 2 升水")
        self.assertTrue(quests.habit_by_title(self.db, "每天喝 2 升水"))
        self.assertTrue(quests.drop_habit(self.db, "每天喝 2 升水"))
        self.assertIsNone(quests.habit_by_title(self.db, "每天喝 2 升水"))


class GameViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        quests.seed(self.db)
        goal = self.db.add_goal("创业拿到第一个订单", "year", status="active")
        self.ids = quests.add_breakdown(self.db, goal, [{"title": "列出 30 个潜在客户", "size": "M"},
                                                        {"title": "联系 20 个", "size": "L", "milestone": True}])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_fixed_buttons(self):
        self.assertEqual(game_view.which("☀️ 今天"), "quests")
        self.assertEqual(game_view.which("🎯 任务"), "quests")  # 旧按钮还能用
        self.assertEqual(game_view.which("商店"), "shop")

    def test_quests_page_check_and_complete(self):
        text, kb = game_view.render(self.db, "quests", MON)
        self.assertIn("· 积分 0 ·", text)
        toast, view = game_view.handle(self.db, "qs:h:1", MON)
        self.assertEqual((toast, view), ("打卡成功 +10 分", "quests"))
        toast, _ = game_view.handle(self.db, f"qs:t:{self.ids[0]}", MON)
        self.assertEqual(toast, "完成！+100 分")
        text, _ = game_view.render(self.db, "quests", MON)
        self.assertIn("· 积分 110 ·", text)

    def test_shop_needs_two_taps(self):
        quests.award(self.db, 400, "x", MON)
        text, kb = game_view.render(self.db, "shop", MON)
        self.assertIn("积分 400", text)
        rid = quests.rewards(self.db)[0]["id"]
        toast, view = game_view.handle(self.db, f"qs:r:{rid}", MON)
        self.assertEqual(quests.totals(self.db)["balance"], 400)  # 第一次只是选
        confirm, kb = game_view.render(self.db, view, MON)
        self.assertIn("确认用 300 分兑换", confirm)
        toast, view = game_view.handle(self.db, f"qs:rc:{rid}", MON)
        self.assertEqual(quests.totals(self.db)["balance"], 100)
        self.assertIn("正在策划：小惊喜", game_view.render(self.db, view, MON)[0])

    def test_progress_page(self):
        quests.award(self.db, 150, "测试得分", MON)
        text, _ = game_view.render(self.db, "progress", MON)
        self.assertIn("Lv2 起步的人", text)
        self.assertIn("+150 测试得分", text)


if __name__ == "__main__":
    unittest.main()
