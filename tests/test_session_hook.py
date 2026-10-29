import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ethan import agents, setup_hook
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

T0 = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


class SessionHookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.inbox = Inbox(self.store, {"grok": "g", "cloud": "c", "hook": "h"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, event, token="h", where="mac", now=T0):
        body = json.dumps({"session_id": "s1", "cwd": "/Users/d/Grab-in-London", **event}).encode()
        return self.inbox.handle(f"/cloud/agent/hook?where={where}", {"Authorization": f"Bearer {token}"}, body, now)[0]

    def test_hook_events_drive_state(self):
        self.assertEqual(self.post({"hook_event_name": "SessionStart"}, token="g"), 401)  # Grok 的钥匙不行
        self.assertEqual(self.post({"hook_event_name": "SessionStart"}), 200)
        self.assertEqual(self.post({"hook_event_name": "UserPromptSubmit", "prompt": "帮我修 Meals 列表错位\n还有头像"}, token="c"), 200)
        s = agents.load_sessions(self.root, T0)
        self.assertTrue(s["live"])
        self.assertEqual(s["sessions"][0]["title"], "Grab-in-London · 帮我修 Meals 列表错位 还有头像")
        self.assertEqual((s["sessions"][0]["state_label"], s["sessions"][0]["where_label"]), ("在干", "Mac"))
        self.post({"hook_event_name": "UserPromptSubmit", "prompt": "第二句不改标题"})
        self.post({"hook_event_name": "Stop", "last_assistant_message": "修好了。" * 100})
        s = agents.load_sessions(self.root, T0)["sessions"][0]
        self.assertEqual(s["state"], "completed")
        self.assertIn("帮我修", s["title"])
        self.assertLessEqual(len(s["summary"]), 120)
        self.post({"hook_event_name": "SessionEnd"})
        self.assertEqual(agents.load_sessions(self.root, T0)["sessions"], [])  # 结束的不显示
        self.assertEqual(self.inbox.handle_get("/cloud/agent/sessions", {"Authorization": "Bearer c"})[1]["sessions"], [])
        self.assertEqual(self.inbox.handle("/cloud/agent/hook", {"Authorization": "Bearer h"}, b"{}", T0)[0], 400)

    def test_project_name_from_worktree_and_tags_stripped(self):
        self.assertEqual(agents._project("/home/ethan/Personal-OS/.claude/worktrees/bridge-cse_01"), "Personal-OS")
        self.assertEqual(agents._project("/tmp/bridge-cse_01Vm4"), "")
        self.assertEqual(agents._project("/Users/d/约饭App"), "约饭App")
        self.post({"hook_event_name": "UserPromptSubmit", "prompt": "<cross-session-message from=\"x\">帮我看下</cross-session-message>"})
        self.assertTrue(agents.load_sessions(self.root, T0)["sessions"][0]["title"].endswith("· 帮我看下"))

    def test_old_sessions_are_dropped(self):
        self.post({"hook_event_name": "SessionStart"}, now=T0 - timedelta(days=5))
        body = json.dumps({"session_id": "s2", "hook_event_name": "SessionStart", "cwd": "/x/Mia.me"}).encode()
        self.inbox.handle("/cloud/agent/hook?where=cloud", {"Authorization": "Bearer h"}, body, T0)
        self.assertEqual([s["title"] for s in agents.load_sessions(self.root, T0)["sessions"]], ["Mia.me"])

    def test_install_is_idempotent_and_keeps_other_settings(self):
        path = self.root / "settings.json"
        path.write_text(json.dumps({"permissions": {"allow": ["Bash(ls:*)"]},
                                    "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}))
        setup_hook.install(path, "mac", "tok")
        setup_hook.install(path, "mac", "tok")
        s = json.loads(path.read_text())
        self.assertEqual(s["permissions"]["allow"], ["Bash(ls:*)"])
        self.assertEqual(len(s["hooks"]["Stop"]), 2)  # 她自己的钩子留着，我们的只有一份
        self.assertEqual(s["env"]["ETHAN_HOOK_TOKEN"], "tok")
        self.assertTrue(all(len(s["hooks"][e]) >= 1 for e in setup_hook.EVENTS))

    def test_hook_command_runs_quietly_without_token_and_guards_cloud(self):
        for where, env in (("cloud", {}), ("cloud", {"CLAUDE_CODE_REMOTE": "true"}), ("mac", {})):
            r = subprocess.run(["sh", "-c", setup_hook.command(where)], input=b"{}", env={"PATH": "/usr/bin:/bin", **env},
                               capture_output=True, timeout=10)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b"", b""))

    def test_repo_settings_have_cloud_hook_and_no_token(self):
        repo = json.loads((Path(__file__).parents[1] / ".claude" / "settings.json").read_text())
        cmd = repo["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
        self.assertIn("where=cloud", cmd)
        self.assertIn('CLAUDE_CODE_REMOTE" = true', cmd)
        self.assertNotIn("env", repo)


if __name__ == "__main__":
    unittest.main()


class AgentReportTest(SessionHookTest):
    def test_any_agent_can_report_and_codex_notify_installs(self):
        """2026-10-29：所有 agent 都汇总到团队页——Codex 等自己报到，和 Claude 钩子并排显示。"""
        body = json.dumps({"agent": "codex", "id": "th1", "project": "DemoApp", "title": "修登录页", "state": "working", "where": "mac"}).encode()
        self.assertEqual(self.inbox.handle("/cloud/agent/report", {"Authorization": "Bearer h"}, body, T0)[0], 401)  # 只认云端钥匙
        code, got = self.inbox.handle("/cloud/agent/report", {"Authorization": "Bearer c"}, body, T0)
        self.assertEqual((code, got["key"]), (200, "codex:th1"))
        s = agents.load_sessions(self.root, T0)["sessions"][0]
        self.assertEqual((s["agent"], s["title"], s["state"]), ("codex", "DemoApp · 修登录页", "working"))
        self.assertEqual(self.inbox.handle("/cloud/agent/report", {"Authorization": "Bearer c"}, b'{"agent": "codex"}', T0)[0], 400)
        home = self.root / "home"
        (home / ".codex").mkdir(parents=True)
        (home / ".codex" / "config.toml").write_text('model = "o3"\n[tui]\nx = 1\n')
        cfg = setup_hook.install_codex(home, "TOKEN")
        lines = cfg.read_text().splitlines()
        self.assertTrue(lines[0].startswith("notify = ") and "ethan_notify.py" in lines[0])  # 放在所有表前面
        setup_hook.install_codex(home, "TOKEN")
        self.assertEqual(sum("ethan_notify" in l for l in cfg.read_text().splitlines()), 1)  # 重装不重复
        self.assertEqual(oct((home / ".codex" / "ethan_token").stat().st_mode)[-3:], "600")
        self.assertTrue((home / ".codex" / "ethan_watch.py").read_text().startswith("#!/usr/bin/env python3"))  # 实时报到的小程序也装上
        self.assertEqual((home / ".codex" / "ethan_where").read_text().strip(), "mac")


class CodexChainTest(unittest.TestCase):
    def test_existing_notify_is_kept_and_chained(self):
        """她 Mac 上 Codex 原来就有 notify（2026-10-29）：换成我们的，原来的存下来接着调。"""
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text('model = "o3"\nnotify = ["bash", "-lc", "say hi"]\n')
            cfg = setup_hook.install_codex(home, "T")
            self.assertEqual(sum(l.startswith("notify") for l in cfg.read_text().splitlines()), 1)
            self.assertEqual(json.loads((home / ".codex" / "ethan_notify_next.json").read_text()), ["bash", "-lc", "say hi"])
            self.assertTrue((home / ".codex" / "config.toml.bak-ethan").exists())
            setup_hook.install_codex(home, "T")  # 重装不把自己当成“原来的”
            self.assertEqual(json.loads((home / ".codex" / "ethan_notify_next.json").read_text()), ["bash", "-lc", "say hi"])
            (home / ".codex" / "config.toml").write_text("notify = [\n 'a',\n]\n")
            self.assertIsNone(setup_hook.install_codex(home, "T"))  # 认不出的不改


class CodexSpoolTest(unittest.TestCase):
    def test_sandboxed_report_is_spooled_and_flushed_later(self):
        """Codex 桌面 App 的沙箱不让报到脚本联网：存进 spool，Mac 上的工作时长小程序替它发（2026-10-29）。"""
        from ethan import worktime
        with tempfile.TemporaryDirectory() as d:
            spool = Path(d) / "ethan_spool.jsonl"
            (Path(d) / "ethan_token").write_text("T\n")
            spool.write_text('{"agent": "codex", "id": "t1"}\n{"agent": "codex", "id": "t2"}\n', encoding="utf-8")
            sent = []

            class Resp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return None

            def opener(req, timeout):
                if b"t2" in req.data:
                    raise OSError("断网")
                sent.append(req.get_header("Authorization"))
                return Resp()
            self.assertEqual(worktime.flush_codex(opener, spool), 1)
            self.assertEqual(sent, ["Bearer T"])
            self.assertIn("t2", spool.read_text())  # 没发出去的留着下次


class CodexWatchTest(unittest.TestCase):
    def test_live_state_from_rollout_files(self):
        """Codex 实时报到（2026-10-29）：从会话记录看出在干还是做完了，状态变了才报，旧服务器的标旧服务器。"""
        import time as _t
        from ethan import codex_watch
        with tempfile.TemporaryDirectory() as d:
            home = Path(d)
            (home / "ethan_token").write_text("T\n")
            (home / "ethan_where").write_text("seoul\n")
            day = home / "sessions" / "2026" / "10" / "29"
            day.mkdir(parents=True)
            f = day / "rollout-2026-10-29T10-00-00-abc123.jsonl"
            ev = lambda **p: json.dumps({"type": "event_msg", "payload": p}, ensure_ascii=False)
            f.write_text("\n".join([json.dumps({"type": "session_meta", "payload": {"id": "abc123", "cwd": "/srv/DemoApp"}}),
                                    ev(type="user_message", message="修一下登录页"), ev(type="task_started")]) + "\n")
            sent = []

            class Resp:
                status = 200
                def __enter__(self): return self
                def __exit__(self, *a): return None

            opener = lambda req, timeout: sent.append(json.loads(req.data)) or Resp()
            now = _t.time()
            self.assertEqual(codex_watch.once(home, now, opener), 1)
            self.assertEqual({k: sent[-1][k] for k in ("id", "state", "where", "project", "title")},
                             {"id": "abc123", "state": "working", "where": "seoul", "project": "DemoApp", "title": "修一下登录页"})
            self.assertEqual(codex_watch.once(home, now + 30, opener), 0)  # 没变不重复报
            with f.open("a") as w:
                w.write(ev(type="task_complete", last_agent_message="改好了，测试都过了") + "\n")
            self.assertEqual(codex_watch.once(home, now + 60, opener), 1)
            self.assertEqual((sent[-1]["state"], sent[-1]["summary"]), ("review_ready", "改好了，测试都过了"))
            body = json.dumps({"agent": "codex", "id": "x", "where": "seoul", "state": "working"}).encode()
            agents.record_report(home, json.loads(body))
            self.assertEqual(agents.load_sessions(home)["sessions"][0]["where_label"], "另一台服务器")
