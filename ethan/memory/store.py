"""记忆服务门面：各入口只通过这里读写记忆。"""

from datetime import date
from pathlib import Path

from .db import StructuredDB
from .raw import RawLog
from .wiki import Wiki
from ..clock import her_today


class MemoryStore:
    """数据目录结构：

    <root>/ethan.sqlite   结构化数据
    <root>/wiki/          Markdown Wiki（关于 Mia、关系、知识库）
    <root>/raw/           原始对话，按天 JSONL
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = StructuredDB(self.root / "ethan.sqlite")
        self.wiki = Wiki(self.root / "wiki")
        self.raw = RawLog(self.root / "raw")

    def close(self):
        self.db.close()

    def _relations(self, today) -> str:
        try:
            from .. import relations
            return relations.chat_hint(self.db, today)
        except Exception:
            return ""

    def _tasks(self, today) -> dict:
        """小伴 聊天时看的待办单：和首页同一份（今日任务 + 其他），另外标出在等、推到明天、还没到开始日的；她说算了的、
        暂停副本的、交给 Claude / Codex / Grok 的不给他（第 7 条，ethan/taskrules.py 的 ethan_ctx 视图）。他自己答应的标“你答应的”。"""
        from .. import taskrules
        keep = ("id", "title", "due", "goal_id")
        mine = [{**{k: t.get(k) for k in keep}, "promised_by_ethan": 0, **({"state": t["label"]} if t["label"] else {})}
                for t in taskrules.rows(self.db, "ethan_ctx", today)]
        his = [{**{k: t.get(k) for k in keep}, "promised_by_ethan": 1, "state": "你答应的"} for t in taskrules.ethan_tasks(self.db, today)]
        rows = mine + his
        return {"open_tasks": rows, "due_tasks": [t for t in rows if t["due"] and t["due"] <= today.isoformat()]}

    def load_context(self, today: date = None, recent_messages: int = 30) -> dict:
        """每次对话开头调用，返回常驻层的今日简报。"""
        today = today or her_today()
        about = self.wiki.read("about-mia")["body"] if self.wiki.exists("about-mia") else ""
        return {
            "today": today.isoformat(),
            "about_mia": about,
            "topics": self.wiki.read("topics")["body"] if self.wiki.exists("topics") else "",
            "wiki_pages": self.wiki.pages(),
            "active_goals": self.db.active_goals(),
            "goal_chain": self.db.goal_chain(),
            **self._tasks(today),
            "review_items": self.db.due_learning_items(today),
            "relations": self._relations(today),
            "open_wishes": self.db.open_wishes(),
            "recent_messages": self.raw.recent(recent_messages),
        }
