"""关系页按微信标签分组（docs/design/relations.md 第 4 节）：
Mac 端从示意 contact.db 读标签（contact_label + extra_buffer 第 30 号字段；3.x 式的直接列；没有标签表的旧版本不报错）、
随私聊统计交标签名、服务器走 relations.upsert 写进人物卡（她改过分组就不覆盖）、关系页接口带标签、前端分组和折叠。"""

import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

from ethan import locks, relations
from ethan import wechatexport as wx
from ethan.inbox import Inbox
from ethan.memory import MemoryStore
from tests.test_relations import AUTH, CLOUD, item
from tests.test_wechatexport import ME, PEER, FakeClient, Fixture, PlainDB, make_db

ROOT = Path(__file__).resolve().parents[1]


def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def extra_buffer(label_csv: str = None) -> bytes:
    """示意 ContactInfo：第 1 号整数（性别）、第 4 号字符串（签名）、第 30 号字符串（标签编号）。"""
    b = varint(1 << 3) + varint(2) + varint((4 << 3) | 2) + varint(len("签名".encode())) + "签名".encode()
    if label_csv is not None:
        raw = label_csv.encode()
        b += varint((30 << 3) | 2) + varint(len(raw)) + raw
    return b


class ReadLabelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "contact.db"

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        db = PlainDB(self.path, "k")
        try:
            return wx.read_labels(db)
        finally:
            db.close()

    def test_v4_extra_buffer_and_label_table(self):
        make_db(self.path, [
            ("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, extra_buffer BLOB)", ()),
            ("CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT, sort_order_ INTEGER)", ()),
            ("INSERT INTO contact_label VALUES (1, '大学同学', 2), (5, '家人', 1), (7, '  伦敦 朋友 ', 3), (9, '', 4)", ()),
            ("INSERT INTO contact VALUES ('wxid_a', 'A', '', ?)", (extra_buffer("1,5"),)),
            ("INSERT INTO contact VALUES ('wxid_b', 'B', '', ?)", (extra_buffer("7,99,7"),)),  # 99 不在标签表、7 重复
            ("INSERT INTO contact VALUES ('wxid_c', 'C', '', ?)", (extra_buffer(),)),  # 没标签
            ("INSERT INTO contact VALUES ('wxid_d', 'D', '', ?)", (b"\xff\xff\xff",)),  # 坏数据不报错
            ("INSERT INTO contact VALUES ('wxid_e', 'E', '', NULL)", ()),
        ])
        got, n = self.read()
        self.assertEqual(n, 3)  # 空名字的不算
        self.assertEqual(got, {"wxid_a": ["家人", "大学同学"], "wxid_b": ["伦敦 朋友"]})  # 按她在微信里的排序

    def test_direct_column_like_3x(self):
        make_db(self.path, [
            ("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT, LabelIDList TEXT)", ()),
            ("CREATE TABLE ContactLabel (LabelId INTEGER, LabelName TEXT)", ()),
            ("INSERT INTO ContactLabel VALUES (2, '同事'), (3, '健身')", ()),
            ("INSERT INTO contact VALUES ('wxid_a', 'A', '', ',2,3,')", ()),
        ])
        self.assertEqual(self.read(), ({"wxid_a": ["同事", "健身"]}, 2))

    def test_old_version_without_label_table(self):
        make_db(self.path, [("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT)", ())])
        self.assertEqual(self.read(), (None, 0))
        make_db(self.path, [("CREATE TABLE contact_label (foo TEXT)", ())])  # 字段名对不上
        self.assertEqual(self.read(), (None, 0))

    def test_proto_parser(self):
        self.assertEqual(wx.proto_strings(extra_buffer("1,2"), 30), [b"1,2"])
        self.assertEqual(wx.proto_strings(b"", 30), [])
        self.assertEqual(wx.proto_strings(varint((30 << 3) | 2) + varint(50) + b"short", 30), [])  # 长度越界就停


class ExportLabelsTest(unittest.TestCase):
    """导出端：dry-run 报读到几个；--send 时随统计交标签名；读不到就不带 labels 字段。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fx = Fixture(self.base, n_private=10)
        self.keys = self.fx.keys(self.base)
        (self.base / "tmpcopies").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def add_labels(self, csv="1,2"):
        c = sqlite3.connect(self.fx.root / "contact" / "contact.db")
        c.execute("ALTER TABLE contact ADD COLUMN extra_buffer BLOB")
        c.execute("CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT, sort_order_ INTEGER)")
        c.execute("INSERT INTO contact_label VALUES (1, '大学同学', 1), (2, '电话 13800138000', 2), (3, '没人用', 3)")
        c.execute("UPDATE contact SET extra_buffer = ? WHERE username = ?", (extra_buffer(csv), PEER))
        c.commit()
        c.close()

    def go(self, mode, client=None):
        return wx.run(self.fx.root, self.keys, ME, mode, self.base / "out", self.base / "state.json",
                      env={"ETHAN_CLOUD_TOKEN": "t" * 32} if client else {}, client=client, opener=PlainDB,
                      handoff_dir=self.base / "handoff", settings=self.base / "nosettings.json", tmp_root=self.base / "tmpcopies")

    def test_dry_run_reports_counts(self):
        self.add_labels()
        self.assertEqual(self.go("dry-run")["labels"], "读到 3 个标签、1 个联系人有标签")

    def test_send_carries_label_names_redacted(self):
        self.add_labels()
        cl = FakeClient()
        rep = self.go("send", cl)
        self.assertFalse(rep["errors"], rep)
        p = next(x for x in cl.stats if x["name"] == "小王")
        self.assertEqual(p["labels"][0], "大学同学")
        self.assertNotIn("13800138000", json.dumps(p, ensure_ascii=False))  # 联系方式照旧去掉
        self.assertEqual(set(p) - {"key", "name", "last", "n90", "n365", "total", "her_share", "her_starts", "rounds", "groups"},
                         {"labels"})  # 只多了标签名

    def test_old_wechat_without_labels(self):
        cl = FakeClient()
        rep = self.go("send", cl)
        self.assertIn("没读到微信标签", rep["labels"])
        self.assertTrue(cl.stats)
        self.assertTrue(all("labels" not in x for x in cl.stats))  # 不带字段：服务器上原来的不动


class ServerLabelsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        self.inbox = Inbox(self.store, {"cloud": CLOUD})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, people):
        return self.inbox.handle("/cloud/people_stats", AUTH, json.dumps({"people": people}, ensure_ascii=False).encode())

    def labels(self, name):
        r = self.db.conn.execute("SELECT id, wx_labels FROM people WHERE name = ?", (name,)).fetchone()
        return r["id"], json.loads(r["wx_labels"]) if r["wx_labels"] else None

    def test_ingest_follow_wechat_and_lock(self):
        self.post([item(labels=["大学同学", "上海", "大学同学", "x" * 40])])
        pid, got = self.labels("小林")
        self.assertEqual(got, ["大学同学", "上海", "x" * 20])
        self.post([item(labels=["家人"])])  # 她在微信里改了标签：跟着改
        self.assertEqual(self.labels("小林")[1], ["家人"])
        self.post([item()])  # 旧版导出不带 labels：不动
        self.assertEqual(self.labels("小林")[1], ["家人"])
        self.post([item(labels=[])])  # 微信里标签都去掉了：清空
        self.assertEqual(self.labels("小林")[1], [])
        self.assertFalse(locks.locked(self.db, "person", pid, "wx_labels"))  # 来源数据不算她改过
        self.assertTrue(relations.set_field(self.db, pid, "wx_labels", "挚友，伦敦、挚友")["ok"])  # 她在工作台改分组 → 锁
        self.post([item(labels=["同事"])])
        self.assertEqual(self.labels("小林")[1], ["挚友", "伦敦"])
        page = relations.page(self.db, relations.date(2026, 11, 6))
        card = next(p for p in page["people"] if p["name"] == "小林")
        self.assertEqual((card["labels"], "wx_labels" in card["locked"]), (["挚友", "伦敦"], True))

    def test_deleted_not_rebuilt_by_labels(self):
        self.post([item(labels=["家人"])])
        relations.delete(self.db, self.labels("小林")[0])
        self.post([item(labels=["家人"])])
        self.assertIsNone(self.db.conn.execute("SELECT 1 FROM people WHERE name = '小林'").fetchone())


class FrontendLabelsTest(unittest.TestCase):
    APP = (ROOT / "ethan/workbench/static/app.js").read_text(encoding="utf-8")

    def test_grouping_ui(self):
        fn = self.APP[self.APP.index("function relLabelGroups("):self.APP.index("function relDeleted(")]
        self.assertIn('"未分组"', fn)
        self.assertIn('data-act="rel-fold"', fn)
        self.assertIn("aria-expanded", fn)
        self.assertIn("${g.length} 人", fn)
        self.assertIn("ls.forEach", fn)  # 多个标签：每组都出现
        self.assertIn('a === "rel-fold"', self.APP)
        self.assertIn('a === "rel-by"', self.APP)
        self.assertIn('field("wx_labels"', self.APP)
        self.assertTrue(re.search(r"REL_GROUPS\.map", self.APP))  # 按重要程度那套还在


if __name__ == "__main__":
    unittest.main()
