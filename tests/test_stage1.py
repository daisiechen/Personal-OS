import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ethan import goals2026, quests
from ethan.core.agent import NO_EXCUSES, Ethan, build_system_prompt, parse_reply
from ethan.core.backends import Reply
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore
from ethan.planner import plan_day
from ethan.vault import board_pages
from ethan.proactive import BRIEF_SLOT, brief_occasion, due_brief, planned_time, tick
from tests.test_planner import FakeTravel, Router as ScriptedRouter, good_plan
from tests.test_progress import seed_parents

DAY = date(2026, 11, 2)  # 周一，伦敦冬令时，UTC = 伦敦


class Backend:
    name = "subscription"

    def __init__(self, text):
        self.text, self.systems = text, []

    def complete(self, system, messages, images=()):
        self.systems.append(system)
        return Reply(self.text, self.name, "m", 0.0)


class Stage1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        seed_parents(self.db)
        goals2026.apply(self.store)
        self.ids = {r["title"]: r["id"] for r in self.db.conn.execute("SELECT id, title FROM goals")}
        plan_day(self.db, ScriptedRouter(lambda: good_plan(self.db, ethan=["想 3 个指标给她审批"])), DAY,
                 travel=FakeTravel(), cold_start_from=DAY)
        self.plan = self.db.latest_plan(DAY.isoformat())
        self.settings = Settings(root / "settings.json")
        self.root = root

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ethan(self, text):
        self.backend = Backend(text)
        return Ethan(self.store, Router(self.backend, Ledger(self.root / "ledger"), self.settings), persona="人设")

    def item(self, title_start):
        return [i for i in self.plan["items"] if i["title"].startswith(title_start)][0]

    def test_ethan_sees_concrete_plan(self):
        self.ethan("好").reply("telegram", "今天练什么", today=DAY)
        system = self.backend.systems[0]
        self.assertIn("路上约 23 分钟（139 路公交），15:15 前出发，14:15 提醒", system)
        self.assertIn("[支线] 冷启动：财务", system)
        self.assertIn("[主线·100分·待她过目·下一步] 列出 30 个潜在客户", system)
        self.assertIn("[你替她准备] 想 3 个指标给她审批", system)
        self.assertIn("Lv1「新手冒险家」，积分 0", system)
        self.assertIn("训练○", system)
        self.assertIn("1. 臀推机：3 组 × 8–12 次", system)  # 训练具体到动作
        self.assertIn("做法：", system)  # 三餐具体到菜谱
        self.assertIn("应急金目标", system)  # 冷启动怎么做
        self.assertIn(NO_EXCUSES, system)

    def test_records_results_weight_training_and_cold_start(self):
        todo = self.item("列出 30 个潜在客户")["id"]
        deal, exp = self.ids["创业拿到第一个订单"], self.ids["每周至少 1 次新体验"]
        text = (f"厉害！\n[结果 #{todo} 完成 发了 5 封] [体重 56.4] [记数 #{exp} 去了 V&A] "
                f"[漏斗 #{deal} 联系 Acme] [训练 臀推机 40x12/12/12 疼痛0] [建档 财务 完成 每月随意花 300 镑]")
        reply = self.ethan(text).reply("telegram", "都做完了", today=DAY)
        self.assertEqual(reply["bubbles"][0], "厉害！")
        # 2026-10-29 她定：她的任务和日常都由她自己勾；训练和称体重只记数，不打卡、不加分（只有系统接入的数据才自动打卡）
        self.assertEqual(reply["bubbles"][1:], [])
        self.assertFalse(quests.checked(self.db, quests.habit_by_title(self.db, "训练")["id"], DAY))
        items = {i["id"]: i for i in self.db.latest_plan(DAY.isoformat())["items"]}
        task = self.db.conn.execute("SELECT done FROM tasks WHERE id = ?", (items[todo]["task_id"],)).fetchone()
        self.assertEqual(task["done"], 0)  # 没替她勾
        self.assertEqual([m["weight"] for m in self.db.body_measures(DAY.isoformat())], [56.4])
        self.assertEqual(len(self.db.metric_entries(exp)), 1)
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM workout_sets").fetchone()[0], 3)
        row = self.db.conn.execute("SELECT * FROM module_status WHERE module = '财务'").fetchone()
        self.assertEqual((row["cold_start"], row["note"]), ("已完成", "每月随意花 300 镑"))

    def test_bad_marks_are_not_guessed_and_she_is_told(self):
        deal, body = self.ids["创业拿到第一个订单"], self.ids["减脂塑形：年底体脂 27%–28%、体重约 56.5 kg"]
        text = (f"好\n[结果 #9999 完成] [体重 5.64] [记数 #{body} 跑步] [漏斗 #{deal} 签约 Acme] "
                "[训练 深蹲架深蹲 50x5/5/5] [建档 感情 完成]")
        reply = self.ethan(text).reply("telegram", "嗯", today=DAY)
        notes = reply["bubbles"][1:]
        self.assertEqual(len(notes), 6)
        self.assertEqual(self.db.conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points").fetchone()[0], 0)
        self.assertTrue(all(n.startswith("【系统提醒】没记上") for n in notes))

    def test_marks_are_hidden(self):
        text = "好的\n[结果 #1 部分 只做了一半][体重 56][训练 臀推机 40x12/12/12 疼痛0][建档 财务 进行中]"
        self.assertEqual(parse_reply(text)["bubbles"], ["好的"])

    def test_no_plan_no_plan_block_but_still_no_excuses(self):
        system = build_system_prompt("人设", {"today": "2026-11-02"})
        self.assertNotIn("今天的计划", system)
        self.assertIn("不准说", system)

    def test_obsidian_today_and_domains_pages(self):
        pages = board_pages(self.store, DAY)
        today = pages["看板/今日计划.md"]
        for text in ("16:00–17:15 训练：下肢（臀为主）（路上约 23 分钟", "## 🎮 任务系统", "### 主线（每个目标完整拆解）",
                     "- [ ] 列出 30 个潜在客户（100 分·草稿待过目）", "- [ ] 冷启动：财务", "1. 臀推机", "## 三餐", "做法："):
            self.assertIn(text, today)
        self.assertIn("- 财务：待建档", pages["看板/各方面.md"])
        self.assertIn("还没开始", pages["看板/各方面.md"])  # 测试数据里没设开始日
        Settings(Path(self.store.root) / "settings.json").set("cold_start_from", "2026-11-02")
        self.assertIn("冷启动从 2026-11-02 开始", board_pages(self.store, DAY)["看板/各方面.md"])

    def test_brief_waits_for_its_time_then_sends_once(self):
        at = planned_time(DAY, BRIEF_SLOT)
        before = datetime.combine(DAY, at, timezone.utc) - timedelta(minutes=1)
        self.assertIsNone(due_brief(before, {}, self.db))
        on_time = datetime.combine(DAY, at, timezone.utc)
        self.assertIsNotNone(due_brief(on_time, {}, self.db))
        self.assertIsNone(due_brief(on_time, {DAY.isoformat(): {"brief": "已发送"}}, self.db))
        self.assertIsNone(due_brief(datetime(2026, 11, 2, 11, 5, tzinfo=timezone.utc), {}, self.db))

    def test_brief_occasion_sections_in_order(self):
        text = brief_occasion(self.plan)
        marks = [text.index(m) for m in ("一、", "二、", "三、", "四、", "五、")]
        self.assertEqual(marks, sorted(marks))
        self.assertIn("16:00–17:15 训练：下肢（臀为主）（路上约 23 分钟", text)
        self.assertIn("主线 创业拿到第一个订单 0/6，下一步：列出 30 个潜在客户", text)
        self.assertIn("支线 1 件：冷启动：财务", text)
        self.assertIn("日常：训练、吃够蛋白质", text)
        self.assertIn("训练：下肢（臀为主）", text)
        self.assertIn("三餐：约", text)
        self.assertIn("财务：待建档", text)

    def test_tick_sends_brief_and_skips_morning_slot(self):
        class Channel:
            owner = 1

            def __init__(self):
                self.sent = []

            def deliver(self, chat_id, bubbles, voices=()):
                self.sent.append(bubbles)

        ethan = self.ethan("早呀\n今天三件事")
        channel = Channel()
        now = datetime.combine(DAY, planned_time(DAY, BRIEF_SLOT), timezone.utc) + timedelta(minutes=1)
        self.assertIn("早报：已发送", tick(ethan, channel, now=now))
        self.assertIn("五、今天的任务", self.backend.systems[-1])
        state = json.loads((Path(self.store.root) / "proactive.json").read_text())
        self.assertEqual(state[DAY.isoformat()]["morning"], "早报已发")
        self.assertNotIn("早报", tick(ethan, channel, now=now + timedelta(minutes=10)))


if __name__ == "__main__":
    unittest.main()


class HerTicksOnlyTest(Stage1Test):
    def test_ethan_never_ticks_or_undoes_her_tasks(self):
        """2026-10-29 她定：所有待办和任务都由她自己勾，小伴 不替她勾、不替她打卡、也不撤销她勾的。"""
        from ethan import quests
        a = self.db.conn.execute("SELECT id FROM tasks WHERE promised_by_ethan = 0 AND done = 0 LIMIT 1").fetchone()[0]
        quests.complete_task(self.db, a, DAY)
        self.ethan(f"好 [撤销完成 #{a} 误点] [打卡 训练]").reply("telegram", "嗯", today=DAY)
        self.assertEqual(self.db.conn.execute("SELECT done FROM tasks WHERE id = ?", (a,)).fetchone()[0], 1)
        self.assertFalse(quests.checked(self.db, quests.habit_by_title(self.db, "训练")["id"], DAY))
        self.assertNotIn("她刚在系统里勾掉的", self.backend.systems[0])

    def test_ticks_only_when_she_explicitly_asks(self):
        """她明确叫 小伴 帮她点的，他才可以点（2026-10-29）。"""
        h = quests.habit_by_title(self.db, "训练")["id"]
        self.ethan("太棒了 [打卡 训练]").reply("telegram", "今天练完了", today=DAY)
        self.assertFalse(quests.checked(self.db, h, DAY))  # 只说做完了，不算叫他点
        self.ethan("好嘞 [打卡 训练]").reply("telegram", "帮我把训练打个卡", today=DAY)
        self.assertTrue(quests.checked(self.db, h, DAY))


class TodoDedupeTest(unittest.TestCase):
    def test_similar_todo_not_added_and_edit_marker(self):
        """2026-10-29 她说 小伴 从聊天里记待办要先查重。"""
        from ethan.core.agent import similar_task, parse_reply
        self.assertTrue(similar_task("回房东的邮件", ["回房东邮件"]))
        self.assertTrue(similar_task("买袜子和手套", ["买袜子"]))
        self.assertFalse(similar_task("交论文", ["改论文"]))
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            tid = store.db.add_task("回房东邮件")
            ethan = Ethan(store, Router(Backend("好"), Ledger(Path(d) / "ledger"), Settings(Path(d) / "s.json")), persona="人设")
            ethan.note_records(f"[待办 回房东的邮件] [待办改 #{tid} 回房东邮件说退租 @2026-11-05]")
            rows = [dict(r) for r in store.db.conn.execute("SELECT title, due FROM tasks")]
            self.assertEqual(rows, [{"title": "回房东邮件说退租", "due": "2026-11-05"}])
            self.assertEqual(parse_reply(f"好 [待办改 #{tid} 回房东邮件说退租]")["bubbles"], ["好"])
            store.close()


class ClaudeChatTest(Stage1Test):
    def test_claude_session_shares_memory_with_telegram(self):
        """2026-10-29：固定的 Claude 会话和 Telegram、微信是同一个 小伴，聊过的互相都知道。"""
        from ethan import chat
        out = chat.talk(self.ethan("收到 [待办 周五交房租]"), "提醒我周五交房租")
        self.assertEqual(out, "收到")
        self.assertIn("周五交房租", [t["title"] for t in self.db.open_tasks()])
        day = [r for r in self.store.raw.read_day(date.today().isoformat()) if r.get("channel") == "claude"]
        self.assertEqual([r["role"] for r in day[-2:]], ["mia", "ethan"])  # 记进同一份原始对话，Telegram 那边的上下文能看到
