"""邮件打理（docs/design/digital-life-spec.md 第 7.5 节；她 2026-10-30：“以后你看过的邮件能不能不提醒我了，而且标为已读？
只有需要我关注的才提醒我去看”，并同意做）。

分两半：
- Mac 上（ethan/mailbridge.py，launchd 每 10 分钟）：读“邮件”App 各账户收件箱里的新邮件（发件人、主题、时间、正文开头），
  发到接收口 POST /cloud/mail；拿回服务器判好的处理办法，照做：不需要她看的移进该账户的“小伴整理/<类别>”并标已读，
  需要她看的留在收件箱、不动已读状态。只移动和标已读，不删、不发、不回。每一步记日志，能撤回。
- 服务器上（本文件）：新邮件先排队，后台马上分类（规则能判的不调模型；判不了的用模型，结构化输出 + 校验 + 留痕），
  记成证据（私密级）；需要她亲自做的事进待办，订单里有确定时间的进日程；每天傍晚 小伴 把当天需要她看的汇总成一条告诉她。
运行（服务器，接收口收到新邮件后会自己起）：python3 -m ethan.mail --data <数据目录>
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """CREATE TABLE IF NOT EXISTS mail_items (
    id INTEGER PRIMARY KEY, msg_key TEXT NOT NULL UNIQUE, account TEXT, sender TEXT, subject TEXT, received_at TEXT,
    body TEXT, status TEXT NOT NULL DEFAULT 'pending', category TEXT, attention INTEGER, reason TEXT, task_id INTEGER,
    event_id INTEGER, digested INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"""
FOLDERS = {"bill": "账单", "membership": "会员积分", "booking": "订单行程", "school": "学校", "work": "工作", "personal": "私人",
           "newsletter": "订阅资讯", "promo": "推广", "junk": "垃圾", "unsure": "待确认"}
ROOT = "小伴整理"
MAX_ITEMS, BODY = 50, 3000
# 规则先判（不调模型）：明显的推广、群发、垃圾
PROMO = re.compile(r"[（(]AD[)）]|广告|unsubscribe.{0,40}$|newsletter|% ?off|折优惠|限时|大促|Rate .* on Trustpilot|How did we do|Your opinion counts", re.I)
JUNK = re.compile(r"征稿|投稿|EI.{0,6}(检索|Scopus|index)|兼职|网赚|Call for papers|submit (your )?paper", re.I)
BULK_SENDERS = re.compile(r"noreply@sysmail\.dajie\.com|lagoujobs|zhaopin\.com|lietou|highpin|job592|insideapple|new\.itunes|applemusic|"
                          r"techinasia|meetup\.com|mba_stu_club|campusgroups", re.I)

# 签证、居留、使领馆、移民局这类：就算发件地址看着怪、Gmail 说像垃圾，也不能判成垃圾 / 推广 / 群发归档掉，一律留给她看
# （她 11-04：“这个为什么是垃圾？”——芬兰签证中心经 london.edu 转发的补材料通知被判成了垃圾）
MUST_SEE = re.compile(r"签证|申根|居留|大使馆|领事馆|移民局|visa\b|schengen|residence permit|\bBRP\b|embassy|consulate|home office|"
                      r"immigration|UKVI|biometric", re.I)
UNREADABLE = "（解析不了）"  # ethan/imapmail.py 给解析失败那封填的发件人
# 邮箱自己的拦截通知（某商学院 的 Mimecast 等）：真信可能被扣在里面，要她看一眼放不放（她 2026-10-31 同意）
HELD = re.compile(r"held messages|quarantin|messages? (are )?(being )?held|被隔离|隔离区|拦截的邮件", re.I)
DIGEST_AT = (18, 21)  # 傍晚汇总的时段（她的时区，小时）

SCHEMA_OUT = {"type": "object", "required": ["category", "attention", "reason"], "properties": {
    "category": {"type": "string", "enum": list(FOLDERS)}, "attention": {"type": "boolean"}, "fyi": {"type": ["boolean", "null"]},
    "reason": {"type": "string", "maxLength": 60},
    "todo": {"type": ["object", "null"], "properties": {"title": {"type": "string", "maxLength": 40}, "due": {"type": ["string", "null"]}}},
    "event": {"type": ["object", "null"], "properties": {
        "day": {"type": "string"}, "start": {"type": "string"}, "end": {"type": ["string", "null"]},
        "title": {"type": "string", "maxLength": 40}, "place": {"type": ["string", "null"]}}},
    # 关于她的有效信息（ontology-plan 1.6，11-04）：录取、签证进度、预约改期这类，每条一句；广告、群发给空
    "about_her": {"type": ["array", "null"], "maxItems": 3, "items": {"type": "object", "required": ["statement", "type"], "properties": {
        "statement": {"type": "string", "maxLength": 60}, "type": {"type": "string", "enum": ["application", "visa", "booking", "account", "case"]}}}}}}


def ensure(db):
    db.conn.execute(SCHEMA)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(mail_items)")}
    if "applied" not in have:  # 服务器直接收的：移没移过（ethan/imapmail.py）
        db.conn.execute("ALTER TABLE mail_items ADD COLUMN applied INTEGER NOT NULL DEFAULT 0")
    if "via" not in have:  # 发到 hi@example.com 的“主页来信”（主页会话 2026-10-30 请求）
        db.conn.execute("ALTER TABLE mail_items ADD COLUMN via TEXT")
    # 她 11-04 同意的三条：fyi = “知道一下”（进行中的大事、不用她做什么）；restore = 她点了“这封不是垃圾”，要放回收件箱
    # （1 = 等放回，2 = 放回了）；spam = 在 Gmail 垃圾箱里发现的
    for col in ("fyi", "restore", "spam"):
        if col not in have:
            db.conn.execute(f"ALTER TABLE mail_items ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key_of(item: dict) -> str:
    """按邮件自带的 Message-ID 认：服务器直接收（ethan/imapmail.py）和 Mac 程序收到同一封，只处理一次。"""
    return "msgid:" + str(item.get("id", "")).strip().strip("<>")


# ---------- 接收口：收新邮件、交还处理办法 ----------
def action(row) -> dict:
    """给 Mac 的处理办法：需要她看的不动；其余移进分类文件夹并标已读。"""
    if row["attention"]:
        return {"key": row["msg_key"], "keep": True}
    return {"key": row["msg_key"], "folder": f"{ROOT}/{FOLDERS[row['category']]}", "mark_read": True}


def receive(db, payload: dict) -> dict:
    """Mac 发来 {"items": [{account, id, sender, subject, received_at, body}], "ask": [已发过、还没拿到办法的 key]}。
    新的先排队；返回已经判好的办法和还在排队的 key。"""
    ensure(db)
    items = [i for i in (payload.get("items") or [])[:MAX_ITEMS] if isinstance(i, dict) and i.get("id")]
    new = 0
    for i in items:
        cur = db.conn.execute(
            "INSERT OR IGNORE INTO mail_items (msg_key, account, sender, subject, received_at, body, via, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (key_of(i), str(i.get("account", ""))[:80], str(i.get("sender", ""))[:200], str(i.get("subject", ""))[:300],
             str(i.get("received_at", ""))[:40], str(i.get("body", ""))[:BODY], "example.com" if i.get("via") == "example.com" else None, _now()))
        new += cur.rowcount
    db.conn.commit()
    keys = [key_of(i) for i in items] + [str(k) for k in (payload.get("ask") or [])[:500]]
    rows = db.conn.execute(f"SELECT * FROM mail_items WHERE msg_key IN ({','.join('?' * len(keys))})", keys).fetchall() if keys else []
    done = [action(r) for r in rows if r["status"] == "done"]
    waiting = [r["msg_key"] for r in rows if r["status"] != "done"]
    return {"ok": True, "new": new, "actions": done, "pending": waiting}


# ---------- 分类 ----------
def rules(sender: str, subject: str, body: str):
    text = f"{subject}\n{body[:500]}"
    if HELD.search(subject):
        return {"category": "membership", "attention": True, "reason": "邮箱扣下了几封信，看看要不要放出来", "todo": None, "event": None}
    if JUNK.search(text):
        return {"category": "junk", "attention": False, "reason": "规则：征稿、兼职这类垃圾", "todo": None, "event": None}
    if PROMO.search(subject) or (PROMO.search(body[-300:]) and "unsubscribe" in body.lower()[-300:] and not re.search(r"booking|confirm|订单|确认", subject, re.I)):
        return {"category": "promo", "attention": False, "reason": "规则：广告促销", "todo": None, "event": None}
    if BULK_SENDERS.search(sender):
        return {"category": "newsletter", "attention": False, "reason": "规则：群发资讯", "todo": None, "event": None}
    return None


def check(data: dict) -> list:
    errors = []
    t = data.get("todo")
    if t and t.get("due"):
        try:
            date.fromisoformat(t["due"])
        except ValueError:
            errors.append("todo.due 不是 YYYY-MM-DD")
    e = data.get("event")
    if e:
        try:
            date.fromisoformat(e["day"])
            for k in ("start", "end"):
                if e.get(k):
                    datetime.strptime(e[k], "%H:%M")
        except (ValueError, KeyError):
            errors.append("event 的日期或时间格式不对")
    for a in data.get("about_her") or []:
        if len((a.get("statement") or "").strip()) < 4:
            errors.append("about_her 里有空的说法")
    return errors


def must_see(data, sender: str, subject: str, body: str):
    """分好类以后的兜底：签证这类被判成垃圾、推广、群发的，改回“要她看”。"""
    if data and data.get("category") in ("junk", "promo", "newsletter", "unsure") and MUST_SEE.search(f"{sender}\n{subject}\n{body[:1500]}"):
        return {**data, "category": "membership", "attention": True,
                "reason": "签证 / 居留相关，不当垃圾；发件人看着怪的话先核对一下真假"}
    return data


def classify(router, db, sender: str, subject: str, body: str, ref: str = ""):
    return must_see(_classify(router, db, sender, subject, body, ref), sender, subject, body)


def _classify(router, db, sender: str, subject: str, body: str, ref: str = ""):
    got = rules(sender, subject, body)
    if got:
        return got
    from . import llm
    version, system = llm.load_prompt("mail_classify")
    text = f"发件人：{sender}\n主题：{subject}\n\n正文开头：\n{body[:BODY]}"
    return llm.structured(router, db, "mail_classify", system, text, SCHEMA_OUT, check=check, version=version,
                          input_ref=ref, purpose="mail-classify")


def process(store, router, limit: int = 40) -> dict:
    """把排队的邮件判完：记证据、建待办和日程。"""
    db = store.db
    ensure(db)
    from . import events, lifebase
    lifebase.ensure(db)
    got = {"done": 0, "failed": 0}
    # 先认领再判，两个后台进程同时跑也不会重复建待办；认领了却没判完（进程死了）的，半小时后放回队列
    me = f"working:{os.getpid()}"
    db.conn.execute("UPDATE mail_items SET status = 'pending' WHERE status LIKE 'working:%' AND created_at < datetime('now', '-30 minutes')")
    db.conn.execute("UPDATE mail_items SET status = ? WHERE id IN (SELECT id FROM mail_items WHERE status = 'pending' ORDER BY id LIMIT ?)",
                    (me, limit))
    db.conn.commit()
    for r in db.conn.execute("SELECT * FROM mail_items WHERE status = ? ORDER BY id", (me,)).fetchall():
        from . import grokexport
        grok_urls = grokexport.is_export_mail(r["sender"] or "", r["subject"] or "", r["body"] or "")
        if grok_urls:  # xAI 的数据导出邮件：小伴 自己下载导入，不算要她处理的事（她 11-04：“都发到邮箱里为什么你不能自动读？”）
            data = {"category": "personal", "attention": False, "fyi": True, "reason": grokexport.HANDLING, "todo": None, "event": None}
        elif r["sender"] == UNREADABLE:  # 服务器解析不了的那封（ethan/imapmail.py）：不猜，留在收件箱不动，请她在邮件 App 里看一眼
            data = {"category": "unsure", "attention": True, "reason": "这封服务器读不了，请你在邮件 App 里看一眼", "todo": None, "event": None}
        else:
            data = classify(router, db, r["sender"] or "", r["subject"] or "", r["body"] or "", r["msg_key"])
        if data is None:  # 两次都没过校验：先当待确认，留在收件箱，不误移
            data = {"category": "unsure", "attention": True, "reason": "没判出来，先留在收件箱", "todo": None, "event": None}
            got["failed"] += 1
        if data.get("attention"):  # 她连着两次对这个发件人点“不用管”：不再算需要她看（ethan/desk.py）
            from . import desk
            if desk.quiet_sender(db, r["sender"] or "") and not MUST_SEE.search(f"{r['sender']}\n{r['subject']}\n{(r['body'] or '')[:1500]}"):
                data = {**data, "attention": False, "todo": None, "reason": "她之前对这个发件人都点了不用管"}
        bank_email(db, r)  # 银行消费邮件：记一笔账（她 2026-11-01：邮件被整理走了，招行的消费就再没进账）
        ev_id = lifebase.try_evidence(db, "email", f"mail:{r['msg_key']}", r["received_at"] or _now(),
                                      f"发件人：{r['sender']}\n主题：{r['subject']}\n{r['body']}", visibility="private")
        if grok_urls:
            grokexport.queue(db, r["id"], grok_urls)
        if ev_id and data.get("about_her") and data.get("category") not in ("junk", "promo", "newsletter"):
            # 收到的邮件里关于她的有效信息记成说法（第三方来源，语境写清发件人；ontology-plan 1.6）。只写说法，不动待办和计划
            lifebase.write_mail_facts(db, ev_id, r["sender"] or "", r["received_at"] or _now(), data["about_her"])
        task_id = event_id = None
        from . import locks
        if data.get("todo") and data["todo"].get("title") and locks.she_deleted(db, data["todo"]["title"]):
            data = {**data, "todo": None}  # 她删掉过的同一件事，邮件不再建回来
        if data.get("attention") and data.get("todo") and data["todo"].get("title") and _already_tasked(db, data["todo"]["title"]):
            data = {**data, "todo": None}  # 同一件事已经有待办，或者最近已经做完（2026-11-01 交行账单她早还清了，旧账单邮件又建了一条）
        ev = data.get("event") or {}
        if data.get("todo") and ev.get("day") and ev.get("start") and (data["todo"].get("due") or ev["day"]) >= ev["day"]:
            data = {**data, "todo": None}  # 有确定时间的活动进日程就够了，不另建待办；要提前准备的（截止在活动之前）才留（她 2026-11-01：送别活动是日程，要什么待办）
        if data.get("attention") and data.get("todo") and data["todo"].get("title"):
            from . import naming
            task_id = db.add_task(naming.title(data["todo"]["title"]), due=data["todo"].get("due"))  # 标题式（ui-spec 第 5 节）
            db.conn.execute("UPDATE tasks SET owner = 'mia', source_type = 'email', source_ref = ?, status = 'open' WHERE id = ?",
                            (f"mail:{r['msg_key']}", task_id))
        e = data.get("event")
        if e and e.get("day") and e.get("start"):
            try:
                end = e.get("end") or f"{min(int(e['start'][:2]) + 2, 23):02d}:{e['start'][3:5]}"
                event_id = events.add(db, e["day"], e["start"], end, e.get("title") or (r["subject"] or "")[:20], e.get("place") or "",
                                      note="来自邮件")
            except ValueError:
                event_id = None
        fyi = 1 if (data.get("fyi") and not data["attention"]) else 0
        db.conn.execute("UPDATE mail_items SET status = 'done', category = ?, attention = ?, reason = ?, task_id = ?, event_id = ?, fyi = ? WHERE id = ?",
                        (data["category"], 1 if data["attention"] else 0, data.get("reason"), task_id, event_id, fyi, r["id"]))
        db.conn.commit()
        if data["attention"] and r["sender"] != UNREADABLE:  # 收件台默认给她看中文（2026-10-30），先译好
            try:
                from . import desk
                desk.translate(db, router, "mail", r["id"], "body")
            except Exception:
                pass
        got["done"] += 1
    return got


def kick(root, spawn=subprocess.Popen) -> bool:
    try:
        spawn([sys.executable, "-m", "ethan.mail", "--data", str(root)], cwd=str(Path(__file__).resolve().parent.parent),
              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


# ---------- 傍晚汇总 ----------
def to_digest(db) -> list:
    ensure(db)
    from . import desk
    desk.ensure(db)
    # 她在工作台已经处理过的（回了、不用管、记成待办又勾掉了）不再汇报（2026-10-31 交行账单她勾完了 小伴 傍晚又说一遍）
    return [dict(r) for r in db.conn.execute(
        "SELECT m.id, m.account, m.sender, m.subject, m.reason, m.task_id FROM mail_items m LEFT JOIN tasks t ON t.id = m.task_id "
        "WHERE m.status = 'done' AND m.attention = 1 AND m.digested = 0 AND COALESCE(m.desk_state, 'open') IN ('open', 'draft', 'drafting', 'todo') "
        "AND COALESCE(t.done, 0) = 0 ORDER BY m.id")]


def archived(db, days: int = 1) -> list:
    """这几天 小伴 替她归进垃圾、推广、群发的（她 11-04 同意：傍晚汇总报个数，工作台能抽查、一键说“不是垃圾”）。"""
    ensure(db)
    return [dict(r) for r in db.conn.execute(
        "SELECT id, account, sender, subject, category, reason, created_at FROM mail_items WHERE status = 'done' AND attention = 0 "
        "AND fyi = 0 AND restore = 0 AND category IN ('junk', 'promo', 'newsletter') AND created_at >= datetime('now', ?) ORDER BY id DESC",
        (f"-{int(days)} days",))]


def not_junk(db, mail_id: int) -> dict:
    """她点了“这封不是垃圾”：改成要她看，下一轮收信时放回收件箱（ethan/imapmail.py）。"""
    ensure(db)
    from . import desk
    desk.ensure(db)  # desk_state 这些列是收件台建的
    r = db.conn.execute("SELECT * FROM mail_items WHERE id = ?", (int(mail_id),)).fetchone()
    if r is None:
        return {"ok": False, "msg": "找不到这封"}
    db.conn.execute("UPDATE mail_items SET attention = 1, fyi = 0, restore = 1, category = 'unsure', reason = '你说这封不是垃圾', "
                    "desk_state = 'open', digested = 1 WHERE id = ?", (r["id"],))
    db.conn.commit()
    return {"ok": True, "msg": "好，这封放回收件箱，也在这里等你看"}


SPAM_WATCH = re.compile(MUST_SEE.pattern + r"|银行|bank\b|payment (failed|declined)|扣款失败|学校|university|school|london\.edu|tuition|学费|offer|录取",
                        re.I)


def receive_spam(db, items: list) -> int:
    """Gmail 垃圾箱里发现的、像是重要的（签证、银行、学校）：只提醒她，不移动、不改已读（她 11-04 同意）。"""
    ensure(db)
    n = 0
    for i in items:
        if not SPAM_WATCH.search(f"{i.get('sender', '')}\n{i.get('subject', '')}\n{str(i.get('body', ''))[:1500]}"):
            continue
        cur = db.conn.execute(
            "INSERT OR IGNORE INTO mail_items (msg_key, account, sender, subject, received_at, body, created_at, status, category, attention, "
            "reason, applied, spam) VALUES (?, ?, ?, ?, ?, ?, ?, 'done', 'unsure', 1, ?, 1, 1)",
            (key_of(i), str(i.get("account", ""))[:80], str(i.get("sender", ""))[:200], str(i.get("subject", ""))[:300],
             str(i.get("received_at", ""))[:40], str(i.get("body", ""))[:BODY], _now(),
             "在 Gmail 垃圾箱里，像是签证 / 银行 / 学校的信，看一眼是不是真的"))
        n += cur.rowcount
    db.conn.commit()
    return n


def mark_digested(db, ids):
    db.conn.executemany("UPDATE mail_items SET digested = 1 WHERE id = ?", [(i,) for i in ids])
    db.conn.commit()


def main():
    parser = argparse.ArgumentParser(description="把排队的邮件分类")
    parser.add_argument("--data", type=Path, required=True)
    args = parser.parse_args()
    from .cli import build_router
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        print(json.dumps(process(store, build_router(args.data, 50.0)), ensure_ascii=False), flush=True)
        from . import grokexport
        if grokexport.has_pending(store.db):  # 刚认出 xAI 的导出邮件：后台去下载（ethan/grokexport.py）
            grokexport.kick(args.data)
    finally:
        store.close()

BANK_SENDER = re.compile(r"cmbchina|95555|招商银行|bankcomm|95559|交通银行", re.I)
BANK_SPEND = re.compile(r"消费|快捷支付|扣款|支出|退款|退货|入账|网上支付")


def bank_email(db, r) -> dict:
    """招行、交行的消费通知邮件交给记账（ethan/finance.py 的银行短信解析，邮件正文同一个格式）。认不出的不记。
    以前靠 Mac 上的邮件规则转过去，10-30 邮件整理上线后邮件被移走、规则就没再触发。"""
    sender, text = r["sender"] or "", " ".join(f"{r['subject'] or ''} {r['body'] or ''}".split())
    if not BANK_SENDER.search(sender + " " + (r["subject"] or "")) or not BANK_SPEND.search(text):
        return {}
    from . import finance
    try:
        at = datetime.fromisoformat(r["received_at"]) if r["received_at"] else None
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
    except ValueError:
        at = None
    body = " ".join((r["body"] or "").split())
    if finance.parse_cmb_digest(body):  # 招行信用卡“每日信用管家”：一封好几笔
        n = finance.record_cmb_digest(db, body, sender)
        return {"parsed": True, "amount": 0, "digest": n}
    m = re.search(r"您(的)?(账户|尾号|信用卡).{0,200}?(元|RMB|CNY)", body)
    got = finance.record_cmb(db, m.group(0) if m else body[:300], at, sender)
    return got if got.get("parsed") else {}


def _already_tasked(db, title: str, days: int = 45) -> bool:
    """邮件要建的待办，和没做完的、或最近 45 天做完的是同一件事：标题差不多，或者里面的金额对得上。"""
    import re
    from .core.agent import similar_task
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    olds = [r[0] for r in db.conn.execute("SELECT title FROM tasks WHERE done = 0 OR done_at >= ?", (since,))]
    money = {m.replace(",", "") for m in re.findall(r"\d[\d,]*\.\d{2}|\d{4,}", title)}
    return similar_task(title, olds) or any(m in o.replace(",", "") for m in money for o in olds)


if __name__ == "__main__":
    main()
