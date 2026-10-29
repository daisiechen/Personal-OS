import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan.memory import MemoryStore

# 2026-10-29 线上事故：Grok 回报正文里带了 U+2028，str.splitlines() 会在那里把一行切断，读整份记录报错，主动消息一上午都没发出去
TEXT = "展厅狭小而密集 每件展品都介于艺术与病态之间 📍 地址：伦敦"


class JsonlSeparatorTest(unittest.TestCase):
    def test_line_separator_in_text_does_not_break_readers(self):
        from ethan import agents, inbox
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = MemoryStore(root)
            store.raw.append("telegram", "ethan", TEXT)
            day = date.today().isoformat()
            self.assertEqual(store.raw.read_day(day)[-1]["text"], TEXT)
            (root / "inbox").mkdir()
            (root / "inbox" / f"{day}.jsonl").write_text(json.dumps({"at": f"{day}T00:00:00+00:00", "path": "/grok/result",
                                                                    "text": TEXT}, ensure_ascii=False) + "\n", encoding="utf-8")
            self.assertEqual(len(inbox.recent(root, hours=10 ** 6)), 1)
            agents.grok_live(root, hours=10 ** 6)
            store.close()
