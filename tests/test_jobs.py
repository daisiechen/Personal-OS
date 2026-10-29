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

    def test_every_timer_job_goes_through_the_recorder(self):
        root = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        for name in ("backup", "brain", "english", "planner", "prep", "proactive", "vault", "weekly"):
            text = (root / f"ethan2-{name}.service").read_text()
            self.assertIn(f"-m ethan.jobs --data /home/ethan/data --name {name} --", text)
            self.assertIn(name, jobs.JOBS)


if __name__ == "__main__":
    unittest.main()
