"""今日卡照昨天的日记出。"""

import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from ethan import cards, diary, memusers, quests
from ethan.core.router import Settings
from ethan.workbench.api import Workbench
from tests.test_workbench import MONDAY, NOON, FakeMaker, make_demo

SUNDAY = MONDAY - timedelta(days=1)
DIARY = "下午我去 Borough Market 吃了生蚝，又沿着泰晤士河走了一大圈。晚上和 Lena 视频，聊到她下个月要来伦敦。"


def shot(line, title="河边散步"):
    return json.dumps({"title": title, "line": line, "scene": "by the Thames", "action": "walking", "outfit": "coat",
                       "camera": "wide shot", "light": "golden hour", "twist": ""}, ensure_ascii=False)


class Router:
    """按 purpose 回话：导演（card-director）按顺序吐 lines；写日记（diary）吐一篇；重写那句（card-line）吐 relines。"""

    def __init__(self, lines=(), diary_text=None, relines=()):
        self.lines, self.diary_text, self.relines = list(lines), diary_text, list(relines)
        self.calls = []

    def complete(self, system, messages, purpose="chat"):
        self.calls.append((purpose, messages[0]["content"]))
        if purpose == "diary":
            return SimpleNamespace(text=self.diary_text or "")
        if purpose == "card-line":
            return SimpleNamespace(text=self.relines.pop(0) if self.relines else "")
        return SimpleNamespace(text=shot(self.lines.pop(0) if len(self.lines) > 1 else self.lines[0]))


class CardFromDiaryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.plan = self.db.latest_plan(MONDAY.isoformat())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make(self, router, store=None):
        return cards.make_card(self.root, self.db, MONDAY, self.plan, maker=FakeMaker(), router=router, store=store,
                               fetch=lambda url, timeout: 1 / 0)

    def test_card_uses_yesterdays_diary_her_version_first(self):
        diary.save(self.db, SUNDAY.isoformat(), "下午我在家睡了一觉。", source="ethan")
        diary.save(self.db, SUNDAY.isoformat(), DIARY)  # 她改过：她的版本为准
        r = Router(["昨天的生蚝好吃吗？今天我请你喝咖啡，小宝"])
        card = self.make(r)
        brief = r.calls[0][1]
        self.assertIn("Borough Market", brief)
        self.assertNotIn("睡了一觉", brief)
        self.assertEqual(card["line"], "昨天的生蚝好吃吗？今天我请你喝咖啡，小宝")
        self.assertEqual(card["checks"], {"retried": False, "dropped": []})
        self.assertEqual((card["diary"]["day"], card["diary"]["source"]), (SUNDAY.isoformat(), "her_edited"))
        self.assertEqual(card["diary"]["hash"], cards.diary_hash(DIARY))
        self.assertEqual(len([c for c in r.calls if c[0] == "card-director"]), 1)

    def test_mismatch_redirects_once(self):
        diary.save(self.db, SUNDAY.isoformat(), DIARY, source="ethan")
        r = Router(["昨天你在 Hyde Park 跑了 10 公里，真棒", "昨天和 Lena 视频开心吗？"])
        card = self.make(r)
        directs = [c for c in r.calls if c[0] == "card-director"]
        self.assertEqual(len(directs), 2)
        self.assertIn("找不到依据", directs[1][1])
        self.assertIn("Hyde Park", directs[1][1])
        self.assertEqual(card["line"], "昨天和 Lena 视频开心吗？")
        self.assertEqual(card["shot"]["line"], card["line"])
        self.assertEqual(card["checks"], {"retried": True, "dropped": []})

    def test_still_wrong_drops_the_sentence_and_logs(self):
        diary.save(self.db, SUNDAY.isoformat(), DIARY, source="ethan")
        r = Router(["昨天你在 Hyde Park 跑了 10 公里，今天也要好好吃饭"])
        card = self.make(r)
        self.assertEqual(card["line"], "今天也要好好吃饭")
        self.assertTrue(card["checks"]["retried"])
        self.assertEqual(card["checks"]["dropped"], ["昨天你在 Hyde Park 跑了 10 公里，"])
        saved = json.loads((self.root / "cards" / f"{MONDAY}.json").read_text())
        self.assertEqual(saved["checks"], card["checks"])  # 记了一笔，卡上查得到
        # 整句都对不上：换成不提昨天的备用句
        r2 = Router(["昨天在 Hyde Park 跑了 10 公里"])
        cards.retire(self.root, MONDAY)
        card2 = self.make(r2)
        self.assertNotIn("Hyde", card2["line"])
        self.assertNotIn("昨", card2["line"])

    def test_chinese_events_must_be_in_diary(self):
        self.assertEqual(cards.off_diary("昨天的生蚝好吃吗？", DIARY), [])
        self.assertTrue(cards.off_diary("昨天你去攀岩还拿了冠军，", DIARY))
        self.assertTrue(cards.off_diary("今天也要去 Soho，", DIARY))  # 说今天的：英文名得在日记或今天的安排里
        self.assertEqual(cards.off_diary("今天也要去 Soho，", DIARY, today="19:00 晚饭 Soho"), [])
        self.assertTrue(cards.off_diary("昨天的生蚝好吃吗？", None))  # 没日记：说到昨天的一律去掉

    def test_editing_diary_marks_card_and_refreshes_text_only(self):
        diary.save(self.db, SUNDAY.isoformat(), DIARY, source="ethan")
        maker = FakeMaker()
        cards.make_card(self.root, self.db, MONDAY, self.plan, maker=maker, router=Router(["昨天的生蚝好吃吗？"]),
                        fetch=lambda url, timeout: 1 / 0)
        self.assertFalse(cards.stale(self.root, self.db, MONDAY))
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        kicked = []
        wb.card_refresh = kicked.append
        new = "下午我去 Tate Modern 看了展。晚上和 Lena 视频。"
        got = wb.act("diary.save", {"day": SUNDAY.isoformat(), "text": new})
        self.assertTrue(got["ok"])
        self.assertIn("卡会跟着更新", got["msg"])
        self.assertEqual(kicked, [MONDAY])
        self.assertTrue(json.loads((self.root / "cards" / f"{MONDAY}.json").read_text())["stale"])
        self.assertTrue(cards.stale(self.root, self.db, MONDAY))
        r = Router(relines=["昨天去 Borough Market 了？", "昨天的 Tate Modern 好看吗？"])
        card = cards.refresh(self.root, self.db, MONDAY, r)
        self.assertEqual(card["line"], "昨天的 Tate Modern 好看吗？")  # 第一版还照旧日记写，对不上重写一次
        self.assertEqual(card["old_line"], "昨天的生蚝好吃吗？")
        self.assertNotIn("stale", card)
        self.assertFalse(cards.stale(self.root, self.db, MONDAY))
        self.assertEqual(len(maker.calls), 1)  # 不重画
        self.assertTrue((self.root / "cards" / f"{MONDAY}.jpg").exists())
        # 工作台首页看到对不上的卡也会补一次重出（日记在别处改的）
        diary.save(self.db, SUNDAY.isoformat(), new + "睡前看了会儿书。")
        kicked.clear()
        wb.card(MONDAY)
        self.assertEqual(kicked, [MONDAY])

    def test_no_diary_writes_it_first(self):
        quests.award(self.db, 10, "晨跑 5 公里", SUNDAY)
        r = Router(["昨天晨跑辛苦啦，今天慢慢来"], diary_text="早上我去晨跑了 5 公里，跑完整个人都醒了。")
        card = self.make(r, store=self.store)
        self.assertEqual([c[0] for c in r.calls][:2], ["diary", "card-director"])  # 先补日记再出卡
        self.assertEqual(diary.get(self.db, SUNDAY.isoformat())["source"], "ethan")
        self.assertIn("出卡前先补写", card["diary_note"])
        self.assertIn("晨跑", r.calls[1][1])
        self.assertEqual(card["line"], "昨天晨跑辛苦啦，今天慢慢来")

    def test_no_diary_and_backfill_fails_keeps_old_material_without_yesterday(self):
        r = Router(["昨天你一定很累吧，今天下雨记得带伞"], diary_text="")  # 补写没成
        card = self.make(r, store=self.store)
        self.assertIsNone(diary.get(self.db, SUNDAY.isoformat()))
        self.assertIsNone(card["diary"]["hash"])
        self.assertIn("没有昨天的日记", [c for c in r.calls if c[0] == "card-director"][0][1])
        self.assertNotIn("训练", [c for c in r.calls if c[0] == "card-director"][0][1])  # 今天（周一）的训练不进昨日卡
        self.assertEqual(card["line"], "今天下雨记得带伞")
        # 后来补上了日记：卡算对不上、要重出
        diary.save(self.db, SUNDAY.isoformat(), DIARY, source="ethan")
        self.assertTrue(cards.stale(self.root, self.db, MONDAY))

    def test_old_cards_without_diary_record_are_left_alone(self):
        (self.root / "cards").mkdir(exist_ok=True)
        (self.root / "cards" / f"{MONDAY}.json").write_text(json.dumps({"day": MONDAY.isoformat(), "line": "x"}))
        diary.save(self.db, SUNDAY.isoformat(), DIARY)
        self.assertFalse(cards.stale(self.root, self.db, MONDAY))

    def test_registered_as_memory_user(self):
        u = memusers.USERS["cards"]
        self.assertIn("昨天的日记", u["uses"])
        self.assertFalse(memusers.may_use("cards", "search"))
        self.assertFalse(memusers.may_use("cards", "portrait"))
        self.assertEqual(memusers.entity_of("cards"), "os")

    def test_diary_no_longer_reads_card_title(self):  # 卡照日记出，日记不再读卡名和理由，免得互相抄
        diary.save(self.db, SUNDAY.isoformat(), DIARY, source="ethan")
        cards.make_card(self.root, self.db, MONDAY, self.plan, maker=FakeMaker(), router=Router(["今天加油"]),
                        fetch=lambda url, timeout: 1 / 0)
        self.store.raw.append("telegram", "mia", "今天去 Tate 看了展")
        m = diary.materials(self.store, MONDAY)
        self.assertNotIn("这天的卡", m)
        self.assertNotIn("河边散步", m)


if __name__ == "__main__":
    unittest.main()
