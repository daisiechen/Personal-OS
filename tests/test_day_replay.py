"""整天回放（docs/design/system-rules.md 第 7 节，她 2026-11-02 认可）：把一天从早到晚跑一遍，每一步后跑“永远要成立”的检查。"""

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from ethan import invariants, planner
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import NOON, FakeCalendar, make_demo


class DayReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root, NOON.date())
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"), calendar=FakeCalendar())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def at(self, h, m=0):
        return NOON.replace(hour=h, minute=m)

    def act(self, at, action, **p):
        out = self.wb.act(action, p, now=at)
        self.assertTrue(out.get("ok"), (action, out))
        return out

    def check(self, step):
        bad = {k: v for k, v in invariants.run_all(self.wb, NOON.date()).items() if v}
        self.assertEqual(bad, {}, f"第「{step}」步后：{bad}")

    def test_one_day(self):
        day = NOON.date()
        self.check("早上打开")
        # 上午：她加两件、点星一件、改一件的名字、删一件
        a = self.act(self.at(9), "task.add", title="回复导师邮件", when="today")["id"]
        b = self.act(self.at(9, 5), "task.add", title="查英语陪练的渠道", when="today")["id"]
        self.act(self.at(9, 6), "task.star", id=a, on=True)
        self.act(self.at(9, 10), "task.rename", id=a, title="给导师回进度邮件")
        self.act(self.at(9, 12), "task.delete", id=b)
        self.check("上午改完")
        # 中午：计划器重排（模型又提了被删的那件、和她改过名字的那件的旧名字）
        plan = self.db.latest_plan(day.isoformat())
        items = [{"kind": "todo", "title": "查英语陪练的渠道", "priority": "side"},
                 {"kind": "todo", "title": "回复导师邮件", "priority": "side"}]
        planner.link_tasks(self.db, items, day)
        self.assertNotIn("查英语陪练的渠道", [i["title"] for i in items])
        self.assertEqual(next(i for i in items if i["title"] == "回复导师邮件")["task_id"], a)  # 旧名字挂回她改过的那件
        self.check("重排后")
        # 下午：做完一件，新加一件，编号往上走
        self.act(self.at(15), "task.done", id=a)
        # 完成时间按回放里的“那天”记（task.done 用的是真实时钟；不改的话真实日期一跨天，测试就跟着变，见 system-rules 第 5 节）
        self.db.conn.execute("UPDATE tasks SET done_at = ? WHERE id = ?", (self.at(15).isoformat(), a))
        c = self.act(self.at(15, 5), "task.add", title="买菜", when="today")["id"]
        self.assertGreater(c, b)
        self.check("下午")
        # 第二天：星标的做完了，不再出现；锁着的名字还是她改的
        tomorrow = NOON + timedelta(days=1)
        self.assertNotIn(a, {t["id"] for t in self.wb.state(tomorrow)["today"]})
        self.assertEqual(self.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (a,)).fetchone()[0], "给导师回进度邮件")
        self.check("第二天")


    def test_inference_and_privacy_checks(self):
        from ethan import lifebase
        lifebase.ensure(self.db)
        ev = lifebase.add_evidence(self.db, "grok", "grok:1", "2026-11-01", "Grok 说她喜欢早起")
        lifebase.add_fact(self.db, "rhythms", "喜欢早起", [ev], confidence=0.9)
        own = lifebase.add_evidence(self.db, "chat", "chat:9", "2026-11-01", "她说的")
        f2 = lifebase.add_fact(self.db, "body", "在减脂", [own], confidence=1.0)
        self.db.conn.execute("UPDATE facts SET visibility = 'public' WHERE id = ?", (f2,))
        bad = invariants.inference_not_fact(self.db)
        self.assertTrue(any("只有转述" in b for b in bad))
        self.assertTrue(any("标成了公开" in b for b in bad))

if __name__ == "__main__":
    unittest.main()
