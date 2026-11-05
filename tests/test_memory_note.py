"""全局说明：她的话交给 小伴 的记忆（她 2026-11-03 要“全局自动生效”），写进 ~/.claude/CLAUDE.md、~/.codex/AGENTS.md。"""

import tempfile
import unittest
from pathlib import Path

from ethan import setup_hook


class MemoryNoteTest(unittest.TestCase):
    def test_write_keep_her_content_and_replace_on_reinstall(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".claude" / "CLAUDE.md"
            p.parent.mkdir()
            p.write_text("# 我自己的规矩\n回复写短。\n", encoding="utf-8")
            setup_hook.install_memory_note(p)
            setup_hook.install_memory_note(p)  # 重装只换自己那段
            text = p.read_text(encoding="utf-8")
            self.assertIn("回复写短。", text)  # 她写的不动
            self.assertEqual(text.count(setup_hook.NOTE_BEGIN), 1)
            self.assertIn("/cloud/interview", text)
            self.assertNotIn("Bearer ey", text)  # 不写钥匙本身

    def test_empty_file_created(self):
        with tempfile.TemporaryDirectory() as d:
            p = setup_hook.install_memory_note(Path(d) / ".codex" / "AGENTS.md")
            self.assertTrue(p.read_text(encoding="utf-8").startswith(setup_hook.NOTE_BEGIN))


if __name__ == "__main__":
    unittest.main()
