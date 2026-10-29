import json
import tempfile
import unittest
from pathlib import Path

from ethan.autodeploy import Deployer, StepFailed

OLD, NEW = "a" * 40, "b" * 40


class FakeServer:
    """假装是服务器：记下执行的命令，按需让某一步失败。"""

    def __init__(self, fail_on=None, units=""):
        self.head, self.calls, self.fail_on, self.units = OLD, [], fail_on, units

    def __call__(self, cmd, cwd=None):
        self.calls.append(cmd)
        line = " ".join(cmd)
        if self.fail_on and self.fail_on in line:
            raise StepFailed(f"{self.fail_on} 出错了")
        if "rev-parse HEAD" in line:
            return self.head + "\n"
        if "rev-parse origin/main" in line:
            return NEW + "\n"
        if "merge --ff-only" in line:
            self.head = NEW
        if "reset --hard" in line:
            self.head = cmd[-1]
        if "log -1" in line:
            return "微信也能发照片\n"
        if "diff --name-only" in line:
            return self.units
        if "is-active" in line:
            return "active\n"
        return ""


class AutoDeployTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def deployer(self, server):
        return Deployer(Path("/repo"), self.data, run=server, sleep=lambda s: None, unit_target=Path("/etc/systemd/system"))

    def test_new_commit_goes_live_in_order(self):
        server = FakeServer(units="deploy/systemd/ethan2-autodeploy.timer\n")
        record = self.deployer(server).once()
        self.assertEqual((record["result"], record["from"], record["to"]), ("已上线", "aaaaaaa", "bbbbbbb"))
        steps = [" ".join(c) for c in server.calls]
        order = [next(i for i, s in enumerate(steps) if key in s)
                 for key in ("ethan.sqlite.bak-auto-bbbbbbb", "merge --ff-only", "unittest",
                             "cp /repo/deploy/systemd/ethan2-autodeploy.timer", "daemon-reload", "systemctl restart")]
        self.assertEqual(order, sorted(order))
        self.assertTrue(all(c[:4] == ["sudo", "-u", "ethan", "-H"] for c in server.calls if "git" in c or "unittest" in c))
        status = json.loads((self.data / "deploy-status.json").read_text())
        self.assertEqual(status["subject"], "微信也能发照片")
        self.assertIsNone(self.deployer(server).once())  # 已经是最新，不再部署

    def test_new_service_unit_is_enabled(self):
        server = FakeServer(units="deploy/systemd/ethan2-workbench.service\n")
        self.deployer(server).once()
        self.assertIn("systemctl enable ethan2-workbench.service", [" ".join(c) for c in server.calls])

    def test_new_timer_is_enabled_and_started(self):
        server = FakeServer(units="deploy/systemd/ethan2-codex.timer\n")
        self.deployer(server).once()
        self.assertIn("systemctl enable --now ethan2-codex.timer", [" ".join(c) for c in server.calls])

    def test_failed_tests_roll_back_and_do_not_retry_same_commit(self):
        server = FakeServer(fail_on="unittest")
        record = self.deployer(server).once()
        self.assertEqual((record["result"], record["step"], record["failed"]), ("失败，已退回原版本", "跑测试", NEW))
        self.assertEqual(server.head, OLD)
        self.assertIn("systemctl restart ethan2-telegram ethan2-weixin ethan2-inbox ethan2-workbench", " ".join(server.calls[-5]))
        self.assertIsNone(self.deployer(server).once())  # 同一个失败的提交不再重试
        log = (self.data / "deploy-log.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 1)

    def test_rollback_problem_is_reported(self):
        server = FakeServer(fail_on="systemctl restart")
        record = self.deployer(server).once()
        self.assertEqual(record["result"], "失败，退回时也出错，需要人工处理")
        self.assertEqual(server.head, OLD)


    def test_failed_restart_restores_units_and_old_backups_pruned(self):
        for i in range(5):
            (self.data / f"ethan.sqlite.bak-auto-{i}").write_text("x")
        server = FakeServer(fail_on="is-active", units="deploy/systemd/ethan2-english.timer\n")
        orig = server.__call__
        def call(cmd, cwd=None):
            if "cat-file" in cmd:
                raise StepFailed("旧版本没有")  # 这个 timer 是新加的
            return orig(cmd, cwd)
        record = self.deployer(call).once()
        self.assertTrue(record["result"].startswith("失败"))
        lines = [" ".join(c) for c in server.calls]
        self.assertIn("systemctl disable --now ethan2-english.timer", lines)
        self.assertIn("rm -f /etc/systemd/system/ethan2-english.timer", lines)
        self.assertEqual(len(list(self.data.glob("ethan.sqlite.bak-auto-*"))), 3)

if __name__ == "__main__":
    unittest.main()
