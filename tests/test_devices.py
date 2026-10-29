import json
import tempfile
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ethan import places_log, quests, territory, worktime
from ethan.inbox import Inbox
from tests.test_workbench import MONDAY, make_demo

AUTH = {"Authorization": "Bearer c"}


def at(day, hh, mm=0):  # 伦敦冬令时 = UTC
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.db = self.store.db
        self.inbox = Inbox(self.store, {"grok": "g", "cloud": "c"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, path, data, headers=AUTH, now=None):
        return self.inbox.handle(path, headers, json.dumps(data).encode(), now)


class WorktimeTest(Base):

    def test_slots_dedupe_and_health_light(self):
        slots = [worktime.slot_of(at(MONDAY, 9) + timedelta(minutes=5 * i)) for i in range(9 * 12)]  # 9 小时
        self.assertEqual(self.post("/cloud/activity", {"slots": slots}, {})[0], 401)
        self.assertEqual(self.post("/cloud/activity", {"slots": slots + slots[:5] + ["坏格子"]})[1]["saved"], len(slots))
        self.assertEqual(worktime.hours(self.db, MONDAY), 9.0)
        row = next(r for r in territory.health(self.db, "career", MONDAY, True) if r["name"] == "本人工作时长")
        self.assertEqual(row["light"], "warn")  # 超过 8 小时黄
        self.assertIn("今天 9 小时", row["actual"])
        # 她在 Mac 上跟 Claude 说话也算一格
        hook = {"session_id": "s", "hook_event_name": "UserPromptSubmit", "cwd": "/x", "prompt": "hi"}
        self.inbox.handle("/cloud/agent/hook?where=mac", AUTH, json.dumps(hook).encode(), at(MONDAY, 22, 3))
        self.assertEqual(worktime.hours(self.db, MONDAY), 9.1)

    def test_mac_script_records_only_when_active_and_retries(self):
        sent = []

        class Resp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return None

        def opener(req, timeout):
            sent.append(json.loads(req.data)["slots"])
            if len(sent) == 1:
                raise OSError("断网")
            return Resp()

        old, worktime.PENDING = worktime.PENDING, self.root / "pending.json"
        self.addCleanup(setattr, worktime, "PENDING", old)
        self.assertEqual(worktime.once(at(MONDAY, 9), idle=lambda: 30, opener=opener), "待发 1 格")  # 断网先攒着
        self.assertEqual(worktime.once(at(MONDAY, 9, 5), idle=lambda: 30, opener=opener), "待发 0 格")  # 连同上一格一起补发
        self.assertEqual(sent[1], [f"{MONDAY}T09:00", f"{MONDAY}T09:05"])
        self.assertEqual(worktime.once(at(MONDAY, 9, 10), idle=lambda: 9999, opener=opener), "待发 0 格")  # 没动键鼠不记
        self.assertEqual(len(sent), 2)

    def test_copy_token_never_prints_it(self):
        got = {}
        with unittest.mock.patch.dict("os.environ", {"ETHAN_HOOK_TOKEN": "secret-xyz"}):
            msg = worktime.copy_token(run=lambda cmd, **kw: got.update(cmd=cmd, input=kw["input"]))
        self.assertEqual(got, {"cmd": ["pbcopy"], "input": "secret-xyz"})
        self.assertNotIn("secret-xyz", msg)


class PlaceTest(Base):
    def test_leave_checks_outing_and_counts_non_work_trips(self):
        self.assertEqual(self.post("/cloud/place", {"event": "去哪"})[0], 400)
        r = self.post("/cloud/place", {"event": "leave"}, now=at(MONDAY, 10))[1]
        self.assertTrue(r["outing"])
        self.assertTrue(self.db.conn.execute("SELECT active FROM habits WHERE title = '每天出门'").fetchone()["active"])
        self.assertEqual(quests.habit_by_title(self.db, "每天出门")["auto"], "outing")
        self.post("/cloud/place", {"event": "arrive"}, now=at(MONDAY, 11, 30))
        self.post("/cloud/place", {"event": "leave"}, now=at(MONDAY, 15, 25))  # 去训练那趟
        self.post("/cloud/place", {"event": "arrive"}, now=at(MONDAY, 17, 40))
        train = [at(MONDAY, 15, 25)]
        self.assertEqual(places_log.non_work_outings(self.db, MONDAY, MONDAY, train), 1)
        self.assertEqual(len(places_log.trips(self.db, MONDAY, MONDAY)), 2)
        body = next(r for r in territory.health(self.db, "body", MONDAY + timedelta(days=1), True) if r["name"] == "每天出门")
        self.assertNotEqual(body["light"], "none")  # 接上定位后亮了
        from ethan import achievements
        ach = next(i for i in achievements.compute(self.db, self.root, MONDAY)["items"] if i["key"] == "outing")
        self.assertEqual((ach["source"], ach["value"]), (True, 1))


if __name__ == "__main__":
    unittest.main()
