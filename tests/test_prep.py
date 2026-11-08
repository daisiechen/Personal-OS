import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import prep
from ethan.core import agent
from tests.test_workbench import make_demo


class FakeRouter:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, messages, purpose="chat"):
        self.calls.append(messages[0]["content"])
        return SimpleNamespace(text=json.dumps(self.replies.pop(0), ensure_ascii=False))


class PrepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        g = self.db.conn.execute("SELECT id FROM goals WHERE status = 'active' LIMIT 1").fetchone()["id"]
        self.season = self.db.add_task("查清 某商学院 能用的资源", promised_by_ethan=True)
        self.main = self.db.add_task("起草 example.com 的大纲", goal_id=g, promised_by_ethan=True)
        for tid in (self.season, self.main):  # 2026-10-31 起新记下的先问她，她说要才做；这里当她都说了要
            prep.start(self.db, tid)
        self.sent = []
        self.grok = lambda task, channel, report_to="": self.sent.append((task, report_to)) or True

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class PrepTest(PrepBase):
    def test_season_quest_first_then_ready_and_chat_approval(self):
        order = [x["id"] for x in prep.queue(self.db)]
        self.assertLess(order.index(self.main), order.index(self.season))  # 本赛季副本相关的先做
        r = FakeRouter([{"status": "ready", "result": "# 大纲\n1. 我是谁", "summary": "大纲三段", "decision": ""}] * 10)
        done = prep.run(self.store, r, self.grok, per_run=1)
        self.assertEqual(done[0][0], order[0])
        ready = prep.ready_for_chat(self.db)
        self.assertEqual(ready[0]["summary"], "大纲三段")
        text = agent.build_system_prompt("人设", {"today": "2026-10-28", "prep_ready": [{**ready[0], **prep.detail(self.db, ready[0]["id"])}]})
        self.assertIn("[准备 #编号 通过]", text)
        prep.note_chat(self.db, f"好嘞 [准备 #{ready[0]['id']} 通过]")
        self.assertEqual(self.db.conn.execute("SELECT done FROM tasks WHERE id = ?", (ready[0]["id"],)).fetchone()[0], 1)
        self.assertEqual(agent.parse_reply(f"好嘞 [准备 #{ready[0]['id']} 通过]")["bubbles"], ["好嘞"])

    def test_grok_round_trip_and_revise(self):
        r = FakeRouter([{"status": "need_grok", "grok_task": "查 某商学院 旁听课和讲座"},
                        {"status": "ready", "result": "三件可以去的", "summary": "挑了三件", "decision": "要不要报名周四讲座"},
                        {"status": "ready", "result": "改好的版本", "summary": "按你说的改了", "decision": ""}])
        self.db.conn.execute("UPDATE tasks SET done = 1 WHERE promised_by_ethan = 1 AND id != ?", (self.season,))
        prep.run(self.store, r, self.grok)
        self.assertEqual(prep.detail(self.db, self.season)["state"], "waiting_grok")
        self.assertTrue(self.sent[0][0].startswith(f"【准备 #{self.season}】"))
        self.assertIn("不下单", self.sent[0][1])
        prep.run(self.store, r, self.grok)  # Grok 还没回，不花钱
        self.assertEqual(len(r.calls), 1)
        (self.root / "inbox").mkdir(exist_ok=True)
        (self.root / "inbox" / "2026-10-28.jsonl").write_text(json.dumps(
            {"at": datetime.now(timezone.utc).isoformat(), "path": "/grok/result", "label": "Grok",
             "text": f"【准备 #{self.season}】周四 18:00 有专业主题讲座"}, ensure_ascii=False) + "\n")
        prep.run(self.store, r, self.grok)
        d = prep.detail(self.db, self.season)
        self.assertEqual((d["state"], d["decision"]), ("ready", "要不要报名周四讲座"))
        self.assertIn("周四 18:00", r.calls[1])  # Grok 查回来的交给模型整理
        self.assertTrue(prep.revise(self.db, self.season, "只要免费的"))
        prep.run(self.store, r, self.grok)
        self.assertIn("只要免费的", r.calls[2])
        self.assertEqual(prep.detail(self.db, self.season)["result"], "改好的版本")

    def test_daily_limit(self):
        r = FakeRouter([{"status": "ready", "result": "x", "summary": "x", "decision": ""}] * 20)
        for _ in range(20):
            prep.start(self.db, self.db.add_task("又一件", promised_by_ethan=True))
        for _ in range(10):
            prep.run(self.store, r, self.grok)
        self.assertEqual(len(r.calls), 20)  # 不限次数，每轮照样做 2 件


    def test_grok_silence_times_out_and_delete_works(self):
        r = FakeRouter([{"status": "need_grok", "grok_task": "查 某商学院 图书馆开放时间"}] + [{"status": "ready", "result": "先这样", "summary": "s", "decision": ""}] * 10)
        prep.run(self.store, r, self.grok, per_run=1)
        tid = next(x["id"] for x in prep.queue(self.db) if x["state"] == "waiting_grok")
        self.assertNotIn(tid, dict(prep.run(self.store, r, self.grok, per_run=5)))  # 刚派出去，还在等
        old = "2026-01-01T00:00:00+00:00"
        self.db.conn.execute("UPDATE prep_work SET updated_at = ? WHERE task_id = ?", (old, tid))
        self.db.conn.commit()
        done = dict(prep.run(self.store, r, self.grok, per_run=5))
        self.assertEqual(done.get(tid), "ready")  # 两天没回就先做出来
        self.db.delete_task(tid)  # 有准备记录的待办也能删（之前外键报错）
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone())

class PrepReviseNowTest(PrepBase):
    def test_her_edit_runs_right_away_even_after_daily_limit(self):
        """跟 小伴 说哪里要改，改不了：意见一提就马上重做，不等下一轮、不被当天额度卡住。"""
        r = FakeRouter([{"status": "ready", "result": "第一版", "summary": "s", "decision": ""},
                        {"status": "ready", "result": "按意见改的", "summary": "s", "decision": ""}])
        prep.run(self.store, r, self.grok, per_run=1, only=self.season)
        self.db.conn.execute("INSERT INTO prep_calls (day, n) VALUES (?, 99) ON CONFLICT(day) DO UPDATE SET n = 99", (prep._today(),))
        spawned = []
        ethan = agent.Ethan.__new__(agent.Ethan)
        ethan.store, ethan.kick = self.store, lambda root, tid: spawned.append(tid)
        ethan.prep_kick(prep.note_chat(self.db, f"好，我改 [准备 #{self.season} 改 只要免费的]"))
        self.assertEqual(spawned, [self.season])
        self.assertEqual(prep.run(self.store, r, self.grok, per_run=1, only=self.season), [(self.season, "ready")])
        self.assertIn("只要免费的", r.calls[1])
        self.assertEqual(prep.detail(self.db, self.season)["result"], "按意见改的")

    def test_kick_spawns_only_that_item(self):
        got = []
        self.assertTrue(prep.kick(self.root, 7, spawn=lambda cmd, **k: got.append(cmd)))
        self.assertEqual(got[0][-4:], ["--data", str(self.root), "--only", "7"])

    def test_ethan_sees_every_ready_item(self):
        prompt = agent.build_system_prompt("人设", {"today": "2026-10-29", "prep_other": [
            {"id": 9, "title": "做旅行清单", "state_label": "做好了，等你看"}]})
        self.assertIn("#9 做旅行清单", prompt)


class PrepKnowsHerGoalsTest(PrepBase):
    def test_brief_has_her_goals_and_chat_and_empty_note_works(self):
        """准备员看不到她的目标、点“让他改”还要再写一遍聊天里说过的话。"""
        prep.ensure(self.db)
        self.store.raw.append("telegram", "mia", "指标要按我每个副本来，不是系统的")
        brief = prep.brief_for(self.store, {"id": self.main, "title": "给每个目标提量化指标", "goal": None})
        g = self.db.conn.execute("SELECT title FROM goals WHERE status = 'active' AND level = 'year' LIMIT 1").fetchone()
        if g:
            self.assertIn(g["title"], brief)
        self.assertIn("指标要按我每个副本来", brief)
        prep.run(self.store, FakeRouter([{"status": "ready", "result": "v1", "summary": "s", "decision": ""}]), self.grok,
                 per_run=1, only=self.main)
        self.assertTrue(prep.revise(self.db, self.main, ""))
        self.assertIn("聊天", prep.detail(self.db, self.main)["feedback"])


class PrepRedoAllTest(PrepBase):
    def test_brief_has_her_memory_and_redo_migration(self):
        """准备员没记忆；七件按新办法重做，旧版留底。"""
        import importlib.util
        self.store.wiki.write("about-mia", "关于 Mia", "她喜欢普拉提，怕冷")
        self.assertIn("怕冷", prep.brief_for(self.store, {"id": self.main, "title": "挑体验课", "goal": None}))
        r = FakeRouter([{"status": "ready", "result": "旧版", "summary": "s", "decision": ""}] * 2
                       + [{"status": "ready", "result": "新版", "summary": "s", "decision": ""}] * 2)
        self.db.conn.execute("UPDATE tasks SET done = 1 WHERE promised_by_ethan = 1 AND id NOT IN (?, ?)", (self.season, self.main))
        prep.run(self.store, r, self.grok, per_run=2)
        spec = importlib.util.spec_from_file_location("m", Path(__file__).resolve().parent.parent / "migrations" / "20261029_prep_redo.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("2 件", m.run(self.store))
        self.assertEqual({x["state"] for x in prep.queue(self.db) if x["id"] in (self.season, self.main)}, {"revising"})
        self.assertEqual(self.db.conn.execute("SELECT value FROM prep_meta WHERE key = ?", (f"old:{self.main}",)).fetchone()["value"], "旧版")
        prep.run(self.store, r, self.grok, revising_only=True, per_run=20)
        self.assertNotIn("旧版", r.calls[2])  # 不给模型看旧版，免得照搬
        self.assertEqual(prep.detail(self.db, self.main)["result"], "新版")
        self.assertIn("自查", prep.PROMPT)


class PrepPendingTest(PrepBase):
    def test_parked_items_are_not_waiting_on_her_and_come_back(self):
        """“先放着”：暂时用不上的收起来，不算等她确认，随时拿出来。"""
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        r = FakeRouter([{"status": "ready", "result": "成品", "summary": "s", "decision": ""}] * 4)
        self.db.conn.execute("UPDATE tasks SET done = 1 WHERE promised_by_ethan = 1 AND id NOT IN (?, ?)", (self.season, self.main))
        prep.run(self.store, r, self.grok, per_run=1, only=self.season)
        wb = Workbench(self.store, Settings(self.root / "s.json"))
        self.assertTrue(wb.act("prep.pending", {"id": self.season})["ok"])
        self.assertTrue(wb.act("prep.pending", {"id": self.main})["ok"])  # 还没开始的也能先放着
        self.assertEqual(prep.ready_for_chat(self.db), [])
        self.assertNotIn(self.season, prep.today_ids(self.db))
        self.assertEqual(prep.run(self.store, r, self.grok), [])  # 准备员不做放着的
        self.assertTrue(wb.act("prep.unpend", {"id": self.season})["ok"])
        self.assertEqual(prep.detail(self.db, self.season)["state"], "ready")  # 回到原来的“做好了等你看”
        self.assertTrue(prep.unpend(self.db, self.main))
        self.assertEqual(prep.detail(self.db, self.main)["state"], "todo")



class PrepOrderTrashTest(PrepBase):
    def test_her_order_wins_and_drop_goes_to_trash(self):
        """小伴 的事按她拖的顺序做；“不要了”进回收站，能放回。"""
        ids = [x["id"] for x in prep.queue(self.db)]
        prep.set_order(self.db, [self.season, self.main])
        self.assertEqual([x["id"] for x in prep.queue(self.db)][:2], [self.season, self.main])
        prep._set(self.db, self.season, state="ready", result="做好了")
        title = prep.drop(self.db, self.season)
        self.assertNotIn(self.season, [x["id"] for x in prep.queue(self.db)])
        trash = prep.trash(self.db)
        self.assertEqual([x["title"] for x in trash], [title])
        prep.restore(self.db, trash[0]["id"])
        back = next(x for x in prep.queue(self.db) if x["title"] == title)
        self.assertEqual(back["state"], "ready")  # 做到哪也原样回来
        self.assertEqual(prep.trash(self.db), [])
        self.assertTrue(ids)

    def test_legacy_deleted_item_can_be_recovered(self):
        title = self.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (self.main,)).fetchone()["title"]
        self.db.delete_task(self.main)  # 以前点“不要了”是直接删
        prep.adopt_legacy(self.db, [title])
        t = prep.trash(self.db)
        self.assertEqual([x["title"] for x in t], [title])
        prep.restore(self.db, t[0]["id"])
        self.assertIn(title, [x["title"] for x in prep.queue(self.db)])


class PrepReviewTest(PrepBase):
    def test_review_decides_order_tops_up_but_never_ticks(self):
        """她排的顺序是参考，小伴 每天通盘看一遍定先后；做没做完她自己点。"""
        self.store.raw.append("telegram", "ethan", "某商学院 的资源我整理好了，发你啦")
        router = FakeRouter([{"order": [{"id": self.season, "why": "她这周就要用"}, {"id": self.main, "why": "副本下一步"}],
                              "done": [{"id": self.season}],
                              "add": [{"title": "整理伦敦冬季室内运动课"}, {"title": "起草 example.com 的大纲"}, {"title": "每周挑一个展"}]}])
        prep.set_order(self.db, [self.main, self.season])  # 她排的
        prep._set(self.db, self.season, state="ready", result="做好了")  # 没开始的只剩 2 件，才会补
        got = prep.review(self.store, router)
        self.assertEqual(got["order"], [self.season, self.main])
        self.assertEqual(self.db.conn.execute("SELECT done FROM tasks WHERE id = ?", (self.season,)).fetchone()[0], 0)  # 不替她勾
        titles = [x["title"] for x in prep.queue(self.db)]
        self.assertNotIn("整理伦敦冬季室内运动课", titles)  # 2026-11-02 起不从聊天里补，改由 helpscope 从她未来 7 天的事里挑
        self.assertEqual([x["id"] for x in prep.queue(self.db)][:2], [self.main, self.season])  # 页面上还是她排的
        self.assertEqual(prep.review(self.store, FakeRouter([])), {"skipped": "今天看过了"})

    def test_review_drops_stale_and_parks_delivered(self):
        """Hidden London 是昨天的事、生蚝罚单昨天已经补上了、查当天的票已经过期，怎么还在今天的活里。"""
        walk = self.db.add_task("出一条散步路线，逛完 Hidden London 后点开", promised_by_ethan=True)
        oyster = self.db.add_task("补上欠她的生蚝罚单", promised_by_ethan=True)
        ticket = self.db.add_task("查这里今天还有没有票", promised_by_ethan=True, due="2000-01-01")
        router = FakeRouter([{"order": [{"id": walk, "why": "x"}, {"id": oyster, "why": "y"}],
                              "stale": [{"id": walk, "why": "Hidden London 昨天就过了"}],
                              "delivered": [{"id": oyster, "why": "照片昨天发了"}]}])
        got = prep.review(self.store, router)
        ids = {x["id"]: x for x in prep.queue(self.db)}
        self.assertNotIn(walk, ids)
        self.assertNotIn(ticket, ids)  # 截止日过了，自动进回收站
        self.assertEqual(set(got["stale"]), {walk, ticket})
        self.assertNotIn(oyster, ids)  # 聊天里交过的直接算做完、收起
        self.assertNotIn(walk, [o["id"] for o in prep.today_order(self.db)])
        self.assertTrue(any("Hidden London" in t["title"] for t in prep.trash(self.db)))

    def test_routines_are_not_in_the_list(self):
        rid = self.db.add_task("每周给她挑一个出门的小计划", promised_by_ethan=True)
        self.assertNotIn(rid, [x["id"] for x in prep.queue(self.db)])

    def test_mark_done_from_any_state(self):
        self.assertTrue(prep.mark_done(self.db, self.main))  # 还没开始也能点
        self.assertNotIn(self.main, [x["id"] for x in prep.queue(self.db)])
        self.assertFalse(prep.mark_done(self.db, self.main))


class PrepStarTest(PrepBase):
    def test_star_goes_first_and_team_queue_only_today(self):
        """她点星星的今天必须做完，最先做；团队页排队只放今天的。"""
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        prep.set_order(self.db, [self.main, self.season])
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        self.assertTrue(wb.act("prep.star", {"id": self.season, "on": 1})["ok"])
        self.assertTrue(next(x for x in prep.queue(self.db) if x["id"] == self.season)["star"])
        r = FakeRouter([{"status": "ready", "result": "x", "summary": "x", "decision": ""}] * 5)
        self.assertEqual(prep.run(self.store, r, self.grok, per_run=1)[0][0], self.season)
        team = wb.team_page()
        queued = [x["title"] for x in team["live"] if x["state"] == "queued"]
        self.assertNotIn("起草 example.com 的大纲", queued)  # 没星星、小伴 今天也没排的不进排队
        prep.set_star(self.db, self.season, False)
        self.assertEqual(prep.stars(self.db), set())

class PrepSecretTest(PrepBase):
    def test_surprise_hidden_from_her(self):
        """“他准备的那个惊喜不用告诉我”：照常准备，不出现在她的清单、红点、团队页和“等你看”里。"""
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        self.db.conn.execute("UPDATE tasks SET done = 1 WHERE promised_by_ethan = 1")
        sid = self.db.add_task("策划周末的小惊喜", promised_by_ethan=True)
        prep.run(self.store, FakeRouter([{"status": "ready", "result": "去看展", "summary": "s", "decision": ""}]), self.grok, per_run=1)
        self.assertEqual(prep.ready_for_chat(self.db), [])
        self.assertEqual([x["id"] for x in prep.secret_ready(self.db)], [sid])
        wb = Workbench(self.store, Settings(self.root / "s.json"))
        team = wb.team_page()
        self.assertFalse(any("惊喜" in x["title"] for x in team["live"]))
        prompt = agent.build_system_prompt("人设", {"today": "2026-10-30", "prep_secret": [{"id": sid, "title": "策划周末的小惊喜", "result": "去看展"}]})
        self.assertIn("别提前说", prompt)

class ParkedRemindTest(PrepBase):
    def test_parked_reminded_once_a_day(self):
        """先放着的每天第一次打开工作台提醒看一次。"""
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import NOON
        wb = Workbench(self.store, Settings(self.root / "s.json"))
        self.assertEqual(wb.state(NOON)["parked_remind"], 0)
        prep.set_pending(self.db, self.season)
        self.assertEqual(wb.state(NOON)["parked_remind"], 1)
        wb.act("prep.parkedseen", {}, NOON)
        self.assertEqual(wb.state(NOON)["parked_remind"], 0)


if __name__ == "__main__":
    unittest.main()


class AskFirstTest(PrepBase):
    def test_new_item_waits_until_she_says_yes(self):
        tid = self.db.add_task("做芬兰装备清单", promised_by_ethan=True)
        self.assertEqual(next(x for x in prep.queue(self.db) if x["id"] == tid)["state"], "ask")
        self.assertIn(tid, [x["id"] for x in prep.asking(self.db)])
        done = prep.run(self.store, FakeRouter("## 结论\n好了"), self.grok, per_run=5)
        self.assertNotIn(tid, [d[0] for d in done])  # 没问过她不做
        prep.note_chat(self.db, "好的我来准备 [准备 #%d 开始]" % tid)
        self.assertEqual(next(x for x in prep.queue(self.db) if x["id"] == tid)["state"], "todo")
        other = self.db.add_task("做第二站行程", promised_by_ethan=True)
        prep.note_chat(self.db, "[准备 #%d 不用]" % other)
        self.assertEqual(next(x for x in prep.queue(self.db) if x["id"] == other)["state"], "pending")
        secret = self.db.add_task("策划周末的小惊喜", promised_by_ethan=True)
        self.assertNotIn(secret, [x["id"] for x in prep.asking(self.db)])  # 惊喜不问她


class HelpScopeTest(unittest.TestCase):
    """quest-structure 第 6 节（让做）：帮办从她未来 7 天的待办和阶段任务里挑。"""

    def setUp(self):
        import tempfile
        from pathlib import Path
        from ethan.memory import MemoryStore
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_picks_from_next_week_and_links_back(self):
        import json
        from datetime import date
        from ethan import helpscope, stages
        from ethan.core.backends import Reply
        day = date(2026, 11, 2)
        self.db.conn.execute("INSERT INTO goals (id, level, title, status, handled_by, updated_at) VALUES (24, 'year', 'DemoApp 上线', 'active', 'agent', 'x')")
        stages.ensure(self.db)
        m = stages.add_milestone(self.db, 24, "M2 内测", "2026-11-30")
        invite = stages.add_task(self.db, m, "找 5 个内测用户")
        soon = self.db.add_task("回复芬兰使馆", due="2026-11-05")
        far = self.db.add_task("约年度体检", due="2027-07-15")
        train = self.db.add_task("训练", due="2026-11-03")
        got = {t["id"] for t in helpscope.candidates(self.db, day)}
        self.assertEqual(got, {invite, soon, train})
        self.assertNotIn(far, got)
        seen = []

        class Router:
            def complete(self, system, messages, purpose="chat"):
                seen.append(messages[0]["content"])
                return Reply(json.dumps({"help": [{"id": invite, "what": "起草给内测用户的邀请消息"}, {"id": 999, "what": "乱写的"}]}), "x", "m", 0)
        new = helpscope.pick(self.db, Router(), day)
        self.assertEqual(len(new), 1)
        self.assertIn("DemoApp 上线，M2 内测", seen[0])
        row = self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (new[0],)).fetchone()
        self.assertEqual((row["title"], row["promised_by_ethan"], row["goal_id"], row["help_for"]), ("起草给内测用户的邀请消息", 1, 24, invite))
        self.assertEqual(helpscope.for_task(self.db, new[0]), "找 5 个内测用户")
        self.assertEqual(helpscope.pick(self.db, Router(), day), [])  # 一天一次
        self.assertNotIn(invite, {t["id"] for t in helpscope.candidates(self.db, date(2026, 11, 3))})  # 已经在帮的不再挑
        self.assertIn(new[0], [x["id"] for x in prep.queue(self.db)])  # 进准备队列，先问她
        self.assertEqual(next(x for x in prep.queue(self.db) if x["id"] == new[0])["state"], "ask")



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
