"""Grok Bot App 的数据导出：邮件到了 小伴 自动下载、导入（ontology-plan 1.8，数据源 S20）。

她在 https://accounts.x.ai/data 点 “Download account data”，xAI 把下载链接发到她邮箱。服务器收邮件时（ethan/mail.py）：
1. 认出这封：发件人是 x.ai / xAI，主题或正文说导出、下载、数据；只认正文里 x.ai 域名下的 https 链接。
   这封在工作台“邮件和留言”里算“知道一下：小伴 在处理”，不算要她处理的事。
2. 下载（后台，python3 -m ethan.grokexport --data <目录> --jobs）：urllib 直接下，不带她的任何登录凭据；
   最多 500MB、超时 120 秒。拿到登录页 / 401 / 403 就不硬来：把这封改成要她看，写清“要你登录才能下载：点链接下好，
   原样发给 小伴”。她用 Telegram / 微信发来的压缩包（或服务器上 --file）走同一个导入。
3. 导入：zip 里所有 JSON 宽松解析（不认死格式）：找“含消息数组的对象”当会话，按 sender / role / author 认她
   （human / user），按会话交整段对话：每行标【她】/【Grok】，
   两边都交、不截短（单条超长 2000 字截断），经访谈记录（interviews.receive，format=transcript，直接写库）进记忆，
   会话名“Grok Bot App 会话（日期）”，场合 SCENE；她说的算原话，Grok 说的整理时记成“agent 转述”（只当资料）。
   她的话筛法同 Codex（agentexport.clean：去点头、代码、截长）；都去邮箱电话。原始包原样存档到 <数据目录>/agent-archive/（600，不进仓库；有密钥就加密）。
   每条消息记指纹（grok_seen），重复导入、续聊后再导，只交新增的。
4. 每月提醒：每月第一次每晚整理时在待办里放一条“去 accounts.x.ai/data 点 Download account data”。
"""

import argparse
import fcntl
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import clock
from .agentexport import pack_transcript, transcript_lines

AGENT = "Grok Bot App"
SCENE = "她在 Grok Bot App 里说的（agent 记忆导出）"
DATA_URL = "https://accounts.x.ai/data"
MAX_BYTES = 500 * 1024 * 1024
JSON_MAX = 300 * 1024 * 1024  # zip 里 JSON 解开后的总量上限（防压缩炸弹）
TIMEOUT = 120
MAX_TRIES = 3
RETRY_MIN = 30
CONF = Path.home() / ".config" / "ethan"
KEYS = (CONF / "agent-archive.key", CONF / "backup.key")
SCHEMA = (
    "CREATE TABLE IF NOT EXISTS grok_jobs (id INTEGER PRIMARY KEY, mail_id INTEGER UNIQUE, urls TEXT NOT NULL, "
    "status TEXT NOT NULL DEFAULT 'pending', tries INTEGER NOT NULL DEFAULT 0, last_at TEXT, note TEXT, created_at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS grok_files (sha TEXT PRIMARY KEY, origin TEXT, is_grok INTEGER NOT NULL, at TEXT NOT NULL, result TEXT)",
    "CREATE TABLE IF NOT EXISTS grok_seen (fp TEXT PRIMARY KEY, conv TEXT, at TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS grok_meta (key TEXT PRIMARY KEY, value TEXT)",
)

# ---------- 认邮件、取链接 ----------
SENDER = re.compile(r"(@|\.|\b)x\.ai\b|\bxai\b", re.I)
TOPIC = re.compile(r"data export|export(ed)? (is )?ready|your export|download (your )?(account )?data|account data|your data|"
                   r"archive|takeout|数据|导出|下载", re.I)
URL = re.compile(r"https?://[^\s<>\"'()\[\]{}]+", re.I)
STRONG = re.compile(r"download|export|archive|takeout|file|token|signed", re.I)
WEAK = re.compile(r"data", re.I)


def is_xai_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host == "x.ai" or host.endswith(".x.ai")


def links(text: str) -> list:
    """正文里 x.ai 域名下的 https 下载链接，像下载的排前面；页脚的隐私政策这类不要。"""
    strong, weak, seen = [], [], set()
    for raw in URL.findall(text or ""):
        url = raw.rstrip(".,;:!?。）」”'")
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" or not is_xai_host(p.hostname or "") or url in seen:
            continue
        seen.add(url)
        rest = f"{p.path}?{p.query}"
        if STRONG.search(rest):
            strong.append(url)
        elif WEAK.search(rest):
            weak.append(url)
    return (strong + weak)[:3]


def is_export_mail(sender: str, subject: str, body: str) -> list:
    """是 xAI 的数据导出邮件就返回下载链接（可能几个），不是返回 []。"""
    if not SENDER.search(sender or ""):
        return []
    if not TOPIC.search(f"{subject}\n{(body or '')[:3000]}"):
        return []
    return links(body)


def ensure(db):
    for s in SCHEMA:
        db.conn.execute(s)


def _now(now: datetime = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def queue(db, mail_id: int, urls: list) -> None:
    ensure(db)
    db.conn.execute("INSERT OR IGNORE INTO grok_jobs (mail_id, urls, created_at) VALUES (?, ?, ?)",
                    (int(mail_id), json.dumps(urls), _now()))
    db.conn.commit()


HANDLING = "xAI 的数据导出：小伴 在自动下载、导进记忆，不用你管"


# ---------- 下载 ----------
def fetch(url: str, opener=None, max_bytes: int = MAX_BYTES, timeout: int = TIMEOUT, _hops: int = 0) -> dict:
    """{"state": ok / login / too_big / error / bad, "data": bytes, "error": 说明}。不带任何 cookie 或令牌。"""
    p = urllib.parse.urlparse(url)
    if p.scheme != "https" or not is_xai_host(p.hostname or ""):
        return {"state": "bad", "error": "不是 x.ai 的 https 链接"}
    opener = opener or urllib.request.urlopen
    req = urllib.request.Request(url, headers={"User-Agent": "Ethan/1.0 (data export import)"})
    try:
        with opener(req, timeout=timeout) as resp:
            final = resp.geturl() if hasattr(resp, "geturl") else url
            if urllib.parse.urlparse(final).scheme != "https":
                return {"state": "bad", "error": "被转到了非 https 的地址"}
            size = int((resp.headers.get("Content-Length") or 0) if resp.headers else 0)
            if size > max_bytes:
                return {"state": "too_big", "error": f"{size // 1024 // 1024}MB"}
            ctype = ((resp.headers.get("Content-Type") if resp.headers else "") or "").lower()
            buf = io.BytesIO()
            while True:
                chunk = resp.read(1024 * 1024)
                if not chunk:
                    break
                buf.write(chunk)
                if buf.tell() > max_bytes:
                    return {"state": "too_big", "error": f"超过 {max_bytes // 1024 // 1024}MB"}
    except urllib.error.HTTPError as e:
        loc = e.headers.get("Location") if e.headers else None
        if e.code in (301, 302, 303, 307, 308) and loc and _hops < 3:  # Python 3.10 的 urllib 不跟 308（11-04 xAI 下载链接就是 308）
            return fetch(urllib.parse.urljoin(url, loc), opener, max_bytes, timeout, _hops + 1)
        if e.code in (401, 403):
            return {"state": "login", "error": f"HTTP {e.code}"}
        return {"state": "error", "error": f"HTTP {e.code}"}
    except (urllib.error.URLError, OSError, ValueError) as e:
        return {"state": "error", "error": f"{type(e).__name__}: {str(e)[:120]}"}
    data = buf.getvalue()
    head = data[:512].lstrip().lower()
    if "text/html" in ctype or head.startswith((b"<!doctype", b"<html")):
        return {"state": "login", "error": "拿到的是网页（多半是登录页）"}
    if data[:4] == b"PK\x03\x04" or head[:1] in (b"{", b"["):
        return {"state": "ok", "data": data}
    return {"state": "bad", "error": "下到的文件认不出（不是 zip 也不是 JSON）"}


# ---------- 宽松解析 ----------
LIST_KEYS = ("messages", "responses", "conversation", "chat_messages", "turns", "items", "history", "entries")
ROLE_KEYS = ("sender", "role", "author", "from", "speaker")
TEXT_KEYS = ("message", "content", "text", "body", "query", "prompt")
TIME_KEYS = ("create_time", "created_at", "createdAt", "timestamp", "time", "date", "created", "updated_at")
ID_KEYS = ("id", "_id", "conversation_id", "conversationId", "uuid")
WRAPS = ("response", "message", "item", "node", "data")
HUMAN = {"human", "user", "me", "她", "mia"}
SKIP_ROLES = {"system", "tool", "function"}


def _unwrap(m):
    for _ in range(3):
        if not isinstance(m, dict) or any(k in m for k in ROLE_KEYS):
            return m
        inner = next((m[k] for k in WRAPS if isinstance(m.get(k), dict)), None)
        if inner is None:
            return m
        m = inner
    return m


def _role(m) -> str:
    for k in ROLE_KEYS:
        v = m.get(k)
        if isinstance(v, dict):
            v = v.get("role") or v.get("type") or v.get("name")
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return ""


def _text(m) -> str:
    for k in TEXT_KEYS:
        v = m.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            v = v.get("text") if isinstance(v.get("text"), str) else v.get("parts")
            if isinstance(v, str) and v.strip():
                return v.strip()
        if isinstance(v, list):
            parts = [x if isinstance(x, str) else x.get("text") for x in v if isinstance(x, (str, dict))]
            t = "\n".join(p for p in parts if isinstance(p, str) and p.strip()).strip()
            if t:
                return t
    return ""


def _time(v) -> str:
    """各种写法的时间 → UTC ISO；认不出给空。"""
    if isinstance(v, dict):
        v = v.get("$date", v.get("$numberLong", v.get("seconds")))
        if isinstance(v, dict):
            v = v.get("$numberLong")
    if isinstance(v, str) and re.fullmatch(r"\d{10,13}(\.\d+)?", v.strip()):
        v = float(v)
    try:
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            v = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(v, timezone.utc).isoformat(timespec="seconds")
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v.strip()):
            t = datetime.fromisoformat(v.strip().replace("Z", "+00:00").replace(" ", "T", 1))
            return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, OverflowError, OSError):
        return ""
    return ""


def _first(d: dict, keys) -> object:
    return next((d[k] for k in keys if d.get(k) not in (None, "", [], {})), None)


def _messages(lst) -> list:
    out = []
    for raw in lst:
        m = _unwrap(raw)
        if not isinstance(m, dict):
            continue
        role, text = _role(m), _text(m)
        if role and text:
            mid = _first(m, ("_id", "id", "uuid", "message_id"))
            out.append({"role": role, "text": text, "at": _time(_first(m, TIME_KEYS)), "id": str(mid) if mid is not None else ""})
    return out


def _conv_list(d: dict):
    for k in LIST_KEYS:
        v = d.get(k)
        if isinstance(v, list) and v:
            msgs = _messages(v)
            if msgs and len(msgs) * 2 >= len(v):
                return msgs
    return None


def conversations(obj) -> list:
    """任意 JSON 里找会话：[{"id", "title", "at", "messages": [{role, text, at, id}]}]。认出的会话不再往里找。"""
    out, stack = [], [obj]
    while stack:
        d = stack.pop()
        if isinstance(d, list):
            stack.extend(reversed(d))
            continue
        if not isinstance(d, dict):
            continue
        msgs = _conv_list(d)
        if msgs is None:
            stack.extend(reversed([v for v in d.values() if isinstance(v, (dict, list))]))
            continue
        meta = d["conversation"] if isinstance(d.get("conversation"), dict) else d
        if all(m["at"] for m in msgs):
            msgs = sorted(msgs, key=lambda m: m["at"])
        cid = _first(meta, ID_KEYS) or _first(d, ID_KEYS)
        title = _first(meta, ("title", "name", "subject"))
        at = _time(_first(meta, TIME_KEYS)) or next((m["at"] for m in msgs if m["at"]), "")
        if cid is None:
            cid = "h:" + hashlib.sha256(f"{title}|{msgs[0]['text']}".encode()).hexdigest()[:16]
        out.append({"id": str(cid), "title": str(title or "")[:60], "at": at, "messages": msgs})
    return out


def is_human(role: str) -> bool:
    return role in HUMAN


def all_turns(conv: dict) -> list:
    """两边的每一段：{"text", "her", "fp", "at"}；她那段的指纹和以前只交她的话时一样（以前导过的不重交）。"""
    out = []
    for m in conv["messages"]:
        if m["role"] in SKIP_ROLES:
            continue
        fp = hashlib.sha256(f"{conv['id']}|{m['id'] or m['at']}|{m['text']}".encode()).hexdigest()[:20]
        out.append({"text": m["text"], "her": is_human(m["role"]), "fp": fp, "at": m["at"]})
    return out


def load_json(data: bytes, limit: int = JSON_MAX) -> list:
    """zip 或 JSON 字节 → 解析出来的对象列表。zip 只在内存里读 .json / .jsonl，不解到磁盘。"""
    objs = []
    if data[:4] == b"PK\x03\x04":
        total = 0
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.is_dir() or not info.filename.lower().endswith((".json", ".jsonl")):
                    continue
                total += info.file_size
                if total > limit:
                    break
                objs += _parse(z.read(info), info.filename)
    else:
        objs += _parse(data, "")
    return objs


def _parse(raw: bytes, name: str) -> list:
    text = raw.decode("utf-8", "replace").lstrip("﻿")
    try:
        return [json.loads(text)]
    except ValueError:
        out = []
        for line in text.splitlines():  # JSONL
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out


# ---------- 存档 ----------
def archive(root: Path, data: bytes, now: datetime = None, keys=KEYS) -> str:
    d = Path(root) / "agent-archive"
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    ext = ".zip" if data[:4] == b"PK\x03\x04" else ".json"
    name = f"grok-{clock.her_now(now=now):%Y%m%d-%H%M%S}{ext}"
    key = next((k for k in keys if Path(k).is_file()), None)
    if key:
        try:
            from .backup import encrypt
            data, name = encrypt(data, Path(key)), name + ".enc"
        except (RuntimeError, OSError, subprocess.SubprocessError):
            pass  # openssl 用不了就先留没加密的（数据目录里、只有 ethan 能读）
    f = d / name
    f.write_bytes(data)
    os.chmod(f, 0o600)
    return str(f)


# ---------- 导入 ----------
def import_bytes(store, data: bytes, origin: str, now: datetime = None, keys=KEYS, need_grok: bool = False) -> dict:
    """一份导出（zip / JSON）→ 存档 + 她说的新话进访谈记录。need_grok：来路不明的（她发来的文件）要看着像 Grok 的才导。"""
    from . import interviews
    db = store.db
    ensure(db)
    sha = hashlib.sha256(data).hexdigest()[:20]
    old = db.conn.execute("SELECT is_grok FROM grok_files WHERE sha = ?", (sha,)).fetchone()
    if old is not None:
        return {"ok": bool(old["is_grok"]), "skipped": "这份导过了" if old["is_grok"] else "这份看过，不是 Grok 的导出"}
    try:
        objs = load_json(data)
    except (zipfile.BadZipFile, OSError, ValueError) as e:
        return {"ok": False, "error": f"打不开：{type(e).__name__}"}
    convs = [c for o in objs for c in conversations(o)]
    grokish = data[:4] == b"PK\x03\x04" and any("grok" in n.lower() for n in zipfile.ZipFile(io.BytesIO(data)).namelist()) \
        or b"grok" in data[:2_000_000].lower()
    if not convs or (need_grok and not grokish):
        db.conn.execute("INSERT OR REPLACE INTO grok_files (sha, origin, is_grok, at, result) VALUES (?, ?, 0, ?, ?)",
                        (sha, origin, _now(now), "里面没找到 Grok 的会话"))
        db.conn.commit()
        return {"ok": False, "error": "里面没找到 Grok 的会话"}
    report = {"ok": True, "conversations": len(convs), "new_turns": 0, "sent": 0, "errors": [],
              "archive": archive(store.root, data, now, keys)}
    seen = {r["fp"] for r in db.conn.execute("SELECT fp FROM grok_seen")}
    for c in convs:
        new = [t for t in all_turns(c) if t["fp"] not in seen]
        if not new:
            continue
        report["new_turns"] += sum(t["her"] for t in new)
        day = clock.her_today(now=datetime.fromisoformat(c["at"])) if c["at"] else clock.her_today(now=now)
        header = f"〔{AGENT} 会话 {day.isoformat()}" + (f"「{c['title']}」" if c["title"] else "") + "〕"
        ok = True
        for part in pack_transcript(transcript_lines(new, "Grok", her=lambda t: t["her"]), header):
            code, res = interviews.receive(store, {"session": f"{AGENT} 会话（{day.isoformat()}）", "scene": SCENE,
                                                    "text": part, "at": c["at"] or _now(now), "format": "transcript",
                                                    "agent": "Grok"}, now=now)
            if code != 200 or not res.get("ok"):
                ok = False
                report["errors"].append(f"{c['id']}: {res.get('error', res)}")
                break
            report["sent"] += part.count("\n【她】")
        if ok:  # 一个会话全交成功才记指纹，失败的下次重交（服务器对同样的内容不重复记）
            db.conn.executemany("INSERT OR IGNORE INTO grok_seen (fp, conv, at) VALUES (?, ?, ?)",
                                [(t["fp"], c["id"], _now(now)) for t in new])
            seen.update(t["fp"] for t in new)
    if not report["errors"]:
        db.conn.execute("INSERT OR REPLACE INTO grok_files (sha, origin, is_grok, at, result) VALUES (?, ?, 1, ?, ?)",
                        (sha, origin, _now(now), json.dumps({k: report[k] for k in ("conversations", "new_turns", "sent")})))
    db.conn.execute("INSERT OR REPLACE INTO grok_meta (key, value) VALUES ('last_import', ?)", (_now(now),))
    db.conn.commit()
    resolve_login(db)
    return report


# ---------- 邮件来的：下载 ----------
def _set_mail(db, mail_id: int, reason: str, attention: bool):
    from . import desk
    desk.ensure(db)
    if attention:  # 要她登录才能下：改成要她看；邮件已经归档走了的放回收件箱，方便她点链接
        db.conn.execute("UPDATE mail_items SET attention = 1, fyi = 0, reason = ?, desk_state = 'open', digested = 0, "
                        "restore = CASE WHEN restore = 2 THEN 2 ELSE 1 END WHERE id = ?", (reason, mail_id))
    else:
        db.conn.execute("UPDATE mail_items SET reason = ? WHERE id = ?", (reason, mail_id))


def login_note(url: str) -> str:
    return (f"xAI 的导出要你登录才能下载：点这个链接 {url} 下载后，把文件原样发给 小伴（Telegram 直接发 zip 就行），"
            "他自动导进记忆")


def resolve_login(db):
    """她自己把文件发来、导好了：之前“要你登录才能下载”的那几条收起。"""
    ensure(db)
    rows = db.conn.execute("SELECT mail_id FROM grok_jobs WHERE status = 'login'").fetchall()
    if not rows:
        return 0
    from . import desk
    desk.ensure(db)
    for r in rows:
        db.conn.execute("UPDATE mail_items SET desk_state = 'closed', reason = 'xAI 的数据导出：你发来的文件已导进记忆' WHERE id = ?",
                        (r["mail_id"],))
    db.conn.execute("UPDATE grok_jobs SET status = 'done', note = '她发来文件导好了' WHERE status = 'login'")
    db.conn.commit()
    return len(rows)


def due_jobs(db, now: datetime = None) -> list:
    ensure(db)
    now = now or datetime.now(timezone.utc)
    out = []
    for r in db.conn.execute("SELECT * FROM grok_jobs WHERE status = 'pending' AND tries < ? ORDER BY id", (MAX_TRIES,)):
        if r["last_at"] and (now - datetime.fromisoformat(r["last_at"])).total_seconds() < RETRY_MIN * 60:
            continue
        out.append(dict(r))
    return out


def run_jobs(store, opener=None, now: datetime = None, keys=KEYS) -> list:
    db = store.db
    out = []
    for job in due_jobs(db, now):
        tries = job["tries"] + 1
        db.conn.execute("UPDATE grok_jobs SET tries = ?, last_at = ? WHERE id = ?", (tries, _now(now), job["id"]))
        db.conn.commit()
        urls = json.loads(job["urls"])
        states, got = [], None
        for url in urls:
            res = fetch(url, opener)
            states.append((url, res))
            if res["state"] == "ok":
                got = import_bytes(store, res["data"], f"mail:{job['mail_id']}", now, keys)
                if got.get("ok"):
                    break
        if got and got.get("ok") and not got.get("errors"):
            status, note = "done", json.dumps({k: got.get(k) for k in ("conversations", "new_turns", "sent", "skipped")}, ensure_ascii=False)
            _set_mail(db, job["mail_id"], f"xAI 的数据导出：小伴 导好了（交了 {got.get('sent', 0)} 段你说的话）", False)
        elif any(r["state"] == "login" for _, r in states) and not any(r["state"] == "error" for _, r in states):
            url = next(u for u, r in states if r["state"] == "login")
            status, note = "login", "要登录"
            _set_mail(db, job["mail_id"], login_note(url), True)
        elif any(r["state"] == "too_big" for _, r in states):
            status, note = "failed", "太大"
            _set_mail(db, job["mail_id"], f"xAI 的导出太大（超过 {MAX_BYTES // 1024 // 1024}MB），小伴 没下：点邮件里的链接下载后，"
                      "解压出里面的 JSON 发给 小伴 就行", True)
        elif tries >= MAX_TRIES:
            status, note = "failed", "; ".join(f"{r['state']} {r.get('error', '')}" for _, r in states)[:300]
            _set_mail(db, job["mail_id"], "xAI 的导出 小伴 试了 3 次没下成：点邮件里的链接下载后，把文件原样发给 小伴", True)
        else:
            status, note = "pending", "; ".join(f"{r['state']} {r.get('error', '')}" for _, r in states)[:300]
        db.conn.execute("UPDATE grok_jobs SET status = ?, note = ? WHERE id = ?", (status, note, job["id"]))
        db.conn.commit()
        out.append({"mail_id": job["mail_id"], "status": status, "note": note})
    return out


# ---------- 她直接发来的文件（Telegram / 微信存在 media/ 下） ----------
def scan_media(store, limit_bytes: int = MAX_BYTES, keys=KEYS) -> list:
    """media 下的 zip 和 JSON：看着像 Grok 导出的就导入。导过的（不管是不是 Grok）按内容哈希跳过。"""
    media = Path(store.root) / "media"
    if not media.is_dir():
        return []
    ensure(store.db)
    out = []
    for p in sorted(media.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in (".zip", ".json") or p.stat().st_size > limit_bytes:
            continue
        data = p.read_bytes()
        if p.suffix.lower() == ".json" and b"grok" not in data[:2_000_000].lower():
            continue  # 普通 JSON 文件走文件那边（ethan/files.py）
        sha = hashlib.sha256(data).hexdigest()[:20]
        if store.db.conn.execute("SELECT 1 FROM grok_files WHERE sha = ?", (sha,)).fetchone():
            continue
        try:
            got = import_bytes(store, data, f"media:{p.relative_to(media).as_posix()}", keys=keys, need_grok=True)
        except Exception as e:  # 一个文件出错不挡别的
            got = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
        out.append({"file": p.name, **{k: v for k, v in got.items() if k != "archive"}})
    return out


def grok_shas(db) -> set:
    ensure(db)
    return {r["sha"][:16] for r in db.conn.execute("SELECT sha FROM grok_files WHERE is_grok = 1")}  # files.sha 只取 16 位


# ---------- 每月提醒 ----------
def monthly_todo(db, today=None) -> dict:
    """每月第一次每晚整理：待办里放一条，提醒她去点导出。这个月已经放过、她删过这个月这条、最近 20 天导过的，都不放。"""
    from datetime import timedelta
    from . import locks
    ensure(db)
    today = today or clock.her_today()
    key = f"reminded:{today:%Y-%m}"
    if db.conn.execute("SELECT 1 FROM grok_meta WHERE key = ?", (key,)).fetchone():
        return {"made": False, "why": "这个月放过了"}
    title = f"去 {DATA_URL} 点 Download account data（{today.month} 月；点完就不用管了，邮件到了 小伴 自动导）"
    last = db.conn.execute("SELECT value FROM grok_meta WHERE key = 'last_import'").fetchone()
    if last and datetime.fromisoformat(last["value"]).date() >= today - timedelta(days=20):
        why = "最近 20 天刚导过"
    elif title in locks.deleted_titles(db, "task"):
        why = "她删过这个月这条"
    else:
        from . import lifebase
        lifebase.ensure(db)
        tid = db.add_task(title, due=today.isoformat())
        db.conn.execute("UPDATE tasks SET owner = 'mia', source_type = 'agent-export', source_ref = ?, status = 'open' WHERE id = ?",
                        (f"grok-export:{today:%Y-%m}", tid))
        why = f"待办 #{tid}"
    db.conn.execute("INSERT OR REPLACE INTO grok_meta (key, value) VALUES (?, ?)", (key, why))
    db.conn.commit()
    return {"made": why.startswith("待办"), "why": why, "title": title}


# ---------- 后台跑 ----------
def has_pending(db) -> bool:
    try:
        ensure(db)
        return bool(due_jobs(db))
    except Exception:
        return False


def kick(root, what: str = "--jobs", spawn=None) -> bool:
    from .bgwork import kick as _kick
    return bool(_kick(root, "grokexport", [what], spawn))


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Grok Bot App 的数据导出：下载、导进记忆（ontology-plan 1.8）")
    p.add_argument("--data", type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--jobs", action="store_true", help="下载邮件里排着的导出")
    g.add_argument("--scan", action="store_true", help="看 media/ 下她发来的压缩包")
    g.add_argument("--file", type=Path, help="导一个文件（zip 或 JSON）")
    args = p.parse_args(argv)
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    lock = open(Path(args.data) / ".grokexport.lock", "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("另一个导入在跑，这次不跑", flush=True)
            return 0
        if args.jobs:
            res = run_jobs(store)
        elif args.scan:
            res = scan_media(store)
        else:
            res = import_bytes(store, args.file.read_bytes(), f"file:{args.file.name}")
            res.pop("archive", None)
        print(f"[{clock.stamp(clock.her_now())}] " + json.dumps(res, ensure_ascii=False), flush=True)
        return 0
    finally:
        lock.close()
        store.close()


if __name__ == "__main__":
    sys.exit(main())
