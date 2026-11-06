"""后台任务表 + 唯一额度检查 + 唯一起进程入口（全系统模块化第三批，她的决定 7）。

决定 7：用付费额度时，除了聊天、早上排计划、收邮件分类，其余后台全停，额度恢复后自动补。
守卫：ethan/ 下调模型不带登记过的 purpose、或者自己 subprocess.Popen 起后台进程的，测试不过。
"""

import ast
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ethan import bgjobs, bgwork, catchup, jobs
from ethan.core import quota
from ethan.core.ledger import Ledger
from ethan.core.router import QuotaBlocked, Router, Settings

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)

# 调模型但 purpose 不是字面量的地方：每条写原因（其余都必须写 purpose="登记过的名字"）
DYNAMIC = {
    "ethan/llm.py": "structured 本身：purpose 由调用方传（调用处另查）",
    "ethan/interviews.py": "extract_transcript / _extract 的 purpose 是函数参数；调用处传的字面量见 PURPOSE_ARGS",
    "ethan/bgwork.py": "PatientRouter 转手调用方的 purpose",
    "ethan/workbench/api.py": "工作台放锁的代理，转手调用方的参数",
    "ethan/core/agent.py": "reach_out 的 purpose 参数（默认 proactive；每周复盘等调用方传登记过的名字）",
}
# 不写 purpose 就是默认的 "chat"：只有聊天本身可以
CHAT_DEFAULT = {"ethan/core/agent.py": "回她的话（含补查、打回重写），默认 purpose=chat"}
# 函数参数形式传进去的 purpose（interviews.extract_transcript / _extract 的最后一个参数）
PURPOSE_ARGS = {"transcript-facts", "file-facts", "interview-facts", "eval-transcript-facts"}
# 自己起进程可以的（不是后台任务）
POPEN_OK = {
    "ethan/bgwork.py": "唯一入口 spawn_logged / kick",
    "ethan/setup_hook.py": "写进 git hook 的脚本文本，不是 ethan 运行时起进程",
    "ethan/bgjobs.py": "表的说明里提到 Popen 这个词",
}


def _py_files():
    for p in sorted((ROOT / "ethan").rglob("*.py")):
        yield p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")


def _model_calls(src):
    """(行号, purpose 字面量或 None, 有没有写 purpose)：xxx.router.complete(...) / router.complete(...) / llm.structured(...)。"""
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        recv = ast.unparse(f.value) if isinstance(f, ast.Attribute) else ""
        is_complete = name == "complete" and "router" in recv.lower()
        is_structured = name == "structured" and recv in ("llm", "")
        if not (is_complete or is_structured):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        p = kw.get("purpose")
        if p is None and is_complete and len(node.args) >= 3:
            p = node.args[2]
        if p is None and is_structured and len(node.args) >= 3:  # 不写 purpose 用 task（第 3 个参数）
            p = node.args[2]
        consts = [p.body, p.orelse] if isinstance(p, ast.IfExp) else [p]  # 'a' if x else 'b' 两个都查
        lits = [c.value for c in consts if isinstance(c, ast.Constant) and isinstance(c.value, str)]
        yield node.lineno, (lits if p is not None and len(lits) == len(consts) else None), p is not None


class GuardTest(unittest.TestCase):
    def test_every_model_call_is_in_the_table(self):
        bad = []
        for path, src in _py_files():
            if path in ("ethan/core/router.py",) or path.startswith("ethan/core/backends"):
                continue
            for line, lits, has in _model_calls(src):
                if path in DYNAMIC and not lits:
                    continue
                if lits is not None:
                    bad += [f"{path}:{line} purpose={lit!r} 没在 bgjobs.TABLE 登记" for lit in lits if lit not in bgjobs.TABLE]
                elif not has:
                    if path not in CHAT_DEFAULT:
                        bad.append(f"{path}:{line} 调模型没写 purpose（默认算聊天、付费额度时照跑）")
                else:
                    bad.append(f"{path}:{line} purpose 不是字面量，查不了表")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_purpose_args_registered(self):
        src = (ROOT / "ethan/interviews.py").read_text(encoding="utf-8") + (ROOT / "ethan/evals.py").read_text(encoding="utf-8")
        for p in PURPOSE_ARGS:
            self.assertIn(f'"{p}"', src)
            self.assertIn(p, bgjobs.TABLE)

    def test_no_popen_outside_bgwork(self):
        bad = []
        for path, src in _py_files():
            if path in POPEN_OK:
                continue
            for i, line in enumerate(src.splitlines(), 1):
                if "Popen" in line or "start_new_session" in line:
                    bad.append(f"{path}:{i} 自己起后台进程（走 bgwork.kick，名字登记在 bgjobs.KICKS）")
        self.assertEqual(bad, [], "\n".join(bad))

    def test_kick_names_are_literal_and_registered(self):
        bad = []
        for path, src in _py_files():
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call):
                    continue
                f = node.func
                ours = getattr(f, "id", "") == "_kick" or (getattr(f, "id", "") == "kick" and "from .bgwork import kick\n" in src) \
                    or (isinstance(f, ast.Attribute) and f.attr == "kick" and ast.unparse(f.value) == "bgwork")
                if isinstance(node, ast.Call) and ours \
                        and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str):
                    if node.args[1].value not in bgjobs.KICKS:
                        bad.append(f"{path}:{node.lineno} {node.args[1].value}")
        self.assertEqual(bad, [])

    def test_table_is_complete(self):
        for purpose, j in bgjobs.TABLE.items():
            self.assertIn(j.kind, bgjobs.KINDS, purpose)
            self.assertTrue(j.name and j.spawn and j.why, purpose)
            if j.kind == bgjobs.BACKGROUND:
                self.assertTrue(j.catchup, f"{purpose} 停了要写怎么补")
            if j.jobs:
                self.assertIn(j.jobs, jobs.JOBS, purpose)
            if (j.catchup or "").startswith("rerun:"):
                self.assertIn(j.catchup[6:], bgjobs.RERUN)
                self.assertIn(j.catchup[6:], jobs.JOBS)
        for name, k in bgjobs.KICKS.items():
            self.assertTrue((ROOT / (k.module.replace(".", "/") + ".py")).exists(), name)
            for p in k.purposes:
                self.assertIn(p, bgjobs.TABLE, name)

    def test_decision_7_essentials(self):
        """她点名照跑的三样：聊天、早上排计划、收邮件分类。"""
        for p in ("chat", "planner", "mail-classify"):
            self.assertEqual(bgjobs.TABLE[p].kind, bgjobs.ESSENTIAL)
        for p in ("memory-nightly", "diary", "prep", "brain-enrich", "english-lesson", "relations-card", "extract-realtime"):
            self.assertTrue(bgjobs.stops_on_paid(p), p)


class Backend:
    name = "subscription"

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, images=()):
        self.calls += 1
        return SimpleNamespace(text="好", backend="subscription", model="m", cost_usd=0.0, rate_limit=None)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.backend = Backend()
        self.router = Router(self.backend, Ledger(self.root / "ledger"), Settings(self.root / "settings.json"))
        quota.block(self.root, datetime.now(timezone.utc) + timedelta(hours=3), "在用付费额度（extra usage）")

    def tearDown(self):
        self.tmp.cleanup()

    def test_background_stops_and_is_noted(self):
        with self.assertRaises(QuotaBlocked):
            self.router.complete("s", [], purpose="memory-nightly")
        self.assertEqual(self.backend.calls, 0)
        self.assertIn("memory-nightly", bgjobs.skipped(self.root)["purposes"])
        self.assertEqual(bgjobs.catchups(self.root), ["journal"])

    def test_essential_and_foreground_run(self):
        for p in ("chat", "planner", "mail-classify", "translate"):
            self.router.complete("s", [], purpose=p)
        self.assertEqual(self.backend.calls, 4)

    def test_quota_blocked_is_a_model_failure(self):
        """原来“模型不可用就兜底”的地方照样接得住（QuotaBlocked 是 AllBackendsFailed 的一种）。"""
        from ethan.core.router import AllBackendsFailed
        self.assertTrue(issubclass(QuotaBlocked, AllBackendsFailed))
        self.assertIs(bgwork.QuotaBlocked, QuotaBlocked)

    def test_kick_refused_and_replayed(self):
        started = []
        self.assertFalse(bgwork.kick(self.root, "extract", ["--ref", "raw:x:3"], spawn=lambda c, **k: started.append(c)))
        self.assertEqual(started, [])
        self.assertEqual(bgjobs.skipped(self.root)["replay"], [["extract", "--ref", "raw:x:3"]])
        quota.clear(self.root)
        real = bgwork.kick
        with mock.patch.object(bgwork, "kick", lambda root, name, args, spawn=None, now=None:
                               real(root, name, args, spawn=lambda c, **k: started.append(c), now=now)):
            self.assertEqual(catchup._replay(self.root), "重起 1/1 个")
        self.assertEqual(started[0][-2:], ["--ref", "raw:x:3"])
        self.assertEqual(bgjobs.skipped(self.root)["replay"], [])

    def test_essential_kick_runs_while_blocked(self):
        started = []
        self.assertTrue(bgwork.kick(self.root, "mail", (), spawn=lambda c, **k: started.append(c)))
        self.assertEqual(len(started), 1)
        self.assertTrue((self.root / "logs" / "mail.log").exists())

    def test_kick_lock(self):
        quota.clear(self.root)
        got = []
        self.assertTrue(bgwork.kick(self.root, "season-note", (), spawn=lambda c, **k: got.append(c)))
        self.assertFalse(bgwork.kick(self.root, "season-note", (), spawn=lambda c, **k: got.append(c)))
        bgwork.release(self.root, "season-note")
        self.assertTrue(bgwork.kick(self.root, "season-note", (), spawn=lambda c, **k: got.append(c)))
        self.assertEqual(len(got), 2)

    def test_unknown_kick_name_fails(self):
        with self.assertRaises(KeyError):
            bgwork.kick(self.root, "没登记的", ())

    def test_catchup_reruns_skipped_timer_job(self):
        bgjobs.note_skipped(self.root, "english-lesson")
        self.assertIn("rerun:english", bgjobs.catchups(self.root))
        ran = []
        with mock.patch.object(jobs, "run", lambda data, name, cmd, runner=None: ran.append((name, cmd)) or 0):
            self.assertEqual(catchup._rerun(self.root, "english"), "补完")
        self.assertEqual(ran[0][0], "english")
        self.assertIn("ethan.english", ran[0][1])
        catchup._done(self.root, "rerun:english")
        self.assertEqual(bgjobs.catchups(self.root), [])
        self.assertEqual(json.loads((self.root / bgjobs.SKIPPED).read_text())["purposes"], {})


    def test_jobs_record_quota_stop_as_not_broken(self):
        out = SimpleNamespace(returncode=1, stdout="", stderr="Traceback\nethan.core.router.QuotaBlocked: 每晚记忆整理：在用付费额度")
        jobs.run(self.root, "memory", ["x"], runner=lambda *a, **k: out)
        rec = jobs.load(self.root)["memory"]
        self.assertTrue(rec["ok"])
        self.assertIn("付费额度停跑", rec["tail"])


if __name__ == "__main__":
    unittest.main()
