"""费用账本：按月记录每次模型调用，判断 OpenRouter 预算状态。"""

import json
from datetime import datetime, timezone
from pathlib import Path

WARN_RATIO = 0.8


class Ledger:
    def __init__(self, root: Path, monthly_budget_usd: float = 50.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = monthly_budget_usd

    def _path(self, at: datetime) -> Path:
        return self.root / f"{at.strftime('%Y-%m')}.jsonl"

    def record(self, backend: str, model: str, cost_usd: float, purpose: str = "chat",
               at: datetime = None):
        at = at or datetime.now(timezone.utc)
        entry = {"at": at.isoformat(timespec="seconds"), "backend": backend, "model": model,
                 "cost_usd": round(cost_usd or 0.0, 6), "purpose": purpose}
        with self._path(at).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def month_spend(self, backend: str = "openrouter", at: datetime = None) -> float:
        path = self._path(at or datetime.now(timezone.utc))
        if not path.exists():
            return 0.0
        total = 0.0
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry["backend"] == backend:
                total += entry["cost_usd"]
        return round(total, 6)

    def budget_status(self, at: datetime = None) -> str:
        """ok / warn（达到 80%）/ over（达到上限）。只统计实际花钱的 OpenRouter。"""
        spend = self.month_spend("openrouter", at)
        if spend >= self.budget:
            return "over"
        if spend >= self.budget * WARN_RATIO:
            return "warn"
        return "ok"
