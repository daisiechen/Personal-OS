import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ethan.backup import ARCHIVE_NAME, backup, decrypt, encrypt, restore
from ethan.memory import MemoryStore


@unittest.skipUnless(shutil.which("openssl") and shutil.which("git"), "需要 openssl 和 git")
class BackupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data, self.repo, self.key = root / "data", root / "backup-repo", root / "backup.key"
        self.key.write_text(os.urandom(24).hex())
        self.key.chmod(0o600)
        store = MemoryStore(self.data)
        store.db.add_goal("创业项目跑出订单", "stage", status="active")
        store.wiki.write("about-mia", "关于 Mia", "喜欢绿色")
        store.raw.append("telegram", "mia", "在吗")
        store.close()
        self.repo.mkdir()
        env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@t"}
        os.environ.update(env)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_encrypt_roundtrip_and_wrong_key(self):
        blob = encrypt(b"secret memory", self.key)
        self.assertNotIn(b"secret memory", blob)
        self.assertEqual(decrypt(blob, self.key), b"secret memory")
        wrong = Path(self.tmp.name) / "wrong.key"
        wrong.write_text("nope")
        try:  # 错钥匙通常报错；偶尔（约 1/256）填充碰巧合法会解出乱码，也算解不开
            self.assertNotEqual(decrypt(blob, wrong), b"secret memory")
        except RuntimeError:
            pass

    def test_backup_commits_and_restores(self):
        self.assertTrue(backup(self.data, self.repo, self.key))
        archive = self.repo / ARCHIVE_NAME
        self.assertNotIn("喜欢绿色".encode(), archive.read_bytes())
        target = Path(self.tmp.name) / "restored"
        restore(archive, self.key, target)
        store = MemoryStore(target)
        self.assertEqual(store.db.active_goals()[0]["title"], "创业项目跑出订单")
        self.assertEqual(store.wiki.read("about-mia")["body"], "喜欢绿色")
        self.assertEqual(store.raw.recent(1)[0]["text"], "在吗")
        store.close()

    def test_first_push_to_empty_remote(self):
        remote = Path(self.tmp.name) / "remote.git"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        clone = Path(self.tmp.name) / "clone"
        subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
        self.assertTrue(backup(self.data, clone, self.key, push=True))
        log = subprocess.run(["git", "--git-dir", str(remote), "log", "--oneline"],
                             capture_output=True, text=True).stdout
        self.assertIn("记忆备份", log)

    def test_restore_refuses_non_empty_target(self):
        backup(self.data, self.repo, self.key)
        with self.assertRaises(FileExistsError):
            restore(self.repo / ARCHIVE_NAME, self.key, self.data)


if __name__ == "__main__":
    unittest.main()
