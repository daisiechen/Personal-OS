"""工作台登录：只认绑定了 小伴 的那个 Telegram 账号（settings.json 里的 telegram_owner_chat_id）。

两条路：
- Telegram 里点“打开工作台”（小程序）：页面把 Telegram 给的 initData 发过来，按 Telegram 文档用 bot token 验签名。
- Safari：她在 Telegram 里发“工作台”，小伴 回一个 10 分钟内有效、只能用一次的登录链接。
- 设备链接（2026-10-28，她的 iPad 没有 Telegram）：她在手机上发“设备链接”，小伴 回一个长期有效的链接 /d/<令牌>，
  在 iPad 的 Safari 打开后“添加到主屏幕”，以后点图标直接进。发“撤销设备链接”，之前发过的设备链接全部作废。
验过以后发一个 30 天的会话令牌（存在 cookie 里，也返回给页面）。令牌用 bot token 派生的密钥签名，不另存密钥。
"""

import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl

SESSION_DAYS = 30
LOGIN_MINUTES = 10
INIT_DATA_MAX_AGE = 24 * 3600


def derive_key(bot_token: str) -> bytes:
    return hashlib.sha256(b"earth-online-workbench:" + bot_token.encode()).digest()


def _sign(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode(), hashlib.sha256).hexdigest()[:32]


def verify_init_data(init_data: str, bot_token: str, now: float = None):
    """Telegram 小程序的 initData 验签；通过返回 user 字典，否则 None。"""
    pairs = dict(parse_qsl(init_data or "", keep_blank_values=True))
    got = pairs.pop("hash", "")
    if not got or not bot_token:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if (now or time.time()) - int(pairs.get("auth_date", "0") or 0) > INIT_DATA_MAX_AGE:
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except ValueError:
        return None


def make_session(key: bytes, owner_id: int, now: float = None) -> str:
    exp = int((now or time.time()) + SESSION_DAYS * 86400)
    body = f"{owner_id}.{exp}"
    return f"{body}.{_sign(key, 's:' + body)}"


def check_session(key: bytes, token: str, owner_id, now: float = None) -> bool:
    try:
        uid, exp, sig = (token or "").split(".")
    except ValueError:
        return False
    body = f"{uid}.{exp}"
    return (owner_id is not None and uid == str(owner_id) and int(exp) > (now or time.time())
            and hmac.compare_digest(sig, _sign(key, "s:" + body)))


def make_login_token(key: bytes, now: float = None) -> str:
    exp = int((now or time.time()) + LOGIN_MINUTES * 60)
    body = f"{exp}.{secrets.token_urlsafe(9)}"
    return f"{body}.{_sign(key, 'l:' + body)}"


def make_device_token(key: bytes, version: int) -> str:
    """设备链接的令牌：不过期，靠版本号作废（settings 里的 workbench_device_ver 加一，旧的全失效）。"""
    body = f"{int(version)}.{secrets.token_urlsafe(12)}"
    return f"{body}.{_sign(key, 'd:' + body)}"


def check_device_token(key: bytes, token: str, version: int) -> bool:
    try:
        ver, nonce, sig = (token or "").split(".")
    except ValueError:
        return False
    return ver == str(int(version)) and hmac.compare_digest(sig, _sign(key, "d:" + f"{ver}.{nonce}"))


class LoginTokens:
    """一次性登录链接：签名对、没过期、没用过才放行（用过的记在内存里，服务重启后旧链接也早过期了）。"""

    def __init__(self, key: bytes):
        self.key, self.used = key, set()

    def redeem(self, token: str, now: float = None) -> bool:
        try:
            exp, nonce, sig = (token or "").split(".")
        except ValueError:
            return False
        body = f"{exp}.{nonce}"
        if token in self.used or int(exp) <= (now or time.time()) or not hmac.compare_digest(sig, _sign(self.key, "l:" + body)):
            return False
        self.used.add(token)
        return True
