import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from ethan import achievements, lifelog, territory
from ethan.core import agent
from tests.test_workbench import MONDAY, make_demo


class LifeLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_markers_feed_health_lines_and_achievements(self):
        reply = ("好耶 [人物 Amy 见面 一起攀岩] [人物 Tom 新认识 某商学院 讲座上认识] [话题 信任为什么难建立] "
                 "[研究 130 读了两篇研究] [体脂 24.5] [作品 歌 1 写完第一首副歌]")
        self.assertEqual(lifelog.note(self.db, reply, MONDAY), 6)
        self.assertEqual(agent.parse_reply(reply)["bubbles"], ["好耶"])  # 标记不发给她
        rows = lambda k: {r["name"]: r for r in territory.health(self.db, k, MONDAY + timedelta(days=1), True)}
        self.assertEqual(rows("social")["和朋友见面"]["actual"], "1 次")
        self.assertEqual(rows("social")["认识新朋友"]["actual"], "本月 1 个")
        self.assertEqual(rows("study")["认知拓展"]["actual"], "1 天")
        self.assertEqual((rows("academic")["研究时间"]["actual"], rows("academic")["研究时间"]["light"]), ("2.2 小时", "ok"))
        a = {i["key"]: i for i in achievements.compute(self.db, self.root, MONDAY)["items"]}
        self.assertEqual(a["friends"]["value"], 1)  # 只算新认识的 Tom
        self.assertEqual(a["song"]["tier"], "铜")
        self.assertEqual(a["bodyfat"]["value"], 24.5)
        self.assertEqual(self.db.conn.execute("SELECT last_contact FROM people WHERE name = 'Amy'").fetchone()[0], MONDAY.isoformat())
        self.assertIn("[人物", agent.build_system_prompt("人设", self.store.load_context(today=MONDAY)))


if __name__ == "__main__":
    unittest.main()
