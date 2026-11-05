import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from ethan import bgwork, interviews

NOW = datetime(2026, 11, 4, 21, 0, tzinfo=timezone.utc)


class Raw:
    def __init__(self, recs):
        self.recs = recs

    def recent(self, n):
        return self.recs[-n:]


def store(minutes_ago=None):
    recs = [] if minutes_ago is None else [{"role": "mia", "at": (NOW - timedelta(minutes=minutes_ago)).isoformat()}]
    return SimpleNamespace(raw=Raw(recs))


class Backend:
    def __init__(self, fails):
        self.fails, self.calls = fails, 0

    def complete(self, system, messages, images=()):
        self.calls += 1
        if self.calls <= self.fails:
            raise RuntimeError("额度满了")
        return "ok"


class Router:
    def __init__(self, backend):
        self.primary, self.fallback_used = backend, False

    def _call(self, backend, system, messages, purpose, images=()):
        return backend.complete(system, messages, images=images)

    def complete(self, *a, **k):
        self.fallback_used = True
        return "fallback"


class BgWorkTest(unittest.TestCase):
    def test_chatting(self):
        self.assertTrue(bgwork.chatting(store(3), NOW))
        self.assertFalse(bgwork.chatting(store(30), NOW))
        self.assertFalse(bgwork.chatting(store(), NOW))

    def test_backs_off_and_never_uses_fallback(self):
        sleeps = []
        r = Router(Backend(fails=2))
        p = bgwork.PatientRouter(r, store(), sleep=sleeps.append, now=lambda: NOW)
        self.assertEqual(p.complete("s", []), "ok")
        self.assertEqual(sleeps, [bgwork.BACKOFF * 60] * 2)
        self.assertFalse(r.fallback_used)

    def test_waits_while_she_chats(self):
        sleeps, clock = [], [NOW]

        def sleep(s):
            sleeps.append(s)
            clock[0] = clock[0] + timedelta(seconds=s)
        p = bgwork.PatientRouter(Router(Backend(0)), store(1), sleep=sleep, now=lambda: clock[0])
        self.assertEqual(p.complete("s", []), "ok")
        self.assertTrue(sleeps and all(s == bgwork.CHECK for s in sleeps))

    def test_priority_private_before_group_recent_first(self):
        evs = [{"id": 1, "text": "〔会话：微信群 · 读书会〕", "occurred_at": "2026-11-01"},
               {"id": 2, "text": "〔会话：微信 · 小王〕", "occurred_at": "2025-01-01"},
               {"id": 3, "text": "〔会话：微信 · 小李〕", "occurred_at": "2026-10-01"},
               {"id": 4, "text": "访谈", "occurred_at": "2026-11-03"}]
        self.assertEqual([e["id"] for e in sorted(evs, key=interviews._priority)], [4, 3, 2, 1])


class RetryLockedTest(unittest.TestCase):
    def test_retries_on_locked_only(self):
        import sqlite3
        calls, sleeps = [], []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise sqlite3.OperationalError("database is locked")
            return "ok"
        self.assertEqual(bgwork.retry_locked(fn, sleep=sleeps.append), "ok")
        self.assertEqual(len(sleeps), 2)
        with self.assertRaises(sqlite3.OperationalError):
            bgwork.retry_locked(lambda: (_ for _ in ()).throw(sqlite3.OperationalError("no such table")), sleep=sleeps.append)


if __name__ == "__main__":
    unittest.main()
