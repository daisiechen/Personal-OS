"""记忆的使用者登记表和纠错单（docs/design/memory-alignment.md，她 2026-10-31 认可“先做吧”）。

1. 登记表 USERS：谁在用这份记忆、能看到哪一级、在哪里用。所有读记忆的地方（memsearch.search、portrait.chat_text）
   都必须报上自己是谁，没登记的直接报错；tests/test_memusers.py 扫代码把关，绕开入口或用了没登记的名字，测试不过、上线自动退回。
   新功能要用记忆：先在这里加一行，并在 docs 里写明用途（AGENTS.md）。
2. 纠错单：她说“你记错了”时，小伴 在回复末尾写 [纠错 她 维度 错：… 对：…]（他记错自己的事写 [纠错 自己 错：… 对：…]），
   她把截图发给开发会话时，开发会话写 migrations/ 脚本调 open_ticket。代码做三件事：
   - 改对：对的说法记成她锁定的事实（来源“她本人纠正”，可信度 1，只有她能改）；错的那条作废，历史留着；
   - 查错在哪一层（代码初判，不靠模型）：没告诉过 / 没记住 / 记错了 / 没查到 / 用错了 / 过时了；
   - 同一件事第二次被纠正，标“复发”。
3. stats()：每个使用者用了多少、正确率、错在哪层、复发，工作台“我”页按登记表自动生成，新登记的自动多一行。
"""

import json
import re
from datetime import datetime, timedelta, timezone

from . import lifebase, tx

# 新使用者在这里登记一行（她 2026-10-31：“未来如果有新进入者，应该是要到这里登记一下”）
# tier（ontology-plan 3.3，她 2026-11-04 定按三档设一次）：internal 自己人（照内部规则看）/ partner 合作方（她信任的外部 agent）/
# public 公开（Dais、主页访客、不认识的 agent）。对外两档按“我”页的对外开关（lifebase.disclose）把关，新登记的只写 tier 就自动按那一档；
# 对外两档的 level 写 "public"（不读内部的画像、检索场景）。
USERS = {
    "ethan": {"tier": "internal", "name": "小伴", "level": "private", "uses": "她的一切、你们之间的过往、他自己的生活",
              "where": "聊天、主动消息、提醒和跟进", "since": "2026-10-31", "hypothesis": True,
              "questions": [1, 2, 3, 4, 5, 11, 13, 18, 22, 23, 26, 27, 28]},
    "twin": {"tier": "public", "name": "Dais", "level": "public", "uses": "公开级那部分", "where": "主页接待访客、起草留言回复", "since": "2026-10-31",
             "questions": [14, 15, 16]},
    "planner": {"tier": "internal", "name": "计划器", "level": "private", "uses": "目标、作息、偏好、她删掉过的事", "where": "每日计划、周报",
                "since": "2026-10-31", "questions": [1, 4, 6, 8, 9, 13, 22, 27]},
    "prep": {"tier": "internal", "name": "准备工作", "level": "private", "uses": "替她办事要用的偏好和事实", "where": "准备的东西、调研",
             "since": "2026-10-31", "questions": [3, 4, 12, 13, 22, 27]},
    "mail": {"tier": "internal", "name": "邮件起草", "level": "private", "uses": "她的背景、和对方的往来、她确认过的写作规范、批准过的“替你回邮件” skill",
             "where": "“邮件和留言”里的回复草稿", "since": "2026-10-31", "questions": [3, 4, 5, 12, 13]},
    "site": {"tier": "public", "name": "主页文案 / 自媒体", "level": "public", "uses": "标了“上主页”的公开内容", "where": "主页、内容草稿",
             "since": "2026-10-31", "questions": [14, 15]},
    # 日记照旧不读记忆（只读当天原始记录），2026-11-05 只多读一项：她确认过的写作规范（onto_query.writing_style），别的记忆一律不给（memory_only 把关）
    "diary": {"tier": "internal", "name": "我的日记", "level": "private", "uses": "那天她自己在聊天里说的话、完成的事、出门、工作时长；她确认过的写作规范",
              "where": "日记本（用她的口吻写她的一天）", "since": "2026-11-01", "questions": [], "memory_only": ["writing_style"]},
    "me_page": {"tier": "internal", "name": "属性分数", "level": "private", "uses": "全部", "where": "7 项属性、画像展示", "since": "2026-10-31", "all": True,
                "questions": [7, 11, 17, 18, 19, 23]},
    "gaps": {"tier": "internal", "name": "补缺口提问", "level": "private", "uses": "每个方面有几条说法、她的聊天和文件里有没有提过某个话题（只判断有没有，不读出内容）",
             "where": "小伴 每天挑要问她的问题（ontology 1.7）", "since": "2026-11-04", "questions": []},
    "relations": {"tier": "internal", "name": "关系（人物卡）", "level": "private", "uses": "她和某个人的微信交接记录（只读这个人的那几份）、人物卡",
                  "where": "每晚整理人物卡、工作台“关系”页、该联系谁三张清单（docs/design/relations.md）", "since": "2026-11-04",
                  "questions": [3]},
    # ontology-plan 3.1（11-04）：每周复盘、巡检原来都记在“系统维护”名下，拆出来各自登记要回答的题，好分开算按题答了多少
    "review": {"tier": "internal", "name": "每周复盘", "level": "private", "uses": "这周的计划和完成、哪几天没记录、开销、这季副本的结果和她的复盘、该联系谁的清单（人物卡）",
               "where": "周日周报和总览（ethan/report.py、ethan/weekreview.py）", "since": "2026-11-04", "internal": True,
               "questions": [6, 7, 10, 26]},
    "patrol": {"tier": "internal", "name": "巡检和维修工", "level": "private", "uses": "改动记录、她否掉的事、生效的决定、纠错单、说过要做的事",
               "where": "系统巡检（ethan/checkup.py）、开发会话修问题", "since": "2026-11-04", "internal": True,
               "questions": [23, 24, 25, 28]},
    # 写作规范（docs/design/style-and-skills.md，她 11-05：“提前做”）：只读她自己写的（发出的邮件、她写的日记、她改草稿的改前→改后、
    # 荧光笔和“写得不好”的批注、聊天里她的原话抽样），经 onto_query.style_materials；归纳出的规范她逐条确认才生效
    "style": {"tier": "internal", "name": "写作规范", "level": "private", "internal": True,
              "uses": "她自己写的文字：发出的邮件、她写的日记、改系统草稿时改动的句子、荧光笔和批注、聊天里她的原话（抽样）",
              "where": "归纳“你的写作规范”草稿（ethan/style.py），每周日夜里增量更新", "since": "2026-11-05", "questions": [2]},
    "system": {"tier": "internal", "name": "系统维护", "level": "private", "uses": "全部", "where": "每晚整理、每周复盘、题集、命令行", "since": "2026-10-31",
               "internal": True, "all": True, "questions": []},
}
# “我”页上按实体看（她 2026-10-31：“你放的应该是在我们这个体系里面的实体”）：计划器、准备、邮件起草、属性分数都是 OS 在做的事；
# Dais和主页文案都是个人主页。登记表照旧按用途细分（进出闸门、统计口径不变），页面上合并。新登记的用途要归到一个实体里。
ENTITIES = {"ethan": ("小伴", ["ethan", "gaps"]), "twin": ("Dais", ["twin"]), "site": ("个人主页", ["site"]), "os": ("OS", ["planner", "prep", "mail", "me_page", "diary", "relations", "style"])}


def entity_of(user: str) -> str:
    return next((k for k, (_, us) in ENTITIES.items() if user in us), user)


def entity_stats(db, days: int = 7, now: datetime = None) -> list:
    """把 stats 按实体加起来。"""
    by = {u["user"]: u for u in stats(db, days, now)}
    out = []
    for key, (name, members) in ENTITIES.items():
        us = [by[m] for m in members if m in by]
        add = lambda k: sum(u[k] for u in us)
        layers = {}
        for u in us:
            for k, v in u["layers"].items():
                layers[k] = layers.get(k, 0) + v
        answers = add("with_memory") + add("misses")
        out.append({"user": key, "name": name, "members": members, "parts": "、".join(USERS[m]["name"] for m in members),
                    "where": "；".join(USERS[m]["where"] for m in members), "internal": False,
                    "queries": add("queries"), "with_memory": add("with_memory"), "misses": add("misses"), "corrections": add("corrections"),
                    "layers": layers, "recurrences": add("recurrences"),
                    "accuracy": round(1 - min(add("corrections"), answers) / answers, 3) if answers else None,
                    "connected": any(u["connected"] for u in us)})
    return out


LAYERS = {"changed": "变了（不算错）", "untold": "没告诉过", "missed": "没记住", "wrong": "记错了", "not_found": "没查到", "misused": "用错了", "outdated": "过时了",
          "self": "他自己的事"}
SCHEMA = """CREATE TABLE IF NOT EXISTS corrections (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, user TEXT NOT NULL, about TEXT NOT NULL, entry TEXT NOT NULL, quote TEXT,
    wrong TEXT, right TEXT NOT NULL, dimension TEXT, wrong_fact_id INTEGER, fact_id INTEGER, layer TEXT NOT NULL,
    recurrence_of INTEGER, status TEXT NOT NULL DEFAULT 'fixed')"""
MARK = re.compile(r"\[纠错 (她|自己)(?: ([^\s\]]+))? 错：([^\]]*?) 对：([^\]]+?)\]")
NOW_CHANGED = re.compile(r"现在|已经不|不再|换了|搬了|改了|以前是|之前是")
SELF_PAGE = "ethan-life/self-corrections"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tier_of(user: str) -> str:
    """这个使用者在哪一档：internal / partner / public（lifebase.TIERS）。"""
    return check(user).get("tier") or ("public" if USERS[user]["level"] == "public" else "internal")


def external(user: str) -> bool:
    """对外的（合作方、公开两档）。"""
    return tier_of(user) in lifebase.EXTERNAL_TIERS


def may_read(user: str, know: str = None, read_by: str = None, dimension: str = None) -> bool:
    """读取与推理（ontology.md 6.1 节，2026-11-03 接进检索）：
    - 系统的假设：只有 小伴 能读（只能拿来和她本人讨论），计划器、准备、邮件、日记、Dais、主页都不读；
    - 条目写了读取名单的，只有名单里的能读；没写的按原来的级别规则；
    - “我”页和系统维护看全部（她本人要能看到系统对她的所有猜测）。
    对外两档（合作方、公开）这里只挡猜测、预测和读取名单；**哪一类能说**要用 lifebase.disclose（按对外开关和逐条例外），
    所有对外的读取路径（检索、画像、查询函数）都走它（ontology-plan 3.3）。"""
    who = check(user)
    if who.get("all"):
        return True
    if know == "hypothesis" and not who.get("hypothesis"):
        return False
    if external(user) and know in ("hypothesis", "prediction"):
        return False  # 对外的：猜测、预测永远不给
    if external(user) and dimension in lifebase.NEVER_PUBLIC and know != "disclosure":
        return False  # 没走 disclose 的路径按最严：身体、钱、关系、性格不给（检索、画像、查询函数对外都改走 lifebase.disclose）
    if read_by:
        try:
            names = {x.get("user") if isinstance(x, dict) else x for x in json.loads(read_by)}
        except ValueError:
            return False  # 写坏了按最严
        if names and user not in names:
            return False
    return True


def check(user: str) -> dict:
    if user not in USERS:
        raise ValueError(f"记忆使用者“{user}”没登记：先在 ethan/memusers.py 的 USERS 里加一行（AGENTS.md）")
    return USERS[user]


def may_use(user: str, what: str) -> bool:
    """登记了 memory_only 的使用者只能用那几项（比如日记只读写作规范）；what 是 onto_query 的函数名，或 "search"（检索）、"portrait"（画像）。"""
    only = check(user).get("memory_only")
    return only is None or what in only


def need(user: str, what: str) -> dict:
    who = check(user)
    if not may_use(user, what):
        raise ValueError(f"“{who['name']}”只登记了读 {who['memory_only']}，不能用 {what}（ethan/memusers.py）")
    return who


def ensure(db):
    lifebase.ensure(db)
    db.conn.execute(SCHEMA)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(corrections)")}
    # kind：wrong 错了（从来不是这样，算一次错）/ changed 变了（以前对、现在不一样，不算错）；status：undone 是她撤销的
    for col, ddl in (("kind", "TEXT NOT NULL DEFAULT 'wrong'"), ("status_note", "TEXT"), ("undone_at", "TEXT")):
        if col not in have:
            db.conn.execute(f"ALTER TABLE corrections ADD COLUMN {col} {ddl}")


# ---------- 纠错单 ----------
def _dim(name: str):
    if not name:
        return None
    if name in lifebase.DIMENSIONS:
        return name
    return next((k for k, v in lifebase.DIM_NAMES.items() if name in v or v in name), None)


def _best_fact(db, text: str):
    """库里和这句话说的是同一件事的、仍成立的事实。"""
    from . import memsearch
    from .core.agent import find_similar
    memsearch.refresh(db)
    for r in memsearch.search(db, [text], "chat", limit=3, kinds=("fact",), user="system", log=False):
        f = db.conn.execute("SELECT * FROM facts WHERE id = ?", (int(r["key"].split(":")[1]),)).fetchone()
        if f and f["valid_to"] is None and f["status"] != "invalid" and find_similar(text, [f["statement"]]):  # 说的是同一件事才算
            return dict(f)
    return None


def _layer(db, user: str, wrong: str, right: str, quote: str, wrong_fact, right_fact) -> str:
    """错在哪一层：代码按顺序查，不靠模型猜（方案第 2 节）。"""
    if wrong_fact:
        return "outdated" if NOW_CHANGED.search(quote or "") else "wrong"
    if right_fact:  # 记忆里是对的：看上一轮有没有查出来
        last = db.conn.execute("SELECT keys FROM retrieval_log WHERE user = ? ORDER BY id DESC LIMIT 1", (user,)).fetchone()
        used = last and f"fact:{right_fact['id']}" in json.loads(last["keys"] or "[]")
        return "misused" if used else "not_found"
    from . import memsearch
    from .core.agent import find_similar
    said = [r["text"] for r in memsearch.search(db, [right], "chat", limit=5, kinds=("evidence",), user="system", log=False)
            if r["label"] != "她纠正过"]  # 她以前纠正时说的不算“说过没记住”
    return "missed" if find_similar(right.replace("她", ""), [s.replace("她", "") for s in said]) else "untold"


def _recurrence(db, about: str, wrong: str, right: str):
    from .core.agent import find_similar
    rows = db.conn.execute("SELECT id, wrong, right FROM corrections WHERE about = ? ORDER BY id", (about,)).fetchall()
    for r in rows:
        if find_similar(right, [r["right"]]) or (wrong and r["wrong"] and find_similar(wrong, [r["wrong"]])):
            return r["id"]
    return None


def open_ticket(db, right: str, wrong: str = "", about: str = "her", user: str = "ethan", entry: str = "chat", quote: str = "",
                dimension: str = None, wiki=None, kind: str = "wrong", since: str = None, until: str = None, until_cond: str = None) -> dict:
    """开一张纠错单并照办。about：her（她的事）或 ethan_self（小伴 记错他自己的事）。
    kind：wrong 错了——旧说法作废，算一次错；changed 变了——旧说法到 since 为止、留作历史，不算错（2026-10-31 她定）。
    since / until / until_cond：对的说法从哪天起、到哪天、在什么条件下成立（比如这个赛季、在伦敦期间）。"""
    if kind not in ("wrong", "changed"):
        raise ValueError("kind 只能是 wrong 或 changed")
    ensure(db)
    check(user)
    right, wrong = right.strip()[:120], (wrong or "").strip()[:120]
    if not right:
        raise ValueError("纠错要有对的说法")
    again = _recurrence(db, about, wrong, right)
    if about == "ethan_self":
        if wiki is not None:  # 他被纠正过的自己的事，聊天时带着（ethan/life.py）
            old = wiki.read(SELF_PAGE)["body"] if wiki.exists(SELF_PAGE) else ""
            line = f"- {_now()[:10]} 你说过“{wrong}”，不对，实际是：{right}"
            wiki.write(SELF_PAGE, "她纠正过的关于你自己的事", (old.rstrip() + "\n" + line).strip())
        cur = db.conn.execute("INSERT INTO corrections (at, user, about, entry, quote, wrong, right, layer, recurrence_of) "
                              "VALUES (?, ?, ?, ?, ?, ?, ?, 'self', ?)", (_now(), user, about, entry, quote, wrong, right, again))
        db.conn.commit()
        _to_eval(db, cur.lastrowid)
        return {"id": cur.lastrowid, "layer": "self", "recurrence_of": again}
    wrong_fact = _best_fact(db, wrong) if wrong else None
    right_fact = _best_fact(db, right)
    if right_fact and wrong_fact and right_fact["id"] == wrong_fact["id"]:  # 对错两句太像，分不清：不动旧的
        wrong_fact = None
    layer = _layer(db, user, wrong, right, quote, wrong_fact, right_fact)
    dim = _dim(dimension) or (wrong_fact or right_fact or {}).get("dimension") or "preferences"
    with tx.atomic(db):  # 对的说法、锁定、旧的作废、纠错单一个事务：每晚整理插在中间看不到“锁定”会改掉它（Codex 评审 11-05）
        ev = lifebase.add_evidence(db, "correction", f"correction:{entry}:{_now()}:{right[:30]}", _now(), f"她纠正：{quote or right}")
        from .core.agent import find_similar
        if right_fact and find_similar(right, [right_fact["statement"]]):  # 库里已经有这句对的：锁上它，不另记一条
            fid = right_fact["id"]
            db.conn.execute("UPDATE facts SET confidence = 1.0, last_confirmed_at = ? WHERE id = ?", (_now(), fid))
        else:
            fid = lifebase.add_fact(db, dim, right, [ev], confidence=1.0, importance=5, tags=("her_correction",),
                                    visibility=(wrong_fact or {}).get("visibility"), valid_from=since)
        db.conn.execute("UPDATE facts SET locked = 1, until = COALESCE(?, until), until_cond = COALESCE(?, until_cond) WHERE id = ?",
                        (until, until_cond, fid))
        if wrong_fact:
            lifebase.supersede(db, wrong_fact["id"], fid, when=since, force=True)
            if kind == "wrong":  # 从来就不对：作废；变了的只是到期，留作历史
                db.conn.execute("UPDATE facts SET status = 'invalid' WHERE id = ?", (wrong_fact["id"],))
        if kind == "changed":
            layer = "changed"
        cur = db.conn.execute("INSERT INTO corrections (at, user, about, entry, quote, wrong, right, dimension, wrong_fact_id, fact_id, layer, "
                              "recurrence_of, kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                              (_now(), user, about, entry, quote, wrong, right, dim, (wrong_fact or {}).get("id"), fid, layer, again, kind))
    _to_eval(db, cur.lastrowid)
    return {"id": cur.lastrowid, "layer": layer, "fact_id": fid, "recurrence_of": again}


def _to_eval(db, ticket_id: int):
    """这次的错变成一道检验题（ethan/memeval.py，第二步）。"""
    try:
        from . import memeval
        memeval.add_for_ticket(db, ticket_id)
    except Exception as e:
        print(f"[memusers] 检验题没出成：{type(e).__name__} {e}", flush=True)


def note(db, reply_text: str, quote: str = "", wiki=None) -> list:
    """小伴 回复里的 [纠错 …] 标记开成纠错单。"""
    out = []
    for who, dim, wrong, right in MARK.findall(reply_text or ""):
        try:
            out.append(open_ticket(db, right, wrong, about="her" if who == "她" else "ethan_self", user="ethan", entry="chat",
                                   quote=quote[:200], dimension=dim, wiki=wiki))
        except Exception as e:
            print(f"[memusers] 纠错单没开成：{type(e).__name__} {e}", flush=True)
    return out


def strip(text: str) -> str:
    return MARK.sub("", text)


# ---------- 每个使用者用得怎么样 ----------
def stats(db, days: int = 7, now: datetime = None) -> list:
    """按登记表逐个算；新登记的使用者自动有一行。"""
    ensure(db)
    from . import memsearch
    memsearch.ensure(db)
    since = ((now or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for uid, u in USERS.items():
        rows = db.conn.execute("SELECT round, keys FROM retrieval_log WHERE user = ? AND at >= ?", (uid, since)).fetchall()
        first = [r for r in rows if r["round"] == 1]
        used = [r for r in first if json.loads(r["keys"] or "[]")]
        fixes = db.conn.execute("SELECT layer, recurrence_of FROM corrections WHERE user = ? AND at >= ? AND kind = 'wrong' "
                                "AND undone_at IS NULL", (uid, since)).fetchall()
        layers = {}
        for f in fixes:
            layers[LAYERS[f["layer"]]] = layers.get(LAYERS[f["layer"]], 0) + 1
        answers = len(first) or len(rows)  # 只读画像的使用者（准备工作、计划器）没有“回答”，按读了几次算
        out.append({"user": uid, **{k: u[k] for k in ("name", "level", "uses", "where", "since")}, "internal": bool(u.get("internal")),
                    "questions": list(u.get("questions", [])),
                    "queries": len(rows), "with_memory": len(used), "misses": answers - len(used),
                    "corrections": len(fixes), "layers": layers, "recurrences": sum(1 for f in fixes if f["recurrence_of"]),
                    "accuracy": round(1 - min(len(fixes), answers) / answers, 3) if answers else None,
                    "connected": bool(db.conn.execute("SELECT 1 FROM retrieval_log WHERE user = ? LIMIT 1", (uid,)).fetchone())})
    return out


def locked_tracking(db) -> list:
    """她锁定的事实之后用过几次、有没有复发。"""
    ensure(db)
    out = []
    for c in db.conn.execute("SELECT * FROM corrections WHERE fact_id IS NOT NULL ORDER BY id DESC LIMIT 50").fetchall():
        key = f'"fact:{c["fact_id"]}"'
        used = db.conn.execute("SELECT COUNT(*) FROM retrieval_log WHERE at >= ? AND keys LIKE ? AND user != 'system'",
                               (c["at"], f"%{key}%")).fetchone()[0]
        again = db.conn.execute("SELECT COUNT(*) FROM corrections WHERE recurrence_of = ?", (c["id"],)).fetchone()[0]
        out.append({"id": c["id"], "at": c["at"], "right": c["right"], "wrong": c["wrong"], "layer": LAYERS[c["layer"]],
                    "used_after": used, "recurred": again})
    return out
