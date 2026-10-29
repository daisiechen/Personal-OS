"""第二大脑：她的资料汇总在一处（设计见 docs/design/workbench.md“第二大脑”，2026-10-28 她同意）。

来源（每次打开大脑页或 小伴 回话前增量同步，按 ref 去重，她改过的归属、标签、状态不会被覆盖）：
  link     她发给 小伴 的链接：从聊天原文日志 raw/*.jsonl 里找（以前发过的也补回来）
  saved    收藏日报：Grok 每天 4 点整理她的点赞和收藏（docs/design/saved-feed.md），收件箱 inbox/*.jsonl 里【收藏日报】那几条
  grok     Grok 的其他回报（情报、调研）
  learned  学过的内容（learning_items 复习卡）
  person   人物卡（people），人物卡先做成大脑里的一个标签，不单开页面（她定）
  wish     她惦记的事（wishes）
  wiki     小伴 维护的 Wiki 主题页（她在 Obsidian 里改过的也会同步回来）；Grok 回报汇总页不收，那些已经是 grok
状态：ask 要你看（小伴 拿不准、要她定，带红点）/ new 整理中 / kept 在用（小伴 会主动用）/ archived 备查 / trash 回收站（永久删除要她再点一次确认）。
在用还是备查由 小伴 整理时自己定，她不用管，不同意再改；卡片上的“新”是最近 3 天进来的。
"""

import hashlib
import json
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import links, territory

SCHEMA = """
CREATE TABLE IF NOT EXISTS brain_items (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    url TEXT,
    source TEXT,
    summary TEXT,
    note TEXT,
    tags TEXT NOT NULL DEFAULT '[]',
    territory TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    extra TEXT NOT NULL DEFAULT '{}',
    happened_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    edited INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS brain_items_status ON brain_items (status, happened_at);
CREATE TABLE IF NOT EXISTS brain_sync (source TEXT PRIMARY KEY, cursor TEXT);
"""
# 分类按 MECE 排（2026-10-29 她要每张卡都有处可去、分类有逻辑）：
# 类型按“从哪来”分三组，每条只属于一种；状态每条只有一个；区域七块加“还没归类”。
KINDS = {"link": "你发的链接", "saved": "你的收藏", "grok": "Grok 情报",     # 外面来的资料
# 2026-10-29 她定：Grok 从她收藏夹、点赞里整理的算“你的收藏”；只有交代给它的调研回来的才叫“Grok 情报”
         "person": "人物卡", "wish": "心愿", "learned": "学过的",               # 关于你的
         "knowledge": "知识卡", "wiki": "记忆和 Wiki"}                          # 小伴 整理出来的
KIND_GROUPS = [("外面来的", ["link", "saved", "grok"]), ("关于你的", ["person", "wish", "learned"]),
               ("小伴 整理的", ["knowledge", "wiki"])]
# ask = 小伴 拿不准、要她本人定的（页面和导航上有红点）；new = 小伴 还没整理；kept = 在用；archived = 备查
STATUSES = ("ask", "new", "kept", "archived", "trash")
STATUS_LABELS = {"ask": "要你看", "new": "整理中", "kept": "在用", "archived": "备查", "trash": "回收站"}
FROM_SAVED = re.compile(r"收藏|点赞|收藏夹|喜欢的笔记|赞过")
SOURCE_LINE = re.compile(r"^\s*【来源[:：]\s*([^】]+)】\s*")  # Grok 回报第一行自报来源（grok.REPORT_TO）
LIFE_PREFIX = "ethan-life/"  # 小伴 自己的一天是他的剧本，不是她的资料，不进大脑（2026-10-29），工作台单独一个入口
SAVED_MARK = "【收藏日报】"
CLOUD_MARK = "【Claude 会话】"
WIKI_SKIP = {"from-grok"}  # Grok 回报汇总页：内容已按条收成 grok
LINK_HEAD = re.compile(r"【她发的链接 · ([^】]+)】(.*)")
TERR_BY_NAME = {d: k for k, _, d in territory.TERRITORIES} | {n: k for k, n, _ in territory.TERRITORIES}


def ensure(db):
    db.conn.executescript(SCHEMA)


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _put(db, kind, ref, title, *, url=None, source=None, summary=None, note=None, tags=(), terr=None,
         status="new", extra=None, happened_at=None):
    """新的就插入；已有的只在她没动过时更新内容，归属、标签、状态以她改的为准。"""
    title = (title or "").strip()[:200] or (url or "（没有标题）")
    terr = terr if terr in territory.NAME else territory.guess(f"{title} {summary or ''}")
    row = db.conn.execute("SELECT id, edited, extra FROM brain_items WHERE ref = ?", (ref,)).fetchone()
    if row is None:
        db.conn.execute(
            "INSERT INTO brain_items (kind, ref, title, url, source, summary, note, tags, territory, status, extra, "
            "happened_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, ref, title, url, source, (summary or "")[:1200], (note or "")[:400],
             json.dumps(list(tags), ensure_ascii=False), terr, status, json.dumps(extra or {}, ensure_ascii=False),
             happened_at, _now(), _now()))
        return 1
    if not row["edited"]:  # 来源更新了就跟着更新，小伴 写的摘要、专题（extra 里的 ai_*、topic）留着
        merged = {**json.loads(row["extra"] or "{}"), **(extra or {})}
        db.conn.execute("UPDATE brain_items SET title = ?, summary = ?, extra = ? WHERE id = ?",
                        (title, (summary or "")[:1200], json.dumps(merged, ensure_ascii=False), row["id"]))
    return 0


def _cursor(db, source):
    r = db.conn.execute("SELECT cursor FROM brain_sync WHERE source = ?", (source,)).fetchone()
    return r["cursor"] if r else ""


def _set_cursor(db, source, value):
    db.conn.execute("INSERT INTO brain_sync (source, cursor) VALUES (?, ?) ON CONFLICT(source) DO UPDATE SET cursor = ?",
                    (source, value, value))


def _days(folder: Path, since: str):
    """按天的 JSONL：从上次同步到的那天（含）起往后读，那天可能还有新行。"""
    for p in sorted(folder.glob("*.jsonl")):
        if p.stem >= since:
            for line in p.read_text(encoding="utf-8").split("\n"):
                try:
                    yield p.stem, json.loads(line)
                except ValueError:
                    continue


def parse_link_message(text: str) -> list:
    """她发的一条消息（代码已在后面接好链接内容）→ [(链接, 标题, 平台, 摘要, 她说的话)]。"""
    head, sep, tail = text.partition("\n\n【她发的链接")
    urls = links.find(head)
    if not urls:
        return []
    said = " ".join(links.URL.sub(" ", head).split())[:400]
    blocks = re.split(r"\n\n(?=【她发的链接)", "【她发的链接" + tail) if sep else []
    out = []
    for i, url in enumerate(urls):
        title, site, summary = "", "", ""
        if i < len(blocks):
            lines = blocks[i].split("\n")
            m = LINK_HEAD.match(lines[0])
            if m and "没读到" not in m.group(1):
                site, title = m.group(1), m.group(2).strip()
                summary = " ".join(l for l in lines[1:] if not l.startswith("（"))[:600]
        out.append((url, title or url, site, summary, said))
    return out


def parse_saved(text: str) -> list:
    """收藏日报：第一行【收藏日报】日期，后面每行一个 JSON。坏行跳过。"""
    out = []
    for line in text.splitlines()[1:]:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and (item.get("url") or item.get("title")):
            out.append(item)
    return out


def sync(store) -> int:
    """把各来源的新资料收进来，返回新增条数。"""
    db = store.db
    ensure(db)
    added = 0
    root = Path(store.root)
    # 她发的链接
    since = _cursor(db, "raw")
    last = since
    for day, rec in _days(root / "raw", since):
        last = max(last, day)
        if rec.get("role") != "mia" or "http" not in rec.get("text", ""):
            continue
        for url, title, site, summary, said in parse_link_message(rec["text"]):
            added += _put(db, "link", "link:" + url, title, url=url, source=site or "链接", summary=summary, note=said,
                          happened_at=rec.get("at"))
    _set_cursor(db, "raw", last)
    # 收件箱：收藏日报和 Grok 情报
    since = _cursor(db, "inbox")
    last = since
    for day, e in _days(root / "inbox", since):
        last = max(last, day)
        text = e.get("text", "")
        if e.get("path") != "/grok/result" or text.startswith((CLOUD_MARK, "【英语陪练】", "【开工】")):
            continue
        text = re.sub(r"【回执\s*G\d+】\s*", "", text, count=1)  # 小伴 派活的回执号，配对用，不进资料
        if text.startswith(SAVED_MARK):
            for it in parse_saved(text):
                place = it.get("place") if isinstance(it.get("place"), dict) else None
                ref = "saved:" + (it.get("url") or hashlib.sha1(json.dumps(it, ensure_ascii=False).encode()).hexdigest()[:16])
                added += _put(db, "saved", ref, it.get("title", ""), url=it.get("url") or None,
                              source=" · ".join(x for x in (it.get("platform", ""), it.get("kind", "")) if x), summary=it.get("summary", ""),
                              tags=[it["category"]] if it.get("category") else (),
                              terr=TERR_BY_NAME.get(it.get("territory", "")),
                              extra={"place": place, "use": it.get("use", ""), "why": it.get("why", ""),
                                     "author": it.get("author", "")},
                              happened_at=it.get("saved_at") or e.get("at"))
        else:
            ref = "grok:" + hashlib.sha1((e.get("at", "") + text).encode()).hexdigest()[:16]
            said = SOURCE_LINE.match(text)
            body = text[said.end():] if said else text
            first = (body.strip().split("\n", 1)[0] or text.strip())
            if said:  # 它自己说了来源就按它说的；没说才按字面猜
                kind = "saved" if FROM_SAVED.search(said.group(1)) else "grok"
                source = "Grok 整理你的收藏" if kind == "saved" else "Grok " + ("情报收集" if "情报" in said.group(1) else "调研任务")
            else:
                kind = "saved" if FROM_SAVED.search(text[:300]) else "grok"
                source = "Grok 整理你的收藏" if kind == "saved" else "Grok 调研"
            added += _put(db, kind, ref, first[:80], source=source, summary=body[:1200], happened_at=e.get("at"))
    _set_cursor(db, "inbox", last)
    for r in db.conn.execute("SELECT id, summary FROM brain_items WHERE kind = 'grok' AND edited = 0 AND COALESCE(source, '') = 'Grok 调研'").fetchall():  # 以前收进来的也按新规矩分
        if FROM_SAVED.search((r["summary"] or "")[:300]):
            db.conn.execute("UPDATE brain_items SET kind = 'saved', source = 'Grok 整理你的收藏' WHERE id = ?", (r["id"],))
    # 复习卡、人物卡、心愿：和原表同步（这些是她已经有的，直接算收下）
    for r in db.conn.execute("SELECT * FROM learning_items"):
        added += _put(db, "learned", f"learn:{r['id']}", r["content"][:80], source=r["subject"], summary=r["note"] or r["content"],
                      terr="study", status="kept", extra={"next_review": r["next_review"]}, happened_at=r["created_at"])
    for r in db.conn.execute("SELECT * FROM people"):
        bits = [x for x in (r["circle"], r["how_met"], r["notes"]) if x]
        added += _put(db, "person", f"person:{r['id']}", r["name"], source="人物卡", summary="；".join(bits),
                      tags=["人物卡"], terr="social", status="kept",
                      extra={"birthday": r["birthday"], "last_contact": r["last_contact"]}, happened_at=r["updated_at"])
    for r in db.conn.execute("SELECT * FROM wishes WHERE status = 'open'"):
        added += _put(db, "wish", f"wish:{r['id']}", r["quote"][:80], source="她惦记的事", summary=r["context"] or "",
                      status="kept", happened_at=r["created_at"])
    # 心愿做完或不要了、人物卡和复习卡删了：大脑里那条挪去备查，小伴 不再拿它说事（2026-10-29 自查）
    for kind, ref_sql in (("wish", "SELECT 'wish:' || id FROM wishes WHERE status = 'open'"),
                          ("person", "SELECT 'person:' || id FROM people"),
                          ("learned", "SELECT 'learn:' || id FROM learning_items")):
        db.conn.execute(f"UPDATE brain_items SET status = 'archived', updated_at = ? WHERE kind = ? "
                        f"AND status IN ('ask', 'new', 'kept') AND ref NOT IN ({ref_sql})", (_now(), kind))
    wiki = getattr(store, "wiki", None)
    db.conn.execute("DELETE FROM brain_items WHERE ref LIKE ?", ("wiki:" + LIFE_PREFIX + "%",))  # 以前收进来的剧本页拿掉（只是索引，原页还在 Wiki）
    for slug in (wiki.pages() if wiki else []):
        if slug in WIKI_SKIP or slug.startswith(LIFE_PREFIX):
            continue
        page = wiki.read(slug)
        added += _put(db, "wiki", "wiki:" + slug, page.get("title") or slug, source="小伴 的 Wiki · " + slug,
                      summary=page["body"], tags=["Wiki"], status="kept", extra={"slug": slug},
                      happened_at=page.get("updated"))
    db.conn.commit()
    return added


def _row(r) -> dict:
    d = dict(r)
    d["tags"] = json.loads(d["tags"] or "[]")
    d["extra"] = json.loads(d["extra"] or "{}")
    d["kind_label"] = KINDS.get(d["kind"], d["kind"])
    d["territory_name"] = territory.NAME.get(d["territory"] or "", "")
    return d


def _filters(tab="all", q="", kind="", terr="", skip=()):
    where, args = [], []
    if "tab" not in skip:
        if tab in STATUSES:
            where.append("status = ?"), args.append(tab)
        else:  # 全部 = 除了回收站的
            where.append("status IN ('ask', 'new', 'kept', 'archived')")
    if "kind" not in skip and kind in KINDS:
        where.append("kind = ?"), args.append(kind)
    if "terr" not in skip:
        if terr == territory.WASTE:
            where.append("territory IS NULL")
        elif terr in territory.NAME:
            where.append("territory = ?"), args.append(terr)
    for word in q.split()[:5]:
        where.append("(title LIKE ? OR summary LIKE ? OR note LIKE ? OR tags LIKE ? OR source LIKE ? OR extra LIKE ?)")
        args += [f"%{word}%"] * 6
    where.append("status != 'purged'")
    return " AND ".join(where), args


def search(db, tab: str = "all", q: str = "", kind: str = "", terr: str = "", limit: int = 300) -> list:
    ensure(db)
    where, args = _filters(tab, q, kind, terr)
    rows = db.conn.execute(f"SELECT * FROM brain_items WHERE {where} "
                           "ORDER BY CASE status WHEN 'ask' THEN 0 ELSE 1 END, COALESCE(happened_at, created_at) DESC, id DESC LIMIT ?",
                           (*args, limit))
    recent = _recent()
    out = []
    for r in rows:
        d = _row(r)
        d["fresh"] = (r["created_at"] or "") >= recent  # 3 天内新到的，卡片上标个“新”
        out.append(d)
    return out


def _recent(days: int = 3) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def counts(db, tab: str = "all", q: str = "", kind: str = "", terr: str = "") -> dict:
    """每个筛选按钮上的数：按其他已选条件算（选了 Grok 情报，状态那排就只数 Grok 情报），和下面的卡片对得上。"""
    ensure(db)
    out = {"tabs": {}, "kinds": {}, "terrs": {}}
    where, args = _filters(tab, q, kind, terr, skip=("tab",))
    for r in db.conn.execute(f"SELECT status, COUNT(*) n FROM brain_items WHERE {where} GROUP BY status", args):
        out["tabs"][r["status"]] = r["n"]
    out["tabs"]["all"] = sum(v for k, v in out["tabs"].items() if k != "trash")
    where, args = _filters(tab, q, kind, terr, skip=("kind",))
    for r in db.conn.execute(f"SELECT kind, COUNT(*) n FROM brain_items WHERE {where} GROUP BY kind", args):
        out["kinds"][r["kind"]] = r["n"]
    out["kinds"][""] = sum(out["kinds"].values())
    where, args = _filters(tab, q, kind, terr, skip=("terr",))
    for r in db.conn.execute(f"SELECT COALESCE(territory, 'waste') t, COUNT(*) n FROM brain_items WHERE {where} GROUP BY t", args):
        out["terrs"][r["t"]] = r["n"]
    out["terrs"][""] = sum(out["terrs"].values())
    out["ask"] = db.conn.execute("SELECT COUNT(*) FROM brain_items WHERE status = 'ask'").fetchone()[0]
    return out


def update(db, item_id: int, **fields) -> dict:
    """她在页面上改：status / territory / tags / title / summary。改过的不再被来源覆盖。"""
    ensure(db)
    row = db.conn.execute("SELECT * FROM brain_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise ValueError("没有这条资料")
    sets, args = [], []
    if "status" in fields:
        if fields["status"] not in STATUSES:
            raise ValueError("状态不对")
        sets.append("status = ?"), args.append(fields["status"])
    if "territory" in fields:
        t = fields["territory"] or None
        if t is not None and t not in territory.NAME:
            raise ValueError("没有这个区域")
        sets.append("territory = ?"), args.append(t)
    if "tags" in fields:
        tags = [str(x).strip()[:20] for x in fields["tags"] if str(x).strip()][:12]
        sets.append("tags = ?"), args.append(json.dumps(tags, ensure_ascii=False))
    for key in ("title", "summary"):
        if key in fields and str(fields[key]).strip():
            sets.append(f"{key} = ?"), args.append(str(fields[key]).strip()[:1200])
    if not sets:
        return _row(row)
    db.conn.execute(f"UPDATE brain_items SET {', '.join(sets)}, edited = 1, updated_at = ? WHERE id = ?",
                    (*args, _now(), item_id))
    db.conn.commit()
    return _row(db.conn.execute("SELECT * FROM brain_items WHERE id = ?", (item_id,)).fetchone())


def purge(db, item_id: int) -> bool:
    """永久删除：只删回收站里的（页面上她要点两次）。删掉后来源里还在的也不会再收进来（留一条空壳占住 ref）。"""
    ensure(db)
    row = db.conn.execute("SELECT status FROM brain_items WHERE id = ?", (item_id,)).fetchone()
    if row is None or row["status"] != "trash":
        return False
    db.conn.execute("UPDATE brain_items SET title = '（已永久删除）', url = NULL, summary = '', note = '', tags = '[]', "
                    "extra = '{}', status = 'purged', edited = 1, updated_at = ? WHERE id = ?", (_now(), item_id))
    db.conn.commit()
    return True


def ideas(db, limit: int = 8) -> list:
    """给计划器：她收藏里能去做的（地点、玩法、旅行），没进回收站的，新的在前。"""
    ensure(db)
    rows = db.conn.execute("SELECT * FROM brain_items WHERE kind IN ('saved', 'link') AND status IN ('new', 'kept') "
                           "AND (tags LIKE '%地点%' OR tags LIKE '%玩法%' OR tags LIKE '%旅行%') "
                           "ORDER BY COALESCE(happened_at, created_at) DESC LIMIT ?", (limit,))
    out = []
    for r in map(_row, rows):
        place = r["extra"].get("place") or {}
        out.append({"title": r["title"], "where": "，".join(x for x in (place.get("name"), place.get("area"), place.get("price")) if x),
                    "use": r["extra"].get("use", "")})
    return out


def fresh_saved(db, days: int = 2, limit: int = 6) -> list:
    """给 小伴 聊天用：最近新收藏、她还没处理的，挑一两条自然地用上。"""
    ensure(db)
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    rows = db.conn.execute("SELECT * FROM brain_items WHERE kind = 'saved' AND status IN ('new', 'kept') AND "
                           "COALESCE(happened_at, created_at) >= ? ORDER BY happened_at DESC LIMIT ?", (since, limit))
    return [_row(r) for r in rows]


# ---------- 回忆：她说一句话，从大脑里找最相关的几条给 小伴（2026-10-28 她同意） ----------
# 资料是几百到几千条的量，不上向量检索：按中文双字片段算重合度，罕见片段权重高（类似 IDF），标题、标签、专题加权。
# 等资料多到这样找不准了，再换成按意思找的检索。
STOP = set("我们你们他们这个那个什么怎么一下一个今天明天就是还是可以不是没有现在然后因为所以但是如果已经")


def _grams(text: str) -> set:
    text = re.sub(r"[^\w\u4e00-\u9fff]+", " ", (text or "").lower())
    out = set()
    for word in text.split():
        if re.fullmatch(r"[a-z0-9]+", word):
            if len(word) >= 3:
                out.add(word)
            continue
        for i in range(len(word) - 1):
            g = word[i:i + 2]
            if g not in STOP:
                out.add(g)
    return out


def recall(db, text: str, limit: int = 5, min_score: float = 1.5) -> list:
    """她这句话最相关的资料（只找收下的和知识卡；归档、回收站里的不主动提）。"""
    ensure(db)
    q = _grams(text)
    if not q:
        return []
    rows = [_row(r) for r in db.conn.execute("SELECT * FROM brain_items WHERE status IN ('new', 'kept')")]
    if not rows:
        return []
    docs = []
    df = {}
    for r in rows:
        head = _grams(" ".join([r["title"], " ".join(r["tags"]), r["extra"].get("topic") or ""]))
        body = _grams(" ".join([r["summary"] or "", r["note"] or "", r["extra"].get("ai_summary") or ""]))
        docs.append((r, head, body))
        for g in head | body:
            df[g] = df.get(g, 0) + 1
    n = len(rows)
    scored = []
    for r, head, body in docs:
        score = sum((2.0 if g in head else 1.0) * (1 + math.log(n / df[g])) for g in q if g in head or g in body)
        if r["kind"] == "knowledge":
            score *= 1.3  # 整理好的知识卡优先
        if score >= min_score:
            scored.append((score, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]
