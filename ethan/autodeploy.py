"""服务器自己上线（2026-10-27 她定：Mac 关机时用 Claude 云端会话兜底，云端改完、她同意合进 main 后自动上线）。

每 2 分钟看一次 GitHub 的 main 有没有新提交，有就按手动部署的步骤走：
备份数据库 → 快进拉代码 → 跑全部测试 → 装上改过的 systemd 文件 → 重启 Telegram、微信、收件箱 → 确认都在跑
→ 执行她同意过的数据改动（migrations/，见 ethan/migrate.py；出错不退回代码，结果写成“已上线，但数据改动出错：<名字>”，巡检报）。
数据改动放在重启之后（Codex 评审 11-05 问过要不要挪到重启前）：数据改动不能撤销，挪到前面、出错再退回旧代码，
就成了“新数据 + 旧代码”，比“新代码 + 有一个改动没做成”更难收拾；跑得久（超时 15 分钟）也不该挡住代码上线。
任何一步失败就退回原来的版本并重启。同一个失败的提交不再重试，等下一个新提交。
结果写进 <数据目录>/deploy-status.json（最新一次）和 deploy-log.jsonl（历史），由 Claude 会话读取后在会话里告诉她；
不发 Telegram（她定：运维消息不打扰她和 小伴 的聊天）。
以 root 运行（要重启服务、装 systemd 文件），git 和测试用 ethan 用户跑。

运行：python3 -m ethan.autodeploy --repo /home/ethan/Personal-OS --data /home/ethan/data
"""

import argparse
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ("ethan2-telegram", "ethan2-weixin", "ethan2-inbox", "ethan2-workbench")
STATUS = "deploy-status.json"
LOG = "deploy-log.jsonl"
CHECK = "deploy-check.json"  # 每 2 分钟看一眼 main 就记一笔（心跳）：定时器停了、拉不到代码，体检能看出来（2026-11-04 统一巡检）
UNIT_DIR = "deploy/systemd"


BACKUP_PY = ("import sqlite3, sys; s = sqlite3.connect(sys.argv[1]); d = sqlite3.connect(sys.argv[2]); "
             "s.backup(d); d.close(); s.close()")


class StepFailed(Exception):
    def __init__(self, msg: str, full: str = ""):
        super().__init__(msg)
        self.full = full or msg  # 完整输出：测试挂了要从里面找出是哪几条（只留最后 600 字会把 FAIL 那几行截掉）


FAILED_TEST = re.compile(r"^(?:FAIL|ERROR): (\S+) \(([\w.]+)\)", re.M)


def failed_tests(output: str, limit: int = 8) -> list:
    """unittest 输出里挂掉的测试，写成“test_文件.测试名”（设置页“最近部署”和巡检直接显示）。"""
    out = []
    for name, where in FAILED_TEST.findall(output or ""):
        mod = next((p for p in where.split(".") if p.startswith("test_") and p != name), where.split(".")[0])
        item = f"{mod}.{name}"
        if item not in out:
            out.append(item)
    return out[:limit]


def bad_migrations(e: StepFailed) -> str:
    """数据改动失败时是哪几个（migrate 每个打一行“名字：失败 …”）；超时、崩了就写原因。"""
    names = re.findall(r"^(\S+)：失败", e.full or "", re.M)
    return "、".join(names) if names else str(e)[:120]


def shell(cmd: list, cwd: str = None) -> str:
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:  # 超时也算这一步没过，照常记结果（2026-11-04 数据改动跑超 15 分钟，整次部署没留下记录）
        raise StepFailed(f"超过 15 分钟没跑完：{' '.join(cmd)[-200:]}")
    if proc.returncode != 0:
        full = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        raise StepFailed((proc.stderr or proc.stdout).strip()[-600:], full[-20000:])
    return proc.stdout


class Deployer:
    def __init__(self, repo: Path, data: Path, run=shell, sleep=time.sleep, unit_target: Path = Path("/etc/systemd/system")):
        self.repo, self.data, self.run, self.sleep, self.unit_target = Path(repo), Path(data), run, sleep, Path(unit_target)

    def as_ethan(self, *cmd) -> str:
        return self.run(["sudo", "-u", "ethan", "-H", *cmd], cwd=str(self.repo))

    def git(self, *args) -> str:
        return self.as_ethan("git", *args).strip()

    def status(self) -> dict:
        try:
            return json.loads((self.data / STATUS).read_text())
        except (OSError, ValueError):
            return {}

    def write(self, record: dict):
        path = self.data / STATUS
        path.write_text(json.dumps(record, ensure_ascii=False, indent=1))
        path.chmod(0o644)
        with (self.data / LOG).open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def heartbeat(self, error: str = ""):
        """每次看 main 都记一笔；看不了（拉代码失败）也记下原因。只在 root 下写，ethan 只读。"""
        try:
            path = self.data / CHECK
            path.write_text(json.dumps({"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "error": error[-300:]},
                                       ensure_ascii=False))
            path.chmod(0o644)
        except OSError:
            pass

    def restart(self):
        self.run(["systemctl", "restart", *SERVICES])
        self.sleep(8)
        for name in SERVICES:
            if self.run(["systemctl", "is-active", name]).strip() != "active":
                raise StepFailed(f"{name} 重启后没在跑")

    def install_units(self, old: str, new: str) -> list:
        changed = [p for p in self.git("diff", "--name-only", old, new, "--", UNIT_DIR).splitlines() if p]
        for p in changed:
            self.run(["cp", str(self.repo / p), str(self.unit_target / Path(p).name)])
        if changed:
            self.run(["systemctl", "daemon-reload"])
        added = self.git("diff", "--name-only", "--diff-filter=A", old, new, "--", UNIT_DIR).splitlines()
        for p in added:  # 新加的常驻服务开机自启；新加的定时器直接启用
            if Path(p).stem in SERVICES:
                self.run(["systemctl", "enable", Path(p).name])
            elif Path(p).suffix == ".timer":
                self.run(["systemctl", "enable", "--now", Path(p).name])
        return changed

    def restore_units(self, old: str, changed: list):
        """部署失败时把 systemd 文件也退回去：改过的拷回旧版，新加的停掉删掉。代码已经 reset 回 old。"""
        for p in changed:
            name = Path(p).name
            try:
                self.git("cat-file", "-e", f"{old}:{p}")
                self.run(["cp", str(self.repo / p), str(self.unit_target / name)])
            except StepFailed:  # 旧版本里没有：这次新加的
                if name.endswith(".timer") or Path(p).stem in SERVICES:
                    self.run(["systemctl", "disable", "--now", name])
                self.run(["rm", "-f", str(self.unit_target / name)])
        if changed:
            self.run(["systemctl", "daemon-reload"])

    def prune_backups(self, keep: int = 3):
        """自动部署前的数据库副本只留最近几份，免得占满磁盘。"""
        olds = sorted(self.data.glob("ethan.sqlite.bak-auto-*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in olds[keep:]:
            try:
                p.unlink()
            except OSError:
                pass

    def once(self, now: datetime = None):
        """有新提交就部署一次，返回这次的记录；没有新提交返回 None。"""
        try:
            self.git("fetch", "-q", "origin", "main")
        except StepFailed as e:
            self.heartbeat(f"拉不到 main：{e}")
            raise
        self.heartbeat()
        old, new = self.git("rev-parse", "HEAD"), self.git("rev-parse", "origin/main")
        if old == new or self.status().get("failed") == new:
            return None
        now = now or datetime.now(timezone.utc)
        subject = self.git("log", "-1", "--format=%s", new)
        record = {"at": now.isoformat(timespec="seconds"), "from": old[:7], "to": new[:7], "subject": subject,
                  "result": "", "step": "", "detail": "", "failed": ""}
        step = "备份数据库"
        units = []
        try:
            db = self.data / "ethan.sqlite"
            # 用 SQLite 自己的备份接口，不直接 cp：库开了 WAL 以后，最近写的可能还在 -wal 文件里（2026-11-02）
            self.as_ethan("python3", "-c", BACKUP_PY, str(db), f"{db}.bak-auto-{new[:7]}")
            step = "拉代码"
            self.git("merge", "--ff-only", "-q", "origin/main")
            step = "跑测试"  # 先测再装 systemd 文件：测试不过就什么都不动
            try:
                self.as_ethan("python3", "-m", "unittest", "discover", "-s", "tests", "-q")
            except StepFailed as e:
                record["failed_tests"] = failed_tests(e.full)
                raise
            changed = self.git("diff", "--name-only", old, new).split()
            from .memeval import touches_memory
            if touches_memory(changed):  # 改到记忆相关的：再跑一遍她的检验题，以前过的这次没过就退回（2026-10-31 她同意）
                step = "跑记忆检验题"
                out = self.as_ethan("python3", "-m", "ethan.memeval", "--data", str(self.data), "--gate")
                record["memeval"] = out.strip()[-300:]
            step = "装 systemd 文件"
            units = self.install_units(old, new)
            step = "重启服务"
            self.restart()
            record.update(result="已上线", step="完成", detail=("更新了 " + "、".join(units)) if units else "")
            try:  # 数据改动：备份已经在第一步做了；出错不退回代码，记进结果
                out = self.as_ethan("python3", "-m", "ethan.migrate", "--data", str(self.data), "--repo", str(self.repo)).strip()
                if out:
                    record["migrations"] = out[-600:]
            except StepFailed as e:  # 有脚本失败（migrate 非零退出）、超时：代码照样上线，但结果里写明，巡检当失败报
                record["migrations"] = f"数据改动出错：{(e.full or str(e))[-600:]}"
                record["result"] = f"已上线，但数据改动出错：{bad_migrations(e)}"
        except StepFailed as e:
            record.update(result="失败，已退回原版本", step=step, detail=str(e), failed=new)
            try:
                if self.git("rev-parse", "HEAD") != old:
                    self.git("reset", "--hard", "-q", old)
                self.restore_units(old, units)
                self.restart()
            except StepFailed as again:
                record["result"] = "失败，退回时也出错，需要人工处理"
                record["detail"] += f"\n退回时：{again}"
        self.prune_backups()
        self.write(record)
        return record


def main():
    parser = argparse.ArgumentParser(description="小伴 服务器自动部署")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    record = Deployer(args.repo, args.data).once()
    if record:
        print(f"[autodeploy] {record['from']} → {record['to']}：{record['result']}（{record['step']}）", flush=True)


if __name__ == "__main__":
    main()
