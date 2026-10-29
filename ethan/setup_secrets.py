"""在服务器上安全录入密钥：输入时不回显，只写进 600 权限的文件，不经过聊天。

用法（Mia 在自己电脑的终端运行）：
  ssh -t my-server "sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.setup_secrets'"

直接回车表示保留已有的值。绑定口令自动生成并显示一次，用于在 Telegram 里发 /start <口令>。
"""

import argparse
import getpass
import os
import re
import secrets
from pathlib import Path

from .core.backends import FALLBACK_MODEL, OLD_FALLBACK_MODELS

FIELDS = [
    # (变量名, 提示, 是否必填, 校验)
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token", True, re.compile(r"^\d+:[A-Za-z0-9_-]{30,}$")),
    ("CLAUDE_CODE_OAUTH_TOKEN", "Claude 令牌（sk-ant-oat 开头）", True, re.compile(r"^sk-ant-\S{20,}$")),
    ("OPENROUTER_API_KEY", "OpenRouter key（可选，直接回车跳过）", False, re.compile(r"^\S{20,}$")),
    ("GROK_WEBHOOK_URL", "Grok Bot webhook 地址（可选，https 开头）", False, re.compile(r"^https://\S+$")),
    ("GROK_WEBHOOK_SECRET", "Grok Bot 发送密钥（可选）", False, re.compile(r"^\S{8,}$")),
    ("ICLOUD_USERNAME", "Apple ID 邮箱（苹果日历，可选）", False, re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    ("ICLOUD_APP_PASSWORD", "Apple App 专用密码（xxxx-xxxx-xxxx-xxxx，可选）", False,
     re.compile(r"^[a-z]{4}-[a-z]{4}-[a-z]{4}-[a-z]{4}$")),
    ("MONZO_CLIENT_ID", "Monzo 开发者客户端 ID（oauth2client_ 开头，可选）", False, re.compile(r"^oauth2client_\S+$")),
    ("MONZO_CLIENT_SECRET", "Monzo 客户端密钥（mnzconf 或 mnzpub 开头，可选）", False, re.compile(r"^mnz\S{10,}$")),
    ("GEMINI_API_KEY", "Google AI Studio 的 Gemini API key（AIza 开头，小伴 发照片用，可选）", False,
     re.compile(r"^AIza[0-9A-Za-z_-]{30,}$")),
    ("INBOX_TOKEN_FANCY", "DemoApp 举报提醒密钥（直接回车：已有就保留，没有就自动生成）", False, re.compile(r"^[A-Za-z0-9_-]{32,}$")),
]
BACKUP_KEY = re.compile(r"^\S{32,}$")


def read_env(path: Path) -> dict:
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def write_secrets(config_dir: Path, answers: dict, backup_key: str = "") -> dict:
    """answers 中空字符串表示保留原值。返回最终 env（调用方不得打印密钥）。"""
    config_dir = Path(config_dir)
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    env_path = config_dir / "ethan.env"
    env = read_env(env_path)
    if env.get("ETHAN_OPENROUTER_MODEL", OLD_FALLBACK_MODELS[0]) in OLD_FALLBACK_MODELS:
        env["ETHAN_OPENROUTER_MODEL"] = FALLBACK_MODEL
    for name, label, required, pattern in FIELDS:
        value = answers.get(name, "").strip()
        if value:
            if not pattern.match(value):
                raise ValueError(f"{label} 格式不对，请检查是否复制完整")
            env[name] = value
        elif required and not env.get(name):
            raise ValueError(f"{label} 不能为空")
        else:
            env.setdefault(name, "")
    key_path = config_dir / "backup.key"
    if backup_key.strip() and not BACKUP_KEY.match(backup_key.strip()):
        raise ValueError("备份密钥太短，请检查是否复制完整")
    if not backup_key.strip() and not key_path.exists():
        raise ValueError("备份密钥不能为空")
    if not env.get("ETHAN_PAIRING_CODE"):
        env["ETHAN_PAIRING_CODE"] = secrets.token_hex(4)
    if not env.get("INBOX_TOKEN_GROK"):  # Grok 往接收口回报用，自动生成，Mia 抄给 Grok
        env["INBOX_TOKEN_GROK"] = secrets.token_urlsafe(32)
    if not env.get("INBOX_TOKEN_CLOUD"):  # Claude 云端会话找 Grok 用，自动生成，Mia 抄进云端环境设置
        env["INBOX_TOKEN_CLOUD"] = secrets.token_urlsafe(32)
    if not env.get("INBOX_TOKEN_FANCY"):  # DemoApp 推举报提醒用，自动生成，Mia 抄进 DemoApp 部署配置的 ETHAN_MODERATION_TOKEN
        env["INBOX_TOKEN_FANCY"] = secrets.token_urlsafe(32)

    old_umask = os.umask(0o077)
    try:
        lines = ["# 由 ethan.setup_secrets 生成，权限 600，不要提交到任何仓库"]
        lines += [f"{k}={v}" for k, v in env.items()]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.chmod(env_path, 0o600)
        if backup_key.strip():
            key_path.write_text(backup_key.strip(), encoding="utf-8")
            os.chmod(key_path, 0o600)
    finally:
        os.umask(old_umask)
    return env


def main():
    parser = argparse.ArgumentParser(description="录入 小伴 的密钥")
    parser.add_argument("--dir", type=Path, default=Path.home() / ".config" / "ethan")
    args = parser.parse_args()
    existing = read_env(args.dir / "ethan.env")
    print("逐项粘贴后回车。输入时屏幕不显示任何字符，这是正常的。已有的值直接回车保留。\n")
    answers = {}
    for name, label, _, _ in FIELDS:
        hint = "（已有，回车保留）" if existing.get(name) else ""
        answers[name] = getpass.getpass(f"{label}{hint}: ")
    hint = "（已有，回车保留）" if (args.dir / "backup.key").exists() else ""
    backup_key = getpass.getpass(f"记忆备份密钥{hint}: ")
    try:
        env = write_secrets(args.dir, answers, backup_key)
    except ValueError as e:
        print(f"\n没有保存：{e}。请重新运行。")
        raise SystemExit(1)
    print("\n已保存。")
    for name, label, _, _ in FIELDS:
        print(f"  {label}: {'已设置' if env.get(name) else '未设置'}")
    print("  记忆备份密钥: 已设置")
    print(f"\n绑定口令：{env['ETHAN_PAIRING_CODE']}")
    print("服务启动后，在 Telegram 给新 bot 发送：/start " + env["ETHAN_PAIRING_CODE"])
    print(f"\nGrok 回报用的密钥（填进 Grok Bot，不要发在聊天里）：{env['INBOX_TOKEN_GROK']}")
    print(f"\n云端会话用的密钥（填进 Claude 云端环境设置的 ETHAN_CLOUD_TOKEN，不要发在聊天里）：{env['INBOX_TOKEN_CLOUD']}")
    print(f"\nDemoApp 举报提醒用的密钥（填进 DemoApp 部署配置的 ETHAN_MODERATION_TOKEN，不要发在聊天里）：{env['INBOX_TOKEN_FANCY']}")


if __name__ == "__main__":
    main()
