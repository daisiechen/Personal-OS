"""微信导出（ontology-plan 1.9，ethan/wechatexport.py）：用普通 sqlite 造假库（替换打开加密库的 opener），
覆盖会话列表、全部历史、她/别人、占位、增量、redact、白名单、只读、没钥匙只生成文件、sqlcipher 命令行的用法。"""

import hashlib
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ethan import wechatexport as wx

ME, PEER, ROOM, BOB = "wxid_me", "wxid_peer", "123@chatroom", "wxid_bob"
KEY = "ab" * 32


class PlainDB:
    """测试用 opener：普通 sqlite，只读打开。"""
    opened = []

    def __init__(self, path, key):
        PlainDB.opened.append((str(path), key))
        self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = 1")

    def query(self, sql):
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def close(self):
        self.conn.close()


class FakeClient:
    def __init__(self, ok=True):
        self.calls, self.ok = [], ok

    stats = None

    def people_stats(self, people):
        self.stats = (self.stats or []) + list(people)
        return {"ok": True, "new": len(people)}

    def interview(self, session, scene, text, fmt=None, at=None, agent=None, key=None):
        self.calls.append((session, scene, text))
        self.keys = getattr(self, "keys", []) + [key]
        self.fmts = getattr(self, "fmts", []) + [(fmt, at)]
        return {"ok": True, "evidence": len(self.calls)} if self.ok else {"ok": False, "error": "boom"}


def make_db(path, sql_list):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    for s, args in sql_list:
        c.execute(s, args)
    c.commit()
    c.close()


def tbl(chat):
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


class Fixture:
    def __init__(self, base: Path, n_private=250):
        self.root = base / "db_storage"
        make_db(self.root / "session" / "session.db",
                [("CREATE TABLE SessionTable (username TEXT, sort_timestamp INT)", ())] +
                [("INSERT INTO SessionTable VALUES (?, 0)", (u,)) for u in
                 (PEER, ROOM, "gh_news", "filehelper", "brandsessionholder", "456@chatroom")])
        make_db(self.root / "contact" / "contact.db",
                [("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT)", ())] +
                [("INSERT INTO contact VALUES (?, ?, ?)", r) for r in
                 ((PEER, "peer昵称", "小王"), (ROOM, "读书会", ""), (BOB, "Bob", ""), ("456@chatroom", "潜水群", ""))])
        self.msg = self.root / "message" / "message_0.db"
        self.n = 0
        rows = [("CREATE TABLE Name2Id (user_name TEXT)", ())] + \
            [("INSERT INTO Name2Id (user_name) VALUES (?)", (u,)) for u in (ME, PEER, BOB)]  # rowid 1 2 3
        for chat in (PEER, ROOM, "456@chatroom", "gh_news", "filehelper"):
            rows.append((f'CREATE TABLE "{tbl(chat)}" (local_id INTEGER PRIMARY KEY, server_id INT, local_type INT, '
                         "create_time INT, real_sender_id INT, message_content BLOB, status INT)", ()))
        make_db(self.msg, rows)
        # 私聊 250 条（超过那个工具 read 的 200 条上限）：她、对方交替
        for i in range(n_private):
            self.add(PEER, 1 if i % 2 == 0 else 2, f"第{i}句话，我们聊聊伦敦的天气" if i % 2 == 0 else f"对方第{i}句")
        self.add(PEER, 2, "", lt=3)  # 对方发图片
        self.add(PEER, 1, "我的电话是 +44 7700 900123，邮箱 me@example.com 记一下")
        self.add(ROOM, 3, f"{BOB}:\n大家周末去爬山吗")
        self.add(ROOM, 1, "我去！我最喜欢爬山了")
        self.add(ROOM, 1, "<msg><appmsg><title>好呀我也想去看看</title><type>57</type></appmsg></msg>", lt=49 | (57 << 32))
        self.add("456@chatroom", 3, f"{BOB}:\n潜水群里只有别人说话")
        self.add("gh_news", 2, "公众号推送")
        self.add("filehelper", 1, "本人身份验收-001")

    def add(self, chat, sender, text, lt=1, ts=None):
        self.n += 1
        c = sqlite3.connect(self.msg)
        c.execute(f'INSERT INTO "{tbl(chat)}" (server_id, local_type, create_time, real_sender_id, message_content, status) '
                  "VALUES (?, ?, ?, ?, ?, 2)", (1000 + self.n, lt, ts or 1700000000 + self.n * 60, sender,
                                               text.encode("utf-8")))
        c.commit()
        c.close()

    def keys(self, base: Path):
        k = base / "keys.json"
        k.write_text(json.dumps({"keys": {r: {"enc_key": KEY} for r in
                                          ("session/session.db", "contact/contact.db", "message/message_0.db")}}))
        os.chmod(k, 0o600)
        return k


def snapshot(root: Path) -> dict:
    return {str(p): (p.stat().st_size, p.stat().st_mtime_ns, hashlib.md5(p.read_bytes()).hexdigest())
            for p in root.rglob("*") if p.is_file()}


class WeChatExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.fx = Fixture(self.base)
        self.keys = self.fx.keys(self.base)
        self.out = self.base / "wechat-export"
        self.state = self.base / "state.json"
        self.handoff = self.base / "handoff"
        self.tmp_root = self.base / "tmpcopies"
        self.tmp_root.mkdir()
        PlainDB.opened = []

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, mode="export", env=None, client=None, **kw):
        return wx.run(self.fx.root, self.keys, ME, mode, self.out, self.state, env=env or {}, client=client,
                      opener=PlainDB, handoff_dir=self.handoff, settings=self.base / "nosettings.json",
                      tmp_root=self.tmp_root, **kw)

    def lines(self, name):
        return [json.loads(x) for x in (self.out / name).read_text(encoding="utf-8").splitlines()]

    def test_sessions_and_full_history(self):
        rep = self.go()
        files = sorted(p.name for p in self.out.iterdir())
        self.assertEqual(files, ["小王.jsonl", "潜水群.jsonl", "读书会.jsonl"])  # 没有公众号、服务号、文件传输助手
        recs = self.lines("小王.jsonl")
        self.assertEqual(len(recs), 252)  # 全部历史，不止 200 条
        self.assertEqual(rep["chats"], 3)
        self.assertEqual(oct(self.out.stat().st_mode & 0o777), "0o700")
        self.assertEqual(oct((self.out / "小王.jsonl").stat().st_mode & 0o777), "0o600")
        self.assertTrue(recs[0]["is_her"])
        self.assertEqual(recs[0]["who"], "她")
        self.assertEqual(recs[1]["who"], "小王")  # 备注名优先
        self.assertFalse(recs[1]["is_her"])
        self.assertTrue(recs[0]["time"].startswith("2023-11-1"))
        self.assertEqual(recs[250]["text"], "[图片]")

    def test_group_sender_and_quote(self):
        self.go()
        recs = self.lines("读书会.jsonl")
        self.assertEqual([(r["who"], r["is_her"], r["text"]) for r in recs],
                         [("Bob", False, "大家周末去爬山吗"), ("她", True, "我去！我最喜欢爬山了"),
                          ("她", True, "好呀我也想去看看")])

    def test_filehelper_optional(self):
        self.go(filehelper=True)
        self.assertTrue((self.out / "文件传输助手.jsonl").exists())

    def test_incremental(self):
        self.go()
        self.fx.add(PEER, 1, "这是新的一句，下周我要去爱丁堡")
        rep = self.go()
        self.assertEqual(rep["new"], 1)
        recs = self.lines("小王.jsonl")
        self.assertEqual(len(recs), 253)
        self.assertEqual(recs[-1]["text"], "这是新的一句，下周我要去爱丁堡")
        self.assertEqual(self.go()["new"], 0)

    def test_dry_run_writes_nothing(self):
        rep = self.go("dry-run")
        self.assertFalse(self.out.exists())
        self.assertFalse(self.state.exists())
        per = {x["chat"]: x for x in rep["per_chat"]}
        self.assertEqual(per["小王"]["new"], 252)
        self.assertEqual(per["小王"]["her"], 126)
        self.assertTrue(per["读书会"]["send"])
        self.assertFalse(per["潜水群"]["send"])  # 她没说过话的群不交

    def test_readonly_and_temp_cleaned(self):
        before = snapshot(self.fx.root)
        self.go()
        self.go("dry-run")
        self.assertEqual(snapshot(self.fx.root), before)
        root = str(self.fx.root.resolve())
        self.assertTrue(PlainDB.opened)
        self.assertTrue(all(not p.startswith(root) for p, _ in PlainDB.opened))  # 只开副本
        self.assertTrue(all(k == KEY for _, k in PlainDB.opened))
        self.assertEqual(list(self.tmp_root.iterdir()), [])  # 临时副本删掉了

    def test_send_full_transcript_and_redact(self):  # 她 11-04：“难道别人跟我的对话不是我的记忆么！！！”
        client = FakeClient()
        rep = self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=client)
        sessions = {s for s, _, _ in client.calls}
        self.assertEqual(sessions, {"微信 · 小王", "微信群 · 读书会"})  # 潜水群不交
        self.assertTrue(all(sc == wx.SCENE for _, sc, _ in client.calls))
        self.assertTrue(all(f == "transcript" and a and len(a) == 10 for f, a in client.fmts))
        keys = {s: k for (s, _, _), k in zip(client.calls, client.keys)}  # 私聊带微信指纹（人物卡按它认人），群不带
        self.assertRegex(keys["微信 · 小王"], r"^[0-9a-f]{24}$")
        self.assertIsNone(keys["微信群 · 读书会"])
        text = "\n\n".join(t for _, _, t in client.calls)
        self.assertIn("【Bob】大家周末去爬山吗\n【她】我去！我最喜欢爬山了\n【她】好呀我也想去看看", text)  # 两边都在、标签对
        self.assertIn("【她】第0句话，我们聊聊伦敦的天气\n【小王】对方第1句\n【她】第2句话", text)
        self.assertIn("【小王】对方第249句", text)  # 别人说的整段交，不截
        self.assertNotIn("me@example.com", text)
        self.assertNotIn("7700 900123", text)
        self.assertNotIn("[图片]", text)
        self.assertNotIn("问：", text)
        for t in [t for _, _, t in client.calls]:
            self.assertTrue(t.startswith("〔"))  # 每批开头都有日期
            self.assertLessEqual(len(t), wx.CHUNK)
        self.assertGreater(rep["sent"], 100)
        # 私聊统计一起交给服务器（人物卡）：只交哈希，不交 username
        self.assertEqual([p["name"] for p in client.stats], ["小王"])
        self.assertEqual(rep["people"], 1)
        self.assertNotIn(PEER, json.dumps(client.stats))
        # 再跑：都交过了
        client2 = FakeClient()
        self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=client2)
        self.assertEqual(client2.calls, [])
        # 新消息只交新的，前面对方的话带进语境
        self.fx.add(PEER, 2, "你下周去哪")
        self.fx.add(PEER, 1, "下周我要去爱丁堡看朋友")
        client3 = FakeClient()
        self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=client3)
        self.assertEqual(len(client3.calls), 1)
        self.assertTrue(client3.calls[0][2].endswith("【小王】你下周去哪\n【她】下周我要去爱丁堡看朋友"))
        self.assertNotIn("第0句话", client3.calls[0][2])  # 交过的不再交

    def test_group_only_days_she_spoke(self):
        recs = [{"time": "2026-11-01T09:00:00", "is_her": False, "who": "Bob", "text": "群里闲聊", "type": 1},
                {"time": "2026-11-02T09:00:00", "is_her": False, "who": "Bob", "text": "周末爬山吗", "type": 1},
                {"time": "2026-11-02T09:01:00", "is_her": True, "who": "她", "text": "我去", "type": 1}]
        got = wx.transcript_blocks(recs, her_days_only=True)
        self.assertEqual([t for t, _ in got], ["〔2026-11-02〕\n【Bob】周末爬山吗\n【她】我去"])
        self.assertEqual(got[-1][1], 3)
        self.assertEqual(len(wx.transcript_blocks(recs)), 2)  # --all：每天都交

    def test_whitelist_and_all(self):
        c = FakeClient()
        self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=c, chats_filter=["读书会"])
        self.assertEqual({s for s, _, _ in c.calls}, {"微信群 · 读书会"})
        st = json.loads(self.state.read_text())
        self.assertEqual(st["chats"][PEER].get("sent", 0), 0)  # 没点名的不前进
        self.assertTrue(wx.should_send("456@chatroom", {"her": 0}, set(), True))
        self.assertFalse(wx.should_send("456@chatroom", {"her": 0}, set(), False))
        self.assertTrue(wx.should_send(PEER, {"her": 0}, set(), False))

    def test_failure_does_not_advance(self):
        self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=FakeClient(ok=False))
        st = json.loads(self.state.read_text())
        self.assertEqual(st["chats"][PEER].get("sent", 0), 0)
        c = FakeClient()
        self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=c)
        self.assertTrue(c.calls)

    def test_max_batches(self):
        c = FakeClient()
        rep = self.go("send", env={"ETHAN_CLOUD_TOKEN": "t"}, client=c, max_batches=1)
        self.assertEqual(len(c.calls), 1)
        self.assertIn("上限", rep["note"])
        st = json.loads(self.state.read_text())
        sent = [st["chats"][k].get("sent", 0) for k in (PEER, ROOM)]
        self.assertTrue(any(0 < s for s in sent))

    def test_no_key_only_files(self):
        rep = self.go("send", env={})
        self.assertTrue(rep["written"])
        self.assertIn("没有钥匙", rep["note"])
        f = Path(rep["written"][0])
        self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
        self.assertIn("【她】", f.read_text(encoding="utf-8"))
        st = json.loads(self.state.read_text())
        self.assertEqual(st["chats"][PEER].get("sent", 0), 0)

    def test_wrong_self_id(self):
        with self.assertRaises(RuntimeError):
            wx.run(self.fx.root, self.keys, "wxid_nobody", "export", self.out, self.state, env={}, opener=PlainDB,
                   tmp_root=self.tmp_root)
        self.assertFalse(self.out.exists())

    def test_keys_must_be_private_and_unique(self):
        os.chmod(self.keys, 0o644)
        with self.assertRaises(RuntimeError):
            wx.load_keys(self.keys)
        with self.assertRaises(RuntimeError):
            wx.key_for({"other.db": KEY}, self.fx.root, self.fx.msg)
        with open(self.fx.msg, "rb") as f:
            salt = f.read(16).hex()
        self.assertEqual(wx.key_for({salt: {"enc_key": KEY.upper()}}, self.fx.root, self.fx.msg), KEY)


class PieceTest(unittest.TestCase):
    def test_content_kinds(self):
        self.assertEqual(wx.content(34, 0, ""), "[语音]")
        self.assertEqual(wx.content(43, 0, ""), "[视频]")
        self.assertEqual(wx.content(47, 0, ""), "[表情]")
        self.assertEqual(wx.content(49, 2001, "<appmsg><title>恭喜发财</title></appmsg>"), "[红包]")
        self.assertEqual(wx.content(49, 0, "<msg><appmsg><title>文章</title><type>5</type></appmsg></msg>"), "[链接] 文章")
        self.assertTrue(wx.content(10000, 0, "<a>你撤回了一条消息</a>").startswith("[系统消息]"))

    def test_zstd_hook(self):
        raw = wx.ZSTD_MAGIC + b"xx"
        self.assertEqual(wx.unzstd(raw, lambda b: "解开了".encode()), "解开了".encode())
        self.assertEqual(wx.unzstd(b"plain"), b"plain")
        row = {"local_type": 1, "body": raw.hex(), "real_sender_id": 1, "create_time": 1700000000, "local_id": 1}
        self.assertEqual(wx.decode(row, PEER, {1: ME}, ME, {}, "message/message_0.db",
                                   lambda b: "压缩的文字".encode())["text"], "压缩的文字")

    def test_skip_rules(self):
        for u in ("gh_abc", "weixin", "brandsessionholder", "@placeholder_foldgroup", "x@openim_bad", "filehelper"):
            self.assertFalse(wx.keep_chat(u), u)
        for u in (PEER, ROOM, "abc@openim"):
            self.assertTrue(wx.keep_chat(u), u)
        self.assertTrue(wx.keep_chat("filehelper", filehelper=True))

    def test_cli_opener_key_on_stdin(self):
        calls = []

        def fake_run(cmd, input, capture_output, text):
            calls.append((cmd, input))
            return SimpleNamespace(returncode=0, stdout='[{"n":3}]', stderr="")
        db = wx.CliDB(Path("/tmp/x.db"), KEY, "/opt/homebrew/bin/sqlcipher", run=fake_run)
        self.assertEqual(db.query("SELECT 3 AS n"), [{"n": 3}])
        cmd, script = calls[-1]
        self.assertIn("-readonly", cmd)
        self.assertNotIn(KEY, " ".join(cmd))  # 钥匙不进命令行
        self.assertIn(f"PRAGMA key = \"x'{KEY}'\"", script)
        self.assertIn("PRAGMA query_only = 1", script)
        self.assertIn("cipher_compatibility = 4", script)
        self.assertIn(".mode json", script)

        def bad_run(cmd, input, capture_output, text):
            return SimpleNamespace(returncode=1, stdout="", stderr="file is not a database")
        with self.assertRaises(RuntimeError):
            wx.CliDB(Path("/tmp/x.db"), KEY, "sqlcipher", run=bad_run)

    def test_launchd_plist(self):
        text = wx.plist_text("/opt/homebrew/bin/python3.14", Path("/Users/d/Personal-OS"),
                             ["--db-root", "/a b/db_storage", "--keys", "/k.json", "--self-id", "wxid_me", "--send"])
        self.assertIn("<integer>0</integer><key>Hour</key><integer>20</integer><key>Minute</key><integer>43</integer>", text)
        self.assertIn("<string>ethan.wechatexport</string>", text)
        self.assertIn("<string>--send</string>", text)
        self.assertIn("/opt/homebrew/bin", text)
        with tempfile.TemporaryDirectory() as d:
            out = wx.install_launchd(["--send"], plist=Path(d) / "x.plist", platform="linux")
            self.assertIn("不是 Mac", out)


if __name__ == "__main__":
    unittest.main()
