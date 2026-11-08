"""每晚整理一步失败不拖垮后面、额度恢复后自动补。"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from ethan import cards, catchup, diary, nightly
from ethan.core import quota
from ethan.core.router import NeedsSwitchApproval
from tests.test_workbench import MONDAY, FakeMaker, make_demo

TUESDAY = MONDAY + timedelta(days=1)
AT = datetime(TUESDAY.year, TUESDAY.month, TUESDAY.day, 13, 0, tzinfo=timezone.utc)  # 本地 13:00，额度已恢复
SHOT = {"title": "河边慢跑", "line": "今天也慢慢来，小宝", "scene": "by the Thames", "action": "jogging", "outfit": "coat",
        "camera": "low angle", "light": "morning", "twist": ""}


class Router:
    """quota=True：一调就报额度满（每晚整理那天的样子）；否则日记、导演都正常回。"""

    def __init__(self, quota=False, director_ok=True):
        self.quota, self.director_ok, self.calls = quota, director_ok, []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append(purpose)
        if self.quota:
            raise NeedsSwitchApproval("订阅额度用完")
        if purpose == "diary":
            return SimpleNamespace(text="今天我去训练了，练完腿有点酸。晚上早早睡了。")
        if purpose == "card-director":
            if not self.director_ok:
                raise NeedsSwitchApproval("订阅额度用完")
            return SimpleNamespace(text=json.dumps(SHOT, ensure_ascii=False))
        return SimpleNamespace(text="好")


class NightlyStepsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        at = datetime(MONDAY.year, MONDAY.month, MONDAY.day, 20, tzinfo=timezone.utc)
        self.store.raw.append("telegram", "mia", "今天练腿了", at=at)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_first_step_quota_error_does_not_stop_the_rest(self):
        done = []
        with mock.patch("ethan.diary.backfill", lambda store, router, today: done.append("日记") or {}), \
                mock.patch("ethan.attributes.snapshot", lambda db, day: done.append("快照")), \
                mock.patch("ethan.lifebase.backfill", lambda store: done.append("地基") or {}), redirect_stdout(io.StringIO()):
            failed = nightly.steps(self.store, lambda: Router(quota=True), self.root, MONDAY, TUESDAY, sunday=False)
        self.assertIn(f"{MONDAY} 聊天整理", failed)
        self.assertEqual(done, ["日记", "快照", "地基"])  # 第一步报额度满，后面的照跑

    def test_main_exits_nonzero_and_flags_failed_steps(self):
        with mock.patch("ethan.nightly.steps", lambda *a: ["聊天整理"]), \
                mock.patch("sys.argv", ["nightly", "--data", str(self.root)]), \
                mock.patch("ethan.cli.build_router", lambda *a: Router()), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                nightly.main()
        self.assertEqual(e.exception.code, 1)  # jobs.json 记成失败，体检看得到


class CatchupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        (self.root / "weather.json").write_text(json.dumps({"days": {MONDAY.isoformat(): {"day": MONDAY.isoformat(), "code": 53, "max": 12,
                                                                                            "min": 6, "wind": 10, "rain": 0.2}}}))
        # 06:31 出卡时额度满：日记补不了、导演也编不出来——不再退回随机拼图，这天先不出
        self.maker = FakeMaker()
        self.card = cards.make_card(self.root, self.db, TUESDAY, self.db.latest_plan(TUESDAY.isoformat()), maker=self.maker,
                                    router=Router(quota=True), store=self.store)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def today_card(self):
        return json.loads((self.root / "cards" / f"{TUESDAY}.json").read_text())

    def test_quota_failure_waits_instead_of_random_card(self):
        self.assertIn("waiting", self.card)
        self.assertFalse((self.root / "cards" / f"{TUESDAY}.json").exists())
        self.assertEqual(self.maker.calls, [])  # 不生图
        self.assertTrue(cards.waiting(self.root, TUESDAY))
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        shown = Workbench(self.store, Settings(self.root / "settings.json")).card(TUESDAY)
        self.assertEqual((shown["waiting"], shown["image"]), ("昨天的日记还没写好", None))  # 工作台显示在等

    def test_director_failure_also_waits(self):
        diary.save(self.db, MONDAY.isoformat(), "今天我去训练了。", source="ethan")
        card = cards.make_card(self.root, self.db, TUESDAY, None, maker=self.maker, router=Router(director_ok=False), store=self.store)
        self.assertIn("导演", card["waiting"])
        self.assertEqual(self.maker.calls, [])

    def test_after_quota_back_writes_diary_then_makes_whole_card(self):
        self.assertEqual(catchup.due(self.store, now=AT), ["diary", "card"])
        got = catchup.run(self.store, Router(), now=AT, maker=FakeMaker())
        self.assertTrue(diary.get(self.db, MONDAY.isoformat()))
        card = self.today_card()
        self.assertEqual((card["title"], card["line"], card["shot"]["scene"]), ("河边慢跑", SHOT["line"], "by the Thames"))
        self.assertTrue(card["generated"])  # 图、卡名、那句话一起出
        self.assertEqual(card["diary"]["day"], MONDAY.isoformat())  # 照补好的日记出
        self.assertEqual((card["weather"], card["weather_day"]), ("阴 6–12°", MONDAY.isoformat()))  # 昨天的实际天气，毛毛雨没下多少写“阴”
        self.assertIsNone(cards.waiting(self.root, TUESDAY))
        self.assertIn("出了", got["card"])
        self.assertEqual(catchup.due(self.store, now=AT + timedelta(minutes=10)), [])

    def test_director_fails_again_keeps_waiting_and_waits_an_hour(self):
        maker = FakeMaker()
        got = catchup.run(self.store, Router(director_ok=False), now=AT, maker=maker)
        self.assertIn("接着等", got["card"])
        self.assertFalse((self.root / "cards" / f"{TUESDAY}.json").exists())
        self.assertEqual(maker.calls, [])  # 没花生图的钱
        r = Router()
        self.assertEqual(catchup.run(self.store, r, now=AT + timedelta(minutes=50), maker=FakeMaker()), {})  # 一小时内不再试
        self.assertEqual(r.calls, [])
        self.assertEqual(catchup.due(self.store, now=AT + timedelta(minutes=61)), ["card"])

    def test_nothing_tried_while_quota_block_is_on(self):
        quota.block(self.root, AT + timedelta(hours=3), "订阅额度用完", now=AT)
        r = Router()
        self.assertEqual(catchup.due(self.store, now=AT), [])
        self.assertEqual(catchup.run(self.store, r, now=AT, maker=FakeMaker()), {})
        self.assertEqual(r.calls, [])
        spawned = []
        self.assertEqual(catchup.kick(self.store, now=AT, spawn=lambda *a, **k: spawned.append(a)), [])
        self.assertEqual(spawned, [])

    def test_waits_for_nightly_before_noon(self):
        early = datetime(TUESDAY.year, TUESDAY.month, TUESDAY.day, 2, 0, tzinfo=timezone.utc)  # 本地 02:00，每晚整理还没跑
        self.assertEqual(catchup.due(self.store, now=early), [])
        from ethan import jobs
        jobs.record(self.root, "memory", False, early + timedelta(hours=1), early + timedelta(hours=1, minutes=5))
        self.assertEqual(catchup.due(self.store, now=early + timedelta(hours=2)), ["diary", "card"])

    def test_kick_spawns_once(self):
        spawned = []
        self.assertEqual(catchup.kick(self.store, now=AT, spawn=lambda *a, **k: spawned.append(a)), ["diary", "card"])
        self.assertEqual(catchup.kick(self.store, now=AT, spawn=lambda *a, **k: spawned.append(a)), [])  # 在补就不再起
        self.assertEqual(len(spawned), 1)


class PatchedCardTest(unittest.TestCase):
    """上线“等补好再出”之前退回 choose() 拼出来的卡（有 variety、没有 shot、没在等）也要重出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        (self.root / "weather.json").write_text(json.dumps({"days": {MONDAY.isoformat(): {"day": MONDAY.isoformat(), "code": 3, "max": 12,
                                                                                            "min": 6, "wind": 10, "rain": 0}}}))
        diary.save(self.db, MONDAY.isoformat(), "今天我去训练了，练完腿有点酸。", source="ethan")
        with redirect_stdout(io.StringIO()):  # 没接模型：照旧按代码拼，和 11-06 那张一样有 variety、没有 shot
            self.old = cards.make_card(self.root, self.db, TUESDAY, None, maker=FakeMaker(), store=self.store)
        self.path = self.root / "cards" / f"{TUESDAY}.json"

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_patched_card_is_due(self):
        self.assertTrue(self.old.get("variety"))
        self.assertNotIn("shot", self.old)
        self.assertTrue(cards.patched(self.root, TUESDAY))
        self.assertEqual(catchup.card_need(self.root, self.db, TUESDAY), "make")
        self.assertIn("card", catchup.due(self.store, now=AT))

    def test_card_with_shot_is_not_due(self):
        self.path.write_text(json.dumps({**self.old, "shot": SHOT}, ensure_ascii=False))
        self.assertFalse(cards.patched(self.root, TUESDAY))
        self.assertNotIn("card", catchup.due(self.store, now=AT))

    def test_remake_replaces_it_with_a_directed_card(self):
        with redirect_stdout(io.StringIO()):
            got = catchup.run(self.store, Router(), now=AT, maker=FakeMaker())
        card = json.loads(self.path.read_text())
        self.assertIn("出了", got["card"])
        self.assertEqual((card["title"], card["shot"]["scene"]), ("河边慢跑", "by the Thames"))
        self.assertTrue(list((self.root / "cards" / "old").glob(f"{TUESDAY}-*.json")))  # 旧的挪进 old，不删
        self.assertNotIn("card", catchup.due(self.store, now=AT + timedelta(hours=2)))

    def test_failed_remake_keeps_her_card_and_retries_hourly(self):
        before = self.path.read_text()
        image = (self.root / "cards" / f"{TUESDAY}.jpg").read_bytes()
        maker = FakeMaker()
        with redirect_stdout(io.StringIO()):
            got = catchup.run(self.store, Router(director_ok=False), now=AT, maker=maker)
        self.assertIn("接着等", got["card"])
        self.assertEqual(self.path.read_text(), before)  # 原来那张挪回来了
        self.assertEqual((self.root / "cards" / f"{TUESDAY}.jpg").read_bytes(), image)
        self.assertFalse((self.root / cards.WAIT).exists())
        self.assertEqual(maker.calls, [])
        self.assertNotIn("card", catchup.due(self.store, now=AT + timedelta(minutes=50)))  # 一小时最多试一次
        self.assertIn("card", catchup.due(self.store, now=AT + timedelta(minutes=61)))


class YesterdayWeatherTest(unittest.TestCase):
    """卡上写昨天的实际天气；毛毛雨几乎没下写“阴”，不写“雨”。"""

    def test_kinds(self):
        w = {"code": 53, "min": 6.2, "max": 11.8}
        self.assertEqual(cards.weather_text({**w, "rain": 0.3}), "阴 6–12°")
        self.assertEqual(cards.weather_text({**w, "rain": 1.2}), "毛毛雨 6–12°")
        self.assertEqual(cards.weather_text({**w, "code": 61, "rain": 4}), "雨 6–12°")
        self.assertEqual(cards.weather_scene({**w, "rain": 0.3}, MONDAY), "W4")  # 不画成雨天

    def test_fetches_the_given_day_and_caches(self):
        with tempfile.TemporaryDirectory() as d:
            urls = []

            def fetch(url, timeout):
                urls.append(url)
                payload = json.dumps({"daily": {"weathercode": [51], "temperature_2m_max": [10], "temperature_2m_min": [5],
                                                "windspeed_10m_max": [8], "precipitation_sum": [0.1]}}).encode()
                return _Resp(payload)
            self.assertEqual(cards.weather_text(cards.weather(Path(d), MONDAY, fetch)), "阴 5–10°")
            self.assertIn(f"start_date={MONDAY}&end_date={MONDAY}", urls[0])
            cards.weather(Path(d), MONDAY, fetch)
            self.assertEqual(len(urls), 1)  # 按天缓存
            self.assertEqual(cards.weather(Path(d), TUESDAY, fetch=None), {})  # 只看缓存


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.payload


if __name__ == "__main__":
    unittest.main()
