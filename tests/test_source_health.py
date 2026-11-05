"""来源健康（ontology-plan 1.10）：断一个来源体检报出来；不定期的、没接上的不报；按各自频率判断。"""
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import checkup, lifebase
from ethan.memory import MemoryStore

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


class SourceHealthTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        lifebase.ensure(self.db)
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS place_events (id INTEGER PRIMARY KEY, at TEXT NOT NULL, event TEXT NOT NULL, place TEXT)")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def health(self, code):
        return next(s for s in lifebase.source_health(self.db, NOW) if s["code"] == code)

    def test_stale_source_reported_and_fresh_not(self):
        self.db.conn.execute("INSERT INTO place_events (at, event) VALUES ('2026-10-28T08:00:00+00:00', 'leave')")
        self.assertEqual(self.health("S14")["state"], "stale")
        self.assertTrue(any("S14" in b for b in lifebase.stale_sources(self.db, NOW)))
        self.db.conn.execute("INSERT INTO place_events (at, event) VALUES ('2026-11-03T08:00:00+00:00', 'arrive')")
        self.assertEqual(self.health("S14")["state"], "ok")

    def test_never_seen_is_not_connected_not_stale(self):
        self.assertEqual(self.health("S15")["state"], "not_connected")
        self.assertEqual(lifebase.stale_sources(self.db, NOW), [])

    def test_irregular_source_never_stale(self):
        lifebase.add_evidence(self.db, "interview", "iv:1", "2026-09-01", text="问：？答：嗯")
        self.db.conn.execute("UPDATE evidence SET recorded_at = '2026-09-01T00:00:00+00:00'")
        self.assertEqual(self.health("S10")["state"], "ok")

    def test_monthly_source_one_day_late_not_reported(self):
        reg = (("SX", "月度账单", "SELECT MAX(at) FROM place_events", 35),)
        self.db.conn.execute("INSERT INTO place_events (at, event) VALUES ('2026-10-03T08:00:00+00:00', 'leave')")  # 32 天前
        self.assertEqual(lifebase.source_health(self.db, NOW, reg)[0]["state"], "ok")

    def test_facts_this_month_counted_per_source(self):
        ev = lifebase.add_evidence(self.db, "chat", "raw:2026-11-03:1", "2026-11-03T09:00:00+00:00", text="我喜欢靠窗")
        lifebase.add_fact(self.db, "preferences", "喜欢靠窗", [ev])
        self.db.conn.execute("UPDATE facts SET recorded_at = '2026-11-03T10:00:00+00:00'")
        s1 = self.health("S1")
        self.assertEqual((s1["facts_this_month"], s1["state"]), (1, "ok"))

    def test_checkup_lists_stale_source(self):
        self.db.conn.execute("INSERT INTO place_events (at, event) VALUES ('2026-10-20T08:00:00+00:00', 'leave')")
        got = checkup.problems(self.tmp.name, self.db, NOW, services=False)
        self.assertTrue(any(p["what"] == "数据来源" and "S14" in p["detail"] for p in got))


if __name__ == "__main__":
    unittest.main()
