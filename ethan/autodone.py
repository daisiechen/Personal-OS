"""系统自己看得出来做完了的事，自动勾上（2026-10-29 她问：我跟你一起做完的事，系统能不能自己认出来勾上）。

每次打开工作台、小伴 回话前检查一遍：某个数据源真的接上了（有数据进来），就把标题里提到它的待办勾掉，照常给分。
只认这些确定的信号，认不准的不勾：她在聊天里说做完了，还是靠 小伴 加 [结果 #编号 完成]。
"""

import json
import re
from datetime import date

from . import finance, places_log, quests, worktime

# (待办标题里出现的词, 怎么判断做完了, 说明)
RULES = [
    # 只认“接上某个数据源”这类设置型待办，免得“还招行信用卡”“Monzo 转账”被误勾（2026-10-29 自查）
    (re.compile(r"(接|授权|连).{0,8}Monzo|Monzo.{0,8}(授权|接入|同步)|自动化数据接入", re.I),
     lambda db, root: bool(finance.load_tokens(root).get("last_sync")), "Monzo 已经在同步"),
    (re.compile(r"(招行|招商银行|银行).{0,10}(自动转发|转发|接入)|(消费|银行)(短信|邮件).{0,6}转发|转发.{0,6}(招行|银行|消费)"),
     lambda db, root: finance.has_source(db, "sms"), "银行卡的账已经进来了"),
    # “定位”只认手机定位：2026-10-29“定好小红书和 IG 定位”被误勾，她撤销了又被勾回去
    (re.compile(r"手机定位|定位.{0,6}(快捷指令|自动化|接上|接入)|(接|设).{0,6}定位|出门.{0,4}到家|快捷指令.{0,6}(离开|到达)"),
     lambda db, root: places_log.has_data(db), "手机定位已经接上"),
    (re.compile(r"工作时长|Mac.{0,6}(记录|脚本)"), lambda db, root: worktime.has_data(db), "Mac 工作时长已经在记"),
]


CHECK_SQL = "CREATE TABLE IF NOT EXISTS task_checks (task_id INTEGER PRIMARY KEY, result TEXT NOT NULL, at TEXT NOT NULL)"


def her_undo(db, task_id: int):
    """她自己撤销过的，系统以后不再自动勾（她说了算）。"""
    db.conn.execute(CHECK_SQL)
    db.conn.execute("INSERT OR REPLACE INTO task_checks (task_id, result, at) VALUES (?, 'her_undo', datetime('now'))", (task_id,))
    db.conn.commit()


def _repair(db, day: date) -> list:
    """以前自动勾的，按现在的规则已经认不出来了（规则写宽误勾的），撤回去。"""
    db.conn.execute(CHECK_SQL)
    fixed = []
    for r in db.conn.execute("SELECT t.id, t.title FROM task_checks c JOIN tasks t ON t.id = c.task_id "
                             "WHERE c.result = 'auto' AND t.done = 1").fetchall():
        if not any(p.search(r["title"]) for p, _, _ in RULES):
            quests.uncomplete_task(db, r["id"])
            db.conn.execute("UPDATE task_checks SET result = 'auto_reverted' WHERE task_id = ?", (r["id"],))
            fixed.append(r["title"])
    if fixed:
        db.add_feedback("系统撤回了误勾的：" + "；".join(fixed)[:400], source="system")
        db.conn.commit()
    return fixed


ENABLED = False  # 2026-10-29 她定：所有待办和任务都由她自己勾，系统不再自动勾（以前误勾的照样撤回）


def check(db, root, day: date) -> list:
    """返回这次自动勾掉的 [(编号, 标题, 原因)]。"""
    _repair(db, day)
    if not ENABLED:
        return []
    done = []
    rows = db.conn.execute("SELECT id, title FROM tasks WHERE done = 0 AND promised_by_ethan = 0 "
                           "AND id NOT IN (SELECT task_id FROM task_checks WHERE result IN ('her_undo', 'auto_reverted'))").fetchall()
    for pattern, ok, why in RULES:
        hits = [r for r in rows if pattern.search(r["title"])]
        if not hits:
            continue
        try:
            if not ok(db, root):
                continue
        except Exception:
            continue
        for r in hits:
            quests.complete_task(db, r["id"], day)
            done.append((r["id"], r["title"], why))
            # 系统认出来的不是她点的，小伴 不用再侧面确认（agent.py 的 task_checks）
            db.conn.execute(CHECK_SQL)
            db.conn.execute("INSERT OR REPLACE INTO task_checks (task_id, result, at) VALUES (?, 'auto', datetime('now'))", (r["id"],))
    if done:
        db.add_feedback("系统自动勾掉：" + "；".join(f"{t}（{w}）" for _, t, w in done)[:500], source="system")
    return done
