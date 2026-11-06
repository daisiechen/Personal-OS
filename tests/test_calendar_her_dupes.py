"""回“按建议删”：11/7 留票券删“日出”，11/14 三条合成一条。先存原样再删，能恢复。"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ethan import caltidy
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
ICS = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:{u}\r\nSUMMARY:{t}\r\nDESCRIPTION:原来的描述\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"


class Cal:
    def __init__(self):
        self.store = {"h/1": ("2026-11-07", "日出"), "h/2": ("2026-11-07", "票券：THE SUNRISE CLUB: meet"),
                      "h/3": ("2026-11-14", "伦敦创业者与投资人 Saturday Social 社交"),
                      "h/4": ("2026-11-14", "伦敦考文特花园中小企业社交（GMax Card）"), "h/5": ("2026-11-14", "创始人交流会 Founder Mixer（考文特花园）")}
        self.ics = {h: ICS.format(u=h, t=t) for h, (_, t) in self.store.items()}
        self.deleted = []

    def all_events(self, day):
        return [("18:30", "21:30", t, {"calendar": "个人", "href": h, "recurring": False}) for h, (d, t) in self.store.items()
                if d == day.isoformat() and h not in self.deleted]

    def request(self, method, url, body=None, depth=None, ctype=None):
        if method == "PUT":
            self.ics[url] = body.decode()
        return 200, self.ics.get(url, "")

    def delete_event(self, href):
        self.deleted.append(href)


class HerDupesTest(unittest.TestCase):
    def test_migration(self):
        spec = importlib.util.spec_from_file_location("m", ROOT / "migrations" / "20261104_calendar_her_dupes.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        cal = Cal()
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            with mock.patch("ethan.icloud.build_calendar", lambda: cal):
                out = m.run(store)
            self.assertEqual(sorted(cal.deleted), ["h/1", "h/4", "h/5"])
            self.assertIn("创始人交流会", cal.ics["h/3"])  # 另外两个名字写进留下那条的描述
            self.assertIn("删了「日出」", out)
            self.assertEqual(store.db.conn.execute("SELECT COUNT(*) FROM cal_tidy").fetchone()[0], 4)  # 原样都存了
            n = caltidy.undo(store.db, cal)
            self.assertEqual(n, 4)
            store.close()


if __name__ == "__main__":
    unittest.main()



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
