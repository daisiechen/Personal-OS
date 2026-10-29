import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED = ["README.md", "AGENTS.md", "CLAUDE.md", ".gitignore", "docs/README.md"]
LINK = re.compile(r"\]\(([^)]+)\)")
SECRET = re.compile(
    r"(sk-ant-[A-Za-z0-9_-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----|\b\d{8,10}:[A-Za-z0-9_-]{35}\b)"
)


def markdown_files():
    return [p for p in ROOT.rglob("*.md") if ".git" not in p.parts]


class RepoDocsTest(unittest.TestCase):
    def test_required_files_exist(self):
        for name in REQUIRED:
            self.assertTrue((ROOT / name).is_file(), name)

    def test_claude_md_imports_agents(self):
        self.assertIn("@AGENTS.md", (ROOT / "CLAUDE.md").read_text(encoding="utf-8"))

    def test_relative_links_resolve(self):
        for md in markdown_files():
            if md.relative_to(ROOT).parts[0] == "public":  # 公开版素材，链接要导出到 Personal-OS 以后才对得上
                continue
            for target in LINK.findall(md.read_text(encoding="utf-8")):
                if re.match(r"[a-z]+:", target) or target.startswith("#"):
                    continue
                path = (md.parent / target.split("#")[0]).resolve()
                self.assertTrue(path.exists(), f"{md.relative_to(ROOT)} -> {target}")

    def test_docs_index_lists_every_doc(self):
        index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
        for md in (ROOT / "docs").rglob("*.md"):
            if md.name == "README.md":
                continue
            self.assertIn(md.relative_to(ROOT / "docs").as_posix(), index)

    def test_persona_has_core_sections(self):
        persona = (ROOT / "persona/ethan.md").read_text(encoding="utf-8")
        for heading in ["## 身份档案", "## 性格", "## 关系定位与安全感准则", "## 边界", "## 示例对话"]:
            self.assertIn(heading, persona)

    def test_no_secrets_committed(self):
        for path in ROOT.rglob("*"):
            if ".git" in path.parts or not path.is_file() or path.suffix == ".pyc":
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if path.name == "test_repo_docs.py":
                continue
            self.assertIsNone(SECRET.search(text), str(path.relative_to(ROOT)))


if __name__ == "__main__":
    unittest.main()
