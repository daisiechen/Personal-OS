"""替代清单：改版时换掉的旧东西不再出现（docs/design/system-rules.md 第 2 节）。

每次改版在设计文档里写“替代了什么、怎么下线、怎么确认”，并在这里加一条检查。清单本身见 system-rules.md 第 2 节“已下线清单”。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "ethan/workbench/static/app.js").read_text(encoding="utf-8")


class RetiredTest(unittest.TestCase):
    def test_task_page_gone_from_nav(self):  # 11-01 去掉任务页，主线在首页副本详情里
        menu = re.search(r"const MENU = \[(.*?)\];", APP, re.S).group(1)
        self.assertNotIn('"quests"', menu)

    def test_no_brain_wording_left(self):  # 11-04 大脑 10-31 就并进了“收藏和资料”，领域页、地图上还写着“大脑”
        self.assertNotIn("在大脑里看全部", APP)
        self.assertNotIn(">第二大脑<", APP)

    def test_me_page_old_blocks_gone(self):  # 11-04 “我”页旧版块下线：大号对齐率和周柱子撤掉，使用者和纠错单挪进第二层
        page = APP[APP.index("function mePage()"):APP.index("function meUsersPanel()")]
        self.assertNotIn("mehero", APP)
        # 谁在用和纠错不要收进弹窗，直接一张卡片放在页面里
        self.assertNotIn('data-k="me-users"', APP)
        self.assertNotIn('S.overlay === "me-users"', APP)
        self.assertIn("${meUsersPanel()}", page)

    def test_weekly_social_from_relations(self):  # 11-04 周报的“社交”改从人物卡三张清单取（ethan/relations.py），不再用旧的 people_to_contact
        src = (ROOT / "ethan/report.py").read_text(encoding="utf-8")
        self.assertNotIn('"社交": db.people_to_contact', src)
        self.assertIn("relations.week_line", src)

    def test_handoff_not_only_her_words(self):  # 11-04 微信 / Grok / Codex 交整段对话，不再只交她的话、别人的话只当“问”
        for f in ("wechatexport.py", "grokexport.py", "agentexport.py"):
            src = (ROOT / "ethan" / f).read_text(encoding="utf-8")
            self.assertNotIn("问：（", src, f)
            self.assertNotIn("上一句：“", src, f)
            self.assertNotIn("别人说的（", src, f)
            self.assertIn("transcript", src, f)

    def test_ai_replan_button_gone(self):  # 11-02 去掉“让 AI 重排”按钮，关掉计划面板时自动重排
        self.assertNotIn('data-act="replan"', APP)

    def test_weekly_report_not_sent_separately(self):  # 11-02 周报并进周日 21:00 的复盘
        src = (ROOT / "ethan/report.py").read_text(encoding="utf-8")
        main = src[src.index("def main("):]
        self.assertLess(main.index("周报并进 21:00 的复盘"), main.index("write_report("))

    def test_planner_does_not_hunt_for_english_tutors(self):  # 11-02 英语陪练已经定了用 Grok
        from ethan.planner import DECIDED
        self.assertTrue(DECIDED.search("查英语陪练的渠道（线上或线下）"))
        self.assertFalse(DECIDED.search("英语陪练（Grok）"))

    def test_wasteland_entry_on_season_map(self):  # 11-02 赛季页改版丢了荒原入口，红点没去处；11-06 改成“七块领地”旁的“待归属”按钮
        board = APP[APP.index("function seasonBoard("):APP.index("function reviewsPage(")]
        self.assertIn("<h3>七个领域</h3>", board)  # 11-06 晚她：是七个领域，不是七块领地
        self.assertIn('<span class="u-mlauto">${pendBtn(z.waste)}</span>', board)  # 待归属放到这一行最右边
        self.assertIn('data-k="waste"', APP[APP.index("const pendBtn = "):APP.index("const mapImg = ")])

    def test_waste_not_an_eighth_territory(self):
        """        替代清单见 docs/design/territories.md“待归属”：赛季页分数柱第八根“荒原”柱、大地图上的“荒原”标签撤掉；给她看的“荒原”改叫“待归属”。"""
        board = APP[APP.index("function seasonBoard("):APP.index("function reviewsPage(")]
        self.assertNotIn("const waste = ", board)
        self.assertNotIn("${waste}", board)
        self.assertNotIn("mtag waste", APP)
        self.assertNotIn("MAPPOS.waste", APP)
        self.assertNotIn(".mtag.waste", (ROOT / "ethan/workbench/static/app.css").read_text(encoding="utf-8"))
        self.assertIn('class="rbadge"', APP[APP.index("const pendBtn = "):APP.index("const mapImg = ")])  # 有就带红点
        code = re.sub(r"/\*.*?\*/", "", re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", APP), flags=re.S)
        self.assertNotIn("荒原", code, "给她看的字里不再叫荒原（代码名 waste 不改）")
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        self.assertNotIn('"荒原"', api)

    def test_boss_bar_says_what_it_counts(self):
        """替代清单见 docs/design/territories.md“副本进度条”：
        以前首页、领域页、周回顾、副本详情各画一根 b.frac 进度条，没依据也画一根空条，按什么算各处不一样（api.bosses 里步骤、里程碑先后打架）。"""
        self.assertNotIn("b.frac || 0", APP)
        self.assertEqual(APP.count("${bossBar("), 4)  # 首页、领域页、周回顾、副本详情
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        self.assertNotIn('frac = round(sum(s["done"] for s in steps) / len(steps), 2)', api)
        self.assertNotIn("{round(100 * (b['frac'] or 0))}%", api)
        self.assertIn("stages.boss_progress(", api)

    def test_domain_page_one_skeleton(self):
        """以前左列是 分数 → 指标图 → 健康线 → 各页自己的图，
        有指标图的页健康线在图下，没有的在图上。现在只有 domainPage 一个骨架（DOMAIN_ORDER），各领域的图在 DOMAIN_CHARTS。"""
        self.assertNotRegex(APP, r"\$\{scorePanel\(t\.score[^}]*\)\}\$\{\(t\.charts \|\| \[\]\)\.map\(metricChart\)")
        self.assertNotIn("const extra = t.key ===", APP)
        stats = (ROOT / "ethan/domain_stats.py").read_text(encoding="utf-8")
        for old in ('"每周训练"', '"每周 0 点前睡"', '"每周认知拓展"', '"每周英语陪练"'):  # 不要这几张图
            self.assertNotIn(old, stats, old)

    def test_season_page_v2_replaced_map_and_cards(self):  # 11-05 赛季页第二版（样稿 A + C 的分数柱）：地图关系线、两张大卡、21 天小日历、领域行撤掉
        page = APP[APP.index("function mapPage()"):APP.index("function seasonVerdict(")]
        for old in ("smap", "fcard", "srccard", "srow", "mcal", "ledger", "nextbox"):
            self.assertNotIn(old, page, old)
            self.assertNotIn(f".{old}", (ROOT / "ethan/workbench/static/app.css").read_text(encoding="utf-8"), old)
        for new in ("seasonVerdict(z)", "seasonBoard(z)"):
            self.assertIn(new, page)
        from ethan import season
        self.assertFalse(hasattr(season, "calendar"))

    def test_season_pass_card_gone(self):
        css = (ROOT / "ethan/workbench/static/app.css").read_text(encoding="utf-8")
        for old in ("seasonPass", "赛季通行证", 'class="spass"', "passtop", "railfoot", "nexts"):
            self.assertNotIn(old, APP, old)
        for old in (".spass", ".passtop", ".lvinfo", ".expbar", ".expmeta", ".tracklabel", ".railfoot", ".nexts", ".nx ", ".clock ", ".rail "):
            self.assertNotIn(old, css, old)
        from ethan import season
        self.assertFalse(hasattr(season, "season_pass"))
        self.assertIn("seasonVerdict(z) + seasonBoard(z)", APP)  # 总评 + 建议、分数柱留着

    def test_season_route_card_gone(self):
        css = (ROOT / "ethan/workbench/static/app.css").read_text(encoding="utf-8")
        page = APP[APP.index("function mapPage()"):APP.index("function reviewsPage(")]
        for old in ("seasonRoute", 'class="quest"', 'class="route"', "主副本关卡", "z.route"):
            self.assertNotIn(old, APP if old == "seasonRoute" else page, old)
        for old in (".quest ", ".qhead", ".route", ".stage", ".tpips", ".sayrow", ".says "):
            self.assertNotIn(old, css, old)
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        self.assertNotIn("def main_route", api)
        self.assertIn("sdgo", page)  # 每块领地“接下来怎么调”
        self.assertIn("z.conclusion", page)  # 结论

    def test_life_page_duplicates_gone(self):
        ds = (ROOT / "ethan/domain_stats.py").read_text(encoding="utf-8")
        self.assertNotIn('_chart("每周新体验"', ds)
        self.assertIn('_chart("每周出门天数"', ds)
        page = APP[APP.index("function territoryPage()"):APP.index("function reviewPage()")]
        self.assertNotIn("t.merged", page)  # 11-06 晚她说没让合并：七个领域都单画“健康线 · 本周”
        # 探索地图在副本和资料中间：11-06 晚并进统一骨架（DOMAIN_ORDER 里 bosses → explore → brain，tests/test_ui_rules.py 守着顺序）
        self.assertIn("还没有探索地图，点“编辑”加地方", page)  # 没上传过表格时的提示
        self.assertNotIn('t.key === "life" && ex ?', page)  # 旧的探索地图位置（左栏最下面）

    def test_waste_counted_in_one_place(self):
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        self.assertNotIn('["territory"] is None', api)
        self.assertNotIn('if not t["territory"])', api)
        self.assertGreaterEqual(api.count('taskrules.rows(self.db, "waste", day)'), 4)  # 红点、地图、赛季页、荒原页

    def test_daily_cold_start_rotation_off(self):  # 11-02 冷启动挪进 M2 阶段任务，旧的每日轮换关掉
        src = (ROOT / "ethan/domains.py").read_text(encoding="utf-8")
        self.assertIn("milestone_id IS NOT NULL AND title LIKE '%冷启动%'", src)  # 行为测试见 test_quest_structure


    def test_finance_history_every_other_day_gone(self):  # 11-04 知识点改成“用得上”，金融历史不再隔天固定讲
        from ethan import lessons
        self.assertFalse(hasattr(lessons, "FIRST"))
        self.assertNotIn("金融历史", [t for t, _, _ in lessons.TOPICS])
        agent = (ROOT / "ethan/core/agent.py").read_text(encoding="utf-8")
        self.assertNotIn("金融历史排第一", agent)

    def test_english_no_fixed_days(self):  # 11-04 英语不排固定时间，冷启动不再问哪几天
        from ethan import domains
        how = dict(domains.COLD_START)["学习"]
        self.assertNotIn("每周哪几天", how)
        self.assertIn("不排固定时间", how)

    def test_no_new_direct_reads_of_old_facts(self):  # 11-04 ontology 3.4 旧结构下线：新代码不许直接读旧 facts，白名单见 ethan/retire.py LEGACY
        from ethan import retire
        self.assertEqual(retire.unlisted(), [], "改走 onto_query / memsearch / statements；实在不行写清原因登记进 retire.LEGACY")
        for f, (reads, writes, why) in retire.LEGACY.items():
            self.assertTrue(why, f)

    def test_source_health_counts_statements(self):  # 11-04 3.4：来源本月贡献改按说法数，旧查询只在回退开关打开时用
        src = (ROOT / "ethan/lifebase.py").read_text(encoding="utf-8")
        body = src[src.index("def source_health("):src.index("def stale_sources(")]
        self.assertIn("if retire.fallback(db)", body)
        self.assertIn("FROM statements s JOIN statement_sources", body)

    def test_outside_not_only_approved_sentences(self):  # 11-04 对外改成三档 × 类别开关（ontology-contracts 1.12），不再只给逐条批准的
        from ethan import memusers
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        self.assertNotIn('to_users=("twin", "site")', api)
        portrait = (ROOT / "ethan/portrait.py").read_text(encoding="utf-8")
        self.assertNotIn('if who["level"] == "public":\n        return ""', portrait)
        mu = (ROOT / "ethan/memusers.py").read_text(encoding="utf-8")
        self.assertNotIn('know not in (None, "disclosure", "fact", "preference", "goal")', mu)
        self.assertTrue(all("tier" in u for u in memusers.USERS.values()))

    def test_diary_reads_only_writing_style(self):  # 11-05 写作规范 v1（style-and-skills.md 第 3 节）：日记不再登记第 2 题，只读写作规范
        from ethan import memusers
        self.assertEqual(memusers.USERS["diary"]["questions"], [])
        self.assertFalse(memusers.may_use("diary", "search"))
        self.assertFalse(memusers.may_use("diary", "portrait"))
        self.assertTrue(memusers.may_use("diary", "writing_style"))
        diary = (ROOT / "ethan/diary.py").read_text(encoding="utf-8")
        self.assertNotIn("memsearch.search(", diary)
        self.assertNotIn("portrait.chat_text(", diary)
    def test_card_line_from_diary_not_loose_material(self):  # 11-05 今日卡照昨天的日记出（diary.md“卡和日记怎么对上”）：日记不再读卡名，那句话不再“昨天或今天素材里有的”随便接
        from ethan import cards
        diary = (ROOT / "ethan/diary.py").read_text(encoding="utf-8")
        self.assertNotIn("这天的卡：", diary)
        self.assertNotIn("要接上她昨天或今天具体的事（素材里有的才写）", cards.LINE_RULE)
        self.assertIn("昨天日记", cards.LINE_RULE)

    def test_card_renamed_yesterday_card_and_waits(self):  # （diary.md“昨日卡（11-06）”）
        import re
        from ethan import cards
        for f in ("ethan/workbench/static/app.js", "ethan/workbench/static/index.html"):  # 界面上不再出现“今日卡”
            self.assertNotIn("今日卡", (ROOT / f).read_text(encoding="utf-8"), f)
        for f in ("ethan/workbench/api.py", "ethan/channels/telegram.py", "ethan/cards.py"):  # 给她看的字符串里也没有（早报那张时间线卡片 11-06 也改叫“昨日卡”）
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertEqual(re.findall(r'"[^"\n]*今日卡(?!片)[^"\n]*"', src), [], f)
        src = (ROOT / "ethan/cards.py").read_text(encoding="utf-8")
        self.assertNotIn("forecast_days=1", cards.WEATHER_URL)  # 不再写当天的预报
        self.assertNotIn("director_failed", src)  # 导演失败不再退回随机拼图出卡
        self.assertNotIn("def redirect(", src)

    def test_card_material_only_yesterday(self):  # 11-06 晚（diary.md“昨日卡：素材只按昨天取”）
        src = (ROOT / "ethan/cards.py").read_text(encoding="utf-8")
        for old in ("她今天的安排", "今天是训练日", "def _today_source(", "latest_plan(day.isoformat())", "今天练{focus}", "训练日出门"):
            self.assertNotIn(old, src, old)
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        card = api[api.index("    def card(self, day: date):"):api.index("    def training_today(")]
        self.assertNotIn("latest_plan(day.isoformat())", card)  # 卡上那句不再按今天的计划改
        for f in ("ethan/planner.py", "ethan/catchup.py"):  # 出卡不再把今天的计划交进去
            text = (ROOT / f).read_text(encoding="utf-8")
            self.assertIsNone(__import__("re").search(r"make(_card)?\([^)]*latest_plan\(today", text), f)

    def test_card_refs_her_photos_first(self):  # 11-06 晚（avatar.md“她的形象参考照”）
        src = (ROOT / "ethan/cards.py").read_text(encoding="utf-8")
        self.assertNotIn('refs = [av / "avatar-day.jpg", av / "avatar-night.jpg"] + sorted((av / "refs").glob("*"))[:3]', src)
        app = (ROOT / "ethan/workbench/static/app.js").read_text(encoding="utf-8")
        self.assertNotIn('const imgUrl = (u) => S.token ? u + "?s="', app)  # 卡图地址自带 ?v=，不能再拼第二个问号

    def test_selfie_ref_is_automatic(self):  # 11-06 晚（avatar.md“她的形象参考照”）
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        app = (ROOT / "ethan/workbench/static/app.js").read_text(encoding="utf-8")
        for text in (api, app):
            self.assertNotIn("只用她点过的", text)
            self.assertNotIn("不擅自用所有照片", text)
        self.assertNotIn('.glob(f"her-{got[1][:16]}.*")', api)  # 是不是参考统一走 cards.ref_state（手动和自动都算）
        self.assertIn("本人照", (ROOT / "ethan/files.py").read_text(encoding="utf-8"))  # 看图那一步顺带判断

    def test_scattered_task_rules_gone(self):
        """11-06 任务规矩统一（ethan/taskrules.py，tasks-and-schedule.md 第 8 节替代清单）：各处自己写的判断不再出现。"""
        src = lambda f: (ROOT / "ethan" / f).read_text(encoding="utf-8")
        api = src("workbench/api.py")
        for old in ("WAITING = re.compile", "SOON_DAYS = ", "def _starred(", "PAUSED_WHY", "quests.side_quests(", "quests.later_quests("):
            self.assertNotIn(old, api, old)
        self.assertNotIn("def not_dropped(", src("quests.py"))
        self.assertNotIn("def later_quests(", src("quests.py"))
        self.assertNotIn("def open_tasks(", src("territory.py"))
        self.assertNotIn("def _already_tasked(", src("mail.py"))
        self.assertNotIn("def next_steps(", src("brief_card.py"))
        self.assertNotIn("以后的副本，不急", src("planner.py"))  # 暂停副本的不再排最后照列
        self.assertNotIn("db.open_tasks()", src("todo.py"))  # Telegram 清单不再列全部
        self.assertNotIn("self.db.open_tasks()", src("memory/store.py"))  # 小伴 看的单子和首页同一份
        self.assertFalse((ROOT / "ethan/taskops.py").exists())  # 并进 taskrules，不留两套
        self.assertNotIn("${false ?", APP)  # “以后再做”不再藏起来

    def test_one_completion_and_delete_path(self):
        """勾完成只有 taskrules.complete 一个入口；删除走 taskrules.remove / delete（底层 db.delete_task 只留给系统自己的整理）。"""
        allowed_complete = {"taskrules.py", "quests.py", "stages.py"}
        allowed_delete = {"taskrules.py", "stages.py", "prep.py", "season1.py", "season1_fix.py", "memory/db.py"}
        for p in (ROOT / "ethan").rglob("*.py"):
            name, text = str(p.relative_to(ROOT / "ethan")), p.read_text(encoding="utf-8")
            if name not in allowed_complete:
                self.assertIsNone(re.search(r"quests\.complete_task\(|stages\.check\(", text), name)
            if name not in allowed_delete:
                self.assertNotIn("db.delete_task(", text, name)

    def test_scattered_desk_rules_gone(self):
        """11-06 邮件和留言“等你处理”统一成 desk.state_of（docs/design/mail-desk.md 替代清单，决定 6）。"""
        src = lambda f: (ROOT / "ethan" / f).read_text(encoding="utf-8")
        # app.js 的 ["open", "draft"].includes 在 tests/test_desk_rules.py、desk.py 的 date('now') 在 tests/test_time_guard.py 查
        self.assertNotIn('x["state"] in ("open", "draft")', src("desk.py") + src("invariants.py"))
        self.assertNotIn("IN ('open', 'draft', 'drafting', 'todo')", src("mail.py"))
        self.assertNotIn("NOT IN ('sent', 'ignored')", src("publicsite.py"))
        self.assertNotIn("\nSTATES = (", src("desk.py"))

    def test_scattered_memory_validity_gone(self):
        """11-06 记忆“这条现在还算不算数”统一成 lifebase.fact_state / mem_where / MEM_VIEWS（memory-alignment.md 替代清单，决定 10）。"""
        src = lambda f: (ROOT / "ethan" / f).read_text(encoding="utf-8")
        self.assertNotIn("def _current(", src("onto_query.py"))  # 题函数各自的“现在成立”
        self.assertNotIn("NOT_GONE_STATEMENT = ", src("lifebase.py"))  # 并进 _state_case
        self.assertNotIn('f["status"] not in lifebase.GONE', src("portrait.py"))
        self.assertNotIn('f["status"] not in ("invalid", "candidate", "suspended", "deleted")', src("mepage.py"))
        self.assertNotIn('r["status"] not in ("invalid", "suspended")', src("memsearch.py"))
        self.assertNotIn("status = 'active' AND valid_to IS NULL", src("mepage.py") + src("portrait.py"))
        self.assertNotIn("UPDATE facts SET status = 'invalid'", src("memusers.py") + src("mepage.py"))  # 作废只走 lifebase.invalidate
    def test_old_contact_list_gone(self):
        """的决定 5：“该联系谁”只用人物卡那套（relations.due / contact_status），老的“手设间隔 + 生日”下线
        （relations.md 第 5 节替代清单）。"""
        for p in (ROOT / "ethan").rglob("*.py"):
            self.assertNotIn("people_to_contact(", p.read_text(encoding="utf-8"), str(p))
        self.assertNotIn('"people_to_contact"', (ROOT / "ethan/core/agent.py").read_text(encoding="utf-8"))
        self.assertNotIn("contact_every_days IS NOT NULL", (ROOT / "ethan/territory.py").read_text(encoding="utf-8"))
        self.assertNotIn("contact_every_days IS NOT NULL", (ROOT / "ethan/attributes.py").read_text(encoding="utf-8"))

    def test_scattered_nudge_rules_gone(self):
        """11-06 主动消息统一（nudges.md 替代清单）：各段各写的免打扰 / 刚聊过 / 例外、手写的 ONCE_PREFIXES、各函数里的时段常量不再出现。"""
        src = lambda f: (ROOT / "ethan" / f).read_text(encoding="utf-8")
        pro = src("proactive.py")
        self.assertNotIn("RECENT_CHAT = ", pro)
        self.assertNotIn("optional=True", pro)
        self.assertNotIn('ONCE_PREFIXES = ("', pro)
        self.assertNotIn("BRIEF_LAST", pro)
        self.assertNotIn("sit_at", pro + src("nudges.py"))
        self.assertNotIn("PROMISE_GAP", src("prep.py"))
        self.assertNotIn("DIGEST_AT", src("mail.py") + src("nudges.py"))
        self.assertNotIn("relay.mark(", src("channels/telegram.py"))  # Grok 转告挪进 proactive.relay_grok，过关口

    def test_modular_batch3_old_ways_gone(self):
        """11-06 全系统模块化第三批：后台、领域标准、时间、日记的旧写法不再出现。
        替代清单见 docs/design/system-rules.md 8.6 节、territories.md“领域标准表”、diary.md“哪些是她写的”。"""
        files = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in (ROOT / "ethan").rglob("*.py")}
        for path, text in files.items():
            if path != "ethan/bgwork.py":  # 12 种各自 Popen 丢进 /dev/null → bgwork.kick（日志、锁、停跑期不起）
                self.assertNotIn("stderr=subprocess.DEVNULL", text, path)
            if path != "ethan/diary.py":  # “哪些是她写的”只在 diary.HER / HER_SQL / her_rows
                self.assertIsNone(re.search(r"['\"]her_edited['\"]", text), path)
                self.assertNotIn("source IN ('her'", text, path)
        for path in ("ethan/diary.py", "ethan/season.py", "ethan/catchup.py"):  # 复制的“mtime 900 秒”锁文件 → bgwork.kick 的锁
            self.assertNotIn("st_mtime <", files[path], path)
        self.assertNotIn("17 * _k", files["ethan/attributes.py"])  # 领域标准两套数字 → ethan/standards.py
        self.assertNotIn('("训练", "训练", 5, 4)', files["ethan/territory.py"])
        self.assertNotIn("04:00 以后", files["ethan/onto_query.py"])  # 决定 9：作息按 5 点分天
        self.assertNotIn("class QuotaBlocked(Exception)", files["ethan/bgwork.py"])  # 挪进 router，Router 自己也抛

    def test_cleanup_1106_scattered_ways_gone(self):
        """上一轮没清完的散落写法不再出现（替代清单见 docs/design/system-rules.md 第 5 节、ui-spec.md“颜色变量”、nudges.md 第 3 节）。"""
        # “本周一”25 份 → clock.week_start、写死的伦敦 → clock.her_tz：全仓扫描在 tests/test_time_guard.py（同样的正则和白名单）
        files = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8") for p in (ROOT / "ethan").rglob("*.py")}
        self.assertNotIn('ZoneInfo(ethan.router.settings.get("timezone"', files["ethan/proactive.py"])
        self.assertNotIn("her_last >= at", files["ethan/nudges.py"])  # 首页提示不再因为她回过一句话就收起
        static = ROOT / "ethan/workbench/static"
        app, css = (static / "app.js").read_text(encoding="utf-8"), (static / "app.css").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"#[0-9A-Fa-f]{6}\b", app))  # app.js 里的十六进制颜色 → app.css 的变量和类
        self.assertNotIn(".ccard.r3, .bigcard .img.r3, .cardthumb.r3", css)  # 卡片稀有度三份 → .rar
        self.assertNotIn("body.day .shop.t1 {", css)  # 白天覆盖规则 → 主题变量
        self.assertNotIn("body.day .cardback span { color: #8D7A55 !important; }", css)
        self.assertNotIn('send("diary.save", { day, text }).then(', app)  # 自己 send + 提示 → save()
        self.assertNotIn('send("plan.replan", {}).then(', app)
        self.assertNotIn('send("diary.note"', app)

    def test_effort_size_old_ways_gone(self):  # 11-06 晚她定方案 A + C 的补漏（原话“要”）：按预计工作量定档、效率补漏算的分、首页英语同口径
        api = (ROOT / "ethan/workbench/api.py").read_text(encoding="utf-8")
        self.assertNotIn("size = 'S'", api)  # 工作台加的副本步骤不再写死 S
        self.assertNotIn("（看大小）", APP)
        self.assertNotIn("· 其他 +20 ·", APP)
        wa = (ROOT / "ethan/workaholic.py").read_text(encoding="utf-8")
        self.assertNotIn("p.ref = 'task:' || t.id", wa)  # 效率不再只算 task: 的分
        self.assertNotIn("WORK_DOMAINS", wa)
        terr = (ROOT / "ethan/territory.py").read_text(encoding="utf-8")
        self.assertNotIn("上周同期 {l['efficiency']", terr)  # 效率看本周和最近 4 周平均
        self.assertIn("workaholic.efficiency", terr)
        self.assertIn("workaholic.efficiency", (ROOT / "ethan/report.py").read_text(encoding="utf-8"))
        q = (ROOT / "ethan/quests.py").read_text(encoding="utf-8")
        self.assertNotIn('"count": week_count(db, h["id"], day)', q)  # 首页周常数走 weekly_count（英语用 practice_days）



class PageResetTest(unittest.TestCase):
    def test_every_page_starts_fresh(self):
        """进任何一页都要初始化再展示（“我”页的星图还停在上次离开时的样子）。切页面、点进领域、从领域返回都先 resetView。"""
        for act in ('a === "go"', 'a === "terr"', 'a === "terrback"'):
            body = APP[APP.index(act):APP.index("else if", APP.index(act) + 10)]
            self.assertIn("resetView()", body, act)
        keep = APP[APP.index("const KEEP_ACROSS_PAGES"):APP.index("function resetView()")]
        for k in ("meStar", "meStarDim", "mapTab", "wishOpen", "relCircle", "cardF", "setTab", "me", "terr", "season"):
            self.assertNotIn(f'"{k}"', keep, k)


if __name__ == "__main__":
    unittest.main()
