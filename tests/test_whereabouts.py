import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ethan import places_log, whereabouts
from ethan.core.agent import parse_reply
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

CLOUD = "c" * 40
T0 = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)


class WhereaboutsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.dir.name))
        self.db = self.store.db
        env = mock.patch.dict(os.environ, {"ETHAN_HOME_CITY": "伦敦"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def test_told_city_then_back_home(self):
        """2026-10-29 她定：告诉了 小伴 就同步给个人主页，没告诉就默认常驻地。"""
        self.assertEqual(whereabouts.current(self.db)["city"], "伦敦")
        self.assertEqual(whereabouts.note(self.db, "玩得开心 [在 巴黎]", T0), ["巴黎"])
        self.assertEqual(whereabouts.current(self.db), {"city": "巴黎", "source": "told", "at": T0.isoformat()})
        places_log.record(self.db, "arrive", now=T0 + timedelta(days=3))  # iPhone 报到家，就是回来了
        self.assertEqual(whereabouts.current(self.db)["city"], "伦敦")
        self.assertEqual(whereabouts.current(self.db)["source"], "home")

    def test_saying_home_city_resets(self):
        whereabouts.note(self.db, "[在 巴黎]", T0)
        whereabouts.note(self.db, "[在 伦敦]", T0 + timedelta(days=1))
        self.assertEqual(whereabouts.current(self.db)["source"], "home")

    def test_mark_hidden_from_her(self):
        self.assertEqual(parse_reply("到了就好 [在 巴黎]")["bubbles"], ["到了就好"])

    def test_profile_endpoint(self):
        inbox = Inbox(self.store, {"cloud": CLOUD})
        self.assertEqual(inbox.handle_get("/cloud/profile", {})[0], 401)
        code, body = inbox.handle_get("/cloud/profile", {"Authorization": f"Bearer {CLOUD}"})
        self.assertEqual(code, 200)
        self.assertEqual(body["city"], "伦敦")
        self.assertRegex(body["birthday"], r"^\d\d-\d\d$")

    def test_home_city_from_timezone(self):
        with mock.patch.dict(os.environ, {"ETHAN_HOME_CITY": ""}), mock.patch("ethan.clock.DEFAULT_TZ", "Asia/Shanghai"):
            self.assertEqual(whereabouts.home_city(), "上海")


if __name__ == "__main__":
    unittest.main()
