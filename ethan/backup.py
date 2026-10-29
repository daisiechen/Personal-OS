"""记忆备份与恢复：打包数据目录 → openssl 加密 → 提交到独立私有仓库。

用法：
  python3 -m ethan.backup backup  --data <数据目录> --repo <备份仓库目录> --key <密钥文件> [--push]
  python3 -m ethan.backup restore --archive <加密包> --key <密钥文件> --data <恢复到的目录>

密钥文件只存在服务器和 Mia 的密码管理器里，不进任何仓库。
"""

import argparse
import io
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

CIPHER = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt"]
ARCHIVE_NAME = "ethan-memory.tar.gz.enc"


def _snapshot_sqlite(src: Path) -> bytes:
    """用 SQLite 在线备份接口取一致的快照，避免复制到写了一半的文件。"""
    with tempfile.TemporaryDirectory() as tmp:
        dst_path = Path(tmp) / src.name
        src_conn = sqlite3.connect(str(src))
        dst_conn = sqlite3.connect(str(dst_path))
        with dst_conn:
            src_conn.backup(dst_conn)
        src_conn.close()
        dst_conn.close()
        return dst_path.read_bytes()


def pack(data: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(Path(data).rglob("*")):
            if not path.is_file() or path.suffix in (".sqlite-journal", ".sqlite-wal") or ".bak-auto-" in path.name:
                continue  # 自动部署前的数据库副本不进备份（数据库本身已经在里面了）
            arcname = path.relative_to(data).as_posix()
            content = _snapshot_sqlite(path) if path.suffix == ".sqlite" else path.read_bytes()
            info = tarfile.TarInfo(arcname)
            info.size = len(content)
            info.mtime = int(path.stat().st_mtime)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _openssl(args: list, data: bytes, key: Path) -> bytes:
    proc = subprocess.run(["openssl", "enc", *args, *CIPHER, "-pass", f"file:{key}"],
                          input=data, capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError(f"openssl 失败：{proc.stderr.decode(errors='ignore')[:200]}")
    return proc.stdout


def encrypt(data: bytes, key: Path) -> bytes:
    return _openssl(["-e"], data, key)


def decrypt(blob: bytes, key: Path) -> bytes:
    return _openssl(["-d"], blob, key)


def _git(repo: Path, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def backup(data: Path, repo: Path, key: Path, push: bool = False) -> bool:
    """写入加密包并提交；内容未变时不提交。返回是否产生了新提交。"""
    repo = Path(repo)
    (repo / ARCHIVE_NAME).write_bytes(encrypt(pack(data), key))
    _git(repo, "add", ARCHIVE_NAME)
    if subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--quiet"]).returncode == 0:
        return False
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    _git(repo, "commit", "-m", f"记忆备份 {stamp}")
    if push:
        _git(repo, "push", "-u", "origin", "HEAD")  # 首次推送到空仓库时同时建立上游分支
    return True


def restore(archive: Path, key: Path, data: Path):
    data = Path(data)
    if data.exists() and any(data.iterdir()):
        raise FileExistsError(f"{data} 不是空目录，为避免覆盖现有记忆，请恢复到新目录")
    data.mkdir(parents=True, exist_ok=True)
    plain = decrypt(Path(archive).read_bytes(), key)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (data / member.name).resolve()
            if not member.isfile() or data.resolve() not in target.parents:
                raise ValueError(f"备份包含非法路径：{member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())


def main():
    parser = argparse.ArgumentParser(description="小伴 记忆加密备份与恢复")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--data", type=Path, required=True)
    b.add_argument("--repo", type=Path, required=True)
    b.add_argument("--key", type=Path, required=True)
    b.add_argument("--push", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("--archive", type=Path, required=True)
    r.add_argument("--key", type=Path, required=True)
    r.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    if args.cmd == "backup":
        changed = backup(args.data, args.repo, args.key, args.push)
        print("已提交新备份" if changed else "记忆无变化，未提交")
    else:
        restore(args.archive, args.key, args.data)
        print(f"已恢复到 {args.data}")


if __name__ == "__main__":
    main()
