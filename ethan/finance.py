"""财务领地的数据（2026-10-28 补）：英国花销走 Monzo，国内花销走招行消费短信。Apple Pay 不单独接（territories.md）。

招行：她 iPhone 上设一条快捷指令自动化“收到来自 95555 的短信 → 把短信全文 POST 到 https://ethan.example.com/cloud/spend”，
      这里认出金额、商户、时间，记一笔。认不出的整条留着（kind=unparsed），不丢。
Monzo：官方 API，要她在 https://developers.monzo.com 用自己的 Monzo 账号建一个“机密”客户端，
      回调地址填 https://ethan.example.com/cloud/monzo/callback，再把 client id / secret 用 setup_secrets 录到服务器。
      之后在工作台设置页点“去授权”，跳到 Monzo 登录，回来后在 Monzo App 里点一次批准。
      每小时同步一次交易（ethan2-money.timer）；Monzo 规定授权 5 分钟内能拉全部历史，之后只能拉最近 90 天，所以授权后马上拉一次。
钥匙（access / refresh token）只存服务器数据目录的 monzo.json（600），不进仓库、不进聊天。
步骤给她看：docs/operations/finance.md
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY, source TEXT NOT NULL, ext_id TEXT UNIQUE, at TEXT NOT NULL, day TEXT NOT NULL,
    amount REAL NOT NULL, currency TEXT NOT NULL, merchant TEXT, category TEXT, raw TEXT);"""
REDIRECT = "https://ethan.example.com/cloud/monzo/callback"
AUTH_URL = "https://auth.monzo.com/"
API = "https://api.monzo.com"
TOKEN_FILE, STATE_FILE = "monzo.json", "monzo-state.json"

# 招行消费短信，例：您账户1234于10月28日12:30在【某某商户】快捷支付扣款人民币56.80元 / 您尾号1234的信用卡10月28日12:30消费人民币56.80元，商户：某某
CMB_AMOUNT = re.compile(r"(?:人民币|RMB|CNY|(?:消费|支出|支付|扣款|交易|入账|存入|转入|退款)(?:金额)?[:：]?)\s*(?:人民币)?\s*([\d,]+\.?\d*)\s*元?")
CMB_WHEN = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(\d{1,2})[:：](\d{2})")
CMB_MERCHANT = re.compile(r"(?:在|商户[:：]\s*)【?([^【】，,。\s]{2,30}?)】?(?:快捷支付|消费|支付|扣款|退款|退货|，|,|。|$)")
TRANSFER = re.compile(r"还款|转账至本人|本人账户|理财|定期|基金申购")
CMB_INCOME = re.compile(r"(?:入账|存入|转入|收入)")
REFUND_FROM = re.compile(r"(?:收到|来自)【?([^【】，,。\s]{2,30}?)】?(?:的)?(?:退款|退货|退回)")
REFUND = re.compile(r"退款|退货|撤销|冲正|退回")  # 退款和消费的邮件标题一样，只看正文（2026-10-29 她提醒）


_FIXED = set()


def ensure(db):
    db.conn.executescript(SCHEMA)
    if id(db) not in _FIXED:  # 之前按北京日期记的，改成伦敦日期（只在每个进程第一次用时检查一遍）
        _FIXED.add(id(db))
        rows = db.conn.execute("SELECT id, at, day FROM transactions").fetchall()
        fix = [(london_day(datetime.fromisoformat(r["at"])).isoformat(), r["id"]) for r in rows
               if london_day(datetime.fromisoformat(r["at"])).isoformat() != r["day"]]
        if fix:
            db.conn.executemany("UPDATE transactions SET day = ? WHERE id = ?", fix)
            db.conn.commit()


def _now():
    return datetime.now(timezone.utc)


def london_day(at: datetime) -> date:
    """按伦敦日期记（2026-10-29：通知里是北京时间，伦敦傍晚的消费在北京已是第二天，财务页按伦敦“今天”统计就漏了）。"""
    try:
        from .clock import LOCAL
        return at.astimezone(LOCAL).date() if at.tzinfo else at.date()
    except Exception:
        return at.date()


def add(db, source, at: datetime, amount: float, currency: str, merchant=None, category=None, raw=None, ext_id=None) -> bool:
    ensure(db)
    cur = db.conn.execute("INSERT OR IGNORE INTO transactions (source, ext_id, at, day, amount, currency, merchant, category, raw) "
                          "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                          (source, ext_id, at.isoformat(timespec="seconds"), london_day(at).isoformat(), amount, currency,
                           (merchant or "")[:80] or None, category, (raw or "")[:500] or None))
    db.conn.commit()
    return cur.rowcount > 0


# ---------- 招行短信 ----------
def parse_cmb(text: str, now: datetime) -> dict:
    """认出一条招行短信：{amount（花出去为负）, merchant, at}；认不出抛 ValueError。"""
    m = CMB_AMOUNT.search(text)
    if not m:
        raise ValueError("没找到金额")
    amount = float(m.group(1).replace(",", ""))
    if not (CMB_INCOME.search(text) or REFUND.search(text)):
        amount = -amount
    at = now
    w = CMB_WHEN.search(text)
    if w:
        try:
            from zoneinfo import ZoneInfo
            cn = ZoneInfo("Asia/Shanghai")
            mon, d, h, mi = map(int, w.groups())
            year = now.astimezone(cn).year
            at = datetime(year, mon, d, h, mi, tzinfo=cn)
            if at > now + timedelta(days=2):  # 跨年
                at = at.replace(year=year - 1)
        except ValueError:
            at = now
    merchant = CMB_MERCHANT.search(text) or REFUND_FROM.search(text)
    return {"amount": amount, "merchant": merchant.group(1) if merchant else None, "at": at}


BANKS = {"招商": "cmb", "交通": "bocom", "交行": "bocom"}


def bank_of(text: str, sender: str = "") -> str:
    """哪家银行（看短信签名或发件号码）：招行 95555，交行 95559；认不出记成 bank。"""
    head = (sender or "") + text[:30]
    if "95555" in head:
        return "cmb"
    if "95559" in head:
        return "bocom"
    return next((v for k, v in BANKS.items() if k in head), "bank")


def record_cmb(db, text: str, now: datetime = None, sender: str = "") -> dict:
    """银行消费短信记一笔（名字沿用招行，交行等也走这里，2026-10-29）。"""
    now = now or _now()
    source = bank_of(str(text), sender)
    text = " ".join(str(text).split())[:500]
    ext = "cmb:" + hashlib.sha1(text.encode()).hexdigest()[:20]  # 同一条短信重复发只记一次
    try:
        got = parse_cmb(text, now)
    except ValueError:
        add(db, source, now, 0, "CNY", category="unparsed", raw=text, ext_id=ext)
        return {"ok": True, "parsed": False, "bank": source}
    category = ("refund" if REFUND.search(text) else
                "transfers" if TRANSFER.search(text) else None)  # 退款冲减花销；储蓄卡还信用卡、转给自己不算花销，免得算两遍
    add(db, source, got["at"], got["amount"], "CNY", merchant=got["merchant"], category=category, raw=text, ext_id=ext)
    return {"ok": True, "parsed": True, "amount": got["amount"], "merchant": got["merchant"], "bank": source}


# ---------- Monzo ----------
def _client():
    return os.environ.get("MONZO_CLIENT_ID"), os.environ.get("MONZO_CLIENT_SECRET")


def auth_link(data: Path) -> str:
    """给她点的授权链接；state 存下来，回调时核对。"""
    cid, _ = _client()
    if not cid:
        raise ValueError("服务器上还没录 Monzo client id（setup_secrets）")
    state = secrets.token_urlsafe(16)
    p = Path(data) / STATE_FILE
    p.write_text(json.dumps({"state": state, "at": _now().isoformat()}))
    p.chmod(0o600)
    return AUTH_URL + "?" + urllib.parse.urlencode({"client_id": cid, "redirect_uri": REDIRECT, "response_type": "code", "state": state})


def _post(url, fields, opener):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST",
                                 headers={"User-Agent": "ethan-finance"})
    with opener(req, timeout=30) as r:
        return json.loads(r.read())


def _get(path, token, opener, params=None):
    url = API + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "User-Agent": "ethan-finance"})
    with opener(req, timeout=30) as r:
        return json.loads(r.read())


def _save_tokens(data: Path, tok: dict):
    p = Path(data) / TOKEN_FILE
    old = load_tokens(data)
    tok = {**old, **tok, "saved_at": _now().isoformat(timespec="seconds")}
    p.write_text(json.dumps(tok))
    p.chmod(0o600)


def load_tokens(data: Path) -> dict:
    try:
        return json.loads((Path(data) / TOKEN_FILE).read_text())
    except (OSError, ValueError):
        return {}


def callback(data: Path, code: str, state: str, opener=urllib.request.urlopen) -> str:
    """Monzo 跳回来时：核对 state，用 code 换钥匙。返回给她看的一句话。"""
    try:
        saved = json.loads((Path(data) / STATE_FILE).read_text())
    except (OSError, ValueError):
        saved = {}
    if not state or state != saved.get("state"):
        raise ValueError("授权链接过期了，回工作台设置页重新点一次“去授权”")
    cid, secret = _client()
    tok = _post(API + "/oauth2/token", {"grant_type": "authorization_code", "client_id": cid, "client_secret": secret,
                                        "redirect_uri": REDIRECT, "code": code}, opener)
    _save_tokens(data, {"access_token": tok["access_token"], "refresh_token": tok.get("refresh_token"),
                        "expires_at": (_now() + timedelta(seconds=int(tok.get("expires_in", 21600)))).isoformat()})
    (Path(data) / STATE_FILE).unlink(missing_ok=True)
    return "Monzo 连上了。现在打开 Monzo App，点一下批准（Allow access），小伴 就能看到你的花销了。"


def _token(data: Path, opener) -> str:
    tok = load_tokens(data)
    if not tok.get("access_token"):
        raise ValueError("Monzo 还没授权")
    if tok.get("expires_at") and datetime.fromisoformat(tok["expires_at"]) - _now() < timedelta(minutes=10) and tok.get("refresh_token"):
        cid, secret = _client()
        new = _post(API + "/oauth2/token", {"grant_type": "refresh_token", "client_id": cid, "client_secret": secret,
                                            "refresh_token": tok["refresh_token"]}, opener)
        _save_tokens(data, {"access_token": new["access_token"], "refresh_token": new.get("refresh_token", tok["refresh_token"]),
                            "expires_at": (_now() + timedelta(seconds=int(new.get("expires_in", 21600)))).isoformat()})
        return new["access_token"]
    return tok["access_token"]


def monzo_time(s: str) -> datetime:
    """Monzo 的时间小数秒位数不固定（如 21:54:18.66Z），Python 3.10 的 fromisoformat 只认 3 或 6 位，先补齐到 6 位。"""
    s = s.replace("Z", "+00:00")
    m = re.match(r"(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$", s)
    if m:
        s = f"{m[1]}.{m[2][:6].ljust(6, '0')}{m[3]}"
    return datetime.fromisoformat(s)


PAGE = 100


def sync_monzo(db, data: Path, opener=urllib.request.urlopen) -> int:
    """拉最近的交易记进 transactions；返回新记了几笔。"""
    token = _token(data, opener)
    ensure(db)
    accounts = [a for a in _get("/accounts", token, opener).get("accounts", []) if not a.get("closed")]
    tok = load_tokens(data)
    since = tok.get("synced_to") or (_now() - timedelta(days=89)).isoformat(timespec="seconds")
    added = 0
    for a in accounts:
        rows, cursor = [], since
        for _ in range(50):  # 一页最多 100 条，满页就从最后一条往后接着拉（2026-10-29 自查：第一次往回拉 89 天会被截断）
            page = _get("/transactions", token, opener, {"account_id": a["id"], "since": cursor, "limit": PAGE,
                                                         "expand[]": "merchant"}).get("transactions", [])
            rows += page
            if len(page) < PAGE or not page[-1].get("id"):
                break
            cursor = page[-1]["id"]
        for t in rows:
            if t.get("decline_reason") or t.get("amount") is None:
                continue
            m = t.get("merchant")
            name = (m.get("name") if isinstance(m, dict) else None) or t.get("description")
            at = monzo_time(t["created"])
            added += add(db, "monzo", at, t["amount"] / 100, t.get("currency", "GBP"), merchant=name,
                         category=t.get("category"), ext_id="monzo:" + t["id"])
    _save_tokens(data, {"synced_to": (_now() - timedelta(days=2)).isoformat(timespec="seconds"),  # 往回多拉两天，补晚入账的
                        "last_sync": _now().isoformat(timespec="seconds")})
    return added


# ---------- 她转发或截图给 小伴 的消费通知（2026-10-29：招行信用卡只有微信通知，快捷指令读不到） ----------
MARK = re.compile(r"\[记账\s+(-?[\d,.]+)\s*(CNY|GBP|人民币|英镑|元|£|¥)?\s*([^\]]*)\]")
PROMPT = ("她转发或截图银行、微信的消费通知给你时（比如招行信用卡的微信通知），在回复末尾加 [记账 金额 币种 商户 银行]，"
          "花出去写负数，例如 [记账 -56.8 CNY 瑞幸咖啡 招行信用卡]；看不清金额就问她，不要猜。同一条只记一次。")
CURRENCY = {"CNY": "CNY", "人民币": "CNY", "元": "CNY", "¥": "CNY", "GBP": "GBP", "英镑": "GBP", "£": "GBP"}


def note_chat(db, text: str, now: datetime = None) -> int:
    now = now or _now()
    got = 0
    for amount, cur, rest in MARK.findall(text):
        try:
            value = float(amount.replace(",", ""))
        except ValueError:
            continue
        rest = rest.strip()
        ext = "chat:" + hashlib.sha1(f"{now.date()}|{value}|{rest}".encode()).hexdigest()[:20]
        got += add(db, "chat", now, value, CURRENCY.get(cur or "CNY", "CNY"), merchant=rest[:80] or None, raw=rest, ext_id=ext)
    return got


def strip(text: str) -> str:
    return MARK.sub("", text)


# ---------- 给健康线和设置页 ----------
def month_spend(db, day: date) -> dict:
    """本月花了多少（按币种）：花出去的减去退款；转账、理财、认不出的不算。"""
    ensure(db)
    start = day.replace(day=1).isoformat()
    out = {}
    for r in db.conn.execute("SELECT currency, -SUM(amount) s FROM transactions WHERE day >= ? AND day <= ? "
                             "AND (amount < 0 OR category = 'refund') "
                             "AND COALESCE(category, '') NOT IN ('transfers', 'savings', 'unparsed', 'ignored') GROUP BY currency",
                             (start, day.isoformat())):
        out[r["currency"]] = round(r["s"], 2)
    return out


def recent(db, limit: int = 20) -> list:
    """最近几笔，给财务页看（她要能看到每一笔，2026-10-29）。"""
    ensure(db)
    return [dict(r) for r in db.conn.execute(
        "SELECT id, day, at, source, amount, currency, merchant, category FROM transactions ORDER BY at DESC, id DESC LIMIT ?", (limit,))]


def ignore(db, tid: int, on: bool = True):
    """她点“不算”：测试的、重复的、不想算进花销的（2026-10-29 她有一笔测试）。再点一次算回来。"""
    ensure(db)
    row = db.conn.execute("SELECT category, raw FROM transactions WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise ValueError("没有这一笔")
    db.conn.execute("CREATE TABLE IF NOT EXISTS ignored_prev (tid INTEGER PRIMARY KEY, category TEXT)")
    if on:
        if row["category"] != "ignored":  # 记下原来的分类，算回来时照原样（Monzo 的转账、存钱没有原文可以重认）
            db.conn.execute("INSERT OR REPLACE INTO ignored_prev (tid, category) VALUES (?, ?)", (tid, row["category"]))
        db.conn.execute("UPDATE transactions SET category = 'ignored' WHERE id = ?", (tid,))
    elif row["category"] == "ignored":
        prev = db.conn.execute("SELECT category FROM ignored_prev WHERE tid = ?", (tid,)).fetchone()
        back = prev["category"] if prev else (
            "refund" if REFUND.search(row["raw"] or "") else "transfers" if TRANSFER.search(row["raw"] or "") else None)
        db.conn.execute("UPDATE transactions SET category = ? WHERE id = ?", (back, tid))
        db.conn.execute("DELETE FROM ignored_prev WHERE tid = ?", (tid,))
    db.conn.commit()


# 汇率（2026-10-29 她要预算只定一个数：在英国这两个月英镑花销按汇率折成人民币一起算，回国就全是人民币）
FX_URL = "https://api.frankfurter.app/latest?from=GBP&to=CNY"
FX_FALLBACK = 9.5  # 1 英镑约合多少人民币；拉不到汇率时用


def fx_rate(db) -> float:
    db.conn.execute("CREATE TABLE IF NOT EXISTS fx (pair TEXT PRIMARY KEY, rate REAL NOT NULL, day TEXT)")
    row = db.conn.execute("SELECT rate FROM fx WHERE pair = 'GBPCNY'").fetchone()
    return row["rate"] if row else FX_FALLBACK


def refresh_fx(db, opener=urllib.request.urlopen) -> float:
    """每小时同步 Monzo 时顺便更新一次汇率（frankfurter，免费、不用 key）；失败就沿用上次的。"""
    fx_rate(db)
    try:
        with opener(FX_URL, timeout=15) as resp:
            rate = float(json.loads(resp.read())["rates"]["CNY"])
    except Exception:
        return fx_rate(db)
    db.conn.execute("INSERT INTO fx (pair, rate, day) VALUES ('GBPCNY', ?, ?) ON CONFLICT(pair) DO UPDATE SET rate = excluded.rate, day = excluded.day",
                    (rate, date.today().isoformat()))
    db.conn.commit()
    return rate


def convert(spent: dict, to: str, rate: float) -> float:
    """把各币种花销折成一个币种（只认英镑和人民币，别的币种不算进去）。"""
    k = {"CNY": {"CNY": 1, "GBP": rate}, "GBP": {"GBP": 1, "CNY": 1 / rate}}[to]
    return sum(v * k[c] for c, v in spent.items() if c in k)


def has_source(db, source: str) -> bool:
    """source 传 "sms" 表示任何一家银行短信。"""
    ensure(db)
    if source == "sms":
        return db.conn.execute("SELECT 1 FROM transactions WHERE source NOT IN ('monzo', 'chat') AND COALESCE(category, '') != 'unparsed' "
                               "LIMIT 1").fetchone() is not None
    return db.conn.execute("SELECT 1 FROM transactions WHERE source = ? LIMIT 1", (source,)).fetchone() is not None


def main():
    parser = argparse.ArgumentParser(description="财务：同步 Monzo 交易")
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        refresh_fx(store.db)
        if not load_tokens(args.data).get("access_token"):
            print("Monzo 还没授权，跳过")
            return
        print(f"Monzo 新记了 {sync_monzo(store.db, args.data)} 笔", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()


def last_unparsed(db):
    """最近一条收到了但没认出来的（设置页提示她检查快捷指令）。"""
    ensure(db)
    r = db.conn.execute("SELECT at, raw FROM transactions WHERE category = 'unparsed' ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None
