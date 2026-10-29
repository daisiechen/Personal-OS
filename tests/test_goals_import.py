import json
import tempfile
import unittest
from pathlib import Path

from ethan import goals_import, quests
from ethan.memory import MemoryStore

EXAMPLE = {"season_end": "2026-12-31",
           "bosses": [{"title": "减脂 4 公斤", "territory": "健康", "main": True,
                       "measure": {"kind": "count_week", "target": 3, "unit": "次"}},
                      {"title": "做出第一个小产品", "territory": "事业"}],
           "habits": [{"title": "读书 20 分钟", "cadence": "daily"}],
           "todos": ["约体检"], "for_ai": ["比较附近的健身房"], "later": ["明年考证"]}


class GoalsImportTest(unittest.TestCase):
    def test_table_becomes_season_and_rerun_does_not_duplicate(self):
        """开源准备（2026-10-29）：用户给一张目标表格，agent 整理成 JSON，一条命令冷启动。"""
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            goals_import.apply(store, EXAMPLE)
            goals_import.apply(store, EXAMPLE)
            db = store.db
            rows = db.conn.execute("SELECT title, status, domain FROM goals WHERE source = '目标表格导入' AND status = 'active' ORDER BY sort").fetchall()
            self.assertEqual([(r["title"], r["status"]) for r in rows][:2], [("减脂 4 公斤", "active"), ("做出第一个小产品", "active")])
            self.assertEqual(rows[0]["domain"], "身体")  # 健康区域在库里记“身体”
            self.assertIn("明年考证", [r["title"] for r in db.conn.execute("SELECT title FROM goals WHERE status = 'paused'")])
            self.assertTrue(quests.habit_by_title(db, "读书 20 分钟"))
            self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
            store.close()
