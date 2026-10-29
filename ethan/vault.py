"""Obsidian 明文看板：把记忆同步到私有 Git 仓库，Mia 用 Obsidian 打开浏览和修改。

仓库结构：
  wiki/        Wiki 页面原样导出；Mia 在 Obsidian 里改过的页面，下次同步时写回记忆库
  看板/        由数据库生成的只读页面（总览、目标树、人物、心愿、学习），每次同步覆盖
  .obsidian/   Mia 自己的 Obsidian 设置，程序不碰

同步顺序：git pull → 收回 Mia 改过的 Wiki 页 → 重新导出 → commit → push。
同一页两边都改了时，以 Mia 的版本为准。删除页面请告诉 小伴，在 Obsidian 里删了下次会被重新导出。

用法：python3 -m ethan.vault --data <数据目录> --repo <看板仓库目录> [--push]
"""

import argparse
import hashlib
import json
import subprocess
from datetime import date, datetime, timezone
from pathlib import Path

from . import english
from .clock import her_tz
from .domains import domain_lines
from .memory import MemoryStore

STATE_FILE = "vault-sync.json"
BOARD = "看板"
NOTICE = "> 由 小伴 自动生成，每次同步都会覆盖；想改内容直接告诉 小伴。\n"
STATUS = {"not_started": "未开始", "active": "进行中", "paused": "暂停", "done": "完成", "dropped": "放弃"}
LEVEL = {"vision": "80 岁画像", "long": "方面", "year": "今年", "stage": "阶段", "task": "任务"}
RECENT_DAYS = 3  # 最近聊天页放几天，给 Grok 里的 小伴 看（设计第 9.7 节）
RECENT_LIMIT = 300


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(repo: Path, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True, text=True)


def _goal_lines(db, parent_id=None, depth=0) -> list:
    where = "parent_id IS NULL" if parent_id is None else "parent_id = ?"
    rows = db.conn.execute(
        f"SELECT * FROM goals WHERE {where} ORDER BY id", () if parent_id is None else (parent_id,)
    ).fetchall()
    lines = []
    for g in rows:
        extra = []
        if g["due"]:
            extra.append(f"截止 {g['due']}")
        if g["next_step"]:
            extra.append(f"下一步：{g['next_step']}")
        suffix = f"（{'；'.join(extra)}）" if extra else ""
        lines.append(f"{'  ' * depth}- [{STATUS[g['status']]}] {LEVEL[g['level']]}：{g['title']}{suffix}")
        logs = db.conn.execute(
            "SELECT note, created_at FROM progress_log WHERE goal_id = ? ORDER BY id DESC LIMIT 3",
            (g["id"],),
        ).fetchall()
        for log in logs:
            lines.append(f"{'  ' * (depth + 1)}- {log['created_at'][:10]} {log['note']}")
        for t in db.conn.execute(
            "SELECT title, due FROM tasks WHERE goal_id = ? AND done = 0 ORDER BY due IS NULL, due, id",
            (g["id"],),
        ).fetchall():
            due = f"（{t['due']}）" if t["due"] else ""
            lines.append(f"{'  ' * (depth + 1)}- [ ] 待办：{t['title']}{due}")
        lines += _goal_lines(db, g["id"], depth + 1)
    return lines


def recent_chat(store: MemoryStore, today: date, tz=None) -> list:
    """最近几天的聊天原文（伦敦时间），小伴 的话去掉内部标记，只留她能看到的部分。"""
    from .core.agent import parse_reply  # 避免循环导入
    tz = tz or her_tz()
    since = today.toordinal() - RECENT_DAYS + 1
    lines = [f"# 最近聊天（{RECENT_DAYS} 天）", "", NOTICE,
             "> 服务器上 小伴 和她的聊天原文，给 Grok 里的 小伴 了解最近聊了什么。", ""]
    for r in store.raw.recent(RECENT_LIMIT):
        at = datetime.fromisoformat(r["at"]).astimezone(tz)
        if at.date().toordinal() < since:
            continue
        if r["role"] == "ethan":
            shown = parse_reply(r["text"])
            text = " / ".join(shown["voice"] + shown["bubbles"])
            if not text:
                continue
            who = "小伴"
        else:
            text, who = " ".join(r["text"].split()), "Mia"
        lines.append(f"- {at:%m-%d %H:%M} [{r.get('channel', '')}] {who}：{text}")
    return lines


def cold_start_from(store: MemoryStore):
    """冷启动开始日，存在数据目录的 settings.json（和计划器同一份）。"""
    path = Path(store.root) / "settings.json"
    value = json.loads(path.read_text(encoding="utf-8")).get("cold_start_from") if path.exists() else None
    return date.fromisoformat(value) if value else None


def today_plan(store: MemoryStore, today: date) -> list:
    """今天的计划：要出门的、待办、训练动作、三餐菜谱（设计第 3、5 节）。"""
    plan = store.db.latest_plan(today.isoformat())
    lines = [f"# 今日计划 {today.isoformat()}", "", NOTICE]
    if plan is None:
        return lines + ["今天的计划还没生成（每天伦敦 06:30 生成）。"]
    brief = json.loads(plan["brief"])
    if brief.get("fallback"):
        lines += ["> 今天是保底版本（自动排计划没成功）。", ""]
    lines += ["## 要出门的（已进苹果日历）", ""]
    outs = [i for i in plan["items"] if i["kind"] == "event"]
    lines += [f"- {i['start']}–{i['end']} {i['title']}" + (f"（{i['detail'].splitlines()[0]}）" if i.get("travel_min") else "")
              for i in outs] or ["- 今天不用出门"]
    if brief.get("busy"):
        lines += ["", "## 日历上已有的安排", ""] + [f"- {x}" for x in brief["busy"]]
    game = brief.get("game") or {}
    if game:
        lines += ["", "## 🎮 任务系统", "",
                  f"Lv{game['level']}「{game['title']}」· 积分 {game['balance']} · 满勤连续 {game['streak']['days']} 天",
                  "", "### 日常", ""] + [f"- [ ] {h['title']}" for h in game.get("daily", [])]
        lines += [f"- {h['title']} {h['count']}/{h['target']}" for h in game.get("weekly", [])]
    mains = [i for i in plan["items"] if i["kind"] == "todo" and i.get("priority") == "main"]
    if mains:
        lines += ["", "### 主线（每个目标完整拆解）", ""]
        goals = {r["id"]: r["title"] for r in store.db.conn.execute("SELECT id, title FROM goals")}
        current = None
        for i in mains:
            if i["goal_id"] != current:
                current = i["goal_id"]
                lines += ["", f"**{goals.get(current, '')}**"]
            info = json.loads(i["detail"]) if i.get("detail") else {}
            tag = f"{info.get('points', '')} 分" + ("·阶段节点" if info.get("milestone") else "") \
                + ("·草稿待过目" if info.get("draft") else "")
            lines.append(f"- [{'x' if i.get('result') == '完成' else ' '}] {i['title']}（{tag}）")
    for label, key in (("### 支线", "side"), ("## 小伴 替你准备（做好给你审批）", "ethan")):
        todos = [i for i in plan["items"] if i["kind"] == "todo" and i.get("priority") == key]
        lines += ["", label, ""]
        lines += [f"- [{'x' if i.get('result') == '完成' else ' '}] {i['title']}"
                  + (f"（办法：{i['how']}）" if i.get("how") else "") for i in todos] or ["- 无"]
    detail = [i for i in plan["items"] if i["kind"] == "event" and i.get("detail")]
    for i in detail:
        lines += ["", f"## {i['title']}", "", i["detail"]]
    cold = [i for i in plan["items"] if i["kind"] == "todo" and i["title"].startswith("冷启动") and i.get("detail")]
    for i in cold:
        lines += ["", f"## {i['title']}", "", i["detail"]]
    if brief.get("meals_text"):
        lines += ["", "## 三餐", "", brief["meals_text"]]
    return lines


def board_pages(store: MemoryStore, today: date) -> dict:
    """生成看板页面：{相对路径: 内容}。"""
    db = store.db
    goals = db.active_goals()
    stage = [g for g in goals if g["level"] in ("year", "stage", "task")]
    tasks = db.due_tasks(today)
    contact = db.people_to_contact(today)
    wishes = db.open_wishes()
    review = db.due_learning_items(today, limit=50)
    reason = {"long_no_contact": "很久没联系", "birthday_soon": "快过生日"}

    overview = [f"# 总览 {today.isoformat()}", "", NOTICE,
                "## 进行中的阶段目标", ""]
    overview += [f"- {g['title']}" + (f"（下一步：{g['next_step']}）" if g["next_step"] else "")
                 for g in stage] or ["- 暂无"]
    overview += ["", "## 到期待办", ""]
    overview += [("- [小伴 答应的] " if t["promised_by_ethan"] else "- ") + f"{t['title']}（{t['due']}）"
                 for t in tasks] or ["- 暂无"]
    overview += ["", "## 该联系的人", ""]
    overview += [f"- {p['name']}：{'、'.join(reason[r] for r in p['reasons'])}" for p in contact] or ["- 暂无"]
    overview += ["", f"## 今天要复习的单词句子：{len(review)} 条", "",
                 f"## 待实现的心愿：{len(wishes)} 条", "",
                 "## 更多", "",
                 "- [[wiki/dashboard|总仪表盘说明]]",
                 f"- [[{BOARD}/目标树]] · [[{BOARD}/人物]] · [[{BOARD}/心愿]] · [[{BOARD}/学习]]"]

    people = ["# 人物", "", NOTICE]
    for r in db.conn.execute("SELECT * FROM people ORDER BY circle, id"):
        people.append(f"## {r['name']}")
        for key, label in (("circle", "圈子"), ("birthday", "生日"), ("how_met", "认识"),
                           ("notes", "备注"), ("last_contact", "上次联系")):
            if r[key]:
                people.append(f"- {label}：{r[key]}")
        people.append("")

    wish_lines = ["# 心愿清单", "", NOTICE]
    wish_lines += [f"- {w['quote']}" + (f"（{w['context']}）" if w["context"] else "") for w in wishes]
    done = db.conn.execute("SELECT quote, done_at FROM wishes WHERE status = 'done' ORDER BY done_at").fetchall()
    if done:
        wish_lines += ["", "## 已实现", ""] + [f"- {w['quote']}（{w['done_at'][:10]}）" for w in done]

    learn = ["# 学习", "", NOTICE]
    for subject, in db.conn.execute("SELECT DISTINCT subject FROM learning_items ORDER BY subject"):
        learn += [f"## {subject}", ""]
        for r in db.conn.execute(
            "SELECT content, note, next_review FROM learning_items WHERE subject = ? ORDER BY id", (subject,)
        ):
            learn.append(f"- {r['content']}" + (f"：{r['note']}" if r["note"] else "")
                         + f"（下次复习 {r['next_review']}）")
        learn.append("")

    return {
        f"{BOARD}/总览.md": "\n".join(overview) + "\n",
        f"{BOARD}/目标树.md": "\n".join(["# 目标树", "", NOTICE, *_goal_lines(db)]) + "\n",
        f"{BOARD}/人物.md": "\n".join(people) + "\n",
        f"{BOARD}/心愿.md": "\n".join(wish_lines) + "\n",
        f"{BOARD}/学习.md": "\n".join(learn) + "\n",
        f"{BOARD}/最近聊天.md": "\n".join(recent_chat(store, today)) + "\n",
        f"{BOARD}/今日计划.md": "\n".join(today_plan(store, today)) + "\n",
        f"{BOARD}/各方面.md": "\n".join(["# 各方面", "", NOTICE,
                                         *("- " + x for x in domain_lines(db, today, cold_start_from(store)))]) + "\n",
        **{f"{BOARD}/{k}": v for k, v in english.vault_pages(store.root, today).items()},
    }


def sync(store: MemoryStore, repo: Path, today: date = None, push: bool = False) -> dict:
    """返回 {"pulled_back": [...], "committed": bool}。"""
    repo = Path(repo)
    today = today or date.today()
    if push and _git(repo, "rev-parse", "--verify", "@{u}", check=False).returncode == 0:
        _git(repo, "pull", "--ff-only")  # 刚 clone 的空仓库还没有上游分支，跳过
    state_path = store.root / STATE_FILE
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}

    # 1. 收回 Mia 改过的 Wiki 页
    wiki_root = store.wiki.root
    vault_wiki = repo / "wiki"
    pulled = []
    if vault_wiki.is_dir():
        for path in sorted(vault_wiki.rglob("*.md")):
            rel = path.relative_to(vault_wiki).as_posix()
            if rel == "index.md":
                continue
            text = path.read_text(encoding="utf-8")
            if state.get(rel) != _hash(text):
                target = wiki_root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                pulled.append(rel)
    if pulled:
        store.wiki._rebuild_index()

    # 2. 导出 Wiki 和看板
    exported = {}
    for path in sorted(wiki_root.rglob("*.md")):
        rel = path.relative_to(wiki_root).as_posix()
        text = path.read_text(encoding="utf-8")
        dest = vault_wiki / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        exported[rel] = _hash(text)
    for rel, text in board_pages(store, today).items():
        dest = repo / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    state_path.write_text(json.dumps(exported, ensure_ascii=False, indent=1), encoding="utf-8")

    # 3. 提交
    _git(repo, "add", "-A", "wiki", BOARD)
    committed = _git(repo, "diff", "--cached", "--quiet", check=False).returncode != 0
    if committed:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        _git(repo, "commit", "-m", f"小伴 同步 {stamp}")
        if push:
            _git(repo, "push", "-u", "origin", "HEAD")
    return {"pulled_back": pulled, "committed": committed}


def main():
    parser = argparse.ArgumentParser(description="同步 小伴 记忆到 Obsidian 看板仓库")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--push", action="store_true")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    try:
        result = sync(store, args.repo, push=args.push)
    finally:
        store.close()
    print(f"收回 {len(result['pulled_back'])} 页修改；" + ("已提交" if result["committed"] else "无变化"))


if __name__ == "__main__":
    main()
