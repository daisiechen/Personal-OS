"""领地地图和领地页的数据（设计稿，规则见 docs/design/territories.md）。

七块领地 + 荒原（没归类的待办）。每块领地的健康线逐项自动算，不用她打卡：
  绿 ok：达到副本标准（没有副本的领地按健康线）；黄 warn：副本落后但在保底线以上；红 bad：跌破保底线；
  灰 none：还没有数据来源。领地的颜色取最差的一项，灰的不算；全灰就是灰。
一周没过完时按进度比：今天还没过完，不算欠账——比如周三，“每周 5 次”的副本标准要求到昨天为止 5×2/7，
今天做了也算进去。所以周一早上不会一片红。
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import standards
from . import lifelog, places_log, worktime
from .clock import her_today
from . import clock

ORDER = {"bad": 0, "warn": 1, "ok": 2, "none": 3}
TERRITORIES = [  # (key, 名字, 数据库里的领地名)；身体区域 改叫“健康”，库里还记“身体”
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
    start = clock.week_start(day)
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
    if elapsed <= 0 and not actual:  # 周一：还没到该做的时候，不算达标也不算落后（本周 0 次怎么是达标）
        return "none"
    if has_quest and quest_target is not None:
        if actual >= quest_target * elapsed / 7 - 1e-9:
            return "ok"
        return "warn" if actual >= floor_target * elapsed / 7 - 1e-9 else "bad"
    return "ok" if actual >= floor_target * elapsed / 7 - 1e-9 else "bad"


def cognitive_days(db, start: date, day: date):
    """认知拓展：聊到新话题（[话题]）或 小伴 教了一个知识点（ethan/lessons.py，每天一个）的天数；都没记录过是 None。"""
    days = set()
    if lifelog.has(db, "topic"):
        lifelog.ensure(db)
        days |= {r[0] for r in db.conn.execute("SELECT DISTINCT day FROM life_log WHERE kind = 'topic' AND day >= ? AND day <= ?",
                                                (start.isoformat(), day.isoformat()))}
    has_lessons = db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'lessons'").fetchone()
    if has_lessons:
        days |= {r[0][:10] for r in db.conn.execute("SELECT taught_at FROM lessons WHERE taught_at >= ? AND taught_at < ?",
                                                    (start.isoformat(), (day + timedelta(days=1)).isoformat()))}
    if not days and not lifelog.has(db, "topic") and not (has_lessons and db.conn.execute("SELECT 1 FROM lessons WHERE taught_at IS NOT NULL").fetchone()):
        return None
    return len(days)


def _row(name, quest, floor, actual_text, light, note="", edit=None, log=None):
    """edit：保底旁边放 ✎（budget = 改每月预算）；log：这一项旁边放“＋ 记一笔”（社交的见面、新朋友、联系）。"""
    out = {"name": name, "quest": quest, "floor": floor, "actual": actual_text, "light": light, "note": note}
    if edit:
        out["edit"] = edit
    if log:
        out["log"] = log
    return out


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
        for k in ("training", "protein", "sleep", "outside"):  # 目标 / 保底只从领域标准表来（ethan/standards.py，她的决定 8）
            sd = standards.get(k)
            title, name = sd.habit, sd.name
            n = _habit_days(db, title, start, day)
            light = _paced(n, sd.target, sd.floor, elapsed, q)
            rows.append(_row(name, standards.week_label(k, "target"), standards.week_label(k, "floor"),
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
        en, cg = standards.get("english"), standards.get("cognitive")
        from . import english  # 次数 = 有练习记录的天 ∪ 打卡的天，和学习页的图同一个口径（english.practice_days）
        days = english.practice_days(db, start, day)
        n = None if days is None else len(days)
        rows.append(_row(en.habit, standards.week_label("english", "target"), standards.week_label("english", "floor"),
                         f"{n} 次" if n is not None else "—",
                         _paced(n, en.target, en.floor, elapsed, quests_active), "" if n is not None else "还没开"))
        n = cognitive_days(db, start, day)
        quest_c, floor_c = standards.week_label("cognitive", "target"), standards.week_label("cognitive", "floor")
        if n is not None:
            rows.append(_row(cg.name, quest_c, floor_c, f"{n} 天", _paced(n, cg.target, cg.floor, elapsed, quests_active),
                             "聊到新话题或学了知识点的天数"))
        else:
            rows.append(_row(cg.name, quest_c, floor_c, "—", "none", "小伴 聊到新话题会记下来"))
    elif key == "life":
        ne, ou = standards.get("new_exp"), standards.get("outing")  # 每周 1 次的：周五前没做黄，不按天折算
        n = _habit_days(db, ne.habit, start, day)
        light = "none" if n is None else "ok" if n >= ne.floor else ("warn" if elapsed >= 5 else "ok")
        rows.append(_row("每周新体验", standards.week_label("new_exp", "target"), standards.week_label("new_exp", "floor"),
                         f"{n} 次" if n is not None else "—", light,
                         "" if n is None or n >= ne.floor else "这周还没去" if elapsed >= 5 else "这周还有时间"))
        if places_log.has_data(db):
            n = places_log.non_work_outings(db, start, day, _training_leaves(db, start, day))
            light = "ok" if n >= ou.floor else ("warn" if elapsed >= 5 else "ok")
            rows.append(_row("出门做非工作的事", standards.week_label("outing", "target"), standards.week_label("outing", "floor"),
                             f"{n} 次", light,
                             "按手机定位：出去满 1 小时、不是去训练" if n else "这周还没有" if elapsed >= 5 else "这周还有时间"))
        else:
            rows.append(_row("出门做非工作的事", standards.week_label("outing", "target"), standards.week_label("outing", "floor"),
                             "—", "none", "等手机定位"))
    elif key == "career":
        if worktime.has_data(db):
            w = worktime.week_row(db, day)
            rows.append(_row("本人工作时长", "—", "每天不超过 8 小时", f"今天 {w['today']:g} 小时 · 本周最长 {w['max']:g} · 日均 {w['avg']:g}",
                             w["light"], "超过 8 小时黄、超过 10 小时红；目标一周比一周少"))
            try:  # 工作狂指数和效率（ethan/workaholic.py）：这周到今天 vs 上周同样几天
                from . import workaholic
                c = workaholic.compare(db, day)
                t, l = c["this"], c["last"]
                arrow = lambda a, b, lower=False: "" if b in (None, 0) or a is None else ("↓" if a < b else "↑" if a > b else "持平")
                b = workaholic.band(t["index"])  # 健康区间 20–49
                rows.append(_row("工作狂指数", "—", f"健康区间 {workaholic.HEALTHY[0]}–{workaholic.HEALTHY[1]}", f"本周 {t['index']}（{b['label']}，上周同期 {l['index']}）{arrow(t['index'], l['index'])}",
                                 b["light"],
                                 f"晚上 9 点后 {t['late']:g} 小时 · 周末 {t['weekend']:g} 小时"))
                e = workaholic.efficiency(db, day)  # 本周和最近 4 周平均，和周报同一个函数
                fmt = lambda v: "—" if v is None else f"{v:g}"
                rows.append(_row("效率 =（事业经验 + 学术经验）÷ 工作小时", "—", "时长往下、产出稳住、效率往上",
                                 f"本周 {fmt(e['week'])} · 最近 4 周平均 {fmt(e['avg4'])}",
                                 "none", f"本周产出 {e['week_points']} 经验（含里程碑和反复做的阶段任务）· 验收通过的交付 {t['output']['accepted']} 件 · 工作 {e['week_hours']:g} 小时"))
            except Exception as e:
                print(f"[territory] 工作狂指数没算出来：{type(e).__name__} {e}", flush=True)
        else:
            rows.append(_row("本人工作时长", "—", "每天不超过 8 小时", "—", "none", "等 Mac 上的记录脚本"))
        rows += [
                 _row("小红书 + Instagram 发帖", "每周各 2 条", "—", "—", "none", "等接账号")]
    elif key == "money":
        # 健康线按周看，花销也按本周算（本周回顾里写“每月花销”口径对不上）；保底是月预算按天数折成一周
        from . import finance
        import calendar
        if finance.has_source(db, "monzo") or finance.has_source(db, "sms") or finance.has_source(db, "chat"):
            fmt = lambda d: " + ".join(f"{'£' if c == 'GBP' else '¥' if c == 'CNY' else c + ' '}{v:,.0f}" for c, v in d.items()) or "0"
            week, month = finance.spend_between(db, start, day), finance.month_spend(db, day)
            budget = settings_budget(db, day)
            rate = finance.fx_rate(db)
            last_day = db.conn.execute("SELECT MAX(day) FROM transactions WHERE amount < 0").fetchone()[0]
            stale = f"最近一笔扣款是 {last_day[5:]}" if last_day and last_day < (day - timedelta(days=2)).isoformat() else ""
            if budget:
                cur, cap = next(iter(budget.items()))
                sym = "£" if cur == "GBP" else "¥"
                wcap = cap * 7 / calendar.monthrange(day.year, day.month)[1]
                wt, mt = finance.convert(week, cur, rate), finance.convert(month, cur, rate)
                light = "bad" if wt > wcap or mt > cap else "warn" if wt > wcap * elapsed / 7 else "ok"
                rows.append(_row("本周花销", "—", f"每周不超 {sym}{wcap:,.0f}", f"本周 {sym}{wt:,.0f}", light,
                                 "；".join(x for x in (f"本月累计 {sym}{mt:,.0f} / 预算 {sym}{cap:,.0f}", stale) if x), edit="budget"))
            else:
                rows.append(_row("本周花销", "—", "按每月预算折算", f"本周 {fmt(week)}", "ok",
                                 "；".join(x for x in (f"本月累计 {fmt(month)}", stale, "还没定每月预算：点保底旁边的 ✎ 填一下") if x), edit="budget"))
        else:
            rows.append(_row("本周花销", "—", "按每月预算折算", "—", "none", "等接 Monzo 和招行（docs/operations/finance.md）", edit="budget"))
    elif key == "social":
        if lifelog.has(db, "meet") or lifelog.has(db, "new_friend") or lifelog.has(db, "contact"):
            mt, nf = standards.get("meet"), standards.get("new_friend")  # 领域标准表（决定 8）
            met = lifelog.count(db, "meet", start, day)
            rows.append(_row("和朋友见面", standards.week_label("meet", "target"), standards.week_label("meet", "floor"), f"{met} 次",
                             "ok" if met >= mt.floor else ("warn" if elapsed >= 5 else "ok"),
                             "" if met >= mt.floor else "这周还没见" if elapsed >= 5 else "这周还有时间", log="meet"))
            month = day.replace(day=1)
            new = lifelog.count(db, "new_friend", month, day)
            rows.append(_row("认识新朋友", standards.week_label("new_friend", "target"), standards.week_label("new_friend", "floor"), f"本月 {new} 个",
                             "ok" if new >= nf.floor or day.day < 20 else ("warn" if new >= 1 else "bad"), log="new_friend"))
            from . import relations  # “该联系谁”只用人物卡那套（5）
            cs = relations.contact_status(db, day, "me_page")
            set_up, due = cs["tracked"], cs["contact"]
            if not set_up:  # 还没有人物卡（或卡上没有最近联系），不能说“都联系过”
                rows.append(_row("联系重要的人", "—", "按各自频率", "—", "none", "还没有人物卡：等微信统计交过来，或在“关系”页设好多久联系一次", log="contact"))
            else:
                rows.append(_row("联系重要的人", "—", "按各自频率", f"{len(due)} 个该联系了" if due else "都联系过",
                                 "ok" if not due else "warn" if len(due) <= 2 else "bad", "、".join(p["name"] for p in due[:4]), log="contact"))
        else:
            rows += [_row("和朋友见面", standards.week_label("meet", "target"), standards.week_label("meet", "floor"), "—", "none", "点＋记一笔，或者跟 小伴 说", log="meet"),
                     _row("认识新朋友", standards.week_label("new_friend", "target"), standards.week_label("new_friend", "floor"), "—", "none", "点＋记一笔，或者跟 小伴 说", log="new_friend"),
                     _row("联系重要的人", "—", "按各自频率", "—", "none", "点＋记一笔；人物卡里设好多久联系一次", log="contact")]
    elif key == "academic":
        rs = standards.get("research")
        label = standards.week_label("research", "floor")
        if lifelog.has(db, "research"):
            mins = lifelog.total(db, "research", start, day)
            rows.append(_row("研究时间", "—", label, f"{mins / 60:.1f} 小时",
                             "ok" if mins >= rs.floor else ("warn" if mins > 0 or elapsed < 6 else "bad")))
        else:
            rows.append(_row("研究时间", "—", label, "—", "none", "做了研究告诉 小伴，他会记下来"))
    return rows


def spend_curve(db, day: date) -> dict:
    """财务页的曲线：本周和上周每天累计花了多少、本月累计；按预算的币种（没有预算按英镑）折算。"""
    from . import finance
    import calendar
    budget = settings_budget(db, day)
    cur, cap = next(iter(budget.items())) if budget else ("GBP", None)
    rate = finance.fx_rate(db)
    monday = clock.week_start(day)

    def cum(start, upto):
        out, total = [], 0.0
        for n in range(7):
            d = start + timedelta(days=n)
            if d > upto:
                break
            total += finance.convert(finance.spend_between(db, d, d), cur, rate)
            out.append(round(total))
        return out
    return {"cur": "£" if cur == "GBP" else "¥", "this": cum(monday, day), "last": cum(monday - timedelta(days=7), day),
            "month": round(finance.convert(finance.month_spend(db, day), cur, rate)), "cap_month": cap,
            "cap_week": round(cap * 7 / calendar.monthrange(day.year, day.month)[1]) if cap else None}


BUDGET_SQL = "CREATE TABLE IF NOT EXISTS budget_plan (month TEXT PRIMARY KEY, amount REAL NOT NULL, set_at TEXT NOT NULL)"


def budget_plan(db, day: date = None) -> dict:
    """每月预算：只按人民币算，定了就要遵守——这个月的锁定，改的只从下个月起生效。
    返回 {"now": 本月预算或 None, "next": 下个月起的新预算或 None}。"""
    day = day or her_today()
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


def set_budget(db, amount: float, day: date = None, when: str = None) -> str:
    """when = "now" 本月就生效，"next" 下个月起；不传时按老规矩：还没定过就本月，定过就下个月（改的时候让她选）。返回生效的月份。"""
    day = day or her_today()
    plan = budget_plan(db, day)
    if when not in ("now", "next"):
        when = "now" if plan["now"] is None else "next"
    month = day.strftime("%Y-%m") if when == "now" else plan["next_month"]
    if when == "now":  # 本月改了，下个月起的那条（如果之前定过）也跟着改，免得下个月又跳回去
        db.conn.execute("DELETE FROM budget_plan WHERE month = ?", (plan["next_month"],))
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
    # 更新了探索地图：原来的“Goal”表改成“成就”表（成就 / 状态 / 成就内容），人生体验是其中“成就 = 人生体验”的几行
    goals = sheets.get("Goal") or [{"Goal": r.get("成就内容"), "状态": r.get("状态")} for r in sheets.get("成就", []) if r.get("成就") == "人生体验"]
    china = sum(1 for r in sheets.get("中国", []) if r.get("状态") == "已结束")
    return {"countries": len(countries), "china": china,
            "goals_done": sum(1 for g in goals if g.get("状态") == "已结束"),
            "goals_doing": sum(1 for g in goals if g.get("状态") == "进行中"), "goals_total": len(goals)}


def task_territory(t: dict):
    """待办归哪块领地：她选过的优先，其次所属副本的领地；都没有就在荒原。"""
    if t.get("domain"):
        return t["domain"] if t["domain"] in NAME else None
    return KEY_OF.get(t.get("goal_domain") or "")
