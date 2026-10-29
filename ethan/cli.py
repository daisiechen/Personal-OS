"""命令行试聊：python3 -m ethan.cli --data <记忆数据目录>"""

import argparse
import os
from pathlib import Path

from .core.agent import Ethan
from .core.backends import FALLBACK_MODEL, OLD_FALLBACK_MODELS, OpenRouterBackend, SubscriptionBackend
from .core.ledger import Ledger
from .core.router import BudgetExceeded, NeedsSwitchApproval, Router, Settings
from .memory import MemoryStore


def build_router(data: Path, budget: float) -> Router:
    key = os.environ.get("OPENROUTER_API_KEY")
    model = os.environ.get("ETHAN_OPENROUTER_MODEL") or FALLBACK_MODEL
    if model in OLD_FALLBACK_MODELS:  # 老配置里写死的 Claude，她的账号用不了
        model = FALLBACK_MODEL
    fallback = OpenRouterBackend(key, model=model) if key else None
    primary = SubscriptionBackend()
    base, api_key = os.environ.get("ETHAN_LLM_BASE_URL", "").strip(), os.environ.get("ETHAN_LLM_API_KEY", "").strip()
    if base and api_key:  # 主力换成兼容 OpenAI 接口的模型，比如 DeepSeek：https://api.deepseek.com + deepseek-chat
        primary = OpenRouterBackend(api_key, model=os.environ.get("ETHAN_LLM_MODEL") or "deepseek-chat",
                                    url=base.rstrip("/") + "/chat/completions", name="api")
    return Router(primary, Ledger(data / "ledger", budget),
                  Settings(data / "settings.json"), fallback=fallback)


def main():
    parser = argparse.ArgumentParser(description="和 小伴 在命令行聊天")
    parser.add_argument("--data", type=Path, required=True, help="记忆数据目录")
    parser.add_argument("--budget", type=float, default=50.0, help="OpenRouter 月度预算（美元）")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    ethan = Ethan(store, build_router(args.data, args.budget))
    try:
        while True:
            text = input("你：").strip()
            if text in ("exit", "quit"):
                break
            if text == "/openrouter on":
                ethan.router.enable_fallback(True)
                print("已切到 OpenRouter 兜底")
                continue
            if text == "/openrouter off":
                ethan.router.enable_fallback(False)
                print("已关闭 OpenRouter 兜底，只用订阅")
                continue
            if text:
                try:
                    for bubble in ethan.respond("cli", text):
                        print(f"小伴：{bubble}")
                except NeedsSwitchApproval as e:
                    print(f"[提醒] Claude 订阅暂时用不了（{e}）。输入 /openrouter on 切到 OpenRouter。")
                except BudgetExceeded as e:
                    print(f"[提醒] {e}，已到月度上限，需要你决定是否提高预算。")
    except (EOFError, KeyboardInterrupt):
        pass
    finally:
        store.close()


if __name__ == "__main__":
    main()
