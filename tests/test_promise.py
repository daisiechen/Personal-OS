"""答应的事到点报进度：小伴 答应她的事到了说好的时间，主动说进度。
做好了带结果；没做好说卡在哪、什么时候能好；她已经知道的不重复；打扰规则挡住就顺延；重启不重发。"""
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from ethan import locks, nudge_registry, nudges, prep, proactive
from ethan.memory import MemoryStore

os.environ.setdefault("ETHAN_NO_KICK", "1")
LDN = ZoneInfo("Europe/London")


def ldn(day, h, m=0):
    return datetime(2026, 11, day, h, m, tzinfo=LDN)  # 11 月伦敦 = UTC


class Backend:
    name = "subscription"

    def complete(self, system, messages, images=()):
        from ethan.core.backends import Reply
        self.last = system
        return Reply("Hidden London 的退改我查好了", self.name, "m", 0.0)


class Channel:
    owner = 42

    def __init__(self):
        self.sent = []

    def deliver(self, chat_id, bubbles, voices=(), **kw):
        self.sent.append(list(bubbles))


class PromiseTest(unittest.TestCase):
    def setUp(self):
        from ethan.core.agent import Ethan
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router, Settings
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        self.backend = Backend()
        self.ethan = Ethan(self.store, Router(self.backend, Ledger(root / "ledger"), Settings(root / "settings.json")), persona="人设")
        self.ethan.kick = lambda *a, **k: True
        self.channel = Channel()
        self.state_path = self.store.root / proactive.STATE_FILE

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def promise(self, body, made=ldn(6, 10)):
        self.ethan.note_records(f"好，我去查 [答应 {body}]")
        tid = self.db.conn.execute("SELECT MAX(id) FROM tasks WHERE promised_by_ethan = 1").fetchone()[0]
        self.db.conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (made.astimezone(timezone.utc).isoformat(timespec="seconds"), tid))
        self.db.conn.commit()
        return tid

    def test_marker_time_is_kept_and_reports_when_due(self):
        tid = self.promise("查 Hidden London 退改规则 @2026-11-06 18:00")
        title = self.db.conn.execute("SELECT title, due FROM tasks WHERE id = ?", (tid,)).fetchone()
        self.assertNotIn("18:00", title["title"])
        self.assertEqual(title["due"], "2026-11-06")
        prep.start(self.db, tid)
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 17, 50), {}))  # 还没到点
        key, why = prep.promise_due(self.db, ldn(6, 18, 5), {})
        self.assertEqual(key, f"promise:{tid}:stuck")
        self.assertIn("卡在", why)
        self.assertIn("排着队", why)
        self.assertIn("什么时候能好", why)

    def test_done_report_carries_the_result(self):
        tid = self.promise("查 Hidden London 退改规则 @2026-11-06 18:00")
        prep._set(self.db, tid, state="ready", result="不能退，可以提前 48 小时改一次日期。", summary="退改规则查好了", decision="要不要改到周六")
        key, why = prep.promise_due(self.db, ldn(6, 18, 5), {})
        self.assertEqual(key, f"promise:{tid}:done")
        self.assertIn("提前 48 小时改一次日期", why)
        self.assertIn("要不要改到周六", why)
        self.assertIn(f"[准备 #编号 通过]", why)

    def test_waiting_grok_says_where_it_is_stuck(self):
        tid = self.promise("比较芬兰租车 @2026-11-06 12:00")
        prep._set(self.db, tid, state="waiting_grok", grok_ask="查 3 家租车公司 12 月四驱的价格")
        _, why = prep.promise_due(self.db, ldn(6, 12, 30), {})
        self.assertIn("Grok", why)
        self.assertIn("四驱", why)

    def test_default_times(self):
        early = self.promise("整理签证材料清单", made=ldn(6, 10))
        prep.start(self.db, early)
        self.assertEqual(prep.promise_at(self.db, {"id": early, "created_at": ""}, LDN), ldn(6, 19))  # 当天晚些
        late = self.promise("写 physio 问题单", made=ldn(6, 16))
        self.assertEqual(prep.promise_at(self.db, {"id": late, "created_at": ""}, LDN), ldn(7, 10, 30))  # 第二天上午
        dated = self.promise("排周末路线 @2026-11-08")
        self.assertEqual(prep.promise_at(self.db, {"id": dated, "created_at": ""}, LDN), ldn(8, 11))

    def test_her_due_date_lock_wins(self):
        tid = self.promise("查 Hidden London 退改规则 @2026-11-06 18:00")
        self.db.conn.execute("UPDATE tasks SET due = '2026-11-09' WHERE id = ?", (tid,))
        locks.lock(self.db, "task", tid, "due", "2026-11-09")
        self.assertEqual(prep.promise_at(self.db, {"id": tid, "created_at": ""}, LDN), ldn(9, 11))

    def test_known_or_old_is_not_reported(self):
        tid = self.promise("查 Hidden London 退改规则 @2026-11-06 18:00")
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 18, 5), {}))  # 还没问过她要不要做：交给“问要不要做”
        prep._set(self.db, tid, state="pending")
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 18, 5), {}))  # 她先放着了
        prep._set(self.db, tid, state="approved")
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 18, 5), {}))  # 她通过了
        prep._set(self.db, tid, state="ready", result="结果")
        self.assertIsNone(prep.promise_due(self.db, ldn(9, 12), {}))  # 过时两天以上的不翻出来
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 22), {}))  # 晚上不报
        # 做好之后聊天里已经说过这件：当她知道了
        prep._set(self.db, tid, state="ready", result="结果")
        later = datetime.now(timezone.utc).replace(year=2099)
        title = self.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
        keys = prep.note_known(self.db, [{"role": "ethan", "at": later.isoformat(timespec="seconds"), "text": f"{title}弄好啦"}])
        self.assertEqual(keys, [f"promise:{tid}:done"])
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 18, 5), {}))
        # 只提到过答应那一刻（在状态变化之前）的不算
        other = self.promise("比较芬兰租车 @2026-11-06 12:00")
        prep.start(self.db, other)
        self.assertEqual(prep.note_known(self.db, [{"role": "ethan", "at": "2026-01-01T00:00:00+00:00", "text": "比较芬兰租车"}]), [])

    def test_secret_is_never_reported(self):
        tid = self.promise("准备生日惊喜 @2026-11-06 12:00")
        prep._set(self.db, tid, state="ready", result="秘密")
        self.assertIsNone(prep.promise_due(self.db, ldn(6, 13), {}))

    def test_registered_and_ordered(self):
        self.assertTrue(nudge_registry.registered("promise:3:done"))
        self.assertIn("_promise", [c.__name__ for c in nudges.checks()])
        self.assertIn("promise:", proactive.ONCE_PREFIXES)

    def only_promise(self):
        none = lambda *a: None
        return mock.patch.multiple(nudges, **{c.__name__: none for c in nudges.checks() if c.__name__ != "_promise"})

    def test_blocked_is_postponed_then_sent_once(self):
        tid = self.promise("查 Hidden London 退改规则 @2026-11-06 14:00")
        prep._set(self.db, tid, state="ready", result="不能退，可以改一次。", summary="查好了")
        self.store.raw.append("telegram", "mia", "我在开会", at=ldn(6, 14, 0))
        with self.only_promise():
            out = proactive.tick(self.ethan, self.channel, now=ldn(6, 14, 20))
            self.assertNotIn("promise:", out)  # 刚聊过：挡住，不记，顺延
            self.assertEqual(self.channel.sent, [])
            out = proactive.tick(self.ethan, self.channel, now=ldn(6, 15, 30))
            self.assertIn(f"promise:{tid}:done：已发送", out)
            self.assertIn("不能退，可以改一次", self.backend.last)
            self.assertTrue(nudges.told(self.db, f"promise:{tid}:done"))
            n = len(self.channel.sent)
            proactive.tick(self.ethan, self.channel, now=ldn(6, 18, 30))
            proactive.tick(self.ethan, self.channel, now=ldn(7, 10, 30))  # 换一天也不再报
            self.assertEqual(len(self.channel.sent), n)

    def test_restart_while_sending_does_not_resend(self):
        tid = self.promise("查 Hidden London 退改规则 @2026-11-06 14:00")
        prep._set(self.db, tid, state="ready", result="结果", summary="查好了")
        key = f"promise:{tid}:done"
        self.state_path.write_text(json.dumps({"2026-11-06": {key: proactive.SENDING}}), encoding="utf-8")
        with self.only_promise():
            out = proactive.tick(self.ethan, self.channel, now=ldn(6, 15))
        self.assertIn("结果未知", out)
        self.assertTrue(nudges.told(self.db, key))
        self.assertNotIn(f"{key}：已发送", out)


if __name__ == "__main__":
    unittest.main()


class SaidBeforeTest(unittest.TestCase):
    """主动消息带上最近发过的，让他换着说。"""

    def test_recent_proactive_lines_go_into_prompt(self):
        import tempfile
        from pathlib import Path
        from ethan.memory.raw import RawLog
        from ethan.core.agent import said_before
        with tempfile.TemporaryDirectory() as d:
            raw = RawLog(Path(d))
            self.assertEqual(said_before(raw), "")
            raw.append("telegram", "ethan", "到点啦，订位那件还在等", kind="proactive")
            raw.append("telegram", "ethan", "普通聊天的回复")
            raw.append("telegram", "mia", "好")
            got = said_before(raw)
            self.assertIn("到点啦，订位那件还在等", got)
            self.assertNotIn("普通聊天的回复", got)
            self.assertIn("换个开头", got)
