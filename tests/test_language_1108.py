""""""

import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan.core.agent import LANG, build_system_prompt, parse_reply


class LanguageTest(unittest.TestCase):
    def test_prompt_follows_her_or_fixed(self):
        self.assertIn("她用英文说你就用英文回", build_system_prompt("我是 小伴", {"today": "2026-11-08"}))
        self.assertIn("一直用英文回", build_system_prompt("我是 小伴", {"today": "2026-11-08", "chat_lang": "英文"}))

    def test_marker_not_sent(self):
        got = parse_reply("OK, I'll stick to English from now on [语言 英文]")["bubbles"]
        self.assertFalse(any("语言" in b for b in got))
        self.assertEqual(LANG.search("好 [语言 跟着她]").group(1), "跟着她")


class FakeWhisper:
    def __init__(self, lang):
        self.lang, self.calls = lang, []

    def transcribe(self, path, language=None, vad_filter=True, initial_prompt=None):
        self.calls.append(language)
        text = "你好" if (language or self.lang) == "zh" else "Hi there"
        return iter([SimpleNamespace(text=text)]), SimpleNamespace(language=self.lang)


class TranscribeTest(unittest.TestCase):
    def run_with(self, lang):
        from ethan.channels import media
        t = media.WhisperTranscriber.__new__(media.WhisperTranscriber)
        t._model = FakeWhisper(lang)
        return t(Path("x.ogg")), t._model.calls

    def test_english_kept_english(self):
        self.assertEqual(self.run_with("en"), ("Hi there", [None]))

    def test_chinese_uses_mandarin_prompt(self):
        self.assertEqual(self.run_with("zh"), ("你好", [None, "zh"]))


if __name__ == "__main__":
    unittest.main()
