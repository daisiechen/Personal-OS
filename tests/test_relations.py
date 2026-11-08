"""关系（ethan/relations.py，docs/design/relations.md）：Mac 端统计（谁先开口、跳过规则）、上报接口鉴权、
people 扩展和她改过的锁、删卡不再建回来、每晚整理（假模型）、三张清单、周报和聊天提示、工作台“关系”页的接口。"""

import json
import os
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import evals, lifebase, locks, relations, report
from ethan import wechatexport as wx
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

TODAY = date(2026, 11, 4)
CLOUD = "c" * 32
AUTH = {"Authorization": f"Bearer {CLOUD}"}


os.environ.setdefault("ETHAN_NO_KICK", "1")  # 测试里不真起后台整理进程


class FakeRouter:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append(messages[0]["content"])
        return SimpleNamespace(text=json.dumps(self.answers.pop(0), ensure_ascii=False))


def ts(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp())


def rec(t, her, sender, text="嗯嗯好的呀"):
    return {"time": t + "+00:00", "ts": ts(t), "is_her": her, "sender": "wxid_me" if her else sender, "text": text, "type": 1}


def item(key="a1" * 8, name="小林", **kw):
    return {"key": key, "name": name, "last": "2026-10-01", "n90": 40, "n365": 300, "total": 500, "her_share": 0.5,
            "her_starts": 0.4, "rounds": 30, "groups": ["读书会"], **kw}


class StatsTest(unittest.TestCase):
    """Mac 端：按私聊联系人算统计。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, f, recs):
        (self.out / f).write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n", encoding="utf-8")

    def test_who_starts_rounds_groups_and_skip(self):
        lin = [rec("2026-11-01T09:00:00", True, "wxid_lin"), rec("2026-11-01T09:05:00", False, "wxid_lin"),
               rec("2026-11-01T12:00:00", False, "wxid_lin"),  # 3 小时：还是同一轮
               rec("2026-11-02T20:00:00", False, "wxid_lin"), rec("2026-11-02T20:01:00", True, "wxid_lin"),  # 新一轮，对方先开口
               rec("2025-01-01T08:00:00", True, "wxid_lin")]  # 一年多以前
        self.write("小林.jsonl", lin)
        self.write("房产中介王.jsonl", [rec("2026-11-01T09:00:00", True, "wxid_ag"), rec("2026-11-01T09:01:00", False, "wxid_ag"),
                                    rec("2026-11-01T09:02:00", False, "wxid_ag")])
        self.write("Bob.jsonl", [rec("2026-11-01T09:00:00", False, "wxid_bob")] * 5)  # 只有对方在说：不建卡
        self.write("读书会.jsonl", [rec("2026-10-01T09:00:00", False, "wxid_lin"), rec("2026-10-01T09:01:00", False, "wxid_lin")])
        self.write("同事群.jsonl", [rec("2026-10-01T09:00:00", False, "wxid_lin")])
        self.write("妈妈.jsonl", [rec("2026-11-03T09:00:00", True, "wxid_mom")] * 2 + [rec("2026-11-03T09:01:00", False, "wxid_mom")])
        st = {"chats": {"wxid_lin": {"name": "小林", "file": "小林.jsonl"}, "wxid_ag": {"name": "房产中介王", "file": "房产中介王.jsonl"},
                        "wxid_bob": {"name": "Bob", "file": "Bob.jsonl"}, "1@chatroom": {"name": "读书会", "file": "读书会.jsonl"},
                        "2@chatroom": {"name": "同事群", "file": "同事群.jsonl"}, "wxid_mom": {"name": "妈妈", "file": "妈妈.jsonl"}}}
        out = wx.people_stats(st, self.out, list(st["chats"]), now_ts=ts("2026-11-04T00:00:00"), skip=["妈妈"])
        self.assertEqual([p["name"] for p in out], ["小林"])  # 中介按规则跳过，妈妈按 --people-skip 跳过，Bob 单方面
        p = out[0]
        self.assertEqual(p["key"], relations.wx_key("wxid_lin"))
        self.assertNotIn("wxid_lin", json.dumps(out))
        self.assertEqual((p["rounds"], p["her_starts"]), (3, round(2 / 3, 3)))  # 2025 那轮她先、先、11-02 对方先
        self.assertEqual((p["n90"], p["n365"], p["total"]), (5, 5, 6))
        self.assertEqual(p["her_share"], 0.5)
        self.assertEqual(p["last"], "2026-11-02")
        self.assertEqual(p["groups"], ["读书会", "同事群"])  # 按对方在群里说话多少排

    def test_service_names(self):
        for n in ("链家房产小张", "Hilton 酒店管家", "签证代办 Amy", "专属服务顾问", "VIP管家", "某某客服", "留学小助手"):
            self.assertTrue(relations.is_service(n), n)
        for n in ("小林", "母上大人", "王老师"):
            self.assertFalse(relations.is_service(n), n)
        self.assertTrue(relations.is_service("张三", ["张"]))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        self.inbox = Inbox(self.store, {"cloud": CLOUD})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, people, auth=AUTH):
        return self.inbox.handle("/cloud/people_stats", auth, json.dumps({"people": people}, ensure_ascii=False).encode())

    def person(self, name):
        r = self.db.conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None

    def test_merge_chat_card_into_wechat_card(self):
        """聊天里冒出来的卡（妈妈）和微信那张（母上大人）是同一个人，给个合并入口；合完以后聊天再提到“妈妈”也记到这张上。"""
        from ethan import lifebase
        self.post([item("b2" * 8, "母上大人", last="2026-11-02")])
        mom = self.person("母上大人")["id"]
        self.db.conn.execute("UPDATE people SET relation = '她妈妈' WHERE id = ?", (mom,))
        chat = relations.upsert(self.db, "妈妈", circle="家人", birthday="03-14", notes="爱跳广场舞")
        self.db.conn.commit()
        sug = relations.merge_suggestions(self.db)
        self.assertEqual([(m["drop"]["name"], m["keep"]["name"]) for m in sug], [("妈妈", "母上大人")])
        lifebase.ensure(self.db)
        ev = lifebase.add_evidence(self.db, "chat", "raw:m:1", "2026-11-01", "mia：我妈爱跳广场舞")
        fid = lifebase.add_fact(self.db, "relationships", "妈妈爱跳广场舞", [ev], subject=f"person:{chat}")
        r = relations.merge(self.db, mom, chat)
        self.assertTrue(r["ok"], r)
        self.assertIsNone(self.person("妈妈"))
        p = self.person("母上大人")
        self.assertEqual((p["circle"], p["birthday"], p["notes"]), ("家人", "03-14", "爱跳广场舞"))
        self.assertEqual(self.db.conn.execute("SELECT subject FROM facts WHERE id = ?", (fid,)).fetchone()[0], f"person:{mom}")
        self.assertEqual(relations.upsert(self.db, "妈妈", recent="最近在学太极"), mom)  # 别名：不再新建
        self.assertIsNone(self.person("妈妈"))
        self.assertEqual(relations.merge_suggestions(self.db), [])
        self.assertFalse(relations.merge(self.db, mom, mom)["ok"])

    def test_loops_list_people_with_open_things(self):
        """待回复改名待履约，列有没了结的事的人（不限 7 天、不分谁欠谁）。"""
        import json as _json
        from datetime import date as _d
        pid = relations.upsert(self.db, "阿杰")
        loops = [{"kind": "she_promised", "date": "2026-09-01", "text": "帮他看简历", "quote": "x"},
                 {"kind": "they_asked", "date": "2026-10-20", "text": "问她要不要一起去芬兰", "quote": "y"}]
        self.db.conn.execute("UPDATE people SET open_loops = ? WHERE id = ?", (_json.dumps(loops, ensure_ascii=False), pid))
        self.db.conn.commit()
        got = relations.due(self.db, _d(2026, 11, 7))["loops"]
        self.assertEqual([(x["name"], x["n"], x["text"]) for x in got], [("阿杰", 2, "问她要不要一起去芬兰")])

    def test_no_star_never_reminds_by_default(self):
        """"""
        self.assertIsNone(relations.interval({"importance": 0, "contact_every_days": None}))
        self.assertIsNone(relations.interval({"importance": 1, "contact_every_days": None}))
        self.assertEqual(relations.interval({"importance": 0, "contact_every_days": 30}), 30)  # 她自己设了天数照她的

    def test_her_edits_are_reference_not_frozen(self):
        """改过的只是冷启动用的，系统要根据交往随时调整，参考她改过的备注。"""
        from datetime import date as _d
        self.post([item()])
        p = self.person("小林")
        self.assertIn("参考", relations.set_field(self.db, p["id"], "relation", "大学室友")["msg"])  # 提示不再说“不动”
        relations.set_field(self.db, p["id"], "notes", "去年搬去上海了")
        self.assertIn("不会改", relations.set_field(self.db, p["id"], "birthday", "03-14")["msg"])
        row = self.db.conn.execute("SELECT * FROM people WHERE id = ?", (p["id"],)).fetchone()
        hers = relations.hers_for(self.db, row)
        self.assertEqual((hers["relation"], hers["notes"]), ("大学室友", "去年搬去上海了"))
        relations._apply_card(self.db, row, {"relation": "大学室友，现在在上海做咨询", "birthday": "05-01"}, _d(2026, 11, 7), started="9999")
        after = self.person("小林")
        self.assertEqual(after["relation"], "大学室友，现在在上海做咨询")  # 描述类跟着往来更新
        self.assertEqual(after["birthday"], "03-14")  # 她的设置不动

    def test_no_star_and_new_circles(self):
        """重要程度加“没星”；关系圈加“中介”“不重要”。"""
        self.post([item()])
        pid = self.person("小林")["id"]
        self.assertTrue(relations.set_field(self.db, pid, "importance", 0)["ok"])
        self.assertIsNone(relations.interval(self.person("小林")))  # 没星：不按重要程度提醒
        self.assertIn("服务", relations.CIRCLES)  # 把“中介”改名“服务”（中介、客服、导游……）
        self.assertNotIn("中介", relations.CIRCLES)
        self.assertNotIn("不重要", relations.CIRCLES)  # 不重要是重要程度（没星），不是关系

    def test_suggestions_only_her_own_kin_and_can_dismiss(self):
        """要能点“不是同一个人”。"""
        self.post([item("c1" * 8, "李林花"), item("c2" * 8, "母上大人"), item("c3" * 8, "新海美婷"), item("c4" * 8, "* Y")])
        for name, rel in (("李林花", "朱天宇的妈妈，想给她介绍对象的长辈"), ("母上大人", "她妈妈"),
                          ("新海美婷", "叫她“姐姐”的妹妹，她给对方拍过照片"), ("* Y", "她的妹妹，两人都叫同一对父母“咱爸咱妈”")):
            self.db.conn.execute("UPDATE people SET relation = ? WHERE name = ?", (rel, name))
        mom, sis = relations.upsert(self.db, "妈妈", circle="家人"), relations.upsert(self.db, "妹妹", circle="家人")
        self.db.conn.commit()
        got = {m["drop"]["name"]: m["keep"]["name"] for m in relations.merge_suggestions(self.db)}
        self.assertEqual(got, {"妈妈": "母上大人", "妹妹": "* Y"})
        relations.not_same(self.db, sis, self.person("* Y")["id"])
        self.assertEqual({m["drop"]["name"] for m in relations.merge_suggestions(self.db)}, {"妈妈"})

    def test_auth(self):
        self.assertEqual(self.post([item()], {"Authorization": "Bearer wrong"})[0], 401)
        self.assertEqual(self.inbox.handle("/cloud/people_stats", AUTH, b"not json")[0], 400)
        self.assertEqual(Inbox(self.store, {"grok": "g" * 32}).handle("/cloud/people_stats", {"Authorization": "Bearer " + "g" * 32}, b"{}")[0], 401)
        code, got = self.post([item()])
        self.assertEqual((code, got["new"]), (200, 1))

    def test_ingest_auto_importance_and_family(self):
        self.post([item(), item("b2" * 8, "母上大人", n90=3, n365=10), item("c3" * 8, "某某客服"),
                   item("d4" * 8, "偶尔", n90=1, n365=5, last="2026-01-01"), item("zz", "坏指纹")])
        self.assertEqual(self.person("小林")["importance"], 2)
        mom = self.person("母上大人")
        self.assertEqual((mom["importance"], mom["circle"]), (3, "家人"))
        self.assertEqual(self.person("偶尔")["importance"], 1)
        self.assertIsNone(self.person("某某客服"))
        self.assertIsNone(self.person("坏指纹"))
        self.assertEqual(self.person("小林")["last_contact"], "2026-10-01")
        self.assertEqual(json.loads(self.person("小林")["stats"])["groups"], ["读书会"])

    def test_existing_card_by_name_merges(self):
        self.db.upsert_person("小林", circle="挚友", last_contact="2026-10-20")
        self.post([item()])
        p = self.person("小林")
        self.assertEqual((p["circle"], p["last_contact"], p["wx_key"]), ("挚友", "2026-10-20", "a1" * 8))  # 更近的联系不被覆盖
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 1)

    def test_her_edits_are_locked(self):
        self.post([item()])
        pid = self.person("小林")["id"]
        self.assertTrue(relations.set_field(self.db, pid, "importance", 3)["ok"])
        self.assertTrue(relations.set_field(self.db, pid, "name", "林林")["ok"])
        self.assertFalse(relations.set_field(self.db, pid, "birthday", "3月14")["ok"])
        self.assertFalse(relations.set_field(self.db, pid, "stats", "{}")["ok"])
        self.post([item(n90=0, n365=0, last="2026-11-01")])  # 统计照样覆盖数字，重要程度和名字不动
        p = self.person("林林")
        self.assertEqual((p["importance"], p["last_contact"]), (3, "2026-11-01"))
        self.assertTrue(locks.locked(self.db, "person", pid, "importance"))

    def test_delete_is_not_rebuilt(self):
        self.post([item()])
        pid = self.person("小林")["id"]
        self.assertTrue(relations.delete(self.db, pid)["ok"])
        self.post([item()])
        self.assertIsNone(self.person("小林"))
        self.post([item("e5" * 8, "小林")])  # 换了指纹同名也不建
        self.assertIsNone(self.person("小林"))

    def evidence(self, name, body):
        lifebase.ensure(self.db)
        lifebase.add_evidence(self.db, "interview", "interview:" + name + str(len(body)), "2026-11-03",
                              f"【访谈】会话：微信 · {name}；场合：{wx.SCENE}；时间：2026-11-03\n{body}")
        self.db.conn.commit()

    def test_organize_with_locks_and_validation(self):
        self.post([item(), item("b2" * 8, "没记录的人")])
        pid = self.person("小林")["id"]
        relations.set_field(self.db, pid, "recent", "她自己写的近况")
        body = "问：（微信 · 小林，2026-11-03 22:15；前面别人说的（小林）：“我下周三答辩，好紧张”；别人说的（小林）：“你圣诞回不回国？”）\n答：答辩加油！"
        self.evidence("小林", body)
        bad = {"relation": "朋友", "topics": [], "recent": "", "dates": [], "open_loops": [
            {"kind": "they_asked", "date": "2026-11-03", "text": "圣诞回不回国", "quote": "编出来的话"}]}
        good = {"relation": "大学同学", "circle": "同学", "topics": ["答辩", "回国"], "recent": "下周三答辩", "draft": "答辩准备得怎么样啦？",
                "dates": [{"date": "03-14", "what": "生日"}, {"date": "2026-11-11", "what": "答辩"}],
                "open_loops": [{"kind": "they_asked", "date": "2026-11-03", "text": "问你圣诞回不回国", "quote": "你圣诞回不回国？"}]}
        router = FakeRouter(bad, good)
        got = relations.organize(self.db, router, TODAY)
        self.assertEqual((got["done"], got["skipped"]), (1, 1))
        self.assertEqual(len(router.calls), 2)  # 第一次原话不在记录里，重试
        self.assertIn("圣诞回不回国", router.calls[0])
        p = self.person("小林")
        self.assertEqual((p["relation"], p["circle"], p["birthday"], p["recent"]), ("大学同学", "同学", "03-14", "下周三答辩"))  # 11-07 起她改的只当参考
        self.assertIn("她自己写的近况", router.calls[-1])  # 她写的交给模型参考了
        self.assertTrue(p["organized_at"])
        self.assertEqual(relations.candidates(self.db), [dict(self.db.conn.execute("SELECT * FROM people WHERE name = '没记录的人'").fetchone())])
        d = relations.due(self.db, TODAY)
        self.assertEqual([x["name"] for x in d["reply"]], ["小林"])
        self.assertEqual([(x["name"], x["what"]) for x in d["days"]], [("小林", "答辩")])
        # 她点了结：拿掉，再整理也不加回来
        relations.loop_done(self.db, pid, "问你圣诞回不回国")
        self.db.conn.execute("UPDATE people SET organized_at = NULL WHERE id = ?", (pid,))
        relations.organize(self.db, FakeRouter(good), TODAY)
        self.assertEqual(relations.due(self.db, TODAY)["reply"], [])

    def test_organize_reads_transcript_format(self):  # 微信交整段对话：人物卡照样读、提示词写清谁说的
        from ethan import interviews, llm
        self.post([item()])
        body = "〔2026-11-03〕\n【小林】我下周三答辩，好紧张\n【小林】你圣诞回不回国？\n【她】答辩加油！"
        code, _ = interviews.receive(self.store, {"session": "微信 · 小林", "scene": wx.SCENE, "text": body, "format": "transcript"})
        self.assertEqual(code, 200)
        self.assertEqual(relations._transcript(self.db, "小林"), body)
        self.assertIn("【她】开头的是她", llm.load_prompt("person_card")[1])
        good = {"relation": "朋友", "topics": [], "recent": "下周三答辩", "dates": [],
                "open_loops": [{"kind": "they_asked", "date": "2026-11-03", "text": "问你圣诞回不回国", "quote": "你圣诞回不回国？"}]}
        router = FakeRouter(good)
        self.assertEqual(relations.organize(self.db, router, TODAY)["done"], 1)
        self.assertIn("【小林】你圣诞回不回国？", router.calls[0])
        self.assertEqual([x["name"] for x in relations.due(self.db, TODAY)["reply"]], ["小林"])

    def test_due_lists(self):
        self.post([item(last="2026-07-01"), item("b2" * 8, "母上大人", last="2026-11-03", n90=200),
                   item("c3" * 8, "老同学", n90=0, n365=2, last="2026-03-01")])
        mom = self.person("母上大人")["id"]
        self.db.conn.execute("UPDATE people SET birthday = '11-10', topics = ? WHERE id = ?", (json.dumps(["羽绒服"]), mom))
        self.db.conn.execute("UPDATE people SET recent = '刚换了工作' WHERE name = '小林'")
        self.db.conn.commit()
        d = relations.due(self.db, TODAY)
        self.assertEqual([x["name"] for x in d["contact"]], ["小林"])  # 2 级 90 天超了；1 级不按时间提醒（改）；妈妈昨天才聊
        self.assertIn("刚换了工作", d["contact"][0]["topic"])
        self.assertEqual([(x["name"], x["in"]) for x in d["days"]], [("母上大人", 6)])
        relations.set_field(self.db, self.person("小林")["id"], "contact_every_days", 200)  # 她改了间隔
        self.assertEqual([x["name"] for x in relations.due(self.db, TODAY)["contact"]], [])
        # 要能选“永不”；再点“默认”回到按重要程度
        old = self.person("老同学")["id"]
        self.assertTrue(relations.set_field(self.db, old, "contact_every_days", "never")["ok"])
        self.assertEqual(relations.due(self.db, TODAY)["contact"], [])
        self.assertIsNone(relations.interval(self.person("老同学")))
        self.assertFalse(relations.set_field(self.db, old, "contact_every_days", "5000")["ok"])
        relations.set_field(self.db, old, "contact_every_days", "")  # 回到默认：一颗星不按时间提醒
        self.assertEqual(relations.due(self.db, TODAY)["contact"], [])
        relations.set_field(self.db, old, "contact_every_days", "30")  # 一颗星也能自己设间隔
        self.assertEqual([x["name"] for x in relations.due(self.db, TODAY)["contact"]], ["老同学"])
        line = relations.week_line(self.db, TODAY)
        self.assertIn("老同学", line["这周该联系谁"][0])
        self.assertIn("母上大人", relations.chat_hint(self.db, TODAY))
        self.assertEqual(report.collect_facts(self.store, TODAY)["社交"]["生日和重要日子"], ["母上大人 11-10 生日"])
        self.assertIn("她身边的人", self._prompt())

    def _prompt(self):
        from ethan.core.agent import build_system_prompt
        return build_system_prompt("persona", self.store.load_context(today=TODAY, recent_messages=0))

    def test_eval_cases_score(self):
        cases = {c["id"]: c for c in evals.load("person_card")}
        self.assertGreaterEqual(len(cases), 3)
        ok = {"relation": "她妈妈", "open_loops": [{"kind": "she_promised", "text": "答应打视频", "quote": "我一定打视频给你"}]}
        self.assertTrue(evals._score_person_card(ok, cases["mom-birthday-promise"]["expect"])[0])
        self.assertFalse(evals._score_person_card({"relation": "老师", "open_loops": [{"kind": "they_asked", "text": "x"}]},
                                                  cases["answered-no-loop"]["expect"])[0])
        # 题集的输入能真走一遍（假模型）：引用的原话在记录里才过
        c = cases["friend-asked-unanswered"]
        out = evals._person_card(FakeRouter({"relation": "朋友", "topics": [], "recent": "", "dates": [], "open_loops": [
            {"kind": "they_asked", "date": "2026-11-03", "text": "圣诞回不回国", "quote": "对了你圣诞回不回国？"}]}), self.db, c)
        self.assertTrue(evals._score_person_card(out, c["expect"])[0])


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root)
        self.wb = Workbench(self.store, Settings(root / "settings.json"))
        relations.ingest_stats(self.store.db, {"people": [item(), item("b2" * 8, "母上大人", n90=200)]})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_page_and_actions(self):
        page = self.wb.relations_page()
        self.assertEqual({"due", "people", "defaults", "circles"} <= set(page), True)
        self.assertEqual([p["name"] for p in page["people"]], ["母上大人", "小林"])  # 重要的在前
        lin = page["people"][1]
        self.assertNotIn("wx_key", lin)
        self.assertEqual((lin["every"], lin["every_set"]), (90, False))  # 两颗星默认 90 天（改）
        out = self.wb.act("person.set", {"id": lin["id"], "field": "circle", "value": "挚友"})
        self.assertTrue(out["ok"], out)
        lin = next(p for p in self.wb.relations_page()["people"] if p["name"] == "小林")
        self.assertEqual((lin["circle"], lin["locked"]), ("挚友", ["circle"]))
        self.assertFalse(self.wb.act("person.set", {"id": lin["id"], "field": "circle", "value": "路人甲"})["ok"])
        self.assertTrue(self.wb.act("person.delete", {"id": lin["id"]})["ok"])
        self.assertEqual([p["name"] for p in self.wb.relations_page()["people"]], ["母上大人"])

    def test_http_route(self):
        from ethan.workbench import server
        self.assertIn('path == "/api/relations"', Path(server.__file__).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()


class OrganizeAllTest(unittest.TestCase):
    def test_kick_all_spawns_background(self):
        calls = []
        self.assertTrue(relations.kick_all("/data", spawn=lambda cmd, **kw: calls.append(cmd)))
        self.assertIn("--organize-all", calls[0])

    def test_organize_all_loops_until_nothing_left(self):
        from unittest import mock
        rounds = [{"done": 20, "skipped": 0, "failed": 0, "names": ["a"]}, {"done": 3, "skipped": 1, "failed": 0, "names": ["b"]},
                  {"done": 0, "skipped": 2, "failed": 0, "names": []}]
        with mock.patch.object(relations, "organize", side_effect=rounds):
            got = relations.organize_all(None, None, None)
        self.assertEqual(got["done"], 23)


class ReviewP2Test(unittest.TestCase):
    """Codex 评审 11-05 的 P2（第 9、13 条）：遗忘覆盖人物卡；名字带 % / _、同名两人、不存在的日期、了结超过 50 条和改写后不复活。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        self.mirror = tempfile.TemporaryDirectory()
        from unittest import mock
        self.patch = mock.patch.object(lifebase, "MIRROR_DIR", Path(self.mirror.name))
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.store.close()
        self.tmp.cleanup()
        self.mirror.cleanup()

    def person(self, name):
        r = self.db.conn.execute("SELECT * FROM people WHERE name = ?", (name,)).fetchone()
        return dict(r) if r else None

    def receive(self, name, body, key=None):
        from ethan import interviews
        p = {"session": f"微信 · {name}", "scene": wx.SCENE, "text": body, "format": "transcript"}
        if key:
            p["key"] = key
        code, got = interviews.receive(self.store, p)
        self.assertEqual(code, 200, got)
        return got["evidence"]

    CARD = {"relation": "大学同学", "how_met": "大一室友", "circle": "同学", "topics": ["答辩"], "recent": "下周三答辩",
            "draft": "答辩准备得怎么样啦？", "dates": [{"date": "03-14", "what": "生日"}, {"date": "2026-11-11", "what": "答辩"}],
            "open_loops": [{"kind": "they_asked", "date": "2026-11-03", "text": "问你圣诞回不回国", "quote": "你圣诞回不回国？"}]}
    BODY = "〔2026-11-03〕\n【小林】我下周三答辩，好紧张\n【小林】你圣诞回不回国？\n【她】答辩加油！"

    def test_forget_clears_card_fields_but_keeps_locked(self):
        relations.ingest_stats(self.db, {"people": [item()]})
        pid = self.person("小林")["id"]
        relations.set_field(self.db, pid, "recent", "她自己写的近况")
        ev = self.receive("小林", self.BODY)
        self.assertEqual(relations.organize(self.db, FakeRouter(self.CARD), TODAY)["done"], 1)
        p = self.person("小林")
        self.assertEqual((p["relation"], p["birthday"]), ("大学同学", "03-14"))
        self.assertEqual(json.loads(p["source_ids"]), [ev])
        rep = lifebase.forget(self.db, [ev], root=self.root)
        self.assertEqual(rep["people_cleared"], [pid])
        p = self.person("小林")
        for k in ("relation", "how_met", "circle", "draft", "birthday"):
            self.assertIsNone(p[k], k)
        self.assertEqual((json.loads(p["topics"]), json.loads(p["dates"]), json.loads(p["open_loops"])), ([], [], []))
        self.assertEqual(p["recent"], "她自己写的近况")  # 她锁住的不动
        self.assertIsNone(p["organized_at"])  # 等下次整理
        self.assertEqual(relations.due(self.db, TODAY)["reply"], [])
        router = FakeRouter(self.CARD)
        self.assertEqual(relations.organize(self.db, router, TODAY)["skipped"], 1)  # 删掉的记录不再读
        self.assertEqual(router.calls, [])

    def test_reapply_deletions_idempotent_and_nightly_no_rehold(self):
        relations.ingest_stats(self.db, {"people": [item()]})
        ev = self.receive("小林", self.BODY)
        lifebase.forget(self.db, [ev], root=self.root)
        self.assertEqual(lifebase.reapply_deletions(self.db, self.root)["redone"], 0)
        self.assertEqual(lifebase.reapply_deletions(self.db, self.root)["redone"], 0)
        self.assertIsNone(lifebase.on_hold(self.db))
        # 换了新服务器、目录外那份不在：恢复时暂停；她放开以后，每晚整理里那次不再暂停
        for f in Path(self.mirror.name).iterdir():
            f.unlink()
        self.assertFalse(lifebase.reapply_deletions(self.db, self.root)["verified"])
        self.assertTrue(lifebase.on_hold(self.db))
        lifebase.release_hold(self.db)
        lifebase.reapply_deletions(self.db, self.root, may_hold=False)
        self.assertIsNone(lifebase.on_hold(self.db))

    def test_nightly_calls_reapply_first(self):
        from ethan import nightly
        src = Path(nightly.__file__).read_text(encoding="utf-8")
        self.assertTrue("reapply_deletions(store.db, data, may_hold=False)" in src)
        self.assertLess(src.index('step(failed, "删除重做"'), src.index("lambda: run(store, router_for(), day)"))  # 2026-11-06 各步分开拦错误后

    def test_name_with_like_wildcards(self):
        relations.ingest_stats(self.db, {"people": [item(), item("b2" * 8, "小%"), item("c3" * 8, "小_")]})
        self.receive("小林", self.BODY)
        self.assertEqual(relations._transcript(self.db, "小%"), "")
        self.assertEqual(relations._transcript(self.db, "小_"), "")
        self.assertEqual(relations._transcript(self.db, "小林"), self.BODY)

    def test_same_name_two_people_not_mixed(self):
        a, b = "a1" * 8, "b2" * 8
        relations.ingest_stats(self.db, {"people": [item(a, "小林"), item(b, "小林")]})
        self.receive("小林", self.BODY, key=a)
        other = "〔2026-11-02〕\n【小林】周末爬山吗\n【她】好呀"
        self.receive("小林", other, key=b)
        self.receive("小林", "〔2026-11-01〕\n【小林】没带指纹的旧记录\n【她】嗯")
        pa = self.db.conn.execute("SELECT * FROM people WHERE wx_key = ?", (a,)).fetchone()
        pb = self.db.conn.execute("SELECT * FROM people WHERE wx_key = ?", (b,)).fetchone()
        self.assertEqual(relations._transcript(self.db, "小林", a), self.BODY)
        self.assertEqual(relations._transcript(self.db, "小林", b), other)  # 同名的另一个人、没指纹分不清的旧记录都不混进来
        self.assertNotEqual(pa["id"], pb["id"])

    def test_impossible_dates_dropped(self):
        relations.ingest_stats(self.db, {"people": [item()]})
        self.receive("小林", self.BODY)
        card = dict(self.CARD, dates=[{"date": "02-30", "what": "生日"}, {"date": "2026-02-30", "what": "纪念日"},
                                      {"date": "2026-11-11", "what": "答辩"}],
                    open_loops=[{"kind": "they_asked", "date": "2026-02-30", "text": "坏日期", "quote": "你圣诞回不回国？"},
                                {"kind": "they_asked", "date": "2026-11-03", "text": "问你圣诞回不回国", "quote": "你圣诞回不回国？"}])
        self.assertEqual(relations.organize(self.db, FakeRouter(card), TODAY)["done"], 1)
        p = self.person("小林")
        self.assertIsNone(p["birthday"])
        self.assertEqual([d["date"] for d in json.loads(p["dates"])], ["2026-11-11"])
        d = relations.due(self.db, TODAY)  # 不整个报错
        self.assertEqual([x["text"] for x in d["reply"]], ["问你圣诞回不回国"])
        relations.page(self.db, TODAY)
        # 以前已经存进去的坏日期也不让 due 报错
        self.db.conn.execute("UPDATE people SET dates = ?, open_loops = ? WHERE id = ?",
                             (json.dumps([{"date": "2026-02-30", "what": "x"}]),
                              json.dumps([{"kind": "they_asked", "date": "2026-02-30", "text": "y"}]), p["id"]))
        self.db.conn.commit()
        self.assertEqual(relations.due(self.db, TODAY)["reply"], [])

    def test_loops_done_not_truncated_and_reworded_stays_done(self):
        relations.ingest_stats(self.db, {"people": [item()]})
        pid = self.person("小林")["id"]
        self.receive("小林", self.BODY)
        relations.organize(self.db, FakeRouter(self.CARD), TODAY)
        relations.loop_done(self.db, pid, "问你圣诞回不回国")
        for i in range(51):
            relations.loop_done(self.db, pid, f"别的事 {i}")
        again = dict(self.CARD, open_loops=[{"kind": "they_asked", "date": "2026-11-03", "text": "问你 圣诞回不回国？", "quote": "你圣诞回不回国？"}])
        self.db.conn.execute("UPDATE people SET organized_at = NULL WHERE id = ?", (pid,))
        self.db.conn.commit()
        relations.organize(self.db, FakeRouter(again), TODAY)
        self.assertEqual(relations.due(self.db, TODAY)["reply"], [])
        reworded = dict(self.CARD, open_loops=[{"kind": "they_asked", "date": "2026-11-03", "text": "她问圣诞回国吗", "quote": "你圣诞回不回国？"}])
        self.db.conn.execute("UPDATE people SET organized_at = NULL WHERE id = ?", (pid,))
        self.db.conn.commit()
        relations.organize(self.db, FakeRouter(reworded), TODAY)
        self.assertEqual(relations.due(self.db, TODAY)["reply"], [])
        self.assertNotIn("圣诞", self.person("小林")["loops_done"])  # 只存编号，不存原文
