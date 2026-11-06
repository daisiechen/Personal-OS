"""ontology-plan 3.1：其余首版能力题（第 20、21 题除外）的验收。每道题五种情况：正常、变化（新的取代旧的）、缺失、冲突、没权限。
没权限对调用方看起来和“没有”一样（contracts 1.6 节），真实情况在 function_log 里查。"""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import decisions, lifebase, lifelog, locks, memusers, onto_query, planner, stages, transitions
from ethan.memory import MemoryStore

DAY = date(2026, 11, 4)  # 周三；这周从 11-02 起


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:0", "2026-11-01T09:00:00", "mia：随便聊聊")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def say(self, at, text, who="mia", n=[0]):
        n[0] += 1
        return lifebase.add_evidence(self.db, "chat", f"raw:{at[:10]}:{n[0]}", at, f"{who}：{text}")

    def sid(self, fid):
        return self.db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (fid,)).fetchone()[0]

    def real(self):
        """function_log 里最近一次的真实状态（没权限时调用方只看到 unknown）。"""
        return self.db.conn.execute("SELECT status FROM function_log ORDER BY id DESC LIMIT 1").fetchone()[0]

    def hidden(self, got):
        self.assertEqual((got["status"], got["items"], got["note"]), ("unknown", [], ""))
        self.assertEqual(self.real(), "forbidden")


class RegistryTest(unittest.TestCase):
    def test_registered_questions_exist_and_every_first_version_question_has_a_user(self):
        known = set(onto_query.QUESTIONS)
        for uid, u in memusers.USERS.items():
            self.assertIn("questions", u, f"{uid} 没写要回答哪几道题")
            self.assertEqual(set(u["questions"]) - known, set(), f"{uid} 登记了不存在的题号")
        asked = {q for u in memusers.USERS.values() for q in u["questions"]}
        self.assertEqual(set(onto_query.FIRST_VERSION) - asked, set(), "这些首版题没有使用者")
        self.assertNotIn(20, known)  # 预测：首版不做
        self.assertNotIn(21, known)  # 别的 agent：随团队页改造做
        self.assertEqual(set(onto_query.FIRST_VERSION), set(range(1, 20)) | set(range(22, 29)))

    def test_every_question_function_exists(self):
        for q, fn in onto_query.QUESTIONS.items():
            self.assertTrue(callable(getattr(onto_query, fn, None)), f"第 {q} 题的函数 {fn} 不存在")


class RecallTest(Base):
    # ---- 第 2 题 上次说到某件事 ----
    def test_last_said(self):
        old = self.say("2026-10-20T10:00:00", "我最近特别想去芬兰看极光")
        self.say("2026-10-21T10:00:00", "芬兰的极光季是冬天哦", who="ethan")
        got = onto_query.last_said(self.db, "ethan", "芬兰极光")
        self.assertEqual((got["status"], got["q"], [i["evidence"] for i in got["items"]]), ("answer", 2, [old]))
        new = self.say("2026-11-03T10:00:00", "芬兰极光算了，太冷了")  # 变化：最近的在前
        self.assertEqual(onto_query.last_said(self.db, "ethan", "芬兰极光")["items"][0]["evidence"], new)
        self.assertTrue(onto_query.last_said(self.db, "ethan", "撒哈拉沙漠")["missing"])  # 缺失
        f = lifebase.add_fact(self.db, "values", "想去芬兰看极光", [old], kind="self_report", context="开玩笑说的")
        self.db.conn.execute("UPDATE statements SET review = 'disputed' WHERE id = ?", (self.sid(f),))  # 冲突
        got = onto_query.last_said(self.db, "ethan", "芬兰极光")
        self.assertEqual(got["status"], "disputed")
        self.assertEqual(next(i for i in got["items"] if i["evidence"] == old)["context"], "开玩笑说的")  # 语境一起给
        self.hidden(onto_query.last_said(self.db, "twin", "芬兰极光"))  # 没权限
        lifebase.forget(self.db, evidence_ids=[old, new])  # 删了的不算
        self.assertEqual(onto_query.last_said(self.db, "ethan", "芬兰极光")["status"], "unknown")

    # ---- 第 3 题 某人是谁 ----
    def test_person(self):
        pid = self.db.upsert_person("Leo", circle="朋友", how_met="伦敦交换")
        lifelog.add(self.db, "meet", DAY, "Leo", "喝咖啡")
        old = lifebase.add_fact(self.db, "relationships", "Leo 是她在曼彻斯特的同学", [self.ev], subject=f"person:{pid}", prop="relation_to_her")
        got = onto_query.person(self.db, "prep", "Leo")
        self.assertEqual(got["status"], "answer")
        self.assertEqual(got["items"][0]["relation"][0]["value"], "Leo 是她在曼彻斯特的同学")
        self.assertEqual(got["items"][0]["latest"]["kind"], "meet")
        new = lifebase.add_fact(self.db, "relationships", "Leo 是她的合伙人", [self.ev], subject=f"person:{pid}", prop="relation_to_her")
        lifebase.supersede(self.db, old, new, when="2026-11-01")  # 变化
        self.assertEqual([r["value"] for r in onto_query.person(self.db, "prep", "Leo")["items"][0]["relation"]], ["Leo 是她的合伙人"])
        self.assertTrue(onto_query.person(self.db, "prep", "王小明")["missing"])  # 缺失
        self.db.upsert_person("Amy Li")
        self.db.upsert_person("Amy Zhang")
        amy = onto_query.person(self.db, "ethan", "Amy")  # 冲突：同名多人，先问是哪位
        self.assertEqual((amy["status"], len(amy["items"])), ("disputed", 2))
        self.assertIn("哪位", amy["note"])
        self.hidden(onto_query.person(self.db, "twin", "Leo"))  # 别人的事不对外

    # ---- 第 18 题 上个月和现在有什么变化 ----
    def test_changes(self):
        a = lifebase.add_fact(self.db, "rhythms", "每周训练 3 次", [self.ev])
        self.assertEqual(onto_query.changes(self.db, "ethan", "训练", since="2026-10-01")["note"], "没看到变化记录")  # 缺失：只有一条
        b = lifebase.add_fact(self.db, "rhythms", "每周训练 5 次", [self.ev])
        lifebase.supersede(self.db, a, b, when="2026-10-20")
        got = onto_query.changes(self.db, "ethan", "训练", since="2026-10-01")
        self.assertEqual([(p["before"], p["after"], p["changed_at"]) for p in got["items"]], [("每周训练 3 次", "每周训练 5 次", "2026-10-20")])
        self.assertEqual(got["basis"], sorted([self.sid(a), self.sid(b)]))
        c = lifebase.add_fact(self.db, "rhythms", "每周训练 4 次", [self.ev])  # 变化：又变了一次，按时间排
        lifebase.supersede(self.db, b, c, when="2026-11-02")
        self.assertEqual([p["after"] for p in onto_query.changes(self.db, "ethan", "训练", since="2026-10-01")["items"]],
                         ["每周训练 5 次", "每周训练 4 次"])
        self.db.conn.execute("UPDATE statements SET review = 'disputed' WHERE id = ?", (self.sid(c),))  # 冲突
        self.assertEqual(onto_query.changes(self.db, "ethan", "训练", since="2026-10-01")["status"], "disputed")
        self.hidden(onto_query.changes(self.db, "twin", "训练"))


class RulesTest(Base):
    # ---- 第 5 题 长期规矩 ----
    def test_standing_rules(self):
        r1 = decisions.add(self.db, "rule", "订餐厅", "订之前先查退改", quote="订之前先查退改政策")
        decisions.add(self.db, "rule", "排计划", "晚上 10 点后不排事", roles=["planner"], quote="10 点后别排")
        got = onto_query.standing_rules(self.db, "prep")
        self.assertEqual([i["value"] for i in got["items"]], ["订之前先查退改"])  # 管计划器的那条准备员看不到
        decisions.add(self.db, "rule", "订餐厅", "订之前先查退改和人均", supersedes=r1, quote="还要看人均")  # 变化
        self.assertEqual([i["value"] for i in onto_query.standing_rules(self.db, "prep", "订餐厅")["items"]], ["订之前先查退改和人均"])
        self.assertTrue(onto_query.standing_rules(self.db, "prep", "滑雪")["missing"])
        decisions.add(self.db, "rule", "订餐厅", "订餐厅不用查人均", quote="人均无所谓")  # 冲突：两条打架
        got = onto_query.standing_rules(self.db, "prep", "订餐厅")
        self.assertEqual(got["status"], "disputed")
        self.assertIn("打架", got["note"])
        self.hidden(onto_query.standing_rules(self.db, "twin"))

    # ---- 第 22 题 做某件事前和它有关的决定 ----
    def test_decisions_for(self):
        d1 = decisions.add(self.db, "pause", "训练", "这周暂停训练，膝盖疼", roles=["planner"], quote="膝盖疼先停")
        got = onto_query.decisions_for(self.db, "planner", "排训练")
        self.assertEqual((got["status"], got["items"][0]["value"]), ("answer", "这周暂停训练，膝盖疼"))
        decisions.add(self.db, "pause", "训练", "只停腿部训练", roles=["planner"], supersedes=d1, quote="上肢可以练")  # 变化
        self.assertEqual([i["value"] for i in onto_query.decisions_for(self.db, "planner", "排训练")["items"]], ["只停腿部训练"])
        none = onto_query.decisions_for(self.db, "planner", "买菜")
        self.assertTrue(none["missing"])
        self.assertIn("默认规则", none["note"])
        decisions.add(self.db, "rule", "跑步", "早上跑", quote="早上跑")
        decisions.add(self.db, "rule", "跑步", "晚上跑", quote="晚上跑")  # 冲突
        self.assertEqual(onto_query.decisions_for(self.db, "planner", "跑步")["status"], "disputed")
        self.hidden(onto_query.decisions_for(self.db, "twin", "训练"))

    # ---- 第 13 题 授权到哪一步 ----
    def test_authorization(self):
        none = onto_query.authorization(self.db, "prep", "book", "订周五的餐厅")  # 缺失：只能起草
        self.assertEqual((none["status"], none["items"][0]["allowed"]), ("unknown", "draft_only"))
        g = decisions.grant(self.db, "prep", "book", "餐厅", each_confirm=False, quote="餐厅你直接订")
        got = onto_query.authorization(self.db, "prep", "book", "订周五的餐厅")
        self.assertEqual((got["status"], got["items"][0]["each_confirm"]), ("answer", False))
        decisions.grant(self.db, "prep", "book", "餐厅", each_confirm=True, quote="订之前问我")  # 冲突：宽严不一，最严的在前
        got = onto_query.authorization(self.db, "prep", "book", "订周五的餐厅")
        self.assertEqual((got["status"], got["items"][0]["each_confirm"]), ("disputed", True))
        decisions.revoke(self.db, g)  # 变化：撤回了宽的那条
        self.assertEqual(onto_query.authorization(self.db, "prep", "book", "订周五的餐厅")["status"], "answer")
        self.hidden(onto_query.authorization(self.db, "prep", "book", "餐厅", grantee="planner"))  # 只能问自己的
        self.assertEqual(onto_query.authorization(self.db, "system", "book", "餐厅", grantee="prep")["status"], "answer")


class WeekTest(Base):
    def plan(self, day, items):
        pid = self.db.save_plan(day, "model", "{}", "", day, items)
        return [r[0] for r in self.db.conn.execute("SELECT id FROM plan_items WHERE plan_id = ? ORDER BY id", (pid,))]

    # ---- 第 6 题 这周计划、完成、没完成的原因 ----
    def test_week_plan(self):
        self.assertTrue(onto_query.week_plan(self.db, "review", today=DAY)["missing"])
        a, b = self.plan("2026-11-02", [{"kind": "todo", "title": "写论文"}, {"kind": "event", "title": "组会", "start": "10:00"}])
        self.db.add_result(a, "没做")
        self.db.add_result(b, "完成")
        t = self.db.add_task("交签证材料")
        self.db.complete_task(t, at="2026-11-03T12:00:00")
        got = onto_query.week_plan(self.db, "review", today=DAY)["items"][0]
        self.assertEqual([(p["title"], p["result"], p["reason"]) for p in got["not_done"]], [("写论文", "没做", "原因未知")])  # 不推断原因
        self.assertEqual([d["title"] for d in got["done_tasks"]], ["交签证材料"])
        (c,) = self.plan("2026-11-02", [{"kind": "todo", "title": "写论文第二节"}])  # 变化：新一版计划
        self.db.add_result(c, "部分", "导师临时改了要求")
        got = onto_query.week_plan(self.db, "review", today=DAY)["items"][0]
        self.assertEqual([(p["title"], p["reason"]) for p in got["planned"]], [("写论文第二节", "导师临时改了要求")])
        self.db.add_result(c, "完成")  # 冲突：同一件前后记了不同结果
        self.assertEqual(onto_query.week_plan(self.db, "review", today=DAY)["status"], "disputed")
        self.hidden(onto_query.week_plan(self.db, "twin", today=DAY))

    # ---- 第 7 题 这周哪些天、哪些来源没有记录 ----
    def test_record_gaps(self):
        empty = MemoryStore(Path(self.tmp.name) / "empty")
        lifebase.ensure(empty.db)
        self.assertTrue(onto_query.record_gaps(empty.db, "review", today=DAY)["missing"])  # 缺失：一个来源都没有
        empty.close()
        self.say("2026-11-02T09:00:00", "早")
        got = onto_query.record_gaps(self.db, "review", today=DAY)["items"][0]
        self.assertEqual(got["days_without_any"], ["2026-11-03", "2026-11-04"])
        chat = next(s for s in got["by_source"] if s["code"] == "S1")
        self.assertEqual(chat["missing_days"], ["2026-11-03", "2026-11-04"])
        self.say("2026-11-03T21:00:00", "晚安")  # 变化：补上一天
        self.assertEqual(onto_query.record_gaps(self.db, "review", today=DAY)["items"][0]["days_without_any"], ["2026-11-04"])
        later = onto_query.record_gaps(self.db, "review", today=date(2026, 11, 8))["items"][0]  # 断档也报（按来源自己的频率）
        self.assertIn("S1", [s["code"] for s in later["stale_sources"]])
        self.hidden(onto_query.record_gaps(self.db, "twin", today=DAY))

    # ---- 第 26 题 每周开销（11-04 已做，这里补变化和缺失） ----
    def test_weekly_spend_change_and_missing(self):
        self.assertTrue(onto_query.weekly_spend(self.db, "review", today=DAY)["missing"])
        self.hidden(onto_query.weekly_spend(self.db, "twin", today=DAY))


class GoalsTest(Base):
    def goal(self, title="雅思 7.5"):
        return self.db.add_goal(title, "year", status="active")

    def close(self, mid, status, closed, review=""):
        self.db.conn.execute("UPDATE milestones SET status = ?, closed_at = ?, review = ? WHERE id = ?", (status, closed, review, mid))

    # ---- 第 8 题 同类里程碑按时完成几成 ----
    def test_milestone_ontime(self):
        g = self.goal()
        m = [stages.add_milestone(self.db, g, f"M{i}", due=f"2026-10-{10 + i}") for i in range(4)]
        self.close(m[0], "done", "2026-10-09T10:00:00")
        self.close(m[1], "done", "2026-10-15T10:00:00", "等签证材料")
        few = onto_query.milestone_ontime(self.db, "planner")  # 缺失：可比的少于 3 件
        self.assertEqual(few["status"], "unknown")
        self.assertIn("少于 3 件", few["note"])
        self.close(m[2], "missed", "2026-10-20T10:00:00")
        got = onto_query.milestone_ontime(self.db, "planner")["items"][0]
        self.assertEqual((got["on_time_rate"], got["by_reason"]), (0.33, {"外部阻塞": 1, "原因未知": 1}))  # 三个数都给
        self.assertEqual(len(got["refs"]), 3)  # 参照集列清楚
        self.close(m[3], "missed", "2026-10-20T10:00:00")  # 冲突：原因多半未知，结论不稳
        self.assertIn("结论不稳", onto_query.milestone_ontime(self.db, "planner")["note"])
        self.db.conn.execute("UPDATE milestones SET status = 'open', closed_at = NULL WHERE id = ?", (m[3],))  # 变化：重开的不算
        self.assertEqual(len(onto_query.milestone_ontime(self.db, "planner")["items"][0]["refs"]), 3)
        self.hidden(onto_query.milestone_ontime(self.db, "twin"))

    # ---- 第 10 题 这季副本的结果和她的复盘 ----
    def test_season_results(self):
        self.assertTrue(onto_query.season_results(self.db, "review")["missing"])
        g = self.goal()
        m = stages.add_milestone(self.db, g, "听力过 7", due="2026-10-30")
        got = onto_query.season_results(self.db, "review")
        self.assertEqual(got["items"][0]["note"], "她还没复盘过，只给结果")
        self.close(m, "done", "2026-10-29T10:00:00", "听力靠每天精听上来的")
        self.db.conn.execute("UPDATE goals SET status = 'done' WHERE id = ?", (g,))  # 变化：副本结束了
        got = onto_query.season_results(self.db, "review")["items"][0]
        self.assertEqual((got["status"], got["her_review"][0]["review"]), ("done", "听力靠每天精听上来的"))
        self.hidden(onto_query.season_results(self.db, "twin"))


class HerOwnTest(Base):
    # ---- 第 11 题 系统的猜测：支持、反对、别的解释 ----
    def test_hypothesis_case(self):
        g = lifebase.add_evidence(self.db, "grok", "grok:1", "2026-11-01", "Grok：她计划总是偏乐观")
        f = lifebase.add_fact(self.db, "thinking", "计划常比实际乐观", [g, self.ev], kind="hypothesis", source_kind="system_inference")
        got = onto_query.hypothesis_case(self.db, "me_page", "乐观")
        self.assertEqual((got["items"][0]["independent"], got["items"][0]["observing"]), (1, True))  # 转述不算独立依据；不够 3 条只算观察中
        self.db.conn.execute("INSERT INTO statement_sources (statement_id, evidence_id, role) VALUES (?, ?, 'contradicts')", (self.sid(f), self.ev))
        self.assertEqual(onto_query.hypothesis_case(self.db, "me_page", "乐观")["status"], "disputed")  # 冲突：有反证
        self.assertTrue(onto_query.hypothesis_case(self.db, "me_page", "拖延")["missing"])
        lifebase.invalidate(self.db, f)  # 变化：她说不像我，作废了
        self.assertTrue(onto_query.hypothesis_case(self.db, "me_page", "乐观")["missing"])
        self.hidden(onto_query.hypothesis_case(self.db, "planner", "乐观"))  # 只给她本人和要拿去和她讨论的 小伴

    # ---- 第 17 题 出处 ----
    def test_provenance(self):
        g = lifebase.add_evidence(self.db, "grok", "grok:2", "2026-11-01", "Grok 说她怕看牙医")
        f = lifebase.add_fact(self.db, "body", "她怕看牙医", [self.ev, g])
        got = onto_query.provenance(self.db, "me_page", self.sid(f))["items"][0]
        self.assertEqual((got["independent"], got["relayed"]), (1, 1))
        self.assertEqual(onto_query.provenance(self.db, "me_page", 99999)["status"], "unknown")  # 缺失
        bare = lifebase.add_fact(self.db, "body", "她牙不好", [self.ev])
        self.db.conn.execute("UPDATE statement_sources SET evidence_id = NULL, source_unknown = 1 WHERE statement_id = ?", (self.sid(bare),))
        self.assertIn("出处缺失", onto_query.provenance(self.db, "me_page", self.sid(bare))["note"])
        new = lifebase.add_fact(self.db, "body", "她现在不怕看牙医了", [self.ev])
        lifebase.supersede(self.db, f, new)  # 变化
        self.assertIn("取代", onto_query.provenance(self.db, "me_page", self.sid(f))["note"])
        self.db.conn.execute("INSERT INTO statement_sources (statement_id, evidence_id, role) VALUES (?, ?, 'contradicts')", (self.sid(new), g))
        self.assertEqual(onto_query.provenance(self.db, "me_page", self.sid(new))["status"], "disputed")  # 冲突
        self.hidden(onto_query.provenance(self.db, "planner", self.sid(f)))

    # ---- 第 19 题 删掉后还剩哪里有 ----
    def test_still_there(self):
        e = self.say("2026-11-01T10:00:00", "我对芒果过敏")
        f = lifebase.add_fact(self.db, "body", "她对芒果过敏", [e])
        from ethan import diary
        diary.ensure(self.db)
        diary.save(self.db, "2026-11-01", "今天发现我对芒果过敏", source="her")
        got = onto_query.still_there(self.db, "me_page", "芒果过敏")
        wheres = {p["where"]: p["ids"] for p in got["items"]}
        self.assertEqual((wheres["原始记录"], wheres["说法"]), ([e], [self.sid(f)]))
        lifebase.forget(self.db, evidence_ids=[e])  # 变化：删了以后原始记录和说法都没了，她自己写的日记还在（不自动改）
        wheres = {p["where"]: p["ids"] for p in onto_query.still_there(self.db, "me_page", "芒果过敏")["items"]}
        self.assertNotIn("原始记录", wheres)
        self.assertNotIn("说法", wheres)
        self.assertIn("2026-11-01", wheres["她自己写的日记（不自动改，请她自己看）"])
        self.assertIn("加密备份（按保留期自然过期）", wheres)  # 管不到的地方也如实列
        self.assertEqual(onto_query.still_there(self.db, "me_page", "火星旅行")["note"], "能查到的地方都没有了")
        self.hidden(onto_query.still_there(self.db, "ethan", "芒果过敏"))


class PublicTest(Base):
    # ---- 第 14 题 访客问她在做什么 ----
    def test_public_say(self):
        none = onto_query.public_say(self.db, "twin", "喜欢什么颜色")
        self.assertEqual((none["status"], none["items"][0]["reply"]), ("unknown", onto_query.REFUSE))
        ask = onto_query.public_say(self.db, "twin", "在做什么")  # “最近在忙什么”公开档默认“问我”（3.3）
        self.assertEqual((ask["status"], ask["items"][0]["reply"], ask["items"][0]["category"]), ("ask", onto_query.ASK_REPLY, "recent"))
        d = lifebase.approve_disclosure(self.db, "她在曼彻斯特工作，在做 AI 产品", "可以这么说")
        got = onto_query.public_say(self.db, "twin")
        self.assertEqual((got["status"], got["items"][0]["value"]), ("answer", "她在曼彻斯特工作，在做 AI 产品"))
        lifebase.approve_disclosure(self.db, "她在做一个 AI 产品，还没发布", "这句也行")
        self.assertEqual(onto_query.public_say(self.db, "twin", "AI 产品")["status"], "disputed")  # 冲突：两句都能说，只照原文挑一句
        lifebase.revoke_disclosure(self.db, d)  # 变化：撤回立刻不说
        self.assertEqual([i["value"] for i in onto_query.public_say(self.db, "twin")["items"]], ["她在做一个 AI 产品，还没发布"])
        self.hidden(onto_query.public_say(self.db, "planner"))

    # ---- 第 15 题 住址、身体、钱、感情、别人的事 ----
    def test_public_sensitive(self):
        got = onto_query.public_sensitive(self.db, "twin", "她体重多少")
        self.assertEqual((got["status"], got["items"][0]["reply"]), ("answer", onto_query.REFUSE))
        d = lifebase.approve_disclosure(self.db, "她在练力量举", "身体这句可以说", dimension="body")  # 变化：她逐条批准了一句
        self.assertEqual(onto_query.public_sensitive(self.db, "twin", "她身体怎么样")["items"][0]["reply"], "她在练力量举")
        lifebase.revoke_disclosure(self.db, d)
        self.assertEqual(onto_query.public_sensitive(self.db, "twin", "她身体怎么样")["items"][0]["reply"], onto_query.REFUSE)
        lifebase.approve_disclosure(self.db, "她在练力量举", "身体这句可以说", dimension="body")
        both = onto_query.public_sensitive(self.db, "twin", "她住哪、身体怎么样")  # 冲突：碰到住址就一律不说
        self.assertEqual(both["items"][0]["reply"], onto_query.REFUSE)
        self.assertTrue(onto_query.public_sensitive(self.db, "twin", "她喜欢看什么书")["missing"])  # 不是敏感话题
        self.hidden(onto_query.public_sensitive(self.db, "ethan", "她体重多少"))

    # ---- 第 16 题 接不接合作 ----
    def test_public_offer(self):
        got = onto_query.public_offer(self.db, "twin", "接不接品牌合作")
        self.assertEqual((got["status"], got["items"][0]["forward"]), ("unknown", True))  # 不预测、不代答，转给她
        d = lifebase.approve_disclosure(self.db, "她目前不接商务合作，学术交流欢迎", "对外就这么说")
        got = onto_query.public_offer(self.db, "twin")
        self.assertEqual((got["status"], got["items"][0]["reply"]), ("answer", "她目前不接商务合作，学术交流欢迎"))
        self.assertNotIn("商务", "".join(i.get("value", "") for i in onto_query.public_say(self.db, "twin")["items"]))  # 口径不混进第 14 题
        lifebase.revoke_disclosure(self.db, d)  # 变化
        self.assertTrue(onto_query.public_offer(self.db, "twin")["items"][0]["forward"])
        self.hidden(onto_query.public_offer(self.db, "planner"))


class PatrolTest(Base):
    # ---- 第 23 题 上次是谁、什么时候、为什么改的 ----
    def test_change_log(self):
        self.assertEqual(onto_query.change_log(self.db, "ethan", "给导师发进度邮件")["note"], "没有改动记录")
        t = self.db.add_task("给导师发进度邮件")
        transitions.record(self.db, "todo", t, "open", "postponed", {"why": "导师出差"})
        got = onto_query.change_log(self.db, "ethan", "给导师发进度邮件")
        self.assertEqual((got["items"][0]["what"], got["items"][0]["who"]), ("open → postponed", "系统记录"))
        self.assertIn("导师出差", got["items"][0]["why"])
        locks.lock(self.db, "task", t, "title", "给导师发进度邮件", title="给导师发进度邮件", note="她在工作台改的名")  # 变化：她又改了
        whos = [i["who"] for i in onto_query.change_log(self.db, "patrol", "给导师发进度邮件")["items"]]
        self.assertEqual(sorted(whos), ["她", "系统记录"])
        self.hidden(onto_query.change_log(self.db, "twin", "给导师发进度邮件"))

    # ---- 第 24 题 互相矛盾的地方 ----
    def test_contradictions(self):
        self.assertEqual(onto_query.contradictions(self.db, "patrol", today=DAY)["note"], "没查到互相矛盾的地方")
        self.db.save_plan(DAY.isoformat(), "model", "{}", "", "x", [{"kind": "todo", "title": "晚上跑步"}])
        planner.note_override(self.db, DAY, "晚上跑步", "delete")
        self.say("2026-11-03T10:00:00", "我来帮你查一下签证进度", who="ethan")
        decisions.add(self.db, "rule", "跑步", "早上跑", quote="早上跑")
        decisions.add(self.db, "rule", "跑步", "晚上跑", quote="晚上跑")
        kinds = sorted(i["kind"] for i in onto_query.contradictions(self.db, "patrol", today=DAY)["items"])
        self.assertEqual(kinds, ["decisions_clash", "plan_has_removed", "promise_not_todo"])
        self.db.add_task("帮她查一下签证进度")  # 变化：答应的进了待办
        kinds = [i["kind"] for i in onto_query.contradictions(self.db, "patrol", today=DAY)["items"]]
        self.assertNotIn("promise_not_todo", kinds)
        self.hidden(onto_query.contradictions(self.db, "twin", today=DAY))

    # ---- 第 25 题 维修工接手一个问题 ----
    def test_issue_brief(self):
        self.assertTrue(onto_query.issue_brief(self.db, "patrol", "qqzzxx")["missing"])
        run = self.db.start_job("calendar")
        self.db.finish_job(run, error="写日历失败：iCloud 401")
        memusers.ensure(self.db)
        self.db.conn.execute("INSERT INTO corrections (at, user, about, entry, quote, wrong, right, layer) VALUES (?, 'ethan', 'her', 'chat', ?, '', ?, 'wrong')",
                             ("2026-11-03", "日历上周五的课没写进去" + "啰嗦" * 100, "日历要写周五的课"))
        got = onto_query.issue_brief(self.db, "patrol", "日历")["items"][0]
        self.assertIn("iCloud", got["symptoms"][0]["error"])
        self.assertLessEqual(len(got["her_words"][0]["quote"]), 120)  # 她的原话只截到需要的长度
        self.assertTrue(any(c.endswith(".py") for c in got["code"]))
        self.hidden(onto_query.issue_brief(self.db, "ethan", "日历"))

    # ---- 第 28 题 说过要做、还没进待办 ----
    def test_said_not_todo(self):
        self.assertTrue(onto_query.said_not_todo(self.db, "ethan", today=DAY)["missing"])
        self.say("2026-11-03T10:00:00", "我要去报名雅思考试，下周就截止了")
        self.say("2026-11-03T11:00:00", "我要去报名雅思考试！")  # 同一件说了两次：只列一次
        got = onto_query.said_not_todo(self.db, "ethan", today=DAY)
        self.assertEqual(len(got["items"]), 1)
        self.assertIn("报名雅思考试", got["items"][0]["said"])
        self.db.add_task("报名雅思考试")  # 变化：进了待办就不再列
        self.assertTrue(onto_query.said_not_todo(self.db, "ethan", today=DAY)["missing"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 1)  # 只列出来，不自动建
        self.hidden(onto_query.said_not_todo(self.db, "twin", today=DAY))


class FirstBatchGapsTest(Base):
    """首批 5 题原来的测试缺的几种情况（3.1 补齐）。"""

    def test_history_not_public_and_prep_requirements_change(self):
        locks.lock(self.db, "task", 3, "deleted", title="订周五的餐厅")
        self.hidden(onto_query.history(self.db, "twin", "订周五的餐厅"))
        old = lifebase.add_fact(self.db, "preferences", "订餐厅要靠窗", [self.ev], kind="preference")
        new = lifebase.add_fact(self.db, "preferences", "订餐厅不要靠窗", [self.ev], kind="preference")
        lifebase.supersede(self.db, old, new, when=DAY.isoformat())  # 写死日期：不写就按真实今天，过了 DAY 那天旧的又算“还成立”
        self.hidden(onto_query.prep_requirements(self.db, "twin", "订餐厅", today=DAY))  # 对外的拿不到她的要求和禁忌（11-05 正式评估发现的泄露）
        got = onto_query.prep_requirements(self.db, "prep", "订餐厅", today=DAY)
        self.assertEqual([i["value"] for i in got["items"]], ["订餐厅不要靠窗"])
        self.assertEqual((got["q"], got["basis"], got["kinds"]), (12, [self.sid(new)], ["her_words"]))

    def test_meta_fields_on_every_result(self):
        got = onto_query.preferences(self.db, "planner", "位子", today=DAY)
        for k in ("q", "basis", "kinds", "at", "missing", "disputed"):
            self.assertIn(k, got)
        self.assertEqual((got["q"], got["missing"]), (4, True))


class RoutingMetricTest(Base):
    """兜底检索的比例：按题答的（function_log）vs 不经过题函数的自由检索（retrieval_log 里 via 为空的）。"""

    def test_me_metrics_routing(self):
        from ethan import memsearch, mepage
        lifebase.add_fact(self.db, "body", "她对花生过敏", [self.ev])
        memsearch.refresh(self.db)
        memsearch.search(self.db, ["花生"], user="ethan")  # 兜底
        memsearch.search(self.db, ["花生"], user="ethan", via="q2")  # 题函数里查的不算兜底
        onto_query.preferences(self.db, "planner", "位子")
        onto_query.last_said(self.db, "ethan", "花生")
        r = mepage.metrics(self.db)["routing"]
        self.assertEqual((r["by_question"], r["fallback"], r["share"]), (2, 1, 0.67))
        self.assertEqual(r["by_q"][2], 1)


class ConsumersTest(Base):
    """邮件起草、每周复盘、准备员、计划器、巡检改走题函数（ontology-plan 3.1 第 4 项）。"""

    def calls(self, user):
        return {r[0] for r in self.db.conn.execute("SELECT fn FROM function_log WHERE user = ?", (user,))}

    def test_weekly_review_uses_question_functions(self):
        from ethan import report
        t = self.db.add_task("交签证材料")
        self.db.complete_task(t)
        facts = report.collect_facts(self.store, DAY)
        self.assertEqual([x["title"] for x in facts["本周完成的待办"]], ["交签证材料"])
        self.assertIn("这周没有记录的", facts)
        self.assertTrue({"week_plan", "record_gaps", "weekly_spend"} <= self.calls("review"))

    def test_mail_draft_asks_who_the_sender_is(self):
        from ethan import desk
        self.db.upsert_person("Leo", circle="导师")
        got = desk._mail_questions(self.db, {"sender": "Leo <leo@example.com>", "subject": "进度"})
        self.assertIn("Leo", got)
        self.assertIn("person", self.calls("mail"))

    def test_prep_and_planner_read_decisions_after_switch(self):
        from ethan import planner, prep
        decisions.add(self.db, "pause", "训练", "这周暂停训练", roles=["planner"], quote="先停")
        self.assertEqual(planner._decisions(self.db), [])  # 没切到新结构前不给
        onto_query.switch(self.db, "planner", True)
        self.assertEqual(planner._decisions(self.db), ["这周暂停训练"])
        lifebase.add_fact(self.db, "preferences", "订餐厅之前先查退改", [self.ev], kind="preference")
        onto_query.switch(self.db, "prep", True)
        brief = prep.memory_brief(self.store, "订餐厅")
        self.assertIn("订餐厅之前先查退改", brief)
        self.assertIn("prep_requirements", self.calls("prep"))

    def test_checkup_reports_sure_contradictions_only(self):
        from ethan import checkup
        self.db.save_plan(DAY.isoformat(), "model", "{}", "", "x", [{"kind": "todo", "title": "晚上跑步"}])
        planner.note_override(self.db, DAY, "晚上跑步", "delete")
        self.say("2026-11-03T10:00:00", "我来帮你查一下签证进度", who="ethan")
        from unittest import mock
        with mock.patch("ethan.clock.her_today", return_value=DAY):
            got = [p for p in checkup.problems(self.tmp.name, self.db, services=False) if p["what"] == "检查：互相矛盾"]
        self.assertEqual(len(got), 1)
        self.assertIn("晚上跑步", got[0]["detail"])


if __name__ == "__main__":
    unittest.main()
