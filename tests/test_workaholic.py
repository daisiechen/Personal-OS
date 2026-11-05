import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import nudges, setup_hook, workaholic, worktime
from ethan.clock import LOCAL
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

HOOK = "h" * 40


def at(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=LOCAL)


class WorkaholicTest(unittest.TestCase):
    """她 2026-10-31 定：工作狂指数、产出只算事业和学术、9 点提醒 10 点劝 11 点停、她放行才继续。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        workaholic.ensure(self.db)
        self.day = date(2026, 11, 2)  # 周一

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def work(self, start, end, day=None):
        day = day or self.day
        t, slots = at(day, *start), []
        while t < at(day, *end):
            slots.append(t.strftime("%Y-%m-%dT%H:%M"))
            t += timedelta(minutes=5)
        worktime.record(self.db, slots, "mac")

    def test_index_counts_long_late_and_nonstop(self):
        self.work((9, 0), (13, 0))
        light = workaholic.day_stats(self.db, self.day)
        self.assertEqual((light["hours"], light["late"], light["stretch"]), (4.0, 0, 4.0))
        self.work((14, 0), (23, 30))
        heavy = workaholic.day_stats(self.db, self.day)
        self.assertEqual((heavy["hours"], heavy["late"]), (13.5, 2.5))
        self.assertGreater(heavy["index"], 70)
        self.assertLess(light["index"], heavy["index"])
        sat = self.day + timedelta(days=5)
        self.work((10, 0), (12, 0), sat)
        self.assertTrue(workaholic.day_stats(self.db, sat)["weekend"])

    def test_small_hours_belong_to_the_night_before(self):
        """2026-11-01：凌晨 0–2 点是“昨晚”，不算进今天。"""
        nxt = self.day + timedelta(days=1)
        worktime.record(self.db, [f"{nxt.isoformat()}T00:{m:02d}" for m in range(0, 60, 5)] + [f"{nxt.isoformat()}T10:00"], "mac")
        self.assertEqual(worktime.hours(self.db, self.day), 1.0)
        self.assertEqual(worktime.hours(self.db, nxt), 0.1)
        self.assertEqual(workaholic.day_stats(self.db, self.day)["late"], 1.0)

    def test_output_only_career_and_academic(self):
        career = self.db.add_task("DemoApp 内测反馈")
        life = self.db.add_task("去攀岩")
        self.db.conn.execute("UPDATE tasks SET domain = '事业' WHERE id = ?", (career,))
        self.db.conn.execute("UPDATE tasks SET domain = '生活体验' WHERE id = ?", (life,))
        for tid, n in ((career, 100), (life, 40)):
            self.db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (?, 'x', ?, ?, datetime('now'))",
                                 (n, f"task:{tid}", self.day.isoformat()))
        self.work((9, 0), (14, 0))
        w = workaholic.week(self.db, self.day)
        self.assertEqual((w["output"]["points"], w["hours"], w["efficiency"]), (100, 5.0, 20.0))

    def test_phone_app_open_close_and_workbench(self):
        workaholic.app_event(self.db, "open", "Claude", at(self.day, 10).astimezone(timezone.utc))
        got = workaholic.app_event(self.db, "close", "Claude", at(self.day, 10, 30).astimezone(timezone.utc))
        self.assertIn("stop", got)
        self.assertEqual(workaholic.day_stats(self.db, self.day)["hours"], 0.5)
        workaholic.app_event(self.db, "open", "Codex", at(self.day, 12).astimezone(timezone.utc))  # 只报了打开：下次打开时算 30 分钟
        workaholic.app_event(self.db, "open", "Codex", at(self.day, 15).astimezone(timezone.utc))
        self.assertEqual(workaholic.day_stats(self.db, self.day)["hours"], 1.0)
        workaholic.heartbeat(self.db, at(self.day, 16).astimezone(timezone.utc))
        self.assertEqual(self.db.conn.execute("SELECT source FROM work_slots WHERE slot LIKE '%T16:00'").fetchone()[0], "workbench")

    def test_curfew_phases_and_override(self):
        c = lambda hh, mm=0, d=None: workaholic.curfew(self.db, at(d or self.day, hh, mm).astimezone(timezone.utc))
        self.assertEqual([c(20)["phase"], c(21, 5)["phase"], c(22, 30)["phase"]], ["ok", "remind", "persuade"])
        self.assertTrue(c(23, 10)["stop"])
        self.assertTrue(c(1, 0, self.day + timedelta(days=1))["stop"])  # 凌晨还算前一晚
        workaholic.note_chat(self.db, "好，今晚放你到 1 点，别太晚[加班 到 01:00]", "明天要交，今晚要赶到一点",
                             at(self.day, 22, 50).astimezone(timezone.utc))
        self.assertFalse(c(23, 30)["stop"])
        self.assertEqual(c(23, 30)["until"], "01:00")
        self.assertTrue(c(1, 10, self.day + timedelta(days=1))["stop"])  # 过了放行时间照样停
        self.assertEqual(workaholic.strip("好[加班 到 01:00]"), "好")

    def test_evening_nudges_once_each_and_only_when_working(self):
        self.assertIsNone(nudges._evening(self.db, at(self.day, 21, 10), {}))  # 没在工作就不说
        self.work((20, 30), (21, 10))
        key, text = nudges._evening(self.db, at(self.day, 21, 10), {})
        self.assertEqual(key, f"evening:{self.day}:remind")
        nudges.mark_told(self.db, [key])
        self.assertIsNone(nudges._evening(self.db, at(self.day, 21, 20), {}))
        self.work((21, 10), (22, 5))
        self.assertIn("劝她收工", nudges._evening(self.db, at(self.day, 22, 5), {})[1])

    def test_endpoints(self):
        inbox = Inbox(self.store, {"grok": "g" * 40, "hook": HOOK})
        auth = {"Authorization": f"Bearer {HOOK}"}
        code, got = inbox.handle_get("/cloud/curfew", auth, at(self.day, 23, 5).astimezone(timezone.utc))
        self.assertEqual((code, got["stop"]), (200, True))
        self.assertEqual(inbox.handle_get("/cloud/curfew", {}, None)[0], 401)
        code, got = inbox.handle("/cloud/activity", auth, json.dumps({"event": "open", "app": "Claude"}).encode(),
                                 at(self.day, 23, 5).astimezone(timezone.utc))
        self.assertEqual((code, got["stop"]), (200, True))  # 快捷指令据此退回桌面

    def test_curfew_hook_blocks_after_eleven(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            setup_hook.install(path, "mac", "tok")
            s = json.loads(path.read_text())
            cmds = [h["command"] for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
            self.assertEqual(sum(setup_hook.CURFEW_MARK in c for c in cmds), 1)
            setup_hook.install(path, "mac", "tok")  # 重装不重复
            s = json.loads(path.read_text())
            self.assertEqual(sum(setup_hook.CURFEW_MARK in h["command"] for g in s["hooks"]["UserPromptSubmit"] for h in g["hooks"]), 1)
            fake = Path(d) / "bin"
            fake.mkdir()
            (fake / "curl").write_text('#!/bin/sh\necho "$FAKE"\n')
            (fake / "curl").chmod(0o755)
            cmd = setup_hook.curfew_command("mac")
            for body, code in (('{"stop": true, "message": "已经 23:10 了"}', 2), ('{"stop": false}', 0), ("坏了", 0)):
                r = subprocess.run(["sh", "-c", cmd], env={"PATH": f"{fake}:/usr/bin:/bin", "FAKE": body, "ETHAN_HOOK_TOKEN": "x"},
                                   capture_output=True, timeout=10)
                self.assertEqual(r.returncode, code, body)

    def test_mac_side_fullscreen_every_15_minutes_and_codex_guard(self):
        with tempfile.TemporaryDirectory() as d:
            calls = []

            class Resp:
                def __init__(self, body):
                    self.body = body
                def __enter__(self):
                    return self
                def __exit__(self, *a):
                    return False
                def read(self):
                    return json.dumps(self.body).encode()
            run = lambda cmd, **k: calls.append(cmd) or SimpleNamespace(returncode=0)
            old = worktime.CURFEW_STATE
            worktime.CURFEW_STATE = Path(d) / "state.json"
            try:
                stop = lambda req, timeout: Resp({"phase": "stop", "stop": True, "message": "已经 23:05 了"})
                self.assertEqual(worktime.curfew_check(True, 1000.0, stop, run), "全屏提醒")
                self.assertEqual(worktime.curfew_check(True, 1300.0, stop, run), "")  # 15 分钟内不再弹
                self.assertEqual(worktime.curfew_check(True, 1000.0 + 901, stop, run), "全屏提醒")
                self.assertEqual(worktime.curfew_check(False, 9999.0, stop, run), "")  # 没在用就不弹
                self.assertIn("JavaScript", calls[0])
                self.assertNotIn("lock", " ".join(" ".join(c) for c in calls).lower())  # 不锁屏
            finally:
                worktime.CURFEW_STATE = old
            rc = Path(d) / ".zshrc"
            rc.write_text("export A=1\n")
            worktime.install_codex_guard(rc)
            worktime.install_codex_guard(rc)
            text = rc.read_text()
            self.assertEqual(text.count("ethan-curfew 开始"), 1)
            self.assertIn("export A=1", text)
            r = subprocess.run(["bash", "-n", str(rc)], capture_output=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
