"""领地地图和领地页的数据（2026-10-28 她确认设计稿，规则见 docs/design/territories.md）。

七块领地 + 荒原（没归类的待办）。每块领地的健康线逐项自动算，不用她打卡：
  绿 ok：达到副本标准（没有副本的领地按健康线）；黄 warn：副本落后但在保底线以上；红 bad：跌破保底线；
  灰 none：还没有数据来源。领地的颜色取最差的一项，灰的不算；全灰就是灰。
一周没过完时按进度比：今天还没过完，不算欠账——比如周三，“每周 5 次”的副本标准要求到昨天为止 5×2/7，
今天做了也算进去。所以周一早上不会一片红。
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import lifelog, places_log, worktime

ORDER = {"bad": 0, "warn": 1, "ok": 2, "none": 3}
TERRITORIES = [  # (key, 名字, 数据库里的领地名)；身体区域 2026-10-29 她改叫“健康”，库里还记“身体”
    ("body", "健康", "身体"), ("career", "事业", "事业"), ("money", "财务", "财务"), ("social", "社交", "社交"),
    ("study", "学习", "学习"), ("academic", "学术", "学术"), ("life", "生活体验", "生活体验")]
NAME = {k: n for k, n, _ in TERRITORIES}
DOMAIN = {k: d for k, _, d in TERRITORIES}
KEY_OF = {d: k for k, _, d in TERRITORIES} | {"健康": "body", "休闲": "life", "旅行": "life", "感情": "social"}
WASTE = "waste"
HABIT_TERRITORY = {"训练": "body", "吃够蛋白质": "body", "训练前称体重": "body", "0 点前睡": "body",
                   "睡前拉伸 5 分钟": "body", "每天出门": "body", "英语陪练（Grok）": "study",
                   "练英语：和 小伴 说 3 句": "study", "在日常聊天里练英语，沉淀单词和句子": "study",
                   "每周 1 次新体验": "life"}
# 小伴 先猜待办属于哪块领地（她在荒原里确认或改）；按关键词，猜不出就留在荒原
GUESS = [
    ("life", ("签证", "旅行", "机票", "酒店", "体验", "舞蹈", "网球", "普拉提", "极光", "芬兰", "parkrun", "万圣")),
    ("money", ("信用卡", "还款", "Monzo", "招行", "账", "预算", "应急金", "收入", "理财")),
    ("social", ("微信", "朋友", "聚会", "组局", "生日", "联系")),
    ("body", ("体检", "训练", "体重", "体脂", "physio", "饮食", "餐单", "睡")),
    ("study", ("英语", "某商学院", "课程", "学习")),
    ("academic", ("论文", "博士", "进修", "研究", "教授")),
    ("career", ("自媒体", "小红书", "Instagram", "LinkedIn", "DemoApp", "客户", "营销", "example.com", "创业")),
]


def guess(title: str):
    for key, name, db_name in TERRITORIES:  # 标题里直接写了区域名
        if name in title or db_name in title:
            return key
    for key, words in GUESS:
        if any(w.lower() in title.lower() for w in words):
            return key
    return None


def worst(lights: list) -> str:
    real = [l for l in lights if l != "none"]
    return min(real, key=ORDER.get) if real else "none"


def _week(day: date) -> tuple:
    start = day - timedelta(days=day.weekday())
    return start, day.weekday() + 1  # 本周一、已经过了几天（含今天）


def _habit_days(db, title: str, start: date, day: date):
    h = db.conn.execute("SELECT id, active FROM habits WHERE title = ?", (title,)).fetchone()
    if h is None or not h["active"]:
        return None
    return db.conn.execute("SELECT COUNT(DISTINCT day) FROM habit_log WHERE habit_id = ? AND day >= ? AND day <= ?",
                           (h["id"], start.isoformat(), day.isoformat())).fetchone()[0]


def _paced(actual, quest_target, floor_target, elapsed: int, has_quest: bool) -> str:
    """按本周进度判灯：quest_target / floor_target 是整周的次数；elapsed 含今天，要求只算到昨天。"""
    if actual is None:
        return "none"
    elapsed -= 1
    if has_quest and quest_target is not None:
        if actual >= quest_target * elapsed / 7 - 1e-9:
            return "ok"
        return "warn" if actual >= floor_target * elapsed / 7 - 1e-9 else "bad"
    return "ok" if actual >= floor_target * elapsed / 7 - 1e-9 else "bad"


def _row(name, quest, floor, actual_text, light, note=""):
    return {"name": name, "quest": quest, "floor": floor, "actual": actual_text, "light": light, "note": note}


def _weights(db, day: date, days: int = 28) -> list:
    """每天的 7 天平均体重（有记录的日子才算），给曲线和趋势用。"""
    rows = db.conn.execute("SELECT day, AVG(weight) w FROM body_measures WHERE weight IS NOT NULL AND day <= ? "
                           "AND day >= ? GROUP BY day ORDER BY day",
                           (day.isoformat(), (day - timedelta(days=days + 7)).isoformat())).fetchall()
    pts = [(date.fromisoformat(r["day"]), r["w"]) for r in rows]
    out = []
    for d, _ in pts:
        if d < day - timedelta(days=days):
            continue
        win = [w for dd, w in pts if d - timedelta(days=6) <= dd <= d]
        out.append({"day": d.isoformat(), "avg": round(sum(win) / len(win), 2)})
    return out


def _training_leaves(db, start: date, day: date) -> list:
    """这几天计划里去训练的出发时间（本地），用来把去训练的那趟排除在“非工作的事”之外。"""
    out = []
    for i in range((day - start).days + 1):
        d = start + timedelta(days=i)
        plan = db.latest_plan(d.isoformat())
        for it in (plan or {}).get("items", []):
            if it.get("kind") == "event" and it.get("title", "").startswith("训练") and it.get("leave"):
                hh, mm = map(int, it["leave"].split(":"))
                out.append(places_log._local(datetime.now(timezone.utc)).replace(year=d.year, month=d.month, day=d.day,
                                                                                    hour=hh, minute=mm, second=0, microsecond=0))
    return out


def health(db, key: str, day: date, quests_active: bool, root: Path = None) -> list:
    start, elapsed = _week(day)
    rows = []
    if key == "body":
        q = quests_active
        for title, name, quest, floor in (("训练", "训练", 5, 4), ("吃够蛋白质", "吃够蛋白质", 7, 5),
                                           ("0 点前睡", "0 点前睡", 6, 5), ("每天出门", "每天出门", 7, 6)):
            n = _habit_days(db, title, start, day)
            light = _paced(n, quest, floor, elapsed, q)
            rows.append(_row(name, f"每周 {quest} 天" if quest < 7 else "每天", f"每周 {floor} 天",
                             f"{n} / {elapsed} 天" if n is not None else "—", light,
                             "" if n is not None else "等手机定位" if title == "每天出门" else "还没有记录"))
        w = _weights(db, day, 14)
        if len(w) >= 2 and date.fromisoformat(w[0]["day"]) <= day - timedelta(days=6):
            week_ago = next((p for p in reversed(w) if date.fromisoformat(p["day"]) <= day - timedelta(days=7)), w[0])
            drop = round(week_ago["avg"] - w[-1]["avg"], 2)
            light = ("ok" if 0.3 <= drop <= 0.7 else "bad" if drop < 0 else "warn") if q else (
                "ok" if abs(drop) <= 1 else "warn")
            rows.append(_row("体重（7 天平均）", "每周降 0.3–0.7 kg", "—", f"{'降' if drop >= 0 else '涨'} {abs(drop)} kg",
                             light))
        else:
            rows.append(_row("体重（7 天平均）", "每周降 0.3–0.7 kg", "—", "—", "none", "记满一周才算趋势"))
    elif key == "study":
        n = _habit_days(db, "英语陪练（Grok）", start, day)
        rows.append(_row("英语陪练（Grok）", "—", "每周 5 次", f"{n} 次" if n is not None else "—",
                         _paced(n, None, 5, elapsed, False), "" if n is not None else "还没开"))
        if lifelog.has(db, "topic"):
            n = lifelog.count(db, "topic", start, day, distinct_days=True)
            rows.append(_row("认知拓展", "—", "每天 1 个话题", f"{n} 天", _paced(n, None, 7, elapsed, False)))
        else:
            rows.append(_row("认知拓展", "—", "每天 1 个话题", "—", "none", "小伴 聊到新话题会记下来"))
    elif key == "life":
        n = _habit_days(db, "每周 1 次新体验", start, day)
        light = "none" if n is None else "ok" if n >= 1 else ("warn" if elapsed >= 5 else "ok")
        rows.append(_row("每周新体验", "每周 1 次", "每周 1 次", f"{n} 次" if n is not None else "—", light,
                         "" if n is None or n >= 1 else "这周还没去" if elapsed >= 5 else "这周还有时间"))
        if places_log.has_data(db):
            n = places_log.non_work_outings(db, start, day, _training_leaves(db, start, day))
            light = "ok" if n >= 1 else ("warn" if elapsed >= 5 else "ok")
            rows.append(_row("出门做非工作的事", "—", "每周 1 次", f"{n} 次", light,
                             "按手机定位：出去满 1 小时、不是去训练" if n else "这周还没有" if elapsed >= 5 else "这周还有时间"))
        else:
            rows.append(_row("出门做非工作的事", "—", "每周 1 次", "—", "none", "等手机定位"))
    elif key == "career":
        if worktime.has_data(db):
            w = worktime.week_row(db, day)
            rows.append(_row("本人工作时长", "—", "每天不超过 8 小时", f"今天 {w['today']:g} 小时 · 本周最长 {w['max']:g} · 日均 {w['avg']:g}",
                             w["light"], "超过 8 小时黄、超过 10 小时红；目标一周比一周少"))
        else:
            rows.append(_row("本人工作时长", "—", "每天不超过 8 小时", "—", "none", "等 Mac 上的记录脚本"))
        rows += [
                 _row("小红书 + Instagram 发帖", "每周各 2 条", "—", "—", "none", "等接账号")]
    elif key == "money":
        from . import finance
        if finance.has_source(db, "monzo") or finance.has_source(db, "sms") or finance.has_source(db, "chat"):
            spent = finance.month_spend(db, day)
            text = " + ".join(f"{'£' if c == 'GBP' else '¥' if c == 'CNY' else c + ' '}{v:,.0f}" for c, v in spent.items()) or "0"
            budget = settings_budget(db, day)
            if budget:  # 预算只有一个数（2026-10-29 她定），别的币种按汇率折过来一起算
                cur, cap = next(iter(budget.items()))
                rate = finance.fx_rate(db)
                total = finance.convert(spent, cur, rate)
                sym = "£" if cur == "GBP" else "¥"
                if len(spent) > 1 or (spent and cur not in spent):
                    text = f"{sym}{total:,.0f}（{text}，按 £1≈¥{rate:.2f}）"
                light = "bad" if total > cap else "warn" if total > cap * day.day / 30 else "ok"
                rows.append(_row("每月花销", "—", f"不超 {sym}{cap:,.0f}", f"本月 {text}", light))
            else:
                rows.append(_row("每月花销", "—", "不超预算", f"本月 {text}", "ok",
                                 "还没定每月预算：在下面“每月预算”里填一下，超了会变红"))
        else:
            rows.append(_row("每月花销", "—", "不超预算", "—", "none", "等接 Monzo 和招行（docs/operations/finance.md）"))
    elif key == "social":
        if lifelog.has(db, "meet") or lifelog.has(db, "new_friend") or lifelog.has(db, "contact"):
            met = lifelog.count(db, "meet", start, day)
            rows.append(_row("和朋友见面", "—", "每周 1 次", f"{met} 次", "ok" if met >= 1 else ("warn" if elapsed >= 5 else "ok"),
                             "" if met else "这周还没见" if elapsed >= 5 else "这周还有时间"))
            month = day.replace(day=1)
            new = lifelog.count(db, "new_friend", month, day)
            rows.append(_row("认识新朋友", "—", "每月 3–4 个", f"本月 {new} 个",
                             "ok" if new >= 3 or day.day < 20 else ("warn" if new >= 1 else "bad")))
            due = [p for p in db.people_to_contact(day) if "long_no_contact" in p["reasons"]]
            rows.append(_row("联系重要的人", "—", "按各自频率", f"{len(due)} 个该联系了" if due else "都联系过",
                             "ok" if not due else "warn" if len(due) <= 2 else "bad", "、".join(p["name"] for p in due[:4])))
        else:
            rows += [_row("和朋友见面", "—", "每周 1 次", "—", "none", "小伴 聊到见了谁会记下来"),
                     _row("认识新朋友", "—", "每月 3–4 个", "—", "none", "小伴 聊到认识新朋友会记下来"),
                     _row("联系重要的人", "—", "按各自频率", "—", "none", "人物卡里设好多久联系一次")]
    elif key == "academic":
        if lifelog.has(db, "research"):
            mins = lifelog.total(db, "research", start, day)
            rows.append(_row("研究时间", "—", "每周 1 次（约 2 小时）", f"{mins / 60:.1f} 小时",
                             "ok" if mins >= 120 else ("warn" if mins > 0 or elapsed < 6 else "bad")))
        else:
            rows.append(_row("研究时间", "—", "每周 1 次（约 2 小时）", "—", "none", "做了研究告诉 小伴，他会记下来"))
    return rows


BUDGET_SQL = "CREATE TABLE IF NOT EXISTS budget_plan (month TEXT PRIMARY KEY, amount REAL NOT NULL, set_at TEXT NOT NULL)"


def budget_plan(db, day: date = None) -> dict:
    """每月预算（2026-10-29 她定）：只按人民币算，定了就要遵守——这个月的锁定，改的只从下个月起生效。
    返回 {"now": 本月预算或 None, "next": 下个月起的新预算或 None}。"""
    day = day or date.today()
    db.conn.execute(BUDGET_SQL)
    month, nxt = day.strftime("%Y-%m"), (day.replace(day=28) + timedelta(days=4)).strftime("%Y-%m")
    if not db.conn.execute("SELECT 1 FROM budget_plan LIMIT 1").fetchone():  # 以前存在 budgets 表里的，算本月的
        db.conn.execute("CREATE TABLE IF NOT EXISTS budgets (currency TEXT PRIMARY KEY, amount REAL NOT NULL)")
        old = db.conn.execute("SELECT amount FROM budgets WHERE currency = 'CNY'").fetchone()
        if old:
            db.conn.execute("INSERT INTO budget_plan (month, amount, set_at) VALUES (?, ?, ?)", (month, old["amount"], day.isoformat()))
            db.conn.commit()
    cur = db.conn.execute("SELECT amount FROM budget_plan WHERE month <= ? ORDER BY month DESC LIMIT 1", (month,)).fetchone()
    later = db.conn.execute("SELECT amount FROM budget_plan WHERE month = ?", (nxt,)).fetchone()
    return {"now": cur["amount"] if cur and cur["amount"] > 0 else None, "next": later["amount"] if later else None,
            "next_month": nxt}


def set_budget(db, amount: float, day: date = None) -> str:
    """还没定过就从本月生效；本月已经定了，就只改下个月起的。返回生效的月份。"""
    day = day or date.today()
    plan = budget_plan(db, day)
    month = day.strftime("%Y-%m") if plan["now"] is None else plan["next_month"]
    db.conn.execute("INSERT INTO budget_plan (month, amount, set_at) VALUES (?, ?, ?) ON CONFLICT(month) DO UPDATE SET "
                    "amount = excluded.amount, set_at = excluded.set_at", (month, amount, day.isoformat()))
    db.conn.commit()
    return month


def settings_budget(db, day: date = None) -> dict:
    """本月的预算，{"CNY": 金额}；没定就是空。"""
    now = budget_plan(db, day)["now"]
    return {"CNY": now} if now else {}

def explore_stats(root: Path) -> dict:
    """探索地图（她的飞书表格导出）：国外点亮了几个国家、14 条人生 Goal 的状态。"""
    try:
        sheets = json.loads((Path(root) / "explore-map.json").read_text(encoding="utf-8"))["sheets"]
    except (OSError, ValueError, KeyError):
        return {}
    countries = set()
    for name in ("亚洲", "欧洲", "大洋洲", "北美洲", "南美洲", "非洲"):
        for r in sheets.get(name, []):
            if r.get("状态") == "已结束" and r.get("国家"):
                countries.add(r["国家"])
    goals = sheets.get("Goal", [])
    china = sum(1 for r in sheets.get("中国", []) if r.get("状态") == "已结束")
    return {"countries": len(countries), "china": china,
            "goals_done": sum(1 for g in goals if g.get("状态") == "已结束"),
            "goals_doing": sum(1 for g in goals if g.get("状态") == "进行中"), "goals_total": len(goals)}


def task_territory(t: dict):
    """待办归哪块领地：她选过的优先，其次所属副本的领地；都没有就在荒原。"""
    if t.get("domain"):
        return t["domain"] if t["domain"] in NAME else None
    return KEY_OF.get(t.get("goal_domain") or "")


def open_tasks(db) -> list:
    return [dict(r) for r in db.conn.execute(
        "SELECT t.id, t.title, t.due, t.quest, t.goal_id, t.domain, g.domain AS goal_domain FROM tasks t "
        # 暂停副本下的事照样是待办（2026-10-29 她定：副本只影响优先级，不影响要不要做），排在后面
        "LEFT JOIN goals g ON g.id = t.goal_id WHERE t.done = 0 AND t.promised_by_ethan = 0 "
        "AND COALESCE(g.status, 'active') != 'dropped' ORDER BY COALESCE(g.status, 'active') = 'paused', t.due IS NULL, t.due, t.id")]
