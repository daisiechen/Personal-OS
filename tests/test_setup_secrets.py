import stat
import tempfile
import unittest
from pathlib import Path

from ethan.setup_secrets import FIELDS, read_env, write_secrets

TG = "123456789:" + "A" * 35
CLAUDE = "sk-ant-oat01-" + "x" * 40
KEY = "k" * 64


class SetupSecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "cfg"

    def tearDown(self):
        self.tmp.cleanup()

    def mode(self, path):
        return stat.S_IMODE(path.stat().st_mode)

    def test_writes_private_files_and_pairing_code(self):
        env = write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE}, KEY)
        self.assertEqual(self.mode(self.dir), 0o700)
        self.assertEqual(self.mode(self.dir / "ethan.env"), 0o600)
        self.assertEqual(self.mode(self.dir / "backup.key"), 0o600)
        saved = read_env(self.dir / "ethan.env")
        self.assertEqual(saved["TELEGRAM_BOT_TOKEN"], TG)
        self.assertEqual(saved["OPENROUTER_API_KEY"], "")
        self.assertRegex(env["ETHAN_PAIRING_CODE"], r"^[0-9a-f]{8}$")
        self.assertEqual((self.dir / "backup.key").read_text(), KEY)

    def test_gemini_key_field(self):
        pattern = {name: p for name, _, _, p in FIELDS}["GEMINI_API_KEY"]
        self.assertTrue(pattern.match("AIza" + "S" * 35))
        self.assertFalse(pattern.match("sk-or-v1-abc"))

    def test_blank_keeps_existing_and_pairing_code_is_stable(self):
        first = write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE}, KEY)
        new_tg = "987654321:" + "B" * 35
        second = write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": new_tg}, "")
        self.assertEqual(second["TELEGRAM_BOT_TOKEN"], new_tg)
        self.assertEqual(second["CLAUDE_CODE_OAUTH_TOKEN"], CLAUDE)
        self.assertEqual(second["ETHAN_PAIRING_CODE"], first["ETHAN_PAIRING_CODE"])
        self.assertGreaterEqual(len(first["INBOX_TOKEN_GROK"]), 40)
        self.assertEqual(second["INBOX_TOKEN_GROK"], first["INBOX_TOKEN_GROK"])
        self.assertGreaterEqual(len(first["INBOX_TOKEN_CLOUD"]), 40)
        self.assertNotEqual(first["INBOX_TOKEN_CLOUD"], first["INBOX_TOKEN_GROK"])
        self.assertEqual(second["INBOX_TOKEN_CLOUD"], first["INBOX_TOKEN_CLOUD"])
        self.assertGreaterEqual(len(first["INBOX_TOKEN_FANCY"]), 40)  # DemoApp 举报提醒的钥匙：自动生成、独立、回车保留
        self.assertNotIn(first["INBOX_TOKEN_FANCY"], (first["INBOX_TOKEN_GROK"], first["INBOX_TOKEN_CLOUD"]))
        self.assertEqual(second["INBOX_TOKEN_FANCY"], first["INBOX_TOKEN_FANCY"])
        self.assertEqual((self.dir / "backup.key").read_text(), KEY)

    def test_fancy_token_can_be_pasted_and_is_validated(self):
        pasted = "F" * 43
        env = write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE,
                                       "INBOX_TOKEN_FANCY": pasted}, KEY)
        self.assertEqual(env["INBOX_TOKEN_FANCY"], pasted)
        self.assertEqual(write_secrets(self.dir, {}, "")["INBOX_TOKEN_FANCY"], pasted)
        with self.assertRaises(ValueError):
            write_secrets(self.dir, {"INBOX_TOKEN_FANCY": "short"}, "")

    def test_old_claude_fallback_becomes_grok_but_custom_model_is_kept(self):
        self.dir.mkdir(parents=True)
        (self.dir / "ethan.env").write_text("ETHAN_OPENROUTER_MODEL=anthropic/claude-sonnet-4.5\n")
        env = write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE}, KEY)
        self.assertEqual(env["ETHAN_OPENROUTER_MODEL"], "x-ai/grok-4.7")
        (self.dir / "ethan.env").write_text("ETHAN_OPENROUTER_MODEL=meta/muse-spark-1.3\n")
        env = write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE}, "")
        self.assertEqual(env["ETHAN_OPENROUTER_MODEL"], "meta/muse-spark-1.3")

    def test_rejects_missing_or_malformed(self):
        with self.assertRaises(ValueError):
            write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG}, KEY)
        with self.assertRaises(ValueError):
            write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": "abc", "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE}, KEY)
        with self.assertRaises(ValueError):
            write_secrets(self.dir, {"TELEGRAM_BOT_TOKEN": TG, "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE}, "")
        self.assertFalse((self.dir / "ethan.env").exists())


if __name__ == "__main__":
    unittest.main()
