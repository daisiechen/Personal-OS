import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import jobs


class JobsTest(unittest.TestCase):
    def test_runs_are_recorded_and_health_flags_failures_and_late_jobs(self):
        with tempfile.TemporaryDirectory() as d:
            data = Path(d)
            ok = lambda cmd, **kw: SimpleNamespace(returncode=0, stdout="备好了：x.md\n", stderr="")
            bad = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nKeyError: 'x'\n")
            self.assertEqual(jobs.run(data, "english", ["x"], runner=ok), 0)
            self.assertEqual(jobs.run(data, "backup", ["x"], runner=bad), 1)
            rows = {r["name"]: r for r in jobs.health(data)}
            self.assertTrue(rows["英语备课"]["ok"])
            self.assertFalse(rows["加密备份"]["ok"])
            self.assertIn("KeyError", rows["加密备份"]["detail"])
            self.assertEqual(rows["看板同步"]["detail"], "还没有运行记录")
            later = {r["name"]: r for r in jobs.health(data, datetime.now(timezone.utc) + timedelta(days=3))}
            self.assertFalse(later["英语备课"]["ok"])  # 三天没跑
            self.assertIn("该跑没跑", later["英语备课"]["detail"])

    def test_failed_eval_detail_names_the_failing_task_not_the_last_line(self):
        with tempfile.TemporaryDirectory() as d:
            out = "extract_realtime：通过 80%（及格线 85%） ✗\n  ✗ a1：todos 漏了\n  ✗ a2：x\n" + \
                  "fact_ops：通过 95%（及格线 90%）\nmail_classify：通过 100%（及格线 90%）\nvisitor_triage：通过 100%（及格线 85%）\n"
            bad = lambda cmd, **kw: SimpleNamespace(returncode=1, stdout=out, stderr="")
            jobs.run(Path(d), "evals", ["x"], runner=bad)
            row = {r["name"]: r for r in jobs.health(Path(d))}["题集评测"]
            self.assertFalse(row["ok"])
            self.assertIn("extract_realtime", row["detail"])

    def test_killed_by_systemd_is_recorded_not_left_running(self):
        """11-08：记忆整理跑超了 15 分钟上限被 systemd 停掉，没留记录，体检一直报“6 小时还没结束”。"""
        import os
        import signal
        with tempfile.TemporaryDirectory() as d:
            def killed(cmd, **kw):
                os.kill(os.getpid(), signal.SIGTERM)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            self.assertEqual(jobs.run(Path(d), "memory", ["x"], runner=killed), 143)
            row = {r["name"]: r for r in jobs.health(Path(d))}["每晚记忆整理"]
            self.assertFalse(row["ok"])
            self.assertIn("被系统停掉", row["detail"])
            self.assertNotIn("还没结束", row["detail"])

    def test_memory_service_has_room_to_finish(self):
        """11-07 跑了 14 分钟、11-08 超过 15 分钟被停掉：上限放到 60 分钟。"""
        svc = (Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "ethan2-memory.service").read_text()
        self.assertIn("TimeoutStartSec=60min", svc)

    def test_every_timer_job_goes_through_the_recorder(self):
        root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        for name in ("backup", "brain", "english", "planner", "prep", "proactive", "vault", "weekly"):
            text = (root / f"ethan2-{name}.service").read_text()
            self.assertIn(f"-m ethan.jobs --data /home/ethan/data --name {name} --", text)
            self.assertIn(name, jobs.JOBS)


if __name__ == "__main__":
    unittest.main()
