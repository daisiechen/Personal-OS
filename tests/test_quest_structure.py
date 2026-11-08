"""任务结构改版（docs/design/quest-structure.md）。"""

import importlib
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from ethan import planner, quests, stages
from ethan.memory import MemoryStore

TODAY = date(2026, 11, 2)
GOALS = [(20, "减脂塑形", "身体", "active", None), (22, "年底前去 3 个新国家", "生活体验", "active", None),
         (24, "DemoApp 上线", "事业", "active", "agent"), (25, "个人管理系统上线", "事业", "active", "agent"),
         (26, "个人品牌：主页和社媒", "事业", "active", "agent"), (23, "拿下第一个订单", "事业", "paused", "agent")]
# 服务器上 11-01 的真实编号和标题（服务器会话只读查的）
TASKS = [(7, 20, "连续一周训练前称体重", 0), (8, 20, "测体脂基线", 0), (10, 20, "按下周采购清单去买菜", 0),
         (12, 20, "做 physio 体态评估，把圆肩纠正加进训练", 0), (13, 20, "连续 4 周每周练满 5 次", 0),
         (14, 20, "11 月底中期复盘，调热量和训练量", 0), (15, 20, "12 月中旬复测体脂", 0), (16, 20, "年底冲刺，12/31 称重测体脂收官", 0),
         (125, 22, "10/29 11:30 去签证中心办签证", 1), (126, 22, "回执拍照发给 小伴", 1), (185, 22, "拿到芬兰签证（等结果）", 0),
         (127, 22, "定芬兰 12 月行程", 0), (128, 22, "定另外两个国家和日期", 0), (130, 22, "出签后核对有效期和入境次数", 0),
         (131, 22, "订芬兰机票和住宿", 0), (132, 22, "买旅行保险", 1), (133, 22, "补齐冬季保暖防滑装备", 0),
         (134, 22, "第 1 站：芬兰", 0), (135, 22, "订第 2 站机票住宿", 0), (136, 22, "第 2 站出发", 0), (137, 22, "订第 3 站机票住宿", 0),
         (138, 22, "第 3 站出发，集齐 3 个国家", 0), (139, 22, "年底出行复盘：最爱哪里、明年去哪", 0),
         (39, 24, "定首发功能，砍掉能延后的", 1), (40, 24, "确认上线检查清单", 1), (50, 25, "审定指标", 1), (52, 25, "连用一周", 1),
         (53, 25, "列出问题", 1), (54, 25, "修掉问题", 1), (106, 26, "审定 example.com 内容大纲", 1), (107, 26, "example.com 上线", 1)]


class QuestStructureTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        db = self.db = self.store.db
        quests.seed(db)
        for gid, title, dom, st, by in GOALS:
            db.conn.execute("INSERT INTO goals (id, level, title, domain, status, handled_by, due, updated_at) "
                            "VALUES (?, 'year', ?, ?, ?, ?, '2026-12-31', 'x')", (gid, title, dom, st, by))
        for tid, gid, title, done in TASKS:
            db.conn.execute("INSERT INTO tasks (id, title, goal_id, quest, done, size, seq, created_at) VALUES (?, ?, ?, 'main', ?, 'M', ?, 'x')",
                            (tid, title, gid, done, tid))
        db.conn.execute("INSERT INTO tasks (id, title, goal_id, created_at) VALUES (6, '冷启动：财务（小伴 带你做）', 25, 'x')")
        for title in ("训练前称体重", "吃够蛋白质", "0 点前睡"):
            db.conn.execute("INSERT OR IGNORE INTO habits (title, cadence, target, points) VALUES (?, 'daily', 1, 10)", (title,))
        db.conn.execute("INSERT OR IGNORE INTO habits (title, cadence, target, points) VALUES ('训练', 'weekly', 5, 50)")
        db.conn.commit()
        self.m = importlib.import_module("migrations.20261101_quest_structure_s1")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_migration_builds_milestones_and_keeps_records(self):
        note = self.m.run(self.store)
        self.assertIn("减脂塑形：4 个里程碑", note)
        self.assertNotIn("对不上", note)
        ms = stages.milestones(self.db, 20)
        self.assertEqual([m["target"] for m in ms], [56.2, 55.4, 54.6, 56.5])
        m1 = {t["title"]: t for t in stages.tasks_of(self.db, ms[0]["id"])}
        self.assertEqual(m1["测体脂"]["cadence"], "per_milestone")
        self.assertEqual((m1["按清单买菜"]["cadence"], m1["按清单买菜"]["per_week"]), ("weekly", 1))
        self.assertTrue(m1["训练 5 次 / 周"]["habit_id"])
        self.assertTrue(m1["M1 复盘"]["is_review"])
        trip = stages.tasks_of(self.db, stages.milestones(self.db, 22)[0]["id"])
        self.assertEqual(next(t for t in trip if t["id"] == 131)["depends_on"], 127)  # 订机票要等定行程
        self.assertTrue(next(t for t in trip if t["id"] == 125)["done"])  # 做完的记录保留
        fancy = stages.milestones(self.db, 24)
        self.assertEqual((fancy[0]["status"], fancy[1]["status"]), ("done", "open"))
        self.assertFalse(stages.has_structure(self.db, 23))  # 暂停的不拆
        self.assertIn("没动", self.m.run(self.store))  # 再跑一次不重复

    def test_daily_todos_come_from_current_milestone(self):
        self.m.run(self.store)
        rest = planner.rest_items(self.db, [], TODAY)
        today = {r["title"] for r in rest if r.get("how") == "下一步"}
        self.assertIn("测体脂", today)
        self.assertIn("按清单买菜", today)
        self.assertNotIn("拿到芬兰签证（等结果）", {r["title"] for r in rest})  # 等别人结果的只在副本详情里列（第 2 条）
        self.assertNotIn("定芬兰 12 月行程", today)  # 前置还没完成
        self.assertNotIn("训练 5 次 / 周", {r["title"] for r in rest})  # 引用日常的在日常里打卡
        self.assertNotIn("中期复盘：调热量和训练量", {r["title"] for r in rest})  # 以后的里程碑不排
        # 交给 agent 的副本：里程碑里要她本人做的照样排；暂停的“拿下第一个订单”不排
        self.assertIn("找 5 个内测用户", today)
        self.assertIn("审内测反馈，定哪些上线前必须修", today)

    def test_checking_repeating_and_review_advances(self):
        self.m.run(self.store)
        ms = stages.milestones(self.db, 20)
        buy = next(t for t in stages.tasks_of(self.db, ms[0]["id"]) if t["title"] == "按清单买菜")
        self.assertEqual(stages.check(self.db, buy["id"], TODAY), 10)
        self.assertEqual(stages.check(self.db, buy["id"], TODAY), 0)  # 一天只记一次
        self.assertFalse(stages.due_today(self.db, stages.tasks_of(self.db, ms[0]["id"])[2], TODAY + timedelta(days=1)))  # 这周够了
        self.assertEqual(stages.uncheck(self.db, buy["id"], TODAY), 10)
        review = next(t for t in stages.tasks_of(self.db, ms[0]["id"]) if t["is_review"])
        gain = stages.check(self.db, review["id"], TODAY)
        self.assertEqual(gain, quests.SIZE_POINTS["S"] + stages.REVIEWED_POINTS)  # 体重没到，复盘过了 +50
        self.assertEqual(stages.current(self.db, 20)["title"], "M2 稳住节奏")
        m2 = {t["title"] for t in stages.tasks_of(self.db, ms[1]["id"])}
        self.assertTrue({"测体脂", "按清单买菜", "训练 5 次 / 周", "中期复盘：调热量和训练量"} <= m2)  # 反复做的带过来了
        self.assertNotIn("做 physio 体态评估，把圆肩纠正加进训练", m2)
        # 她不小心勾了复盘：撤销能退回去（M1 重新打开、分收回、带过去的反复任务删掉，M2 自己的复盘留着）
        self.assertEqual(stages.uncheck(self.db, review["id"], TODAY), gain)
        self.assertEqual(stages.current(self.db, 20)["title"], "M1 打好基础")
        self.assertEqual({t["title"] for t in stages.tasks_of(self.db, ms[1]["id"])}, {"中期复盘：调热量和训练量"})

    def test_weekly_progress_counts_weeks_in_phase_and_tree_order(self):
        self.m.run(self.store)
        self.db.conn.execute("UPDATE milestones SET created_at = '2026-11-01T12:00:00+00:00'")  # 里程碑是 11-01 建的
        m1 = stages.milestones(self.db, 20)[0]
        buy = next(t for t in stages.tasks_of(self.db, m1["id"]) if t["title"] == "按清单买菜")
        self.assertIn("这阶段 0 / 3 周", stages.progress(self.db, buy, TODAY)["text"])  # 11-01 到 11-15 跨三个自然周
        trip = stages.view(self.db, 22, TODAY)[0]["tasks"]
        depth = {t["id"]: t["depth"] for t in trip}
        self.assertEqual((depth[185], depth[127], depth[131]), (0, 1, 2))  # 拿签证 → 定行程 → 订机票，一级级挂
        with self.assertRaises(ValueError):
            stages.set_task(self.db, 185, depends_on=131)  # 绕回自己

    def test_m1_review_undo_migration(self):
        self.m.run(self.store)
        m1 = stages.milestones(self.db, 20)[0]
        review = next(t for t in stages.tasks_of(self.db, m1["id"]) if t["is_review"])
        stages.check(self.db, review["id"], date(2026, 11, 1))
        self.db.conn.execute("INSERT INTO rest_days (day, at) VALUES ('2026-11-01', 'x')")
        note = importlib.import_module("migrations.20261101_undo_m1_review_and_rest").run(self.store)
        self.assertIn("M1 重新打开", note)
        self.assertEqual(stages.current(self.db, 20)["id"], m1["id"])
        self.assertFalse(quests.is_rest_day(self.db, date(2026, 11, 1)))


    def test_fix_migration_reopens_m1_closed_without_review(self):
        """服务器上的样子：M1 被关成没达成、加了 50 分，复盘却没勾（勾了又撤销）。"""
        self.m.run(self.store)
        m1 = stages.milestones(self.db, 20)[0]
        review = next(t for t in stages.tasks_of(self.db, m1["id"]) if t["is_review"])
        stages.check(self.db, review["id"], TODAY)
        self.db.conn.execute("UPDATE tasks SET done = 0 WHERE id = ?", (review["id"],))
        self.db.conn.execute("DELETE FROM points WHERE ref = ?", (f"task:{review['id']}",))
        self.db.conn.execute("INSERT INTO tasks (id, title, created_at) VALUES (149, '11/12 前没出签就催问', 'x')")
        note = importlib.import_module("migrations.20261101_fix_m1_and_others").run(self.store)
        self.assertIn("收回 50 分", note)
        self.assertIn("#149", note)
        self.assertEqual(stages.current(self.db, 20)["id"], m1["id"])

    def test_agent_milestone_without_review_closes_when_all_done(self):
        self.m.run(self.store)
        m3 = stages.milestones(self.db, 24)[2]
        t = stages.tasks_of(self.db, m3["id"])[0]
        self.db.conn.execute("UPDATE milestones SET status = 'done' WHERE id = ?", (stages.milestones(self.db, 24)[1]["id"],))
        self.assertGreaterEqual(stages.check(self.db, t["id"], TODAY), stages.REACHED_POINTS)
        self.assertEqual(stages.current(self.db, 24)["title"], "M4 正式上线，找到长期在英国运营的合伙人")


    def test_lbs_farewell_becomes_event(self):
        self.db.conn.execute("INSERT INTO tasks (id, title, due, created_at) VALUES (155, '11/12 11:00 某商学院 交换生送别活动（Garden Room）', '2026-11-12', 'x')")
        note = importlib.import_module("migrations.20261101_lbs_farewell_to_calendar").run(self.store)
        self.assertIn("11:00-13:00", note)
        from ethan import events
        self.assertEqual(events.for_day(self.db, date(2026, 11, 12))[0][2], "某商学院 交换生送别活动")
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM tasks WHERE id = 155").fetchone())

    def test_later_milestone_previews_carried_tasks(self):
        self.m.run(self.store)
        m3 = next(m for m in stages.view(self.db, 20, TODAY) if m["title"] == "M3 加码")
        titles = {t["title"]: t for t in m3["tasks"]}
        self.assertTrue(titles["按清单买菜"]["carry"])
        self.assertIn("训练 5 次 / 周", titles)


    def test_prep_cleanup_migration(self):
        self.m.run(self.store)
        db = self.db
        db.conn.execute("UPDATE tasks SET title = '每周挑一个出门小计划', promised_by_ethan = 1 WHERE id = 1") if db.conn.execute("SELECT 1 FROM tasks WHERE id = 1").fetchone() else \
            db.conn.execute("INSERT INTO tasks (id, title, promised_by_ethan, created_at) VALUES (1, '每周挑一个出门小计划', 1, 'x')")
        db.conn.execute("INSERT OR IGNORE INTO habits (title, cadence, target, points) VALUES ('每周 1 次新体验', 'weekly', 1, 50)")
        for tid, title in ((77, "挑 K-pop、古典舞、普拉提体验课各一节"), (140, "做芬兰 12 月行程方案"), (121, "每周找好 2–3 篇研究相关论文")):
            db.conn.execute("INSERT INTO tasks (id, title, promised_by_ethan, created_at) VALUES (?, ?, 1, 'x')", (tid, title))
        note = importlib.import_module("migrations.20261102_prep_cleanup").run(self.store)
        self.assertIn("#77 体验课删掉", note)
        self.assertIn("#140 挂到 #127", note)
        self.assertIsNone(db.conn.execute("SELECT 1 FROM tasks WHERE id = 77").fetchone())
        self.assertEqual(db.conn.execute("SELECT depends_on FROM tasks WHERE id = 140").fetchone()[0], 127)


    def test_review_only_due_near_milestone_end(self):
        """今天怎么这么多 M1 复盘——复盘只在阶段最后两天排，没截止日的要等其他一次性任务做完。"""
        self.m.run(self.store)
        rest = {r["title"] for r in planner.rest_items(self.db, [], TODAY) if r.get("how") == "下一步"}
        self.assertNotIn("M1 复盘", rest)
        m1 = stages.milestones(self.db, 20)[0]
        review = next(t for t in stages.tasks_of(self.db, m1["id"]) if t["is_review"])
        self.assertTrue(stages.due_today(self.db, review, date(2026, 11, 13)))


    def test_goal_scope_migration(self):
        self.m.run(self.store)
        self.db.conn.execute("INSERT INTO tasks (id, title, due, created_at) VALUES (208, '完成工作台改版', '2026-11-02', 'x')")
        self.db.conn.execute("INSERT INTO tasks (id, title, goal_id, created_at) VALUES (211, '5 天内回复芬兰使馆补材料', 22, 'x')")
        note = importlib.import_module("migrations.20261102_goal_scope_and_tasks").run(self.store)
        self.assertIn("#208 挂进 M2", note)
        self.assertIn("#211 挂进 M1", note)
        g = next(x for x in self.db.goal_chain() if x["id"] == 25)
        self.assertIn("工作台", g["scope"])
        from ethan.core.agent import chain_text
        self.assertIn("管：", chain_text([{**g, "depth": 0}]))


    def test_start_day_keeps_task_out_until_then(self):
        """算 11-12 的待办，不算今天的。"""
        self.m.run(self.store)
        m1 = stages.milestones(self.db, 22)[0]
        tid = stages.add_task(self.db, m1["id"], "11/12 前没出签就催问")
        self.db.conn.execute("UPDATE tasks SET start_day = '2026-11-12' WHERE id = ?", (tid,))
        t = dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
        self.assertFalse(stages.due_today(self.db, t, TODAY))
        self.assertTrue(stages.due_today(self.db, t, date(2026, 11, 12)))


    def test_old_cold_start_rotation_off_once_in_milestones(self):
        """小伴 为什么来找我做财务冷启动——冷启动挪进管理系统 M2 以后，老的每日轮换不再塞、不再让他来约。"""
        from ethan import domains
        self.assertIsNotNone(domains.todays_cold_start(self.db, TODAY, date(2026, 10, 28)))  # 还没拆结构时照旧
        self.m.run(self.store)
        self.assertIsNone(domains.todays_cold_start(self.db, TODAY, date(2026, 10, 28)))

    def test_draft_written_when_review_due_and_kept_on_close(self):
        """里程碑复盘由 小伴 先起草（数据小结和调整建议），她看完再勾。"""
        from ethan import msreview
        from ethan.core.backends import Reply
        self.m.run(self.store)
        m1 = stages.milestones(self.db, 20)[0]
        self.assertEqual(msreview.to_write(self.db, TODAY), [])  # 复盘还没排进待办，不起草
        day = date.fromisoformat(m1["due"]) - timedelta(days=1)

        class Router:
            def complete(self, system, messages, purpose="chat"):
                assert "测体脂" in system
                return Reply('好的：{"summary": "体脂测了，买菜两周都做到了。", "suggest": ["M2 训练前称体重改成每周 3 次"]}', "x", "m", 0)
        wrote = msreview.write_due(self.db, Router(), day)
        self.assertIn(m1["title"], wrote)
        v = next(x for x in stages.view(self.db, 20, day) if x["id"] == m1["id"])
        self.assertEqual(v["draft"]["summary"], "体脂测了，买菜两周都做到了。")
        self.assertEqual(v["draft"]["suggest"], ["M2 训练前称体重改成每周 3 次"])
        self.assertEqual(msreview.to_write(self.db, day), [])  # 写过一次不再写
        self.assertTrue(msreview.waiting(self.db))
        rev = next(t for t in stages.tasks_of(self.db, m1["id"]) if t["is_review"])
        stages.check(self.db, rev["id"], day)
        closed = next(x for x in stages.milestones(self.db, 20) if x["id"] == m1["id"])
        self.assertEqual(closed["review"], "体脂测了，买菜两周都做到了。")
        self.assertFalse(any("M1" in w for w in msreview.waiting(self.db)))

    def test_model_down_falls_back_to_numbers(self):
        from ethan import msreview
        self.m.run(self.store)
        m1 = stages.milestones(self.db, 20)[0]
        d = msreview.write(self.db, None, m1, TODAY)
        self.assertIn("这一阶段做完", d["summary"])
        self.assertEqual(d["by"], "")


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
