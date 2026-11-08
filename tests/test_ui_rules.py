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
        self.assertLessEqual(APP.count('style="'), 144)  # 11-06 前 607；第三批后 155；11-06 清颜色后 144
        self.assertLessEqual(len(re.findall(r"font-size:\s*\d+px", APP)), 52)  # 11-06 前 321；第三批后 62；清颜色后 52
        self.assertEqual(re.findall(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(\s*\d", APP), [])  # 十六进制 / rgba 颜色清零（11-06 前 21 处）：用 app.css 的变量和类

    def test_colors_only_in_tokens(self):
        """app.css 里的颜色只许写在顶上“设计变量”那一段（两套主题 + 固定色板），别处一律 var(--…)。
        以前 CSS 里硬写的颜色约 300 处（十六进制 209、rgba 86），白天的覆盖规则各自写一遍颜色。"""
        head, sep, rest = CSS.partition("/* ==== 设计变量结束 ==== */")
        self.assertTrue(sep and "/* ==== 设计变量" in head)
        loose = re.findall(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(\s*\d|hsla?\(\s*\d|(?<![\w-])(?:white|black)(?![\w-])", rest)
        self.assertEqual(loose, [], "颜色写进 app.css 顶上的设计变量，再用 var(--…)")
        # 两套主题定义的变量一样多（白天只多“只在白天用的细调”那几个）
        night = set(re.findall(r"(--[\w-]+):", head[head.index(":root, body.night {"):head.index("body.day {")]))
        day = set(re.findall(r"(--[\w-]+):", head[head.index("body.day {"):head.index("/* 固定色板")]))
        self.assertEqual(night - day, set(), "夜间有的主题变量白天也要有")
        self.assertEqual(day - night, {"--panel-shadow", "--primary-fill", "--primary-line", "--primary-emboss", "--on-fill"})
        # 用到的变量都有定义
        bare = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
        inline = set(re.findall(r"(--[\w-]+):", APP))  # app.js 在 style 里给的（进度 --p、柱高 --n、填充 --fill……）
        defined = set(re.findall(r"(--[\w-]+)\s*:", bare)) | inline | {"--eo-a"}
        used = set(re.findall(r"var\((--[\w-]+)", bare))
        self.assertEqual(used - defined, set())

    def test_one_rarity_frame(self):
        """卡片稀有度以前在 .ccard / .bigcard .img / .cardthumb 三份选择器里各写一遍：现在三处都带 .rar（app.js 的 rarity()）。"""
        self.assertIsNone(re.search(r"\.(ccard|cardthumb)\.r[1-4]", CSS))
        self.assertEqual(len(re.findall(r"^\.rar\.r[1-4] \{", CSS, re.M)), 4)
        self.assertEqual(len(re.findall(r"\brarity\(\w+\.rarity\)", APP)), 4)  # 首页小卡、卡册、翻开的大卡（两处）

    def test_saves_go_through_save(self):
        """保存提示只有 save() 一种写法；直接 send 的只剩 save / act / meAct 三个共用入口和副本改名的批量存。"""
        calls = re.findall(r"\bsend\(", APP)
        self.assertEqual(len(calls), 5, "send 只许在：定义、save、act、meAct、flushSteps（批量改名，一次提示）")
        self.assertGreaterEqual(APP.count("save(\""), 8)


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



class DomainPageTest(unittest.TestCase):
    """    所有领域详情页走同一个骨架 domainPage：标题 → 这个领域 → 健康线 → 图表 → 副本 → 探索地图 → 资料（docs/design/territories.md）。"""

    def test_one_order_for_every_domain(self):
        self.assertIn('const DOMAIN_ORDER = ["score", "health", "charts", "bosses", "explore", "brain"];', APP)
        page = APP[APP.index("function territoryPage()"):APP.index("const DOMAIN_ORDER = ")]
        self.assertIn("return domainPage(head, parts);", page)
        parts = page[page.index("const parts = {"):page.index("const head = ")]
        keys = re.findall(r"^    (\w+): ", parts, re.M)
        self.assertEqual(keys, ["score", "health", "charts", "bosses", "explore", "brain"])
        # 健康线、分数、图只在骨架里出现一次，各领域不另排
        self.assertEqual(APP.count("<h2>健康线 · 本周</h2>"), 1)
        self.assertEqual(APP.count("scorePanel(t.score, t)"), 1)
        self.assertEqual(APP.count(".map(metricChart)"), 1)
        for fn in ("weightChart(t.weights)", "spendChart(t.spend)"):
            self.assertEqual(APP.count(fn), 1, fn)
            self.assertIn(fn, APP[APP.index("const DOMAIN_CHARTS = {"):])
        self.assertNotRegex(page, r"t\.key === \"(body|money|career|study|social|academic)\"")  # 只有生活体验的探索地图按领域出

    def test_boss_bar_one_component(self):
        """副本进度条只有 bossBar 一处画，旁边写依据（按指标 / 按里程碑 / 按步骤），没依据不画。"""
        bar = APP[APP.index("const bossBar = "):APP.index("function bossPill(")]
        self.assertIn("barby", bar)
        self.assertIn("p && p.frac != null ?", bar)
