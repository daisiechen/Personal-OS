"""对外开关（ontology-plan 3.3，她 2026-11-04 认可）：三档受众 × 类别（可以说 / 问我 / 不说），逐条放行和挡住是例外，
两道保险（别人说的、系统推的、转述的不对外；删掉的、她说不对的不对外），“问我”在待你定里生成一条。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, memsearch, memusers, mepage, onto_query, portrait
from ethan.memory import MemoryStore

DAY = date(2026, 11, 4)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        lifebase.ensure(self.db)
        mepage.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-01:1", "2026-11-01T09:00:00", "mia：随便聊聊")
        self.mail = lifebase.add_evidence(self.db, "email", "mail:1", "2026-11-01T09:00:00", "导师来信")

    def tearDown(self):
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def fact(self, dim, text, prop=None, ev=None, **kw):
        return lifebase.add_fact(self.db, dim, text, [ev or self.ev], prop=prop, **kw)

    def row(self, fid):
        return self.db.conn.execute("SELECT * FROM facts WHERE id = ?", (fid,)).fetchone()

    def say(self, user, fid):
        return lifebase.disclose(self.db, user, self.row(fid))


class PolicyTest(Base):
    def test_every_user_has_a_tier_and_outside_tiers_are_level_public(self):
        for uid, u in memusers.USERS.items():
            self.assertIn(u.get("tier"), lifebase.TIERS, f"{uid} 没登记 tier")
            if u["tier"] in lifebase.EXTERNAL_TIERS:
                self.assertEqual(u["level"], "public", f"{uid} 对外却不是 public 级别")
        self.assertEqual((memusers.tier_of("twin"), memusers.tier_of("site"), memusers.tier_of("planner")), ("public", "public", "internal"))

    def test_defaults(self):
        pub = {r["key"]: r["mode"] for r in lifebase.policy(self.db)[0]["rows"]}
        par = {r["key"]: r["mode"] for r in lifebase.policy(self.db)[1]["rows"]}
        for k in ("career", "education", "skills", "projects", "hobbies", "views"):
            self.assertEqual((pub[k], par[k]), ("allow", "allow"), k)
        self.assertEqual((pub["whereabouts"], pub["recent"]), ("ask", "ask"))
        for k in ("rules", "prefs", "rhythms", "body", "money", "family", "temperament", "address"):
            self.assertEqual(pub[k], "deny", k)
        self.assertEqual((par["recent"], par["rules"], par["prefs"], par["whereabouts"]), ("allow", "allow", "allow", "ask"))
        for k in ("body", "money", "family", "temperament", "address"):
            self.assertEqual(par[k], "deny", k)
        self.assertEqual(len(lifebase.DISCLOSE_CATEGORIES), len(pub))

    def test_three_tiers_judged_by_switches(self):
        school = self.fact("abilities", "在 某商学院 读 进修", prop="studied_at")
        pref = self.fact("preferences", "开会喜欢排在下午")
        body = self.fact("body", "每周练五次力量举")
        self.assertEqual([self.say("twin", f) for f in (school, pref, body)], ["allow", "deny", "deny"])
        self.assertEqual([self.say("planner", f) for f in (school, pref, body)], ["allow"] * 3)  # 自己人照内部规则
        memusers.USERS["agent_x"] = {"tier": "partner", "name": "某合作 agent", "level": "public", "uses": "测试", "where": "测试",
                                     "since": "2026-11-04", "questions": []}
        try:  # 新使用者只登记 tier 就自动按那一档
            self.assertEqual([self.say("agent_x", f) for f in (school, pref, body)], ["allow", "allow", "deny"])
            self.assertIn("agent_x", lifebase.external_users())
            lifebase.set_policy(self.db, "partner", "body", "allow")  # 她改一格，这一档的都跟着变
            self.assertEqual(self.say("agent_x", body), "allow")
            self.assertEqual(self.say("twin", body), "deny")  # 别的档不动
            row = self.db.conn.execute("SELECT * FROM disclosure_policy WHERE tier = 'partner' AND category = 'body'").fetchone()
            self.assertEqual((row["mode"], row["by"]), ("allow", "mia"))  # 她改过的有记录
            self.assertTrue(next(r for r in lifebase.policy(self.db)[1]["rows"] if r["key"] == "body")["changed"])
        finally:
            del memusers.USERS["agent_x"]

    def test_address_locked(self):
        with self.assertRaises(ValueError):
            lifebase.set_policy(self.db, "partner", "address", "allow")
        with self.assertRaises(ValueError):
            lifebase.set_policy(self.db, "internal", "career", "allow")
        lifebase.set_policy(self.db, "public", "career", "allow")
        f = self.fact("abilities", "在 12 Baker Street 的公司上班", prop="worked_at")  # 原文像住址：不管归哪类都锁死
        self.assertEqual(self.say("twin", f), "deny")
        lifebase.approve_disclosure(self.db, "在 12 Baker Street 的公司上班", "可以说", fact_id=f)
        self.assertEqual(self.say("twin", f), "deny")  # 逐条放行也打不开
        self.assertTrue(next(r for r in lifebase.policy(self.db)[0]["rows"] if r["key"] == "address")["locked"])
        got = onto_query.public_sensitive(self.db, "twin", "她电话多少")
        self.assertEqual(got["items"][0]["reply"], onto_query.REFUSE)

    def test_exceptions_allow_and_deny_one_sentence(self):
        body = self.fact("body", "在练力量举")
        lifebase.approve_disclosure(self.db, "在练力量举", "这句可以说", dimension="body", fact_id=body)
        self.assertEqual(self.say("twin", body), "allow")  # 某类不说，这一句放行
        other = self.fact("body", "膝盖有旧伤")
        self.assertEqual(self.say("twin", other), "deny")  # 同类别的照样不说
        proj = self.fact("abilities", "在做一个 AI 产品", prop="project")
        self.assertEqual(self.say("twin", proj), "allow")
        d = lifebase.deny_disclosure(self.db, "", "这个先别说", fact_id=proj)
        self.assertEqual(self.say("twin", proj), "deny")  # 某类可以说，这一句挡住
        memsearch.refresh(self.db)
        self.assertEqual(memsearch.search(self.db, ["AI 产品"], "public", user="twin", log=False), [])
        self.assertNotIn(d, [x["id"] for x in lifebase.disclosures(self.db, "twin")])  # 挡住的不是能说的话
        lifebase.revoke_disclosure(self.db, d)
        self.assertEqual(self.say("twin", proj), "allow")

    def test_two_safeguards(self):
        lifebase.set_policy(self.db, "public", "prefs", "allow")
        mine = self.fact("preferences", "喜欢靠窗的位子")
        heard = self.fact("abilities", "导师说她写作很强", prop="skill", ev=self.mail)  # 别人说的
        guess = self.fact("preferences", "可能喜欢安静的地方", kind="hypothesis", source_kind="system_inference")
        relay = self.fact("abilities", "Grok 说她会剪视频", prop="skill", source_kind="agent_relay", kind="hypothesis")
        self.assertEqual([self.say("twin", f) for f in (mine, heard, guess, relay)], ["allow", "deny", "deny", "deny"])
        lifebase.invalidate(self.db, mine, force=True)  # 她说过“不对”的
        self.assertEqual(self.say("twin", mine), "deny")
        gone = self.fact("abilities", "会写 Python", prop="skill")
        lifebase.forget(self.db, fact_ids=[gone], root=self.root)  # 删掉的
        self.assertEqual(self.say("twin", gone), "deny")
        cand = self.fact("abilities", "会一点日语", prop="skill", status="candidate")  # 还没确认的：最多问她
        self.assertEqual(self.say("twin", cand), "ask")

    def test_statements_rows_go_through_same_gate(self):
        f = self.fact("abilities", "在 某商学院 读 进修", prop="studied_at")
        sid = self.db.conn.execute("SELECT statement_id FROM fact_map WHERE fact_id = ?", (f,)).fetchone()[0]
        st = self.db.conn.execute("SELECT * FROM statements WHERE id = ?", (sid,)).fetchone()
        self.assertEqual(lifebase.disclose(self.db, "twin", st), "allow")
        lifebase.deny_disclosure(self.db, "", "别说", statement_id=sid)
        self.assertEqual(lifebase.disclose(self.db, "twin", st), "deny")
        self.assertEqual(self.say("twin", f), "deny")  # 按说法挡住，对应的旧条目也挡住


class OutsideReadTest(Base):
    def test_portrait_for_outside_is_switch_filtered(self):
        self.fact("abilities", "在 某商学院 读 进修", prop="studied_at")
        self.fact("body", "每周练五次")
        text = portrait.chat_text(self.db, "twin")
        self.assertIn("在 某商学院 读 进修", text)
        self.assertNotIn("每周练五次", text)

    def test_search_collects_ask_categories(self):
        self.fact("resources", "现在住在伦敦", prop="lives_in")
        memsearch.refresh(self.db)
        asked = []
        self.assertEqual(memsearch.search(self.db, ["伦敦"], "public", user="twin", log=False, asked=asked), [])
        self.assertEqual([a["category"] for a in asked], ["whereabouts"])
        lifebase.set_policy(self.db, "public", "whereabouts", "allow")
        self.assertEqual(len(memsearch.search(self.db, ["伦敦"], "public", user="twin", log=False)), 1)

    def test_q14_switches_and_ask_creates_pending(self):
        self.fact("abilities", "在 某商学院 读 进修", prop="studied_at")
        city = self.fact("resources", "现在住在伦敦", prop="lives_in")
        got = onto_query.public_say(self.db, "twin", "她在哪读书")
        self.assertEqual((got["status"], got["items"][0]["value"]), ("answer", "在 某商学院 读 进修"))
        ans = onto_query.public_answer(self.db, "twin", "她现在在哪个城市", recipient="Leo")
        self.assertEqual((ans["status"], ans["reply"]), ("ask", onto_query.ASK_REPLY))
        self.assertTrue(ans["pending"])
        item = next(p for p in mepage.pending_items(self.db) if p["id"] == ans["pending"])
        self.assertEqual((item["kind"], item["options"][0]["text"], item["options"][0]["category"]), ("disclose", "现在住在伦敦", "whereabouts"))
        self.assertIn("Leo", item["question"])  # 谁问的、问了什么
        self.assertEqual(onto_query.public_answer(self.db, "twin", "她现在在哪个城市", recipient="Leo")["pending"], 0)  # 不重复建
        self.assertTrue(mepage.resolve(self.db, ans["pending"], "yes")["ok"])  # 她点“可以说”：这次对 Leo 放行一次
        self.assertEqual(lifebase.disclose(self.db, "twin", self.row(city), recipient="Leo"), "allow")
        self.assertEqual(lifebase.disclose(self.db, "twin", self.row(city), recipient="Mia"), "ask")
        again = onto_query.public_answer(self.db, "twin", "她在哪个城市", recipient="Mia")
        mepage.resolve(self.db, again["pending"], "always")  # “以后都可以”：这一类改成可以说
        self.assertEqual(lifebase.policy_mode(self.db, "public", "whereabouts"), "allow")
        self.assertEqual(onto_query.public_say(self.db, "twin", "她在哪个城市")["items"][0]["value"], "现在住在伦敦")

    def test_q15_follows_switch_but_exceptions_still_work(self):
        self.fact("body", "在练力量举")
        self.assertEqual(onto_query.public_sensitive(self.db, "twin", "她身体怎么样")["items"][0]["reply"], onto_query.REFUSE)
        lifebase.set_policy(self.db, "public", "body", "ask")
        ans = onto_query.public_answer(self.db, "twin", "她身体怎么样")
        self.assertEqual((ans["status"], ans["reply"]), ("ask", onto_query.ASK_REPLY))
        lifebase.set_policy(self.db, "public", "body", "allow")
        self.assertEqual(onto_query.public_sensitive(self.db, "twin", "她身体怎么样")["items"][0]["reply"], "在练力量举")
        self.assertEqual(onto_query.public_sensitive(self.db, "twin", "她住哪、身体怎么样")["items"][0]["reply"], onto_query.REFUSE)

    def test_q16_forward_goes_to_pending(self):
        ans = onto_query.public_answer(self.db, "twin", "她接不接品牌合作")
        self.assertTrue(ans["items"][0]["forward"])
        item = next(p for p in mepage.pending_items(self.db) if p["id"] == ans["pending"])
        self.assertEqual(item["kind"], "disclose")
        out = mepage.resolve(self.db, ans["pending"], "own", text="她目前不接商务合作")
        self.assertTrue(out["ok"])
        self.assertEqual(onto_query.public_offer(self.db, "twin")["items"][0]["reply"], "她目前不接商务合作")

    def test_manage_shows_switches(self):
        g = mepage.manage(self.db)
        self.assertEqual([t["tier"] for t in g["policy"]], ["public", "partner"])
        self.assertTrue(mepage.set_policy(self.db, "public", "hobbies", "deny")["ok"])
        self.assertFalse(mepage.set_policy(self.db, "public", "address", "allow")["ok"])


if __name__ == "__main__":
    unittest.main()
