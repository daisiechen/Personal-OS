"""界面规则的静态检查（docs/design/ui-spec.md 第 4 节、system-rules.md 第 6 节）：每个按钮都有对应的处理，不出现点了没反应的按钮。"""

import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parent.parent / "ethan/workbench/static/app.js").read_text(encoding="utf-8")


class UIRulesTest(unittest.TestCase):
    def test_every_button_action_is_handled(self):
        acts = set(re.findall(r'data-act="([a-z0-9_-]+)"', APP))
        handled = set(re.findall(r'a === "([a-z0-9_-]+)"', APP))
        for grp in re.findall(r"\[([^\]]+)\]\.includes\(a\)", APP):
            handled |= set(re.findall(r'"([a-z0-9_-]+)"', grp))
        self.assertEqual(sorted(acts - handled), [])

    def test_no_browser_dialogs(self):  # 交互规范：不用浏览器自带的弹框
        self.assertIsNone(re.search(r"\b(window\.)?(alert|confirm|prompt)\(", APP))

    def test_writes_go_through_send(self):
        # Codex 评审 11-05：保存失败时还提示成功、提前清草稿。写操作统一走 send()（只有服务器回 ok 才算成），
        # 直接调 /api/act 的只剩只读或不用告诉她结果的几处
        direct = re.findall(r'api\("/api/act", \{ action(?:: "([a-z.]+)")?', APP)
        self.assertEqual(sorted(direct), sorted(["", "diary.mark", "desk.translate", "ach.seen", "ach.seen"]))
        self.assertNotIn('.then(() => showUndo("日记的改动保存好了"))', APP)
        self.assertNotIn("catch (e) {} }", APP)  # 副本改名失败不再吞掉
