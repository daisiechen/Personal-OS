"""她的微信聊天，全部历史导出 + 每天增量交给 小伴（ontology-plan 1.9，数据源 S19；说明 docs/operations/wechat-export.md）。

开源工具 DaneelOlivaw1/macos-wechat-export 的 `read` 每会话最多 200 条，
这里照它的读法（wechat_db.py / sqlcipher_probe.py）自己读全部历史。在她 Mac 上跑：

1. 只读：微信 4.x 的库是 SQLCipher 4（raw key，cipher_compatibility=4）。每个库先连同 -wal/-shm 复制到临时目录（复制前后
   大小、修改时间不变才算数），只开副本：优先 python 的 sqlcipher3 模块，没有就调 brew 装的 `sqlcipher -readonly`，
   都加 PRAGMA query_only=1；读完删副本。**绝不写微信目录**。钥匙来自那个工具 bootstrap_keys.py 生成的 keys.json
   （{"keys": {"message/message_0.db": {"enc_key": 64 位十六进制}}}，也认按盐值 / 绝对路径登记的），文件必须 600。
2. 会话：session/session.db 的 SessionTable；名字从 contact/contact.db（备注 > 昵称 > 微信号）。排除公众号（gh_）、
   服务号和系统号、折叠群入口；文件传输助手默认不导（--filehelper 才导）。
3. 消息：message/message_<n>.db 里的 Msg_<md5(会话 username)> 表（local_id、server_id、local_type、create_time、
   real_sender_id、message_content）。local_type 低 32 位是种类、高 32 位是子类；正文可能是 zstd 压缩（28 b5 2f fd 开头）；
   群里别人的消息正文前面带“<username>:\\n”。real_sender_id 是 Name2Id 的 rowid → username，等于 --self-id 的就是她。
   文字照写，别的写占位（[图片]、[语音]……），引用回复取回复的那句话。
4. 写文件：~/wechat-export/<会话名>.jsonl（目录 700、文件 600），一行一条：time（ISO，她的时区）、who（她 / 备注名 /
   群成员名）、is_her、text，另带 db、local_id 方便对账。
5. 增量：每个会话、每个库记最后的 local_id（和库的盐值、最后的 create_time；库被重建了就按时间接着导），
   记在 ~/.config/ethan/wechatexport-state.json。再跑只导新的。
6. 交给 小伴（--send）：交整段对话：
   两边都交，一天一段、开头〔日期〕，每行标说话人【她】/【备注名或群成员名】，不截短（单条超长 2000 字截断），图片语音这类占位不交；
   去掉电话邮箱住址（docquotes.redact）。服务器整理时她说的算原话，别人说的关于她的记成“别人说的”（ethan/interviews.py）。
   私聊都交；群只交她说过话的群、她说过话的那几天（--all 不筛：所有群、所有天）；--chats 只交点名的。每会话一个访谈会话名
   “微信 · <会话名>”，场合“她的微信聊天（导出）”，经 cloud.CloudClient.interview 交，一次一批（一批交成功才前进游标），
   一次运行最多 --max-batches 批（默认 30，免得服务器一晚整理不完；剩下的下次接着交）。没钥匙只生成交接文件。

7. 人物卡（docs/design/relations.md）：--send 时顺带按私聊联系人算统计（最近联系、90 / 365 天条数、她说的占比、
   谁先开口〔隔 6 小时算新一轮〕、共同群），经 /cloud/people_stats 交给服务器；username 只交哈希。客服、中介、商家、酒店管家
   这类按名字跳过，--people-skip 补排除。
8. 微信标签：从 contact.db 读她给联系人打的标签
   （contact_label 表 + contact.extra_buffer 第 30 号字段，Reader.labels），随统计只交标签名；读不到不报错，--dry-run 里打印读到几个。

用法（在 Personal-OS 目录里，用 python3.14，它自带 zstd）：
  python3.14 -m ethan.wechatexport --db-root <db_storage> --keys <keys.json> --self-id <她的 sender id> --dry-run
  ... --export          只导出到 ~/wechat-export，不交
  ... --send            导出 + 交给 小伴
  ... --install-launchd 装每天 22:43 自动 --send（Mac）
"""

import argparse
import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from ethan import clock
from ethan.agentexport import token
from ethan.docquotes import redact

CONF = Path.home() / ".config" / "ethan"
STATE_FILE = CONF / "wechatexport-state.json"
OUT_DIR = CONF / "wechatexport-out"  # 没钥匙时生成的交接文件
LOG_FILE = CONF / "wechatexport.log"
EXPORT_DIR = Path.home() / "wechat-export"
LABEL = "com.ethan.wechatexport"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
SCENE = "她的微信聊天（导出）"
CHUNK = 12000  # 一批的字数：和文件整理的一段（interviews.FILE_CHUNK）一样大，每晚好整理
MAX_BATCHES = 30
MAX_CHARS = 2000
FILEHELPER = "filehelper"

SKIP_IDS = {"weixin", "newsapp", "fmessage", "medianote", "floatbottle", "qmessage", "qqmail", "tmessage", "qqsafe",
            "officialaccounts", "notifymessage", "notification_messages", "brandsessionholder",
            "brandservicesessionholder", "opencustomerservicemsg", "mphelper", "exmail_tool", "voipnotifymessage",
            "appbrand_notify_message", "userexperience_alarm", "helper_entry", "masssendapp", "blogapp",
            "voicevoipapp", "voiceinputapp", "linkedinplugin", "facebookapp", "feedsapp", "cardpackage",
            "shakeapp", "lbsapp", "googlecontact", "qqfriend", "readerapp", "pc_share"}
KINDS = {3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]", 48: "[位置]", 50: "[通话]",
         66: "[企业微信名片]"}
APP_KINDS = {3: "[音乐]", 4: "[视频链接]", 5: "[链接]", 6: "[文件]", 8: "[表情]", 19: "[聊天记录]", 24: "[文件]",
             33: "[小程序]", 36: "[小程序]", 49: "[链接]", 51: "[视频号]", 53: "[接龙]", 62: "[拍一拍]", 74: "[文件]",
             76: "[音乐]", 87: "[群公告]", 2000: "[转账]", 2001: "[红包]"}
PLACEHOLDER = re.compile(r"^\[[^\]]{1,8}\]")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
GROUP_PREFIX = re.compile(r"^([A-Za-z0-9_@.\-]{3,80}):\n")
MSG_DB = re.compile(r"^message_(\d+)\.db$")


# ---------- 钥匙 ----------
def load_keys(path: Path) -> dict:
    p = Path(path)
    if p.stat().st_mode & 0o077:
        raise RuntimeError(f"钥匙文件要只有她能读：chmod 600 {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("keys"), dict):
        data = data["keys"]
    if not isinstance(data, dict):
        raise RuntimeError("keys.json 格式不对：要是 {\"keys\": {库: {\"enc_key\": ...}}}")
    return data


def key_for(keys: dict, root: Path, db_path: Path) -> str:
    """照 sqlcipher_probe.load_key：按 盐值（库文件头 16 字节）/ 相对 db_storage 的路径 / 绝对路径 找，必须唯一。"""
    target = Path(db_path).resolve()
    with target.open("rb") as f:
        salt = f.read(16).hex()
    rel = target.relative_to(Path(root).resolve()).as_posix()
    found = set()
    for name, value in keys.items():
        n = str(name).replace("\\", "/")
        if n == salt or n == rel or rel.endswith("/" + n.lstrip("/")) or str(target).endswith("/" + n.lstrip("/")) \
                or (n.startswith("/") and Path(n).resolve() == target):
            if isinstance(value, dict):
                value = value.get("enc_key", value.get("key"))
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                found.add(value.lower())
    if len(found) != 1:
        raise RuntimeError(f"keys.json 里没有（或不止一把）{rel} 的钥匙：重新跑 bootstrap_keys.py")
    return found.pop()


# ---------- 打开加密库（可替换：测试用普通 sqlite） ----------
class CliDB:
    """brew 装的 sqlcipher 命令行：-readonly 打开，钥匙走标准输入（不进命令行参数、ps 看不到），每次查询起一个进程。"""

    def __init__(self, path: Path, key: str, exe: str, run=subprocess.run):
        self.path, self.key, self.exe, self.run = str(path), key, exe, run
        self.query("SELECT count(*) AS n FROM sqlite_master")  # 钥匙不对在这里就报错

    def script(self, sql: str) -> str:
        return (".bail on\n.output /dev/null\n"
                f"PRAGMA key = \"x'{self.key}'\";\nPRAGMA cipher_compatibility = 4;\nPRAGMA query_only = 1;\n"
                ".output stdout\n.mode json\n" + sql.strip().rstrip(";") + ";\n")

    def query(self, sql: str) -> list:
        res = self.run([self.exe, "-readonly", "-init", os.devnull, self.path], input=self.script(sql),
                       capture_output=True, text=True)
        if res.returncode:
            raise RuntimeError(f"sqlcipher 打不开 {Path(self.path).name}（退出码 {res.returncode}）：钥匙对不上或库坏了")
        out = res.stdout.strip()
        return json.loads(out) if out else []

    def close(self):
        pass


class ModuleDB:
    def __init__(self, path: Path, key: str, mod):
        self.conn = mod.connect(f"file:{path}?mode=ro", uri=True)
        self.conn.row_factory = mod.Row
        self.conn.execute(f"PRAGMA key = \"x'{key}'\"")
        self.conn.execute("PRAGMA cipher_compatibility = 4")
        self.conn.execute("PRAGMA query_only = 1")
        self.query("SELECT count(*) AS n FROM sqlite_master")

    def query(self, sql: str) -> list:
        return [dict(r) for r in self.conn.execute(sql).fetchall()]

    def close(self):
        self.conn.close()


def sqlcipher_exe() -> str:
    for c in (shutil.which("sqlcipher"), "/opt/homebrew/bin/sqlcipher", "/usr/local/bin/sqlcipher"):
        if c and Path(c).exists():
            return c
    return ""


def default_opener(path: Path, key: str):
    try:
        from sqlcipher3 import dbapi2 as mod
        return ModuleDB(path, key, mod)
    except ImportError:
        pass
    exe = sqlcipher_exe()
    if not exe:
        raise RuntimeError("没有 sqlcipher：先跑 brew install sqlcipher")
    return CliDB(path, key, exe)


def _stat(p: Path):
    try:
        s = p.stat()
        return s.st_size, s.st_mtime_ns
    except FileNotFoundError:
        return None


class Reader:
    """一个账号的 db_storage。open(rel) 先复制（含 -wal/-shm）到临时目录再开，读完删掉；原目录只读不写。"""

    def __init__(self, root: Path, keys: dict, opener=default_opener, tmp_root: Path = None):
        self.root, self.keys, self.opener, self.tmp_root = Path(root), keys, opener, tmp_root

    def snapshot(self, rel: str, tmp: Path) -> Path:
        src = self.root / rel
        parts = [src] + [Path(str(src) + s) for s in ("-wal", "-shm")]
        for _ in range(5):  # 微信开着也能读：复制前后都没变才算一份完整快照
            before = [_stat(p) for p in parts]
            for p in parts:
                if p.exists():
                    shutil.copyfile(p, tmp / p.name)
                    os.chmod(tmp / p.name, 0o600)
                elif (tmp / p.name).exists():
                    (tmp / p.name).unlink()
            if [_stat(p) for p in parts] == before:
                return tmp / src.name
            time.sleep(1)
        raise RuntimeError(f"{rel} 一直在变，复制不出完整的一份：退出微信再跑")

    def open(self, rel: str):
        reader = self

        class _Ctx:
            def __enter__(self):
                self.tmp = Path(tempfile.mkdtemp(prefix="wechatexport-", dir=reader.tmp_root))
                os.chmod(self.tmp, 0o700)
                try:
                    key = key_for(reader.keys, reader.root, reader.root / rel)
                    self.db = reader.opener(reader.snapshot(rel, self.tmp), key)
                except BaseException:
                    shutil.rmtree(self.tmp, ignore_errors=True)
                    raise
                return self.db

            def __exit__(self, *a):
                try:
                    self.db.close()
                finally:
                    shutil.rmtree(self.tmp, ignore_errors=True)
        return _Ctx()

    def salt(self, rel: str) -> str:
        with (self.root / rel).open("rb") as f:
            return f.read(16).hex()

    def message_dbs(self) -> list:
        d = self.root / "message"
        dbs = [p for p in d.glob("message_*.db") if MSG_DB.match(p.name)] if d.is_dir() else []
        return ["message/" + p.name for p in sorted(dbs, key=lambda p: int(MSG_DB.match(p.name).group(1)))]

    def contacts(self) -> dict:
        """username → 显示名（备注 > 昵称 > 微信号）。"""
        with self.open("contact/contact.db") as db:
            rows = db.query("SELECT username, nick_name, remark FROM contact")
        return {r["username"]: (r.get("remark") or r.get("nick_name") or r["username"]).strip() or r["username"]
                for r in rows if r.get("username")}

    def labels(self) -> tuple:
        """她在微信里给联系人打的标签。
        返回 ({username: [标签名, ...]}, 标签总数)；读不到（旧版本没有标签表、字段名对不上）返回 (None, 0)，不报错。

        macOS 微信 4.x 的 contact/contact.db：标签名在 contact_label 表（label_id_、label_name_、sort_order_）；
        联系人身上的标签在 contact.extra_buffer（protobuf）顶层第 30 号字段，是逗号隔开的标签编号，如 "1,5"。
        出处：开源的 r266-tech/wx-cli、pandorafuture/wx-cli 都这样读。字段名拿不准的按候选名探测（LABEL_*），
        也认 3.x 那种直接的列（label_id_list / LabelIDList）。"""
        try:
            with self.open("contact/contact.db") as db:
                return read_labels(db)
        except Exception:  # noqa: BLE001 —— 标签是锦上添花，读不到不拖累导出
            return None, 0

    def sessions(self) -> list:
        with self.open("session/session.db") as db:
            return [r["username"] for r in db.query("SELECT username FROM SessionTable") if r.get("username")]


# ---------- 联系人标签 ----------
LABEL_TABLES = ("contact_label", "ContactLabel", "contact_labels", "label")
LABEL_ID_COLS = ("label_id_", "label_id", "LabelId", "LabelID", "id")
LABEL_NAME_COLS = ("label_name_", "label_name", "LabelName", "name")
LABEL_SORT_COLS = ("sort_order_", "sort_order", "SortOrder")
CONTACT_LABEL_COLS = ("label_id_list", "label_ids", "label_list", "LabelIDList", "labelIDList")
LABEL_PROTO_FIELD = 30  # contact.extra_buffer 里放标签编号的字段
MAX_LABELS = 10


def _cols(db, table_name: str) -> list:
    return [r["name"] for r in db.query(f"SELECT name FROM pragma_table_info('{table_name}')")]


def _pick(cols: list, cands: tuple) -> str:
    low = {c.lower(): c for c in cols}
    return next((low[c.lower()] for c in cands if c.lower() in low), "")


def _varint(b: bytes, i: int) -> tuple:
    n = shift = 0
    while i < len(b):
        x = b[i]
        i += 1
        n |= (x & 0x7F) << shift
        if not x & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            break
    return None, i


def proto_strings(b: bytes, field: int) -> list:
    """protobuf 顶层第 field 号、长度型（wire type 2）字段的所有值；坏数据就停，不猜。"""
    out, i = [], 0
    while i < len(b):
        key, i = _varint(b, i)
        if not key:
            break
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _varint(b, i)
            if v is None:
                break
        elif wt == 1:
            i += 8
        elif wt == 5:
            i += 4
        elif wt == 2:
            ln, i = _varint(b, i)
            if ln is None or i + ln > len(b):
                break
            if num == field:
                out.append(b[i:i + ln])
            i += ln
        else:
            break
    return out


def _label_ids(raw) -> list:
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw or "")
    out = []
    for part in re.split(r"[,，|\s]+", text):
        part = part.strip()
        if part.isdigit() and int(part) > 0 and part not in out:
            out.append(str(int(part)))
    return out


def read_labels(db) -> tuple:
    """一个已打开的 contact.db → ({username: [标签名]}, 标签总数)；没有标签表 → (None, 0)。"""
    tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
    lt = next((t for t in LABEL_TABLES if t in tables), "")
    if not lt or "contact" not in tables:
        return None, 0
    cols = _cols(db, lt)
    cid, cname, csort = _pick(cols, LABEL_ID_COLS), _pick(cols, LABEL_NAME_COLS), _pick(cols, LABEL_SORT_COLS)
    if not cid or not cname:
        return None, 0
    order = f'"{csort}", ' if csort else ""
    names, rank = {}, {}
    for r in db.query(f'SELECT "{cid}" AS i, "{cname}" AS n FROM "{lt}" ORDER BY {order}"{cid}"'):
        n = " ".join(str(r.get("n") or "").split())[:20]
        i = str(r.get("i") or "").strip()
        if i.isdigit() and int(i) > 0 and n:
            names[str(int(i))] = n
            rank[str(int(i))] = len(rank)
    ccols = _cols(db, "contact")
    direct = _pick(ccols, CONTACT_LABEL_COLS)
    if direct:
        rows = [(r["username"], _label_ids(r.get("v"))) for r in db.query(f'SELECT username, "{direct}" AS v FROM contact')]
    elif "extra_buffer" in ccols:
        rows = [(r["username"], [i for v in proto_strings(bytes.fromhex(r.get("eb") or ""), LABEL_PROTO_FIELD)
                                 for i in _label_ids(v)])
                for r in db.query("SELECT username, hex(extra_buffer) AS eb FROM contact "
                                  "WHERE extra_buffer IS NOT NULL AND length(extra_buffer) > 0")]
    else:
        return None, 0
    out = {}
    for user, ids in rows:
        got = []
        for i in sorted(dict.fromkeys(ids), key=lambda x: rank.get(x, 1 << 30)):
            if i in names and names[i] not in got:
                got.append(names[i])
        if user and got:
            out[user] = got[:MAX_LABELS]
    return out, len(names)


# ---------- 会话、消息 ----------
def is_group(chat: str) -> bool:
    return chat.endswith("@chatroom")


def keep_chat(chat: str, filehelper: bool = False) -> bool:
    if chat == FILEHELPER:
        return filehelper
    return not (chat.startswith(("gh_", "@")) or chat in SKIP_IDS or chat.endswith("@placeholder_foldgroup")
                or "@" in chat and not chat.endswith(("@chatroom", "@openim")))


def table(chat: str) -> str:
    return "Msg_" + hashlib.md5(chat.encode()).hexdigest()


def unzstd(raw: bytes, decompress=None) -> bytes:
    if not raw.startswith(ZSTD_MAGIC):
        return raw
    if decompress is None:
        try:
            from compression import zstd  # Python 3.14 自带
            decompress = zstd.decompress
        except ImportError:
            try:
                import zstandard
                decompress = zstandard.ZstdDecompressor().decompress
            except ImportError:
                return b""
    try:
        return decompress(raw)
    except Exception:  # noqa: BLE001 —— 坏的一条不拖累整个会话
        return b""


def _xml(tag: str, s: str) -> str:
    m = re.search(rf"<{tag}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{tag}>", s, re.S)
    return html.unescape(m.group(1)).strip() if m else ""


def content(base: int, sub: int, body: str) -> str:
    """正文 → 文字：文字消息照写，别的写占位；引用回复（49/57）取她回的那句。"""
    if base == 1:
        return body
    if base == 49:
        appmsg = body[body.find("<appmsg"):] if "<appmsg" in body else body
        kind = sub or int(_xml("type", appmsg) or 0)
        title = _xml("title", appmsg)
        if kind == 57:
            return title or "[引用]"
        label = APP_KINDS.get(kind, "[分享]")
        return f"{label} {title[:80]}".strip() if kind not in (2000, 2001, 62) else label
    if base in (10000, 10002):
        text = re.sub(r"<[^>]+>", "", body)
        return ("[系统消息] " + " ".join(text.split())[:200]).strip()
    return KINDS.get(base, "[其他消息]")


def decode(row: dict, chat: str, names: dict, self_id: str, people: dict, db: str, decompress=None) -> dict:
    lt = int(row.get("local_type") or 0)
    base, sub = lt & 0xFFFFFFFF, lt >> 32
    body = unzstd(bytes.fromhex(row.get("body") or ""), decompress)
    text = body.decode("utf-8", "replace") if body else ""
    if not body and (row.get("body") or "") and base == 1:
        text = "[文字（解压不了：用 python3.14 跑）]"
    sender = names.get(int(row.get("real_sender_id") or 0))
    if is_group(chat):
        m = GROUP_PREFIX.match(text)
        if m:
            sender = sender if sender == self_id else m.group(1)
            text = text[m.end():]
    if sender is None and not is_group(chat):
        sender = ""  # 认不出是谁：不算她
    her = bool(self_id) and sender == self_id
    ts = int(row.get("create_time") or 0)
    return {"time": datetime.fromtimestamp(ts, clock.her_tz()).isoformat(timespec="seconds"), "ts": ts,
            "who": "她" if her else (people.get(sender) or sender or "（不知道是谁）"), "is_her": her,
            "text": content(base, sub, text).strip() or KINDS.get(base, "[其他消息]"),
            "type": base, "sender": sender or "", "db": db.rsplit("/", 1)[-1], "local_id": int(row["local_id"]),
            "server_id": str(row.get("server_id") or "")}


# ---------- 进度 ----------
def load_state(path: Path = STATE_FILE) -> dict:
    try:
        st = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        st = {}
    st.setdefault("chats", {})
    return st


def save_state(st: dict, path: Path = STATE_FILE) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def safe_name(name: str) -> str:
    s = re.sub(r"[/\\:\x00-\x1f]", "_", name).strip().lstrip(".")
    return s[:80] or "未命名"


def assign_files(chats: list, people: dict, st: dict) -> None:
    """会话 → 文件名；第一次定下就不变，同名的加微信号的哈希。"""
    used = {c["file"] for c in st["chats"].values() if c.get("file")}
    for chat in chats:
        c = st["chats"].setdefault(chat, {})
        c["name"] = "文件传输助手" if chat == FILEHELPER else people.get(chat, chat)
        if not c.get("file"):
            f = safe_name(c["name"]) + ".jsonl"
            if f in used:
                f = safe_name(c["name"]) + "-" + hashlib.md5(chat.encode()).hexdigest()[:6] + ".jsonl"
            c["file"] = f
            used.add(f)
        c.setdefault("cursor", {})
        c.setdefault("salts", {})


# ---------- 导出 ----------
def export(reader: Reader, self_id: str, out_dir: Path, st: dict, chats: list, people: dict, dry_run: bool,
           decompress=None, report: dict = None) -> dict:
    """每个消息库过一遍：每个会话只读 local_id 比游标大的。dry_run 只数条数。"""
    out_dir = Path(out_dir)
    counts = {chat: {"new": 0, "her": 0} for chat in chats}
    self_seen = False
    for rel in reader.message_dbs():
        name = rel.rsplit("/", 1)[-1]
        salt = reader.salt(rel)
        with reader.open(rel) as db:
            tables = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")}
            todo = [c for c in chats if table(c) in tables]
            if not todo:
                continue
            names = {int(r["rid"]): r["user_name"] for r in db.query("SELECT rowid AS rid, user_name FROM Name2Id")}
            self_rows = [k for k, v in names.items() if v == self_id]
            self_seen = self_seen or bool(self_rows)
            batch = {}
            for chat in todo:
                c = st["chats"][chat]
                cur = int(c["cursor"].get(name, 0))
                where = f"local_id > {cur}"
                if c["salts"].get(name) and c["salts"][name] != salt:  # 库被重建：local_id 重新数了，按时间接着导
                    where = f"create_time > {int(c.get('last_time', 0))}"
                    report["warnings"].append(f"{name} 换过（盐值变了），{c['name']} 按时间接着导")
                if dry_run:
                    her = f"sum(real_sender_id = {self_rows[0]})" if self_rows else "0"
                    r = db.query(f'SELECT count(*) AS n, {her} AS her FROM "{table(chat)}" WHERE {where}')[0]
                    counts[chat]["new"] += int(r["n"] or 0)
                    counts[chat]["her"] += int(r["her"] or 0)
                    continue
                rows = db.query("SELECT local_id, server_id, local_type, create_time, real_sender_id, "
                                f'hex(message_content) AS body FROM "{table(chat)}" WHERE {where} ORDER BY local_id')
                batch[chat] = [decode(r, chat, names, self_id, people, rel, decompress) for r in rows]
            if dry_run:
                continue
            if any(batch.values()) and not self_seen and not st.get("self_seen"):  # 最早的库里她都不在：多半填错了
                raise RuntimeError(f"--self-id {self_id} 不在 {name} 里：填错了？按说明在文件传输助手发一条找")
        for chat, recs in batch.items():
            c = st["chats"][chat]
            c["salts"][name] = salt
            if not recs:
                continue
            recs.sort(key=lambda r: (r["ts"], r["local_id"]))
            out_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(out_dir, 0o700)
            f = out_dir / c["file"]
            with f.open("a", encoding="utf-8") as fh:
                for r in recs:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            os.chmod(f, 0o600)
            c["cursor"][name] = max([int(c["cursor"].get(name, 0))] + [r["local_id"] for r in recs])
            c["last_time"] = max([int(c.get("last_time", 0))] + [r["ts"] for r in recs])
            c["lines"] = int(c.get("lines", 0)) + len(recs)
            c["her"] = int(c.get("her", 0)) + sum(r["is_her"] for r in recs)
            counts[chat]["new"] += len(recs)
            counts[chat]["her"] += sum(r["is_her"] for r in recs)
        if not dry_run:
            st["self_seen"] = st.get("self_seen") or self_seen
    if dry_run and not self_seen and any(v["new"] for v in counts.values()):
        report["warnings"].append(f"--self-id {self_id} 在哪个库里都找不到：“她说的”都会是 0，先核对")
    return counts


# ---------- 交接文本 ----------
def read_jsonl(path: Path) -> list:
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _said(r: dict) -> str:
    t = (r.get("text") or "").strip()
    return "" if PLACEHOLDER.match(t) and r.get("type") != 1 else t


def _line(r: dict) -> str:
    t = " ".join(_said(r).split())
    if not t:
        return ""
    if len(t) > MAX_CHARS:
        t = t[:MAX_CHARS] + "……（太长，截断）"
    who = "她" if r.get("is_her") else (" ".join(str(r.get("who") or "（不知道是谁）").split()).replace("】", "」").replace("【", "「")[:40])
    return f"【{who}】{redact(t)}"


def transcript_blocks(recs: list, start: int = 0, her_days_only: bool = False, size: int = CHUNK) -> list:
    """[(交接块, 这一块交完后游标该到的行号)]：整段对话，两边都交，一天一段（开头〔日期〕），一行一句标说话人。
    her_days_only（群）：只交她那天说过话的日子。一天太长的拆成几块，每块都带〔日期〕。"""
    keep = {r["time"][:10] for r in recs[start:] if r.get("is_her") and _said(r)} if her_days_only else None
    out, cur, n, day = [], [], 0, None

    def flush(end):
        nonlocal cur, n
        if cur:
            out.append(("\n".join([f"〔{day}〕"] + cur), end))
        cur, n = [], 0

    for i in range(start, len(recs)):
        r = recs[i]
        d = r["time"][:10]
        if d != day:
            flush(i)
            day = d
        if keep is not None and d not in keep:
            continue
        ln = _line(r)
        if not ln:
            continue
        if cur and n + len(ln) + 1 > size - 20:
            flush(i)
        cur.append(ln)
        n += len(ln) + 1
    flush(len(recs))
    return out


def pack(items: list, size: int = CHUNK) -> list:
    """[(块, 游标)] → [(一批文本, 交完后游标)]。"""
    out, cur = [], []
    for text, end in items:
        if cur and sum(len(t) + 2 for t, _ in cur) + len(text) > size:
            out.append(("\n\n".join(t for t, _ in cur), cur[-1][1]))
            cur = []
        cur.append((text, end))
    if cur:
        out.append(("\n\n".join(t for t, _ in cur), cur[-1][1]))
    return out


def should_send(chat: str, c: dict, chats_filter: set, send_all: bool) -> bool:
    if chats_filter:
        return chat in chats_filter or c.get("name") in chats_filter
    return send_all or not is_group(chat) or int(c.get("her", 0)) > 0


# ---------- 私聊统计（交给服务器建人物卡，ethan/relations.py） ----------
ROUND_GAP = 6 * 3600  # 隔 6 小时以上算新的一轮，看谁先开口
STATS_BATCH = 150  # 一次交多少人（服务器一次最多收 64KB）


def people_stats(st: dict, out_dir: Path, chats: list, now_ts: int = None, skip=(), labels: dict = None) -> list:
    """按私聊联系人算：最近一次联系、90 / 365 天条数、她说的占比、谁先开口、共同群（最多 5 个）。
    labels：{username: [她在微信里给这个人打的标签名]}；读到了才交（每人都带，没标签交空列表，服务器据此清掉）；
    读不到（None）就不带这个字段，服务器上原来的标签不动。只交标签名，去掉联系方式。
    username 只交哈希（relations.wx_key），名字是备注 / 昵称。客服、中介、商家这类跳过（relations.is_service + --people-skip）。"""
    from ethan import relations
    now_ts = int(now_ts if now_ts is not None else time.time())
    group_of = {}  # username → {群名: 这个人在群里说了几句}
    for chat in chats:
        if not is_group(chat):
            continue
        c = st["chats"].get(chat) or {}
        for r in read_jsonl(Path(out_dir) / c.get("file", "")) if c.get("file") else []:
            u = r.get("sender")
            if u and not r.get("is_her"):
                g = group_of.setdefault(u, {})
                g[c.get("name", chat)] = g.get(c.get("name", chat), 0) + 1
    out = []
    for chat in chats:
        if is_group(chat) or chat == FILEHELPER:
            continue
        c = st["chats"].get(chat) or {}
        name = c.get("name") or chat
        if relations.is_service(name, skip) or not c.get("file"):
            continue
        recs = sorted((r for r in read_jsonl(Path(out_dir) / c["file"]) if r.get("ts")), key=lambda r: r["ts"])
        her = sum(1 for r in recs if r.get("is_her"))
        if len(recs) < 3 or not her or her == len(recs):  # 只有一边在说的（群发、通知、单方面加的）不建卡
            continue
        rounds = her_starts = 0
        prev = None
        for r in recs:
            if prev is None or r["ts"] - prev > ROUND_GAP:
                rounds += 1
                her_starts += bool(r.get("is_her"))
            prev = r["ts"]
        groups = sorted((group_of.get(chat) or {}).items(), key=lambda x: -x[1])[:5]
        extra = {"labels": [redact(x) for x in labels.get(chat, [])]} if labels is not None else {}
        out.append({**extra, "key": relations.wx_key(chat), "name": name, "last": recs[-1]["time"][:10],
                    "n90": sum(1 for r in recs if r["ts"] >= now_ts - 90 * 86400),
                    "n365": sum(1 for r in recs if r["ts"] >= now_ts - 365 * 86400), "total": len(recs),
                    "her_share": round(her / len(recs), 3), "her_starts": round(her_starts / rounds, 3), "rounds": rounds,
                    "groups": [g for g, _ in groups]})
    return out


def send_people_stats(client, stats: list, report: dict) -> None:
    for i in range(0, len(stats), STATS_BATCH):
        res = client.people_stats(stats[i:i + STATS_BATCH])
        if not res.get("ok"):
            report["errors"].append(f"人物统计没交上：{res.get('error', res)}")
            return
        report["people_new"] = report.get("people_new", 0) + int(res.get("new", 0))
    report["people"] = len(stats)


# ---------- 一次运行 ----------
def run(db_root: Path, keys_path: Path, self_id: str, mode: str = "dry-run", out_dir: Path = EXPORT_DIR,
        state_path: Path = STATE_FILE, chats_filter=(), send_all: bool = False, filehelper: bool = False,
        max_batches: int = MAX_BATCHES, env=os.environ, client=None, opener=default_opener, handoff_dir: Path = OUT_DIR,
        settings: Path = None, decompress=None, tmp_root: Path = None, people_skip=()) -> dict:
    reader = Reader(db_root, load_keys(keys_path), opener, tmp_root)
    st = load_state(state_path)
    people = reader.contacts()
    labels, n_labels = reader.labels()
    chats = [c for c in dict.fromkeys(reader.sessions()) if keep_chat(c, filehelper)]
    work = json.loads(json.dumps(st))  # dry-run 不动真进度
    assign_files(chats, people, work)
    report = {"chats": len(chats), "new": 0, "her": 0, "per_chat": [], "sent": 0, "batches": 0, "written": [],
              "warnings": [], "errors": []}
    # 她跑一次 --dry-run 就能看出标签读没读到（按微信标签分组）
    report["labels"] = (f"读到 {n_labels} 个标签、{len(labels)} 个联系人有标签" if labels is not None
                        else "没读到微信标签（这个版本的微信库里没有标签表或字段对不上），关系页照旧按重要程度分")
    counts = export(reader, self_id, out_dir, work, chats, people, mode == "dry-run", decompress, report)
    filt = {s.strip() for s in chats_filter if s.strip()}
    for chat in chats:
        c, n = work["chats"][chat], counts[chat]
        report["new"] += n["new"]
        report["her"] += n["her"]
        if n["new"]:
            report["per_chat"].append({"chat": c["name"], "group": is_group(chat), "new": n["new"], "her": n["her"],
                                       "send": should_send(chat, {**c, "her": int(c.get("her", 0)) + (n["her"] if mode == "dry-run" else 0)}, filt, send_all)})
    report["per_chat"].sort(key=lambda x: -x["new"])
    if mode == "dry-run":
        return report
    save_state(work, state_path)
    st = work
    if mode != "send":
        return report
    tok = token(env, settings)
    if tok and client is None:
        from ethan.cloud import CloudClient, DEFAULT_URL
        client = CloudClient(tok, env.get("ETHAN_URL", DEFAULT_URL))
    from ethan import relations
    for chat in chats:
        c = st["chats"][chat]
        if not should_send(chat, c, filt, send_all):
            continue
        recs = read_jsonl(Path(out_dir) / c["file"])
        sent = int(c.get("sent", 0))
        if sent > len(recs):
            sent = 0  # 文件被删过重导：从头交（服务器对同样的内容不重复记）
        parts = pack(transcript_blocks(recs, sent, her_days_only=is_group(chat) and not send_all))
        if not parts:
            c["sent"] = len(recs)
            save_state(st, state_path)
            continue
        session = f"{'微信群' if is_group(chat) else '微信'} · {c['name']}"[:80]  # 群单独标，后台先整理私聊
        if not tok:
            Path(handoff_dir).mkdir(parents=True, exist_ok=True)
            os.chmod(handoff_dir, 0o700)
            for k, (text, _) in enumerate(parts, 1):
                f = Path(handoff_dir) / f"{Path(c['file']).stem}-{k}.md"
                f.write_text(text, encoding="utf-8")
                os.chmod(f, 0o600)
                report["written"].append(str(f))
            continue
        ok = True
        for text, end in parts:
            if max_batches and report["batches"] >= max_batches:
                ok = False
                break
            m = re.search(r"〔(\d{4}-\d{2}-\d{2})〕", text)
            res = client.interview(session, SCENE, text, fmt="transcript", at=m.group(1) if m else None,
                                   key=None if is_group(chat) else relations.wx_key(chat))  # 指纹：人物卡按它认人
            if not res.get("ok"):
                ok = False
                report["errors"].append(f"{c['name']}: {res.get('error', res)}")
                break
            report["batches"] += 1
            report["sent"] += text.count("【她】")
            c["sent"] = end  # 一批交成功才前进
            save_state(st, state_path)
        if ok:
            c["sent"] = len(recs)
            save_state(st, state_path)
    if tok:  # 私聊统计交给服务器建人物卡（工作台“关系”页）
        send_people_stats(client, people_stats(st, out_dir, chats, skip=people_skip, labels=labels), report)
    if not tok:
        report["note"] = ("没有钥匙（ETHAN_CLOUD_TOKEN），只生成了文件、没有交；有钥匙后重跑 --send 就会交，"
                          f"或者逐个用 python3 -m ethan.cloud interview --session \"微信 · 会话名\" --scene \"{SCENE}\" --file <文件> 交")
    elif max_batches and report["batches"] >= max_batches:
        report["note"] = f"这次交了 {max_batches} 批（上限），剩下的下次接着交；想一次交完加 --max-batches 0"
    return report


# ---------- 每天自动跑（原来每周日） ----------
def plist_text(python: str, repo: Path, args: list, log: Path = LOG_FILE) -> str:
    prog = "".join(f"<string>{escape(str(a))}</string>" for a in [python, "-m", "ethan.wechatexport"] + args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array>{prog}</array>
  <key>WorkingDirectory</key><string>{escape(str(repo))}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string></dict>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>22</integer><key>Minute</key><integer>43</integer></dict>
  <key>StandardOutPath</key><string>{escape(str(log))}</string>
  <key>StandardErrorPath</key><string>{escape(str(log))}</string>
</dict></plist>
"""


def install_launchd(args: list, plist: Path = PLIST, run_cmd=subprocess.run, platform: str = sys.platform) -> str:
    plist = Path(plist)
    plist.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    text = plist_text(sys.executable, Path(__file__).resolve().parents[1], args)
    plist.write_text(text, encoding="utf-8")
    if platform != "darwin":
        return f"{text}\n写好了 {plist}；这台不是 Mac，没有 launchctl load。"
    run_cmd(["launchctl", "unload", str(plist)], capture_output=True)
    run_cmd(["launchctl", "load", str(plist)], check=True)
    return (f"{text}\n装好了：{plist}，每天 22:43 增量导出微信并交给 小伴（Mac 那会儿睡着就醒来后补跑），日志在 {LOG_FILE}。"
            f"\n不想要了：launchctl unload {plist}")


def main(argv=None, env=os.environ) -> int:
    p = argparse.ArgumentParser(description="导出她的微信聊天（全部历史 + 增量），交给 小伴（ontology-plan 1.9）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只数：会话数、每个会话新的条数、她说的条数；不写不交")
    g.add_argument("--export", action="store_true", help="只导出到 ~/wechat-export，不交")
    g.add_argument("--send", action="store_true", help="导出 + 交给 小伴（没钥匙只生成文件）")
    g.add_argument("--install-launchd", action="store_true", help="装每天 22:43 自动 --send（Mac）")
    p.add_argument("--db-root", type=Path, required=True, help="她账号的 db_storage 目录")
    p.add_argument("--keys", type=Path, required=True, help="bootstrap_keys.py 生成的 keys.json")
    p.add_argument("--self-id", required=True, help="她自己的 sender id（文件传输助手里找到的那串）")
    p.add_argument("--chats", action="append", default=[], help="只交这些会话（会话名或 username，逗号分隔，可写多次）")
    p.add_argument("--all", action="store_true", help="不筛：群也都交（默认群只交她说过话的）")
    p.add_argument("--filehelper", action="store_true", help="也导文件传输助手")
    p.add_argument("--max-batches", type=int, default=MAX_BATCHES, help=f"一次最多交几批（默认 {MAX_BATCHES}，0 = 不限）")
    p.add_argument("--people-skip", action="append", default=[], help="人物卡不建这些人（名字里含这些字就跳过，逗号分隔，可写多次）")
    p.add_argument("--out-dir", type=Path, default=EXPORT_DIR)
    p.add_argument("--state-file", type=Path, default=STATE_FILE)
    a = p.parse_args(argv)
    chats = [s for item in a.chats for s in item.split(",") if s.strip()]
    skip = [s.strip() for item in a.people_skip for s in item.split(",") if s.strip()]
    if a.install_launchd:
        args = ["--db-root", str(a.db_root.expanduser().resolve()), "--keys", str(a.keys.expanduser().resolve()),
                "--self-id", a.self_id, "--send"] + (["--chats", ",".join(chats)] if chats else []) \
            + (["--all"] if a.all else []) + (["--filehelper"] if a.filehelper else []) \
            + ["--max-batches", str(a.max_batches)] + (["--people-skip", ",".join(skip)] if skip else [])
        print(install_launchd(args))
        return 0
    mode = "dry-run" if a.dry_run else "export" if a.export else "send"
    try:
        res = run(a.db_root.expanduser(), a.keys.expanduser(), a.self_id, mode, a.out_dir.expanduser(), a.state_file,
                  chats, a.all, a.filehelper, a.max_batches, env, people_skip=skip)
    except (RuntimeError, OSError, ValueError) as e:
        print(f"[{clock.stamp(clock.her_now())}] 出错了：{e}")
        return 2
    print(f"[{clock.stamp(clock.her_now())}] " + json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if not res["errors"] else 1


if __name__ == "__main__":
    sys.exit(main())
