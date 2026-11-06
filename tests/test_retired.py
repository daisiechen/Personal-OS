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
        page = APP[APP.index("function mePage()"):APP.index("function meUsersSheet()")]
        self.assertNotIn("mehero", APP)
        self.assertNotIn("meusers", page)
        self.assertNotIn("meticket", page)
        self.assertIn('data-k="me-users"', page)

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

    def test_wasteland_entry_on_season_map(self):  # 11-02 赛季页改版丢了荒原入口，红点没去处；11-05 第二版荒原是分数柱最右边那根虚线柱
        board = APP[APP.index("function seasonBoard("):APP.index("function seasonRoute(")]
        self.assertIn('data-k="waste"', board)

    def test_season_page_v2_replaced_map_and_cards(self):  # 11-05 赛季页第二版（样稿 A + C 的分数柱）：地图关系线、两张大卡、21 天小日历、领域行撤掉
        page = APP[APP.index("function mapPage()"):APP.index("function seasonPass(")]
        for old in ("smap", "fcard", "srccard", "srow", "mcal", "ledger", "nextbox"):
            self.assertNotIn(old, page, old)
            self.assertNotIn(f".{old}", (ROOT / "ethan/workbench/static/app.css").read_text(encoding="utf-8"), old)
        for new in ("seasonPass(z)", "seasonVerdict(z)", "seasonBoard(z)", "seasonRoute(z.route)"):
            self.assertIn(new, page)
        from ethan import season
        self.assertFalse(hasattr(season, "calendar"))

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
        self.assertNotIn('["open", "draft"].includes(x.state)', APP)
        self.assertNotIn("date('now')", src("desk.py"))
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
        self.assertNotIn("date('now')", files["ethan/desk.py"])  # 邮件台“今天发的”按她的时区
        self.assertNotIn("17 * _k", files["ethan/attributes.py"])  # 领域标准两套数字 → ethan/standards.py
        self.assertNotIn('("训练", "训练", 5, 4)', files["ethan/territory.py"])
        self.assertNotIn("04:00 以后", files["ethan/onto_query.py"])  # 决定 9：作息按 5 点分天
        self.assertNotIn("class QuotaBlocked(Exception)", files["ethan/bgwork.py"])  # 挪进 router，Router 自己也抛


if __name__ == "__main__":
    unittest.main()
