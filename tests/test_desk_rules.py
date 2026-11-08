"""收件台“等你处理”统一成一处判断（ethan/desk.py 的 state_of / VIEWS / rows，docs/design/mail-desk.md）。

决定 6：主页留言里“Days 能回 / 不用回”的不算红点；小伴 正在起草的算“等你”。
"""

import re
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import desk, invariants, mail, publicsite, transitions
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent / "ethan"
NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def mail_row(**kw):
    return {"status": "done", "attention": 1, "fyi": 0, "desk_state": "open", "created_at": "2026-11-06T10:00:00+00:00", **kw}


class StateTest(unittest.TestCase):
    """对照表：每种情况在红点、列表、傍晚汇总里算不算。"""

    def test_table(self):
        cases = [
            ("mail", mail_row(), "needs_her"),
            ("mail", mail_row(desk_state="drafting"), "drafting"),   # 决定 6：小伴 在起草的算等你
            ("mail", mail_row(desk_state="draft"), "draft_ready"),
            ("mail", mail_row(desk_state="todo"), "todo"),
            ("mail", mail_row(desk_state="ignored"), "done"),
            ("mail", mail_row(desk_state="closed"), "done"),
            ("mail", mail_row(status="new"), "hidden"),
            ("mail", mail_row(attention=0, fyi=1), "fyi"),
            ("mail", mail_row(attention=0, fyi=1, created_at="2026-10-20T10:00:00+00:00"), "hidden"),  # 知道一下只留 7 天
            ("mail", mail_row(attention=0, fyi=0), "hidden"),
            ("visitor", {"tier": "reply", "desk_state": "open"}, "needs_her"),
            ("visitor", {"tier": "fyi", "desk_state": "open"}, "can_skip"),  # 决定 6：Days能回或不用回的不算红点
            ("visitor", {"tier": "fyi", "desk_state": "draft"}, "draft_ready"),  # 她让起草了、草稿回来了：要她看
            ("visitor", {"tier": "reply", "desk_state": "handed", "draft": None}, "handed"),
            ("visitor", {"tier": "reply", "desk_state": "handed", "draft": "hi"}, "draft_ready"),
            ("visitor", {"tier": "spam", "desk_state": "open"}, "hidden"),
            ("out", {"desk_state": "drafting"}, "drafting"),
            ("out", {"desk_state": "draft"}, "draft_ready"),
            ("out", {"desk_state": "sent", "sent_at": "2026-11-06T09:00:00+00:00"}, "sent_today"),
            ("out", {"desk_state": "sent", "sent_at": "2026-11-04T09:00:00+00:00"}, "done"),
        ]
        for kind, row, want in cases:
            self.assertEqual(desk.state_of(kind, row, NOW), want, (kind, row))
        self.assertEqual(set(desk.LABEL) | {"hidden", "done"}, set(desk.WAIT))
        badge = desk.VIEWS["badge"]["states"]
        self.assertEqual(badge, {"needs_her", "drafting", "draft_ready"})
        self.assertNotIn("can_skip", badge)
        self.assertEqual(desk.VIEWS["invariant"]["states"], badge)
        for v in desk.VIEWS.values():
            self.assertTrue(set(v["states"]) <= set(desk.WAIT))
            self.assertTrue(all(r in desk.RULES for r in v["rules"]))

    def test_today_is_her_day(self):
        """“今天发出的”按她的时区：夏令时 0 点 30 分（UTC 前一天 23:30）发的，算她的今天。"""
        summer_now = datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc)
        row = {"tier": "reply", "desk_state": "sent", "sent_at": "2026-07-01T23:30:00+00:00"}
        self.assertEqual(desk.state_of("visitor", row, summer_now), "sent_today")


class DeskRulesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        desk.ensure(self.db)
        now = "2026-11-06T10:00:00+00:00"
        ins = ("INSERT INTO mail_items (account, msg_key, sender, subject, received_at, status, attention, fyi, reason, desk_state, created_at) "
               "VALUES ('Gmail', ?, 'A <a@x>', ?, ?, 'done', ?, ?, '要你回', ?, ?)")
        for uid, subj, att, fyi, ds in (("1", "要回", 1, 0, "open"), ("2", "在起草", 1, 0, "drafting"), ("3", "知道一下", 0, 1, "open"),
                                        ("4", "记成待办", 1, 0, "todo")):
            self.db.conn.execute(ins, (uid, subj, now, att, fyi, ds, now))
        vins = ("INSERT INTO visitor_messages (visitor_id, at, name, intent, message, received_at, status, tier, summary, desk_state) "
                "VALUES ('v', ?, ?, '其他', '你好', ?, 'new', ?, ?, 'open')")
        self.db.conn.execute(vins, ("t1", "Amy", now, "fyi", "夸主页"))
        self.db.conn.execute(vins, ("t2", "Lena", now, "reply", "想合作"))
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_badge_list_digest_agree(self):
        items = desk.items(self.db)
        waits = {x["title"]: x["wait"] for x in items}
        self.assertEqual(waits, {"要回": "needs_her", "在起草": "drafting", "知道一下": "fyi", "夸主页": "can_skip", "想合作": "needs_her"})
        self.assertEqual(desk.count(self.db), 3)  # 要回、在起草、想合作；Days能回的和知道一下不算
        self.assertEqual(desk.count(self.db), sum(x["waiting"] for x in items))
        self.assertEqual(sorted(m["subject"] for m in mail.to_digest(self.db)), ["在起草", "要回", "记成待办"])
        self.assertEqual(sorted(v["summary"] for v in publicsite.to_digest(self.db)), sorted(["想合作", "夸主页"]))  # 汇总里说一声“不用她回”的

    def test_digest_drops_done_task(self):
        tid = self.db.conn.execute("INSERT INTO tasks (title, created_at, done) VALUES ('回 A', '2026-11-06', 1)").lastrowid
        self.db.conn.execute("UPDATE mail_items SET task_id = ? WHERE msg_key = '4'", (tid,))
        self.assertNotIn("记成待办", [m["subject"] for m in mail.to_digest(self.db)])

    def test_unknown_desk_state_reported(self):
        self.db.conn.execute("UPDATE mail_items SET desk_state = 'weird' WHERE msg_key = '1'")
        self.assertTrue(any("weird" in x for x in invariants.states_valid(self.db)))
        self.assertEqual(set(transitions.STATES["mail_desk"]) >= {"open", "drafting", "draft", "todo", "sent", "ignored", "closed"}, True)


    def test_every_state_desk_writes_is_in_the_graph(self):
        """11-06 巡检：desk.ignore 能把发件箱写成 ignored，状态图里却没有，体检一直报“不在状态图里”。"""
        import inspect
        import re
        from ethan import desk
        self.assertIn("out", inspect.getsource(desk._table))  # ignore 接受 out
        written = set(re.findall(r"desk_state = '([a-z_]+)'", inspect.getsource(desk)))
        for state in written:
            self.assertTrue(any(state in transitions.STATES[g] for g in desk.DESK_STATES.values()), state)
        self.assertIn("ignored", transitions.STATES["outbox_desk"])

class GuardTest(unittest.TestCase):
    """别的模块不再自己写“等她处理”的 desk_state 条件；前端只认服务端给的 waiting。"""
    COND = re.compile(r"\b(?:WHERE|AND|OR)\s+(?:COALESCE\()?(?:\w+\.)?desk_state\b")
    # 白名单每条写原因
    ALLOWED = {"desk.py": "唯一判断（rows 取候选）",
               "onto_query.py": "写作规范只读她发出去的（desk_state = 'sent'），不是“等她处理”"}

    def test_no_scattered_desk_conditions(self):
        bad = []
        for p in ROOT.rglob("*.py"):
            if "__pycache__" in p.parts or p.name in self.ALLOWED:
                continue
            if self.COND.search(p.read_text(encoding="utf-8")):
                bad.append(str(p.relative_to(ROOT)))
        self.assertEqual(bad, [], "改走 desk.rows / desk.state_of")

    def test_front_end_reads_server_flag(self):
        app = (ROOT / "workbench/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn('["open", "draft"].includes(x.state)', app)
        self.assertIn("x.waiting", app)


if __name__ == "__main__":
    unittest.main()


class IgnoreDropsTaskTest(unittest.TestCase):
    """邮件里点了“不用管”，从它来的待办应该从她的待办里消失。"""

    def test_ignore_mail_drops_its_tasks(self):
        import tempfile
        from pathlib import Path
        from ethan import desk, taskrules
        from ethan.memory import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            db = store.db
            desk.ensure(db)
            taskrules.ensure(db)
            db.conn.execute("INSERT INTO mail_items (msg_key, sender, subject, created_at, desk_state) VALUES "
                            "('k1', 'Anthropic <no-reply@anthropic.com>', '新权限', '2026-11-06', 'open')")
            mid = db.conn.execute("SELECT id FROM mail_items WHERE msg_key = 'k1'").fetchone()[0]
            auto = db.add_task("查看并决定是否接受 Claude 新权限")
            db.conn.execute("UPDATE tasks SET source_type = 'email', source_ref = 'mail:k1', status = 'open' WHERE id = ?", (auto,))
            other = db.add_task("别的事")
            db.conn.commit()
            got = desk.ignore(db, "mail", mid)
            self.assertIn("1 件", got.get("msg", ""))
            st = {r[0]: r[1] for r in db.conn.execute("SELECT id, status FROM tasks")}
            self.assertEqual(st[auto], "drop_asked")  # 收进“你说算了的”，不删
            self.assertNotEqual(st[other], "drop_asked")
            self.assertTrue(taskrules.restore(db, auto))  # 能恢复
            store.close()
