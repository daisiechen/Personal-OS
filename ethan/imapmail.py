"""服务器直接收邮件（她 2026-10-30 选的 B：电脑关机、人在外面也不停）。

每 10 分钟（ethan2-mail.timer）：
1. 按 ~/.config/ethan/mail_accounts.json 里的邮箱（她自己用 --add 录，专用密码不进聊天和仓库），用 IMAP 读收件箱里的新邮件
   （第一次只看最近 3 天；以后从上次读到的地方接着读，停多久都不漏）。读时不改已读状态。
2. 交给 ethan/mail.py 排队、分类（规则优先、模型兜底）、记证据、建待办和日程。
3. 照分类办：不需要她看的，标已读、移进该邮箱的“小伴整理/<类别>”文件夹；需要她看的，留在收件箱、不动。
   每次移动记进 mail_moves 表，--undo 能放回原处并恢复原来的已读状态。不删、不发、不回。
支持：Gmail、iCloud、126/163、QQ、某大学（用各自的“专用密码 / 授权码”）。微软的学校邮箱多半不让用密码登录，建议设成自动转发到 Gmail。

录邮箱（她在服务器上自己跑，一个一个加，密码输入时不显示）：
    python3 -m ethan.imapmail --add
看录了哪些（不显示密码）：python3 -m ethan.imapmail --list
试一次、不移动：python3 -m ethan.imapmail --data /home/ethan/data --dry-run
撤回最近 24 小时的移动：python3 -m ethan.imapmail --data /home/ethan/data --undo
"""

import argparse
import base64
import email
import email.policy
import getpass
import imaplib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import mail

CONFIG = Path.home() / ".config" / "ethan" / "mail_accounts.json"
PRESETS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com", "icloud.com": "imap.mail.me.com",
           "me.com": "imap.mail.me.com", "mac.com": "imap.mail.me.com", "126.com": "imap.126.com", "163.com": "imap.163.com",
           "qq.com": "imap.qq.com", "foxmail.com": "imap.qq.com", "mails.tsinghua.edu.cn": "mails.tsinghua.edu.cn",
           "london.edu": "outlook.office365.com"}
FIRST_DAYS = 3
FETCH_BYTES = 200_000
SEARCH_BYTES = 40_000  # 搜索只取开头一段：够看到订单号；QQ 整箱返回时每封取 200K 会拖到 30 秒（2026-10-31）
SCHEMA = """
CREATE TABLE IF NOT EXISTS imap_state (account TEXT PRIMARY KEY, uidvalidity TEXT, last_uid INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS mail_moves (id INTEGER PRIMARY KEY, account TEXT NOT NULL, msg_key TEXT NOT NULL, folder TEXT NOT NULL,
    was_seen INTEGER NOT NULL, at TEXT NOT NULL, undone INTEGER NOT NULL DEFAULT 0);
"""
imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED", "NONAUTH"))  # 网易邮箱要先报客户端身份，不然拒绝选收件箱
imaplib.Commands.setdefault("MOVE", ("SELECTED",))


def ensure(db):
    db.conn.executescript(SCHEMA)
    mail.ensure(db)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(imap_state)")}
    for col, ddl in (("backlog_done", "INTEGER NOT NULL DEFAULT 0"), ("flat", "INTEGER NOT NULL DEFAULT 0"), ("fail_count", "INTEGER NOT NULL DEFAULT 0"),
                     ("failed_since", "TEXT"), ("last_try", "TEXT"), ("last_error", "TEXT")):
        if col not in have:
            db.conn.execute(f"ALTER TABLE imap_state ADD COLUMN {col} {ddl}")
    if "move_tries" not in {r[1] for r in db.conn.execute("PRAGMA table_info(mail_items)")}:
        db.conn.execute("ALTER TABLE mail_items ADD COLUMN move_tries INTEGER NOT NULL DEFAULT 0")


# 登录连续失败（2026-10-31 的 126：授权码作废后每 10 分钟从洛杉矶登一次，失败几十次）：连续 3 次失败后改成每 2 小时试一次，
# 别让一直失败的登录把网易的风控越触越紧；设置页体检里写清从几点开始登不上、报错原文。录了新码（--add）就恢复每轮都试。
FAIL_BACKOFF = 3
RETRY_EVERY = timedelta(hours=2)


def _state(db, name: str):
    db.conn.execute("INSERT OR IGNORE INTO imap_state (account) VALUES (?)", (name,))
    return db.conn.execute("SELECT * FROM imap_state WHERE account = ?", (name,)).fetchone()


def should_try(db, name: str, now: datetime, changed_at: float = 0) -> bool:
    """changed_at：账户文件最后改的时间（她刚用 --add 录了新码，就马上试，不等 2 小时）。"""
    r = _state(db, name)
    if (r["fail_count"] or 0) < FAIL_BACKOFF or not r["last_try"]:
        return True
    if changed_at and changed_at > datetime.fromisoformat(r["last_try"]).timestamp():
        return True
    return now - datetime.fromisoformat(r["last_try"]) >= RETRY_EVERY


def note_login(db, name: str, now: datetime, error: str = None):
    _state(db, name)
    if error:
        db.conn.execute("UPDATE imap_state SET fail_count = fail_count + 1, failed_since = COALESCE(failed_since, ?), last_try = ?, last_error = ? "
                        "WHERE account = ?", (now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"), error[:200], name))
    else:
        db.conn.execute("UPDATE imap_state SET fail_count = 0, failed_since = NULL, last_try = ?, last_error = NULL WHERE account = ?",
                        (now.isoformat(timespec="seconds"), name))
    db.conn.commit()


def failing(db) -> list:
    """登不上的邮箱（给设置页体检）。"""
    ensure(db)
    return [dict(r) for r in db.conn.execute("SELECT account, failed_since, last_error, fail_count FROM imap_state WHERE fail_count >= ?",
                                              (FAIL_BACKOFF,))]


# ---------- 账户 ----------
def load_accounts(path: Path = CONFIG) -> list:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_accounts(accounts: list, path: Path = CONFIG):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = os.umask(0o077)
    try:
        path.write_text(json.dumps(accounts, ensure_ascii=False, indent=1), encoding="utf-8")
        os.chmod(path, 0o600)
    finally:
        os.umask(old)


def add_account(path: Path = CONFIG, ask=input, secret=getpass.getpass) -> str:
    addr = ask("邮箱地址：").strip()
    domain = addr.split("@")[-1].lower()
    host = ask(f"IMAP 服务器（回车用 {PRESETS.get(domain, '')}）：").strip() or PRESETS.get(domain, "")
    if not host:
        return "没有服务器地址，没保存"
    name = ask(f"叫它什么（回车用 {addr}）：").strip() or addr
    pw = secret("专用密码 / 授权码（输入时不显示）：").strip()
    if not pw:
        return "没有密码，没保存"
    accounts = [a for a in load_accounts(path) if a["name"] != name]
    accounts.append({"name": name, "user": addr, "host": host, "port": 993, "password": pw})
    save_accounts(accounts, path)
    return (f"已保存 {name}（{host}）。在你 Mac 的终端里整行复制，先试一次（只看不动邮箱）：\n"
            "ssh -t my-server \"sudo -H -u ethan bash -c 'cd ~/Personal-OS && python3 -m ethan.imapmail --data /home/ethan/data --dry-run'\"")


# ---------- IMAP 小工具 ----------
def utf7(name: str) -> str:
    """文件夹名转成 IMAP 要的 modified UTF-7（中文文件夹名必须这样传）。"""
    out, buf = [], []

    def flush():
        if buf:
            b = base64.b64encode("".join(buf).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append("&" + b + "-")
            buf.clear()
    for ch in name:
        if 0x20 <= ord(ch) <= 0x7e:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def from_utf7(name: str) -> str:
    """modified UTF-7 → 正常文字（服务器列出来的中文文件夹名）。"""
    def dec(m):
        b = m.group(1)
        if not b:
            return "&"
        b = b.replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be", errors="replace")
    return re.sub(r"&([A-Za-z0-9+,]*)-", dec, name)


def quote(name: str) -> str:
    return '"' + utf7(name).replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect(acc: dict, factory=imaplib.IMAP4_SSL):
    m = factory(acc["host"], acc.get("port", 993))
    m.login(acc["user"], acc["password"])
    if re.search(r"(126|163|yeah)\.(com|net)$", acc["host"]):
        m._simple_command("ID", '("name" "ethan" "version" "1.0" "vendor" "mia")')
    return m


def delimiter(m) -> str:
    typ, data = m.list('""', '""')
    raw = (data[0] or b"").decode(errors="replace") if data and data[0] else ""
    got = re.search(r'\) "(.)"', raw)
    return got.group(1) if got else "/"


def _header(msg, name: str) -> str:
    """解码邮件头，去掉折行。有的邮件头格式不规范（比如地址里夹着换行），新版解析器会直接报错，所以用宽松的解析。"""
    v = msg.get(name, "")
    try:
        v = str(make_header(decode_header(str(v))))
    except Exception:
        v = str(v)
    return " ".join(v.split())


def parse(raw: bytes) -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    plain = html = ""
    for part in msg.walk():
        if part.is_multipart() or part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            text = (part.get_payload(decode=True) or b"").decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
        if part.get_content_subtype() == "plain" and not plain:
            plain = text
        elif part.get_content_subtype() == "html" and not html:
            html = text
    body = plain or re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style).*?</\1>", " ", html))
    # 只有网页版的信，链接在 href 里、去标签就丢了：x.ai 的链接放到正文最前面（xAI 数据导出的下载链接，ethan/grokexport.py）
    xai = [u.replace("&amp;", "&") for u in dict.fromkeys(re.findall(r"""href=["'](https://[^"'\s]*x\.ai/[^"'\s]*)""", html, re.I))]
    xai = [u for u in xai if u not in body]
    if xai:
        body = "链接：" + " ".join(xai[:5]) + "\n\n" + body
    try:
        when = parsedate_to_datetime(_header(msg, "Date")).astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        when = ""
    to = " ".join(_header(msg, h) for h in ("To", "Cc", "Delivered-To", "X-Forwarded-To", "X-Original-To")).lower()
    body = re.sub(r"\n\s*\n\s*", "\n\n", re.sub(r" *\n *", "\n", re.sub(r"[ \t\r\f\v]+", " ", body))).strip()  # 留着段落，收件台里好读
    return {"id": _header(msg, "Message-ID"), "sender": _header(msg, "From"), "subject": _header(msg, "Subject"),
            "received_at": when, "body": body[:mail.BODY], "via": "example.com" if "hi@example.com" in to else None}


def fetch_new(m, db, acc_name: str, now: datetime) -> list:
    """收件箱里还没读过的新邮件 [(uid, 内容)]；不改已读状态。"""
    typ, data = m.select("INBOX")
    if typ != "OK":
        raise RuntimeError(f"打不开收件箱：{data}")
    typ, uv = m.response("UIDVALIDITY")
    uidvalidity = (uv[0] or b"").decode() if uv and uv[0] else ""
    row = db.conn.execute("SELECT * FROM imap_state WHERE account = ?", (acc_name,)).fetchone()
    if row and row["uidvalidity"] == uidvalidity:
        typ, data = m.uid("SEARCH", None, f"UID {row['last_uid'] + 1}:*")
    else:  # 第一次（或者邮箱重建过）：只看最近几天
        since = (now - timedelta(days=FIRST_DAYS)).strftime("%d-%b-%Y")
        typ, data = m.uid("SEARCH", None, f"SINCE {since}")
    uids = [int(u) for u in (data[0] or b"").split()] if data else []
    if row and row["uidvalidity"] == uidvalidity:
        uids = [u for u in uids if u > row["last_uid"]]  # “UID n:*”在没有新邮件时会返回最后一封
    out = []
    for uid in uids[:200]:
        typ, got = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{FETCH_BYTES}>)")
        raw = next((p[1] for p in got if isinstance(p, tuple)), b"") if got else b""
        if raw:
            try:
                item = parse(raw)
            except Exception as e:  # 一封解析不了，不挡别的：留个占位，原样留在收件箱等她看
                item = {"id": "", "sender": "（解析不了）", "subject": f"一封解析不了的邮件（{type(e).__name__}）",
                        "received_at": "", "body": ""}
            item["id"] = item["id"] or f"{acc_name}-uid-{uidvalidity}-{uid}"
            out.append((uid, item))
    last = max(uids) if uids else (row["last_uid"] if row and row["uidvalidity"] == uidvalidity else 0)
    db.conn.execute("INSERT OR REPLACE INTO imap_state (account, uidvalidity, last_uid) VALUES (?, ?, ?)", (acc_name, uidvalidity, last))
    db.conn.commit()
    return out


BACKLOG_PER_RUN = 30


def fetch_backlog(m, db, acc_name: str, skip=()) -> list:
    """接进来之前就在收件箱里的旧未读，按同样的规则补整理（2026-10-31 她要）。每轮最多 30 封，补完这个邮箱就不再看。
    要在 fetch_new 之后调（收件箱已选中）。"""
    row = db.conn.execute("SELECT * FROM imap_state WHERE account = ?", (acc_name,)).fetchone()
    if not row or row["backlog_done"]:
        return []
    typ, data = m.uid("SEARCH", None, "UNSEEN")
    uids = sorted((int(u) for u in (data[0] or b"").split()), reverse=True) if typ == "OK" and data else []
    uids = [u for u in uids if u <= row["last_uid"] and u not in set(skip)]
    out = []
    for uid in uids:
        if len(out) >= BACKLOG_PER_RUN:
            break
        typ, got = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{FETCH_BYTES}>)")
        raw = next((p[1] for p in got if isinstance(p, tuple)), b"") if got else b""
        if not raw:
            continue
        try:
            item = parse(raw)
        except Exception:
            continue
        item["id"] = item["id"] or f"{acc_name}-uid-{row['uidvalidity']}-{uid}"
        if db.conn.execute("SELECT 1 FROM mail_items WHERE msg_key = ?", (mail.key_of(item),)).fetchone():
            continue  # 处理过了（留给她看的那些）
        out.append((uid, item))
    if not out:
        db.conn.execute("UPDATE imap_state SET backlog_done = 1 WHERE account = ?", (acc_name,))
        db.conn.commit()
    return out


def move(m, uid: int, folder: str, delim: str, has_move: bool, flat: bool = False) -> str:
    """移到 folder；返回实际用的文件夹名。flat：这个邮箱不让建子文件夹（126 报 TRYCREATE Folder not exist，2026-11-01），
    改成一层的“小伴整理-账单”。"""
    path = folder.replace("/", "-") if flat else folder.replace("/", delim)
    if not flat:
        m.create(quote(path.split(delim)[0]))  # 已经有了会报错，忽略
    typ, data = m.create(quote(path))
    made = f"{typ} {data}"
    m.uid("STORE", str(uid), "+FLAGS", "(\\Seen)")
    if has_move:
        typ, data = m.uid("MOVE", str(uid), quote(path))
    else:
        typ, data = m.uid("COPY", str(uid), quote(path))
        if typ == "OK":
            m.uid("STORE", str(uid), "+FLAGS", "(\\Deleted)")
            m.expunge()
    if typ != "OK":
        raise RuntimeError(f"移动失败：{data}（建文件夹：{made[:80]}）")
    return path


MOVE_TRIES = 3  # 一封移不走最多试三轮，之后留在收件箱（已读），不再每 10 分钟重试


# ---------- 一轮 ----------
def run(store, router, accounts=None, dry_run: bool = False, factory=imaplib.IMAP4_SSL, now: datetime = None) -> dict:
    db = store.db
    ensure(db)
    now = now or datetime.now(timezone.utc)
    accounts = load_accounts() if accounts is None else accounts
    report = {}
    try:
        changed = CONFIG.stat().st_mtime
    except OSError:
        changed = 0
    uid_of = {}  # msg_key → (账户, uid, 原来是否已读)
    conns = {}
    for acc in accounts:
        if not should_try(db, acc["name"], now, changed):
            report[acc["name"]] = {"skipped": "连续登不上，2 小时试一次"}
            continue
        try:
            try:
                m = connect(acc, factory)
            except Exception as e:
                note_login(db, acc["name"], now, f"{type(e).__name__}: {str(e)[:160]}")
                raise
            note_login(db, acc["name"], now)
            conns[acc["name"]] = m
            new = fetch_new(m, db, acc["name"], now)
            old = fetch_backlog(m, db, acc["name"], {u for u, _ in new})
            new += old
            mail.receive(db, {"items": [{**item, "account": acc["name"]} for _, item in new]})
            for uid, item in new:
                uid_of[mail.key_of(item)] = (acc["name"], uid)
            report[acc["name"]] = {"new": len(new) - len(old), **({"backlog": len(old)} if old else {})}
            try:  # Gmail 垃圾箱里像是重要的：只提醒（她 11-04 同意）
                found = mail.receive_spam(db, [{**i, "account": acc["name"]} for i in fetch_spam(m, db, acc["name"], now)])
                if found:
                    report[acc["name"]]["spam_watch"] = found
            except Exception as e:
                print(f"[imapmail] {acc['name']} 垃圾箱没看成：{type(e).__name__} {e}", flush=True)
            try:  # 她自己发出去的信：她写的部分算她的原话（她 11-04 “后面的继续做吧”，ontology-plan 1.6）
                mine = fetch_sent(m, db, acc["name"], now)
                if mine:
                    report[acc["name"]]["sent"] = mine
            except Exception as e:
                print(f"[imapmail] {acc['name']} 已发送没看成：{type(e).__name__} {e}", flush=True)
            if not dry_run:
                try:  # 她说“不是垃圾”的放回收件箱
                    back = put_back(db, m, acc["name"])
                    if back:
                        report[acc["name"]]["put_back"] = back
                except Exception as e:
                    print(f"[imapmail] {acc['name']} 放回收件箱没成：{type(e).__name__} {e}", flush=True)
        except Exception as e:  # 一个邮箱出错不挡别的（多半是密码不对或没开 IMAP）
            report[acc["name"]] = {"error": f"{type(e).__name__}: {str(e)[:160]}"}
    report["classified"] = mail.process(store, router)
    if dry_run:
        report["would_move"] = [f"{r['subject'][:40]} → {mail.ROOT}/{mail.FOLDERS[r['category']]}"
                                for r in db.conn.execute("SELECT * FROM mail_items WHERE status = 'done' AND attention = 0 AND applied = 0")
                                if r["msg_key"] in uid_of]
    else:
        report["moved"] = apply(db, conns, uid_of, now)
    for m in conns.values():
        try:
            m.logout()
        except Exception:
            pass
    return report


def apply(db, conns: dict, uid_of: dict, now: datetime) -> int:
    """把判好的照办：不需要她看的标已读并移走；需要她看的不动。这一轮刚收的和以前没办完的都办。"""
    moved = 0
    delims, moves = {}, {}
    for r in db.conn.execute("SELECT * FROM mail_items WHERE status = 'done' AND applied = 0").fetchall():
        if r["attention"]:
            db.conn.execute("UPDATE mail_items SET applied = 1 WHERE id = ?", (r["id"],))
            continue
        acc = r["account"]
        m = conns.get(acc)
        if m is None:
            continue  # Mac 程序收的，或者这个邮箱这轮没连上
        uid = uid_of.get(r["msg_key"], (acc, None))[1]
        if uid is None:  # 以前收的、还没办：按 Message-ID 在收件箱里找
            mid = r["msg_key"][len("msgid:"):]
            typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID", f'"{mid}"')
            found = (data[0] or b"").split() if data else []
            if not found:
                db.conn.execute("UPDATE mail_items SET applied = 1 WHERE id = ?", (r["id"],))  # 她自己已经挪走了
                continue
            uid = int(found[-1])
        if acc not in delims:
            delims[acc] = delimiter(m)
            moves[acc] = "MOVE" in (m.capabilities or ())
            m.select("INBOX")
        typ, flags = m.uid("FETCH", str(uid), "(FLAGS)")
        first = flags[0] if flags else b""
        was_seen = 1 if b"\\Seen" in (first if isinstance(first, bytes) else first[0]) else 0
        folder = f"{mail.ROOT}/{mail.FOLDERS[r['category']]}"
        st = db.conn.execute("SELECT flat FROM imap_state WHERE account = ?", (acc,)).fetchone()
        flat = bool(st and st["flat"])
        try:
            try:
                folder = move(m, uid, folder, delims[acc], moves[acc], flat)
            except Exception:
                if flat:
                    raise
                folder = move(m, uid, folder, delims[acc], moves[acc], True)  # 子文件夹建不了：换一层的名字
                db.conn.execute("UPDATE imap_state SET flat = 1 WHERE account = ?", (acc,))
        except Exception as e:
            tries = (r["move_tries"] or 0) + 1
            gave_up = tries >= MOVE_TRIES
            db.conn.execute("UPDATE mail_items SET move_tries = ?, applied = ? WHERE id = ?", (tries, 2 if gave_up else 0, r["id"]))
            print(f"[imapmail] {acc} 没移成（第 {tries} 次{'，不再试，留在收件箱' if gave_up else ''}）：{e}", flush=True)
            continue
        db.conn.execute("INSERT INTO mail_moves (account, msg_key, folder, was_seen, at) VALUES (?, ?, ?, ?, ?)",
                        (acc, r["msg_key"], folder, was_seen, now.isoformat(timespec="seconds")))
        db.conn.execute("UPDATE mail_items SET applied = 1 WHERE id = ?", (r["id"],))
        moved += 1
    db.conn.commit()
    return moved


def undo(store, hours: float = 24, accounts=None, factory=imaplib.IMAP4_SSL) -> str:
    db = store.db
    ensure(db)
    accounts = {a["name"]: a for a in (load_accounts() if accounts is None else accounts)}
    cut = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    n = 0
    rows = db.conn.execute("SELECT * FROM mail_moves WHERE undone = 0 AND at >= ? ORDER BY id DESC", (cut,)).fetchall()
    conns = {}
    for r in rows:
        acc = accounts.get(r["account"])
        if not acc:
            continue
        m = conns.get(r["account"]) or connect(acc, factory)
        conns[r["account"]] = m
        d = delimiter(m)
        m.select(quote(r["folder"].replace("/", d)))
        typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID", f'"{r["msg_key"][len("msgid:"):]}"')
        found = (data[0] or b"").split() if data else []
        if not found:
            continue
        uid = found[-1].decode()
        if not r["was_seen"]:
            m.uid("STORE", uid, "-FLAGS", "(\\Seen)")
        m.uid("COPY", uid, "INBOX")
        m.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        m.expunge()
        db.conn.execute("UPDATE mail_moves SET undone = 1 WHERE id = ?", (r["id"],))
        n += 1
    db.conn.commit()
    for m in conns.values():
        m.logout()
    return f"放回了 {n} 封"


# ---------- 替她在邮箱里找东西（她 2026-10-31：“订单号也在我邮箱里自己去找”） ----------
def _search_folder(m, folder: str, words: list, gmail: bool) -> list:
    typ, _ = m.select(quote(folder), readonly=True)  # 只读打开，不改已读状态
    if typ != "OK":
        return []
    if gmail:
        m.literal = " ".join(words).encode()
        typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", "X-GM-RAW")
    else:
        crit = []
        for w in words:
            crit += ["OR", "OR", "SUBJECT", "\"%s\"" % w, "FROM", "\"%s\"" % w, "TEXT", "\"%s\"" % w] if w.isascii() else ["TEXT", "\"%s\"" % w]
        if all(w.isascii() for w in words):
            typ, data = m.uid("SEARCH", None, *crit)
        else:
            m.literal = " ".join(words).encode()
            typ, data = m.uid("SEARCH", "CHARSET", "UTF-8", "TEXT")
    return [int(u) for u in (data[0] or b"").split()] if typ == "OK" and data else []


def all_mail(m) -> str:
    """Gmail 的“所有邮件”文件夹：界面是中文时不叫 [Gmail]/All Mail，按 \\All 标记找（2026-10-31 线上 1 秒就返回 0 封）。"""
    try:
        typ, data = m.list('""', '"*"')
    except Exception:
        typ, data = "NO", []
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\All" in line.split(")")[0]:
            name = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if name:
                return from_utf7(name.group(1).replace('\\"', '"'))
    return "[Gmail]/All Mail"


def spam_folder(m):
    """垃圾邮件文件夹（按 \\Junk 标记找，Gmail 中文界面叫“垃圾邮件”）；没有就返回 None。"""
    try:
        typ, data = m.list('""', '"*"')
    except Exception:
        return None
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\Junk" in line.split(")")[0]:
            name = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if name:
                return from_utf7(name.group(1).replace('\\"', '"'))
    return None


def fetch_spam(m, db, acc_name: str, now: datetime) -> list:
    """垃圾箱里新来的（只读、不改已读、不移动；她 11-04 同意）。返回 [内容]，交给 mail.receive_spam 挑签证 / 银行 / 学校的。"""
    folder = spam_folder(m)
    if not folder:
        return []
    typ, data = m.select(quote(folder), readonly=True)
    if typ != "OK":
        return []
    key = f"{acc_name}#spam"
    typ, uv = m.response("UIDVALIDITY")
    uidvalidity = (uv[0] or b"").decode() if uv and uv[0] else ""
    row = db.conn.execute("SELECT * FROM imap_state WHERE account = ?", (key,)).fetchone()
    if row and row["uidvalidity"] == uidvalidity:
        typ, data = m.uid("SEARCH", None, f"UID {row['last_uid'] + 1}:*")
    else:
        typ, data = m.uid("SEARCH", None, f"SINCE {(now - timedelta(days=FIRST_DAYS)).strftime('%d-%b-%Y')}")
    uids = [int(u) for u in (data[0] or b"").split()] if data else []
    if row and row["uidvalidity"] == uidvalidity:
        uids = [u for u in uids if u > row["last_uid"]]
    out = []
    for uid in uids[:100]:
        typ, got = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{FETCH_BYTES}>)")
        raw = next((p[1] for p in got if isinstance(p, tuple)), b"") if got else b""
        try:
            item = parse(raw) if raw else None
        except Exception:
            item = None
        if item:
            item["id"] = item["id"] or f"{acc_name}-spam-{uidvalidity}-{uid}"
            out.append(item)
    last = max(uids) if uids else (row["last_uid"] if row and row["uidvalidity"] == uidvalidity else 0)
    db.conn.execute("INSERT OR REPLACE INTO imap_state (account, uidvalidity, last_uid) VALUES (?, ?, ?)", (key, uidvalidity, last))
    db.conn.commit()
    m.select("INBOX")
    return out


def sent_folder(m):
    """“已发送”文件夹（按 \\Sent 标记找）；没有就返回 None。"""
    try:
        typ, data = m.list('""', '"*"')
    except Exception:
        return None
    for raw in data or []:
        line = raw.decode(errors="replace") if isinstance(raw, bytes) else str(raw)
        if "\\Sent" in line.split(")")[0]:
            name = re.search(r'"((?:[^"\\]|\\.)*)"\s*$', line) or re.search(r"(\S+)\s*$", line)
            if name:
                return from_utf7(name.group(1).replace('\\"', '"'))
    return None


QUOTED = re.compile(r"^(On .{5,200} wrote:|在 .{2,200}写道：|.{0,80}于 .{2,80}写道：|-{2,} ?Original Message ?-{2,}|-{2,} ?原始邮件 ?-{2,}|From: .+)$", re.M)


def own_words(body: str) -> str:
    """她发出去的信里只留她自己写的：去掉“> ”引用行和“On … wrote:”之后的原信。"""
    text = "\n".join(l for l in (body or "").splitlines() if not l.lstrip().startswith(">"))
    m = QUOTED.search(text)
    return (text[:m.start()] if m else text).strip()


def fetch_sent(m, db, acc_name: str, now: datetime) -> int:
    """“已发送”里新的：她写的部分记成她的原话（lifebase 来源 sent），每晚和访谈一起整理成说法。只读、不移动。"""
    from . import lifebase
    lifebase.ensure(db)
    folder = sent_folder(m)
    if not folder:
        return 0
    typ, _ = m.select(quote(folder), readonly=True)
    if typ != "OK":
        return 0
    key = f"{acc_name}#sent"
    typ, uv = m.response("UIDVALIDITY")
    uidvalidity = (uv[0] or b"").decode() if uv and uv[0] else ""
    row = db.conn.execute("SELECT * FROM imap_state WHERE account = ?", (key,)).fetchone()
    if row and row["uidvalidity"] == uidvalidity:
        typ, data = m.uid("SEARCH", None, f"UID {row['last_uid'] + 1}:*")
    else:
        typ, data = m.uid("SEARCH", None, f"SINCE {(now - timedelta(days=FIRST_DAYS)).strftime('%d-%b-%Y')}")
    uids = [int(u) for u in (data[0] or b"").split()] if data else []
    if row and row["uidvalidity"] == uidvalidity:
        uids = [u for u in uids if u > row["last_uid"]]
    n = 0
    for uid in uids[:100]:
        typ, got = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{FETCH_BYTES}>)")
        raw = next((p[1] for p in got if isinstance(p, tuple)), b"") if got else b""
        try:
            item = parse(raw) if raw else None
        except Exception:
            item = None
        words = own_words(item["body"]) if item else ""
        if item and len(words) >= 20:  # 太短的（“好的，谢谢”）不收
            ref = f"sent:{(item['id'] or f'{acc_name}-{uidvalidity}-{uid}').strip('<>')}"
            n += 1 if lifebase.try_evidence(db, "sent", ref, item["received_at"] or now.isoformat(),
                                            f"主题：{item['subject']}\n{words[:6000]}", visibility="private") else 0
    last = max(uids) if uids else (row["last_uid"] if row and row["uidvalidity"] == uidvalidity else 0)
    db.conn.execute("INSERT OR REPLACE INTO imap_state (account, uidvalidity, last_uid) VALUES (?, ?, ?)", (key, uidvalidity, last))
    db.conn.commit()
    m.select("INBOX")
    return n


def put_back(db, m, acc_name: str) -> int:
    """她在工作台点了“这封不是垃圾”的：从 小伴 归档的文件夹放回收件箱、恢复原来的已读状态。"""
    n = 0
    for r in db.conn.execute("SELECT * FROM mail_items WHERE restore = 1 AND account = ?", (acc_name,)).fetchall():
        mv = db.conn.execute("SELECT * FROM mail_moves WHERE msg_key = ? AND account = ? AND undone = 0 ORDER BY id DESC LIMIT 1",
                             (r["msg_key"], acc_name)).fetchone()
        if mv is None:  # 还没移走过（或者她自己挪了）：不用放
            db.conn.execute("UPDATE mail_items SET restore = 2 WHERE id = ?", (r["id"],))
            continue
        d = delimiter(m)
        m.select(quote(mv["folder"].replace("/", d)))
        typ, data = m.uid("SEARCH", None, "HEADER", "Message-ID", f'"{r["msg_key"][len("msgid:"):]}"')
        found = (data[0] or b"").split() if data else []
        if found:
            uid = found[-1].decode() if isinstance(found[-1], bytes) else str(found[-1])
            if not mv["was_seen"]:
                m.uid("STORE", uid, "-FLAGS", "(\\Seen)")
            m.uid("COPY", uid, "INBOX")
            m.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            m.expunge()
            n += 1
        db.conn.execute("UPDATE mail_moves SET undone = 1 WHERE id = ?", (mv["id"],))
        db.conn.execute("UPDATE mail_items SET restore = 2 WHERE id = ?", (r["id"],))
    db.conn.commit()
    m.select("INBOX")
    return n


def _matches(words: list, item: dict) -> bool:
    """有的邮箱（QQ）不认搜索条件，整个收件箱都给回来：自己再核一遍，词真在发件人、主题或正文里才算。"""
    hay = f"{item['sender']} {item['subject']} {item['body']}".lower()
    return any(w.lower() in hay for w in words)


def _search_account(acc: dict, words: list, factory, limit: int) -> list:
    m = connect(acc, factory)
    hits = []
    try:
        gmail = acc["host"] == "imap.gmail.com"
        folders = [all_mail(m)] if gmail else ["INBOX"] + [f"{mail.ROOT}/{name}" for name in mail.FOLDERS.values()]
        for folder in folders:
            try:
                uids = _search_folder(m, folder, words, gmail)
            except Exception as e:
                print(f"[imapmail] {acc['name']} 搜 {folder} 出错：{type(e).__name__} {str(e)[:120]}", flush=True)
                continue
            got_here = 0
            for uid in reversed(uids[-limit * (2 if gmail else 4):]):  # 从新到旧，核过的够数就停
                if got_here >= limit:
                    break
                typ, got = m.uid("FETCH", str(uid), f"(BODY.PEEK[]<0.{SEARCH_BYTES}>)")
                raw = next((p[1] for p in got if isinstance(p, tuple)), b"") if got else b""
                try:
                    item = parse(raw)
                except Exception:
                    continue
                if not gmail and not _matches(words, item):
                    continue
                hits.append({"account": acc["name"], "folder": folder, "sender": item["sender"], "subject": item["subject"],
                             "at": item["received_at"], "text": item["body"][:800]})
                got_here += 1
    finally:
        try:
            m.logout()
        except Exception:
            pass
    return hits


def search(words: list, accounts=None, factory=imaplib.IMAP4_SSL, limit: int = 5, budget: float = 30.0) -> list:
    """在她所有邮箱里搜（含 小伴整理 的归档；Gmail 搜全部邮件），只读不动。返回最新的几封：账户、文件夹、发件人、主题、日期、正文开头。
    各邮箱同时搜（2026-10-31：一个一个搜时 QQ 就要 25 秒，后面的邮箱轮不到），总共等 budget 秒。"""
    from concurrent.futures import ThreadPoolExecutor, wait
    words = [w for w in (str(x).strip() for x in words) if w][:4]
    if not words:
        return []
    accounts = load_accounts() if accounts is None else accounts
    if not accounts:
        return []
    pool = ThreadPoolExecutor(max_workers=len(accounts))
    jobs = {pool.submit(_search_account, acc, words, factory, limit): acc for acc in accounts}
    done, late = wait(jobs, timeout=budget)
    hits = []
    for job in done:
        try:
            hits += job.result()
        except Exception as e:
            print(f"[imapmail] {jobs[job]['name']} 连不上：{type(e).__name__} {str(e)[:120]}", flush=True)
    for job in late:
        print(f"[imapmail] {jobs[job]['name']} 超过 {budget:.0f} 秒没搜完，先不等了", flush=True)
    pool.shutdown(wait=False)
    hits.sort(key=lambda h: h["at"] or "", reverse=True)
    return hits[:limit]


def lines(hits: list) -> str:
    return "\n".join(f"- [{h['account']}｜{(h['at'] or '')[:10]}] {h['sender']}：{h['subject']}\n  {' '.join(h['text'].split())[:500]}" for h in hits)


def main():
    p = argparse.ArgumentParser(description="服务器直接收邮件")
    p.add_argument("--data", type=Path)
    p.add_argument("--add", action="store_true")
    p.add_argument("--list", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--undo", action="store_true")
    a = p.parse_args()
    if a.add:
        print(add_account())
        return
    if a.list:
        for acc in load_accounts():
            print(f"{acc['name']}：{acc['user']} @ {acc['host']}")
        return
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(a.data)
    try:
        if a.undo:
            print(undo(store))
        else:
            print(json.dumps(run(store, build_router(a.data, 50.0), dry_run=a.dry_run), ensure_ascii=False), flush=True)
            from . import grokexport
            if not a.dry_run and grokexport.has_pending(store.db):  # xAI 的导出邮件：后台下载导入（没下成的半小时后再试，最多 3 次）
                grokexport.kick(a.data)
    finally:
        store.close()


if __name__ == "__main__":
    main()
