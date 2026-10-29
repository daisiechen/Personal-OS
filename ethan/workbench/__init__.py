"""Earth Online 工作台（设计见 docs/design/workbench.md，第一版 2026-10-27）。

服务器上的独立服务 ethan2-workbench，只监听本机 8788，经 Cloudflare Tunnel 对外为 https://os.example.com。
和 小伴 用同一个数据库：打卡、完成、兑换都走 ethan/quests.py，和 Telegram 按钮效果一样。
只认她一个人：Telegram 小程序的签名，或 小伴 发的一次性登录链接（之后 30 天免登录）。
"""

import hashlib
import os
from pathlib import Path

URL = os.environ.get("ETHAN_WORKBENCH_URL", "https://os.example.com")
STATIC = Path(__file__).parent / "static"
# 页面文件的版本号：内容一变就换地址。Cloudflare 和 Telegram 内置浏览器会把静态文件缓存 4 小时，
# 不换地址她手机上就一直是旧脚本（2026-10-27 上线当晚卡在“加载中”就是这个原因）
VERSION = hashlib.sha256(b"".join((STATIC / n).read_bytes() for n in ("index.html", "app.css", "app.js", "moves.js"))).hexdigest()[:10]


def open_button(text: str = "🌍 打开工作台") -> dict:
    """Telegram 里打开工作台小程序的按钮（只在和她的私聊里可用）。"""
    return {"text": text, "web_app": {"url": f"{URL}/?v={VERSION}"}}
