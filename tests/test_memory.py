import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ethan.memory import MemoryStore


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class GoalTest(MemoryTestCase):
    def test_goal_tree_and_progress_log(self):
        db = self.store.db
        vision = db.add_goal("这辈子干一件大事", "vision")
        stage = db.add_goal("确定进修目标学校和教授", "stage", parent_id=vision, status="active")
        db.update_goal(stage, note="列出 5 位候选教授", source="telegram", next_step="读 某教授 的论文")
        goal = db.get_goal(stage)
        self.assertEqual(goal["next_step"], "读 某教授 的论文")
        self.assertEqual(goal["log"][0]["note"], "列出 5 位候选教授")
        self.assertEqual(db.get_goal(vision)["children"][0]["id"], stage)
        self.assertEqual([g["id"] for g in db.active_goals()], [stage])

    def test_invalid_status_rejected(self):
        with self.assertRaises(Exception):
            self.store.db.add_goal("x", "stage", status="whatever")


class TaskTest(MemoryTestCase):
    def test_due_tasks_and_completion(self):
        db = self.store.db
        promise = db.add_task("帮她查伦敦的 K-pop 舞房", due="2026-10-22", promised_by_ethan=True)
        db.add_task("下周的事", due="2026-10-30")
        due = db.due_tasks(date(2026, 10, 22))
        self.assertEqual([t["id"] for t in due], [promise])
        self.assertEqual(due[0]["promised_by_ethan"], 1)
        db.complete_task(promise)
        self.assertEqual(db.due_tasks(date(2026, 10, 22)), [])


class LearningTest(MemoryTestCase):
    def test_spaced_repetition(self):
        db = self.store.db
        today = date(2026, 10, 22)
        item = db.add_learning_item("english", "right up my alley", today=today)
        self.assertEqual(db.due_learning_items(today), [])
        tomorrow = date(2026, 10, 23)
        self.assertEqual(db.due_learning_items(tomorrow)[0]["id"], item)
        db.review_learning_item(item, remembered=True, today=tomorrow)
        self.assertEqual(db.due_learning_items(tomorrow), [])
        row = db.conn.execute("SELECT interval_days, next_review FROM learning_items").fetchone()
        self.assertGreaterEqual(row["interval_days"], 2)
        db.review_learning_item(item, remembered=False, today=tomorrow)
        row = db.conn.execute("SELECT interval_days FROM learning_items").fetchone()
        self.assertEqual(row["interval_days"], 1)


class PeopleTest(MemoryTestCase):
    def test_contact_reminders(self):
        db = self.store.db
        db.upsert_person("小雨", circle="挚友", birthday="10-25",
                         contact_every_days=14, last_contact="2026-10-01")
        db.upsert_person("同学A", contact_every_days=30, last_contact="2026-10-20")
        due = db.people_to_contact(date(2026, 10, 22))
        self.assertEqual(due, [{"name": "小雨", "reasons": ["long_no_contact", "birthday_soon"]}])

    def test_upsert_updates_and_delete(self):
        db = self.store.db
        first = db.upsert_person("妹妹", circle="家人")
        second = db.upsert_person("妹妹", notes="彼此惦记")
        self.assertEqual(first, second)
        db.delete_person("妹妹")
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM people").fetchone()[0], 0)

    def test_birthday_across_new_year(self):
        db = self.store.db
        db.upsert_person("新年宝宝", birthday="01-02")
        self.assertEqual(db.people_to_contact(date(2026, 12, 30))[0]["reasons"], ["birthday_soon"])


class WishTest(MemoryTestCase):
    def test_wish_lifecycle(self):
        db = self.store.db
        wish = db.add_wish("好想去苏格兰看极光", context="聊旅行时随口说的")
        self.assertEqual(db.open_wishes()[0]["quote"], "好想去苏格兰看极光")
        db.close_wish(wish)
        self.assertEqual(db.open_wishes(), [])


class WikiTest(MemoryTestCase):
    def test_write_read_links_and_index(self):
        wiki = self.store.wiki
        wiki.write("about-mia", "关于 Mia", "喜欢绿色，见 [[relationship]]。", sources=["2026-10-22 口述"])
        wiki.write("knowledge/marketing", "营销案例", "品牌溢价与信任信号")
        page = wiki.read("about-mia")
        self.assertEqual(page["title"], "关于 Mia")
        self.assertEqual(page["links"], ["relationship"])
        self.assertIn("2026-10-22 口述", page["sources"])
        self.assertEqual(wiki.pages(), ["about-mia", "knowledge/marketing"])
        index = (wiki.root / "index.md").read_text(encoding="utf-8")
        self.assertIn("[[knowledge/marketing]] 营销案例", index)
        self.assertEqual(wiki.search("信任"), ["knowledge/marketing"])
        wiki.delete("knowledge/marketing")
        self.assertEqual(wiki.pages(), ["about-mia"])

    def test_rejects_path_escape(self):
        with self.assertRaises(ValueError):
            self.store.wiki.write("../secrets", "x", "y")


class RawAndContextTest(MemoryTestCase):
    def test_raw_log_is_append_only_by_day(self):
        raw = self.store.raw
        day1 = datetime(2026, 10, 21, 23, 0, tzinfo=timezone.utc)
        day2 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
        raw.append("telegram", "mia", "晚安", at=day1)
        raw.append("telegram", "ethan", "早呀", at=day2)
        raw.append("telegram", "mia", "早", at=day2)
        self.assertEqual(len(raw.read_day("2026-10-22")), 2)
        self.assertEqual([r["text"] for r in raw.recent(2)], ["早呀", "早"])
        self.assertEqual(len(raw.recent(10)), 3)

    def test_load_context(self):
        store = self.store
        store.wiki.write("about-mia", "关于 Mia", "处女座，INFJ")
        store.db.add_goal("创业项目跑出订单", "stage", status="active")
        store.raw.append("telegram", "mia", "在吗")
        ctx = store.load_context(today=date(2026, 10, 22))
        self.assertEqual(ctx["about_mia"], "处女座，INFJ")
        self.assertEqual(ctx["active_goals"][0]["title"], "创业项目跑出订单")
        self.assertEqual(ctx["recent_messages"][0]["text"], "在吗")
        self.assertIn("about-mia", ctx["wiki_pages"])


if __name__ == "__main__":
    unittest.main()
