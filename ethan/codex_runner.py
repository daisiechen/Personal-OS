"""Codex 自动领活（2026-10-28 她同意）：定时从 agent 任务队列领一件 Codex 的活，在独立副本里用 codex exec 干完，
自动跑验收命令，把改动（patch）、用量、用时记回队列，状态变成“等审查”。

Codex 不碰 GitHub：服务器的仓库钥匙只读，改动由 Claude 取回、复测、审查后推送；打回就重新排队（返工 +1）。
一次只干一件（systemd oneshot，上一次没跑完下一次不会开始），额度用完就把这件放回队列，过一小时再试，不设每日上限。

运行：python3 -m ethan.codex_runner --data /home/ethan/data --repo /home/ethan/Personal-OS
（systemd：ethan2-codex.timer 每 5 分钟一次）
"""

import argparse
import json
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import agents
from .memory import MemoryStore

# codex-cli 0.165.0（2026-10-28 服务器实测）：没有 --full-auto 了；--approve-for-me 自带只写当前副本的沙盒、
# 审批请求自动处理（不能再和 --sandbox 一起用）
CODEX_CMD = ["codex", "exec", "--json", "--approve-for-me", "--skip-git-repo-check"]
CODEX_TIMEOUT = 45 * 60
VERIFY_TIMEOUT = 15 * 60
MAX_PATCH = 2 * 1024 * 1024
MAX_LOG = 20000
QUOTA = re.compile(r"usage limit|rate.?limit|quota|exceeded your|try again (in|at)", re.I)
RETRY_AFTER = timedelta(hours=1)

RULES = """你是 Mia 的工程团队里的执行工（Codex）。只做下面这件事，做完就停：
- 只改完成这件事需要改的文件，沿用周边代码的写法、注释密度和命名，不顺手重构别的。
- 按“验收”里的命令自己跑一遍，跑通为止；需要时补测试。
- 不要 git commit、不要 push、不要改 .github 和部署配置、不要碰密钥或 /home/ethan/data 下的真实数据。
- 文档和注释用中文。
最后用几句话说明改了什么、验收结果如何。"""


def build_prompt(task: dict) -> str:
    parts = [RULES, f"\n# 任务 #{task['id']}：{task['title']}"]
    if task.get("project"):
        parts.append(f"项目：{task['project']}")
    parts.append(task.get("spec") or "")
    if task.get("verify"):
        parts.append(f"\n# 验收\n在仓库根目录运行：\n{task['verify']}")
    return "\n".join(p for p in parts if p)


def parse_usage(stdout: str) -> tuple:
    """从 codex exec --json 的事件流里找 token 用量（取出现过的最大值）。"""
    best = (0, 0)

    def walk(node):
        nonlocal best
        if isinstance(node, dict):
            i, o = node.get("input_tokens"), node.get("output_tokens")
            if isinstance(i, int) and isinstance(o, int) and i + o > sum(best):
                best = (i, o)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    for line in stdout.split("\n"):
        try:
            walk(json.loads(line))
        except ValueError:
            continue
    return best


def _git(run, *args, cwd=None) -> str:
    res = run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout


def recover_stale(db, now=None) -> list:
    """上次干到一半进程没了（服务器重启、内存不够被杀），活会一直挂在“在干”：超过两个超时还没收尾的判失败，她可以点重试。
    （2026-10-29 自查发现的）"""
    from datetime import datetime, timedelta, timezone
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(seconds=CODEX_TIMEOUT + VERIFY_TIMEOUT + 600)).isoformat(timespec="seconds")
    rows = db.conn.execute("SELECT id FROM agent_tasks WHERE agent = 'codex' AND status = 'running' AND started_at < ?", (cutoff,)).fetchall()
    for r in rows:
        agents.update(db, r["id"], status="failed", finished_at=agents.now(), review_note="中途断了（服务器重启或内存不够），没干完，可以重试")
    return [r["id"] for r in rows]


def run_once(db, repo: Path, work_root: Path, run=subprocess.run, cmd=None, clock=time.monotonic):
    """领一件干一件，返回这件活最后的状态；没有活返回 None。"""
    task = agents.next_queued(db, "codex")
    if task is None:
        return None
    tid = task["id"]
    agents.update(db, tid, status="running", started_at=agents.now())
    work = Path(work_root) / f"task-{tid}-r{task['reworks']}"
    log = []
    try:
        _git(run, "-C", str(repo), "fetch", "-q", "origin", "main")
        base = _git(run, "-C", str(repo), "rev-parse", "origin/main").strip()
        if work.exists():
            shutil.rmtree(work)
        _git(run, "-C", str(repo), "worktree", "add", "--detach", str(work), base)
        started = clock()
        try:
            res = run([*(cmd or CODEX_CMD), build_prompt(task)], cwd=str(work), capture_output=True, text=True,
                      timeout=CODEX_TIMEOUT)
            out, err, code = res.stdout or "", res.stderr or "", res.returncode
        except subprocess.TimeoutExpired:
            out, err, code = "", f"Codex 超过 {CODEX_TIMEOUT // 60} 分钟没做完", -1
        spent = clock() - started
        tin, tout = parse_usage(out)
        fields = dict(tokens_in=task["tokens_in"] + tin, tokens_out=task["tokens_out"] + tout,
                      work_seconds=task["work_seconds"] + spent, base_sha=base)
        if code != 0 and tin + tout == 0 and not QUOTA.search(out + err):  # 没开始干就报错（参数、登录）：环境问题
            raise RuntimeError(f"codex 退出码 {code}：{(err or out).strip()[-500:]}")
        if code != 0 and QUOTA.search(out + err):  # 额度用完：放回队列，过一会儿再试，不算失败
            retry = (datetime.now(timezone.utc) + RETRY_AFTER).isoformat(timespec="seconds")
            agents.update(db, tid, status="queued", started_at=None, retry_after=retry,
                          log=f"额度用完，{retry} 后再试：{(err or out)[-500:]}", **fields)
            return "queued"
        log.append(f"== codex 退出码 {code}，用时 {spent:.0f} 秒，tokens {tin}+{tout}")
        log.append(_last_message(out) or err[-2000:])
        verify_ok = None
        if code == 0 and task["verify"]:
            try:
                v = run(["bash", "-lc", task["verify"]], cwd=str(work), capture_output=True, text=True,
                        timeout=VERIFY_TIMEOUT)
                verify_ok = v.returncode == 0
                log.append(f"== 验收（退出码 {v.returncode}）\n" + (v.stdout + v.stderr)[-4000:])
            except subprocess.TimeoutExpired:
                verify_ok = False
                log.append(f"== 验收超过 {VERIFY_TIMEOUT // 60} 分钟")
        _git(run, "-C", str(work), "add", "-A")
        patch = _git(run, "-C", str(work), "diff", "--cached", "--binary", base)
        ok = code == 0 and verify_ok is not False and bool(patch.strip())
        if code == 0 and not patch.strip():
            log.append("== 没有改动")
        status = "review" if ok else "failed"
        agents.update(db, tid, status=status, finished_at=agents.now(), verify_ok=verify_ok,
                      patch=patch[:MAX_PATCH], log="\n".join(log)[-MAX_LOG:], **fields)
        return status
    except Exception as e:  # 环境问题（拉代码失败、副本建不起来）：记下原因，不算 Codex 的失败，放回队列
        agents.update(db, tid, status="queued", started_at=None,
                      retry_after=(datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(timespec="seconds"),
                      log=f"环境出错，30 分钟后重试：{type(e).__name__}: {str(e)[:500]}")
        return "queued"
    finally:
        try:
            _git(run, "-C", str(repo), "worktree", "remove", "--force", str(work))
        except Exception:
            shutil.rmtree(work, ignore_errors=True)


def _last_message(stdout: str) -> str:
    """codex exec --json 里最后一条助手消息的文字。"""
    last = ""
    for line in stdout.split("\n"):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        item = ev.get("item") if isinstance(ev, dict) else None
        for node in (item, ev):
            if isinstance(node, dict) and isinstance(node.get("text"), str) and "message" in str(node.get("type", "")):
                last = node["text"]
    return last[-3000:]


def main():
    parser = argparse.ArgumentParser(description="Codex 自动领活")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--work", type=Path, default=Path.home() / "agent-work")
    args = parser.parse_args()
    if shutil.which("codex") is None:  # 还没装好就安静跳过，装好后下一次定时自动开始
        print("[codex] 还没装 codex 命令行，跳过", flush=True)
        return
    args.work.mkdir(parents=True, exist_ok=True)
    store = MemoryStore(args.data)
    agents.ensure(store.db)
    stale = recover_stale(store.db)
    if stale:
        print(f"[codex] 上次中断没收尾的判失败：{stale}", flush=True)
    while True:  # 一次把能干的都干完（额度用完或没活就停）
        status = run_once(store.db, args.repo, args.work)
        print(f"[codex] {status or '没有活'}", flush=True)
        if status in (None, "queued"):
            break
    store.close()


if __name__ == "__main__":
    main()
