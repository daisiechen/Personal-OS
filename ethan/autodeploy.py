"""服务器自己上线（2026-10-27 她定：Mac 关机时用 Claude 云端会话兜底，云端改完、她同意合进 main 后自动上线）。

每 2 分钟看一次 GitHub 的 main 有没有新提交，有就按手动部署的步骤走：
备份数据库 → 快进拉代码 → 跑全部测试 → 装上改过的 systemd 文件 → 重启 Telegram、微信、收件箱 → 确认都在跑
→ 执行她同意过的数据改动（migrations/，见 ethan/migrate.py；出错只记下，不退回代码）。
任何一步失败就退回原来的版本并重启。同一个失败的提交不再重试，等下一个新提交。
结果写进 <数据目录>/deploy-status.json（最新一次）和 deploy-log.jsonl（历史），由 Claude 会话读取后在会话里告诉她；
不发 Telegram（她定：运维消息不打扰她和 小伴 的聊天）。
以 root 运行（要重启服务、装 systemd 文件），git 和测试用 ethan 用户跑。

运行：python3 -m ethan.autodeploy --repo /home/ethan/Personal-OS --data /home/ethan/data
"""

import argparse
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ("ethan2-telegram", "ethan2-weixin", "ethan2-inbox", "ethan2-workbench")
STATUS = "deploy-status.json"
LOG = "deploy-log.jsonl"
UNIT_DIR = "deploy/systemd"


class StepFailed(Exception):
    pass


def shell(cmd: list, cwd: str = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0:
        raise StepFailed((proc.stderr or proc.stdout).strip()[-600:])
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
        self.git("fetch", "-q", "origin", "main")
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
            self.as_ethan("cp", str(db), f"{db}.bak-auto-{new[:7]}")
            step = "拉代码"
            self.git("merge", "--ff-only", "-q", "origin/main")
            step = "跑测试"  # 先测再装 systemd 文件：测试不过就什么都不动
            self.as_ethan("python3", "-m", "unittest", "discover", "-s", "tests", "-q")
            step = "装 systemd 文件"
            units = self.install_units(old, new)
            step = "重启服务"
            self.restart()
            record.update(result="已上线", step="完成", detail=("更新了 " + "、".join(units)) if units else "")
            try:  # 数据改动：备份已经在第一步做了；出错不退回代码，记进结果
                out = self.as_ethan("python3", "-m", "ethan.migrate", "--data", str(self.data), "--repo", str(self.repo)).strip()
                if out:
                    record["migrations"] = out[-600:]
            except StepFailed as e:
                record["migrations"] = f"数据改动出错：{e}"
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
