"""记忆服务门面：各入口只通过这里读写记忆。"""

from datetime import date
from pathlib import Path

from .db import StructuredDB
from .raw import RawLog
from .wiki import Wiki


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

    def load_context(self, today: date = None, recent_messages: int = 30) -> dict:
        """每次对话开头调用，返回常驻层的今日简报。"""
        today = today or date.today()
        about = self.wiki.read("about-mia")["body"] if self.wiki.exists("about-mia") else ""
        return {
            "today": today.isoformat(),
            "about_mia": about,
            "topics": self.wiki.read("topics")["body"] if self.wiki.exists("topics") else "",
            "wiki_pages": self.wiki.pages(),
            "active_goals": self.db.active_goals(),
            "goal_chain": self.db.goal_chain(),
            "due_tasks": self.db.due_tasks(today),
            "open_tasks": self.db.open_tasks(),
            "review_items": self.db.due_learning_items(today),
            "people_to_contact": self.db.people_to_contact(today),
            "open_wishes": self.db.open_wishes(),
            "recent_messages": self.raw.recent(recent_messages),
        }
