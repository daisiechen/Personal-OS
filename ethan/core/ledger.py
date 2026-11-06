"""费用账本：按月记录每次模型调用，判断 OpenRouter 预算状态；按 5 小时滑动窗口算订阅用量（额度预算）。"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

WARN_RATIO = 0.8
TOKENS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
WINDOW_HOURS = 5   # 订阅额度按 5 小时滚动（docs/design/build-plan.md）
AHEAD_HOURS = 1    # “预计”：再过这么久，窗口里会是多少（按最近一小时的速度往前推）


def _at(entry):
    try:
        t = datetime.fromisoformat(str(entry.get("at", "")))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


class Ledger:
    def __init__(self, root: Path, monthly_budget_usd: float = 50.0):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.budget = monthly_budget_usd

    def _path(self, at: datetime) -> Path:
        return self.root / f"{at.strftime('%Y-%m')}.jsonl"

    def record(self, backend: str, model: str, cost_usd: float, purpose: str = "chat",
               at: datetime = None, tokens: dict = None):
        """tokens：后端拿得到的 input / output / cache_read / cache_write 四个数，拿不到的记 null。"""
        at = at or datetime.now(timezone.utc)
        entry = {"at": at.isoformat(timespec="seconds"), "backend": backend, "model": model,
                 "cost_usd": round(cost_usd or 0.0, 6), "purpose": purpose}
        if tokens is not None:
            entry.update({k: tokens.get(k) for k in TOKENS})
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

    def entries(self, since: datetime, until: datetime = None) -> list:
        """since 之后（含）、until 之前（含）的记录；跨月就多读上一个月的文件。"""
        until = until or datetime.now(timezone.utc)
        months, cur = [], since.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        while cur <= until:
            months.append(cur)
            cur = (cur + timedelta(days=32)).replace(day=1)
        out = []
        for m in months:
            path = self._path(m)
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").split("\n"):
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                t = _at(e)
                if t is not None and since <= t <= until:
                    out.append(e)
        return out

    def window(self, now: datetime = None, hours: float = WINDOW_HOURS, ahead: float = AHEAD_HOURS,
               backend: str = "subscription") -> dict:
        """订阅最近 hours 小时的用量（照 ccusage 的 5 小时窗口 + 消耗速度）。

        cost_usd 是 claude -p 自己算的“按 API 价折算的美元”，已经按 input / output / 缓存读写分别加权，拿它当用量；
        token 四个数照实加总（某条拿不到就不算进那一项）。
        rate_per_hour = 最近一小时的用量；projected = 再过 ahead 小时窗口里会剩的旧用量 + rate × ahead。
        """
        now = now or datetime.now(timezone.utc)
        start = now - timedelta(hours=hours)
        rows = [e for e in self.entries(start, now) if backend is None or e.get("backend") == backend]
        used = sum(e.get("cost_usd") or 0.0 for e in rows)
        last_hour = sum(e.get("cost_usd") or 0.0 for e in rows if _at(e) >= now - timedelta(hours=1))
        stays = sum(e.get("cost_usd") or 0.0 for e in rows if _at(e) >= start + timedelta(hours=ahead))
        tokens = {k: sum(e.get(k) or 0 for e in rows) for k in TOKENS}
        by_purpose = {}
        for e in rows:
            by_purpose[e.get("purpose", "")] = round(by_purpose.get(e.get("purpose", ""), 0.0) + (e.get("cost_usd") or 0.0), 6)
        return {"hours": hours, "calls": len(rows), "used_usd": round(used, 4), "rate_per_hour": round(last_hour, 4),
                "projected_usd": round(stays + last_hour * ahead, 4), "tokens": tokens,
                "tokens_known": sum(1 for e in rows if e.get("input_tokens") is not None), "by_purpose": by_purpose}
