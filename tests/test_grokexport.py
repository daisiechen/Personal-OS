"""Grok Bot App 数据导出（ontology-plan 1.8，ethan/grokexport.py）：认邮件、只认 x.ai 链接、要登录时留工作台提示、
宽松解析几种结构、只交她的话、去重、去联系方式、每月提醒、她自己发来的压缩包。"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from ethan import desk, files, grokexport as gx, imapmail, lifebase, mail
from ethan.channels import media
from ethan.memory import MemoryStore

os.environ.setdefault("ETHAN_NO_KICK", "1")  # 测试里不真起后台整理进程

SAID = "我以前在伦敦读书的时候特别喜欢早上去跑步"
SAID2 = "最近在想要不要换一个城市生活，伦敦太贵了"
BODY = ("Hi, your Grok data export is ready. Download it here: https://accounts.x.ai/download/abc123?token=xyz . "
        "This link expires in 7 days. Privacy: https://x.ai/legal/privacy-policy  Other: https://evil.com/download/abc "
        "https://evilx.ai/download/1 http://accounts.x.ai/download/plain")


def grok_backend(turns, cid="c1", title="伦敦生活"):
    """真实导出里的样子（prod-grok-backend.json）：conversations → {conversation, responses: [{response: {...}}]}。"""
    return {"conversations": [{"conversation": {"id": cid, "title": title, "create_time": "2026-10-20T09:00:00Z"},
                               "responses": [{"response": {"_id": f"{cid}-{k}", "message": text, "sender": who,
                                                           "create_time": {"$date": {"$numberLong": str(1760950800000 + k * 1000)}}}}
                                             for k, (who, text) in enumerate(turns)]}],
            "projects": []}


def zipped(obj, name="ttl/30d/export_data/u1/prod-grok-backend.json") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, json.dumps(obj, ensure_ascii=False))
        z.writestr("ttl/30d/export_data/u1/prod-mc-auth-mgmt-api.json", json.dumps({"user": {"email": "her@example.com"}}))
    return buf.getvalue()


class Resp:
    def __init__(self, data: bytes, ctype="application/zip", url="https://accounts.x.ai/download/abc123?token=xyz"):
        self.data, self.headers, self.url = io.BytesIO(data), {"Content-Type": ctype, "Content-Length": str(len(data))}, url

    def read(self, n=-1):
        return self.data.read(n)

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener_of(*results):
    calls = []

    def op(req, timeout=None):
        calls.append(req)
        r = results[min(len(calls) - 1, len(results) - 1)]
        if isinstance(r, Exception):
            raise r
        return r
    op.calls = calls
    return op


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def interviews(self):
        return [dict(r) for r in self.db.conn.execute("SELECT * FROM evidence WHERE source = 'interview' ORDER BY id")]


class MailTest(Base):
    def test_recognize_and_links(self):
        urls = gx.is_export_mail("xAI <noreply@x.ai>", "Your data export is ready", BODY)
        self.assertEqual(urls[0], "https://accounts.x.ai/download/abc123?token=xyz")
        self.assertTrue(all(u.startswith("https://") and "evil" not in u for u in urls))
        self.assertNotIn("https://x.ai/legal/privacy-policy", urls)  # 页脚链接不要
        self.assertEqual(gx.is_export_mail("Grok fan <a@evil.com>", "Your data export is ready", BODY), [])  # 不是 x.ai 发的
        self.assertEqual(gx.is_export_mail("xAI <noreply@x.ai>", "Welcome to SuperGrok", "Thanks https://x.ai/download/x"), [])
        self.assertEqual(gx.links("https://evil.com/download https://x.ai.evil.com/export http://x.ai/export"), [])

    def test_html_only_mail_keeps_xai_href(self):
        raw = ("From: xAI <noreply@x.ai>\r\nSubject: Your data is ready\r\nMessage-ID: <h1@x.ai>\r\nContent-Type: text/html\r\n\r\n"
               '<p>Click <a href="https://accounts.x.ai/export/download?id=9&amp;t=2">here</a> to download.</p>').encode()
        got = imapmail.parse(raw)
        self.assertIn("https://accounts.x.ai/export/download?id=9&t=2", got["body"])
        self.assertEqual(gx.is_export_mail(got["sender"], got["subject"], got["body"])[0], "https://accounts.x.ai/export/download?id=9&t=2")

    def _mail(self):
        mail.receive(self.db, {"items": [{"account": "Google", "id": "<x1@x.ai>", "sender": "xAI <noreply@x.ai>",
                                          "subject": "Your data export is ready", "received_at": "2026-11-04T10:00:00", "body": BODY}]})
        mail.process(self.store, router=None)
        return self.db.conn.execute("SELECT * FROM mail_items").fetchone()

    def test_mail_not_her_todo_and_queued(self):
        r = self._mail()
        self.assertEqual((r["attention"], r["fyi"]), (0, 1))
        self.assertEqual(r["reason"], gx.HANDLING)
        self.assertEqual(desk.count(self.db), 0)  # 不算要她处理的
        self.assertTrue(gx.has_pending(self.db))
        self.assertIn("https://accounts.x.ai/download/abc123?token=xyz", json.loads(self.db.conn.execute("SELECT urls FROM grok_jobs").fetchone()[0]))

    def test_login_page_leaves_desk_note(self):
        r = self._mail()
        op = opener_of(Resp(b"<!DOCTYPE html><html>Sign in to xAI</html>", "text/html"))
        got = gx.run_jobs(self.store, op, keys=())
        self.assertEqual(got[0]["status"], "login")
        self.assertNotIn("Cookie", dict(op.calls[0].header_items()))  # 不带她的登录凭据
        r = self.db.conn.execute("SELECT * FROM mail_items WHERE id = ?", (r["id"],)).fetchone()
        self.assertEqual(r["attention"], 1)
        self.assertIn("要你登录才能下载", r["reason"])
        self.assertIn("https://accounts.x.ai/download/abc123?token=xyz", r["reason"])
        self.assertEqual(desk.count(self.db), 1)
        self.assertFalse(gx.has_pending(self.db))

    def test_403_is_login(self):
        self._mail()
        err = urllib.error.HTTPError("https://accounts.x.ai/download/abc123", 403, "Forbidden", {}, None)
        self.assertEqual(gx.run_jobs(self.store, opener_of(err), keys=())[0]["status"], "login")

    def test_network_error_retries_then_gives_up(self):
        self._mail()
        err = urllib.error.URLError("timed out")
        self.assertEqual(gx.run_jobs(self.store, opener_of(err), keys=())[0]["status"], "pending")
        self.assertEqual(gx.run_jobs(self.store, opener_of(err), keys=()), [])  # 半小时内不重试
        self.db.conn.execute("UPDATE grok_jobs SET last_at = '2026-01-01T00:00:00+00:00', tries = 2")
        self.assertEqual(gx.run_jobs(self.store, opener_of(err), keys=())[0]["status"], "failed")
        self.assertEqual(desk.count(self.db), 1)

    def test_too_big(self):
        self.assertEqual(gx.fetch("https://accounts.x.ai/d", opener_of(Resp(b"PK\x03\x04" + b"0" * 100)), max_bytes=50)["state"], "too_big")
        self.assertEqual(gx.fetch("https://evil.com/d", opener_of(Resp(b"{}")))["state"], "bad")

    def test_download_and_import(self):
        r = self._mail()
        data = zipped(grok_backend([("human", SAID), ("assistant", "听起来很棒！伦敦的早晨跑步路线有很多"), ("human", "好的")]))
        got = gx.run_jobs(self.store, opener_of(Resp(data)), keys=())
        self.assertEqual(got[0]["status"], "done")
        ivs = self.interviews()
        self.assertEqual(len(ivs), 1)
        self.assertIn("会话：Grok Bot App 会话（2026-10-20）", ivs[0]["text"])
        self.assertIn(gx.SCENE, ivs[0]["text"])
        self.assertIn(f"\n【她】{SAID}\n", ivs[0]["text"])
        self.assertNotIn("【她】好的", ivs[0]["text"])  # 点头不交
        self.assertIn("\n【Grok】听起来很棒！伦敦的早晨跑步路线有很多", ivs[0]["text"])  # 两边都交
        self.assertIn("格式：整段对话；agent：Grok", ivs[0]["text"].split("\n", 1)[0])
        arch = list((self.root / "agent-archive").iterdir())
        self.assertEqual(len(arch), 1)
        self.assertEqual(oct(os.stat(arch[0]).st_mode & 0o777), "0o600")
        self.assertIn("导好了", self.db.conn.execute("SELECT reason FROM mail_items WHERE id = ?", (r["id"],)).fetchone()[0])


class ParseTest(Base):
    def test_three_shapes(self):
        a = grok_backend([("human", SAID), ("ASSISTANT", "好的")])
        b = [{"id": "b1", "title": "B", "created_at": 1761000000,
              "messages": [{"role": "user", "content": SAID, "timestamp": "2026-10-21T08:00:00"},
                           {"role": "assistant", "content": [{"type": "text", "text": "嗯"}]}]}]
        c = {"data": {"chats": [{"uuid": "c9", "name": "C", "conversation": [
            {"author": {"role": "human"}, "text": SAID}, {"author": {"role": "grok"}, "text": "是的"}]}]}}
        for obj, cid in ((a, "c1"), (b, "b1"), (c, "c9")):
            convs = gx.conversations(obj)
            self.assertEqual([x["id"] for x in convs], [cid], obj)
            turns = gx.all_turns(convs[0])
            self.assertEqual([t["text"] for t in turns if t["her"]], [SAID])
            self.assertEqual(len(turns), 2)
        self.assertTrue(gx.conversations(a)[0]["at"].startswith("2026-10-20"))
        self.assertTrue(gx.conversations(b)[0]["at"].startswith("2025-10-2"))  # 秒数时间戳

    def test_full_transcript_not_cut(self):  # Grok 的话不再截 120 字，整段交
        long = "你好，" + "很长" * 100
        conv = gx.conversations(grok_backend([("assistant", long), ("human", SAID), ("assistant", "x"), ("human", SAID2)]))[0]
        lines = gx.transcript_lines(gx.all_turns(conv), "Grok", her=lambda t: t["her"])
        self.assertEqual(lines, [f"【Grok】{long}", f"【她】{SAID}", "【Grok】x", f"【她】{SAID2}"])

    def test_not_conversation(self):
        self.assertEqual(gx.conversations({"user": {"email": "a@b.c"}, "list": [1, 2, 3]}), [])


class ImportTest(Base):
    def test_redact_and_cut(self):
        obj = grok_backend([("human", "我的邮箱是 her.name@gmail.com，电话 +44 7700 900123，有事找我" + "长" * 3000)])
        gx.import_bytes(self.store, zipped(obj), "test", keys=())
        text = self.interviews()[0]["text"]
        self.assertNotIn("her.name@gmail.com", text)
        self.assertNotIn("7700 900123", text)
        self.assertIn("已截断", text)

    def test_dedupe(self):
        one = zipped(grok_backend([("human", SAID), ("assistant", "嗯嗯")]))
        self.assertEqual(gx.import_bytes(self.store, one, "a", keys=())["sent"], 1)
        self.assertIn("skipped", gx.import_bytes(self.store, one, "a", keys=()))  # 同一包
        two = zipped(grok_backend([("human", SAID), ("assistant", "嗯嗯"), ("human", SAID2)]))
        got = gx.import_bytes(self.store, two, "b", keys=())  # 续聊后再导：只交新增的
        self.assertEqual((got["new_turns"], got["sent"]), (1, 1))
        ivs = self.interviews()
        self.assertEqual(len(ivs), 2)
        self.assertNotIn(SAID, ivs[1]["text"])
        self.assertIn(SAID2, ivs[1]["text"])

    def test_her_file_via_media_and_resolves_login(self):
        mail.receive(self.db, {"items": [{"account": "G", "id": "<x2@x.ai>", "sender": "xAI <noreply@x.ai>",
                                          "subject": "Your data export is ready", "received_at": "", "body": BODY}]})
        mail.process(self.store, router=None)
        gx.run_jobs(self.store, opener_of(Resp(b"<html>login</html>", "text/html")), keys=())
        self.assertEqual(desk.count(self.db), 1)
        day = self.root / "media" / "2026-11-04"
        day.mkdir(parents=True)
        (day / "101010-1.zip").write_bytes(zipped(grok_backend([("human", SAID)])))
        (day / "101011-2.zip").write_bytes(zipped({"a": 1}, "photos/list.json"))  # 别的压缩包
        (day / "note.json").write_text(json.dumps({"x": 1}), encoding="utf-8")  # 普通 JSON 不碰
        got = gx.scan_media(self.store, keys=())
        self.assertEqual(sorted(g["file"] for g in got), ["101010-1.zip", "101011-2.zip"])
        self.assertEqual(len(self.interviews()), 1)
        self.assertEqual(desk.count(self.db), 0)  # “要你登录”的那条收起
        self.assertEqual(gx.scan_media(self.store, keys=()), [])  # 看过的不再看
        self.assertIn(files.sha(day / "101010-1.zip"), gx.grok_shas(self.db))

    def test_media_zip_kind(self):
        self.assertEqual(media.file_kind({"file_name": "export.zip"}), "zip")
        self.assertEqual(media.document_suffix("zip", "export.zip"), ".zip")


class MonthlyTest(Base):
    def test_monthly_todo_once(self):
        got = gx.monthly_todo(self.db, date(2026, 12, 1))
        self.assertTrue(got["made"])
        self.assertIn("https://accounts.x.ai/data", got["title"])
        self.assertIn("Download account data", got["title"])
        self.assertFalse(gx.monthly_todo(self.db, date(2026, 12, 2))["made"])
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE '%accounts.x.ai%'").fetchone()[0], 1)

    def test_skip_if_just_imported(self):
        self.db.conn.execute(gx.SCHEMA[3])
        self.db.conn.execute("INSERT INTO grok_meta VALUES ('last_import', '2026-11-25T10:00:00+00:00')")
        self.assertFalse(gx.monthly_todo(self.db, date(2026, 12, 1))["made"])

    def test_source_health(self):
        # Grok 导出登记成 S20g（S20 留给 Codex 导出，2026-11-04 统一巡检）；每月点一次，45 天没新的才算断档
        s = next(x for x in lifebase.source_health(self.db) if x["code"] == "S20g")
        self.assertEqual(s["state"], "not_connected")
        gx.import_bytes(self.store, zipped(grok_backend([("human", SAID)])), "t", keys=())
        s = next(x for x in lifebase.source_health(self.db) if x["code"] == "S20g")
        self.assertEqual((s["state"], s["every"]), ("ok", 45))


if __name__ == "__main__":
    unittest.main()


class RedirectTest(unittest.TestCase):
    def test_follows_308_on_xai_only(self):
        import io as _io, urllib.error
        from email.message import Message
        calls = []

        class Resp(_io.BytesIO):
            headers = {"Content-Type": "application/zip"}
            def geturl(self):
                return calls[-1]
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def opener(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                h = Message(); h["Location"] = "/download?t=1"
                raise urllib.error.HTTPError(req.full_url, 308, "moved", h, None)
            return Resp(b"PK\x03\x04rest")
        got = gx.fetch("https://accounts.x.ai/download/?t=1", opener=opener)
        self.assertEqual(got["state"], "ok")
        self.assertEqual(calls[-1], "https://accounts.x.ai/download?t=1")

        def evil(req, timeout=None):
            h = Message(); h["Location"] = "https://evil.example.com/x"
            raise urllib.error.HTTPError(req.full_url, 308, "moved", h, None)
        self.assertEqual(gx.fetch("https://accounts.x.ai/download/", opener=evil)["state"], "bad")
