"""原始对话层：按天的 JSONL 文件，只追加不修改。"""

import json
from datetime import datetime, timezone
from pathlib import Path


class RawLog:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def append(self, channel: str, role: str, text: str, at: datetime = None, **extra):
        at = at or datetime.now(timezone.utc)
        record = {"at": at.isoformat(timespec="seconds"), "channel": channel,
                  "role": role, "text": text, **extra}
        path = self.root / f"{at.date().isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def read_day(self, day: str) -> list:
        path = self.root / f"{day}.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line]

    def recent(self, limit: int) -> list:
        records = []
        for path in sorted(self.root.glob("*.jsonl"), reverse=True):
            records = self.read_day(path.stem) + records
            if len(records) >= limit:
                break
        return records[-limit:] if limit else []
