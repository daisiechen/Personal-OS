"""模型路由：订阅优先。订阅不可用时不自动切换，先提醒 Mia，由她决定是否启用 OpenRouter。"""

import json
from pathlib import Path

from .backends import BackendUnavailable
from .ledger import Ledger


class AllBackendsFailed(Exception):
    pass


class NeedsSwitchApproval(Exception):
    """订阅不可用、且 Mia 尚未同意切到 OpenRouter。入口层应提醒她并询问是否切换。"""


class BudgetExceeded(Exception):
    """OpenRouter 本月预算已用完。入口层应提醒 Mia，由她决定是否提高预算或换便宜模型。"""


class Settings:
    """持久化开关，存在数据目录的 settings.json。"""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}

    def get(self, key, default=None):
        return self._load().get(key, default)

    def set(self, key, value):
        data = self._load()
        data[key] = value
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


class Router:
    def __init__(self, primary, ledger: Ledger, settings: Settings, fallback=None):
        """primary 为订阅后端；fallback 为 OpenRouter，仅在 Mia 开启后使用。"""
        self.primary = primary
        self.fallback = fallback
        self.ledger = ledger
        self.settings = settings

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.settings.get("openrouter_enabled", False))

    def enable_fallback(self, enabled: bool):
        self.settings.set("openrouter_enabled", enabled)

    def complete(self, system: str, messages: list, purpose: str = "chat", images=()):
        errors = []
        if not self.fallback_enabled:
            try:
                return self._call(self.primary, system, messages, purpose, images)
            except BackendUnavailable as e:
                if self.fallback is None:
                    raise AllBackendsFailed(f"{self.primary.name}: {e}") from e
                raise NeedsSwitchApproval(str(e)) from e
        # Mia 已同意切换：先试订阅（可能已恢复），再走 OpenRouter
        try:
            return self._call(self.primary, system, messages, purpose, images)
        except BackendUnavailable as e:
            errors.append(f"{self.primary.name}: {e}")
        if self.ledger.budget_status() == "over":
            raise BudgetExceeded(f"本月 OpenRouter 已花 {self.ledger.month_spend():.2f} 美元")
        try:
            return self._call(self.fallback, system, messages, purpose, images)
        except BackendUnavailable as e:
            errors.append(f"{self.fallback.name}: {e}")
        raise AllBackendsFailed("; ".join(errors))

    def _call(self, backend, system, messages, purpose, images=()):
        reply = backend.complete(system, messages, images=images)
        self.ledger.record(reply.backend, reply.model, reply.cost_usd, purpose)
        return reply
