import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ethan import lifebase, memsearch, memusers, portrait
from ethan.core.agent import parse_reply
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent / "ethan"
# 只有这几个模块能直接碰记忆表；别的地方要用记忆，走 memsearch.search / portrait.chat_text 并报上登记过的名字
TABLE_OWNERS = {"lifebase.py", "memsearch.py", "portrait.py", "extract.py", "memusers.py", "mepage.py"}
MEMORY_TABLES = re.compile(r"\b(FROM|JOIN|UPDATE|INTO)\s+(facts|profile_blocks|mem_docs|mem_fts)\b")
CALL = re.compile(r"(memsearch\.search|portrait\.chat_text|\bsearch)\(([^()]*(?:\([^()]*\)[^()]*)*)\)", re.S)


class GateTest(unittest.TestCase):
    """新进来的使用者必须先登记（她 2026-10-31）：绕开入口或用没登记的名字，这里不过、上线自动退回。"""

    def files(self):
        return [p for p in ROOT.rglob("*.py") if "__pycache__" not in p.parts]

    def test_only_owners_touch_memory_tables(self):
        bad = [f"{p.relative_to(ROOT)}" for p in self.files() if p.name not in TABLE_OWNERS and MEMORY_TABLES.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "这些文件直接读写记忆表，改成走 memsearch.search / portrait.chat_text")

    def test_every_memory_call_names_a_registered_user(self):
        bad = []
        for p in self.files():
            text = p.read_text(encoding="utf-8")
            for m in re.finditer(r"(memsearch\.search|portrait\.chat_text)\(", text):
                if text[m.start() - 4:m.start()] == "def ":
                    continue
                depth, i = 1, m.end()
                while depth and i < len(text):
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    i += 1
                args = text[m.end():i - 1]
                who = re.search(r"user=\"(\w+)\"", args) or (m.group(1) == "portrait.chat_text" and re.search(r",\s*\"(\w+)\"\s*$", args))
                if not who or who.group(1) not in memusers.USERS:
                    bad.append(f"{p.relative_to(ROOT)}: {m.group(1)}({args[:60]})")
        self.assertEqual(bad, [])

    def test_unregistered_user_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            with self.assertRaises(ValueError):
                memsearch.search(store.db, ["膝盖"], user="newcomer")
            with self.assertRaises(ValueError):
                portrait.chat_text(store.db, "newcomer")
            store.close()


class MemUsersTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:1", "2026-10-30", "她说她住在 Camden")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_twin_cannot_see_private(self):
        lifebase.add_fact(self.db, "resources", "她住在 Camden 公寓", [self.ev], visibility="private")
        memsearch.refresh(self.db)
        self.assertTrue(memsearch.search(self.db, ["Camden"], user="ethan"))
        self.assertEqual(memsearch.search(self.db, ["Camden"], scene="chat", user="twin"), [])

    def test_correction_locks_the_right_fact_and_tracks(self):
        wrong = lifebase.add_fact(self.db, "resources", "她住在 Camden 公寓", [self.ev])
        memsearch.refresh(self.db)
        memsearch.search(self.db, ["Camden"], user="ethan")
        got = memusers.note(self.db, "对不起我记错了[纠错 她 资源与处境 错：她住在 Camden 对：她住在 Islington 公寓]", quote="我现在住 Islington 了")
        self.assertEqual(len(got), 1)
        t = got[0]
        self.assertEqual(t["layer"], "outdated")  # 原来记的是对的，后来她搬了
        f = self.db.conn.execute("SELECT * FROM facts WHERE id = ?", (t["fact_id"],)).fetchone()
        self.assertEqual((f["statement"], f["locked"], f["confidence"], f["dimension"]), ("她住在 Islington 公寓", 1, 1.0, "resources"))
        self.assertIsNotNone(self.db.conn.execute("SELECT valid_to FROM facts WHERE id = ?", (wrong,)).fetchone()[0])
        # 系统以后自己不能改它：冲突时变成问题
        newer = lifebase.add_fact(self.db, "resources", "她住在 Hackney", [self.ev])
        self.assertFalse(lifebase.supersede(self.db, t["fact_id"], newer))
        self.assertIsNone(self.db.conn.execute("SELECT valid_to FROM facts WHERE id = ?", (t["fact_id"],)).fetchone()[0])
        self.assertEqual(self.db.conn.execute("SELECT status FROM facts WHERE id = ?", (newer,)).fetchone()[0], "candidate")
        from ethan import mepage
        self.assertEqual([p["kind"] for p in mepage.pending_items(self.db)], ["conflict"])  # 两条让她选
        self.assertFalse(lifebase.invalidate(self.db, t["fact_id"]))
        q = [p for p in mepage.pending_items(self.db) if p["kind"] == "question"]
        self.assertIn("Islington", q[0]["question"])  # 可能不成立了：也进“待你定”，不另开一条线
        # 同一件事再被纠正：复发
        again = memusers.open_ticket(self.db, "她住在 Islington 公寓", "她住在 Hackney")
        self.assertEqual(again["recurrence_of"], t["id"])
        memsearch.refresh(self.db)
        memsearch.search(self.db, ["Islington"], user="ethan")
        track = {x["id"]: x for x in memusers.locked_tracking(self.db)}
        self.assertEqual((track[t["id"]]["recurred"], track[t["id"]]["used_after"] >= 1), (1, True))

    def test_layers_untold_and_not_found(self):
        self.assertEqual(memusers.open_ticket(self.db, "她对芒果过敏")["layer"], "untold")
        lifebase.add_fact(self.db, "body", "她对花生过敏", [self.ev])
        self.assertEqual(memusers.open_ticket(self.db, "她对花生过敏", "她什么都能吃")["layer"], "not_found")

    def test_ethan_self_correction_is_remembered(self):
        got = memusers.note(self.db, "[纠错 自己 错：这个月只喝了一杯咖啡 对：这周做过咖啡实验，买了好几杯]", wiki=self.store.wiki)
        self.assertEqual(got[0]["layer"], "self")
        self.assertIn("咖啡实验", self.store.wiki.read(memusers.SELF_PAGE)["body"])
        self.assertEqual(parse_reply("好吧我记混了[纠错 自己 错：a 对：b]")["bubbles"], ["好吧我记混了"])

    def test_stats_has_a_row_for_every_registered_user(self):
        lifebase.add_fact(self.db, "body", "她对花生过敏", [self.ev])
        memsearch.refresh(self.db)
        memsearch.search(self.db, ["花生"], user="ethan")
        memsearch.search(self.db, ["火星"], user="ethan")
        portrait.chat_text(self.db, "mail")
        memusers.open_ticket(self.db, "她对花生和芒果都过敏", "她只对花生过敏")
        rows = {r["user"]: r for r in memusers.stats(self.db)}
        self.assertEqual(set(rows), set(memusers.USERS))
        e = rows["ethan"]
        self.assertEqual((e["queries"], e["with_memory"], e["misses"], e["corrections"], e["accuracy"]), (2, 1, 1, 1, 0.5))
        self.assertTrue(rows["mail"]["connected"])
        self.assertFalse(rows["twin"]["connected"])
        with mock.patch.dict(memusers.USERS, {"newbie": {"name": "新来的", "level": "public", "uses": "x", "where": "y", "since": "z"}}):
            self.assertIn("newbie", {r["user"] for r in memusers.stats(self.db)})  # 新登记的自动多一行
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        page = Workbench(self.store, Settings(Path(self.tmp.name) / "s.json")).memory_page()
        self.assertEqual(len(page["users"]), len(memusers.USERS))
        self.assertEqual(page["fixes"][0]["right"], "她对花生和芒果都过敏")


if __name__ == "__main__":
    unittest.main()
