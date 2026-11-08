"""部署前的测试分几路同时跑（串行 8 分钟，她嫌上线慢）：挂了要照样认得出是哪条、整体判失败。"""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ethan import testrun
from ethan.autodeploy import failed_tests

ROOT = Path(__file__).resolve().parent.parent

GOOD = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\n"
BAD = "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self):\n        pass\n    def test_bad(self):\n        self.assertEqual(1, 2)\n"
CRASH = "import os, unittest\nclass T(unittest.TestCase):\n    def test_die(self):\n        os._exit(3)\n"
BROKEN = "import unittest\nimport no_such_module_here\n"
LEAK = ("import unittest\nfrom unittest import mock\nimport ethan.clock\nclass T(unittest.TestCase):\n"
        "    def test_forgets_to_stop(self):\n        mock.patch.object(ethan.clock, 'her_today').start()\n")


class TestRunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_dir(self, **files):
        for name, src in files.items():
            (self.dir / f"{name}.py").write_text(src, encoding="utf-8")
        return subprocess.run([sys.executable, "-m", "ethan.testrun", "-q", "-j", "2", "-s", str(self.dir)],
                              cwd=ROOT, capture_output=True, text=True, timeout=120)

    def test_all_pass(self):
        out = self.run_dir(test_a=GOOD, test_b=GOOD, test_c=GOOD)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("Ran 3 tests", out.stderr)
        self.assertTrue(out.stderr.rstrip().endswith("OK"))

    def test_failure_named_like_unittest_and_exit_1(self):
        out = self.run_dir(test_a=GOOD, test_b=BAD)
        self.assertEqual(out.returncode, 1)
        self.assertIn("Ran 3 tests", out.stderr)
        self.assertIn("FAILED (failures=1)", out.stderr)
        self.assertEqual(failed_tests(out.stderr), ["test_b.test_bad"])  # 设置页“最近部署”靠这个写出是哪条

    def test_import_error_and_crashed_worker_fail(self):
        out = self.run_dir(test_a=GOOD, test_broken=BROKEN, test_crash=CRASH)
        self.assertEqual(out.returncode, 1)
        self.assertIn("FAILED", out.stderr)
        self.assertIn("ERROR: test_crash (整个文件没跑完)", out.stderr)  # 子进程死了：只怪真正崩的那个文件
        self.assertIn("No module named 'no_such_module_here'", out.stderr)
        self.assertNotIn("test_broken（子进程崩了", out.stderr)
        self.assertIn("Ran 2 tests", out.stderr)  # test_a 照样跑完

    def test_leftover_mock_fails_the_file_that_left_it(self):
        """11-07：补丁没撤（或补丁期间头一次 import 的模块抓走了假的），同一进程里后跑的测试会随机挂。"""
        out = self.run_dir(test_a=GOOD, test_leak=LEAK)
        self.assertEqual(out.returncode, 1)
        self.assertIn("ERROR: test_leak (跑完留下了假对象)", out.stderr)
        self.assertIn("ethan.clock.her_today", out.stderr)

    def test_every_test_file_is_picked_up_big_first(self):
        names = testrun.modules()
        self.assertEqual(sorted(names), sorted(p.stem for p in (ROOT / "tests").glob("test_*.py")))
        sizes = [(ROOT / "tests" / f"{n}.py").stat().st_size for n in names]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_deploy_uses_parallel_runner(self):
        src = (ROOT / "ethan/autodeploy.py").read_text(encoding="utf-8")
        self.assertIn('"ethan.testrun", "-q"', src)


if __name__ == "__main__":
    unittest.main()
