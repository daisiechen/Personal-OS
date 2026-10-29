import tempfile
import unittest
from pathlib import Path

from ethan import domains, planner, quests, season1
from tests.test_workbench import MONDAY, make_demo


class Season1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = make_demo(Path(self.tmp.name))
        self.db = self.store.db
        # 试用时留下的东西：过时的事、被推迟的主线、一次兑换
        self.db.add_task("周三提醒她查周四看日落的地点和天气", promised_by_ethan=True)
        self.db.add_task("确认并预约一节网球初学者体验课（付款前先问她）")
        self.db.conn.execute("UPDATE tasks SET due = '2026-11-03', hide_day = '2026-11-02' WHERE quest = 'main'")
        self.db.conn.execute("INSERT INTO habits (title, cadence, target, points, sort) VALUES (?, 'daily', 1, 10, 9)",
                             (season1.ENGLISH_OLD,))
        self.db.conn.execute("INSERT INTO redemptions (reward_id, cost, status, at) VALUES (1, 300, '待策划', 'x')")
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def goal(self, title):
        return self.db.conn.execute("SELECT * FROM goals WHERE title = ?", (title,)).fetchone()

    def test_preview_changes_nothing(self):
        before = self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        lines = [d for d, _ in season1.plan(self.db)]
        self.assertTrue(any("领地改名：健康 → 身体" in d for d in lines))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], before)
        self.assertIsNone(self.goal(season1.SELF_MEDIA))

    def test_apply_reshapes_territories_and_is_idempotent(self):
        done = season1.apply(self.store)
        self.assertTrue(done)
        domains_now = {r[0] for r in self.db.conn.execute("SELECT DISTINCT domain FROM goals WHERE domain IS NOT NULL")}
        self.assertTrue(domains_now <= set(season1.TERRITORIES), domains_now)
        self.assertEqual(self.goal("创业拿到第一个订单")["status"], "paused")
        self.assertEqual(self.goal("年底前去 3 个新地方")["status"], "active")
        self.assertEqual(self.goal("个人管理系统上线")["domain"], "生活体验")
        media = self.goal(season1.SELF_MEDIA)
        self.assertEqual((media["domain"], media["status"]), ("事业", "active"))
        self.assertEqual(len(quests.main_quests(self.db)[media["id"]]), 5)
        self.assertIsNotNone(self.goal(season1.ACADEMIC))
        self.assertFalse(self.db.conn.execute("SELECT 1 FROM tasks WHERE title LIKE '%看日落%' OR title LIKE '%网球初学者%'")
                         .fetchone())
        self.assertFalse(self.db.conn.execute(
            "SELECT 1 FROM tasks WHERE quest = 'main' AND (due IS NOT NULL OR hide_day IS NOT NULL)").fetchone())
        eng = self.db.conn.execute("SELECT * FROM habits WHERE title = ?", (season1.ENGLISH_NEW,)).fetchone()
        self.assertEqual((eng["cadence"], eng["target"]), ("weekly", 5))
        self.assertEqual(self.db.conn.execute("SELECT active FROM habits WHERE title = '每天出门'").fetchone()[0], 0)
        self.assertEqual(quests.totals(self.db)["balance"], -300)  # 没让撤销兑换就不动
        self.assertEqual(season1.plan(self.db), [])  # 再跑一次没有要改的

    def test_cancel_test_redemption_only_when_asked(self):
        season1.apply(self.store, cancel_test_redemption=True)
        self.assertEqual(quests.totals(self.db)["balance"], 0)

    def test_fresh_start_clears_trial_records(self):
        from ethan.core.router import Settings
        settings = Settings(Path(self.tmp.name) / "settings.json")
        quests.award(self.db, 70, "试用", MONDAY)
        season1.apply(self.store, fresh_start="2026-11-03", settings=settings)
        self.assertEqual(quests.totals(self.db)["earned"], 0)
        self.assertEqual(quests.totals(self.db)["balance"], 0)
        self.assertEqual(settings.get("cold_start_from"), "2026-11-03")
        self.assertEqual(season1.plan(self.db, fresh_start="2026-11-03", settings=settings), [])

    def test_paused_goal_not_planned_today_and_domain_lines(self):
        season1.apply(self.store)
        paused = self.goal("创业拿到第一个订单")["id"]
        rest = planner.rest_items(self.db, [])
        self.assertFalse([i for i in rest if i.get("goal_id") == paused and i.get("how") == "下一步"])
        lines = domains.domain_lines(self.db, MONDAY)
        self.assertEqual([l.split("：")[0] for l in lines], [{"身体": "健康"}.get(t, t) for t in season1.TERRITORIES])  # 给她看的叫健康
        self.assertTrue(any(l.startswith("健康：减脂塑形") for l in lines))


if __name__ == "__main__":
    unittest.main()
