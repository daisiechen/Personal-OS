"""替代清单：改版时换掉的旧东西不再出现（docs/design/system-rules.md 第 2 节，她 2026-11-02 认可）。

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

    def test_no_brain_wording_left(self):  # 11-04 大脑 10-31 就并进了“收藏和资料”，领域页、地图上还写着“大脑”（她问“到底哪个是全部？”）
        self.assertNotIn("在大脑里看全部", APP)
        self.assertNotIn(">第二大脑<", APP)

    def test_me_page_old_blocks_gone(self):  # 11-04 “我”页旧版块下线（她：“旧版的还没下”）：大号对齐率和周柱子撤掉，使用者和纠错单挪进第二层
        page = APP[APP.index("function mePage()"):APP.index("function meUsersSheet()")]
        self.assertNotIn("mehero", APP)
        self.assertNotIn("meusers", page)
        self.assertNotIn("meticket", page)
        self.assertIn('data-k="me-users"', page)

    def test_weekly_social_from_relations(self):  # 11-04 周报的“社交”改从人物卡三张清单取（ethan/relations.py），不再用旧的 people_to_contact
        src = (ROOT / "ethan/report.py").read_text(encoding="utf-8")
        self.assertNotIn('"社交": db.people_to_contact', src)
        self.assertIn("relations.week_line", src)

    def test_handoff_not_only_her_words(self):  # 11-04 微信 / Grok / Codex 交整段对话（她：“难道别人跟我的对话不是我的记忆么！！！”），不再只交她的话、别人的话只当“问”
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

    def test_wasteland_entry_on_season_map(self):  # 11-02 赛季页改版丢了荒原入口，红点没去处
        self.assertIn("stag waste", APP)
        self.assertIn('data-k="waste"', APP)

    def test_daily_cold_start_rotation_off(self):  # 11-02 冷启动挪进 M2 阶段任务，旧的每日轮换关掉
        src = (ROOT / "ethan/domains.py").read_text(encoding="utf-8")
        self.assertIn("milestone_id IS NOT NULL AND title LIKE '%冷启动%'", src)  # 行为测试见 test_quest_structure


    def test_finance_history_every_other_day_gone(self):  # 11-04 知识点改成“用得上”，金融历史不再隔天固定讲（她：“讲的也太古早了”）
        from ethan import lessons
        self.assertFalse(hasattr(lessons, "FIRST"))
        self.assertNotIn("金融历史", [t for t, _, _ in lessons.TOPICS])
        agent = (ROOT / "ethan/core/agent.py").read_text(encoding="utf-8")
        self.assertNotIn("金融历史排第一", agent)

    def test_english_no_fixed_days(self):  # 11-04 英语不排固定时间（她：“我有空了就去找grok聊就好了”），冷启动不再问哪几天
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

if __name__ == "__main__":
    unittest.main()
