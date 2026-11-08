"""凌晨在手机上看工作台的五条反馈（docs/changelog.md 2026-11-05）。

1. “今日待办和其他怎么又有一样的任务？”——排进今日任务的不在“其他”里重复。
2. “小伴帮我办里面怎么还有一个放行Claude？这个他不是没答应么？”——系统运维事务不进准备员；不是 小伴 答应的不进。
3. “先放着也给个红点啊”——首页的“先放着”红点，点进去那栏上有同样的红点和数，看过就消失。
4. “荒原里面一条事业的不小心归成了生活体验，我都不知道怎么改”——归过的能再改、能撤销，改了记锁。
5. “我自己归的确认的这些东西也会出错，怎么办？”——她确认过（锁住）的说法照样能“不对 / 变了”。
"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ethan import extract, helpscope, invariants, lifebase, locks, mepage, prep, season1
from ethan.core.router import Settings
from ethan.memory import MemoryStore
from ethan.workbench.api import Workbench
from tests.test_workbench import MONDAY, NOON, make_demo


class WorkbenchFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        season1.apply(self.store)
        self.db = self.store.db
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # 1
    def test_side_list_skips_what_is_already_in_today(self):
        tid = self.db.add_task("和开发会话一起定正式评估的题集", due=MONDAY.isoformat())
        today = {t["id"] for t in self.wb.state(NOON)["today"]}
        self.assertIn(tid, today)
        q = self.wb.quests_page(NOON)
        self.assertNotIn(tid, [s["id"] for s in q["side"]])  # 不在“其他”里重复
        self.assertGreaterEqual(q["side_today"], 1)  # 只说一句“另有几件已经排进今日任务”
        # 没排进今天的其他事照样列着
        later = self.db.add_task("约年度体检")
        self.assertIn(later, [s["id"] for s in self.wb.quests_page(NOON)["side"]])

    # 2
    def test_ops_chores_never_reach_prep(self):
        for t in ("放行 Claude 的权限", "给开发会话授权读邮箱", "配置 Claude Code", "Codex 登录一下"):
            self.assertTrue(prep.is_ops(t), t)
        for t in ("和开发会话一起定正式评估的题集", "起草给内测用户的邀请消息", "查伦敦普拉提课的价格"):
            self.assertFalse(prep.is_ops(t), t)
        ops = self.db.add_task("放行 Claude 访问服务器", promised_by_ethan=True)
        self.assertNotIn(ops, [x["id"] for x in prep.queue(self.db)])
        self.assertNotIn(ops, [x["id"] for x in self.wb.state(NOON)["prep"]])
        self.assertNotIn("放行 Claude 访问服务器", self.wb.state(NOON)["prep_routines"])
        # 她自己的待办里有这类事，准备员也不挑来“帮办”
        mine = self.db.add_task("给 Codex 开权限", due=MONDAY.isoformat())
        self.assertNotIn(mine, [t["id"] for t in helpscope.candidates(self.db, MONDAY)])

    def test_helpscope_drops_ops_pick(self):
        tid = self.db.add_task("准备内测", due=MONDAY.isoformat())

        class R:
            def complete(self, system, messages, purpose="chat", images=()):
                from types import SimpleNamespace
                return SimpleNamespace(text=json.dumps({"help": [{"id": tid, "what": "放行 Claude 的权限"}]}, ensure_ascii=False))
        self.assertEqual(helpscope.pick(self.db, R(), MONDAY), [])

    def test_extract_only_ethan_owner_is_his_promise(self):
        lifebase.ensure(self.db)
        data = {"todos": [{"op": "add", "title": "放行 Claude 读日历", "owner": "claude"},
                          {"op": "add", "title": "让 Grok 查芬兰签证要求", "owner": "grok"},
                          {"op": "add", "title": "起草给房东的邮件", "owner": "ethan"}]}
        extract.apply(self.db, data, None, "raw:2026-11-05:1", MONDAY)
        rows = {r["title"]: r["promised_by_ethan"] for r in self.db.conn.execute("SELECT title, promised_by_ethan FROM tasks")}
        self.assertEqual(rows["放行 Claude 读日历"], 0)  # 小伴 没答应过，不进准备员
        self.assertEqual(rows["让 Grok 查芬兰签证要求"], 0)
        self.assertEqual(rows["起草给房东的邮件"], 1)

    # 3
    def test_parked_dot_has_a_target_and_clears_after_seen(self):
        tid = next(x["id"] for x in prep.queue(self.db) if not x["secret"])
        prep.set_pending(self.db, tid)
        st = self.wb.state(NOON)
        self.assertEqual(st["parked_remind"], sum(1 for x in st["prep"] if x["state"] == "pending"))
        self.assertGreater(st["parked_remind"], 0)
        self.assertEqual(invariants.badges_have_targets(self.wb, MONDAY), [])
        self.wb.act("prep.parkedseen", {}, NOON)  # 她点开“先放着”看过
        self.assertEqual(self.wb.state(NOON)["parked_remind"], 0)

    # 4
    def test_change_territory_again_locks_and_undo(self):
        tid = self.db.add_task("做小红书第一条")
        r = self.wb.act("task.domain", {"id": tid, "key": "life"}, NOON)  # 荒原里不小心归错
        self.assertTrue(r["ok"])
        self.assertEqual(r["undo"], {"action": "task.domain", "id": tid, "undo": 1, "prev": ""})
        self.assertTrue(locks.locked(self.db, "task", tid, "domain"))
        # 撤销：回到没归过，锁放开（交还 小伴 猜）
        u = dict(r["undo"])
        self.assertTrue(self.wb.act(u.pop("action"), u, NOON)["ok"])
        self.assertIsNone(self.db.conn.execute("SELECT domain FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
        self.assertFalse(locks.locked(self.db, "task", tid, "domain"))
        # 归过的，在领域页里再改：她改的优先，锁不挡她自己
        self.wb.act("task.domain", {"id": tid, "key": "life"}, NOON)
        r2 = self.wb.act("task.domain", {"id": tid, "key": "career"}, NOON)
        self.assertTrue(r2["ok"])
        self.assertEqual(self.db.conn.execute("SELECT domain FROM tasks WHERE id = ?", (tid,)).fetchone()[0], "career")
        self.assertTrue(locks.locked(self.db, "task", tid, "domain"))
        # 撤销第二次改动：回到她上一次选的生活体验，锁还在
        u = dict(r2["undo"])
        self.wb.act(u.pop("action"), u, NOON)
        self.assertEqual(self.db.conn.execute("SELECT domain FROM tasks WHERE id = ?", (tid,)).fetchone()[0], "life")
        self.assertTrue(locks.locked(self.db, "task", tid, "domain"))
        # 回荒原的“最近归好的”、“其他”的全部列表、“日历 · 改计划”都带着现在归哪块，界面上能改
        waste = self.wb.territory_page("waste", NOON)
        self.assertEqual(next(t for t in waste["recent"] if t["id"] == tid)["terr"], "life")
        self.assertNotIn(tid, [t["id"] for t in waste["tasks"]])
        self.assertEqual(next(t for t in self.wb.quests_page(NOON)["side"] if t["id"] == tid)["terr"], "life")
        todo = next(t for t in self.wb.state(NOON)["planner"]["todos"] if t["id"] == tid)
        self.assertEqual(todo["terr"], "life")


class MeFixLockedTest(unittest.TestCase):
    # 5
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        mepage.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:1", "2026-10-30", "她说的")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def current(self, fid):
        f = mepage._fact(self.db, fid)
        return f["valid_to"] is None and f["status"] not in ("invalid", "deleted")

    def test_confirmed_fact_can_still_be_fixed_by_her(self):
        fid = lifebase.add_fact(self.db, "rhythms", "每周练 5 次", [self.ev], confidence=1.0)
        mepage.verdict_fact(self.db, fid, "yes")  # 她点过“对”，锁住
        self.assertTrue(mepage._fact(self.db, fid)["locked"])
        r = mepage.fix_fact(self.db, fid, "现在每周练 3 次", kind="changed")
        self.assertTrue(r["ok"])
        self.assertFalse(self.current(fid))  # 锁只挡系统，不挡她自己

    def test_fix_replaces_the_one_she_clicked(self):
        a = lifebase.add_fact(self.db, "preferences", "喜欢喝美式咖啡", [self.ev], confidence=1.0)
        mepage.verdict_fact(self.db, a, "yes")
        r = mepage.fix_fact(self.db, a, "其实喜欢拿铁", kind="wrong")
        self.assertTrue(r["ok"])
        self.assertFalse(self.current(a))
        self.assertTrue(any(x["text"] == "其实喜欢拿铁" for x in mepage.all_memory(self.db)["items"]))


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
