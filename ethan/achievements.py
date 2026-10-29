"""成就（2026-10-27 晚她确认，内容见 docs/design/territories.md“成就”）。

全部由代码按真实数据算，不让她打卡：有数据来源的显示进度，还没有来源的标“等记录”（以后接上一个亮一个）。
奖励只给解锁、不给积分（免得刷成就）：解锁 小伴 的惊喜玩法（ethan/surprises.py，她不要提前看到，页面上没解锁的不露名字）。
铜 / 银 / 金三档：达到第一档就算拿到铜，依次类推。快达成的排在最上面。
"""

import json
from datetime import date, timedelta
from pathlib import Path

from . import english, lifelog, places_log, quests, review, surprises, territory

TIERS = ("铜", "银", "金")


def _habit_count(db, title: str) -> int:
    return db.conn.execute("SELECT COUNT(DISTINCT l.day) FROM habit_log l JOIN habits h ON h.id = l.habit_id "
                           "WHERE h.title = ?", (title,)).fetchone()[0]


def _best_run(db, title: str) -> int:
    """日常最长连续几天。"""
    days = sorted({date.fromisoformat(r[0]) for r in db.conn.execute(
        "SELECT l.day FROM habit_log l JOIN habits h ON h.id = l.habit_id WHERE h.title = ?", (title,))})
    best = run = 0
    for i, d in enumerate(days):
        run = run + 1 if i and (d - days[i - 1]).days == 1 else 1
        best = max(best, run)
    return best


_best_habit_run = _best_run


def _train_week(db) -> bool:
    weeks = {}
    for r in db.conn.execute("SELECT DISTINCT l.day FROM habit_log l JOIN habits h ON h.id = l.habit_id WHERE h.title = '训练'"):
        k = date.fromisoformat(r[0]).isocalendar()[:2]
        weeks[k] = weeks.get(k, 0) + 1
    return any(n >= 5 for n in weeks.values())


def _full_weekend(db) -> bool:
    sats = {date.fromisoformat(r[0]) for r in db.conn.execute("SELECT DISTINCT day FROM habit_log")}
    return any(d.weekday() == 5 and quests.full_day(db, d) and quests.full_day(db, d + timedelta(days=1)) for d in sats)


def _weekly_hits(db, title: str) -> int:
    """周常达标了几周。"""
    h = db.conn.execute("SELECT id, target FROM habits WHERE title = ?", (title,)).fetchone()
    if not h:
        return 0
    weeks = {}
    for r in db.conn.execute("SELECT day FROM habit_log WHERE habit_id = ?", (h["id"],)):
        d = date.fromisoformat(r["day"])
        key = d.isocalendar()[:2]
        weeks[key] = weeks.get(key, 0) + 1
    return sum(1 for n in weeks.values() if n >= h["target"])


def _done_task(db, *words) -> bool:
    return any(db.conn.execute("SELECT 1 FROM tasks WHERE done = 1 AND title LIKE ?", (f"%{w}%",)).fetchone() for w in words)


def _explore(root: Path) -> dict:
    try:
        return json.loads((Path(root) / "explore-map.json").read_text(encoding="utf-8"))["sheets"]
    except (OSError, ValueError, KeyError):
        return {}


def _tiered(key, name, cat, value, steps, unit, source=True, hint=""):
    got = sum(1 for s in steps if value is not None and value >= s)
    nxt = steps[got] if got < len(steps) else None
    return {"key": key, "name": name, "cat": cat, "value": value, "steps": list(steps), "unit": unit,
            "tier": TIERS[got - 1] if got else None, "next": nxt,
            "frac": (min(1.0, (value or 0) / nxt) if nxt and value is not None else 1.0 if not nxt else 0),
            "source": source, "hint": hint}


def _once(key, name, cat, done, source=True, hint=""):
    return {"key": key, "name": name, "cat": cat, "done": bool(done), "source": source, "hint": hint, "steps": None}


# 成就的名字和说明（2026-10-29 她说原来写得太随便：名字要像个称号，说明写清楚怎么拿）
NAMES = {
    "first_weigh": ("直面数字", "第一次称体重"),
    "first_dance": ("第一支舞", "上第一节舞蹈课，K-pop、古典舞都算"),
    "first_xhs": ("第一次发声", "发出第一篇小红书"),
    "first_english": ("十分钟不切中文", "用英语连续聊满 10 分钟"),
    "first_prof": ("敲开学术的门", "给教授发出第一封邮件"),
    "site_live": ("我的主页", "example.com 上线"),
    "streak": ("满勤连击", "连续满勤的天数"),
    "outing": ("日日出门", "连续每天出门的天数（手机定位算）"),
    "english_weeks": ("英语周周达标", "英语陪练达标的周数"),
    "xhs_weeks": ("周更不断", "小红书每周都更新的周数"),
    "train": ("铁与汗", "累计训练次数"),
    "friends": ("新朋友图鉴", "累计认识的新朋友（家人和老朋友不算）"),
    "experiences": ("体验收集家", "累计新体验次数"),
    "phrases": ("英语词句库", "攒下的英语词句"),
    "bodyfat": ("雕刻身形", "体脂降到 25% / 22% / 20%"),
    "countries": ("点亮世界", "探索地图上点亮的国家"),
    "hikes": ("城市山径", "走完的城市徒步路线"),
    "green": ("长青区域", "任意一个区域连续全绿的周数"),
    "book": ("我的书", "初稿 → 签约或自出版 → 正式出版"),
    "song": ("我的歌", "写完第一首 → 发布 → 播放过万"),
    "fans": ("被看见", "小红书和 Instagram 粉丝合计"),
    "hidden:sleep_week": ("早睡的人", "连续 7 天 0 点前睡"),
    "hidden:train_week": ("练满一周", "一周练满 5 次"),
    "hidden:protein_week": ("喂饱肌肉", "连续 7 天吃够蛋白质"),
    "hidden:stretch_week": ("柔软的夜", "连续 7 天睡前拉伸"),
    "hidden:rest": ("会停的人", "第一次用休整日"),
    "hidden:weekend": ("周末不打烊", "同一个周末的周六、周日都满勤"),
}
# 隐藏成就没达成时只给一句谜语一样的提示（2026-10-29 她要：目标是引导她去做，不是藏着不让发现）
RIDDLES = {  # 2026-10-29 她说原来的太直白：要像谜语，读得出方向、读不出答案
    "hidden:sleep_week": "灰姑娘的规矩，守满七回。",
    "hidden:train_week": "一周有五天，镜子里都是同一个倔强的人。",
    "hidden:protein_week": "七天里，没亏待过那些会变强的部分。",
    "hidden:stretch_week": "像猫一样，连着七个夜里，睡前都伸了个懒腰。",
    "hidden:rest": "骑士也有卸甲的一天。",
    "hidden:weekend": "别人的假日，你的满格。",
}
LEGEND = [("敲钟", "公司上市"), ("成家", "结婚"), ("新生命", "生娃")]


def compute(db, root: Path, day: date) -> dict:
    items = []
    best = max(quests.streak(db, day)["best"], quests.streak_best_recorded(db))
    sheets = _explore(root)
    ex = territory.explore_stats(root)
    hikes = next((v for k, v in sheets.items() if "徒步" in k), [])
    fat = db.conn.execute("SELECT MIN(bodyfat) FROM body_measures WHERE bodyfat IS NOT NULL").fetchone()[0]
    people = lifelog.new_friends(db)  # 只算新认识的（家人和老朋友不算，2026-10-28）
    phrases = db.conn.execute("SELECT COUNT(*) FROM learning_items WHERE subject LIKE '%英语%' OR subject LIKE '%English%'").fetchone()[0]
    weighed = db.conn.execute("SELECT COUNT(*) FROM body_measures WHERE weight IS NOT NULL").fetchone()[0]

    # 第一次
    items += [_once("first_weigh", "第一次称体重", "第一次", weighed > 0),
              _once("first_dance", "第一节舞蹈课", "第一次", _done_task(db, "舞蹈课", "K-pop", "古典舞")),
              _once("first_xhs", "第一篇小红书", "第一次", _done_task(db, "发出第一篇", "第一篇小红书")),
              _once("first_english", "第一次用英语聊满 10 分钟", "第一次", english.longest(db) >= 10),
              _once("first_prof", "第一封给教授的邮件", "第一次", _done_task(db, "给教授", "教授的邮件")),
              _once("site_live", "example.com 上线", "第一次", _done_task(db, "example.com 个人主页上线", "example.com 上线"))]
    # 连续
    items += [_tiered("streak", "连续满勤", "连续", best, (7, 30, 100), "天"),
              _tiered("outing", "每天出门", "连续", _best_run(db, "每天出门") if places_log.has_data(db) else None,
                      (7, 30, 100), "天", source=places_log.has_data(db), hint="" if places_log.has_data(db) else "等手机定位"),
              _tiered("english_weeks", "英语陪练达标", "连续", _weekly_hits(db, "英语陪练（Grok）"), (2, 4, 12), "周"),
              _tiered("xhs_weeks", "小红书每周更新", "连续", None, (4, 12, 26), "周", source=False, hint="等接小红书账号")]
    # 累计
    items += [_tiered("train", "累计训练", "累计", _habit_count(db, "训练"), (50, 150, 500), "次"),
              _tiered("friends", "新朋友", "累计", people, (10, 50, 100), "个", hint="小伴 记下的新认识的人"),
              _tiered("experiences", "新体验", "累计", _habit_count(db, "每周 1 次新体验"), (10, 50, 100), "次"),
              _tiered("phrases", "英语词句", "累计", phrases, (100, 500, 2000), "条")]
    # 领地
    got_fat = sum(1 for t in (25, 22, 20) if fat is not None and fat <= t)  # 体脂越低越好
    items += [{"key": "bodyfat", "name": "体脂降到", "cat": "领域", "value": fat, "steps": [25, 22, 20], "unit": "%",
               "tier": TIERS[got_fat - 1] if got_fat else None, "next": (25, 22, 20)[got_fat] if got_fat < 3 else None,
               "frac": 0 if fat is None else 1.0 if got_fat == 3 else max(0.0, min(1.0, (30 - fat) / (30 - (25, 22, 20)[got_fat]))),
               "source": fat is not None, "hint": "" if fat is not None else "等测体脂", "lower": True},
              _tiered("countries", "探索地图点亮国家", "领域", ex.get("countries"), (20, 25, 30), "个", source=bool(ex)),
              _tiered("hikes", "城市徒步", "领域", sum(1 for r in hikes if r.get("状态") == "已结束") if hikes else None,
                      (10, 25, 47), "条", source=bool(hikes)),
              _tiered("green", "某个区域连续全绿", "领域", review.green_streak(db), (4, 12, 26), "周", hint="按战报里过完的周算")]
    # 作品
    items += [_tiered("book", "出一本书", "作品", lifelog.best_stage(db, "book") or 0, (1, 2, 3), "", hint="初稿 / 签约或自出版 / 正式出版，告诉 小伴"),
              _tiered("song", "写一首歌", "作品", lifelog.best_stage(db, "song") or 0, (1, 2, 3), "", hint="写完第一首 / 发布 / 播放过万，告诉 小伴"),
              _tiered("fans", "自媒体粉丝", "作品", None, (1000, 10000, 100000), "", source=False, hint="小红书和 Instagram 合计，等接账号")]
    # 人生 Goal（探索地图）
    for g in sheets.get("Goal", []):
        name = g.get("Goal") or ""
        if name and "56 个民族" not in name:  # 她删掉了这条
            items.append(_once("goal:" + name, name, "人生 Goal", g.get("状态") == "已结束", hint=g.get("状态") or ""))
    # 隐藏：都列出来，没达成只显示谜语提示，达成那一刻弹出来、揭晓名字（2026-10-29 她改：引导她做，不是藏起来）
    hidden = [_once("hidden:sleep_week", "0 点前睡满一周", "隐藏", _best_habit_run(db, "0 点前睡") >= 7),
              _once("hidden:train_week", "一周练满 5 次", "隐藏", _train_week(db)),
              _once("hidden:protein_week", "连续 7 天吃够蛋白质", "隐藏", _best_habit_run(db, "吃够蛋白质") >= 7),
              _once("hidden:stretch_week", "连续 7 天睡前拉伸", "隐藏", _best_habit_run(db, "睡前拉伸 5 分钟") >= 7),
              _once("hidden:rest", "第一次用休整日", "隐藏", db.conn.execute("SELECT 1 FROM rest_days LIMIT 1").fetchone()),
              _once("hidden:weekend", "周末两天都满勤", "隐藏", _full_weekend(db))]
    for h in hidden:
        h["riddle"] = RIDDLES[h["key"]]
    items += hidden
    for it in items:
        if it["key"] in NAMES:
            it["name"], it["desc"] = NAMES[it["key"]]
        elif it["cat"] == "人生 Goal":
            it["desc"] = "探索地图里的人生 Goal"
    legend = [{"name": n, "desc": d, "cat": "终极"} for n, d in LEGEND]
    for it in items:  # 快达成的排前面：有进度、离下一档最近的
        it["near"] = it.get("frac", 0) if it.get("steps") and it.get("next") and it.get("source") else -1
    got = sum(1 for i in items if i.get("done") or i.get("tier"))
    rewards = surprises.page(surprises.sync(db, items))
    return {"legend": legend, "items": items, "rewards": rewards, "got": got, "total": len(items),
            "hidden": "隐藏成就照着提示猜，做到了才揭晓名字。"}


# ---------- 达成那一刻弹出来（2026-10-28 她要：不管在哪个界面都弹，让她开心一下） ----------
SEEN = "CREATE TABLE IF NOT EXISTS ach_seen (key TEXT PRIMARY KEY, tier TEXT, at TEXT NOT NULL, shown INTEGER NOT NULL DEFAULT 0);"
RANK = {None: 0, "达成": 1, "铜": 1, "银": 2, "金": 3}


def _level(i) -> str:
    return i.get("tier") or ("达成" if i.get("done") else None)


def celebrate(db, root: Path, day: date) -> list:
    """新达成（或升档）还没弹过的成就。第一次用时把已有的都记成弹过，免得一下子弹一串。"""
    db.conn.executescript(SEEN)
    first = db.conn.execute("SELECT 1 FROM ach_seen LIMIT 1").fetchone() is None
    data = compute(db, root, day)
    seen = {r["key"]: r for r in db.conn.execute("SELECT * FROM ach_seen")}
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for i in data["items"]:
        lv = _level(i)
        if not lv:
            continue
        old = seen.get(i["key"])
        if old is None or RANK[lv] > RANK.get(old["tier"], 0):
            db.conn.execute("INSERT INTO ach_seen (key, tier, at, shown) VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                            "tier = excluded.tier, at = excluded.at, shown = excluded.shown", (i["key"], lv, now, int(first)))
    db.conn.commit()
    by_key = {i["key"]: i for i in data["items"]}
    out = []
    for r in db.conn.execute("SELECT * FROM ach_seen WHERE shown = 0 ORDER BY at"):
        i = by_key.get(r["key"])
        if i:
            out.append({"key": i["key"], "name": i["name"], "cat": i["cat"], "tier": r["tier"],
                        "hidden": i["cat"] == "隐藏"})
    if out:
        new_plays = [p for p in surprises.for_ethan(db) if p["new"]]
        out[-1]["gift"] = len(new_plays)  # 同时解锁了几个惊喜（不说是什么）
    # 商店里攒够了能拆的，弹一次（2026-10-29 她说小惊喜能拆了系统没提示）；拆过以后再攒够还会再弹
    bal = quests.totals(db)["balance"]
    for r in quests.rewards(db):
        if bal >= r["cost"]:
            n = db.conn.execute("SELECT COUNT(*) FROM redemptions WHERE reward_id = ?", (r["id"],)).fetchone()[0]
            key = f"shop:{r['id']}:{n}"
            if not db.conn.execute("SELECT 1 FROM ach_seen WHERE key = ? AND shown = 1", (key,)).fetchone():
                db.conn.execute("INSERT OR IGNORE INTO ach_seen (key, tier, at, shown) VALUES (?, '达成', ?, 0)", (key, now))
                out.append({"key": key, "name": f"能拆「{r['title']}」了", "cat": "商店", "tier": "达成", "shop": True, "reward": r["id"]})
    db.conn.commit()
    # 小伴 在聊天里揭晓了的奖励，工作台上也弹一次（2026-10-29 她说解锁了新奖励自己都不知道）
    out += [{"key": "play:" + p["key"], "name": p["name"], "cat": "奖励", "tier": p["tier"], "play": True, "note": p["reveal"]}
            for p in surprises.reveals(db)]
    return out


def mark_shown(db, keys):
    surprises.mark_reveal_shown(db, [k[5:] for k in keys if k.startswith("play:")])
    db.conn.executescript(SEEN)
    db.conn.executemany("UPDATE ach_seen SET shown = 1 WHERE key = ?", [(k,) for k in keys])
    db.conn.commit()
