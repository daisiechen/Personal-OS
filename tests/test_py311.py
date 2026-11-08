"""服务器跑的是 Python 3.11：本机 3.12+ 能过、3.11 编译不过的写法（比如 f-string 里带反斜杠）会让部署整批测试失败、自动退回（11-06 出过一次）。"""
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Py311Test(unittest.TestCase):
    def test_all_code_compiles_on_311(self):
        py = shutil.which("python3.11")
        if not py:
            self.skipTest("本机没有 python3.11")
        files = [str(p) for d in ("ethan", "scripts", "migrations", "tests") for p in (ROOT / d).rglob("*.py")]
        code = "import sys, ast\nbad=[]\nfor f in sys.argv[1:]:\n    try: ast.parse(open(f, encoding='utf-8').read(), f)\n    except SyntaxError as e: bad.append(f'{f}:{e.lineno} {e.msg}')\nprint('\\n'.join(bad))"
        out = subprocess.run([py, "-c", code, *files], capture_output=True, text=True, timeout=120)
        self.assertEqual(out.stdout.strip(), "", "Python 3.11 编译不过：\n" + out.stdout)


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
