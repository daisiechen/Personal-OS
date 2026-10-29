"""在 Claude Code 会话里和 小伴 聊（2026-10-29 她要：一个固定的 Claude 会话，和 Telegram、微信共享同一份实时记忆）。

走的是和 Telegram 一模一样的处理：她的话记进同一份原始对话（channel 记成 claude），小伴 用同样的人设、记忆、
任务系统回她，回复里的标记（记待办、记账、下周重点……）照样执行。三边是同一个 小伴，聊过的互相都知道。

  python3 -m ethan.chat --data /home/ethan/data "今天好累"
  echo "今天好累" | python3 -m ethan.chat --data /home/ethan/data

只打印 小伴 的回复（一条气泡一行）；生成的照片打印文件路径。固定会话的做法见 deploy/ethan-chat/CLAUDE.md。
"""

import argparse
import sys
from pathlib import Path

from .core.agent import Ethan
from .memory import MemoryStore

CHANNEL = "claude"


def talk(ethan: Ethan, text: str) -> str:
    reply = ethan.reply(CHANNEL, text)
    lines = list(reply.get("bubbles") or [])
    lines += [f"（语音）{v}" for v in reply.get("voice") or []]
    lines += [f"（照片）{p}" for p in reply.get("photo_files") or []]
    lines += [f"（图片）{u}" for u in reply.get("image_urls") or []]
    return "\n".join(lines) if lines else "（小伴 这条没回话）"


def main():
    from . import links
    from .cli import build_router
    from .grok import build_grok
    from .photos import build as build_photos

    parser = argparse.ArgumentParser(description="在 Claude 会话里和 小伴 聊")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("text", nargs="*")
    args = parser.parse_args()
    text = " ".join(args.text).strip() or sys.stdin.read().strip()
    if not text:
        raise SystemExit("没有要说的话")
    store = MemoryStore(args.data)
    try:
        router = build_router(args.data, args.budget)
        ethan = Ethan(store, router, delegate=build_grok(args.data), photographer=build_photos(args.data, router.ledger),
                      reader=links.Reader(args.data))
        print(talk(ethan, text))
    finally:
        store.close()


if __name__ == "__main__":
    main()
