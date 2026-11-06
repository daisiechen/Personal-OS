"""界面规则的静态检查（docs/design/ui-spec.md 第 4 节、system-rules.md 第 6 节）：每个按钮都有对应的处理，不出现点了没反应的按钮。"""

import re
import unittest
from pathlib import Path

APP = (Path(__file__).resolve().parent.parent / "ethan/workbench/static/app.js").read_text(encoding="utf-8")


CSS = (Path(__file__).resolve().parent.parent / "ethan/workbench/static/app.css").read_text(encoding="utf-8")


class UIRulesTest(unittest.TestCase):
    def test_no_class_defined_twice_at_top_level(self):
        """11-06：赛季页新加的 .cols 和首页三栏同名，把首页日历、任务、赛季副本三栏都挤窄了。同一个类名在最外层只定义一次布局。"""
        names = re.findall(r"^\.([a-zA-Z][\w-]*) \{[^}]*display:", CSS, re.M)
        dup = sorted({n for n in names if names.count(n) > 1})
        self.assertEqual(dup, [], f"这些类名在 app.css 最外层定义了两次布局：{dup}")

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


class SharedComponentTest(unittest.TestCase):
    """2026-11-06 全系统模块化第三批（盘点 research/2026-11-system-modular-audit.md 第 8 节）：
    同一种交互只写一遍——点两次确认、改完副本重拉、弹层标题、保存提示；临时写的字号颜色只减不增。"""

    def test_one_confirm_twice(self):
        # 以前 8 套：S.confirmTask / confirmMark（5 秒）/ confirmOwn / confirmExplore / purge（不过期）/ dropAsk（不过期）……
        self.assertIsNone(re.search(r"S\.(confirm[A-Z]\w*|purge|dropAsk)\b", APP))
        sets = re.findall(r"S\.confirm = (?!null)", APP)
        self.assertEqual(len(sets), 1, "“等确认”只在 confirmTwice 里记")
        self.assertIn("const CONFIRM_MS = 6000;", APP)
        self.assertEqual(len(re.findall(r"setTimeout\(\(\) => \{ if \(S\.confirm", APP)), 1, "过时作废只在 confirmTwice 里")
        self.assertGreaterEqual(APP.count("confirmTwice("), 8)

    def test_quests_reload_in_one_place(self):
        # 以前 10 处各写 S.quests = await api("/api/quests"); render()
        self.assertNotIn('S.quests = await api("/api/quests"); render()', APP)
        n = APP.count('api("/api/quests")')
        self.assertEqual(n, 4, "只许：reloadQuests、flushSteps（不重画）、兑换弹层、副本详情第一次打开")
        self.assertGreaterEqual(APP.count(".then(reloadQuests)"), 8)

    def test_sheet_head_and_save(self):
        self.assertNotIn('<h2 style="font-size:18px">', APP)
        self.assertGreaterEqual(APP.count("${sheetHead(`"), 9)
        self.assertNotIn('S.explore = await api("/api/explore").catch(() => S.explore); renderOverlay(); });', APP)
        self.assertGreaterEqual(APP.count('save("explore.'), 4)

    def test_inline_styles_only_go_down(self):
        """600 多处临时写的 style="…" 先把最常见的换成 app.css 的设计变量和共用小类；剩下的数只减不增（新写的用类）。"""
        self.assertIn("--fs-sm: 12px", CSS)
        for c in (".t-xs", ".t-sm", ".t-md", ".t-h", ".u-between", ".u-g8"):
            self.assertIn(c + " {", CSS)
        self.assertLessEqual(APP.count('style="'), 155)  # 11-06 前 607
        self.assertLessEqual(len(re.findall(r"font-size:\s*\d+px", APP)), 62)  # 11-06 前 321
        self.assertLessEqual(len(re.findall(r"#[0-9A-Fa-f]{6}\b", APP)), 21)  # 十六进制颜色（卡片稀有度、天气插画等，下一轮换变量）


class TaskComponentTest(unittest.TestCase):
    """任务行只有一个渲染组件（taskItem），
    勾选 / 推迟 / 删除 / ★ / 恢复只有一套事件处理；各页面只传 TASKVIEW 里登记过的视图名。"""

    def parts(self):
        return (APP[APP.index("function stageItem("):APP.index("const short = (t) =>")],
                APP[APP.index("const TASKVIEW = {"):APP.index("const taskRow = ")])

    def test_task_actions_only_in_component(self):
        rest = APP
        for part in self.parts():
            rest = rest.replace(part, "")
        for act in ("task", "star", "postpone", "deltask", "undrop"):
            self.assertNotIn(f'data-act="{act}"', rest, act)

    def test_every_view_used_is_registered(self):
        views = set(re.findall(r"^  (\w+):\s+\{ look:", APP[APP.index("const TASKVIEW = {"):], re.M))
        used = set(re.findall(r'taskList\([^,]+, "(\w+)"', APP)) | set(re.findall(r'taskItem\([^,]+, "(\w+)"', APP))
        self.assertEqual(used - views, set())
        self.assertTrue({"today", "other", "later", "dropped", "plan", "stage"} <= used)

