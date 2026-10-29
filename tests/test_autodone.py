import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import autodone, finance, places_log
from tests.test_workbench import MONDAY, make_demo


class AutoDoneTest(unittest.TestCase):
    """规则本身的测试（开着时）；2026-10-29 起默认关掉，她自己勾。"""

    def setUp(self):
        autodone.ENABLED = True

    def tearDown(self):
        autodone.ENABLED = False

    def test_off_by_default_ticks_nothing(self):
        autodone.ENABLED = False
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            store.db.add_task("在 Monzo 里授权一次")
            finance.record_cmb(store.db, f"您账户0000于{MONDAY.month}月{MONDAY.day}日10:00在瑞幸快捷支付30.00元", datetime.now(timezone.utc))
            places_log.record(store.db, "leave", now=datetime.now(timezone.utc))
            self.assertEqual(autodone.check(store.db, Path(d), MONDAY), [])
            store.close()

    def test_connected_sources_tick_matching_tasks(self):
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            db = store.db
            bank = db.add_task("iPhone 快捷指令转发招行消费短信")
            monzo = db.add_task("在 Monzo 里授权一次")
            other = db.add_task("回房东邮件")
            self.assertEqual(autodone.check(db, Path(d), MONDAY), [])  # 还没接上，什么都不勾
            finance.record_cmb(db, f"您账户0000于{MONDAY.month}月{MONDAY.day}日10:00在瑞幸快捷支付30.00元", datetime.now(timezone.utc))
            got = autodone.check(db, Path(d), MONDAY)
            self.assertEqual([g[0] for g in got], [bank])
            done = {r["id"]: r["done"] for r in db.conn.execute("SELECT id, done FROM tasks WHERE id IN (?, ?, ?)", (bank, monzo, other))}
            self.assertEqual(done, {bank: 1, monzo: 0, other: 0})
            self.assertEqual(autodone.check(db, Path(d), MONDAY), [])  # 勾过不再勾
            store.close()

    def test_positioning_task_not_ticked_and_wrong_ticks_reverted(self):
        """2026-10-29：“定好小红书和 IG 定位”被“手机定位接上了”误勾，她撤销后又被勾回去。"""
        from ethan import quests
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            db = store.db
            xhs = db.add_task("定好小红书和 IG 定位，各发第一篇")
            loc = db.add_task("接手机定位（快捷指令设两条自动化）")
            places_log.record(db, "leave", now=datetime.now(timezone.utc))
            got = [g[0] for g in autodone.check(db, Path(d), MONDAY)]
            self.assertEqual(got, [loc])
            # 以前的宽规则勾过的：现在认不出来就撤回
            quests.complete_task(db, xhs, MONDAY)
            db.conn.execute("INSERT OR REPLACE INTO task_checks (task_id, result, at) VALUES (?, 'auto', 'x')", (xhs,))
            autodone.check(db, Path(d), MONDAY)
            self.assertEqual(db.conn.execute("SELECT done FROM tasks WHERE id = ?", (xhs,)).fetchone()[0], 0)
            # 她撤销过的不再自动勾
            other = db.add_task("设好手机定位")
            autodone.her_undo(db, other)
            self.assertNotIn(other, [g[0] for g in autodone.check(db, Path(d), MONDAY)])
            store.close()

if __name__ == "__main__":
    unittest.main()
