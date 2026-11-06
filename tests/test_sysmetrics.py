import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ethan import prep, sysmetrics
from ethan.memory import MemoryStore


class SysMetricsTest(unittest.TestCase):
    def test_counts_this_week_vs_last(self):
        """偏系统的量化指标是开发会话的活：从聊天和后台记录自动算，本周和上周比。"""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            today = datetime.now(timezone.utc).date()
            store.raw.append("telegram", "mia", "我之前跟你说过的流水你又忘了")
            store.raw.append("telegram", "ethan", "抱歉，我这边没有那份记录")
            store.raw.append("telegram", "mia", "今天天气好")
            t = store.db.add_task("做餐单", promised_by_ethan=True)
            prep.ensure(store.db)
            store.db.conn.execute("INSERT INTO prep_work (task_id, state, result, updated_at) VALUES (?, 'ready', 'v1', ?)", (t, today.isoformat()))
            prep.revise(store.db, t, "少放辣")
            got = {m["key"]: m for m in sysmetrics.weekly(store, d, today)}
            self.assertEqual(got["forgot"]["this_week"], 1)
            self.assertEqual(got["blank"]["this_week"], 1)
            self.assertEqual(got["revise"]["this_week"], 1)
            self.assertIs(got["forgot"]["better"], False)  # 上周 0，本周 1：变差了
            self.assertIsNone(got["errors"]["better"])
            store.close()


if __name__ == "__main__":
    unittest.main()
