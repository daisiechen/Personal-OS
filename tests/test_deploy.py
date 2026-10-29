import configparser
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy"
OLD_PATHS = ("Personal-Assistant", "ethan-workbench", "Obsidian", "/home/ubuntu")


class DeployConfigTest(unittest.TestCase):
    def units(self):
        return sorted((DEPLOY / "systemd").glob("*"))

    def test_units_isolated_under_ethan_user(self):
        for unit in self.units():
            self.assertTrue(unit.name.startswith("ethan2-"), unit.name)
            text = unit.read_text(encoding="utf-8")
            for old in OLD_PATHS:
                self.assertNotIn(old, text, f"{unit.name} 引用了旧版路径 {old}")
            if unit.suffix == ".service":
                cfg = configparser.ConfigParser(interpolation=None)
                cfg.read_string(text)
                # 自动部署要重启服务、装 systemd 文件，只有它以 root 运行（git 和测试仍用 ethan）
                self.assertEqual(cfg["Service"]["User"], "root" if unit.name == "ethan2-autodeploy.service" else "ethan")
                for path in re.findall(r"/home/\w+", text):
                    self.assertEqual(path, "/home/ethan")

    def test_install_script_does_not_touch_old_services(self):
        script = (DEPLOY / "install.sh").read_text(encoding="utf-8")
        self.assertNotRegex(script, r"systemctl (stop|disable)")
        for old in OLD_PATHS:
            self.assertNotIn(old, script)

    def test_commands_point_to_current_server(self):
        # 2026-10-26 迁到洛杉矶，旧服务器不再跑 小伴，录入密钥、扫码等命令不能再指向旧服务器
        for path in (ROOT / "docs" / "operations" / "deploy.md", ROOT / "ethan" / "setup_secrets.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("my-other-server", text, path.name)
            self.assertIn("ssh -t my-server", text, path.name)

    def test_env_example_has_no_values_for_secrets(self):
        for line in (DEPLOY / "ethan.env.example").read_text(encoding="utf-8").splitlines():
            if line.startswith(("TELEGRAM_BOT_TOKEN", "ETHAN_PAIRING_CODE", "OPENROUTER_API_KEY", "GROK_WEBHOOK_", "INBOX_TOKEN_", "ICLOUD_APP_PASSWORD",
                                "CLAUDE_CODE_OAUTH_TOKEN")):
                self.assertTrue(line.endswith("="), line)


if __name__ == "__main__":
    unittest.main()
