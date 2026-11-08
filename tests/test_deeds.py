import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ethan import deeds
from ethan.core.agent import Ethan, parse_reply
from ethan.memory import MemoryStore


class DeedsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_check_catches_claims_without_action(self):
        now = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
        said = "我翻了你的邮件，没找到 Hidden London 的订单确认"
        self.assertEqual([k for k, _ in deeds.check(said, {}, now)], ["mail"])  # 10-31 那句
        self.assertEqual(deeds.check(said, {"mail_lookups": [{"asked": "ltmuseum", "found": []}]}, now), [])  # 真搜过就不算
        self.assertEqual([k for k, _ in deeds.check("官网条款我也翻到了，写的就是不能退", {}, now)], ["web"])
        fresh = {"from_grok": [{"at": (now - timedelta(minutes=5)).isoformat(), "text": "不能退"}]}
        self.assertEqual(deeds.check("官网条款我也翻到了，写的就是不能退", fresh, now), [])  # Grok 刚回报过
        self.assertEqual([k for k, _ in deeds.check("邮件我已经发出去了", {}, now)], ["sent"])
        self.assertEqual([k for k, _ in deeds.check("我帮你订好了周三的场次", {}, now)], ["booked"])
        for fine in ("邮件我写好了，在工作台里等你点发送", "我让 Grok 去查官网了，等他回报", "我翻了翻今天的计划", "照片发给你了"):
            self.assertEqual(deeds.check(fine, {}, now), [], fine)

    def test_reply_is_quietly_rewritten_and_counted(self):
        texts = ["我翻了你的邮件，没找到订单", "[查邮件 Transport Museum]", "找到了，订单号 778899，我填进邮件里"]
        systems = []

        def complete(system, messages, purpose="chat", images=()):
            systems.append(system)
            return SimpleNamespace(text=texts.pop(0), backend="x", model="m", cost_usd=0)
        ethan = Ethan(self.store, SimpleNamespace(complete=complete, settings=None), persona="人设")
        ethan.mail_search = lambda words: [{"account": "Gmail", "folder": "x", "sender": "LTM", "subject": "Order 778899",
                                            "at": "2026-10-20", "text": "Order 778899"}]
        out = ethan.reply("cli", "订单号在我邮箱里，你自己找", today=datetime(2026, 10, 31).date())
        self.assertEqual(out["bubbles"], ["找到了，订单号 778899，我填进邮件里"])  # 她只看到重写后的
        self.assertIn("发出去之前的核对", systems[1])
        self.assertNotIn("发出去之前的核对", systems[2])
        self.assertEqual(deeds.summary(self.db), {"虚报共几次": 1, "发出去前拦下": 1, "拦下后改对": 1, "她或复盘发现的": 0,
                                                  "按类型": {"说搜过她的邮箱": 1}})

    def test_her_report_mark_and_migration(self):
        self.assertEqual(parse_reply("对不起，是我没查就说了[虚报 我翻了你的邮件]")["bubbles"], ["对不起，是我没查就说了"])
        self.assertEqual(deeds.note_chat(self.db, "[虚报 我翻了你的邮件]"), 1)
        spec = importlib.util.spec_from_file_location("m", Path(__file__).resolve().parent.parent / "migrations" / "20261031_hollow_mail_claim.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("1 笔", m.run(self.store))
        self.assertEqual(m.run(self.store), "已经记过了")
        s = deeds.summary(self.db)
        self.assertEqual((s["虚报共几次"], s["她或复盘发现的"], s["按类型"]["说搜过她的邮箱"]), (2, 2, 2))


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
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
