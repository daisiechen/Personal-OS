"""成就奖励：小伴的惊喜玩法。成就每拿到一个铜级解锁下一个铜档玩法，银、金同理。
下面是示例，换成你自己的点子；没解锁的在页面上不露名字。"""

from datetime import datetime, timezone

SCHEMA = """CREATE TABLE IF NOT EXISTS play_unlocks (key TEXT PRIMARY KEY, at TEXT NOT NULL, announced INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS play_base (tier TEXT PRIMARY KEY, n INTEGER NOT NULL);"""
TIER_ORDER = ("铜", "银", "金")

# how：给 小伴 的做法说明（解锁后进他的系统提示词）。按档位内的顺序依次解锁。
PLAYS = [  # 公开版示例：成就解锁的惊喜玩法（作者自己的那套不公开）。按档位顺序解锁，铜 4、银 3、金 3，换成你自己的点子
    {"key": "pet_name", "tier": "铜", "name": "只属于你的称呼",
     "reveal": "想换个只有我会这么叫你的叫法，你来挑？",
     "how": "提三四个称呼让她挑，定下来后一直用这个称呼叫她。"},
    {"key": "photo_of_day", "tier": "铜", "name": "今天我看到的",
     "reveal": "以后我会偶尔发你一张我这边此刻的样子。",
     "how": "挑一个剧本里的时刻，用 [照片 …] 发一张那一刻的画面，配一句话。"},
    {"key": "tiny_quiz", "tier": "铜", "name": "睡前三个问题",
     "reveal": "今晚开始，睡前我出三个小问题考你。",
     "how": "晚上她说要睡时，出三个轻松的小问题（关于她今天、关于你们、关于明天），答完说晚安。"},
    {"key": "pep_talk", "tier": "铜", "name": "出门前的一句",
     "reveal": "你有重要的事要出门时，我会提前给你打个气。",
     "how": "计划里有要出门的重要日程时，出发前发一句具体的打气，提到这件事本身。"},
    {"key": "voice_story", "tier": "银", "name": "睡前故事",
     "reveal": "解锁了：我给你讲个睡前故事吧。",
     "how": "她说睡不着时，用语音讲一个三五分钟的小故事，主角是她，结尾温柔。"},
    {"key": "rewind", "tier": "银", "name": "那天我其实在想",
     "reveal": "有件事我一直没告诉你，当时我心里想的是……",
     "how": "挑你们聊天里一个有意思的时刻，讲当时他心里其实在想什么，轻松一点。"},
    {"key": "map_day", "tier": "银", "name": "一张周末路线",
     "reveal": "我给你画了一条这个周末可以走的小路线。",
     "how": "按她收藏的地方和天气，排一条半天能走完的路线，写清顺序和每站做什么。"},
    {"key": "season_recap", "tier": "金", "name": "这个赛季的你",
     "reveal": "我把这个赛季的你整理成了一份小回顾。",
     "how": "用这个赛季的成就、副本进度和聊天里的高光时刻，写一份带标题的回顾，每段一个瞬间。"},
    {"key": "treasure_hunt", "tier": "金", "name": "城市寻宝",
     "reveal": "准备好了吗？今天给你安排了一场只属于你的寻宝。",
     "how": "按她的城市和收藏，设计 3–5 站的小寻宝，每站一个线索，最后一站是一个小奖励（要花钱的先问她）。"},
    {"key": "future_note", "tier": "金", "name": "一年后的来信",
     "reveal": "有一封一年后的你写来的信，要看吗？",
     "how": "以一年后的她的口吻写一封信，提到她这个赛季真做到的事，语气温柔，不编具体事件。"},
]


def ensure(db):
    db.conn.executescript(SCHEMA)
    if "shown" not in {r[1] for r in db.conn.execute("PRAGMA table_info(play_unlocks)")}:  # 揭晓后工作台弹过没有
        db.conn.execute("ALTER TABLE play_unlocks ADD COLUMN shown INTEGER NOT NULL DEFAULT 0")
        db.conn.commit()


def reveals(db) -> list:
    """小伴 已经在聊天里揭晓、工作台还没弹过的奖励。"""
    ensure(db)
    done = {r["key"] for r in db.conn.execute("SELECT key FROM play_unlocks WHERE announced = 1 AND shown = 0")}
    return [p for p in PLAYS if p["key"] in done]


def mark_reveal_shown(db, keys):
    ensure(db)
    db.conn.executemany("UPDATE play_unlocks SET shown = 1 WHERE key = ?", [(k,) for k in keys])
    db.conn.commit()


def _earned(items: list) -> dict:
    """每一档拿到了几个成就：铜 = 任何达成（含“第一次”类），银 = 银或金，金 = 金。"""
    n = {"铜": 0, "银": 0, "金": 0}
    for i in items:
        tier = i.get("tier") or ("铜" if i.get("done") else None)
        if not tier:
            continue
        for t in TIER_ORDER[:TIER_ORDER.index(tier) + 1]:
            n[t] += 1
    return n


def sync(db, items: list, now: datetime = None) -> list:
    """按成就算该解锁哪些玩法，新解锁的记下时间；返回所有玩法和解锁状态。"""
    ensure(db)
    now = now or datetime.now(timezone.utc)
    earned = _earned(items)
    base = {r["tier"]: r["n"] for r in db.conn.execute("SELECT * FROM play_base")}
    if not base:  # 第一次算：上线前就有的成就（比如早年点亮的人生 Goal）只送一个铜档，其余从现在起拿一个解一个
        base = {t: (max(0, earned[t] - 1) if t == "铜" else earned[t]) for t in TIER_ORDER}
        db.conn.executemany("INSERT INTO play_base (tier, n) VALUES (?, ?)", list(base.items()))
    earned = {t: max(0, earned[t] - base.get(t, 0)) for t in TIER_ORDER}
    seen = {t: 0 for t in TIER_ORDER}
    for p in PLAYS:
        seen[p["tier"]] += 1
        if seen[p["tier"]] <= earned[p["tier"]]:
            db.conn.execute("INSERT OR IGNORE INTO play_unlocks (key, at) VALUES (?, ?)", (p["key"], now.isoformat(timespec="seconds")))
    db.conn.commit()
    got = {r["key"]: dict(r) for r in db.conn.execute("SELECT * FROM play_unlocks")}
    return [{**p, "unlocked": p["key"] in got, "at": (got.get(p["key"]) or {}).get("at"),
             "announced": bool((got.get(p["key"]) or {}).get("announced"))} for p in PLAYS]


def page(plays: list) -> dict:
    """给页面看的：没解锁的不露名字（她要惊喜）。"""
    out = []
    for p in plays:
        if p["unlocked"] and p["announced"]:
            out.append({"tier": p["tier"], "unlocked": True, "name": p["name"], "note": p["reveal"], "at": p["at"]})
        elif p["unlocked"]:  # 解锁了但 小伴 还没在聊天里揭晓：页面上也先不露
            out.append({"tier": p["tier"], "unlocked": True, "waiting": True})
        else:
            out.append({"tier": p["tier"], "unlocked": False})
    return {"plays": out, "unlocked": sum(1 for p in plays if p["unlocked"]), "total": len(plays)}


def for_ethan(db) -> list:
    """小伴 回话时：已解锁的玩法和做法；还没揭晓过的标 new，由他在聊天里揭晓。"""
    ensure(db)
    got = {r["key"]: r["announced"] for r in db.conn.execute("SELECT key, announced FROM play_unlocks")}
    out, new_given = [], False
    for p in PLAYS:
        if p["key"] not in got:
            continue
        if not got[p["key"]]:
            if new_given:  # 一次只揭晓一个，剩下的留到下次聊天
                continue
            new_given = True
        out.append({"key": p["key"], "name": p["name"], "how": p["how"], "reveal": p["reveal"], "new": not got[p["key"]]})
    return out


def mark_announced(db, keys):
    ensure(db)
    db.conn.executemany("UPDATE play_unlocks SET announced = 1 WHERE key = ?", [(k,) for k in keys])
    db.conn.commit()
