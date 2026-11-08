"""推荐见谁（“小伴帮我推荐跟朋友见面的时候，跟我我当前所在地或者后面的计划来推荐一下见谁？比如去了珠海找赖总，出了BP跟谁聊？
这种怎么落实呢？不能考系统里的记录，得有程序来保证吧？”）。

全是写死的规则，不靠模型翻记录（模型只在聊天里把推荐说成一句话）：
1. 行程：接下来 14 天的待办里出现人物卡上有人住的城市（“去珠海见客户”），或她说了自己现在在外地（whereabouts），就从住在那个城市的人里挑。
2. 事件：最近 3 天做完的待办 / 里程碑碰上规则表（meet_rules：标题里有“BP” → 投资人、创业者），就从带这些身份的人里挑。
3. 平时：在常驻城市，每周从住在同城的人里挑几个（给每周“和朋友见面”用）。
挑人和排序：重要程度 × 3 + 多久没联系（到了提醒间隔算满）+ 说过“改天约饭”（想见）+ 2 + 有没了结的事 + 1；她选了“永不提醒”的人只在说过想见时才推荐。
城市（city）和身份标签（roles）在人物卡上：她在卡上改、和 小伴 语音聊时问清楚、整理聊天时看得出就写。都空着时不推荐。
"""

import json
import re
from datetime import date, timedelta

TRIP_DAYS = 14
EVENT_DAYS = 3
TOP = 3
SCHEMA = "CREATE TABLE IF NOT EXISTS meet_rules (id INTEGER PRIMARY KEY, match TEXT NOT NULL UNIQUE, roles TEXT NOT NULL)"
DEFAULT_RULES = (("BP", ["投资人", "创业者"]), ("融资", ["投资人"]), ("路演", ["投资人", "创业者"]))

# 随口约的（“下次一起吃饭”“改天约”）：不用她现在做什么，不进待履约，记成“想见”
CASUAL = re.compile(r"约饭|吃饭|吃个饭|聚一聚|聚聚|见面|见一面|喝咖啡|喝一杯|喝酒|喝一顿|约起来|约一下|再约|下次|下回|改天|有空")
CONCRETE = re.compile(r"发给|发他|发她|帮他|帮她|帮忙|寄|转给|介绍|推荐|回复|报价|简历|资料|文件|付|还钱|借")


def is_casual(loop: dict) -> bool:
    text = f"{loop.get('text') or ''} {loop.get('quote') or ''}"
    return bool(CASUAL.search(text)) and not CONCRETE.search(loop.get("text") or "")


def ensure(db):
    db.conn.execute(SCHEMA)
    if not db.conn.execute("SELECT 1 FROM meet_rules LIMIT 1").fetchone():
        db.conn.executemany("INSERT OR IGNORE INTO meet_rules (match, roles) VALUES (?, ?)",
                            [(m, json.dumps(r, ensure_ascii=False)) for m, r in DEFAULT_RULES])
        db.conn.commit()


def _j(v, default):
    try:
        return json.loads(v) if v else default
    except ValueError:
        return default


def _same_city(a: str, b: str) -> bool:
    a, b = (a or "").strip().lower(), (b or "").strip().lower()
    return bool(a and b) and (a in b or b in a)


def pick(db, today: date, city: str = None, roles=(), limit: int = TOP) -> list:
    """按城市或身份挑人，排好序。[{id, name, city, roles, why}]"""
    from . import relations
    relations.ensure(db)
    out = []
    for r in db.conn.execute("SELECT * FROM people"):
        p = dict(r)
        if city and not _same_city(city, p.get("city")):
            continue
        mine = _j(p.get("roles"), [])
        if roles and not set(roles) & set(mine):
            continue
        want = _j(p.get("want_meet"), [])
        if p.get("contact_every_days") == relations.NEVER and not want:
            continue
        every, gap = relations.interval(p), None
        if p.get("last_contact"):
            try:
                gap = (today - date.fromisoformat(str(p["last_contact"])[:10])).days
            except ValueError:
                pass
        score = 3 * int(p.get("importance") or 0) + (min(gap / every, 1.0) if every and gap is not None else 0) \
            + (2 if want else 0) + (1 if _j(p.get("open_loops"), []) else 0)
        why = [x for x in (f"说过“{want[-1]['text']}”" if want else "", f"{gap} 天没联系" if gap and every and gap >= every else "") if x]
        out.append({"id": p["id"], "name": p["name"], "city": p.get("city") or "", "roles": mine, "why": "，".join(why), "score": score})
    out.sort(key=lambda x: -x["score"])
    return out[:limit]


def _cities(db) -> list:
    return sorted({r[0].strip() for r in db.conn.execute("SELECT DISTINCT city FROM people WHERE COALESCE(city, '') != ''")}, key=len, reverse=True)


def trips(db, today: date) -> list:
    """接下来要去 / 现在在的外地城市：[{city, why}]。只认人物卡上有人住的城市。"""
    cities = _cities(db)
    if not cities:
        return []
    out, seen = [], set()
    from . import whereabouts
    try:
        cur = whereabouts.current(db)
        if cur["source"] == "told":
            hit = next((c for c in cities if _same_city(c, cur["city"])), None)
            if hit:
                out.append({"city": hit, "why": f"你说现在在{cur['city']}"})
                seen.add(hit)
    except Exception:
        pass
    end = (today + timedelta(days=TRIP_DAYS)).isoformat()
    from . import taskrules
    for t in taskrules.rows(db, "export", today):  # 没做完的待办都从 taskrules 取（不自己查表）
        due = str(t.get("due") or "")[:10]
        if due and not (today.isoformat() <= due <= end):
            continue
        hit = next((c for c in cities if c in (t.get("title") or "")), None)
        if hit and hit not in seen:
            seen.add(hit)
            out.append({"city": hit, "why": f"待办“{t['title']}”" + (f"（{due}）" if due else "")})
    return out


def events(db, today: date) -> list:
    """最近做完、碰上规则表的事：[{match, roles, why}]。"""
    ensure(db)
    rules = [(r["match"], _j(r["roles"], [])) for r in db.conn.execute("SELECT match, roles FROM meet_rules")]
    since = (today - timedelta(days=EVENT_DAYS)).isoformat()
    out, seen = [], set()
    for t in db.conn.execute("SELECT title FROM tasks WHERE done = 1 AND done_at >= ?", (since,)):
        for m, roles in rules:
            if m.lower() in (t["title"] or "").lower() and m not in seen:
                seen.add(m)
                out.append({"match": m, "roles": roles, "why": f"刚做完“{t['title']}”"})
    return out


def suggest(db, today: date) -> list:
    """关系页和 小伴 聊天用：[{why, people: [...]}]，没人可推荐的不列。"""
    out = []
    for t in trips(db, today):
        ppl = pick(db, today, city=t["city"])
        if ppl:
            out.append({"why": f"{t['why']}，在{t['city']}可以见", "people": ppl})
    for e in events(db, today):
        ppl = pick(db, today, roles=e["roles"])
        if ppl:
            out.append({"why": f"{e['why']}，可以找{'、'.join(e['roles'])}聊聊", "people": ppl})
    if not out:
        from . import whereabouts
        try:
            home = whereabouts.current(db)["city"]
        except Exception:
            home = ""
        ppl = pick(db, today, city=home) if home else []
        if ppl:
            out.append({"why": f"这周在{home}，可以约", "people": ppl})
    return out


def chat_lines(db, today: date) -> str:
    """给 小伴 聊天的一两行（顺口提，不播报）。"""
    return "\n".join(f"- {s['why']}：" + "、".join(p["name"] + (f"（{p['why']}）" if p["why"] else "") for p in s["people"])
                     for s in suggest(db, today)[:2])
