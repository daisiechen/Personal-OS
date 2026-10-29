import subprocess
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan.memory import MemoryStore
from ethan.vault import BOARD, sync


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


class VaultTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.repo = root / "vault"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "ethan@example.com")
        git(self.repo, "config", "user.name", "小伴")
        db = self.store.db
        vision = db.add_goal("这辈子干一件大事", "vision", status="active")
        stage = db.add_goal("创业项目有订单", "stage", parent_id=vision, status="active",
                            next_step="约代理商", due="2026-12-31")
        db.update_goal(stage, note="见了第一个代理商")
        db.add_task("帮她查伦敦 physio", due="2026-10-22", promised_by_ethan=True)
        db.upsert_person("小雨", circle="挚友", birthday="10-25")
        db.add_wish("想学网球", "阿明常打")
        db.add_learning_item("english", "right up my alley", "正合我意", today=date(2026, 10, 21))
        self.store.wiki.write("modules/health", "健康总览", "先矫正后增肌")
        self.today = date(2026, 10, 22)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def read(self, rel):
        return (self.repo / rel).read_text(encoding="utf-8")

    def test_export_board_and_wiki(self):
        result = sync(self.store, self.repo, self.today)
        self.assertTrue(result["committed"])
        overview = self.read(f"{BOARD}/总览.md")
        for text in ("创业项目有订单", "约代理商", "[小伴 答应的] 帮她查伦敦 physio", "小雨：快过生日",
                     "复习的单词句子：1 条", "心愿：1 条"):
            self.assertIn(text, overview)
        tree = self.read(f"{BOARD}/目标树.md")
        self.assertIn("  - [进行中] 阶段：创业项目有订单", tree)
        self.assertIn("见了第一个代理商", tree)
        self.assertIn("先矫正后增肌", self.read("wiki/modules/health.md"))
        self.assertIn("right up my alley", self.read(f"{BOARD}/学习.md"))
        self.assertFalse(sync(self.store, self.repo, self.today)["committed"])

    def test_mia_edits_flow_back_and_obsidian_settings_untouched(self):
        sync(self.store, self.repo, self.today)
        (self.repo / ".obsidian").mkdir()
        (self.repo / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
        page = self.repo / "wiki" / "modules" / "health.md"
        page.write_text(page.read_text(encoding="utf-8").replace("先矫正后增肌", "先矫正，周三练腿"),
                        encoding="utf-8")
        result = sync(self.store, self.repo, self.today)
        self.assertEqual(result["pulled_back"], ["modules/health.md"])
        self.assertEqual(self.store.wiki.read("modules/health")["body"], "先矫正，周三练腿")
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                capture_output=True, text=True).stdout
        self.assertIn(".obsidian/", status)

    def test_push_to_empty_remote_then_pull_mia_edit(self):
        root = Path(self.tmp.name)
        remote = root / "remote.git"
        git(root, "init", "-q", "--bare", str(remote))
        server = root / "server"
        git(root, "clone", "-q", str(remote), str(server))
        for repo in (server,):
            git(repo, "config", "user.email", "ethan@example.com")
            git(repo, "config", "user.name", "小伴")
        self.assertTrue(sync(self.store, server, self.today, push=True)["committed"])
        mac = root / "mac"
        git(root, "clone", "-q", str(remote), str(mac))
        git(mac, "config", "user.email", "d@example.com")
        git(mac, "config", "user.name", "Mia")
        page = mac / "wiki" / "modules" / "health.md"
        page.write_text(page.read_text(encoding="utf-8").replace("先矫正后增肌", "周三练腿"), encoding="utf-8")
        git(mac, "commit", "-qam", "edit")
        git(mac, "push", "-q")
        result = sync(self.store, server, self.today, push=True)
        self.assertEqual(result["pulled_back"], ["modules/health.md"])
        self.assertEqual(self.store.wiki.read("modules/health")["body"], "周三练腿")

    def test_board_pages_are_regenerated(self):
        sync(self.store, self.repo, self.today)
        (self.repo / BOARD / "心愿.md").write_text("乱改", encoding="utf-8")
        sync(self.store, self.repo, self.today)
        self.assertIn("想学网球", self.read(f"{BOARD}/心愿.md"))


if __name__ == "__main__":
    unittest.main()
