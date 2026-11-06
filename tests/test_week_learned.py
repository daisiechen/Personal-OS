"""每周复盘“这周系统新认识了你什么”（ontology-plan 3.2）：数字和“我”页 metrics 一致、空周、有纠正的周、能力题答不上 → 答得上。"""

import tempfile
import unittest
from datetime import date
from pathlib import Path

from ethan import lifebase, memusers, mepage, onto_query, weekreview
from ethan.core.router import Settings
from ethan.memory import MemoryStore

DAY = date(2026, 11, 8)  # 周日


class WeekLearnedTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root / "data")
        self.db = self.store.db
        lifebase.ensure(self.db)
        mepage.ensure(self.db)
        memusers.ensure(self.db)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def add(self, dim, text, sk="her_words", at="2026-11-06T10:00:00+00:00", importance=3, kind="fact"):
        ev = lifebase.add_evidence(self.db, "chat", f"raw:{text}", at, f"mia：{text}")
        fid = lifebase.add_fact(self.db, dim, text, [ev], source_kind=sk, importance=importance, kind=kind)
        self.db.conn.execute("UPDATE facts SET recorded_at = ? WHERE id = ?", (at, fid))
        self.db.conn.commit()
        return fid

    def test_empty_week(self):
        w = mepage.week_learned(self.db, DAY)
        self.assertEqual((w["new"]["n"], w["corrected"]["n"]), (0, 0))
        self.assertFalse(w["questions"]["compared"])
        text = mepage.learned_text(w)
        self.assertIn("新记下的说法 0 条", text)
        self.assertIn("这周没有被纠正", text)
        self.assertIn("还没有对比数据", text)

    def test_new_by_source_and_dim_and_matches_metrics(self):
        self.add("preferences", "喜欢靠窗的座位", importance=5)
        self.add("preferences", "不吃香菜")
        self.add("relationships", "小王说她最近很忙", sk="other_person")
        self.add("rhythms", "这周三天 1 点后睡", sk="behavior")
        self.add("values", "可能看重自由", sk="system_inference", kind="hypothesis")
        self.add("body", "膝盖受过伤", at="2026-10-20T10:00:00+00:00")  # 上上周记的：不算这周新的
        w = mepage.week_learned(self.db, DAY)
        self.assertEqual(w["new"]["n"], 5)
        self.assertEqual(w["new"]["by_source"], {"hers": 2, "others": 1, "behavior": 1, "system": 1})
        self.assertEqual(w["new"]["by_dim"][lifebase.DIM_NAMES["preferences"]], 2)
        self.assertEqual(w["new"]["top"][0]["text"], "喜欢靠窗的座位")  # 她说的、重要的在前
        self.assertLessEqual(len(w["new"]["top"]), 5)
        m = mepage.metrics(self.db, DAY)
        self.assertEqual(m["learned"], w)  # “我”页和复盘同一套数
        self.assertLessEqual(w["new"]["by_source"]["hers"], m["said"]["hers"])

    def test_corrections_week(self):
        self.add("resources", "住在 Camden")
        memusers.open_ticket(self.db, "住在 Greenwich", "住在 Camden", quote="不对，我住 Greenwich")
        memusers.open_ticket(self.db, "现在在伦敦读书", "在上海工作", kind="changed")
        self.db.conn.execute("UPDATE corrections SET at = '2026-11-07T10:00:00+00:00'")
        self.db.conn.commit()
        w = mepage.week_learned(self.db, DAY)
        c = w["corrected"]
        self.assertEqual((c["n"], c["wrong"], c["changed"]), (2, 1, 1))
        self.assertTrue(any(e["kind"] == "变了" for e in c["examples"]))
        m = mepage.metrics(self.db, DAY)
        self.assertEqual(sum(m["layers"].values()), c["n"])  # 和“我”页这周纠错按层的数一致
        self.assertEqual(m["layers"].get("changed", 0), c["changed"])
        self.assertIn("这周被纠正 2 次：记错 1、变了 1", mepage.learned_text(w))

    def test_question_flips_from_missed_to_answered(self):
        onto_query.preferences(self.db, "ethan", today=DAY)  # 上周：答不上
        self.db.conn.execute("UPDATE function_log SET at = '2026-10-28T10:00:00+00:00'")
        self.add("preferences", "喜欢靠窗的座位")
        onto_query.preferences(self.db, "ethan", today=DAY)  # 这周：答得上
        self.db.conn.execute("UPDATE function_log SET at = '2026-11-06T10:00:00+00:00' WHERE id = (SELECT MAX(id) FROM function_log)")
        self.db.conn.commit()
        q = mepage.week_learned(self.db, DAY)["questions"]
        self.assertTrue(q["compared"])
        self.assertEqual(q["now_answered"], [4])
        self.assertIn("从答不上变成答得上：第 4 题", mepage.learned_text(mepage.week_learned(self.db, DAY)))

    def test_sunday_opener_has_the_section(self):
        self.add("preferences", "喜欢靠窗的座位")
        p = weekreview.opener_prompt("人设", self.store, Settings(self.root / "settings.json"), DAY, ["week"])
        self.assertIn("这周系统新认识了你什么", p)
        self.assertIn("喜欢靠窗的座位", p)
        self.assertIn(mepage.learned_text(mepage.week_learned(self.db, DAY)).split("\n")[0], p)


if __name__ == "__main__":
    unittest.main()
