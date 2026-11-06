"""Codex 记忆和聊天导出（ontology-plan 1.8，ethan/agentexport.py）：两种格式、去掉注入块、不重复交、没钥匙只生成文件、去联系方式。"""

import json
import tempfile
import unittest
from pathlib import Path

from ethan import agentexport as ax

INJ_ENV = "<environment_context>\n  <cwd>/Users/x/proj</cwd>\n</environment_context>"
INJ_AGENTS = "# AGENTS.md instructions for /Users/x/proj\n\n<INSTRUCTIONS>\n回复用中文\n</INSTRUCTIONS>"
SAID = "我以前在伦敦读书的时候特别喜欢早上去跑步"


def new_line(role, text, kind="input_text"):
    return json.dumps({"timestamp": "2026-11-01T10:00:00Z", "type": "response_item",
                       "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]}},
                      ensure_ascii=False)


def old_line(role, text, kind="input_text"):
    return json.dumps({"type": "message", "role": role, "content": [{"type": kind, "text": text}]}, ensure_ascii=False)


class FakeClient:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def interview(self, session, scene, text, fmt=None, at=None, agent=None):
        self.calls.append((session, scene, text))
        self.kw = {"fmt": fmt, "agent": agent}
        return {"ok": self.ok, "evidence": len(self.calls)} if self.ok else {"ok": False, "error": "boom"}


class ParseTest(unittest.TestCase):
    def test_new_format_and_injected_removed(self):
        lines = [json.dumps({"type": "session_meta", "payload": {"id": "x"}}),
                 new_line("user", INJ_AGENTS), new_line("user", INJ_ENV),
                 new_line("assistant", "好的，我先看一下代码。", "output_text"),
                 new_line("user", SAID), "不是 JSON", json.dumps({"type": "event_msg", "payload": {"type": "user_message"}})]
        turns = ax.parse_turns(lines)
        self.assertEqual([(t["role"], t["text"]) for t in turns], [("assistant", "好的，我先看一下代码。"), ("user", SAID)])

    def test_old_format(self):
        turns = ax.parse_turns([old_line("user", INJ_ENV + "\n" + SAID), old_line("assistant", "收到", "output_text")])
        self.assertEqual([t["text"] for t in turns], [SAID, "收到"])

    def test_transcript_both_sides_agent_cut_500(self):  # 别人跟我的对话也是我的记忆
        turns = [{"role": "user", "text": SAID}, {"role": "assistant", "text": "建议你" + "多休息" * 300},
                 {"role": "assistant", "text": "```\nprint(1)\n```"}, {"role": "user", "text": "好的"}]
        lines = ax.transcript_lines(turns, "Codex", ax.AGENT_MAX)
        self.assertEqual(lines[0], "【她】" + SAID)
        self.assertTrue(lines[1].startswith("【Codex】建议你"))
        self.assertLessEqual(len(lines[1]), len("【Codex】") + ax.AGENT_MAX + len(ax.LONG_NOTE))
        self.assertEqual(len(lines), 2)  # 纯代码、点头不交
        parts = ax.pack_transcript(lines * 50, "〔Codex 会话 2026-11-01〕", size=3000)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(p.startswith("〔Codex 会话 2026-11-01〕\n【") and len(p) <= 3000 for p in parts))

    def test_filters(self):
        self.assertEqual(ax.clean("可以，继续"), "")  # 点头
        self.assertEqual(ax.clean("python3 -m unittest discover -s tests"), "")  # 命令
        self.assertEqual(ax.clean("~/projects/ethan/agentexport.py"), "")  # 路径
        self.assertEqual(ax.clean("def f(x):\n    return x + 1"), "")  # 代码
        self.assertEqual(ax.clean("```\nTraceback ...\n```"), "")  # 只有代码块
        self.assertTrue(ax.clean("I usually work late at night, it is a bad habit"))  # 英文的话留下
        long = ax.clean("我" + "很长的日志" * 1000)
        self.assertLessEqual(len(long), ax.MAX_CHARS + len(ax.CUT_NOTE))
        self.assertTrue(long.endswith(ax.CUT_NOTE))

    def test_redact(self):
        out = ax.clean("我以前常用的邮箱是 someone@example.com，电话 +44 7700 900123")
        self.assertNotIn("someone@example.com", out)
        self.assertNotIn("7700", out)
        self.assertIn("［已去掉］", out)


class RunTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "codex"
        d = self.home / "sessions" / "2026" / "11" / "01"
        d.mkdir(parents=True)
        (self.home / "AGENTS.md").write_text("全局说明", encoding="utf-8")
        (self.home / "auth.json").write_text("{\"token\": \"secret\"}", encoding="utf-8")
        self.sess = d / "rollout-2026-11-01T10-00-00-abc.jsonl"
        self.sess.write_text("\n".join([new_line("user", INJ_ENV), new_line("user", SAID)]) + "\n", encoding="utf-8")
        self.kw = dict(state_path=self.tmp / "state.json", archive_dir=self.tmp / "archive", key=self.tmp / "no.key",
                       out_dir=self.tmp / "out", settings=self.tmp / "none.json")

    def test_send_once_then_only_new(self):
        c = FakeClient()
        res = ax.run(self.home, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual(res["sent"], 1)
        session, scene, text = c.calls[0]
        self.assertEqual(session, "Codex 会话（2026-11-01）")
        self.assertEqual(scene, ax.SCENE)
        self.assertEqual(text, "〔Codex 会话 2026-11-01〕\n【她】" + SAID)
        self.assertEqual(c.kw, {"fmt": "transcript", "agent": "Codex"})
        self.assertNotIn("environment_context", text)
        # 存档里有会话和 AGENTS.md，没有 auth.json
        import tarfile
        with tarfile.open(res["archive"]) as tar:
            names = tar.getnames()
        self.assertIn("codex/AGENTS.md", names)
        self.assertIn("codex/sessions/2026/11/01/" + self.sess.name, names)
        self.assertNotIn("codex/auth.json", names)
        # 重跑：没变就不交、不再存档
        res2 = ax.run(self.home, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual((res2["sent"], res2["archive"], len(c.calls)), (0, "", 1))
        # 会话续写：只交新增的
        with self.sess.open("a", encoding="utf-8") as f:
            f.write(new_line("user", "我小时候住在海边，周末总是去看海") + "\n")
        import os
        os.utime(self.sess, (1e9, 1e9))
        ax.run(self.home, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual(len(c.calls), 2)
        self.assertNotIn(SAID, c.calls[1][2])
        self.assertIn("【她】我小时候住在海边", c.calls[1][2])

    def test_old_progress_converted(self):  # 旧进度数的是她说的段数：换算成整段对话里的位置，不重交
        self.sess.write_text("\n".join([new_line("user", SAID), new_line("assistant", "记下了，跑步是好习惯", "output_text"),
                                         new_line("user", "我小时候住在海边，周末总是去看海")]) + "\n", encoding="utf-8")
        self.kw["state_path"].write_text(json.dumps({"archived": {}, "sessions": {str(self.sess): {"sig": "old", "sent": 1}}}))
        c = FakeClient()
        ax.run(self.home, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual(c.calls[0][2], "〔Codex 会话 2026-11-01〕\n【Codex】记下了，跑步是好习惯\n【她】我小时候住在海边，周末总是去看海")

    def test_failed_send_not_recorded(self):
        ax.run(self.home, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=FakeClient(ok=False), **self.kw)
        c = FakeClient()
        ax.run(self.home, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual(len(c.calls), 1)

    def test_no_token_only_writes_files(self):
        c = FakeClient()
        res = ax.run(self.home, "send", {}, client=c, **self.kw)
        self.assertEqual((res["sent"], c.calls), (0, []))
        self.assertEqual(len(res["written"]), 1)
        self.assertIn(SAID, Path(res["written"][0]).read_text(encoding="utf-8"))
        self.assertEqual(ax.load_state(self.kw["state_path"])["sessions"], {})  # 没交不记

    def test_dry_run_writes_nothing(self):
        res = ax.run(self.home, "dry-run", {"ETHAN_CLOUD_TOKEN": "t"}, client=FakeClient(), **self.kw)
        self.assertEqual(res["blocks"], 1)
        self.assertFalse(self.kw["state_path"].exists())
        self.assertFalse(self.kw["archive_dir"].exists())

    def test_archive_encrypted_with_key(self):
        import shutil
        if not shutil.which("openssl"):
            self.skipTest("没有 openssl")
        key = self.tmp / "a.key"
        key.write_text("k" * 32, encoding="utf-8")
        f = ax.archive([self.home / "AGENTS.md"], self.home, self.tmp / "arc", key)
        self.assertTrue(f.endswith(".tar.gz.enc"))
        self.assertNotIn("全局说明".encode(), Path(f).read_bytes())

    def test_token_from_claude_settings(self):
        s = self.tmp / "settings.json"
        s.write_text(json.dumps({"env": {"ETHAN_HOOK_TOKEN": "k"}}), encoding="utf-8")
        self.assertEqual(ax.token({}, s), "k")
        self.assertEqual(ax.token({"ETHAN_CLOUD_TOKEN": "c"}, s), "c")


class LaunchdTest(unittest.TestCase):
    def test_plist_weekly_sunday(self):
        text = ax.plist_text("/usr/bin/python3", Path("/repo"))
        self.assertIn("<key>Weekday</key><integer>0</integer><key>Hour</key><integer>20</integer>"
                      "<key>Minute</key><integer>13</integer>", text)
        self.assertIn("<string>--send</string>", text)
        self.assertIn("agentexport.log", text)

    def test_install_loads(self):
        calls = []
        plist = Path(tempfile.mkdtemp()) / "x.plist"
        out = ax.install_launchd(plist, lambda cmd, **kw: calls.append(cmd), "darwin")
        self.assertTrue(plist.exists())
        self.assertEqual(calls[-1], ["launchctl", "load", str(plist)])
        self.assertIn("20:13", out)


if __name__ == "__main__":
    unittest.main()
