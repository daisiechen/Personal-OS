"""系统巡检统一 review：定时器都登记、下次时间算得对、在跑/卡住/从没跑过分得清、大批整理进度和卡住、
内存低、部署挂掉的测试名和心跳、收工钩子的云端启动脚本。"""

import json
import os
import re
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ethan import autodeploy, checkup, interviews, jobs, lifebase, setup_hook, syscheck
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "deploy" / "systemd"
NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)  # 周三，本地 12:00
class AgentBoxBackupTest(unittest.TestCase):
    """Agent-Box 每晚备份进体检（Agent-Box 仓库 docs/mia/design.md 第 3 节）。"""

    def write(self, base, name, kind, t):
        (base / name).mkdir(parents=True, exist_ok=True)
        (base / name / "status.json").write_text(json.dumps({kind: {"time": t, "bytes": 10}}))

    def test_fresh_stale_missing_and_not_installed(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d) / "data"
            data.mkdir()
            base = Path(d) / "agent-box-backup"  # 在数据目录旁边
            self.assertEqual(syscheck.backup_info(data, NOW), [])  # 这台机器没有备份仓库：不报
            (base / ".git").mkdir(parents=True)
            self.write(base, "seoul", "db", "2026-11-03T03:20:00Z")   # 33 小时前：好
            self.write(base, "la", "homes", "2026-11-02T04:10:00Z")   # 56 小时前：连续两晚没备上
            self.write(base, "mac", "homes", "2026-11-02T12:30:00Z")  # 48 小时：Mac 放宽到 3 天，还算好
            got = {b["name"]: b["ok"] for b in syscheck.backup_info(data, NOW)}
            self.assertEqual(got, {"seoul": True, "la": False, "mac": True})
            (base / "mac" / "status.json").unlink()
            self.assertFalse({b["name"]: b for b in syscheck.backup_info(data, NOW)}["mac"]["ok"])
            whats = [p for p in syscheck.problems(data, None, NOW, meminfo=Path("/nonexistent")) if p["what"] == "Agent-Box 备份"]
            self.assertEqual(len(whats), 2)
            self.assertIn("la", whats[0]["detail"] + whats[1]["detail"])


class TimersRegisteredTest(unittest.TestCase):
    def test_every_timer_is_registered_with_the_same_schedule(self):
        """deploy/systemd 每个定时器都在 jobs.JOBS 里，频率一字不差；服务经 ethan.jobs 留痕（自动上线以 root 跑，看心跳文件）。"""
        for timer in sorted(UNITS.glob("*.timer")):
            key = timer.stem.removeprefix("ethan2-")
            if key == "autodeploy":
                self.assertIn("autodeploy", (ROOT / "ethan" / "syscheck.py").read_text())
                continue
            self.assertIn(key, jobs.JOBS, f"{timer.name} 没登记")
            lines = [l.strip() for l in timer.read_text().splitlines() if l.startswith(("OnCalendar=", "OnUnitInactiveSec=", "OnUnitActiveSec="))]
            self.assertEqual(lines, [jobs.JOBS[key][2]], timer.name)
            svc = (UNITS / f"ethan2-{key}.service").read_text()
            self.assertIn(f"-m ethan.jobs --data /home/ethan/data --name {key} --", svc)
        for key in jobs.JOBS:
            self.assertTrue((UNITS / f"ethan2-{key}.timer").exists(), key)

    def test_backlog_timer_picks_up_after_deploy_restart(self):
        """部署重启会杀掉收件口起的后台整理（11-04）：由 ethan2-backlog 每 10 分钟接着整理，两步都跑、各自有锁。"""
        svc = (UNITS / "ethan2-backlog.service").read_text()
        self.assertIn("ethan.interviews --data /home/ethan/data --digest-all", svc)
        self.assertIn("ethan.relations --data /home/ethan/data --organize-all", svc)
        self.assertIn("$$?", svc)  # systemd 里 $ 要写成 $$，不然被当成变量
        self.assertIn("TimeoutStartSec=infinity", svc)
        self.assertIn("OnUnitInactiveSec=10min", (UNITS / "ethan2-backlog.timer").read_text())
        self.assertIn("def lock(", (ROOT / "ethan" / "interviews.py").read_text())
        self.assertIn(".relations-organize.lock", (ROOT / "ethan" / "relations.py").read_text())

    def test_every_text_is_plain(self):
        self.assertEqual(jobs.every_text("OnCalendar=Sun *-*-* 20:00:00 Europe/London"), "每周日 20:00")
        self.assertEqual(jobs.every_text("OnUnitInactiveSec=10min"), "跑完隔 10 分钟")
        self.assertEqual(jobs.every_text("OnCalendar=*-*-* 03:30:00"), "每天 03:30 UTC")


class NextRunTest(unittest.TestCase):
    def test_schedules(self):
        at = datetime(2026, 11, 4, 22, 25, tzinfo=timezone.utc)  # 本地 22:25（冬令时 = UTC）
        self.assertEqual(jobs.next_run(jobs.JOBS["prep"][2], at), datetime(2026, 11, 5, 8, 20, tzinfo=timezone.utc))
        self.assertEqual(jobs.next_run(jobs.JOBS["proactive"][2], at), datetime(2026, 11, 4, 22, 30, tzinfo=timezone.utc))
        self.assertEqual(jobs.next_run(jobs.JOBS["vault"][2], at), datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc))
        self.assertEqual(jobs.next_run(jobs.JOBS["weekly"][2], at), datetime(2026, 11, 8, 20, 0, tzinfo=timezone.utc))
        self.assertEqual(jobs.next_run(jobs.JOBS["mail"][2], at), at + timedelta(minutes=10))
        summer = datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc)  # 夏令时：本地 06:30 = UTC 05:30
        self.assertEqual(jobs.next_run(jobs.JOBS["planner"][2], summer), datetime(2026, 7, 1, 5, 30, tzinfo=timezone.utc))


class JobStatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, key, now=NOW):
        return next(r for r in jobs.overview(self.root, now) if r["key"] == key)

    def test_running_is_not_late_and_old_marker_is_stuck(self):
        jobs.record(self.root, "codex", True, NOW - timedelta(hours=1), NOW - timedelta(minutes=50))
        jobs.mark_start(self.root, "codex", NOW - timedelta(minutes=40))
        self.assertEqual(self.row("codex")["state"], "running")
        self.assertEqual(self.row("codex", NOW + timedelta(hours=5))["state"], "stuck")
        jobs.mark_start(self.root, "backlog", NOW - timedelta(hours=9))  # 大批整理跑多久都不按时长判断
        self.assertEqual(self.row("backlog")["state"], "running")

    def test_finishing_clears_running(self):
        res = lambda cmd, **kw: type("R", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()
        jobs.run(self.root, "brain", ["x"], runner=res)
        self.assertEqual(self.row("brain", datetime.now(timezone.utc))["state"], "ok")

    def test_never_ran_reported_after_its_first_due_time(self):
        self.assertEqual(self.row("evals")["state"], "waiting")  # 第一次看到：还不报
        self.assertEqual(self.row("evals", NOW + timedelta(days=4))["state"], "never")  # 周六 15:00 过了还没跑
        self.assertTrue(any(p["what"] == "题集评测" for p in checkup.problems(self.root, None, NOW + timedelta(days=4), services=False)))

    def test_late_uses_the_schedule(self):
        jobs.record(self.root, "prep", True, NOW - timedelta(hours=14, minutes=5), datetime(2026, 11, 3, 22, 25, tzinfo=timezone.utc))
        self.assertEqual(self.row("prep", datetime(2026, 11, 4, 9, 0, tzinfo=timezone.utc))["state"], "ok")  # 夜里停，早上 08:20 才该跑
        self.assertEqual(self.row("prep", datetime(2026, 11, 4, 10, 30, tzinfo=timezone.utc))["state"], "late")


class SyscheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.mem = self.root / "meminfo"
        self.mem.write_text("MemTotal: 1990000 kB\nMemAvailable: 800000 kB\nSwapTotal: 2000000 kB\nSwapFree: 1500000 kB\n")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def wechat(self, n, done, at):
        self.db.conn.execute(interviews.SCHEMA)
        for i in range(n):
            ev = lifebase.add_evidence(self.db, "interview", f"interview:w{i}", at.isoformat(),
                                       text=f"【访谈】会话：微信 · 朋友{i}；场合：她的微信聊天（导出）；整段对话\n【她】好")
            self.db.conn.execute("UPDATE evidence SET recorded_at = ? WHERE id = ?", (at.isoformat(), ev))
            if i < done:
                self.db.conn.execute("INSERT INTO interview_digest (evidence_id, at) VALUES (?, ?)", (ev, at.isoformat()))
        self.db.conn.commit()

    def test_backlog_progress_and_stuck(self):
        self.wechat(5, 2, NOW - timedelta(minutes=30))
        b = syscheck.backlog(self.db, NOW)
        self.assertEqual((b["wechat"]["done"], b["wechat"]["total"], b["talks_left"], b["talks_stuck"]), (2, 5, 3, False))
        late = syscheck.backlog(self.db, NOW + timedelta(hours=3))
        self.assertTrue(late["talks_stuck"])
        self.assertFalse(late["people_stuck"])  # 对话没整理完，人物卡还没轮到
        got = checkup.problems(self.root, self.db, NOW + timedelta(hours=3), services=False, meminfo=self.mem)
        self.assertTrue(any(p["what"] == "大批整理" and "3 份" in p["detail"] for p in got))

    def test_memory_levels(self):
        self.assertEqual(syscheck.memory_info(self.mem)["level"], "ok")
        self.assertEqual(syscheck.memory_info(self.mem)["swap_used_mb"], 489)
        self.mem.write_text("MemTotal: 1990000 kB\nMemAvailable: 250000 kB\n")
        self.assertEqual(syscheck.memory_info(self.mem)["level"], "low")
        self.assertFalse(any(p["what"] == "服务器内存" for p in syscheck.problems(self.root, None, NOW, self.mem)))  # 300 以下只标黄
        self.mem.write_text("MemTotal: 1990000 kB\nMemAvailable: 150000 kB\n")
        self.assertTrue(any(p["what"] == "服务器内存" for p in syscheck.problems(self.root, None, NOW, self.mem)))
        self.assertIsNone(syscheck.memory_info(self.root / "没有"))

    def test_deploy_failed_tests_and_heartbeat(self):
        (self.root / "deploy-status.json").write_text(json.dumps({"to": "abcdef1234", "result": "失败，已退回原版本", "step": "跑测试",
                                                                  "at": NOW.isoformat(), "failed_tests": ["test_x.test_y"]}))
        d = syscheck.deploy_info(self.root, NOW)
        self.assertEqual((d["commit"], d["ok"], d["failed_tests"], d["check_ok"]), ("abcdef1", False, ["test_x.test_y"], True))
        (self.root / "deploy-check.json").write_text(json.dumps({"checked_at": (NOW - timedelta(hours=1)).isoformat()}))
        got = {p["what"]: p["detail"] for p in syscheck.problems(self.root, None, NOW, self.mem)}
        self.assertIn("自动上线检查", got)
        self.assertIn("test_x.test_y", got["自动上线"])

    def test_feeds_show_proactive_parts_and_mac_exports(self):
        names = [f["name"] for f in syscheck.feeds(self.db, NOW)]
        for want in ("聊聊", "这周末可以去", "派 Grok", "微信导出", "Codex 导出", "Grok Bot App"):
            self.assertTrue(any(want in n for n in names), want)

    def test_new_sources_registered_and_irregular_ones_never_stale(self):
        codes = {s["code"]: s for s in lifebase.source_health(self.db, NOW)}
        for c in ("S4b", "S17b", "S19", "S20", "S20g"):
            self.assertIn(c, codes)
        old = NOW - timedelta(days=60)
        lifebase.add_evidence(self.db, "interview", "interview:c", old.isoformat(), text="【访谈】会话：Codex 会话（x）；场合：y；整段对话；agent：Codex\n【她】好")
        lifebase.add_evidence(self.db, "sent", "sent:1", old.isoformat(), text="我写的")
        self.db.conn.execute("UPDATE evidence SET recorded_at = ?", (old.isoformat(),))
        codes = {s["code"]: s for s in lifebase.source_health(self.db, NOW)}
        self.assertEqual((codes["S20"]["state"], codes["S4b"]["state"], codes["S10"]["state"]), ("ok", "ok", "not_connected"))
        self.wechat(1, 0, NOW - timedelta(days=12))
        self.assertEqual({s["code"]: s for s in lifebase.source_health(self.db, NOW)}["S19"]["state"], "stale")

    def test_overview_counts_problems(self):
        out = syscheck.overview(self.root, self.db, NOW, meminfo=self.mem, services=False)
        for k in ("jobs", "deploy", "memory", "backlog", "feeds", "sources", "checks", "other", "bad"):
            self.assertIn(k, out)


class WeekendFeedTest(unittest.TestCase):
    def test_sent_read_from_nudge_log(self):
        """11-07 体检误报“这周五过了还没发”：发送记在 nudge_sent（weekend:周五），weekend_recs.sent_at 没人写。"""
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from ethan import nudges, weekend
        from ethan.memory import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            try:
                weekend.ensure(store.db)
                store.db.conn.execute("INSERT INTO weekend_recs (week, asked_at, got_at) VALUES ('2026-11-06', '2026-11-05T09:00:00+00:00', '2026-11-05T09:01:00+00:00')")
                now = datetime(2026, 11, 7, 20, 0, tzinfo=timezone.utc)
                bad = lambda: next(f for f in syscheck.feeds(store.db, now) if "周末" in f["name"])
                self.assertFalse(bad()["ok"])
                nudges.log_sent(store.db, "weekend:2026-11-06", datetime(2026, 11, 6, 12, 32, tzinfo=timezone.utc))
                self.assertTrue(bad()["ok"])
            finally:
                store.close()


class AutodeployHeartbeatTest(unittest.TestCase):
    def test_heartbeat_and_failed_test_names(self):
        from tests.test_autodeploy import FakeServer
        with tempfile.TemporaryDirectory() as d:
            server = FakeServer()
            orig = server.__call__

            def run(cmd, cwd=None):
                if "ethan.testrun" in cmd:
                    raise autodeploy.StepFailed("FAILED (failures=1)", "FAIL: test_b (tests.test_mepage.MeTest.test_b)\nERROR: test_c (tests.test_jobs.JobsTest.test_c)\nFAILED")
                return orig(cmd, cwd)
            rec = autodeploy.Deployer(Path("/repo"), Path(d), run=run, sleep=lambda s: None).once()
            self.assertEqual(rec["failed_tests"], ["test_mepage.test_b", "test_jobs.test_c"])
            self.assertIn("checked_at", json.loads((Path(d) / autodeploy.CHECK).read_text()))

    def test_python_311_and_old_formats(self):
        self.assertEqual(autodeploy.failed_tests("FAIL: test_a (tests.test_x.T)\nFAIL: test_a (tests.test_x.T.test_a)"), ["test_x.test_a"])


class CloudCurfewTest(unittest.TestCase):
    def test_repo_settings_match_generated_curfew(self):
        repo = json.loads((ROOT / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for g in repo["hooks"]["UserPromptSubmit"] for h in g["hooks"] if setup_hook.CURFEW_MARK in h["command"]]
        self.assertEqual(cmds, [setup_hook.curfew_command("cloud")])
        self.assertIn("定时巡检", cmds[0])  # 定时任务和巡检放行

    def test_cloud_user_script_writes_user_settings_without_token(self):
        script = setup_hook.cloud_user_script()
        self.assertNotRegex(script, r"[0-9a-f]{32}")
        self.assertIn("ETHAN_CLOUD_TOKEN", script)
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".claude").mkdir()
            (Path(home) / ".claude" / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]}}))
            for _ in range(2):  # 每次开环境都会跑，重复跑只留一份
                r = subprocess.run(["bash", "-c", script], env={"HOME": home, "PATH": os.environ["PATH"]}, capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 0, r.stderr)
            s = json.loads((Path(home) / ".claude" / "settings.json").read_text())
            self.assertEqual(s["permissions"]["allow"], ["Bash(ls:*)"])
            hooks = s["hooks"]["UserPromptSubmit"]
            self.assertEqual(len(hooks), 1)
            self.assertEqual(hooks[0]["hooks"][0]["command"], setup_hook.curfew_command("cloud"))
            self.assertEqual(setup_hook.check([Path(home) / ".claude" / "settings.json"])[0][1:], (True, True))

    def test_doc_has_the_same_script(self):
        doc = (ROOT / "docs" / "operations" / "cloud-curfew.md").read_text()
        self.assertEqual(re.search(r"```bash\n(.*?)```", doc, re.S).group(1), setup_hook.cloud_user_script())

    def test_curfew_passes_scheduled_and_patrol_messages(self):
        cmd = setup_hook.curfew_command("cloud")
        for text, code in (('{"prompt": "定时巡检（……）"}', 0), ('{"prompt": "<queued-remote-notifications>"}', 0)):
            r = subprocess.run(["sh", "-c", cmd], input=text, env={"PATH": os.environ["PATH"], "CLAUDE_CODE_REMOTE": "true", "ETHAN_CLOUD_TOKEN": "x"},
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, code)


class BadgeChecksTest(unittest.TestCase):
    def test_unregistered_badge_and_brain_count_reported(self):
        from ethan import invariants
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        from tests.test_workbench import MONDAY, make_demo
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            try:
                wb = Workbench(store, Settings(Path(d) / "settings.json"))
                self.assertEqual(invariants.badges_have_targets(wb, MONDAY), [])
                wb.badges = lambda day: {"relations": 2}
                self.assertTrue(any("relations" in x for x in invariants.badges_have_targets(wb, MONDAY)))
                wb.badges = lambda day: {"brain": 3}
                self.assertTrue(any("收藏和资料" in x for x in invariants.badges_have_targets(wb, MONDAY)))
            finally:
                store.close()


class NudgePrefixesTest(unittest.TestCase):
    def test_prefixes_built_from_constants_are_registered(self):
        from ethan import nudge_registry, nudges, weekend
        for p in (nudges.CHAT_PREFIX, weekend.PREFIX):
            self.assertTrue(nudge_registry.registered(p + "2026-11-04"), p)
        from ethan import proactive  # 发出去要记进 nudge_once 的那几类（11-05 起写成常量 ONCE_PREFIXES）
        self.assertTrue(proactive.ONCE_PREFIXES)
        for p in proactive.ONCE_PREFIXES:
            self.assertTrue(nudge_registry.registered(p + "x"), p)


if __name__ == "__main__":
    unittest.main()
