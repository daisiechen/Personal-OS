"""记忆备份与恢复：打包数据目录 → openssl 加密 → 提交到独立私有仓库。

用法：
  python3 -m ethan.backup backup  --data <数据目录> --repo <备份仓库目录> --key <密钥文件> [--push]
  python3 -m ethan.backup restore --archive <备份仓库目录或加密包> --key <密钥文件> --data <恢复到的目录>
  python3 -m ethan.backup prune   --repo <备份仓库目录> [--dry-run] [--force]

2026-11-06 保留整理：最近 14 天每天留一份，再往前每月留最后一份，其余删掉。
每天的备份是同名分片覆盖、旧版本只在 git 历史里，所以“删”= 重写备份仓库历史，见 prune()；
backup --push 成功后顺带检查，攒够 MIN_DROP 个可删的提交（约一周）才真的重写。

11-05、11-06 两天 GitHub 拒收（pre-receive hook declined）。11-04 新加的检索模型 data/models/fastembed（91MB）
被打进包里，加密包从约 69MB 涨到约 286MB，超过 GitHub 单文件 100MB 上限。现在：
- 能重新下载 / 重新生成的目录不进包（EXCLUDE_DIRS，每项写了原因）；
- 加密包切成每片 PART_SIZE（90 MiB，约 94MB）一片：ethan-memory.tar.gz.enc.000、.001……，按序拼回再解密；
  老格式（一整个 ethan-memory.tar.gz.enc）照样能恢复。

密钥文件只存在服务器和 Mia 的密码管理器里，不进任何仓库。
"""

import argparse
import io
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CIPHER = ["-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt"]
ARCHIVE_NAME = "ethan-memory.tar.gz.enc"   # 老格式整包名；新格式是它加 .000、.001……
PART_SIZE = 90 * 1024 * 1024               # 每片 90 MiB（约 94MB），GitHub 单文件上限 100MB，留余量
_PART = re.compile(re.escape(ARCHIVE_NAME) + r"\.(\d{3})$")

# 不进备份的目录（任一层目录名对上就跳过）：都能重新下载或重新生成，丢了不丢记忆
EXCLUDE_DIRS = {
    "models": "检索用的 fastembed 模型（11-04 起约 91MB），memsearch 第一次用时自动重新下载",
    "__pycache__": "Python 编译缓存，运行时自动生成",
    ".cache": "各种库的下载缓存（如 huggingface），可重新下载",
}
# 不进备份的文件：数据库日志（快照已含）、自动部署前的数据库副本（数据库本身已在包里）、写到一半的临时文件
SKIP_SUFFIXES = (".sqlite-journal", ".sqlite-wal", ".sqlite-shm", ".tmp", ".pyc")


def _skip(rel: Path) -> bool:
    return (any(p in EXCLUDE_DIRS for p in rel.parts[:-1]) or rel.suffix in SKIP_SUFFIXES
            or ".bak-auto-" in rel.name)


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
            rel = path.relative_to(data)
            if not path.is_file() or _skip(rel):
                continue
            arcname = rel.as_posix()
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


class GitFailed(RuntimeError):
    pass


def _git(repo: Path, *args):
    """失败时把 git 自己的报错写进异常（11-05 体检只看到 CalledProcessError，看不出是提交还是推送、为什么）。"""
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip().splitlines()
        key = ("error", "rejected", "fatal", "denied", "exceeds", "remote:", "hint:")
        picked = [l.strip() for l in err if any(k in l.lower() for k in key) and not l.lower().startswith("hint:")]
        reason = " / ".join((picked or err)[-3:])[:240] or f"退出码 {proc.returncode}"  # 11-06：先挑报错行，“To 仓库”那行没用
        raise GitFailed(f"备份 git {args[0]} 失败：{reason}")


def split(blob: bytes, size: int = PART_SIZE) -> list:
    """加密包切成每片不超过 size 字节（至少一片）。"""
    return [blob[i:i + size] for i in range(0, len(blob), size)] or [b""]


def parts(where: Path) -> list:
    """找某个备份的所有分片，按序号排好。where：备份仓库目录、某一片、或老格式整包。
    序号必须从 000 连着，缺片直接报错（拼出来也解不开）。"""
    where = Path(where)
    if where.is_dir():
        folder = where
    elif _PART.search(where.name):
        folder = where.parent
    else:
        if where.is_file():
            return [where]  # 老格式整包
        folder = where.parent
    found = sorted((int(m.group(1)), p) for p in folder.iterdir() if (m := _PART.fullmatch(p.name)))
    if not found:
        old = folder / ARCHIVE_NAME
        if old.is_file():
            return [old]
        raise FileNotFoundError(f"{where} 里没有备份（{ARCHIVE_NAME}.000 或 {ARCHIVE_NAME}）")
    if [n for n, _ in found] != list(range(len(found))):
        raise ValueError(f"备份分片不连续：{[p.name for _, p in found]}")
    return [p for _, p in found]


def join(where: Path) -> bytes:
    return b"".join(p.read_bytes() for p in parts(where))


def write_parts(repo: Path, blob: bytes, size: int = PART_SIZE) -> list:
    """写新的分片，删掉上一份多出来的分片和老格式整包。返回写了哪些文件名。"""
    repo = Path(repo)
    names = []
    for i, chunk in enumerate(split(blob, size)):
        name = f"{ARCHIVE_NAME}.{i:03d}"
        (repo / name).write_bytes(chunk)
        names.append(name)
    for p in repo.iterdir():
        if (p.name == ARCHIVE_NAME or _PART.fullmatch(p.name)) and p.name not in names:
            p.unlink()
    return names


def backup(data: Path, repo: Path, key: Path, push: bool = False, size: int = PART_SIZE) -> bool:
    """写入加密分片并提交；内容未变时不提交。返回是否产生了新提交。"""
    repo = Path(repo)
    write_parts(repo, encrypt(pack(data), key), size)
    _git(repo, "add", "-A", "--", f"{ARCHIVE_NAME}*")  # 连同删掉的旧分片 / 老整包一起记进提交
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
    plain = decrypt(join(archive), key)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
        for member in tar.getmembers():
            target = (data / member.name).resolve()
            if not member.isfile() or data.resolve() not in target.parents:
                raise ValueError(f"备份包含非法路径：{member.name}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())


# ---------- 保留整理 ----------

KEEP_DAYS = 14          # 最近 14 天（按提交时间的 UTC 日期，以最新一份那天为第 14 天）每天留最后一份
MIN_DROP = 7            # 至少有 7 个提交可删才重写历史：约一周一次，不天天强推
KEEP_BRANCH = "keep-"   # 本地留底分支前缀（如服务器 11-06 建的 keep-unpushed-1106）：超过 14 天删，不推


class RetentionFailed(RuntimeError):
    pass


def _out(repo: Path, *args, data: bytes = None, env: dict = None) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, env=env)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise GitFailed(f"备份 git {args[0]} 失败：{err[-240:] or proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def history(repo: Path) -> list:
    """当前分支的提交，旧的在前：[(sha, 提交时间 UTC)]。只沿第一父提交走。"""
    out = _out(repo, "log", "--first-parent", "--reverse", "--format=%H %ct", "HEAD")
    rows = [line.split() for line in out.splitlines() if line.strip()]
    return [(sha, datetime.fromtimestamp(int(ct), timezone.utc)) for sha, ct in rows]


def pick_keep(commits: list, days: int = KEEP_DAYS) -> list:
    """从 [(sha, 时间)]（旧的在前）里挑要留的：最新那天往前 days 天每天最后一个，更早的每月最后一个；最新一个必留。"""
    if not commits:
        return []
    newest = max(t for _, t in commits).date()
    start = newest - timedelta(days=days - 1)
    last = {}
    for sha, t in commits:
        d = t.date()
        last[("d", d) if d >= start else ("m", d.year, d.month)] = sha
    last["tip"] = commits[-1][0]
    keep = set(last.values())
    return [sha for sha, _ in commits if sha in keep]


def _rebuild(repo: Path, keep: list) -> list:
    """用孤立链依次重建要留的快照：同一棵树、原作者 / 提交者、原时间和说明。不动任何分支。返回新提交（旧的在前）。"""
    new, parent = [], None
    for sha in keep:
        an, ae, ad, cn, ce, cd, tree = _out(
            repo, "log", "-1", "--date=raw", "--format=%an%x00%ae%x00%ad%x00%cn%x00%ce%x00%cd%x00%T", sha
        ).rstrip("\n").split("\x00")
        raw = subprocess.run(["git", "-C", str(repo), "cat-file", "commit", sha], capture_output=True, check=True).stdout
        msg = raw.split(b"\n\n", 1)[1] if b"\n\n" in raw else b""  # 原样的提交说明
        env = {**os.environ, "GIT_AUTHOR_NAME": an, "GIT_AUTHOR_EMAIL": ae, "GIT_AUTHOR_DATE": ad,
               "GIT_COMMITTER_NAME": cn, "GIT_COMMITTER_EMAIL": ce, "GIT_COMMITTER_DATE": cd}
        args = ["commit-tree", "--no-gpg-sign", tree] + (["-p", parent] if parent else [])
        parent = _out(repo, *args, data=msg, env=env).strip()
        new.append(parent)
    return new


def _check(repo: Path, old_head: str, keep: list, new: list):
    """核对：条数对、每份树和原提交一样、最新一份和重写前（含工作区里的分片）逐字节一致。不过就抛 RetentionFailed。"""
    if len(new) != len(keep) or not new:
        raise RetentionFailed(f"重建条数不对：要留 {len(keep)} 份，建出 {len(new)} 份")
    for a, b in zip(keep, new):
        if _out(repo, "rev-parse", a + "^{tree}") != _out(repo, "rev-parse", b + "^{tree}"):
            raise RetentionFailed(f"重建的 {b[:8]} 和原提交 {a[:8]} 内容不一样")
    if int(_out(repo, "rev-list", "--count", new[-1])) != len(keep):
        raise RetentionFailed("新历史的提交数和要留的份数对不上")
    if _out(repo, "rev-parse", old_head + "^{tree}") != _out(repo, "rev-parse", new[-1] + "^{tree}"):
        raise RetentionFailed("最新一份和重写前不一样")
    names = [n for n in _out(repo, "ls-tree", "--name-only", new[-1]).split()
             if n == ARCHIVE_NAME or _PART.fullmatch(n)]
    if not names:
        raise RetentionFailed("最新一份里找不到加密分片")
    for n in names:
        blob = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{new[-1]}:{n}"],
                              capture_output=True, check=True).stdout
        before = subprocess.run(["git", "-C", str(repo), "cat-file", "blob", f"{old_head}:{n}"],
                                capture_output=True, check=True).stdout
        if blob != before or blob != (Path(repo) / n).read_bytes():
            raise RetentionFailed(f"最新一份的 {n} 和重写前不是逐字节一致")


def _old_keep_branches(repo: Path, now: datetime, days: int) -> list:
    out = _out(repo, "for-each-ref", "--format=%(refname:short) %(committerdate:unix)", f"refs/heads/{KEEP_BRANCH}*")
    rows = [line.split() for line in out.splitlines() if line.strip()]
    return [name for name, ct in rows if now - datetime.fromtimestamp(int(ct), timezone.utc) > timedelta(days=days)]


def prune(repo: Path, days: int = KEEP_DAYS, min_drop: int = MIN_DROP, dry_run: bool = False,
          now: datetime = None, log=print) -> dict:
    """保留整理。只在本地分支和远程 origin 同一个提交时动；核对不过、远程被别人动过（--force-with-lease）都放弃、不推、抛错。
    keep- 开头的本地留底分支：最后一个提交超过 days 天的删掉，没超过的留着（从不推）。最后清掉本地旧对象。"""
    repo = Path(repo)
    now = now or datetime.now(timezone.utc)
    commits = history(repo)
    keep = pick_keep(commits, days)
    drop = len(commits) - len(keep)
    stale = _old_keep_branches(repo, now, days)
    result = {"commits": len(commits), "keep": len(keep), "drop": drop, "rewritten": False, "branches_deleted": stale}
    if dry_run:
        log(f"保留整理（只看不动）：共 {len(commits)} 份，留 {len(keep)} 份，可删 {drop} 份；过期留底分支 {stale or '无'}")
        return result
    if drop >= min_drop:
        branch = _out(repo, "symbolic-ref", "--short", "HEAD").strip()
        old_head = commits[-1][0]
        remote = _out(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()
        if not remote or remote[0] != old_head:
            raise RetentionFailed(f"远程 {branch} 和本地不一致（远程 {remote[0][:8] if remote else '没有'}，本地 {old_head[:8]}），先不整理")
        new = _rebuild(repo, keep)
        _check(repo, old_head, keep, new)
        _out(repo, "push", f"--force-with-lease=refs/heads/{branch}:{old_head}", "origin", f"{new[-1]}:refs/heads/{branch}")
        if _out(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[:1] != [new[-1]]:
            raise RetentionFailed("强推后远程不是新历史，请人工查看")
        _out(repo, "update-ref", f"refs/heads/{branch}", new[-1], old_head)
        _out(repo, "update-ref", f"refs/remotes/origin/{branch}", new[-1])
        result["rewritten"] = True
        log(f"保留整理：共 {len(commits)} 份，留 {len(keep)} 份，删 {drop} 份，已强推 {branch} → {new[-1][:8]}")
    else:
        log(f"保留整理：可删 {drop} 份，不到 {min_drop} 份，这次不动")
    for name in stale:
        _out(repo, "branch", "-D", name)
        log(f"删掉过期留底分支 {name}")
    if result["rewritten"] or stale:
        _out(repo, "reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
        _out(repo, "gc", "--prune=now", "--quiet")
    return result


def main():
    parser = argparse.ArgumentParser(description="小伴 记忆加密备份与恢复")
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--data", type=Path, required=True)
    b.add_argument("--repo", type=Path, required=True)
    b.add_argument("--key", type=Path, required=True)
    b.add_argument("--push", action="store_true")
    r = sub.add_parser("restore")
    r.add_argument("--archive", type=Path, required=True, help="备份仓库目录、任一分片或老格式整包")
    r.add_argument("--key", type=Path, required=True)
    r.add_argument("--data", type=Path, required=True)
    p = sub.add_parser("prune", help="保留整理：最近 14 天每天一份，更早每月一份")
    p.add_argument("--repo", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true", help="只报会留几份、删几份，不动")
    p.add_argument("--force", action="store_true", help="不管攒没攒够一周，有可删的就整理")
    args = parser.parse_args()
    if args.cmd == "backup":
        changed = backup(args.data, args.repo, args.key, args.push)
        print("已提交新备份" if changed else "记忆无变化，未提交")
        if args.push:  # 推送成功才走到这里；整理失败让定时任务记失败，备份本身已经推上去了
            prune(args.repo)
    elif args.cmd == "prune":
        prune(args.repo, min_drop=1 if args.force else MIN_DROP, dry_run=args.dry_run)
    else:
        restore(args.archive, args.key, args.data)
        print(f"已恢复到 {args.data}")


if __name__ == "__main__":
    main()
