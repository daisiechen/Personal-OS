import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import brain, files, finance
from ethan.memory import MemoryStore

STATEMENT = "\n".join(["招商银行 交易明细 2026-07-01 至 2026-09-30"] + [f"2026-0{m}-1{d} 星巴克 -3{d}.50 CNY" for m in (7, 8, 9) for d in range(3)])


class FakeRouter:
    def __init__(self):
        self.calls = []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append((purpose, list(images)))
        if purpose == "file-transcribe":
            return SimpleNamespace(text="图片里写着：周六午餐 4 人")
        if purpose == "file-digest":
            body = messages[0]["content"]
            if "交易明细" in body:
                return SimpleNamespace(text=json.dumps({"title": "招行 7–9 月流水", "summary": "三个月的流水", "doc_type": "bank_statement", "tags": ["流水"]}))
            return SimpleNamespace(text=json.dumps({"title": "午餐订位截图", "summary": "周六午餐", "doc_type": "screenshot", "tags": []}))
        if purpose == "statement-import":
            rows = [{"date": f"2026-0{m}-1{d}", "time": "", "amount": -(30 + d + 0.5), "currency": "CNY", "merchant": "星巴克", "kind": "spend"}
                    for m in (7, 8, 9) for d in range(3)]
            return SimpleNamespace(text=json.dumps(rows))
        return SimpleNamespace(text="")


class FilesTest(unittest.TestCase):
    def test_every_file_goes_into_memory_and_statement_into_ledger(self):
        """发的所有文件都要提取内容存进记忆；流水逐笔入账（她同意）。"""
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = MemoryStore(root)
            day = root / "media" / "2026-10-20"
            day.mkdir(parents=True)
            (day / "120000-1.txt").write_text(STATEMENT, encoding="utf-8")
            (day / "130000-2.jpg").write_bytes(b"\xff\xd8fake")
            (day / "130000-3-f0.jpg").write_bytes(b"\xff\xd8frame")  # 视频截图不单独存
            r = FakeRouter()
            got = files.process(store, r)
            self.assertEqual(len(got), 2)
            stmt = next(g for g in got if g["doc_type"] == "bank_statement")
            self.assertEqual(stmt["transactions"], 9)
            self.assertEqual(finance.month_spend(store.db, __import__("datetime").date(2026, 8, 31))["CNY"], 30.5 + 31.5 + 32.5)
            items = brain.search(store.db, "all", kind="file")
            self.assertEqual({i["title"] for i in items}, {"招行 7–9 月流水", "午餐订位截图"})
            img = next(i for i in items if i["title"] == "午餐订位截图")
            self.assertIn("周六午餐 4 人", files.full_text(root, img))
            self.assertEqual(files.process(store, r), [])  # 存过的不再存
            # 同一笔 Monzo 已经记过的，流水里不重复
            finance.add(store.db, "monzo", __import__("datetime").datetime(2026, 10, 1, 12), -5.0, "GBP", ext_id="m1")
            self.assertEqual(finance.import_statement(store.db, type("R", (), {"complete": lambda s, *a, **k: SimpleNamespace(
                text=json.dumps([{"date": "2026-10-01", "amount": -5, "currency": "GBP", "merchant": "x", "kind": "spend"}]))})(), "x"), 0)
            store.close()

    def test_kick_needs_media_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(files.kick(d, spawn=lambda *a, **k: None))


if __name__ == "__main__":
    unittest.main()
