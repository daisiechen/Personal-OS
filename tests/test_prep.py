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


class PrepTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        g = self.db.conn.execute("SELECT id FROM goals WHERE status = 'active' LIMIT 1").fetchone()["id"]
        self.season = self.db.add_task("查清 某商学院 能用的资源", promised_by_ethan=True)
        self.main = self.db.add_task("起草 example.com 的大纲", goal_id=g, promised_by_ethan=True)
        self.sent = []
        self.grok = lambda task, channel, report_to="": self.sent.append((task, report_to)) or True

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

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
            self.db.add_task("又一件", promised_by_ethan=True)
        for _ in range(10):
            prep.run(self.store, r, self.grok)
        self.assertEqual(len(r.calls), prep.DAILY_LIMIT)


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

if __name__ == "__main__":
    unittest.main()


class PrepOrderTrashTest(PrepTest):
    def test_her_order_wins_and_drop_goes_to_trash(self):
        """2026-10-29：小伴 的事按她拖的顺序做；“不要了”进回收站，能放回。"""
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


class PrepReviewTest(PrepTest):
    def test_review_decides_order_tops_up_but_never_ticks(self):
        """2026-10-29：她排的顺序是参考，小伴 每天通盘看一遍定先后；没开始的快没了从聊天里补；做没做完她自己点。"""
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
        self.assertIn("整理伦敦冬季室内运动课", titles)
        self.assertEqual(titles.count("起草 example.com 的大纲"), 1)  # 重复的不加
        self.assertNotIn("每周挑一个展", titles)  # 长期工作不加
        self.assertEqual([x["id"] for x in prep.queue(self.db)][:2], [self.main, self.season])  # 页面上还是她排的
        self.assertEqual(prep.review(self.store, FakeRouter([])), {"skipped": "今天看过了"})

    def test_routines_are_not_in_the_list(self):
        rid = self.db.add_task("每周给她挑一个出门的小计划", promised_by_ethan=True)
        self.assertNotIn(rid, [x["id"] for x in prep.queue(self.db)])

    def test_mark_done_from_any_state(self):
        self.assertTrue(prep.mark_done(self.db, self.main))  # 还没开始也能点
        self.assertNotIn(self.main, [x["id"] for x in prep.queue(self.db)])
        self.assertFalse(prep.mark_done(self.db, self.main))


class PrepStarTest(PrepTest):
    def test_star_goes_first_and_team_queue_only_today(self):
        """2026-10-29：她点星星的今天必须做完，最先做；团队页排队只放今天的。"""
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
