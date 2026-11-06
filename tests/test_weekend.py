"""每周五“这周末可以去”。
收藏优先、Grok 补本周新的、舞蹈单列；周五 12:00–20:30；推荐过的几周内不重复；登记在打扰登记表。"""

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ethan import brain, brain_enrich, nudge_registry, nudges, prep, weekend
from ethan.memory import MemoryStore

LDN = ZoneInfo("Europe/London")


def at(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=LDN)


FRI = at(2026, 11, 6, 13)


class WeekendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        brain.ensure(self.db)
        self.sent = []

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def grok(self, task, channel="", report_to=None):
        self.sent.append((task, channel, report_to))
        return True

    def save(self, title, tags=("地点",), place=None, kind="saved", extra=None):
        brain._put(self.db, kind, f"{kind}:{title}", title, tags=tags, status="kept",
                   extra={"place": place or {"name": title, "area": "Shoreditch"}, **(extra or {})})
        self.db.conn.commit()

    def grok_reply(self, fri, rows):
        inbox = self.root / "inbox"
        inbox.mkdir(exist_ok=True)
        text = f"【周末推荐 {fri}】\n" + "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
        with (inbox / "2026-11-05.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"path": "/grok/result", "at": "2026-11-05T18:00:00+00:00", "text": text}, ensure_ascii=False) + "\n")

    def test_friday_of(self):
        from datetime import date
        self.assertEqual(weekend.friday_of(date(2026, 11, 3)), date(2026, 11, 6))
        self.assertEqual(weekend.friday_of(date(2026, 11, 8)), date(2026, 11, 6))

    def test_ask_grok_thursday_once_with_dance(self):
        self.save("Dishoom 早午餐")
        self.assertFalse(weekend.ask(self.db, self.grok, at(2026, 11, 4, 12)))  # 周三不派
        self.assertFalse(weekend.ask(self.db, None, at(2026, 11, 5, 12)))  # 没有 Grok 不派
        self.assertTrue(weekend.ask(self.db, self.grok, at(2026, 11, 5, 10)))
        self.assertFalse(weekend.ask(self.db, self.grok, at(2026, 11, 6, 9)))  # 一个周末一次
        task = self.sent[0][0]
        self.assertIn("【周末推荐 2026-11-06】", task)
        self.assertIn("Dishoom 早午餐", task)  # 和她收藏同类
        self.assertIn("K-pop、中国古典舞、现代舞", task)  # 以前没找过舞蹈课：让它找
        self.assertIn("【周末推荐 2026-11-06】", self.sent[0][2])

    def test_ask_reuses_earlier_dance_search(self):
        prep.ensure(self.db)
        tid = self.db.add_task("挑 K-pop、古典舞、普拉提体验课各一节")
        self.db.conn.execute("INSERT INTO prep_work (task_id, state, result, updated_at) VALUES (?, 'ready', ?, '2026-10-30')",
                             (tid, "K-pop：Base Dance Studios 周六 14:00 £12"))
        self.db.conn.commit()
        self.assertTrue(weekend.ask(self.db, self.grok, at(2026, 11, 5, 10)))
        self.assertIn("先核对这周的上课时间和价格", self.sent[0][0])
        self.assertIn("挑 K-pop、古典舞", self.sent[0][0])

    def test_friday_saved_first_then_grok_and_dance_line(self):
        for t in ("屋顶花园 Sky Garden", "运河边二手书店", "Borough Market", "Columbia Road 花市"):
            self.save(t)
        self.save("芬兰观光团", tags=("旅行",), place={"name": "芬兰", "area": "Finland"}, extra={"intent": "travel"})
        weekend.ask(self.db, self.grok, at(2026, 11, 5, 10))
        self.grok_reply("2026-11-06", [{"kind": "展", "title": "Tate Modern 新展", "when": "周六 10:00", "where": "Bankside", "price": "£18"},
                                       {"kind": "演出", "title": "西区音乐剧", "when": "周六 19:30", "price": "£35"},
                                       {"kind": "店", "title": "新开的抹茶店"},
                                       {"kind": "舞蹈课", "title": "K-pop 零基础体验课", "when": "周日 15:00", "price": "£10"}])
        self.assertEqual(weekend.collect(self.store), 1)
        self.assertIsNone(weekend.due(self.db, at(2026, 11, 6, 11), {}))  # 12 点前不发
        self.assertIsNone(weekend.due(self.db, at(2026, 11, 7, 13), {}))  # 只在周五
        key, text = weekend.due(self.db, FRI, {})
        self.assertEqual(key, "weekend:2026-11-06")
        self.assertTrue(nudge_registry.registered(key))
        got = weekend.picks(self.db, FRI.date())
        self.assertEqual(len(got["saved"]), 3)  # 收藏优先，最多 3 个
        self.assertEqual(len(got["saved"]) + len(got["new"]), 5)  # 合计不超过 5
        self.assertNotIn("芬兰观光团", text)  # 外地旅行的不进周末
        self.assertLess(text.index("她自己收藏过"), text.index("Grok 刚查的"))
        self.assertIn("舞蹈单列", text)
        self.assertIn("K-pop 零基础体验课", text)
        self.assertIn("[日程 日期 HH:MM-HH:MM 标题 @地点]", text)  # 她点头就排进日历
        self.assertIn("等她点头", text)
        nudges.mark_told(self.db, [key])
        self.assertIsNone(weekend.due(self.db, FRI.replace(hour=15), {}))  # 这周发过了

    def test_waits_for_grok_until_four_then_no_repeat_next_week(self):
        self.save("屋顶花园 Sky Garden")
        weekend.ask(self.db, self.grok, at(2026, 11, 5, 10))
        self.assertIsNone(weekend.due(self.db, FRI, {}))  # Grok 还没回，16 点前等
        key, text = weekend.due(self.db, at(2026, 11, 6, 16, 30), {})
        self.assertIn("屋顶花园", text)
        self.assertIn("不到 3 个", text)  # 不够就让 小伴 补
        self.assertIn("让 Grok 在找", text)  # 舞蹈还没查到
        nudges.mark_told(self.db, [key])
        self.save("Columbia Road 花市")
        _, text2 = weekend.due(self.db, at(2026, 11, 13, 13), {})
        self.assertNotIn("屋顶花园", text2)  # 上周推荐过的不再推
        self.assertIn("Columbia Road", text2)

    def test_registered_in_nudges(self):
        self.save("屋顶花园 Sky Garden")
        got = nudges.due(self.db, FRI.replace(hour=20), {})
        self.assertIsNotNone(got)
        self.assertTrue(any(k == "weekend:" for k, *_ in nudge_registry.MESSAGES))

    def test_enrich_recognises_she_wants_to_go(self):
        brain._put(self.db, "link", "link:https://x.com/a", "Shoreditch 新开的甜品店", url="https://x.com/a", status="new")
        self.db.conn.commit()
        self.assertEqual(brain.ideas(self.db), [])  # 还没整理：认不出来
        self.assertIn("自己判断", brain_enrich.PROMPT)
        items = brain_enrich.pending(self.db, 10)
        brain_enrich.apply(self.db, items, [{"id": items[0]["id"], "summary": "甜品店", "tags": ["甜品"], "territory": "life",
                                             "keep": True, "intent": "go", "place": {"name": "某甜品店", "area": "Shoreditch"}}])
        ideas = brain.ideas(self.db)
        self.assertEqual([i["title"] for i in ideas], ["Shoreditch 新开的甜品店"])
        self.assertEqual(ideas[0]["where"], "某甜品店，Shoreditch")
        self.assertEqual(ideas[0]["intent"], "go")


if __name__ == "__main__":
    unittest.main()
