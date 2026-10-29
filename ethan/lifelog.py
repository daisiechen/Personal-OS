"""生活记录：小伴 在聊天里顺手记下的几类事，给一直是灰的健康线和拿不到的成就当数据（2026-10-28 全面检查后补）。

标记（小伴 写在回复末尾，不会发给她）：
  [人物 名字 见面|联系|新认识 一句话]  社交领地：和朋友见面、认识新朋友、联系重要的人；顺手更新人物卡和最近联系
  [话题 一句话]                       学习领地：认知拓展，每天 1 个话题
  [研究 分钟 一句话]                   学术领地：研究时间，每周约 2 小时
  [体脂 24.5]                         身体：体脂（成就“体脂降到”）
  [作品 书|歌 1|2|3 一句话]             成就“出一本书 / 写一首歌”：1 初稿或写完第一首，2 签约或发布，3 正式出版或播放过万
"""

import re
from datetime import date, datetime, timedelta, timezone

SCHEMA = """CREATE TABLE IF NOT EXISTS life_log (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, day TEXT NOT NULL,
    who TEXT, text TEXT, n REAL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS life_log_kind ON life_log (kind, day);"""
PERSON = re.compile(r"\[人物\s+(\S+?)\s+(见面|联系|新认识)\s*([^\]]*)\]")
TOPIC = re.compile(r"\[话题\s+([^\]]+?)\s*\]")
RESEARCH = re.compile(r"\[研究\s+(\d+)\s*(?:分钟)?\s*([^\]]*)\]")
BODYFAT = re.compile(r"\[体脂\s*([\d.]+)\s*%?\s*\]")
WORK = re.compile(r"\[作品\s+(书|歌)\s+([123])\s*([^\]]*)\]")
KIND = {"见面": "meet", "联系": "contact", "新认识": "new_friend"}
PROMPT = ("她聊到生活里的这几类事时，在回复末尾加标记（不会发给她）：和谁见面、联系了谁、认识了新朋友 [人物 名字 见面/联系/新认识 一句话]；"
          "你们聊了一个新的知识话题（认知拓展）[话题 一句话]；她做了学术研究 [研究 分钟 做了什么]；她报体脂 [体脂 24.5]；"
          "她的书或歌有进展 [作品 书/歌 1/2/3 一句话]（1 初稿或写完第一首，2 签约或发布，3 正式出版或播放过万）。")


def ensure(db):
    db.conn.executescript(SCHEMA)


def add(db, kind: str, day: date, who: str = None, text: str = "", n: float = None) -> bool:
    """同一天同一件事只记一次（小伴 下一轮又提到同一次见面、同一个话题不重复算，2026-10-29 自查）。"""
    ensure(db)
    if who is not None:
        dup = db.conn.execute("SELECT 1 FROM life_log WHERE kind = ? AND day = ? AND who = ?", (kind, day.isoformat(), who)).fetchone()
    else:
        dup = db.conn.execute("SELECT 1 FROM life_log WHERE kind = ? AND day = ? AND text = ?", (kind, day.isoformat(), (text or "")[:300])).fetchone()
    if dup:
        return False
    db.conn.execute("INSERT INTO life_log (kind, day, who, text, n, at) VALUES (?, ?, ?, ?, ?, ?)",
                    (kind, day.isoformat(), who, (text or "")[:300], n, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return True


def note(db, text: str, day: date) -> int:
    """从 小伴 的回复里读标记记下来，返回记了几条。"""
    ensure(db)
    got = 0
    for name, what, line in PERSON.findall(text):
        name = name.strip()[:30]
        fields = {"last_contact": day.isoformat()} if what in ("见面", "联系") else {}
        if what == "新认识" and not db.conn.execute("SELECT 1 FROM people WHERE name = ?", (name,)).fetchone():
            fields["circle"], fields["how_met"] = "新朋友", line.strip()[:100] or None
            fields["last_contact"] = day.isoformat()
        db.upsert_person(name, **fields)
        add(db, KIND[what], day, name, line.strip())
        got += 1
    for line in TOPIC.findall(text):
        add(db, "topic", day, text=line)
        got += 1
    for minutes, line in RESEARCH.findall(text):
        add(db, "research", day, text=line.strip(), n=float(minutes))
        got += 1
    for v in BODYFAT.findall(text):
        fat = float(v)
        if 5 <= fat <= 60:
            db.conn.execute("INSERT INTO body_measures (day, bodyfat, at) VALUES (?, ?, ?)",
                            (day.isoformat(), fat, datetime.now(timezone.utc).isoformat(timespec="seconds")))
            got += 1
    for kind, stage, line in WORK.findall(text):
        add(db, "book" if kind == "书" else "song", day, text=line.strip(), n=float(stage))
        got += 1
    db.conn.commit()
    return got


def strip(text: str) -> str:
    for r in (PERSON, TOPIC, RESEARCH, BODYFAT, WORK):
        text = r.sub("", text)
    return text


def count(db, kind: str, start: date, end: date, distinct_days: bool = False) -> int:
    ensure(db)
    col = "COUNT(DISTINCT day)" if distinct_days else "COUNT(*)"
    return db.conn.execute(f"SELECT {col} FROM life_log WHERE kind = ? AND day >= ? AND day <= ?",
                           (kind, start.isoformat(), end.isoformat())).fetchone()[0]


def total(db, kind: str, start: date, end: date) -> float:
    ensure(db)
    return db.conn.execute("SELECT COALESCE(SUM(n), 0) FROM life_log WHERE kind = ? AND day >= ? AND day <= ?",
                           (kind, start.isoformat(), end.isoformat())).fetchone()[0]


def has(db, kind: str) -> bool:
    ensure(db)
    return db.conn.execute("SELECT 1 FROM life_log WHERE kind = ? LIMIT 1", (kind,)).fetchone() is not None


def best_stage(db, kind: str):
    ensure(db)
    v = db.conn.execute("SELECT MAX(n) FROM life_log WHERE kind = ?", (kind,)).fetchone()[0]
    return int(v) if v else None


def new_friends(db) -> int:
    ensure(db)
    logged = {r[0] for r in db.conn.execute("SELECT DISTINCT who FROM life_log WHERE kind = 'new_friend'")}
    marked = {r[0] for r in db.conn.execute("SELECT name FROM people WHERE circle = '新朋友'")}
    return len(logged | marked)
