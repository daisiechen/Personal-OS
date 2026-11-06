"""别的 agent / AI 的记忆进来（ontology-plan 1.8，借鉴清单第 1、2 条）：
Claude Code 的自动记忆只交关于她的、去重、算转述；别的 AI 的“导出提示词”格式认得出、导成假设进“待你定”、不当她的原话。"""

import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from ethan import agentexport as ax
from ethan import interviews, lifebase, mepage
from ethan.core.agent import Ethan
from ethan.core.ledger import Ledger
from ethan.core.router import Router, Settings
from ethan.memory import MemoryStore

os.environ.setdefault("ETHAN_NO_KICK", "1")  # 测试里不真起后台整理进程

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "operations" / "agent-export.md"


class FakeClient:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    def interview(self, session, scene, text, fmt=None, at=None, agent=None):
        self.calls.append({"session": session, "scene": scene, "text": text, "fmt": fmt, "agent": agent})
        return {"ok": self.ok} if self.ok else {"ok": False, "error": "boom"}


MEMORY_USER = """---
name: 她的写作习惯
description: 回复风格
type: feedback
---
回复写短，只说结论；不喜欢先交一份计划。
Why: 她觉得长回复浪费时间
"""

MEMORY_PROJECT = """---
name: 项目约定
type: project
---
- 测试入口：`python3 -m unittest discover -s tests`
- 合并前先问 Mia 同意，她不喜欢被突然上线新功能
- def foo(): return 1
"""

PROJECT_CLAUDE = """# 项目协作规则

- 所有回复使用中文。
- 每次改动后创建 git commit。
- 验证入口：`python3 -m unittest discover -s tests -v`
- 她平时在 Mac 上用 Claude 桌面 App，Mac 休眠时才用云端。
- 她的邮箱是 someone@example.com，有事先发邮件给她。

```sh
git push origin main
```
"""


class ClaudeExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "claude"
        self.proj = self.tmp / "Personal-OS"
        self.proj.mkdir()
        (self.proj / "CLAUDE.md").write_text(PROJECT_CLAUDE, encoding="utf-8")
        pdir = self.home / "projects" / "-Users-mia-Personal-OS"
        (pdir / "memory").mkdir(parents=True)
        (pdir / "memory" / "writing.md").write_text(MEMORY_USER, encoding="utf-8")
        (pdir / "memory" / "project.md").write_text(MEMORY_PROJECT, encoding="utf-8")
        (pdir / "abc.jsonl").write_text(json.dumps({"type": "summary"}) + "\n" +
                                        json.dumps({"type": "user", "cwd": str(self.proj)}) + "\n", encoding="utf-8")
        (self.home / "agent-memory" / "reviewer").mkdir(parents=True)
        (self.home / "agent-memory" / "reviewer" / "MEMORY.md").write_text(
            "- The user prefers plain Chinese explanations over jargon\n- Run lint before commit\n", encoding="utf-8")
        (self.home / "settings.json").write_text(json.dumps({"env": {"ETHAN_CLOUD_TOKEN": "secret"}}), encoding="utf-8")
        self.codex = self.tmp / "codex"
        self.codex.mkdir()
        self.kw = dict(state_path=self.tmp / "state.json", archive_dir=self.tmp / "archive", key=self.tmp / "no.key",
                       out_dir=self.tmp / "out", settings=self.tmp / "none.json", codex=False, claude=True,
                       claude_home=self.home)

    def sent_text(self, c):
        return "\n".join(x["text"] for x in c.calls)

    def test_files_found_from_cwd(self):
        files = ax.claude_files(self.home)
        names = [p.name for p in files["memory"]]
        self.assertIn("writing.md", names)
        self.assertIn("MEMORY.md", names)
        self.assertNotIn("settings.json", names)
        self.assertEqual(files["docs"], [self.proj / "CLAUDE.md"])

    def test_only_about_her(self):
        lines = ax.about_her(PROJECT_CLAUDE)
        self.assertTrue(any("Mac 上用 Claude 桌面 App" in ln for ln in lines))
        joined = "\n".join(lines)
        for code_rule in ("所有回复使用中文", "git commit", "unittest", "git push"):  # 代码规范、命令不交
            self.assertNotIn(code_rule, joined)
        self.assertNotIn("someone@example.com", joined)  # 去掉邮箱
        user = ax.about_her(MEMORY_USER)  # type: feedback 整份都算关于她
        self.assertIn("回复写短，只说结论；不喜欢先交一份计划。", user)
        proj = ax.about_her(MEMORY_PROJECT)
        self.assertEqual(proj, ["合并前先问 Mia 同意，她不喜欢被突然上线新功能"])

    def test_send_as_relay_then_only_new(self):
        c = FakeClient()
        res = ax.run(self.codex, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertGreater(res["claude"]["sent"], 0)
        self.assertEqual(res["sent"], 0)  # 没有【她】的行：一句都不算她说的
        for call in c.calls:
            self.assertEqual((call["fmt"], call["agent"]), ("transcript", "Claude Code"))
            self.assertTrue(call["session"].startswith("Claude Code 记忆（"))
            self.assertIn("agent 转述", call["scene"])
            self.assertNotIn("【她】", call["text"])
            body = [ln for ln in call["text"].split("\n") if not ln.startswith("〔")]
            self.assertTrue(all(ln.startswith("【Claude Code】") for ln in body))
        text = self.sent_text(c)
        self.assertIn("The user prefers plain Chinese explanations", text)
        self.assertNotIn("Run lint", text)
        self.assertNotIn("secret", text)
        self.assertTrue(res["claude"]["archive"])  # ~/.claude 里的记忆原样存档
        import tarfile
        with tarfile.open(res["claude"]["archive"]) as tar:
            names = tar.getnames()
        self.assertIn("claude/agent-memory/reviewer/MEMORY.md", names)
        self.assertNotIn("claude/settings.json", names)
        # 重跑：没有新的就不交
        n = len(c.calls)
        ax.run(self.codex, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual(len(c.calls), n)
        # 加了一行：只交这一行
        with (self.proj / "CLAUDE.md").open("a", encoding="utf-8") as f:
            f.write("- 她早上不喜欢开会，下午精神最好。\n")
        ax.run(self.codex, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertEqual(len(c.calls), n + 1)
        self.assertIn("她早上不喜欢开会", c.calls[-1]["text"])
        self.assertNotIn("Mac 上用 Claude 桌面 App", c.calls[-1]["text"])

    def test_failed_send_retried(self):
        ax.run(self.codex, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=FakeClient(ok=False), **self.kw)
        self.assertEqual(ax.load_state(self.kw["state_path"])["claude_seen"], {})
        c = FakeClient()
        ax.run(self.codex, "send", {"ETHAN_CLOUD_TOKEN": "t"}, client=c, **self.kw)
        self.assertTrue(c.calls)

    def test_dry_run_and_no_token(self):
        res = ax.run(self.codex, "dry-run", {}, client=FakeClient(), **self.kw)
        self.assertGreater(res["claude"]["lines"], 0)
        self.assertFalse(self.kw["state_path"].exists())
        res = ax.run(self.codex, "send", {}, client=FakeClient(), **self.kw)
        self.assertTrue(res["written"])
        self.assertEqual(ax.load_state(self.kw["state_path"])["claude_seen"], {})  # 没交不记

    def test_cli_default_on_and_plist(self):
        text = ax.plist_text("/usr/bin/python3", Path("/repo"))
        self.assertIn("<string>--codex</string><string>--claude</string><string>--send</string>", text)
        seen = {}
        orig = ax.run
        try:
            ax.run = lambda *a, **k: seen.update(k) or {"errors": []}
            ax.main(["--codex", "--dry-run", "--state-file", str(self.tmp / "s.json")], env={})
            self.assertTrue(seen["claude"])
            ax.main(["--codex", "--no-claude", "--dry-run", "--state-file", str(self.tmp / "s.json")], env={})
            self.assertFalse(seen["claude"])
        finally:
            ax.run = orig


def doc_example(lang: str) -> str:
    """文档里的提示词：取出格式那两行当成一个 AI 照着回的样子。"""
    text = DOC.read_text(encoding="utf-8")
    block = re.findall(r"```text\n(.*?)```", text, re.S)
    return block[0] if lang == "zh" else block[1]


ZH = """好的，下面是我记得的：

【AI 记忆导出】来自：ChatGPT
- [她说过][经历] 她在示例商学院读 进修，2025 年入学。（2025-09）
- [推测][偏好] 她喜欢极简、留白多的界面设计。（2025 年）
- [她说过][习惯] 她习惯晚上很晚才开始写代码。（不确定）
- [推测][偏好] 她喜欢极简、留白多的界面设计。（2025 年）
"""

EN = """**[AI MEMORY EXPORT] from: Gemini**
1. \\[said\\]\\[health\\] She goes running three times a week. (2024-05)
2. [inferred][relationships] She is close to her younger sister. (unknown)
- [something][x] not a valid item (2024)
"""


class MemoryExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "data")
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def test_parse_zh_en(self):
        zh = interviews.parse_memory_export(ZH)
        self.assertEqual(zh["ai"], "ChatGPT")
        self.assertEqual([i["kind"] for i in zh["items"]], ["said", "guess", "said"])  # 重复的一条只算一次
        self.assertEqual(zh["items"][0]["dim"], "abilities")
        self.assertEqual(zh["items"][0]["since"], "2025-09-01")
        self.assertEqual(zh["items"][0]["text"], "她在示例商学院读 进修，2025 年入学")
        self.assertIsNone(zh["items"][2]["since"])
        en = interviews.parse_memory_export(EN)
        self.assertEqual(en["ai"], "Gemini")
        self.assertEqual([(i["kind"], i["dim"]) for i in en["items"]], [("said", "body"), ("guess", "relationships")])

    def test_not_recognized(self):
        for text in ("我今天去跑步了", "- [她说过][经历] 没有抬头行（2024）", "【AI 记忆导出】来自：ChatGPT\n只有抬头没有条目", ""):
            self.assertFalse(interviews.is_memory_export(text), text)

    def test_doc_prompts_match_parser(self):
        for lang, ai in (("zh", "豆包"), ("en", "Grok")):
            block = doc_example(lang)
            fmt = [ln for ln in block.split("\n") if ln.startswith(("【AI 记忆导出】", "[AI MEMORY EXPORT]", "- ["))]
            self.assertEqual(len(fmt), 3, lang)
            filled = "\n".join(re.sub(r"[（(]写你的产品名.*|\(your product name.*", ai, ln).replace("一件事", f"第 {k} 件事")
                                .replace("one thing", f"thing number {k}") for k, ln in enumerate(fmt))
            got = interviews.parse_memory_export(filled)
            self.assertEqual(got["ai"], ai)
            self.assertEqual([i["kind"] for i in got["items"]], ["said", "guess"])

    def test_import_as_hypothesis_not_her_words(self):
        res = interviews.receive_memory_export(self.store, ZH, via="测试")
        self.assertEqual((res["ok"], res["added"], res["said"]), (True, 3, 2))
        facts = [dict(r) for r in self.db.conn.execute("SELECT * FROM facts ORDER BY id")]
        self.assertEqual(len(facts), 3)
        for f in facts:
            self.assertEqual((f["kind"], f["source_kind"], f["visibility"]), ("hypothesis", "agent_relay", "private"))
            self.assertTrue(f["statement"].startswith("ChatGPT"))
            self.assertIn("只当资料", f["context"])
            self.assertLess(f["confidence"], 0.5)
        ev = self.db.conn.execute("SELECT source, text FROM evidence").fetchall()
        self.assertEqual([r["source"] for r in ev], ["agent"])  # 原文是 agent 转述，不是访谈（她的原话）
        self.assertIn("不是她的原话", ev[0]["text"])
        self.assertEqual(interviews.pending(self.db), [])  # 不进访谈整理（那边按她的原话提取）
        # “我”页“待你定”：每条一题
        items = [x for x in mepage.pending_items(self.db) if x["kind"] == "confirm"]
        self.assertEqual(len(items), 3)
        self.assertIn("ChatGPT", items[0]["question"])
        self.assertIn("转述", items[0]["options"][0]["src"])
        # 她点“不对”：作废；再贴一遍不再记回来、不再问
        mepage.resolve(self.db, items[0]["id"], "no")
        again = interviews.receive_memory_export(self.store, ZH + "\n", via="测试")
        self.assertEqual((again["added"], again["skipped"]), (0, 3))
        self.assertEqual(len([x for x in mepage.pending_items(self.db) if x["kind"] == "confirm"]), 2)

    def test_redacts_contacts(self):
        text = "【AI 记忆导出】来自：豆包\n- [她说过][处境] 她的邮箱是 someone@example.com，电话 +44 7700 900123。（2025）"
        interviews.receive_memory_export(self.store, text)
        blob = json.dumps([dict(r) for r in self.db.conn.execute("SELECT statement, detail, context FROM facts")], ensure_ascii=False)
        blob += self.db.conn.execute("SELECT text FROM evidence").fetchone()[0]
        self.assertNotIn("someone@example.com", blob)
        self.assertNotIn("7700", blob)

    def test_interview_endpoint_recognizes(self):
        code, res = interviews.receive(self.store, {"session": "某会话", "scene": "", "text": EN})
        self.assertEqual((code, res["added"]), (200, 2))
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM evidence WHERE source = 'interview'").fetchone()[0], 0)

    def test_chat_paste_not_her_words(self):
        class Boom:
            name = "boom"

            def complete(self, *a, **k):
                raise AssertionError("认出导出格式就不该调模型")
        ethan = Ethan(self.store, Router(Boom(), Ledger(self.root / "ledger"), Settings(self.root / "settings.json")), persona="人设")
        out = ethan.reply("telegram", ZH)
        self.assertIn("ChatGPT", out["bubbles"][0])
        self.assertIn("待你定", out["bubbles"][0])
        raw = [json.loads(ln) for f in (self.root / "data").rglob("*.jsonl") for ln in f.read_text(encoding="utf-8").splitlines()]
        hers = [r["text"] for r in raw if r.get("role") == "mia"]
        self.assertEqual(len(hers), 1)
        self.assertNotIn("示例商学院", hers[0])  # 聊天记录里不留原文，每晚整理不会当成她说的
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM facts WHERE source_kind = 'agent_relay'").fetchone()[0], 3)

    def test_workbench_action(self):
        from ethan.workbench.api import Workbench
        wb = Workbench.__new__(Workbench)
        wb.store, wb.db = self.store, self.db
        res = wb.do_me_import_ai({"text": ZH}, None)
        self.assertEqual((res["ok"], res["added"]), (True, 3))
        self.assertFalse(wb.do_me_import_ai({"text": "随便写的"}, None)["ok"])


if __name__ == "__main__":
    unittest.main()
