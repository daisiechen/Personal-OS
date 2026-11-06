"""记忆“这条现在还算不算数”统一成一处判断（ethan/lifebase.py 的 fact_state / mem_where / MEM_VIEWS，
docs/design/memory-alignment.md “算不算数”一节）。

决定 10：过了预计日期的临时状态，所有地方都不再当“现在”，“我”页标“到期了”。
"""

import re
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, memsearch, mepage, onto_query, ontology, portrait
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent / "ethan"
TODAY = date(2026, 11, 6)


class StateTableTest(unittest.TestCase):
    def test_facts(self):
        cases = [
            ({"status": "active"}, "current"),
            ({"status": "candidate"}, "candidate"),
            ({"status": "deleted"}, "forgotten"),
            ({"status": "invalid", "valid_to": "2026-11-01"}, "retracted"),
            ({"status": "suspended"}, "suspended"),
            ({"status": "active", "valid_to": "2026-11-01", "superseded_by": 9}, "past"),
            ({"status": "active", "until": "2026-11-05"}, "expired"),   # 决定 10
            ({"status": "active", "until": "2026-11-06"}, "current"),   # 当天还算
            ({"status": "suspended", "until": "2026-11-01"}, "suspended"),
        ]
        for row, want in cases:
            self.assertEqual(lifebase.fact_state(row, TODAY), want, row)

    def test_statements(self):
        base = {"property": "preference", "said_by": "her_words", "valid_to": None, "valid_from": None, "superseded_by": None}
        cases = [
            ({"review": "accepted"}, "current"),
            ({"review": "disputed"}, "disputed"),
            ({"review": "candidate"}, "candidate"),
            ({"review": "forgotten"}, "forgotten"),
            ({"review": "retracted"}, "retracted"),
            ({"review": "accepted", "valid_to": "2026-11-06"}, "past"),
            ({"review": "accepted", "valid_to": "2026-12-01"}, "current"),
            ({"review": "accepted", "valid_from": "2026-12-01"}, "future"),
            ({"review": "accepted", "superseded_by": 3}, "past"),
            ({"review": "accepted", "until": "2026-11-01"}, "expired"),
        ]
        for row, want in cases:
            self.assertEqual(lifebase.fact_state({**base, **row}, TODAY), want, row)

    def test_views_well_formed(self):
        for name, v in lifebase.MEM_VIEWS.items():
            self.assertTrue(v["states"] <= set(lifebase.MEM_STATES), name)
            self.assertTrue(v["rules"] and all(r in lifebase.MEM_RULES for r in v["rules"]), name)
        for name in ("now", "topic", "portrait", "fact_ops", "public", "count", "style"):  # 决定 10：当“现在”的地方都不收到期的
            self.assertNotIn("expired", lifebase.MEM_VIEWS[name]["states"], name)
        self.assertNotIn("suspended", lifebase.MEM_VIEWS["fact_ops"]["states"])
        self.assertEqual(set(lifebase.MEM_LABEL), set(lifebase.MEM_STATES))


class DbTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        ontology.ensure(self.db)
        self.ev = lifebase.add_evidence(self.db, "chat", "raw:1", "2026-11-01", "她说这周在生病，喜欢喝热水")
        self.sick = lifebase.add_fact(self.db, "body", "她这周在生病", [self.ev], until="2026-11-03", source_kind="her_words")
        self.water = lifebase.add_fact(self.db, "preferences", "她喜欢喝热水", [self.ev], source_kind="her_words")
        self.cand = lifebase.add_fact(self.db, "preferences", "她喜欢喝冰水", [self.ev], source_kind="her_words")
        self.db.conn.execute("UPDATE facts SET status = 'candidate' WHERE id = ?", (self.cand,))
        self.gone = lifebase.add_fact(self.db, "preferences", "她喜欢喝咖啡", [self.ev], source_kind="her_words")
        self.db.conn.execute("UPDATE facts SET status = 'suspended' WHERE id = ?", (self.gone,))
        self.db.conn.commit()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sql_and_python_agree(self):
        """mem_where（SQL）和 fact_state（Python）是同一份判断：每个视图、两张表都对得上。"""
        for table in ("facts", "statements"):
            rows = [dict(r) for r in self.db.conn.execute(f"SELECT * FROM {table}")]
            self.assertTrue(rows)
            for view in lifebase.MEM_VIEWS:
                cond, args = lifebase.mem_where(view, table, TODAY)
                got = {r[0] for r in self.db.conn.execute(f"SELECT id FROM {table} WHERE {cond}", args)}
                want = {r["id"] for r in rows if lifebase.mem_ok(view, r, TODAY, self.db)}
                self.assertEqual(got, want, (table, view))

    def test_expired_not_now_anywhere(self):
        """决定 10：到期的“这周在生病”画像、能力题、current_facts 都不当现在；检索能查到但标“到期了”；“我”页标“到期了”。"""
        self.assertNotIn(self.sick, [f["id"] for f in lifebase.current_facts(self.db, today=TODAY)])
        self.assertIn(self.sick, [f["id"] for f in lifebase.current_facts(self.db, include_expired=True, today=TODAY)])
        memsearch.refresh(self.db)
        body = self.db.conn.execute("SELECT body, valid FROM mem_docs WHERE key = ?", (f"fact:{self.sick}",)).fetchone()
        self.assertTrue(body["valid"] and "到期了" in body["body"])
        self.assertNotIn("生病", portrait.plain_block([f for f in lifebase.current_facts(self.db) if f["dimension"] == "body"]))
        got = onto_query.expired_states(self.db, "ethan", today=TODAY)
        self.assertIn("她这周在生病", [i["value"] for i in got["items"]])
        mine = {x["id"]: x for x in mepage.all_memory(self.db)["items"]}
        self.assertEqual((mine[self.sick]["state"], mine[self.sick]["state_label"]), ("expired", "到期了"))
        self.assertNotIn(self.gone, mine)  # 删后停用的不在“现在”
        self.assertNotIn(self.cand, mine)

    def test_fact_ops_skips_suspended(self):
        self.assertFalse(portrait._current(lifebase._fact_row(self.db, self.gone) if hasattr(lifebase, "_fact_row")
                                           else dict(self.db.conn.execute("SELECT * FROM facts WHERE id = ?", (self.gone,)).fetchone())))
        self.assertTrue(portrait._current(dict(self.db.conn.execute("SELECT * FROM facts WHERE id = ?", (self.cand,)).fetchone())))


# ---------- 守卫 ----------
# 新结构的表只许这几个模块直接碰：别的要用记忆走 memsearch.search / portrait.chat_text / onto_query
NEW_TABLES = re.compile(r"\b(FROM|JOIN|UPDATE|INTO)\s+(statements|statement_sources|statement_changes|evidence|fact_map)\b")
NEW_OWNERS = {"lifebase.py": "写入层、双写触发器、删除和权限把关、“算不算数”的唯一判断",
              "ontology.py": "新结构的建表和对象、说法的写入",
              "onto_query.py": "能力题的唯一查询入口（按 lifebase.MEM_VIEWS 取）"}
# 不是“算不算数”的读：每条写原因（只减不增，新加的要么改走入口，要么在这里写原因）
NEW_ALLOWED = {
    "diary.py": (1, "日记补写时按来源编号找回原始记录（证据）挂上日记，不判断有效性"),
    "formaleval.py": (4, "正式评估：取最小编号当参数默认值、看题目依据的可见级别、抽题时连上旧编号对照（抽题的“还算数”已改用 lifebase.mem_where）"),
    "interviews.py": (3, "整理流水线：从原始记录（证据）里挑还没整理的对话和文件、数待整理的条数"),
    "memsearch.py": (2, "统一检索入口：索引从证据增量建、转述来源的过滤"),
    "mepage.py": (3, "“我”页：转述来源的证据编号、按出处找回原文来源（不判断有效性）"),
    "relations.py": (3, "人物卡整理从访谈原始记录里挑对话（证据），不读说法；恢复删掉的人时看卡上引用的原始记录还在不在（她忘掉的不跟着回来）"),
    "style.py": (1, "写作规范：按旧 facts 编号找到对应说法编号（对照表），状态仍由 onto_query.writing_style 判"),
    "syscheck.py": (5, "巡检只数原始记录的条数和最近一份的时间"),
    "upgrades.py": (1, "升级记录按某一刻回看说法条数（时间点快照，不是“现在算不算数”）"),
}
# 自己判断“算不算数”的写法：只许在 lifebase.py（规则表）里出现
VALIDITY = re.compile(r"valid_to IS NULL|\breview\s*(?:IN|NOT IN|=|!=)\s*\(?'|lifebase\.expired\(|lifebase\.GONE\b|NOT_GONE_STATEMENT|"
                      r"statement_gone\(|status\s*NOT IN\s*\('(?:invalid|deleted|suspended)|status\s*IN\s*\('(?:suspended|deleted|invalid)|"
                      r"status\s*!=\s*'invalid'")
VALIDITY_ALLOWED = {
    "ontology.py": (1, "建表语句里 review 的 CHECK 约束，不是判断"),
    "retire.py": (1, "旧表下线清单里的文字说明"),
    "upgrades.py": (1, "升级记录按某一刻回看说法条数（时间点快照：那一刻已确认、还没撤回的），和“现在算不算数”不是一回事"),
}


def _scan(pattern):
    out = {}
    for p in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        n = len(pattern.findall(p.read_text(encoding="utf-8")))
        if n:
            out[str(p.relative_to(ROOT))] = n
    return out


class GuardTest(unittest.TestCase):
    def test_new_tables_only_owners(self):
        got = {f: n for f, n in _scan(NEW_TABLES).items() if f not in NEW_OWNERS}
        over = {f: n for f, n in got.items() if f not in NEW_ALLOWED or n > NEW_ALLOWED[f][0]}
        self.assertEqual(over, {}, "这些模块直接读写说法 / 证据表：改走 onto_query / memsearch，或在白名单写原因")
        stale = {f: (got.get(f, 0), a[0]) for f, a in NEW_ALLOWED.items() if got.get(f, 0) != a[0]}
        self.assertEqual(stale, {}, "白名单的数只减不增：改完把数改小")

    def test_validity_only_in_lifebase(self):
        got = {f: n for f, n in _scan(VALIDITY).items() if f != "lifebase.py"}
        over = {f: n for f, n in got.items() if f not in VALIDITY_ALLOWED or n > VALIDITY_ALLOWED[f][0]}
        self.assertEqual(over, {}, "这些模块自己判断记忆算不算数：改用 lifebase.fact_state / mem_where / MEM_VIEWS")
        stale = {f: (got.get(f, 0), a[0]) for f, a in VALIDITY_ALLOWED.items() if got.get(f, 0) != a[0]}
        self.assertEqual(stale, {})
        self.assertTrue(all(a[1] for a in {**NEW_ALLOWED, **VALIDITY_ALLOWED}.values()), "白名单每条都要写原因")


if __name__ == "__main__":
    unittest.main()
