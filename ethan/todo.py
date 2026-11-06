"""可交互的待办清单（Telegram 内联按钮，定）。

她发“待办”弹出清单：每项一行按钮，✓ 完成、⬆ 往前挪、✕ 删掉。
三栏：待办、小伴 答应你的、我们的心愿（两人一起实现的事）。
“加待办 买牛奶”直接加一条；聊天里 小伴 用标记记下的待办和心愿也会出现在这里。
"""

import re
from datetime import date

from . import quests, taskrules
from .clock import her_today

LIST_WORDS = ("待办", "清单", "todo", "/todo")
ADD = re.compile(r"^(?:加待办|/todo add|/add)\s*(.+)$", re.S)
DUE = re.compile(r"\s*@(\d{4}-\d{2}-\d{2})\s*$")
MAX_ITEMS = 15  # 每栏最多显示几条，太长 Telegram 不好点


def is_list_request(text: str) -> bool:
    return text.strip().lower() in LIST_WORDS


def parse_add(text: str):
    """“加待办 交论文 @2026-11-01” → ("交论文", "2026-11-01")；不是加待办返回 None。"""
    m = ADD.match(text.strip())
    if not m:
        return None
    title = m.group(1).strip()
    due = DUE.search(title)
    if due:
        title = title[:due.start()].strip()
    return (title, due.group(1) if due else None) if title else None


def _short(text: str, n: int = 14) -> str:
    return text if len(text) <= n else text[:n - 1] + "…"


def render(db, today: date = None) -> tuple:
    """返回 (消息文字, inline_keyboard)。她的待办和首页一样：今日任务 + 其他（第 9 条，ethan/taskrules.py 的 tg_list 视图），
    不再列全部没做完的；在等的、推到明天的、说算了的、暂停副本的、交给 Claude / Codex / Grok 的都不在这里。"""
    day = today or her_today()
    mine = taskrules.rows(db, "tg_list", day)[:MAX_ITEMS]
    his = taskrules.ethan_tasks(db, day)[:MAX_ITEMS]
    wishes = db.open_wishes()[:MAX_ITEMS]
    lines, keyboard = [], []

    def section(title, items, kind, label, empty):
        lines.append(title)
        if not items:
            lines.append(f"  {empty}")
        for n, item in enumerate(items, 1):
            text = item.get("title") or item.get("quote")
            due = f"（{item['due'][5:]}）" if item.get("due") else ""
            lines.append(f"{label}{n}. {text}{due}")
            keyboard.append([
                {"text": f"✓ {label}{n} {_short(text)}", "callback_data": f"td:done:{kind}:{item['id']}"},
                {"text": "⬆", "callback_data": f"td:up:{kind}:{item['id']}"},
                {"text": "✕", "callback_data": f"td:del:{kind}:{item['id']}"},
            ])
        lines.append("")

    section("📋 待办", mine, "t", "", "暂时没有，发“加待办 xxx”加一条")
    section("🤝 小伴 答应你的", his, "t", "E", "都做完啦")
    section("💞 我们的心愿", wishes, "w", "♡", "还没有，想到什么跟我说")
    keyboard.append([{"text": "🔄 刷新", "callback_data": "td:refresh"}])
    return "\n".join(lines).strip(), keyboard


def handle_callback(db, data: str, today: date = None) -> str:
    """处理按钮，返回给她的小提示（显示在顶部气泡）。"""
    parts = data.split(":")
    if parts[:2] == ["td", "refresh"]:
        return "已刷新"
    if len(parts) != 4 or parts[0] != "td":
        return "这个按钮过期了"
    _, action, kind, raw_id = parts
    item_id = int(raw_id)
    if kind == "t":
        if action == "done":
            gain = taskrules.complete(db, item_id, today or her_today())  # 和工作台同一个完成函数（阶段任务走 stages.check）
            return f"完成一件！+{gain} 分" if gain else "完成一件！"
        if action == "del":
            if not taskrules.delete(db, item_id, today or her_today()):  # 和工作台删除一样记“她删过”
                return "这件已经不在了"
            return "删掉了"
        if action == "up":  # 在她看到的那一栏里往前挪
            day = today or her_today()
            mine, his = [t["id"] for t in taskrules.rows(db, "tg_list", day)], [t["id"] for t in taskrules.ethan_tasks(db, day)]
            db.move_up("tasks", item_id, mine if item_id in mine else his)
            return "往前挪了"
    if kind == "w":
        if action == "done":
            db.close_wish(item_id, "done")
            return "我们又一起实现了一个 💞"
        if action == "del":
            db.close_wish(item_id, "dropped")
            return "先放下了"
        if action == "up":
            db.move_up("wishes", item_id, [w["id"] for w in db.open_wishes()])
            return "往前挪了"
    return "这个按钮过期了"
