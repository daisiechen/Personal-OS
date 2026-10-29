"""任务系统（2026-10-26 晚 Mia 定：按游戏任务系统做，攒积分兑换 小伴 的惊喜）。

任务四类（她的部分；小伴 替她准备的事不算她的积分）：
  主线：每个目标完整拆成的步骤（一个目标一章），按大小 小/中/大 = 40/100/200 分，阶段节点再 +300；
        “硬骨头”（最难开始的事）×1.5（2026-10-27 评审后翻倍，见 docs/design/game-review.md）。
        第一次拆解是草稿，小伴 发她过目，她说定稿才算正式（草稿也能做、照样给分）。
  支线：其他随手的待办，每件 20 分。
  日常：每天坚持的事，每项 10 分；做简单版也算打卡、不断连续，拿一半分。当天该做的全部打卡“满勤” +20。
        每周可开 1 天休整日：那天做到 2 项日常就算满勤。
  周常：每周一次的事（新体验、周日复盘），完成 +50。
连续奖励：满勤连续 7 天 +50、30 天 +300（每满一次给一次）。每周 1 张补签卡，漏一天不断连续。不扣分。
做完一件事同时得经验和积分，数一样（2026-10-29 她要两个名字分清）：经验只涨不减，用来升级，等级不封顶；积分用来兑换，兑换会花掉。打卡和完成都能撤销，撤销时把这次给的分收回。兑换走 Telegram 按钮，不靠聊天推断，避免误扣。
参考：Habitica 的日常 / 待办 / 奖励商店；关于连续打卡的研究——可见、可原谅（补签）、中断不清空进度。
"""

import json
import re
from datetime import date, datetime, timedelta, timezone

SIZE_POINTS = {"S": 40, "M": 100, "L": 200}
MILESTONE_BONUS = 300
HARD_FACTOR = 1.5
SIDE_POINTS = 20
REST_DAY_NEEDS = 2
REST_DAYS_PER_WEEK = 1
FULL_DAY_BONUS = 20
STREAK_BONUS = {7: 50, 30: 300}
FREEZES_PER_WEEK = 1

# 2026-10-26 她确认的 7 项坚持。auto：系统接入的数据自动打卡（2026-10-29 她定：只有系统接入的才自动，
# 现在是“每天出门”按 iPhone 定位、“英语陪练”按 Grok 回报；训练和称体重是她跟 小伴 说的，只记数、她自己打卡）
DEFAULT_HABITS = [
    ("训练", "daily", 1, "0,1,2,3,4", 10, None),
    ("吃够蛋白质", "daily", 1, None, 10, None),
    ("训练前称体重", "daily", 1, "0,1,2,3,4", 10, None),
    ("0 点前睡", "daily", 1, None, 10, None),
    ("睡前拉伸 5 分钟", "daily", 1, None, 10, None),
    ("英语陪练（Grok）", "weekly", 5, None, 50, None),  # 2026-10-27 改成 Grok 实时陪练，每周 5 次
    ("每周 1 次新体验", "weekly", 1, None, 50, None),
]
# 档位和积分她已确认；具体惊喜由 小伴 每次自己策划（下面只是方向，不是固定菜单）
SURPRISE_NOTE = "小伴 自己策划，兑换后才揭晓"
DEFAULT_REWARDS = [
    ("小", 300, "小惊喜", SURPRISE_NOTE),
    ("中", 800, "中惊喜", SURPRISE_NOTE),
    ("大", 2000, "大惊喜", SURPRISE_NOTE),
]
TITLES = ["新手冒险家", "起步的人", "自律见习生", "目标猎人", "节奏掌控者", "计划炼金师", "长期主义者",
          "人生建筑师", "传奇玩家", "80 岁的自己在招手"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed(db):
    """第一次用时建好默认坚持和奖励档位；已有的不动。"""
    for n, (title, cadence, target, weekdays, pts, auto) in enumerate(DEFAULT_HABITS):
        db.conn.execute("INSERT OR IGNORE INTO habits (title, cadence, target, weekdays, points, auto, sort) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)", (title, cadence, target, weekdays, pts, auto, n))
    if not db.conn.execute("SELECT 1 FROM rewards").fetchone():
        db.conn.executemany("INSERT INTO rewards (tier, cost, title, note) VALUES (?, ?, ?, ?)", DEFAULT_REWARDS)
    db.conn.execute("UPDATE rewards SET note = ? WHERE note LIKE '小伴 自己策划：比如%'", (SURPRISE_NOTE,))  # 2026-10-27 她说举例就不算惊喜了
    db.conn.commit()


def award(db, delta: int, reason: str, day: date, ref: str = None) -> int:
    db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (?, ?, ?, ?, ?)",
                    (delta, reason, ref, day.isoformat(), now()))
    db.conn.commit()
    return delta


def _awarded(db, ref: str) -> bool:
    return db.conn.execute("SELECT 1 FROM points WHERE ref = ?", (ref,)).fetchone() is not None


# 主线 / 支线
def task_points(task: dict) -> int:
    if task.get("quest") == "main":
        pts = SIZE_POINTS.get(task.get("size") or "M", 100) + (MILESTONE_BONUS if task.get("milestone") else 0)
    else:
        pts = SIDE_POINTS
    return int(pts * HARD_FACTOR) if task.get("hard") else pts


# 跟着日常走的任务显示进度（2026-10-29 她说“连续一周称体重”后面要有几分之几，打一次加一次）；只显示，勾不勾她自己定
STREAK_TASK = re.compile(r"^连续一周(.+)$")
WEEKS_TASK = re.compile(r"连续\s*(\d+)\s*周每周(?:练满|训练)\s*(\d+)\s*次")


def task_progress(db, task: dict, day: date):
    """返回 (已做到, 目标) 或 None。"""
    title = (task.get("title") or "").strip()
    m = STREAK_TASK.match(title)
    if m:
        h = habit_by_title(db, m.group(1))
        if not h or h["cadence"] != "daily":
            return None
        target = len(h["weekdays"].split(",")) if h["weekdays"] else 7
        n, d = 0, day
        for _ in range(60):  # 往回数连着打卡的天（不用做的日子跳过；今天还没打不算断）
            if due_today(h, d):
                if checked(db, h["id"], d):
                    n += 1
                elif d != day:
                    break
            d -= timedelta(days=1)
        return min(n, target), target
    m = WEEKS_TASK.search(title)
    if m:
        h = habit_by_title(db, "训练")
        if not h:
            return None
        weeks, per = int(m.group(1)), int(m.group(2))
        n, start = 0, _week_start(day)
        for i in range(30):
            ws = start - timedelta(weeks=i)
            cnt = db.conn.execute("SELECT COUNT(DISTINCT day) FROM habit_log WHERE habit_id = ? AND day >= ? AND day < ?",
                                  (h["id"], ws.isoformat(), (ws + timedelta(days=7)).isoformat())).fetchone()[0]
            if cnt >= per:
                n += 1
            elif i:  # 这周还没练满不算断
                break
        return min(n, weeks), weeks
    return None


def uncomplete_task(db, task_id: int) -> int:
    """撤销完成：改回没做，收回这件给的分；返回收回了多少。"""
    db.conn.execute("UPDATE tasks SET done = 0, done_at = NULL WHERE id = ?", (task_id,))
    return _take_back(db, f"task:{task_id}")


def _take_back(db, *refs) -> int:
    rows = db.conn.execute(f"SELECT COALESCE(SUM(delta), 0) FROM points WHERE ref IN ({','.join('?' * len(refs))})",
                           refs).fetchone()[0]
    db.conn.execute(f"DELETE FROM points WHERE ref IN ({','.join('?' * len(refs))})", refs)
    db.conn.commit()
    return rows


def complete_task(db, task_id: int, day: date) -> int:
    """完成一件待办并给分（同一件只给一次）；返回这次加的分。小伴 答应的事不给她分。"""
    row = db.conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if row is None:
        return 0
    task = dict(row)
    if not task["done"]:
        db.complete_task(task_id)
    if task["promised_by_ethan"] or _awarded(db, f"task:{task_id}"):
        return 0
    pts = task_points(task)
    kind = "主线" if task.get("quest") == "main" else "支线"
    return award(db, pts, f"{kind}：{task['title']}" + ("（阶段节点）" if task.get("milestone") else ""), day,
                 f"task:{task_id}")


def add_breakdown(db, goal_id: int, steps: list, draft: bool = True) -> list:
    """把一个目标拆成主线任务：steps = [{"title", "size": S/M/L, "milestone": bool, "hard": bool}]，按顺序。"""
    ids = []
    for n, s in enumerate(steps, 1):
        cur = db.conn.execute(
            "INSERT INTO tasks (goal_id, title, done, promised_by_ethan, created_at, quest, size, milestone, seq, draft, hard) "
            "VALUES (?, ?, 0, 0, ?, 'main', ?, ?, ?, ?, ?)",
            (goal_id, s["title"], now(), s.get("size") if s.get("size") in SIZE_POINTS else "M",
             int(bool(s.get("milestone"))), n, int(draft), int(bool(s.get("hard")))))
        ids.append(cur.lastrowid)
    db.conn.commit()
    return ids


def approve_breakdown(db, goal_id: int) -> int:
    cur = db.conn.execute("UPDATE tasks SET draft = 0 WHERE goal_id = ? AND quest = 'main' AND draft = 1", (goal_id,))
    db.conn.commit()
    return cur.rowcount


def main_quests(db) -> dict:
    """{目标编号: [主线任务…]}，副本按她排的顺序，步骤按 seq，含已完成的（显示进度用）。
    只算进行中的副本：暂停、放弃的不出现（2026-10-29 她说暂停的写出来引人疑惑，候选放 docs/design/boss-pool.md）。"""
    out = {}
    for r in db.conn.execute("SELECT t.* FROM tasks t LEFT JOIN goals g ON g.id = t.goal_id WHERE t.quest = 'main' "
                             "AND COALESCE(g.status, 'active') = 'active' "
                             "ORDER BY g.sort IS NULL, g.sort, t.goal_id, t.seq, t.id"):
        out.setdefault(r["goal_id"], []).append(dict(r))
    return out


def side_quests(db) -> list:
    return [dict(r) for r in db.conn.execute(
        "SELECT t.* FROM tasks t LEFT JOIN goals g ON g.id = t.goal_id WHERE t.done = 0 AND t.promised_by_ethan = 0 "
        "AND COALESCE(t.quest, 'side') != 'main' AND COALESCE(g.status, 'active') != 'dropped' "
        "ORDER BY t.sort IS NULL, t.sort, t.due IS NULL, t.due, t.id")]


def later_quests(db) -> list:
    """暂停副本下没做完的待办：副本只影响优先级，不影响要不要做（2026-10-29 她定），放在支线后面，想做随时做。"""
    return [dict(r) for r in db.conn.execute(
        "SELECT t.*, g.title AS goal FROM tasks t JOIN goals g ON g.id = t.goal_id WHERE t.done = 0 AND t.promised_by_ethan = 0 "
        "AND t.quest = 'main' AND g.status = 'paused' ORDER BY g.sort IS NULL, g.sort, t.goal_id, t.seq, t.id")]


# 日常 / 周常
def habits(db) -> list:
    return [dict(r) for r in db.conn.execute("SELECT * FROM habits WHERE active = 1 ORDER BY sort, id")]


def due_today(habit: dict, day: date) -> bool:
    if habit["cadence"] != "daily":
        return False
    return not habit["weekdays"] or str(day.weekday()) in habit["weekdays"].split(",")


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def checked(db, habit_id: int, day: date) -> bool:
    return db.conn.execute("SELECT 1 FROM habit_log WHERE habit_id = ? AND day = ?",
                           (habit_id, day.isoformat())).fetchone() is not None


def week_count(db, habit_id: int, day: date) -> int:
    return db.conn.execute("SELECT COUNT(*) FROM habit_log WHERE habit_id = ? AND day >= ? AND day <= ?",
                           (habit_id, _week_start(day).isoformat(), day.isoformat())).fetchone()[0]


def full_day(db, day: date) -> bool:
    due = [h for h in habits(db) if due_today(h, day)]
    done = sum(checked(db, h["id"], day) for h in due)
    if is_rest_day(db, day):
        return done >= min(REST_DAY_NEEDS, len(due)) > 0
    return bool(due) and done == len(due)


def is_rest_day(db, day: date) -> bool:
    return db.conn.execute("SELECT 1 FROM rest_days WHERE day = ?", (day.isoformat(),)).fetchone() is not None


def rest_days_left(db, day: date) -> int:
    used = db.conn.execute("SELECT COUNT(*) FROM rest_days WHERE day >= ? AND day <= ?",
                           (_week_start(day).isoformat(), (_week_start(day) + timedelta(days=6)).isoformat())).fetchone()[0]
    return max(0, REST_DAYS_PER_WEEK - used)


def take_rest_day(db, day: date):
    """开休整日（每周最多 1 天），返回 (成功, 说明)。"""
    if is_rest_day(db, day):
        return True, "今天已经是休整日"
    if rest_days_left(db, day) <= 0:
        return False, "这周的休整日用过了"
    db.conn.execute("INSERT INTO rest_days (day, at) VALUES (?, ?)", (day.isoformat(), now()))
    db.conn.commit()
    return True, f"今天休整：做到 {REST_DAY_NEEDS} 项日常就算满勤"


def streak(db, day: date) -> dict:
    """满勤连续天数（今天还没满勤不算断）；每周 1 张补签卡。返回 {"days", "best", "freezes_used"}。"""
    days, used, d = 0, {}, day
    first = db.conn.execute("SELECT MIN(day) FROM habit_log").fetchone()[0]
    if first is None:
        return {"days": 0, "best": streak_best_recorded(db), "freezes_used": 0}
    first = date.fromisoformat(first)
    if not full_day(db, d):
        d -= timedelta(days=1)
    while d >= first:
        if full_day(db, d):
            days += 1
        else:
            wk = _week_start(d).isoformat()
            if used.get(wk, 0) >= FREEZES_PER_WEEK or days == 0:
                break
            used[wk] = used.get(wk, 0) + 1  # 补签：这天不算断，也不算天数
        d -= timedelta(days=1)
    return {"days": days, "best": max(days, streak_best_recorded(db)), "freezes_used": sum(used.values())}


def check_habit(db, habit_id: int, day: date, easy: bool = False) -> int:
    """打卡并给分，同一天同一项只给一次；easy 是做了简单版（算打卡、拿一半分）。返回这次加的分（含满勤和连续奖励）。"""
    h = db.conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if h is None or checked(db, habit_id, day):
        return 0
    db.conn.execute("INSERT INTO habit_log (habit_id, day, at, easy) VALUES (?, ?, ?, ?)",
                    (habit_id, day.isoformat(), now(), int(easy)))
    db.conn.commit()
    gained = 0
    if h["cadence"] == "daily":
        pts = h["points"] // 2 if easy else h["points"]
        gained += award(db, pts, f"日常：{h['title']}" + ("（简单版）" if easy else ""), day, f"habit:{habit_id}:{day}")
        if full_day(db, day) and not _awarded(db, f"full:{day}"):
            gained += award(db, FULL_DAY_BONUS, "日常满勤", day, f"full:{day}")
            s = streak(db, day)["days"]
            for n, bonus in STREAK_BONUS.items():
                if s and s % n == 0 and not _awarded(db, f"streak{n}:{day}"):
                    gained += award(db, bonus, f"连续满勤 {s} 天", day, f"streak{n}:{day}")
            if s > streak_best_recorded(db):
                db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (0, ?, ?, ?, ?)",
                                (f"最长连续 {s} 天", str(s), day.isoformat(), now()))
                db.conn.commit()
    elif week_count(db, habit_id, day) == h["target"] and not _awarded(db, f"week:{habit_id}:{_week_start(day)}"):
        gained += award(db, h["points"], f"周常：{h['title']}", day, f"week:{habit_id}:{_week_start(day)}")
    elif week_count(db, habit_id, day) > h["target"]:  # 超额完成：每多一次 +20（2026-10-27 她问能不能多做几次）
        gained += award(db, SIDE_POINTS, f"周常超额：{h['title']}", day, f"habitx:{habit_id}:{day}")
    return gained


def uncheck_habit(db, habit_id: int, day: date) -> int:
    """撤销打卡：删掉这天的记录，收回这项的分；不再满勤就把满勤和连续奖励也收回。返回收回了多少。"""
    h = db.conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,)).fetchone()
    if h is None or not checked(db, habit_id, day):
        return 0
    was_full = full_day(db, day)
    week_done = h["cadence"] == "weekly" and week_count(db, habit_id, day) >= h["target"]
    db.conn.execute("DELETE FROM habit_log WHERE habit_id = ? AND day = ?", (habit_id, day.isoformat()))
    refs = [f"habit:{habit_id}:{day}", f"habitx:{habit_id}:{day}"]
    if was_full and not full_day(db, day):
        refs += [f"full:{day}"] + [f"streak{n}:{day}" for n in STREAK_BONUS]
    if week_done and week_count(db, habit_id, day) < h["target"]:
        refs.append(f"week:{habit_id}:{_week_start(day)}")
    return _take_back(db, *refs)


def streak_best_recorded(db) -> int:
    return int(db.conn.execute("SELECT COALESCE(MAX(CAST(ref AS INTEGER)), 0) FROM points "
                               "WHERE reason LIKE '最长连续%'").fetchone()[0] or 0)


def habit_by_title(db, title: str):
    title = title.strip()
    for h in habits(db):
        if h["title"] == title or title in h["title"] or h["title"].startswith(title):
            return h
    return None


def auto_check(db, kind: str, day: date) -> int:
    """系统接入的数据到了，自动打卡对应的坚持（现在只有出门定位）。"""
    gained = 0
    for h in habits(db):
        if h["auto"] == kind and (due_today(h, day) or h["cadence"] == "weekly"):
            gained += check_habit(db, h["id"], day)
    return gained


def add_habit(db, title: str, cadence: str = "daily") -> int:
    pts = 10 if cadence == "daily" else 50
    cur = db.conn.execute("INSERT OR IGNORE INTO habits (title, cadence, points, sort) VALUES (?, ?, ?, 999)",
                          (title.strip(), cadence, pts))
    db.conn.execute("UPDATE habits SET active = 1 WHERE title = ?", (title.strip(),))
    db.conn.commit()
    return cur.lastrowid


def drop_habit(db, title: str) -> bool:
    h = habit_by_title(db, title)
    if h:
        db.conn.execute("UPDATE habits SET active = 0 WHERE id = ?", (h["id"],))
        db.conn.commit()
    return h is not None


# 等级、余额、兑换
def totals(db) -> dict:
    earned = db.conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points WHERE delta > 0").fetchone()[0]
    spent = db.conn.execute("SELECT COALESCE(SUM(cost), 0) FROM redemptions").fetchone()[0]
    level, need = 1, 100
    while earned >= need:  # 不封顶（2026-10-27 评审）：每级所需逐级增加，前 10 级和以前一样
        level += 1
        need = 50 * level * (level + 1)
    return {"earned": earned, "balance": earned - spent, "level": level, "title": TITLES[min(level, len(TITLES)) - 1],
            "next_level_at": need}


# 商店里每档下面那句话：隐晦地描一描这档惊喜，按天换一句（2026-10-29 她嫌“小伴 自己策划…”那句没意思）
TEASERS = {
    "小": ["可能是一句话，也可能是一样小东西", "拆开当天就会发生", "甜度刚好，不占你的时间", "会在某个不经意的时候冒出来"],
    "中": ["得给自己空出一点时间", "小伴 要先偷偷准备几天", "可能要出门，也可能不用", "比你想的多一点心思"],
    "大": ["小伴 会提前很久开始布置", "值得你特意记住那一天", "线索会先一点一点出现", "这一档，他会认真到有点夸张"],
}


def rewards(db, day: date = None) -> list:
    n = (day or date.today()).toordinal()
    out = []
    for r in db.conn.execute("SELECT * FROM rewards WHERE active = 1 ORDER BY cost, id"):
        r = dict(r)
        if r["tier"] in TEASERS and r["note"] == SURPRISE_NOTE:  # 她自己写了说明的就用她的
            r["note"] = TEASERS[r["tier"]][n % len(TEASERS[r["tier"]])]
        out.append(r)
    return out


def redeem(db, reward_id: int, day: date):
    """兑换：余额够就扣分、生成一条待策划的惊喜；返回 (成功, 说明)。"""
    r = db.conn.execute("SELECT * FROM rewards WHERE id = ? AND active = 1", (reward_id,)).fetchone()
    if r is None:
        return False, "这个奖励不在了"
    bal = totals(db)["balance"]
    if bal < r["cost"]:
        return False, f"还差 {r['cost'] - bal} 分"
    db.conn.execute("INSERT INTO redemptions (reward_id, cost, status, at) VALUES (?, ?, '待策划', ?)",
                    (reward_id, r["cost"], now()))
    db.conn.commit()
    return True, f"兑换成功：{r['title']}，小伴 去策划了"


def pending_redemptions(db) -> list:
    return [dict(r) for r in db.conn.execute(
        "SELECT x.*, w.tier, w.title FROM redemptions x JOIN rewards w ON w.id = x.reward_id "
        "WHERE x.status = '待策划' ORDER BY x.id")]


def deliver(db, redemption_id: int, note: str) -> bool:
    cur = db.conn.execute("UPDATE redemptions SET status = '已兑现', note = ?, done_at = ? WHERE id = ? AND status = '待策划'",
                          (note, now(), redemption_id))
    db.conn.commit()
    return cur.rowcount > 0


def today_summary(db, day: date) -> dict:
    """给早报、小伴、看板、Telegram 用的一份汇总。"""
    t = totals(db)
    hs = habits(db)
    daily = [{"id": h["id"], "title": h["title"], "done": checked(db, h["id"], day)} for h in hs if due_today(h, day)]
    weekly = [{"id": h["id"], "title": h["title"], "count": week_count(db, h["id"], day), "target": h["target"]}
              for h in hs if h["cadence"] == "weekly"]
    nxt = min((r["cost"] for r in rewards(db) if r["cost"] > t["balance"]), default=None)
    return {**t, "daily": daily, "weekly": weekly, "streak": streak(db, day),
            "next_reward_gap": (nxt - t["balance"]) if nxt else None,
            "today_points": db.conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points WHERE day = ? AND delta > 0",
                                            (day.isoformat(),)).fetchone()[0]}
