"""合并冲突的标记不许留在仓库里（11-06 changelog 里留了一组，部署和测试都没拦住）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NoConflictMarkersTest(unittest.TestCase):
    def test_no_conflict_markers(self):
        import subprocess
        try:  # 只查 git 管着的文件：服务器仓库里 .claude/worktrees 下有别的会话的工作副本，不归这里管（11-06 部署因此退回）
            files = [ROOT / f for f in subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.split("\n") if f]
        except (OSError, subprocess.CalledProcessError):
            files = [p for p in ROOT.rglob("*") if not ({".git", ".claude", "__pycache__"} & set(p.relative_to(ROOT).parts))]
        bad = []
        for p in files:
            if not p.is_file() or p.suffix not in (".md", ".py", ".js", ".css", ".html", ".json", ".txt"):
                continue
            for n, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if line.startswith(("<<<<<<< ", ">>>>>>> ")) or line == "=======" and p.suffix != ".md":
                    bad.append(f"{p.relative_to(ROOT)}:{n}")
        self.assertEqual(bad, [], "留着合并冲突的标记")


if __name__ == "__main__":
    unittest.main()
