import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ethan.backup import (ARCHIVE_NAME, EXCLUDE_DIRS, backup, decrypt, encrypt, join, pack, parts, pick_keep, prune,
                          restore, write_parts)
from ethan import backup as backup_mod
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
        archive = self.repo / f"{ARCHIVE_NAME}.000"
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

    def test_push_failure_says_why(self):
        """11-05：体检只显示 CalledProcessError，看不出 git 为什么失败。现在异常里带 git 自己的报错。"""
        from ethan.backup import GitFailed
        subprocess.run(["git", "-C", str(self.repo), "remote", "add", "origin", str(Path(self.tmp.name) / "no-such-remote")], check=True)
        with self.assertRaises(GitFailed) as cm:
            backup(self.data, self.repo, self.key, push=True)
        self.assertIn("备份 git push 失败", str(cm.exception))
        self.assertIn("repository", str(cm.exception))  # git 的报错原文带出来了

    def test_git_error_line_comes_first(self):
        """11-06：推送失败的第一行是“To 仓库地址”，体检只显示 80 个字就看不到原因；要先挑报错行。"""
        from unittest import mock
        from ethan import backup as b
        out = b"To github-backup:x/y.git\n ! [remote rejected] main -> main (pre-receive hook declined)\nerror: failed to push some refs\n"
        with mock.patch.object(b.subprocess, "run", return_value=mock.Mock(returncode=1, stderr=out, stdout=b"")):
            with self.assertRaises(b.GitFailed) as cm:
                b._git(self.repo, "push")
        self.assertTrue(str(cm.exception).startswith("备份 git push 失败：! [remote rejected]"))

    def _files(self, commit="HEAD"):
        out = subprocess.run(["git", "-C", str(self.repo), "ls-tree", "--name-only", commit],
                             capture_output=True, text=True, check=True).stdout
        return sorted(out.split())

    def test_split_into_parts_and_join_back(self):
        """11-05、11-06 GitHub 拒收（单文件超 100MB）：加密包切片，每片不超过上限，按序拼回能解开。"""
        (self.data / "media").mkdir(exist_ok=True)
        (self.data / "media" / "big.bin").write_bytes(os.urandom(5000))  # 不可压缩，包一定有好几片
        self.assertTrue(backup(self.data, self.repo, self.key, size=1024))
        names = self._files()
        self.assertGreater(len(names), 3)
        self.assertEqual(names, [f"{ARCHIVE_NAME}.{i:03d}" for i in range(len(names))])
        self.assertTrue(all((self.repo / n).stat().st_size <= 1024 for n in names))
        for where in (self.repo, self.repo / names[2]):  # 给仓库目录或任一片都行
            target = Path(self.tmp.name) / f"restored-{where.name}"
            restore(where, self.key, target)
            self.assertEqual((target / "media" / "big.bin").read_bytes(), (self.data / "media" / "big.bin").read_bytes())
        # 下一份变小了：多出来的旧分片从仓库里删掉，不留下拼错的尾巴
        (self.data / "media" / "big.bin").unlink()
        self.assertTrue(backup(self.data, self.repo, self.key, size=1 << 20))
        self.assertEqual(self._files(), [f"{ARCHIVE_NAME}.000"])
        self.assertEqual(sorted(p.name for p in self.repo.glob(ARCHIVE_NAME + "*")), [f"{ARCHIVE_NAME}.000"])

    def test_missing_part_refuses(self):
        backup(self.data, self.repo, self.key, size=512)
        (self.repo / f"{ARCHIVE_NAME}.001").unlink()
        with self.assertRaises(ValueError):
            parts(self.repo)

    def test_old_single_archive_still_restores_and_is_replaced(self):
        """老格式（一整个 ethan-memory.tar.gz.enc）照样能恢复；下一次备份换成分片，整包从仓库里删掉。"""
        from ethan.backup import encrypt as enc
        (self.repo / ARCHIVE_NAME).write_bytes(enc(pack(self.data), self.key))
        subprocess.run(["git", "-C", str(self.repo), "add", ARCHIVE_NAME], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "老格式"], check=True)
        self.assertEqual(parts(self.repo), [self.repo / ARCHIVE_NAME])
        restore(self.repo / ARCHIVE_NAME, self.key, Path(self.tmp.name) / "old")
        self.assertTrue(backup(self.data, self.repo, self.key))
        self.assertEqual(self._files(), [f"{ARCHIVE_NAME}.000"])
        self.assertFalse((self.repo / ARCHIVE_NAME).exists())
        self.assertTrue(join(self.repo))

    def test_regenerable_dirs_left_out(self):
        """11-04 新加的检索模型 data/models/fastembed（91MB）被打进包里；能重新下载 / 生成的不进备份。"""
        import io
        import tarfile
        for d in ("models/fastembed/x", "__pycache__", "wiki/.cache"):
            (self.data / d).mkdir(parents=True, exist_ok=True)
        (self.data / "models" / "fastembed" / "x" / "model.onnx").write_bytes(b"0" * 100)
        (self.data / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"0")
        (self.data / "wiki" / ".cache" / "c").write_bytes(b"0")
        (self.data / "settings.json.tmp").write_text("{}")
        (self.data / "ethan.sqlite.bak-auto-1").write_bytes(b"0")
        (self.data / "cards" / "old").mkdir(parents=True, exist_ok=True)
        (self.data / "cards" / "old" / "2026-11-06-0631.json").write_text("{}")  # 她的旧卡不是缓存，要留
        with tarfile.open(fileobj=io.BytesIO(pack(self.data)), mode="r:gz") as tar:
            names = tar.getnames()
        self.assertFalse([n for n in names if n.split("/")[0] in EXCLUDE_DIRS or "/.cache/" in n or n.endswith((".tmp", ".pyc"))
                          or ".bak-auto-" in n], names)
        self.assertIn("cards/old/2026-11-06-0631.json", names)
        self.assertIn("ethan.sqlite", names)
        self.assertTrue(all(EXCLUDE_DIRS.values()))  # 每项写了原因

    def test_restore_refuses_non_empty_target(self):
        backup(self.data, self.repo, self.key)
        with self.assertRaises(FileExistsError):
            restore(self.repo / ARCHIVE_NAME, self.key, self.data)


@unittest.skipUnless(shutil.which("openssl") and shutil.which("git"), "需要 openssl 和 git")
class RetentionTest(unittest.TestCase):
    """最近 14 天每天一份，更早每月最后一份，其余删掉（重写备份仓库历史）。"""

    DAYS = 60
    LAST = date(2026, 11, 6)

    @classmethod
    def setUpClass(cls):
        # 60 天的加密包只做一次（openssl 慢），每个测试拷一份仓库；这里只测留哪些，密钥派生轮数降下来（真实轮数 BackupTest 照测）
        fast = list(backup_mod.CIPHER)
        fast[fast.index("-iter") + 1] = "1000"
        cls.cipher = mock.patch.object(backup_mod, "CIPHER", fast)
        cls.cipher.start()
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.key = root / "backup.key"
        cls.key.write_text(os.urandom(24).hex())
        os.environ.update({"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                           "GIT_COMMITTER_EMAIL": "t@t"})
        cls.seed = root / "seed"
        cls.seed.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(cls.seed)], check=True)
        cls._g(cls.seed, "config", "commit.gpgsign", "false")
        data = root / "data"
        data.mkdir()
        cls.days = []
        for i in range(cls.DAYS):
            d = cls.LAST - timedelta(days=cls.DAYS - 1 - i)
            times = ["01:00", "03:30"] if d.day % 10 == 0 else ["03:30"]  # 有的日子一天两份，留晚的那份
            for hm in times:
                (data / "day.txt").write_text(f"{d} {hm}")
                write_parts(cls.seed, encrypt(pack(data), cls.key), size=2048)
                cls._g(cls.seed, "add", "-A")
                stamp = f"{d}T{hm}:00+00:00"
                env = {**os.environ, "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
                subprocess.run(["git", "-C", str(cls.seed), "commit", "-qm", f"记忆备份 {d} {hm} UTC"],
                               check=True, env=env)
            cls.days.append(d)

    @classmethod
    def tearDownClass(cls):
        cls.cipher.stop()
        cls.tmp.cleanup()

    @staticmethod
    def _g(repo, *args, check=True):
        return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=check).stdout

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=self.tmp.name))
        self.remote = self.root / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(self.seed), str(self.remote)], check=True)
        self.repo = self.root / "ethan-memory-backup"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(self.repo)], check=True)
        self._g(self.repo, "config", "commit.gpgsign", "false")
        self.head = self._g(self.repo, "rev-parse", "HEAD").strip()
        self.tip_bytes = join(self.repo)

    def _log(self, where):
        out = self._g(where, "log", "--format=%H|%cI|%s", "main")
        return [line.split("|") for line in out.splitlines()]

    def _expected(self):
        start = self.LAST - timedelta(days=13)
        older = [d for d in self.days if d < start]
        month_last = {}
        for d in older:
            month_last[(d.year, d.month)] = d
        return sorted(month_last.values()) + [d for d in self.days if d >= start]

    def test_keeps_14_days_and_one_per_month(self):
        r = prune(self.repo, log=lambda *_: None)
        self.assertTrue(r["rewritten"])
        log = self._log(self.repo)
        kept = sorted(date.fromisoformat(s.split()[1]) for _, _, s in log)
        self.assertEqual(kept, self._expected())  # 9/30、10/23 各一份 + 10/24–11/6 每天一份
        self.assertEqual(len(kept), 16)
        self.assertTrue(all(s.endswith("03:30 UTC") for _, _, s in log))  # 一天两份的留晚的
        self.assertTrue(all(t[11:16] == "03:30" and s.split()[1] == t[:10] for _, t, s in log))  # 原时间、原说明
        # 最新一份逐字节不变，工作区干净
        self.assertEqual(join(self.repo), self.tip_bytes)
        self.assertEqual(self._g(self.repo, "status", "--porcelain"), "")
        # 远程被更新成同一份新历史
        self.assertEqual(self._log(self.remote), log)
        self.assertNotEqual(log[0][0], self.head)
        # 本地旧对象清掉了
        r2 = subprocess.run(["git", "-C", str(self.repo), "cat-file", "-e", self.head + "^{commit}"], capture_output=True)
        self.assertNotEqual(r2.returncode, 0)

    def test_any_kept_snapshot_restores(self):
        prune(self.repo, log=lambda *_: None)
        for sha, _, subject in self._log(self.repo):
            snap = self.root / f"snap-{sha[:8]}"
            snap.mkdir()
            tar = subprocess.run(["git", "-C", str(self.repo), "archive", sha], capture_output=True, check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(snap)], input=tar, check=True)
            target = self.root / f"restored-{sha[:8]}"
            restore(snap, self.key, target)
            self.assertEqual((target / "day.txt").read_text(), " ".join(subject.split()[1:3]))

    def test_check_failure_does_not_push(self):
        from unittest import mock
        from ethan import backup as b
        real = b._rebuild
        with mock.patch.object(b, "_rebuild", side_effect=lambda repo, keep: real(repo, keep[:-1])):  # 少建最新一份
            with self.assertRaises(b.RetentionFailed):
                prune(self.repo, log=lambda *_: None)
        self.assertEqual(self._g(self.repo, "rev-parse", "HEAD").strip(), self.head)
        self.assertEqual(self._g(self.remote, "rev-parse", "main").strip(), self.head)
        self.assertEqual(len(self._log(self.repo)), self.DAYS + 6)

    def test_remote_moved_does_not_push(self):
        from ethan import backup as b
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True)
        self._g(other, "commit", "-q", "--allow-empty", "--no-gpg-sign", "-m", "别处推的")
        self._g(other, "push", "-q", "origin", "main")
        moved = self._g(self.remote, "rev-parse", "main").strip()
        with self.assertRaises(b.RetentionFailed):
            prune(self.repo, log=lambda *_: None)
        self.assertEqual(self._g(self.remote, "rev-parse", "main").strip(), moved)
        self.assertEqual(self._g(self.repo, "rev-parse", "HEAD").strip(), self.head)

    def test_waits_until_a_week_to_drop(self):
        r = prune(self.repo, min_drop=1000, log=lambda *_: None)
        self.assertFalse(r["rewritten"])
        self.assertEqual(self._g(self.repo, "rev-parse", "HEAD").strip(), self.head)
        self.assertGreater(r["drop"], 7)
        self.assertFalse(prune(self.repo, dry_run=True, log=lambda *_: None)["rewritten"])
        self.assertEqual(self._g(self.remote, "rev-parse", "main").strip(), self.head)

    def test_old_keep_branches_deleted_new_kept_never_pushed(self):
        first = self._log(self.repo)[-1][0]  # 最早那份（9/8）：留底分支超过 14 天
        self._g(self.repo, "branch", "keep-unpushed-0908", first)
        self._g(self.repo, "branch", "keep-unpushed-1106", "HEAD")
        r = prune(self.repo, now=datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc), log=lambda *_: None)
        self.assertEqual(r["branches_deleted"], ["keep-unpushed-0908"])
        branches = self._g(self.repo, "branch", "--format=%(refname:short)").split()
        self.assertIn("keep-unpushed-1106", branches)
        self.assertNotIn("keep-unpushed-0908", branches)
        self.assertNotIn("keep-", self._g(self.remote, "branch", "--format=%(refname:short)"))
        # 没过期的留底分支还指着旧提交，它的内容还在
        self.assertEqual(self._g(self.repo, "rev-parse", "keep-unpushed-1106").strip(), self.head)

    def test_pick_keep_rules(self):
        t = lambda s: datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        commits = [("a", t("2026-08-31T03:30")), ("b", t("2026-09-01T03:30")), ("c", t("2026-09-30T03:30")),
                   ("d", t("2026-10-30T01:00")), ("e", t("2026-10-30T03:30")), ("f", t("2026-11-06T03:30"))]
        self.assertEqual(pick_keep(commits), ["a", "c", "e", "f"])
        self.assertEqual(pick_keep([]), [])
        self.assertEqual(pick_keep(commits[:1]), ["a"])


if __name__ == "__main__":
    unittest.main()
