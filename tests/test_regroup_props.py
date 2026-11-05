"""ontology-plan 1.11 回头整理：老说法按已登记的属性重新归（lifebase.regroup_props）。"""

import json
import tempfile
import unittest
from pathlib import Path

from ethan import lifebase, locks, migrate, onto_query
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent


class FakeRouter:
    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls += 1
        ids = [int(x.split("（")[0][1:]) for x in messages[0]["content"].split("\n") if x.startswith("#")]

        class R:
            text = json.dumps([{"id": i, "property": self.answer(i)} for i in ids], ensure_ascii=False)
        return R()


class RegroupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:d:1", "2026-11-01", "mia：……")

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def fact(self, dim, text, **kw):
        return lifebase.add_fact(self.db, dim, text, [self.ev], **kw)

    def prop(self, fid):
        return self.db.conn.execute("SELECT prop FROM facts WHERE id = ?", (fid,)).fetchone()[0]

    def stmt_prop(self, fid):
        return self.db.conn.execute("SELECT s.property FROM statements s JOIN fact_map m ON m.statement_id = s.id WHERE m.fact_id = ?",
                                    (fid,)).fetchone()[0]

    def test_rules(self):
        cases = [("abilities", "2024 年起在 某商学院 读 进修", "studied_at"),
                 ("abilities", "本科毕业于复旦大学经济系", "studied_at"),
                 ("abilities", "2019-2021 年在某咨询公司实习", "worked_at"),
                 ("abilities", "在字节跳动任职产品经理", "worked_at"),
                 ("abilities", "会用 Python 和 SQL 做数据分析", "skill"),
                 ("abilities", "做过一个校园二手交易的项目", "project"),
                 ("rhythms", "平时 7 点半起床", "wake_time"),
                 ("preferences", "不吃香菜", "preference"),
                 ("resources", "英国学生签证已获批", "application_status"),
                 ("resources", "在 Monzo 开了银行账户", "account_status"),
                 ("resources", "预约了 11 月 10 日的 GP", "appointment"),
                 ("abilities", "某商学院 的 进修 项目在读", "studied_at")]  # “项目”没有做 / 牵头这类动作，不算项目
        for dim, text, want in cases:
            self.assertEqual(lifebase.regroup_rule(dim, text), [want], text)
        # 拿不准、计划、不沾边的：不归
        for dim, text in [("abilities", "想读博士"), ("rhythms", "11 点睡 7 点起"), ("values", "重视家人"),
                          ("abilities", "不擅长 Excel"),
                          ("relationships", "妈妈在银行上班")]:
            self.assertNotEqual(len(lifebase.regroup_rule(dim, text)), 1, text)

    def test_rules_server_trial_fixes(self):
        """服务器只读试跑（规则版）里归错的那几类：宁可留在“总的”里，也不要归错。例句是改写过的示意句。"""
        keep = [  # 该归的照样归
            ("resources", "信用卡额度 5 万元", "account_status"),
            ("resources", "持有一张招商银行信用卡", "account_status"),
            ("resources", "在 Monzo 开户了", "account_status"),
            ("resources", "拿到了 某商学院 的录取", "application_status"),
            ("abilities", "在 某短视频公司 负责广告投放平台迭代", "worked_at"),
            ("abilities", "在腾讯待了一年", "worked_at"),
            ("abilities", "后来离开了今日头条", "worked_at"),
            ("abilities", "做过 某电商 的产品经理", "worked_at"),
            ("abilities", "牵头做了一个跨境电商项目", "project"),
            ("abilities", "在做一个 AI 助手项目", "project"),
            ("abilities", "本科在国内读的是经济", "studied_at"),
            ("preferences", "她最喜欢的颜色是蓝色", "preference"),
            ("preferences", "不喜欢的食物是香菜", "preference"),
            ("preferences", "更喜欢一个人安静工作", "preference"),
            ("preferences", "做决定倾向于先看数据", "preference"),
        ]
        for dim, text, want in keep:
            self.assertEqual(lifebase.regroup_rule(dim, text), [want], text)
        wrong = [  # (方面, 句子, 不该归到的属性)
            ("resources", "信用卡还款 1,000 元", "account_status"),
            ("resources", "信用卡扣了 300 元年费", "account_status"),
            ("resources", "收到某旅行平台 500 元退款到银行卡", "account_status"),
            ("resources", "银行卡挂失费 £5", "account_status"),
            ("resources", "TfL 从银行卡扣费 £2.80", "account_status"),
            ("resources", "用信用卡支付了 ¥200", "account_status"),
            ("resources", "她是某商学院分社会员", "account_status"),
            ("resources", "向 UKVI 支付了签证申请费 100 元", "application_status"),
            ("resources", "交了签证费 £500", "application_status"),
            ("abilities", "工作一年半时拿到过某互联网公司的 offer", "application_status"),
            ("resources", "入职前拿到过两家公司的 offer", "application_status"),
            ("preferences", "在腾讯只待了一年就离开，因为不喜欢办公室政治", "preference"),
            ("preferences", "想过把智能耳机做成女性喜欢的耳饰", "preference"),
            ("preferences", "做的产品是年轻用户喜欢的风格", "preference"),
            ("preferences", "自述有点 ADHD 倾向", "preference"),
            ("abilities", "在 某短视频公司 负责某投放平台项目迭代", "project"),
            ("abilities", "电脑被偷了，项目因此停滞", "project"),
            ("abilities", "项目进度一般", "project"),
            ("thinking", "她认为自己原来只在国内读书工作", "studied_at"),
            ("abilities", "她觉得自己原来只在国内读书", "studied_at"),
        ]
        for dim, text, bad in wrong:
            self.assertNotIn(bad, lifebase.regroup_rule(dim, text), text)
        # 负责 + 公司名：算任职，不同时算项目
        self.assertEqual(lifebase.regroup_rule("abilities", "在 某短视频公司 负责某投放平台项目迭代"), ["worked_at"])

    def test_rules_second_trial_fixes(self):
        """服务器第二次只读试跑里又归错的几类（示意句，金额改写过）。"""
        keep = [
            ("resources", "在 Monzo 银行开了账户", "account_status"),
            ("resources", "Monzo 出具证明信确认她名下有账户", "account_status"),
            ("resources", "持有某银行双币信用卡", "account_status"),
            ("resources", "双币卡额度若干元", "account_status"),
            ("resources", "个人总授信额度若干元", "account_status"),
            ("resources", "芬兰签证申请类型是短期签证", "application_status"),
            ("resources", "芬兰签证材料已备齐", "application_status"),
            ("resources", "签证申请被 VFS Global 收到", "application_status"),
            ("abilities", "在 某短视频公司 带 3 名初级产品经理", "worked_at"),
            ("abilities", "入职某资讯公司前两周学广告系统", "worked_at"),
            ("abilities", "在 某电商 负责整块业务", "worked_at"),
            ("abilities", "在 某商学院 做交换生", "studied_at"),
            ("abilities", "2016 年校招时是某大学大三在读学生", "studied_at"),
        ]
        for dim, text, want in keep:
            self.assertEqual(lifebase.regroup_rule(dim, text), [want], text)
        wrong = [
            # 1. 某天哪笔钱进出：日期 + 金额，或流水词
            ("resources", "她的信用卡 9 月 15 日有两笔入账，100 元和 200 元", "account_status"),
            ("resources", "9 月 15 日信用卡入账 100 元", "account_status"),
            ("resources", "2026-09-25 购汇转出若干元，汇率若干，用于还美元账户欠款", "account_status"),
            ("resources", "2026-09-25 银行账户 100 元", "account_status"),
            ("resources", "09-21 和 09-28 信用卡各有一笔 Travel Charge，含若干英镑", "account_status"),
            ("resources", "09-21 信用卡 2 英镑", "account_status"),
            ("resources", "信用卡有一笔待查款项", "account_status"),
            # 2. 求职申请不算申请进度
            ("resources", "收到了某公司校园招聘的面试邀请，申请换面试场次", "application_status"),
            ("abilities", "发邮件申请‘XX-YY-某大学-软件工程’", "application_status"),
            ("abilities", "看到招聘消息后发简历，申请了暑期游戏策划实习生岗位", "application_status"),
            ("abilities", "申请营销事业部的实习生岗位，可实习 6 个月", "application_status"),
            ("abilities", "发简历申请市场部实习生岗位", "application_status"),
            ("resources", "申请了某公司的产品职位", "application_status"),
            # 3. 投简历、没具体单位、学到流程，不算任职
            ("abilities", "向一家公司投了市场部实习生的简历", "worked_at"),
            ("abilities", "看到一家公司招聘互动营销策划实习生的消息，并投了简历", "worked_at"),
            ("abilities", "因为大四没有课要修，实习的工作地点非常灵活", "worked_at"),
            ("abilities", "大四已没有课程安排，实习地点和时间都容易协调", "worked_at"),
            ("abilities", "在实习中了解了产品从需求到落地的基本流程", "worked_at"),
            ("abilities", "在腾讯实习时学到了产品上线的流程", "worked_at"),
            # 4. 奖学金、社团职务不算就读
            ("abilities", "本科期间获得过专业奖学金", "studied_at"),
            ("abilities", "某大学 进修 人工智能俱乐部副主席", "studied_at"),
            ("abilities", "本科时是学生会外联部部长", "studied_at"),
        ]
        for dim, text, bad in wrong:
            self.assertNotIn(bad, lifebase.regroup_rule(dim, text), text)

    def test_write_and_mirror(self):
        a = self.fact("abilities", "2024 年起在 某商学院 读 进修")
        b = self.fact("values", "重视家人")
        got = lifebase.regroup_props(self.db, dry_run=False)
        self.assertEqual((got["changed"], got["by_prop"], got["left"]), (1, {"studied_at": 1}, 1))
        self.assertEqual((self.prop(a), self.stmt_prop(a)), ("studied_at", "studied_at"))  # 双写触发器跟着改
        self.assertEqual((self.prop(b), self.stmt_prop(b)), (None, "dim:values"))
        ch = self.db.conn.execute("SELECT old, new FROM statement_changes WHERE field = 'property'").fetchall()
        self.assertEqual([tuple(r) for r in ch], [("dim:abilities", "studied_at")])
        self.assertEqual(lifebase.reconcile_statements(self.db), [])
        # 内容不变、可重复跑
        self.assertEqual(self.db.conn.execute("SELECT statement FROM facts WHERE id = ?", (a,)).fetchone()[0], "2024 年起在 某商学院 读 进修")
        self.assertEqual(lifebase.regroup_props(self.db, dry_run=False)["changed"], 0)
        # 经历那道题能查到了
        self.assertEqual(onto_query.background(self.db, "ethan")["status"], "answer")

    def test_dry_run_writes_nothing(self):
        a = self.fact("abilities", "会用 Python 和 SQL 做数据分析")
        got = lifebase.regroup_props(self.db)
        self.assertEqual((got["dry_run"], got["changed"], got["by_prop"]), (True, 1, {"skill": 1}))
        self.assertEqual(got["samples"][0]["id"], a)
        self.assertIsNone(self.prop(a))
        self.assertEqual(self.stmt_prop(a), "dim:abilities")

    def test_locked_untouched(self):
        a = self.fact("abilities", "本科毕业于复旦大学经济系")
        self.db.conn.execute("UPDATE facts SET locked = 1 WHERE id = ?", (a,))
        b = self.fact("abilities", "硕士在 某商学院 就读")
        locks.lock(self.db, "fact", b, "prop")
        got = lifebase.regroup_props(self.db, dry_run=False)
        self.assertEqual((got["changed"], got["locked"]), (0, 2))
        self.assertIsNone(self.prop(a))
        self.assertIsNone(self.prop(b))

    def test_existing_prop_untouched(self):
        a = self.fact("abilities", "本科毕业于复旦大学经济系", prop="project")
        got = lifebase.regroup_props(self.db, dry_run=False)
        self.assertEqual(got["changed"], 0)
        self.assertEqual(self.prop(a), "project")

    def test_others_and_deleted_untouched(self):
        self.db.conn.execute("INSERT INTO people (id, name, updated_at) VALUES (7, 'Leo', '2026-11-01')")
        p = self.fact("abilities", "在 某商学院 读 进修", subject="person:7")
        d = self.fact("abilities", "在 某商学院 读 进修")
        self.db.conn.execute("UPDATE facts SET status = 'deleted' WHERE id = ?", (d,))
        self.assertEqual(lifebase.regroup_props(self.db, dry_run=False)["changed"], 0)
        self.assertIsNone(self.prop(p))

    def test_model_only_for_unsure(self):
        a = self.fact("abilities", "2024 年起在 某商学院 读 进修")  # 规则能归，不问模型
        b = self.fact("abilities", "大学时在学生会做外联")  # 规则没把握
        c = self.fact("values", "重视家人")
        router = FakeRouter(lambda i: "worked_at" if i == b else "不确定")
        got = lifebase.regroup_props(self.db, router=router, dry_run=False)
        self.assertEqual((got["by_rule"], got["by_model"], got["left"]), (1, 1, 1))
        self.assertEqual((self.prop(a), self.prop(b), self.prop(c)), ("studied_at", "worked_at", None))
        # 模型乱填没登记的属性：校验不过，什么都不改
        e = self.fact("abilities", "高中时参加过辩论队")
        bad = FakeRouter(lambda i: "lives_in")
        self.assertEqual(lifebase.regroup_props(self.db, router=bad, dry_run=False)["by_model"], 0)
        self.assertIsNone(self.prop(e))

    def test_preferences_still_answered(self):
        self.fact("preferences", "不吃香菜")
        lifebase.regroup_props(self.db, dry_run=False)
        items = onto_query.preferences(self.db, "ethan")["items"]
        self.assertTrue(any("香菜" in str(i) for i in items))
        # 归到细属性后，对外那道“身体、钱、关系、性格不对外”的双保险按属性的敏感标签照样生效
        self.assertEqual(onto_query.SENSITIVE["application_status"], "resources")

    def test_migration_approved_and_pending(self):
        """她 11-04 “同意归类”：草稿改名成正式迁移，开头写了她的原话，会被执行。"""
        names = [p.name for p in migrate.pending(ROOT, self.root)]
        self.assertIn("20261104_regroup_props.py", names)
        self.assertIn("同意归类", (ROOT / "migrations" / "20261104_regroup_props.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class ThirdTrialFixTest(unittest.TestCase):
    def test_situations_around_a_contest_are_not_projects(self):
        from ethan import lifebase
        for t in ("她写邮件时，校园网上的比赛名单公示打不开，学校联系人办公室电话也没人接。",
                  "她写邮件时，还不知道某团队在比赛中的晋级情况。"):
            self.assertNotIn("project", lifebase.regroup_rule("abilities", t), t)
        self.assertEqual(lifebase.regroup_rule("abilities", "她牵头立项自建归因项目，并完全负责落地。"), ["project"])



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
