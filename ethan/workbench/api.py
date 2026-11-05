"""工作台的数据和操作。页面每次刷新拿一份完整的 state；她的每个操作走 act()，改完立即生效，能撤销。

数字都由代码从数据库算（和 Telegram 今日卡片同一套：quests、year_progress、计划的时间线），不经过模型。
她在工作台上改了什么记进 <数据目录>/workbench-log.jsonl，小伴 下一轮聊天就知道（见 recent_changes）。
"""

import json
import threading
import os
import re
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import helpscope
from .. import tx
from .. import achievements, autodone, checkup, finance, jobs, agents, brain, brief_card, cards, meals, places_log, prep, quests, review, sysmetrics, territory, training, worktime
from ..clock import her_now, DEFAULT_TZ
from ..planner import goal_card
from ..progress import year_progress

MAIN_BOSS = "减脂"  # 本赛季主副本（2026-10-27 她定），今日任务优先排它
from ..season import SEASON  # 赛季定义放在 ethan/season.py
from .. import season as season_mod
LOG = "workbench-log.jsonl"
SUN = {1: (8.0, 16.3), 2: (7.4, 17.3), 3: (6.4, 18.2), 4: (6.2, 19.9), 5: (5.2, 20.7), 6: (4.8, 21.3),
       7: (5.1, 21.1), 8: (5.9, 20.3), 9: (6.8, 19.3), 10: (7.3, 17.6), 11: (7.3, 16.2), 12: (8.0, 15.9)}  # 伦敦大致日出日落
WEEKDAYS = "一二三四五六日"


def is_night(now: datetime) -> bool:
    rise, sset = SUN[now.month]
    h = now.hour + now.minute / 60
    return not (rise <= h < sset)


def recent_changes(root: Path, hours: int = 24, now: datetime = None) -> list:
    """她最近在工作台上改了什么（给 小伴 的上下文用）。"""
    path = Path(root) / LOG
    if not path.exists():
        return []
    now = now or datetime.now(timezone.utc)
    cutoff = (now if now.tzinfo else now.replace(tzinfo=timezone.utc)) - timedelta(hours=hours)
    rows = []
    for line in path.read_text(encoding="utf-8").split("\n"):
        try:
            row = json.loads(line)
            if datetime.fromisoformat(row["at"]) >= cutoff:
                rows.append(row)
        except (ValueError, KeyError, TypeError):
            continue
    return rows[-20:]


TODAY_ORDER = "CREATE TABLE IF NOT EXISTS today_order (day TEXT NOT NULL, task_id INTEGER NOT NULL, pos INTEGER NOT NULL, PRIMARY KEY (day, task_id))"


def _starred(t: dict, d: str) -> bool:
    """点了 ★、到今天还算：从点星那天起一直有效，直到做完或取消（她 2026-11-02）。"""
    return bool(t.get("star_day")) and t["star_day"] <= d


SOON_DAYS = 2  # 截止在这么多天以内的待办进今天（11-04 她定 2 天，原来 5 天）

class _OutsideLock:
    """包一层模型：调 complete 时先放开工作台的大锁，等模型回答时别的页面照常；答完再拿回锁接着写结果
    （Codex 评审 11-05：一个慢请求堵住所有页面）。同一个请求里几个一起问（问一问两个并行）只放一次、都回来才拿回。"""

    def __init__(self, router, lock, db):
        self._router, self._lock, self._db = router, lock, db
        self._n, self._mu = 0, threading.Lock()

    def __getattr__(self, name):
        return getattr(self._router, name)

    def complete(self, *a, **k):
        with self._mu:
            if self._n == 0:
                if self._db.conn.in_transaction:  # 锁内写了一半的先提交，别被别的请求带着提交或回滚
                    self._db.conn.commit()
                self._lock.release()
            self._n += 1
        try:
            return self._router.complete(*a, **k)
        finally:
            with self._mu:
                self._n -= 1
                if self._n == 0:
                    self._lock.acquire()


class Workbench:
    def __init__(self, store, settings, calendar=None, places=None, background=False, card_runner=None, replan_runner=None):
        """background：苹果日历放到后台线程同步（服务器上开，测试里关）；card_runner()：现画今日卡，返回是否开始画了。"""
        self.store, self.db, self.settings = store, store.db, settings
        self.calendar, self.places = calendar, places or {}
        self.card_runner, self.replan_runner = card_runner, replan_runner
        self.redo_runner = None  # 重画今天的卡（server.build 里接上）
        self.router = None  # “看中文”翻译用（server.build 里接上）
        self.lock = None  # 服务器处理请求的那把大锁（server.make_handler 里接上）：等模型时放开，见 _model()
        self.diary_kick = None  # 后台补写日记（server.build 里接上）
        self.prep_kick = prep.kick if background else (lambda root, task_id: False)
        self.services_check = background  # 看 systemd 服务在不在跑（服务器上开，测试里关）  # 她提意见后马上重做（服务器上开，测试里关）
        self._sync_pool = ThreadPoolExecutor(max_workers=1) if background and calendar is not None else None
        self._synced_uids = []  # 后台同步写回的 (日程 id, uid)，下次请求时在主线程存进数据库

    def _model(self):
        """要调模型的操作用这个，不直接用 self.router：在服务器里等模型时放开大锁。
        放开期间数据可能被别的请求改了，写结果前要重读、核对（diary.fix / weave、desk.translate 里）。"""
        if self.router is None or self.lock is None:
            return self.router
        return _OutsideLock(self.router, self.lock, self.db)

    # ---------- 读 ----------
    def now(self) -> datetime:
        return her_now(self.settings)

    def state(self, now: datetime = None) -> dict:
        now = now or self.now()
        day = now.date()
        db = self.db
        self._save_synced()
        quests.seed(db)
        plan = db.latest_plan(day.isoformat())
        brief = json.loads(plan["brief"]) if plan else {}
        g = quests.today_summary(db, day)
        nxt = min((r for r in quests.rewards(db) if r["cost"] > g["balance"]), key=lambda r: r["cost"], default=None)
        start = self.settings.get("cold_start_from")
        bosses = self.bosses(day)
        cur_season = season_mod.current(self.settings)[0]
        season_end = date.fromisoformat(cur_season["end"])
        fracs = [b["frac"] for b in bosses if b["frac"] is not None]
        main = next((b for b in bosses if b["main"]), None)
        try:  # 数据源接上了，对应的待办自动勾掉（ethan/autodone.py）
            autodone.check(db, self.store.root, day)
        except Exception as e:
            print(f"[workbench] 自动勾待办没跑成：{type(e).__name__} {e}", flush=True)
        card = self.card(day)
        try:  # 刚达成的成就，不管她在哪个页面都弹出来
            celebrate = achievements.celebrate(db, self.store.root, day)
        except Exception as e:
            print(f"[workbench] 成就弹窗没算上：{type(e).__name__} {e}", flush=True)
            celebrate = []
        return {
            "celebrate": celebrate, "badges": self.badges(day),
            "day": day.isoformat(), "weekday": WEEKDAYS[day.weekday()], "time": now.strftime("%H:%M"),
            "night": is_night(now), "weather": card.get("weather") if card else "",
            "work": self._work(now),
            "profile": {
                "level": g["level"], "title": g["title"], "earned": g["earned"], "balance": g["balance"],
                "next_level_at": g["next_level_at"], "level_at": g["next_level_at"] - quests.LEVEL_STEP * g["level"],
                "streak": g["streak"]["days"],
                "day_n": (day - date.fromisoformat(start)).days + 1 if start else None,
                "next_reward": {"title": nxt["title"], "cost": nxt["cost"], "gap": nxt["cost"] - g["balance"]} if nxt else None,
            },
            "season": {**cur_season, "days_left": max(0, (season_end - day).days),
                       # 赛季按时间走了几成（她 11-01：不要副本平均；主副本的落后和下面副本进度重复，不显示）
                       "pct": self._season_pct(day, cur_season), "boss_pct": round(100 * sum(fracs) / len(fracs)) if fracs else 0},
            "timeline": self._live_timeline(day, plan, brief),
            "plan_ready": plan is not None,
            "detail": {k: brief_card.detail(plan, k) for k in ("train", "meals")} if plan else {},
            "meals": meals.parse_text(brief.get("meals_text")),
            "training": self.training_today(day, plan),
            **self.today_tasks(day, plan),
            "habits": {"daily": [{**h, "easy": self._easy(h["id"], day),
                                  "yesterday": quests.checked(self.db, h["id"], day - timedelta(days=1))} for h in g["daily"]],
                       "weekly": self._weekly(g, day), "rest_day": quests.is_rest_day(db, day),
                       "rest_left": quests.rest_days_left(db, day)},
            "bosses": bosses,
            "prep_trash": self.prep_trash(),
            "prep_today": prep.today_order(db),
            # 先放着的：每天第一次打开工作台提醒她看一眼（2026-10-30 她要）
            "parked_remind": self.parked_remind(day),
            "prep_routines": [r["title"] for r in db.conn.execute("SELECT title FROM tasks WHERE done = 0 AND promised_by_ethan = 1 ORDER BY id")
                              if prep.ROUTINE.search(r["title"]) and not prep.is_ops(r["title"])],
            "prep": [{k: x.get(k) for k in ("id", "title", "goal", "season", "state", "state_label", "summary", "decision", "star")}
                     | {"result": (prep.detail(db, x["id"]) or {}).get("result"), "help_for": helpscope.for_task(db, x["id"])}
                     for x in prep.queue(db) if not x["secret"]],  # 惊喜不给她看；help_for：替她哪件事提前准备的（2026-11-02）
            "card": card,
            "card_making": bool(self.card_runner and getattr(self.card_runner, "busy", lambda: False)()),
            "planner": self.plan_panel(day, plan),
            "goals": self.goal_options(),
        }

    def parked_remind(self, day: date) -> int:
        """首页“小伴 今天在准备”上的小红点：先放着的有几件、今天还没点开“先放着”看过（2026-10-30 她要）。
        点进去“先放着”那栏上有同样的红点和数（她 11-05：“我每天从小伴帮我办进去都不知道红点说的是哪儿”），看过就消失。"""
        if self.settings.get("parked_seen") == day.isoformat():
            return 0
        return sum(1 for x in prep.queue(self.db) if x["state"] == "pending" and not x["secret"])

    def _weekly(self, g: dict, day: date) -> list:
        """周常：达标后还能接着做（超额每次 +20），today 是今天做过没有（点了就撤销今天这次）。"""
        return [{**h, "today": quests.checked(self.db, h["id"], day)} for h in g["weekly"]]

    def _easy(self, habit_id: int, day: date) -> bool:
        r = self.db.conn.execute("SELECT easy FROM habit_log WHERE habit_id = ? AND day = ?",
                                 (habit_id, day.isoformat())).fetchone()
        return bool(r and r["easy"])

    def bosses(self, day: date) -> list:
        mains = quests.main_quests(self.db, with_agent_goals=True)
        agent_goals = {r[0] for r in self.db.conn.execute("SELECT id FROM goals WHERE handled_by IS NOT NULL")}
        plan = self.db.latest_plan(day.isoformat())
        review = (json.loads(plan["brief"]).get("review") or {}) if plan else {}
        metrics = {r["goal_id"]: dict(r) for r in self.db.conn.execute("SELECT goal_id, target, unit FROM goal_metrics")}
        dues = {r["id"]: r["due"] for r in self.db.conn.execute("SELECT id, due FROM goals")}
        titles = {r["id"]: r["title"] for r in self.db.conn.execute("SELECT id, title FROM goals")}
        out = []
        for p in year_progress(self.db, day):
            if p["status"] == "暂停":
                continue
            c = goal_card(p)
            steps = mains.get(p["goal_id"], [])
            frac = c["frac"]
            if frac is None and steps and p["goal_id"] not in agent_goals:  # 交给 agent 的副本她名下只剩做完的几步，算出来会是 100%
                frac = round(sum(s["done"] for s in steps) / len(steps), 2)
            nxt = next((s for s in steps if not s["done"]), None)
            nxt_title = nxt["title"] if nxt else None
            from .. import stages
            if stages.has_structure(self.db, p["goal_id"]):  # 新结构：进度按过了几个里程碑，下一步写当前里程碑
                gp = stages.goal_progress(self.db, p["goal_id"])
                if frac is None or p["goal_id"] in agent_goals:
                    frac = gp["frac"]
                nxt_title = (f"{gp['current']['title']}" + (f"（{gp['current']['due'][5:]}）" if gp["current"]["due"] else "")) if gp["current"] else None
            out.append({"goal_id": p["goal_id"], "name": c["name"], "line": c["line"], "status": c["status"],
                        "steps_done": sum(1 for s in steps if s["done"]), "steps_total": len(steps),
                        "frac": frac, "main": MAIN_BOSS in c["name"], "next": nxt_title,
                        "comment": review.get(str(p["goal_id"])), "due": dues.get(p["goal_id"]), "title": titles.get(p["goal_id"]),
                        "target": (metrics.get(p["goal_id"]) or {}).get("target"), "unit": (metrics.get(p["goal_id"]) or {}).get("unit")})
        return sorted(out, key=lambda b: not b["main"])

    @staticmethod
    def _season_pct(day: date, cur: dict = None) -> int:
        cur = cur or SEASON
        a, b = date.fromisoformat(cur["start"]), date.fromisoformat(cur["end"])
        return max(0, min(100, round(100 * ((day - a).days + 1) / ((b - a).days + 1))))

    def goal_options(self) -> list:
        """能挂待办的副本：进行中的年目标都算，还没拆主线的也要能加第一步（2026-11-01 她在 DemoApp 里加步骤找不到它）。"""
        rows = self.db.conn.execute("SELECT id, title FROM goals WHERE level = 'year' AND status = 'active' "
                                    "ORDER BY sort IS NULL, sort, due IS NULL, due, id").fetchall()
        ids = [r["id"] for r in rows] + [g for g in quests.main_quests(self.db) if g not in {r["id"] for r in rows}]
        goals = {r["id"]: r["title"] for r in self.db.conn.execute("SELECT id, title FROM goals")}
        return [{"id": gid, "name": goals.get(gid, "").split("：")[0]} for gid in ids]

    def today_tasks(self, day: date, plan) -> dict:
        """今日任务（不限件数，2026-10-27 她定）：没做完的在前（★ 的、主副本减脂的主线、别的主线、支线），做完的排最后。"""
        db, d = self.db, day.isoformat()
        # 快到期的和邮件、留言里来的待处理事项也进今天（她 2026-11-02：五天内回复芬兰使馆、申请在学证明怎么没进今日任务）；
        # 提前量 11-04 从 5 天改成 2 天（她选 B：“今天的计划，11.8 和 11.9 的怎么又排进来了？”）
        soon = (day + timedelta(days=SOON_DAYS)).isoformat()
        src = "source_type" in {r[1] for r in db.conn.execute("PRAGMA table_info(tasks)")}
        due = {r["id"] for r in db.conn.execute(
            "SELECT id FROM tasks WHERE done = 0 AND ((due IS NOT NULL AND due <= ?)"
            + (" OR source_type IN ('email', 'visitor'))" if src else ")"), (soon,))}
        # 计划里列着所有没做完的事；算“今天”的只有：每个副本的下一步、截止到今天的、计划器今天新建的（如冷启动），加上她标 ★ 的
        ids = [i["task_id"] for i in (plan or {}).get("items", [])
               if i["kind"] == "todo" and i.get("task_id") and i.get("priority") != "ethan"
               and ((i.get("priority") == "main" and i.get("how") == "下一步") or i.get("task_created") or i["task_id"] in due)]
        ids += sorted(due)  # 计划排好以后才进来的（比如刚从邮件确认的）也算
        # 她点了 ★ 的：没做完就一直留着（星星也留着），直到做完或她取消星星（她 2026-11-02）；star_day 记的是从哪天起点的星
        ids += [r["id"] for r in db.conn.execute("SELECT id FROM tasks WHERE star_day IS NOT NULL AND star_day <= ? "
                                                 "AND (done = 0 OR SUBSTR(COALESCE(done_at, ''), 1, 10) >= ?)", (d, d))]
        rows = {r["id"]: dict(r) for r in db.conn.execute(
            f"SELECT t.*, g.title AS goal FROM tasks t LEFT JOIN goals g ON g.id = t.goal_id "
            f"WHERE t.id IN ({','.join('?' * len(ids)) or 'NULL'}) AND t.promised_by_ethan = 0 "
            "AND (t.goal_id IS NULL OR COALESCE(g.status, 'active') = 'active' OR (t.star_day IS NOT NULL AND t.star_day <= ?))", (*ids, d))}  # 暂停副本的步骤不自动进今天，她点了 ★ 照样进
        done_today = {r[0] for r in db.conn.execute("SELECT ref FROM points WHERE day = ? AND ref LIKE 'task:%'", (d,))}
        from .. import stages
        for t in rows.values():
            if t.get("milestone_id"):  # 阶段任务：今天做没做（反复做的不会整件勾掉）
                t["done"] = int(stages.done_on(db, t, day))
                t["hide"] = not t["done"] and not stages.due_today(db, t, day)  # 早上排的计划里有、但按现在的规矩今天不该做的（比如还没到时候的复盘）
                t["stage_text"] = stages.progress(db, t, day)["text"]
        tasks = [rows[i] for i in dict.fromkeys(ids) if i in rows and rows[i].get("hide_day") != d and not rows[i].get("hide")
                 and not (rows[i].get("start_day") and rows[i]["start_day"] > d and not rows[i].get("done"))  # 还没到开始那天（她 2026-11-02）
                 and not rows[i].get("habit_id")  # 引用日常的步骤在日常里打卡；改之前排的计划里也不再显示（她 2026-11-01）
                 and (not rows[i]["done"] or rows[i].get("milestone_id") or f"task:{i}" in done_today or (rows[i]["done_at"] or "")[:10] >= d)]

        db.conn.execute(TODAY_ORDER)
        pos = {r[0]: r[1] for r in db.conn.execute("SELECT task_id, pos FROM today_order WHERE day = ?", (d,))}

        def rank(t):  # 她拖过的按她排的（2026-10-29 她要今日任务也能拖），没拖过的跟在后面按原来的规矩
            return (bool(t["done"]), t["id"] not in pos, pos.get(t["id"], 0), not _starred(t, d),
                    not (t.get("quest") == "main" and MAIN_BOSS in (t.get("goal") or "")), t.get("quest") != "main")
        tasks.sort(key=rank)
        return {"today": [self._task(t, d) for t in tasks]}

    def _task(self, t: dict, d: str) -> dict:
        main = t.get("quest") == "main"
        tag = ("★ " if main and MAIN_BOSS in (t.get("goal") or "") else "") + ((t.get("goal") or "").split("：")[0] if main else "其他")
        from .. import stages
        pts = stages.points_for(t) if t.get("milestone_id") else quests.task_points(t)
        return {"id": t["id"], "title": t["title"], "done": bool(t["done"]), "points": pts, "stage": t.get("stage_text"),
                "tag": tag, "main": main, "hard": bool(t.get("hard")), "star": _starred(t, d),
                "progress": None if t["done"] else quests.task_progress(self.db, t, date.fromisoformat(d))}

    def _apple_busy(self, day: date) -> list:
        """她苹果日历当天的安排（不含 小伴 计划日历）。读不到返回 None。
        读一次 iCloud 要 7–10 秒（服务器 2026-11-02 实测，切页面因此卡）：服务器上后台每 4 分钟刷一次（warm_busy），
        请求里只用缓存；缓存过了 5 分钟也先用旧的，同时后台去刷。当天还没读过才当场等。测试里（background 关）照旧当场读。"""
        if self.calendar is None or not hasattr(self.calendar, "busy"):
            return None
        d = day.isoformat()
        cache = getattr(self, "_busy_cache", None)
        if cache and cache[0] == d:
            if (datetime.now() - cache[1]).total_seconds() <= 300:
                return cache[2]
            if self._sync_pool is not None:
                self._refresh_busy_later(day)
                return cache[2]
        try:
            return self._refresh_busy(day)
        except Exception as e:
            print(f"[workbench] 读日历没成：{type(e).__name__}", flush=True)
            return None

    def _refresh_busy(self, day: date) -> list:
        busy = self.calendar.busy(day)
        self._busy_cache = (day.isoformat(), datetime.now(), busy)
        return busy

    def _refresh_busy_later(self, day: date):
        if getattr(self, "_busy_refreshing", False):
            return
        self._busy_refreshing = True

        def run():
            try:
                self._refresh_busy(day)
            except Exception as e:
                print(f"[workbench] 后台读日历没成：{type(e).__name__}", flush=True)
            finally:
                self._busy_refreshing = False
        threading.Thread(target=run, daemon=True).start()

    def warm_busy(self, every: int = 240):
        """服务器上开：后台一直把今天的日历读好，她打开工作台时不用等 iCloud。"""
        import time

        def loop():
            while True:
                try:
                    self._refresh_busy(self.now().date())
                except Exception as e:
                    print(f"[workbench] 后台读日历没成：{type(e).__name__}", flush=True)
                time.sleep(every)
        if self.calendar is not None and hasattr(self.calendar, "busy"):
            threading.Thread(target=loop, daemon=True).start()

    def _live_timeline(self, day: date, plan, brief: dict) -> list:
        """首页时间线：把今天要去的地方串成一条路线现算（ethan/route.py，她 2026-11-02：两次出门、到家比训练结束还早，乱七八糟）。
        她日历上的、邮件 / 聊天记下的约、计划里的训练一起排；删了的约马上去掉，改了的时间马上生效。同样的输入 10 分钟内用上次算的。"""
        if not plan:
            return brief.get("timeline", [])
        from .. import events as fev, route
        from ..travel import Travel, places
        fev.ensure(self.db)
        apple = self._apple_busy(day)
        if apple is None:  # 读不到她的日历：她日历上的照早上的算
            try:
                facts = json.loads(plan.get("facts") or "{}")
            except ValueError:
                facts = {}
            ethan = {r["title"] for r in self.db.conn.execute("SELECT title FROM future_events WHERE day = ?", (day.isoformat(),))}
            self.db.conn.execute("CREATE TABLE IF NOT EXISTS deleted_future (day TEXT, title TEXT)")
            gone = {r[0] for r in self.db.conn.execute("SELECT title FROM deleted_future WHERE day = ?", (day.isoformat(),))}
            apple = [(b["start"], b["end"], b["title"], {"location": b.get("place") or ""}) for b in facts.get("busy") or []
                     if isinstance(b, dict) and b.get("title") not in ethan and b.get("title") not in gone]
        from .. import weekreview  # 周日 21:00 的复盘（只按周日算，不在这里读日历）
        busy = list(apple) + [(r[0], r[1], r[2], {"location": (r[3] or {}).get("location", "")}) for r in fev.for_day(self.db, day)] \
            + weekreview.busy_for(day)
        items = [dict(i) for i in plan["items"] if i["kind"] == "event"]
        key = json.dumps([day.isoformat(), [b[:3] + ((b[3] or {}).get("location", ""),) for b in busy],
                          [(i["start"], i.get("end"), i["title"], i.get("place")) for i in items]], ensure_ascii=False, default=str)
        memo = getattr(self, "_route_memo", None)
        if memo and memo[0] == key and (datetime.now() - memo[1]).total_seconds() < 600:
            return memo[2]
        try:
            out = route.timeline(busy, items, places(self.settings), Travel(self.store.root, self.settings), day)["rows"]
        except Exception as e:
            print(f"[workbench] 路线没算出来：{type(e).__name__} {e}", flush=True)
            out = route.timeline(busy, items, places(self.settings))["rows"]
        self._route_memo = (key, datetime.now(), out)
        return out

    def plan_panel(self, day: date, plan) -> dict:
        d = day.isoformat()
        events = []
        from ..planner import OVERRIDES  # 她今天亲手改过时间的：显示“撤销改动”（她 2026-11-03 改名；docs/design/system-rules.md 第 1 节）
        self.db.conn.execute(OVERRIDES)
        mine = {r[0] for r in self.db.conn.execute("SELECT title FROM plan_overrides WHERE day = ? AND action = 'time'", (d,))}
        for i in (plan or {}).get("items", []):
            if i["kind"] != "event" or not i.get("start"):
                continue
            leave = None
            if i.get("travel_min"):
                h, m = map(int, i["start"].split(":"))
                t = h * 60 + m - i["travel_min"]
                leave = f"{t // 60:02d}:{t % 60:02d}"
            events.append({"id": i["id"], "start": i["start"], "end": i.get("end"), "title": i["title"],
                           "place": i.get("place") or "", "leave": leave, "mine": i["title"] in mine})
        # 日程和苹果日历对得上（她 2026-11-02：两家饭店的约在这里看不到、删不掉）：
        # 邮件 / 聊天里记下的约（小伴 日历里那份）能在这里删；她自己日历上的照列出来，标“在你的日历里”，要删在苹果日历里删
        from .. import events as fev
        fev.ensure(self.db)
        for r in self.db.conn.execute("SELECT * FROM future_events WHERE day = ? ORDER BY start", (d,)):
            events.append({"fid": r["id"], "start": r["start"], "end": r["end"], "title": r["title"], "place": r["place"] or "",
                           "source": "小伴 记的" + ("（来自邮件）" if (r["note"] or "").startswith("来自邮件") else ""),
                           "clash": r["conflict"]})
        for b in self._apple_busy(day) or []:
            info = (b[3] or {}) if len(b) > 3 else {}
            events.append({"own": True, "start": b[0], "end": b[1], "title": b[2], "place": info.get("location", ""),
                           "href": None if info.get("recurring") else info.get("href"), "recurring": bool(info.get("recurring")),
                           "source": "在你的苹果日历里" + ("（重复的约，去日历 App 里删这一次）" if info.get("recurring") else "")})
        events.sort(key=lambda e: e["start"])
        for e in events:  # 撞时间的标出来
            e["clash_with"] = [x["title"] for x in events if x is not e and x["start"] < (e.get("end") or e["start"]) and (x.get("end") or x["start"]) > e["start"]]
        today_ids = {t["id"] for t in self.today_tasks(day, plan)["today"]}
        todos = [dict(r) for r in self.db.conn.execute(
            "SELECT t.id, t.title, t.due, t.quest, t.star_day, t.domain, g.title AS goal, g.domain AS goal_domain FROM tasks t "
            "LEFT JOIN goals g ON g.id = t.goal_id WHERE t.done = 0 AND t.promised_by_ethan = 0 "
            "AND (t.id IN (%s) OR (COALESCE(t.quest, 'side') != 'main' AND (t.due IS NULL OR t.due <= ?))) "  # 今天任务里有的一律列上，不看截止日（她 2026-11-02：首页三条、编辑页两条）
            "ORDER BY t.sort IS NULL, t.sort, t.id" % (",".join(map(str, today_ids)) or "NULL"), (d,))]
        for t in todos:
            t["star"] = t["id"] in today_ids
            t["sub"] = (t.get("goal") or "").split("：")[0] if t.get("quest") == "main" else "其他"
            t["terr"] = territory.task_territory(t) or territory.WASTE  # 归哪块领域，这里也能改（她 11-05）
        skipped = (json.loads(plan["brief"]).get("skipped") or []) if plan else []
        # 修好之前排的计划里也可能有暂停副本的事，显示时再滤一遍（她 2026-11-01：“那怎么现在系统里还有？”）
        from ..planner import PAUSED_WHY
        paused = {r["title"] for r in self.db.conn.execute(
            "SELECT t.title FROM tasks t JOIN goals g ON g.id = t.goal_id WHERE g.status = 'paused'")}
        skipped = [x for x in skipped if x.get("title") not in paused
                   and not PAUSED_WHY.search(f"{x.get('title')} {x.get('why') or ''}")]
        return {"events": events, "todos": todos, "skipped": skipped, "replanning": bool(self.replan_runner and self.replan_runner.busy())}

    def card(self, day: date):
        path = Path(self.store.root) / "cards" / f"{day.isoformat()}.json"
        try:
            c = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        if c.get("code") == "S1":  # 训练日那句按今天计划里实际练的写（之前写死“练腿”）
            plan = self.db.latest_plan(day.isoformat())
            train = next((i for i in (plan or {}).get("items", []) if i["kind"] == "event" and i["title"].startswith("训练")), None)
            c["line"] = cards.line_for("S1", train["title"] if train else "")
        return {k: c.get(k) for k in ("day", "title", "rarity", "reason", "line", "weather")} | {
            "image": f"/img/card/{c['day']}" if c.get("image") else None}

    def training_today(self, day: date, plan):
        """训练详情排成卡片用：今天的动作处方（和日历里同一套代码算的）+ 计划里训练那一项的时间和路上安排。"""
        item = next((i for i in (plan or {}).get("items", []) if i["kind"] == "event" and i["title"].startswith("训练")), None)
        rx = training.prescribe(self.db, day) if item else None
        if not rx:
            return None
        lib, _ = training.load()
        trip = (item.get("detail") or "").split("\n", 1)[0]
        return {"title": item["title"], "trip": trip if "出发" in trip else "", "focus": rx["focus"], "warmup": rx["warmup"],
                "exercises": [{**e, "muscles": lib.get(e["id"], {}).get("muscles", [])} for e in rx["exercises"]]}

    # ---------- 领地 ----------
    def _territory_parts(self, day: date):
        goal_dom = {r["id"]: territory.KEY_OF.get(r["domain"] or "") for r in self.db.conn.execute("SELECT id, domain FROM goals")}
        bosses = self.bosses(day)
        for b in bosses:
            b["territory"] = goal_dom.get(b["goal_id"])
        tasks = territory.open_tasks(self.db)
        for t in tasks:
            t["territory"] = territory.task_territory(t)
        return bosses, tasks

    def map_page(self, now: datetime = None) -> dict:
        """领地地图：每块领地的灯色、正在打的副本、待办件数；荒原里没归类的件数。"""
        now = now or self.now()
        day = now.date()
        quests.seed(self.db)
        bosses, tasks = self._territory_parts(day)
        out = []
        for key, name, _ in territory.TERRITORIES:
            mine = [b for b in bosses if b["territory"] == key]
            rows = territory.health(self.db, key, day, bool(mine), self.store.root)
            light = territory.worst([r["light"] for r in rows])
            if mine:
                line = " · ".join(f"{'★ ' if b['main'] else ''}{b['name']} {round(100 * (b['frac'] or 0))}%" for b in mine[:2])
            else:
                lit = sorted((r for r in rows if r["light"] != "none"), key=lambda r: territory.ORDER[r["light"]])
                line = f"{lit[0]['name']} {lit[0]['actual']}" if lit else next((r["note"] for r in rows if r["note"]), "没有副本")
            if key == "life":
                ex = territory.explore_stats(self.store.root)
                if ex:
                    line += f" · 点亮 {ex['countries']} 国"
            out.append({"key": key, "name": name, "light": light, "line": line,
                        "todos": sum(1 for t in tasks if t["territory"] == key)})
        brain.sync(self.store)
        return {"territories": out, "waste": sum(1 for t in tasks if t["territory"] is None),
                "brain_ask": brain.counts(self.db)["ask"]}

    def spend_page(self, start: str, end: str) -> dict:
        from .. import domain_stats
        return domain_stats.daily_spend(self.db, date.fromisoformat(start), date.fromisoformat(end))

    def territory_page(self, key: str, now: datetime = None) -> dict:
        now = now or self.now()
        day = now.date()
        if key == territory.WASTE:
            _, tasks = self._territory_parts(day)
            rows = [t for t in tasks if t["territory"] is None]
            # 最近 14 天她归过的：归错了回到这里能改（她 11-05：“我都不知道怎么改”）
            from .. import locks
            locks.ensure(self.db)
            since = (day - timedelta(days=14)).isoformat()
            at = {int(r["obj"].split(":")[1]): r["at"][:10] for r in self.db.conn.execute(
                "SELECT obj, at FROM manual_locks WHERE field = 'domain' AND obj LIKE 'task:%' AND released_at IS NULL AND at >= ? ORDER BY at",
                (since,))}
            recent = sorted((t for t in tasks if t["id"] in at and t["territory"]), key=lambda t: at[t["id"]], reverse=True)
            return {"key": key, "name": "荒原", "tasks": [{"id": t["id"], "title": t["title"], "due": t["due"],
                                                         "guess": territory.guess(t["title"])} for t in rows],
                    "recent": [{"id": t["id"], "title": t["title"], "terr": t["territory"], "at": at[t["id"]][5:]} for t in recent[:30]],
                    "options": [{"key": k, "name": n} for k, n, _ in territory.TERRITORIES]}
        if key not in territory.NAME:
            raise ValueError("没有这个领域")
        quests.seed(self.db)
        bosses, tasks = self._territory_parts(day)
        mine = [b for b in bosses if b["territory"] == key]
        rows = territory.health(self.db, key, day, bool(mine), self.store.root)
        light = territory.worst([r["light"] for r in rows])
        g = quests.today_summary(self.db, day)
        habits = [{**h, "kind": "daily"} for h in g["daily"] if territory.HABIT_TERRITORY.get(h["title"]) == key] + \
                 [{**h, "kind": "weekly"} for h in self._weekly(g, day) if territory.HABIT_TERRITORY.get(h["title"]) == key]
        out = {"key": key, "name": territory.NAME[key], "light": light, "rows": rows, "bosses": mine,
               "habits": habits, "options": [{"key": k, "name": n} for k, n, _ in territory.TERRITORIES],
               "tasks": [{"id": t["id"], "title": t["title"], "due": t["due"], "main": t["quest"] == "main"}
                         for t in tasks if t["territory"] == key]}
        brain.sync(self.store)
        out["brain"] = brain.search(self.db, "all", terr=key, limit=12)
        from .. import domain_stats, season  # 这个领域的分析：分数（开季→现在、由哪几项组成、走势）+ 关键指标的变化图（她 2026-11-02）
        _, sstart = season.current(self.settings)
        out["score"] = domain_stats.score_block(self.db, key, sstart, day)
        out["charts"] = domain_stats.charts(self.db, key, day)
        if key == "money":  # 每一笔都看得到（2026-10-29 她说财务页看不到刚才那笔）
            out["transactions"] = finance.recent(self.db, 30)
            out["budget"] = territory.budget_plan(self.db, day)
            out["spend"] = domain_stats.daily_spend(self.db, day - timedelta(days=6), day)  # 默认最近 7 天，她可以选
            out["fx"] = finance.fx_rate(self.db)
        if key == "body":
            out["weights"] = territory._weights(self.db, day, 28)
        if key == "life":
            out["explore"] = territory.explore_stats(self.store.root)
        return out

    # ---------- 回顾、设置 ----------
    def season_page(self, now: datetime = None) -> dict:
        """赛季页（她 2026-11-02 选了样稿 B）：七块领域这个赛季怎么样、彼此怎么此消彼长。"""
        from .. import season
        day = (now or self.now()).date()
        s, start = season.current(self.settings)
        out = season.page(self.db, s, start, day)
        from .. import weekreview
        out["reviews"] = weekreview.recent(self.db)  # 每周复盘的结果（她 2026-11-02：工作台只看汇总）
        from .. import nextseason  # 下个赛季草稿、往季结算（她 2026-11-02）
        d = nextseason.draft(self.db, s["name"])
        out["draft"] = d["body"] if d else None
        out["draft_at"] = d["updated_at"][:16].replace("T", " ") if d else None
        out["past"] = nextseason.archive(self.db)
        if out.get("stale") and getattr(self, "season_kick", None):  # 小伴 那句结论旧了：后台重写，过一会儿刷新就是新的（她 2026-11-02）
            out["rewriting"] = bool(self.season_kick())
        return out

    def review_page(self, now: datetime = None) -> dict:
        day = (now or self.now()).date()
        cur, start = season_mod.current(self.settings)  # 第一季从 10-28 开始算
        bosses, _ = self._territory_parts(day)
        return review.page(self.db, self.store.root, day, {**cur, "start": start.isoformat()}, bosses)

    def do_review_feedback(self, p, day):
        note = str(p.get("note", "")).strip()
        if not note:
            return {"ok": False, "msg": "写一句再发"}
        self.db.add_feedback("回顾反馈：" + note[:500])
        return {"ok": True, "msg": "收到，下周排计划照这个来", "log": f"给回顾写了反馈：{note[:60]}"}

    def settings_page(self, now: datetime = None) -> dict:
        now = now or self.now()
        day, root, db = now.date(), Path(self.store.root), self.db
        mz = finance.load_tokens(root)
        def latest(folder, mark=None):
            for p in sorted((root / folder).glob("*.jsonl"), reverse=True)[:14]:
                for line in reversed(p.read_text(encoding="utf-8").split("\n")):
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if mark is None or str(e.get("text", "")).startswith(mark):
                        return e.get("at", "")[:16].replace("T", " ")
            return ""
        saved = latest("inbox", "【收藏日报】")
        fmt = lambda t: str(t)[:16].replace("T", " ")
        conns = [
            {"name": "Telegram", "ok": self.settings.get("telegram_owner_chat_id") is not None, "detail": "和 小伴 聊天、早报、提醒"},
            {"name": "苹果日历", "ok": self.calendar is not None, "detail": "日程同步到你的 iPhone", "fix": "服务器上录入 iCloud 专用密码（setup_secrets）"},
            {"name": "Grok", "ok": bool(os.environ.get("GROK_WEBHOOK_URL")), "detail": "替 小伴 上网查、跑腿", "fix": "服务器上录入 Grok webhook（setup_secrets）"},
            {"name": "收藏日报", "ok": bool(saved), "detail": f"最近一份 {saved}" if saved else "Grok 每天伦敦 4 点发，第一份 10-29", "fix": "往共享收藏夹里放就行"},
            {"name": "Monzo", "ok": bool(mz.get("last_sync")),
             # 2026-10-29 她授权过了却还显示“去授权”：授权了只是还没同步成功的，不再让她重新授权
             "detail": f"英国的花销 · 最近同步 {fmt(mz['last_sync'])}" if mz.get("last_sync")
             else "英国的花销 · 已授权，还没同步成功（系统在处理，不用你动）" if mz.get("access_token") else "英国的花销",
             "fix": "" if mz.get("access_token") else "按 docs/operations/finance.md 建好 Monzo 客户端后，点这里去授权",
             "link": "" if mz.get("access_token") else "/api/monzo/start"},
            {"name": "国内银行卡（招行、交行）", "ok": finance.has_source(db, "sms"),
             "detail": "国内的花销（招行动账邮件，交行短信）" + (
                 f" · 最近收到一条没认出来的：“{(finance.last_unparsed(db)['raw'] or '')[:20]}”" if finance.last_unparsed(db) and not finance.has_source(db, "sms") else ""),
             "fix": ("收到了但内容不对：快捷指令里 text 的值要选蓝色变量（邮件或短信的正文），不能是打的字" if finance.last_unparsed(db)
                     else "iPhone 快捷指令收到银行动账邮件时自动转发（docs/operations/finance.md）")},
            {"name": "手机定位", "ok": places_log.has_data(db), "detail": "每天出门、出门做非工作的事", "fix": "iPhone 快捷指令设两条自动化（docs/operations/devices.md）"},
            {"name": "Mac 工作时长", "ok": worktime.has_data(db), "detail": "事业领域的工作时长上限", "fix": "Mac 上跑一次 python3 -m ethan.worktime --install"},
            {"name": "小红书 / Instagram 账号", "ok": False, "detail": "发帖数、粉丝", "fix": "等接账号"}]
        plan = db.latest_plan(day.isoformat())
        hooks = agents.load_sessions(root).get("at") or ""
        health = [
            {"name": "每日计划", "ok": plan is not None, "detail": "今天的计划排好了" if plan else "今天还没排（每天 06:30）"},
            {"name": "今日卡", "ok": (root / "cards" / f"{day.isoformat()}.json").exists(), "detail": "今天的卡画好了" if (root / "cards" / f"{day.isoformat()}.json").exists() else "今天还没画"},
            {"name": "Claude 会话报到", "ok": bool(hooks), "detail": f"最近一次 {fmt(hooks)}" if hooks else "还没有会话报到"},
            {"name": "Grok 回报", "ok": bool(latest("inbox")), "detail": f"最近一次 {latest('inbox') or '没有'}"}]
        # 系统体检一屏（她 11-04 要升级系统页）：定时任务、部署、大批整理、来源、内存、红点自检，和巡检看同一份（ethan/syscheck.py）
        try:
            from .. import syscheck
            system = syscheck.overview(root, db, now.astimezone(timezone.utc) if now.tzinfo else None, wb=self, services=self.services_check)
        except Exception as e:
            system = {"error": f"{type(e).__name__} {str(e)[:200]}"}
        try:
            metrics = sysmetrics.weekly(self.store, root)
        except Exception:
            metrics = []
        try:  # 记忆一栏（ontology 总方案 1.10 的前身）：有多少条、各来源、文件整理到哪、语义检索开没开
            from .. import interviews, memsearch
            from .. import lifebase
            mem = {**lifebase.memory_stats(db), "files": interviews.files_progress(db), "semantic": memsearch.semantic_on(db)}  # 来源健康挪到系统体检
        except Exception:
            mem = None
        return {"memory": mem, "prefs": {"proactive": self.settings.get("proactive_enabled", True) is not False,
                          "quiet": self.settings.get("quiet_hours", "00:00-08:00"), "timezone": self.settings.get("timezone", DEFAULT_TZ)},
                "connections": conns, "health": health, "metrics": metrics, "system": system}

    def do_settings_set(self, p, day):
        if "proactive" in p:
            self.settings.set("proactive_enabled", bool(p["proactive"]))
            return {"ok": True, "msg": "小伴 会按时段主动找你" if p["proactive"] else "先不主动找你了",
                    "log": "打开主动消息" if p["proactive"] else "关掉主动消息"}
        if "quiet" in p:
            from ..proactive import parse_quiet
            try:
                parse_quiet(str(p["quiet"]))
            except ValueError as e:
                return {"ok": False, "msg": f"没改成：{e}"}
            self.settings.set("quiet_hours", str(p["quiet"]))
            return {"ok": True, "msg": f"记住了，{p['quiet']} 不主动打扰你", "log": f"免打扰改成 {p['quiet']}"}
        return {"ok": False, "msg": "不认识的设置"}

    # ---------- 卡册、成就 ----------
    def _diary(self, day: str) -> dict:
        """那天的日记（她的口吻，ethan/diary.py；她 2026-11-01 定）。小伴 的聊天记忆不再放进日记本。"""
        from .. import diary
        d = diary.get(self.db, day)
        return ({"diary": d["body"], "diary_mine": d["source"] in diary.HER, "marks": diary.marks(self.db, day), "notes": diary.notes(self.db, day)} if d
                else {"diary": "", "diary_mine": False, "marks": [], "notes": []})

    def do_diary_save(self, p, day):
        from .. import diary
        d = diary.save(self.db, str(p["day"])[:10], p.get("text") or "")
        return {"ok": True, "msg": "日记存好了", "diary": d["body"], "log": f"改日记：{d['day']}"}

    def do_diary_mark(self, p, day):
        """记号笔：把日记里写得好的句子标出来，再点一次取消（她 2026-11-02）。"""
        from .. import diary
        d, text = str(p["day"])[:10], str(p.get("text") or "")
        ms = diary.unmark(self.db, d, text) if p.get("off") else diary.mark(self.db, d, text)
        return {"ok": True, "msg": "取消了标记" if p.get("off") else "标好了", "marks": ms, "log": f"日记{'取消' if p.get('off') else ''}标记：{d}"}

    def do_diary_note(self, p, day):
        """批注：她划出一句写评论，小伴 只按批注重写这一句，同时进纠错本（她 2026-11-02）。"""
        from .. import diary
        d, text, note = str(p["day"])[:10], str(p.get("text") or ""), str(p.get("note") or "")
        nid = diary.annotate(self.db, d, text, note)
        if self.router is None:
            return {"ok": True, "msg": "批注记下了，小伴 连上模型后会改", "notes": diary.notes(self.db, d), "log": f"日记批注：{d}"}
        r = diary.fix(self.store, self._model(), nid)
        return {"ok": True, "msg": "小伴 改好了这一句" if r["status"] == "done" else "批注记下了，这句 小伴 没改成",
                "notes": diary.notes(self.db, d), "diary": (diary.get(self.db, d) or {}).get("body", ""), "log": f"日记批注：{d}"}

    def do_diary_weave(self, p, day):
        if self.router is None:
            return {"ok": False, "msg": "现在连不上模型，先用“直接加进去”"}
        from .. import diary
        try:
            d = diary.weave(self.store, self._model(), str(p["day"])[:10], p.get("note") or "")
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "写进去了", "diary": d["body"], "log": f"补日记：{d['day']}"}

    def _journal(self, day: str) -> dict:
        """那天的日记（每晚整理写的 journal 页）：卡册里看过去某天时一起显示（2026-10-30 她要）。"""
        slug = f"journal/{day}"
        if not self.store.wiki.exists(slug):
            return {}
        body = self.store.wiki.read(slug)["body"]
        lines = [l for l in body.splitlines() if l.strip() and not l.startswith("# ")]
        first = lines[0] if lines else ""
        if len(first) > 300:  # 截在句号上并标“……”，不在半句话中间断（2026-10-31 她截图里断在“加餐时间等”）
            cut = first[:300]
            end = max(cut.rfind(p) for p in "。！？；")
            first = (cut[:end + 1] if end > 120 else cut) + "……"
        return {"journal_summary": first, "journal": body}

    def cards_page(self) -> dict:
        if self.redo_runner is not None and cards.redo_waiting(self.db):  # 上一张画完了、还有“现在重画”的要求没用上：接着画
            self.redo_runner()
        if self.diary_kick is not None:  # 开始以来哪天还没有日记，后台补写（前几天的就是这样按新写法重写的）
            from .. import diary
            if diary.missing(self.store, self.now().date()):
                self.diary_kick()
        out = []
        for c in cards.collection(self.store.root):
            if c.get("image"):
                # 每天早上画的卡是照着前一天画的（她 2026-11-01）：日记本、卡册里这张卡算前一天的，和前一天的日记放一起；今天的还没有
                dday = (date.fromisoformat(c["day"]) - timedelta(days=1)).isoformat()
                out.append({k: c.get(k) for k in ("title", "rarity", "reason", "line", "weather")}
                           | {"day": dday, "made": c["day"], "image": f"/img/card/{c['day']}",
                              "kind": "点单" if c.get("wish") else {"E": "节点", "S": "训练"}.get((c.get("code") or "W")[0], "日常"),
                              "wish": c.get("wish") or ""} | self._diary(dday))
        busy = bool(self.redo_runner is not None and getattr(self.redo_runner, "busy", lambda: False)())
        return {"cards": out, "wishes": cards.wishes(self.db), "drawing": busy}

    def do_ach_seen(self, p, day):
        achievements.mark_shown(self.db, [str(k) for k in p.get("keys") or []])
        return {"ok": True}

    def _work(self, now: datetime) -> dict:
        """首页一行：今天工作了几小时、今天的工作狂指数（ethan/workaholic.py）。"""
        try:
            from .. import workaholic
            return workaholic.today_line(self.db, now)
        except Exception:
            return {}

    def week_page(self, now: datetime = None) -> dict:
        from .. import weekly
        return weekly.get(self.db, (now or self.now()).date())

    def ach_page(self, now: datetime = None) -> dict:
        return achievements.compute(self.db, self.store.root, (now or self.now()).date())

    # ---------- 第二大脑 ----------
    def ethan_days(self, n: int = 7) -> dict:
        """小伴 的一天：他自己的剧本（ethan/life.py 每天写），2026-10-29 她说不算她的资料，单独一个入口随时看。"""
        from .. import life
        wiki = self.store.wiki
        days = sorted((s for s in wiki.pages() if s.startswith(life.PREFIX + "/day-")), reverse=True)[:n]
        return {"days": [{"day": s.rsplit("day-", 1)[1], "title": wiki.read(s).get("title") or s, "body": wiki.read(s)["body"]} for s in days]}

    def badges(self, day: date) -> dict:
        """导航上的红点：要她本人去系统里处理的事（2026-10-29 她要）。待办和任务不算，那些在今日页本来就看得到。"""
        out = {}
        try:
            out["brain"] = brain.counts(self.db)["ask"]  # 大脑里 小伴 拿不准、要她定的
        except Exception:
            pass
        out["main"] = sum(1 for x in prep.queue(self.db) if x["state"] == "ready" and not x["secret"])  # 小伴 准备好、等她过目的
        try:
            from .. import desk
            if desk.count(self.db):
                out["desk"] = desk.count(self.db)  # 今日邮件和留言里等她处理的（首页按钮上的红点）
        except Exception:
            pass
        try:
            from .. import mepage
            out["me"] = mepage.pending_count(self.db) + out.get("brain", 0)  # “我”页里等她定的 + 收藏和资料里要她看的（大脑并进“我”页，2026-10-31）
        except Exception:
            pass
        g = quests.totals(self.db)
        ready = sum(1 for r in quests.rewards(self.db) if g["balance"] >= r["cost"])
        if ready:
            out["quests"] = ready  # 商店里有能拆的惊喜
        out["map"] = sum(1 for t in territory.open_tasks(self.db) if not territory.task_territory(t))  # 荒原里等她归类的（不用再算一遍副本）
        return {k: v for k, v in out.items() if v}

    def brain_page(self, tab: str = "all", q: str = "", kind: str = "", terr: str = "") -> dict:
        brain.sync(self.store)
        return {"items": brain.search(self.db, tab, q, kind, terr), "counts": brain.counts(self.db, tab, q, kind, terr),
                "kinds": brain.KINDS, "groups": brain.KIND_GROUPS, "statuses": brain.STATUS_LABELS,
                "territories": [{"key": k, "name": n} for k, n, _ in territory.TERRITORIES]}

    def do_brain_set(self, p, day):
        """放进在用 / 备查 / 回收站 / 恢复，改归属区域、标签。"""
        fields = {k: p[k] for k in ("status", "territory", "tags", "title", "summary") if k in p}
        if isinstance(fields.get("tags"), str):
            fields["tags"] = [t for t in re.split(r"[，,\s]+", fields["tags"]) if t]
        try:
            item = brain.update(self.db, int(p["id"]), **fields)
        except (ValueError, KeyError) as e:
            return {"ok": False, "msg": str(e)}
        msg = {"kept": "放进在用了", "archived": "放进备查了", "trash": "放进回收站了", "new": "交回 小伴 整理"}.get(fields.get("status"), "改好了")
        return {"ok": True, "msg": msg, "undo": {"action": "brain.set", "id": item["id"], "status": p["was"]} if p.get("was") else None}

    def do_prep_approve(self, p, day):
        ok = prep.approve(self.db, int(p["id"]))
        return {"ok": ok, "msg": "通过！这件收工了" if ok else "这件还没做好", "log": "通过 小伴 准备的一件事" if ok else None}

    def do_prep_pending(self, p, day):
        ok = prep.set_pending(self.db, int(p["id"]))
        return {"ok": ok, "msg": "先放着了，在“先放着”里随时拿出来" if ok else "这件放不了", "log": "小伴 准备的一件事先放着" if ok else None,
                "undo": {"action": "prep.unpend", "id": int(p["id"])} if ok else None}

    def do_prep_parkedseen(self, p, day):
        self.settings.set("parked_seen", day.isoformat())
        return {"ok": True}

    def do_prep_unpend(self, p, day):
        ok = prep.unpend(self.db, int(p["id"]))
        return {"ok": ok, "msg": "拿出来了" if ok else "这件不在“先放着”里", "log": "把先放着的一件拿出来" if ok else None}

    def do_prep_revise(self, p, day):
        ok = prep.revise(self.db, int(p["id"]), str(p.get("note", "")))
        if ok:
            self.prep_kick(self.store.root, int(p["id"]))  # 马上重做，不等下一轮
        return {"ok": ok, "msg": "收到，小伴 马上按你的意见重做，几分钟后回来看" if ok else "这件找不到了", "log": "让 小伴 改准备的东西" if ok else None}

    def memory_page(self) -> dict:
        from .. import memusers
        return {"users": memusers.stats(self.db), "fixes": memusers.locked_tracking(self.db), "layers": memusers.LAYERS}

    # ---------- “我”页（ethan/mepage.py，她 2026-10-31 定稿） ----------
    def me_page(self) -> dict:
        from .. import mepage
        return mepage.page(self.db)

    def me_pending(self) -> dict:
        from .. import mepage
        return {"items": mepage.pending_items(self.db)}

    def me_all(self, q: str = "", dim: str = "", history: bool = False, tab: str = "facts", limit: int = 100, prop: str = "") -> dict:
        """“全部记忆”四个标签（她 2026-10-31 定）：关于你 facts、发生过的事 days、人 people；收藏和资料是原来的大脑页。"""
        from .. import mepage
        limit = max(30, min(int(limit or 100), 2000))
        if tab == "days":
            return mepage.days(self.db, self.store.wiki, q.strip(), limit)
        if tab == "people":
            return mepage.people(self.db, q.strip(), limit)
        return mepage.all_memory(self.db, q.strip(), dim, history, limit, prop=prop)

    def do_me_resolve(self, p, day):
        from .. import mepage
        # 定法放在 how 里：放 action 会顶掉外层的 "me.resolve"，变成“不认识的操作”（2026-10-31 她点不了）
        out = mepage.resolve(self.db, int(p["id"]), p.get("how"), pick=p.get("pick"), text=p.get("text") or "", type_=p.get("type") or "long",
                             since=p.get("since") or None, until=p.get("until") or None, ranges=p.get("ranges"))
        return {**out, "log": f"待你定 #{p['id']}：{out.get('msg', '')}"} if out.get("ok") else out

    def do_me_reopen(self, p, day):
        from .. import mepage
        return mepage.reopen(self.db, int(p["id"]))

    def do_me_ask(self, p, day):
        from .. import mepage
        if self.router is None:
            return {"ok": False, "msg": "工作台这边连不上模型"}
        return mepage.ask(self.db, self._model(), p.get("question") or "")

    def do_me_compare(self, p, day):
        from .. import mepage
        if self.router is None:
            return {"ok": True, "conflict": True, "why": "连不上模型，按矛盾处理"}
        return {"ok": True, **mepage.compare(self._model(), p.get("a") or "", p.get("b") or "")}

    def do_me_verdict(self, p, day):
        from .. import mepage
        return mepage.verdict(self.db, int(p["id"]), p.get("marks") or {}, text=p.get("text") or "", type_=p.get("type") or "long",
                              since=p.get("since") or None, until=p.get("until") or None, split=p.get("split"), kind=p.get("kind") or "wrong")

    def do_me_undo(self, p, day):
        from .. import mepage
        return mepage.undo_ticket(self.db, int(p["id"]))

    def me_manage(self) -> dict:
        from .. import mepage
        return mepage.manage(self.db)

    def do_me_reimport(self, p, day):
        from .. import mepage
        return mepage.allow_reimport_request(self.db, int(p["id"]), root=self.store.root)

    def do_me_keep(self, p, day):
        from .. import mepage
        return mepage.keep_suspended(self.db, int(p["id"]))

    def do_me_dropsusp(self, p, day):
        from .. import lifebase
        lifebase.forget(self.db, fact_ids=[int(p["id"])], root=self.store.root)
        return {"ok": True, "msg": "也删掉了"}

    def do_me_revoke(self, p, day):
        from .. import lifebase
        return {"ok": lifebase.revoke_disclosure(self.db, int(p["id"])), "msg": "撤回了：从现在起不再对外说"}

    def do_me_disclose(self, p, day):
        from .. import lifebase
        text = (p.get("text") or "").strip()
        try:
            did = lifebase.approve_disclosure(self.db, text, (p.get("quote") or text).strip(), to_users=lifebase.external_users(),
                                              once=bool(p.get("once")), to_whom=(p.get("to_whom") or "").strip() or None)
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "记下了：对外的（Dais、主页、合作方）可以说这一句", "id": did}

    def do_me_policy(self, p, day):
        """对外开关：某一档的某一类改成 可以说 / 问我 / 不说（ontology-plan 3.3）。"""
        from .. import mepage
        out = mepage.set_policy(self.db, p.get("tier") or "", p.get("category") or "", p.get("mode") or "")
        return {**out, "log": out["msg"]} if out.get("ok") else out

    def do_me_deny(self, p, day):
        """逐条挡住：这一类可以说，但这一句不说。"""
        from .. import lifebase
        try:
            did = lifebase.deny_disclosure(self.db, (p.get("text") or "").strip(), "她在“我”页挡住这句",
                                           statement_id=p.get("statement_id"), fact_id=p.get("fact_id"))
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "挡住了：这句不对外说", "id": did}

    def do_me_skip(self, p, day):
        from .. import mepage
        return mepage.skip_question(self.db, int(p["id"]))

    def do_me_verdict_fact(self, p, day):
        from .. import mepage
        return mepage.verdict_fact(self.db, int(p["id"]), str(p.get("v") or ""), root=self.store.root)

    def do_me_fix(self, p, day):
        from .. import mepage
        return mepage.fix_fact(self.db, int(p["id"]), p.get("text") or "", kind=p.get("kind") or "wrong", type_=p.get("type") or "long",
                               since=p.get("since") or None, until=p.get("until") or None)

    def relations_page(self) -> dict:
        """“关系”页（ethan/relations.py，她 11-04 定名）：三张清单 + 按重要程度分组的人物卡。"""
        from .. import relations
        return relations.page(self.db, self.now().date())

    def do_person_set(self, p, day):
        """她在人物卡上改一项（改了锁住，自动整理不再动）。"""
        from .. import relations
        return relations.set_field(self.db, int(p["id"]), str(p["field"]), p.get("value"))

    def do_person_loopdone(self, p, day):
        from .. import relations
        return relations.loop_done(self.db, int(p["id"]), str(p.get("text") or ""))

    def do_person_delete(self, p, day):
        """删卡：以后不再从微信建回来。"""
        from .. import relations
        return relations.delete(self.db, int(p["id"]))

    def desk_page(self) -> dict:
        from .. import desk
        from .. import mail
        return {"items": desk.items(self.db), "accounts": desk.send_accounts(), "archived": mail.archived(self.db, 2)}

    def do_desk_notjunk(self, p, day):
        from .. import mail
        return {**mail.not_junk(self.db, int(p["id"])), "log": f"收件台：邮件 #{p['id']} 不是垃圾"}

    def do_desk_todo(self, p, day):
        from .. import desk
        out = desk.todo(self.db, p["kind"], int(p["id"]), (p.get("note") or "").strip()[:200])
        return {**out, "log": f"收件台：{p['kind']} #{p['id']} 记成待办"}

    def do_desk_ignore(self, p, day):
        from .. import desk
        return {**desk.ignore(self.db, p["kind"], int(p["id"]), (p.get("note") or "").strip()[:200]), "log": f"收件台：{p['kind']} #{p['id']} 不用管"}

    def do_desk_draft(self, p, day):
        from .. import desk
        kick = (lambda rid: desk.kick_draft(self.store.root, rid)) if self.services_check else None
        return {**desk.ask_draft(self.db, p["kind"], int(p["id"]), (p.get("note") or "").strip()[:300], kick),
                "log": f"收件台：{p['kind']} #{p['id']} 起草回复"}

    def do_desk_translate(self, p, day):
        """邮件原文或回复草稿“看中文”，只给她看。"""
        from .. import desk
        return desk.translate(self.db, self._model(), p["kind"], int(p["id"]), p.get("what", "body"), p.get("text"))

    def do_desk_send(self, p, day):
        """她点了“发送”：替她发邮件只在她本人点这一下时发生。"""
        from .. import desk
        kind = p.get("kind") if p.get("kind") in ("visitor", "out") else "mail"
        if kind == "out":
            out = desk.send_new(self.db, int(p["id"]), p.get("text") or "", p.get("account"), to=p.get("to"))
        else:
            out = desk.send(self.db, int(p["id"]), p.get("text") or "", kind=kind)
        return {**out, "log": f"收件台：{kind} #{p['id']} 发出回复"} if out.get("ok") else out

    def do_desk_replied(self, p, day):
        """她复制去别处发了，回来点“已回”。"""
        from .. import desk
        return {**desk.replied(self.db, p["kind"], int(p["id"])), "log": f"收件台：{p['kind']} #{p['id']} 她在别处回了"}

    def do_brain_purge(self, p, day):
        ok = brain.purge(self.db, int(p["id"]))
        return {"ok": ok, "msg": "永久删除了" if ok else "只能删回收站里的"}

    def do_task_domain(self, p, day):
        """待办归到哪块领地（荒原里确认 小伴 猜的，或改掉；领域页和“日历 · 改计划”里随时再改）；waste 表示先放荒原。
        她改的记锁、系统不改回；她自己随时能再改（她 11-05：“荒原里面一条事业的不小心归成了生活体验，我都不知道怎么改”）。
        返回撤销：几秒内点“撤销”回到改之前（undo=1 带着原来的值 prev）。"""
        tid = int(p["id"])
        row = self.db.conn.execute("SELECT title, domain FROM tasks WHERE id = ?", (tid,)).fetchone()
        if row is None:
            raise ValueError("这件事不在了")
        if p.get("undo"):  # 撤销：原样放回（原来没归过的回到 小伴 猜 / 跟着副本，锁在 locks.record_action 里放开）
            prev = p.get("prev") or None
            if prev is not None and prev not in territory.NAME and prev != "waste":
                raise ValueError("没有这个领域")
            self.db.conn.execute("UPDATE tasks SET domain = ? WHERE id = ?", (prev, tid))
            self.db.conn.commit()
            return {"ok": True, "msg": "撤销了", "log": f"撤销了待办的领域：{row['title']}"}
        key = str(p.get("key", ""))
        if key not in territory.NAME and key != territory.WASTE:
            raise ValueError("没有这个领域")
        self.db.conn.execute("UPDATE tasks SET domain = ? WHERE id = ?", (key if key in territory.NAME else "waste", tid))
        self.db.conn.commit()
        name = territory.NAME.get(key, "荒原")
        return {"ok": True, "msg": f"归到{name}了", "log": f"待办归到{name}：{row['title']}",
                "undo": {"action": "task.domain", "id": tid, "undo": 1, "prev": row["domain"] or ""}}

    def do_goal_up(self, p, day):
        """副本在同一块领地里往前挪一位（2026-10-28 她要能自己定先后）；整块重新编号 1、2、3…"""
        gid = int(p["id"])
        bosses, _ = self._territory_parts(day)
        key = next((b["territory"] for b in bosses if b["goal_id"] == gid), None)
        ids = [b["goal_id"] for b in bosses if b["territory"] == key]
        if gid not in ids:
            raise ValueError("没有这个副本")
        i = ids.index(gid)
        if i == 0:
            return {"ok": True, "msg": "已经在最前面了"}
        ids[i - 1], ids[i] = ids[i], ids[i - 1]
        self.db.conn.executemany("UPDATE goals SET sort = ? WHERE id = ?", [(n, g) for n, g in enumerate(ids, 1)])
        self.db.conn.commit()
        name = next(b["name"] for b in bosses if b["goal_id"] == gid)
        return {"ok": True, "msg": "往前挪了", "log": f"副本排序：{name} 往前挪一位"}

    def team_page(self, now: datetime = None) -> dict:
        """团队页：各 agent 的成绩单（最近 7 天）和任务看板（见 docs/design/agent-team.md）。"""
        db = self.db
        agents.ensure(db)
        grok = agents.grok_live(self.store.root)
        for g in grok["done"]:  # Grok 的回报记成一件通过的活，能打分（2026-10-29 她说待评价里没有手指头）
            key = "Grok 回报 " + g["at"]
            title = ("回报：" + g["task"][:30] if g.get("task") else g["title"])
            old = db.conn.execute("SELECT id, review_note FROM agent_tasks WHERE agent = 'grok' AND project = ?", (key,)).fetchone()
            if not old:
                agents.record_done(db, "grok", title, 1, project=key, note=g["text"])
            elif not old["review_note"]:  # 以前记的只有一行乱标题：补上全文和像样的标题
                db.conn.execute("UPDATE agent_tasks SET title = ?, review_note = ? WHERE id = ?", (title, g["text"], old["id"]))
                db.conn.commit()
        agents.auto_rate(db)
        agents.expire_self_started(db)
        week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
        cols = []
        for key, label, sql, args in (
                ("queued", "排队", "status = 'queued'", ()),
                ("running", "在干", "status = 'running'", ()),
                ("review", "等审查", "status = 'review'", ()),
                ("accepted", "通过 · 待评价", "status = 'accepted' AND rating IS NULL", ()),  # 评过的进归档（2026-10-28 她要）
                ("failed", "失败 · 放弃（7 天）", "status IN ('failed', 'dropped') AND finished_at >= ? AND archived_at IS NULL", (week,))):
            rows = db.conn.execute(f"SELECT {', '.join(agents.LIST_FIELDS)}, review_note, spec FROM agent_tasks "
                                   f"WHERE {sql} ORDER BY id DESC LIMIT 30", args)
            cols.append({"key": key, "label": label, "tasks": [dict(r) for r in rows]})
        archive = [dict(r) for r in db.conn.execute(
            f"SELECT {', '.join(agents.LIST_FIELDS)}, review_note, spec FROM agent_tasks WHERE (status = 'accepted' AND rating IS NOT NULL) "
            "OR (status IN ('failed', 'dropped') AND finished_at < ?) OR archived_at IS NOT NULL "
            "ORDER BY COALESCE(archived_at, reviewed_at, finished_at) DESC LIMIT 100", (week,))]
        # 系统之外也在干的活（2026-10-29 她说团队页漏了很多）：Claude 会话（钩子报到）、准备员（算 小伴 的）、Grok（派出去还没回的）
        sessions = agents.load_sessions(self.store.root)
        live = [{"agent": x.get("agent", "claude"), "title": x["title"],
                 "sub": f"会话 · {x['where_label']}", "at": x["updated_at"],
                 "state": "running" if x["state"] == "working" else "review", "summary": x.get("summary", ""),
                 "status_label": x.get("state_label", ""), "detail": x.get("summary", ""), "detail_label": "它最近说的一段",
                 "key": x.get("key", "")}
                for x in sessions.get("sessions", []) if x["state"] in ("working", "blocked", "review_ready")]
        # 排队 = 资源不够轮不上（准备员每次做 2 件、每天有上限；Codex 等额度）或者前置没做完；不放“等她审批”的（2026-10-29 她定）
        waiting = 0
        today = set(prep.today_ids(db))
        for x in prep.queue(db):
            if x["secret"]:  # 给她的惊喜不上团队页（2026-10-30）
                continue
            if x["state"] == "todo" and x["id"] not in today:  # 排队只看今天的活饱不饱和：她点星星的和 小伴 今天定的（2026-10-29）
                continue
            if x["state"] == "todo":
                waiting += 1
            if x["state"] in ("working", "waiting_grok", "revising"):
                live.append({"agent": "grok" if x["state"] == "waiting_grok" else "ethan", "title": x["title"],
                             "sub": "准备员 · " + (x.get("state_label") or ""), "at": x.get("created_at") or "", "state": "running"})
            elif x["state"] == "ready":
                live.append({"agent": "ethan", "title": x["title"], "sub": "准备员 · 做好了等你看，点开就是", "at": x.get("created_at") or "",
                             "state": "review", "prep": x["id"]})
            elif x["state"] == "todo":
                why = f"准备员每轮做 {prep.PER_RUN} 件，前面还有 {waiting - 1} 件" if waiting > 1 else "下一轮就做"
                live.append({"agent": "ethan", "title": ("★ " if x["star"] else "") + x["title"], "sub": f"准备员 · {why}", "at": x.get("created_at") or "", "state": "queued", "prep": x["id"]})
        live += [{"agent": "grok", "title": g["title"], "sub": "你在 Grok 里交代的 · 它报了开工" if g.get("self") else "小伴 派的 · 等 Grok 回报",
                  "at": g["at"], "state": "running", "detail": g.get("task", ""), "detail_label": "交代给 Grok 的原话"}
                 for g in grok["running"]]
        board = agents.scoreboard(db)
        for r in board:
            r["open"] = (r.get("open") or 0) + sum(1 for x in live if x["agent"] == r["agent"] and x["state"] in ("running", "queued"))
        return {"scoreboard": board, "columns": cols, "archive": archive, "live": live,
                "claude_sessions": sessions,
                "names": {**agents.REPORT_AGENTS, "ethan": "小伴"}}

    def quests_page(self, now: datetime = None) -> dict:
        now = now or self.now()
        day, db = now.date(), self.db
        quests.seed(db)
        from .. import stages
        goals = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM goals")}
        chapters = []
        for gid, steps in quests.main_quests(db, with_agent_goals=True).items():
            nxt = next((s["id"] for s in steps if not s["done"]), None)
            chapters.append({"goal_id": gid, "name": goals.get(gid, "").split("：")[0],
                             "main": MAIN_BOSS in goals.get(gid, ""),
                             "steps": [{"id": s["id"], "title": s["title"], "points": quests.task_points(s),
                                        "state": "done" if s["done"] else "now" if s["id"] == nxt else "locked",
                                        "hard": bool(s.get("hard")), "milestone": bool(s["milestone"]),
                                        "draft": bool(s["draft"]),
                                        "progress": None if s["done"] else quests.task_progress(db, s, day)} for s in steps],
                             "milestones": stages.view(db, gid, day) if stages.has_structure(db, gid) else None})
        chapters.sort(key=lambda c: not c["main"])
        week = [day - timedelta(days=n) for n in range(6, -1, -1)]
        dailies = [{"id": h["id"], "title": h["title"],
                    "week": [quests.checked(db, h["id"], d) for d in week]}
                   for h in quests.habits(db) if h["cadence"] == "daily"]
        g = quests.today_summary(db, day)
        # 已经排进今日任务的不在“其他”里再列一遍（她 11-05：“今日待办和其他怎么又有一样的任务？”）
        in_today = {t["id"] for t in self.today_tasks(day, db.latest_plan(day.isoformat()))["today"]}
        side = quests.side_quests(db)
        goal_dom = {r["id"]: r["domain"] for r in db.conn.execute("SELECT id, domain FROM goals")}
        return {
            "chapters": chapters,
            "side": [{"id": q["id"], "title": q["title"], "points": quests.task_points(q), "progress": quests.task_progress(db, q, day),
                      "terr": territory.task_territory({**q, "goal_domain": goal_dom.get(q.get("goal_id"))}) or territory.WASTE}
                     for q in side if q["id"] not in in_today],
            "side_today": sum(1 for q in side if q["id"] in in_today),
            "later": [{"id": q["id"], "title": q["title"], "goal": q["goal"].split("：")[0], "points": quests.task_points(q)}
                      for q in quests.later_quests(db)],
            "habits": [{"id": h["id"], "title": h["title"], "cadence": h["cadence"], "target": h["target"]} for h in quests.habits(db)],
            "dailies": dailies, "weekly": self._weekly(g, day), "streak": g["streak"],
            "rest_day": quests.is_rest_day(db, day), "rest_left": quests.rest_days_left(db, day),
            "rewards": [{**r, "affordable": g["balance"] >= r["cost"]} for r in quests.rewards(db)],
            "balance": g["balance"],
            "pace": round(db.conn.execute(  # 最近 7 天平均每天挣多少分，商店用来估“还要几天”
                "SELECT COALESCE(SUM(delta), 0) FROM points WHERE delta > 0 AND day > ? AND day <= ?",
                ((day - timedelta(days=7)).isoformat(), day.isoformat())).fetchone()[0] / 7),
            "history": [dict(r) for r in db.conn.execute(
                "SELECT x.id, x.at, x.status, x.cost, x.note, w.title FROM redemptions x JOIN rewards w ON w.id = x.reward_id "
                "ORDER BY x.id DESC LIMIT 10")],
            "goals": self.goal_options(),
        }

    # ---------- 写 ----------
    def log(self, text: str, now: datetime = None):
        path = Path(self.store.root) / LOG
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(
                timespec="seconds"), "text": text}, ensure_ascii=False) + "\n")

    def act(self, action: str, p: dict, now: datetime = None) -> dict:
        self._save_synced()
        now = now or self.now()
        day = now.date()
        handler = getattr(self, "do_" + action.replace(".", "_"), None)
        if handler is None:
            return {"ok": False, "msg": "不认识的操作"}
        redo = None
        before = self._lock_before(action, p)
        try:
            if action == "task.undo":
                redo = ("task_done", {"id": p["id"]})
            elif action == "habit.uncheck":
                redo = ("habit_check", {"id": p["id"], "easy": self._easy(int(p["id"]), day)})
            if self._locks_action(action):
                # 她改的值和“她改过”的锁一个事务（Codex 评审 11-05）：以前先提交值、再另外提交锁，记锁失败只打一行日志，
                # 中间每晚整理、计划器插进来看不到锁，会把她的改动覆盖掉。现在锁记不上，这次改动一起退回
                try:
                    with tx.atomic(self.db):
                        out = handler(p, day)
                        if out.get("ok"):
                            self._lock_after(action, p, day, before, strict=True)
                except (KeyError, ValueError, TypeError):
                    raise
                except Exception as e:
                    print(f"[workbench] 改动没存上（记锁出错，已整体退回）：{type(e).__name__} {e}", flush=True)
                    return {"ok": False, "msg": "没改成，请再试一次"}
            else:
                out = handler(p, day)
        except (KeyError, ValueError, TypeError) as e:
            return {"ok": False, "msg": f"参数不对：{e}"}
        if redo and quests.totals(self.db)["balance"] < 0:  # 分已经拿去兑换了，撤销会让余额变负：退回去，不让撤（2026-10-27）
            getattr(self, "do_" + redo[0])(redo[1], day)
            return {"ok": False, "msg": "这些分已经用来兑换了，撤销不了"}
        if out.get("ok") and not self._locks_action(action):
            self._lock_after(action, p, day, before)
        if out.get("ok") and out.get("log"):
            self.log(out.pop("log"), now)
        out.pop("log", None)
        return out

    def _lock_before(self, action: str, p: dict):
        """要记“她改过”的锁时，先拿操作前那条（删了就拿不到了）：名字、所属副本。"""
        try:
            if action in ("task.delete", "task.rename") and p.get("id"):
                r = self.db.conn.execute("SELECT title, goal_id FROM tasks WHERE id = ?", (int(p["id"]),)).fetchone()
                return dict(r) if r else None
            if action in ("event.delete", "event.time") and p.get("id"):
                r = self.db.conn.execute("SELECT title FROM plan_items WHERE id = ?", (int(p["id"]),)).fetchone()
                return dict(r) if r else None
            if action == "prep.restore" and p.get("id"):
                r = self.db.conn.execute("SELECT title FROM prep_trash WHERE id = ?", (int(p["id"]),)).fetchone()
                return dict(r) if r else None
            if action == "prep.drop" and p.get("id"):
                r = self.db.conn.execute("SELECT title, goal_id FROM tasks WHERE id = ?", (int(p["id"]),)).fetchone()
                return dict(r) if r else None
        except Exception:
            return None
        return None

    @staticmethod
    def _locks_action(action: str) -> bool:
        """这个操作要记（或放开）“她改过”的锁。"""
        from .. import locks
        return action in locks.ACTION_LOCKS or action in ("task.restore", "prep.restore")

    def _lock_after(self, action: str, p: dict, day: date, before, strict: bool = False):
        """她的操作成功了：记“她改过”的锁，自动流程以后绕开（ethan/locks.py，docs/design/system-rules.md 第 1 节）。
        strict：在 act 的同一事务里，出错往外抛（整次改动退回），不吞掉。"""
        from .. import locks
        try:
            if action in ("task.restore", "prep.restore"):  # 恢复了：删除的锁放开
                title = (p.get("row") or {}).get("title") or (before or {}).get("title")
                if title:
                    locks.release_deleted(self.db, title, "prep" if action == "prep.restore" else "task")
                return
            locks.record_action(self.db, action, p, day, before)
        except Exception as e:
            if strict:
                raise
            print(f"[workbench] 记锁没成：{type(e).__name__} {e}", flush=True)

    def _title(self, task_id: int) -> str:
        r = self.db.conn.execute("SELECT title FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if r is None:
            raise ValueError("这件事不在了")
        return r["title"]

    def _habit(self, habit_id: int) -> str:
        r = self.db.conn.execute("SELECT title FROM habits WHERE id = ?", (habit_id,)).fetchone()
        if r is None:
            raise ValueError("这项日常不在了")
        return r["title"]

    def do_habit_check(self, p, day):
        title = self._habit(int(p["id"]))
        if p.get("yesterday"):  # 补昨天的卡（她 2026-11-01：常忘了打卡），只能补前一天
            day = day - timedelta(days=1)
        gain = quests.check_habit(self.db, int(p["id"]), day, easy=bool(p.get("easy")))
        if p.get("yesterday"):
            title = self._habit(int(p["id"]))
            return {"ok": True, "gain": gain, "msg": f"补上了昨天的「{title}」" + (f" +{gain}" if gain else "（昨天已经打过了）"),
                    "log": f"补打卡（{day}）：{title}"}
        return {"ok": True, "gain": gain, "msg": f"+{gain}" if gain else "今天已经打过卡了",
                "undo": {"action": "habit.uncheck", "id": p["id"]},
                "log": f"打卡：{title}" + ("（简单版）" if p.get("easy") else "")}

    def do_habit_uncheck(self, p, day):
        title = self._habit(int(p["id"]))
        back = quests.uncheck_habit(self.db, int(p["id"]), day)
        return {"ok": True, "gain": -back, "msg": f"已撤销 −{back}", "log": f"撤销打卡：{title}"}

    def _stage(self, tid: int) -> bool:
        from .. import stages
        stages.ensure(self.db)
        r = self.db.conn.execute("SELECT milestone_id FROM tasks WHERE id = ?", (tid,)).fetchone()
        return bool(r and r["milestone_id"])

    def do_task_done(self, p, day):
        from .. import stages
        title = self._title(int(p["id"]))
        if self._stage(int(p["id"])):  # 阶段任务：反复做的记今天一笔，复盘勾完进下一个里程碑（quest-structure.md）
            gain = stages.check(self.db, int(p["id"]), day)
            return {"ok": True, "gain": gain, "msg": f"+{gain}" if gain else "记上了",
                    "undo": {"action": "task.undo", "id": p["id"]}, "log": f"完成：{title}"}
        gain = quests.complete_task(self.db, int(p["id"]), day)
        return {"ok": True, "gain": gain, "msg": f"+{gain}" if gain else "完成了",
                "undo": {"action": "task.undo", "id": p["id"]}, "log": f"完成：{title}"}

    def do_task_undo(self, p, day):
        from .. import stages
        title = self._title(int(p["id"]))
        back = stages.uncheck(self.db, int(p["id"]), day) if self._stage(int(p["id"])) else quests.uncomplete_task(self.db, int(p["id"]))
        autodone.her_undo(self.db, int(p["id"]))  # 她撤销的，系统不再自动勾回去
        return {"ok": True, "gain": -back, "msg": f"已撤销 −{back}", "log": f"撤销完成：{title}"}

    def do_task_add(self, p, day):
        title = str(p["title"]).strip()
        if not title:
            raise ValueError("要做什么还没填")
        when = p.get("when", "today")  # 今天 = 进今日任务；这周；不急（让计划器排）
        due = {"today": day, "week": day + timedelta(days=6 - day.weekday())}.get(when)
        gid = int(p["goal_id"]) if p.get("goal_id") else None
        tid = self.db.add_task(title, due=due.isoformat() if due else None, goal_id=gid)
        key = str(p.get("territory") or "")
        if not gid:  # 没挂副本的：她选了领地就用她选的，没选就让 小伴 按标题猜，猜不出留在荒原
            key = key if key in territory.NAME or key == territory.WASTE else (territory.guess(title) or "")
            if key:
                self.db.conn.execute("UPDATE tasks SET domain = ? WHERE id = ?",
                                     (key if key in territory.NAME else "waste", tid))
        if gid:  # 挂到副本：算那个副本主线的一步，排在最后
            seq = self.db.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tasks WHERE goal_id = ? AND quest = 'main'",
                                       (gid,)).fetchone()[0]
            self.db.conn.execute("UPDATE tasks SET quest = 'main', size = 'S', seq = ? WHERE id = ?", (seq, tid))
        if when == "today" or p.get("star"):
            self._star(tid, day, True)
        self.db.conn.commit()
        return {"ok": True, "id": tid, "msg": "加上了", "undo": {"action": "task.delete", "id": tid},
                "log": f"加待办：{title}" + ("（今日任务）" if when == "today" or p.get("star") else "")}

    def do_task_delete(self, p, day):
        tid = int(p["id"])
        row = dict(self.db.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone() or {})
        if not row:
            raise ValueError("这件事不在了")
        self.db.delete_task(tid)
        from .. import transitions
        transitions.record(self.db, "todo", tid, "done" if row.get("done") else "open", "deleted", {"title": row.get("title")})
        return {"ok": True, "msg": "删掉了", "undo": {"action": "task.restore", "row": row}, "log": f"删掉待办：{row['title']}"}

    def do_task_restore(self, p, day):
        row = {k: v for k, v in dict(p["row"]).items() if k != "goal"}
        cols = [c["name"] for c in self.db.conn.execute("PRAGMA table_info(tasks)")]
        row = {k: row[k] for k in cols if k in row}
        self.db.conn.execute(f"INSERT INTO tasks ({','.join(row)}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        self.db.conn.commit()
        return {"ok": True, "msg": "恢复了", "log": f"撤销删除：{row.get('title')}"}

    def do_task_postpone(self, p, day):
        tid = int(p["id"])
        old = self.db.conn.execute("SELECT due, star_day FROM tasks WHERE id = ?", (tid,)).fetchone()
        title = self._title(tid)
        self.db.conn.execute("UPDATE tasks SET due = ?, star_day = NULL, hide_day = ? WHERE id = ?",
                             ((day + timedelta(days=1)).isoformat(), day.isoformat(), tid))
        from .. import transitions
        transitions.record(self.db, "todo", tid, "open", "postponed", {"due_before": old["due"]})
        self.db.conn.commit()
        return {"ok": True, "msg": "推到明天了", "undo": {"action": "task.set", "id": tid, "due": old["due"],
                                                       "star_day": old["star_day"], "hide_day": None},
                "log": f"推到明天：{title}"}

    def do_money_ignore(self, p, day):
        on = bool(p.get("on"))
        finance.ignore(self.db, int(p["id"]), on)
        return {"ok": True, "msg": "这一笔不算了" if on else "算回来了", "undo": {"action": "money.ignore", "id": p["id"], "on": "" if on else 1}}

    def do_money_budget(self, p, day):
        """每月预算：只按人民币。改的时候她选本月就生效还是下个月起（2026-11-01 她定；10-29 原来是定了就锁住这个月）。"""
        v = str(p.get("amount") or "").replace(",", "").replace("¥", "").strip()
        try:
            amount = float(v)
        except ValueError:
            return {"ok": False, "msg": "预算要填数字"}
        if amount <= 0:
            return {"ok": False, "msg": "预算要大于 0"}
        month = territory.set_budget(self.db, amount, day, p.get("when"))
        now = day.strftime("%Y-%m") == month
        return {"ok": True, "msg": f"每月预算 ¥{amount:,.0f}，" + ("从这个月开始" if now else f"{int(month[5:])} 月起生效，这个月还按原来的"),
                "log": f"定每月预算 ¥{amount:,.0f}（{month} 起）"}

    def do_goal_set(self, p, day):
        """她在副本详情里改名字、截止日、目标值（2026-10-28 全面检查补）。"""
        gid = int(p["id"])
        row = self.db.conn.execute("SELECT title, due FROM goals WHERE id = ?", (gid,)).fetchone()
        if row is None:
            raise ValueError("没有这个副本")
        title = str(p.get("title") or row["title"]).strip()[:120]
        due = (p.get("due") or None) if "due" in p else row["due"]  # 只改名字时不动截止日（11-01 名字、目标分开改）
        if due:
            date.fromisoformat(due)
        self.db.conn.execute("UPDATE goals SET title = ?, due = ? WHERE id = ?", (title, due, gid))
        if p.get("target") not in (None, ""):
            target = float(p["target"])
            if self.db.conn.execute("SELECT 1 FROM goal_metrics WHERE goal_id = ?", (gid,)).fetchone():
                self.db.conn.execute("UPDATE goal_metrics SET target = ? WHERE goal_id = ?", (target, gid))
        self.db.conn.commit()
        return {"ok": True, "msg": "改好了", "log": f"改副本：{title}（截止 {due or '不限'}）"}

    def do_task_rename(self, p, day):
        title = str(p.get("title") or "").strip()[:200]
        if not title:
            return {"ok": False, "msg": "名字不能空"}
        old = self._title(int(p["id"]))
        self.db.conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (title, int(p["id"])))
        self.db.conn.commit()
        return {"ok": True, "msg": "改好了", "log": f"改待办名字：{old} → {title}"}

    def do_life_log(self, p, day):
        """社交领域自己记一笔：见了朋友 / 认识了新朋友 / 联系了重要的人（她 2026-11-01）。几个人用顿号隔开，一人一条。"""
        from .. import lifelog
        import re as _re
        kind = str(p.get("kind") or "")
        if kind not in ("meet", "new_friend", "contact"):
            raise ValueError("不认识的类型")
        when = date.fromisoformat(p["day"]) if p.get("day") else day
        if when > day:
            return {"ok": False, "msg": "还没到那天"}
        names = [x.strip()[:30] for x in _re.split(r"[、，,；;/]+", str(p.get("who") or "")) if x.strip()]
        if not names:
            return {"ok": False, "msg": "写一下是谁"}
        n = 0
        for name in names:
            n += lifelog.add(self.db, kind, when, name, "她在工作台记的")
            if kind in ("meet", "contact") and self.db.conn.execute("SELECT 1 FROM people WHERE name = ?", (name,)).fetchone():
                self.db.upsert_person(name, last_contact=when.isoformat())
        self.db.conn.commit()
        label = {"meet": "见了", "new_friend": "认识了", "contact": "联系了"}[kind]
        return {"ok": True, "msg": f"记上了：{label}{'、'.join(names)}" if n else "这天已经记过了", "log": f"社交记一笔：{label}{'、'.join(names)}（{when}）"}

    def do_step_add(self, p, day):
        """副本里加一个任务（主线的一步）：只要内容和是不是节点，排在最后（她 2026-11-01：任务和待办不要混在一起）。"""
        gid, title = int(p["goal_id"]), str(p.get("title") or "").strip()[:200]
        if not title:
            return {"ok": False, "msg": "写一下要做什么"}
        if not self.db.goal_exists(gid):
            raise ValueError("没有这个副本")
        seq = self.db.conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM tasks WHERE goal_id = ? AND quest = 'main'", (gid,)).fetchone()[0]
        tid = self.db.add_task(title, goal_id=gid)
        self.db.conn.execute("UPDATE tasks SET quest = 'main', size = 'S', seq = ?, milestone = ? WHERE id = ?",
                             (seq, 1 if p.get("milestone") else 0, tid))
        self.db.conn.commit()
        return {"ok": True, "id": tid, "msg": "加上了", "undo": {"action": "task.delete", "id": tid},
                "log": f"副本加任务：{title}" + ("（节点）" if p.get("milestone") else "")}

    def do_milestone_add(self, p, day):
        from .. import stages
        mid = stages.add_milestone(self.db, int(p["goal_id"]), str(p.get("title") or ""), p.get("due") or None,
                                   float(p["target"]) if str(p.get("target") or "").strip() else None)
        return {"ok": True, "id": mid, "msg": "加上了", "log": f"加里程碑：{p.get('title')}"}

    def do_milestone_set(self, p, day):
        from .. import stages
        stages.set_milestone(self.db, int(p["id"]), p.get("title"), p.get("due", "keep"), p.get("target", "keep"))
        return {"ok": True, "msg": "改好了", "log": f"改里程碑 #{p['id']}"}

    def do_stage_add(self, p, day):
        """当前里程碑里加一件阶段任务：内容、频次（她 2026-11-01 定的新结构）。"""
        from .. import stages
        title = str(p.get("title") or "").strip()
        if not title:
            return {"ok": False, "msg": "写一下要做什么"}
        tid = stages.add_task(self.db, int(p["milestone_id"]), title, str(p.get("cadence") or "once"),
                              int(p["per_week"]) if str(p.get("per_week") or "").strip() else None)
        return {"ok": True, "id": tid, "msg": "加上了", "undo": {"action": "task.delete", "id": tid}, "log": f"加阶段任务：{title}"}

    def do_stage_set(self, p, day):
        """改阶段任务的频次、前置条件。"""
        from .. import stages
        stages.set_task(self.db, int(p["id"]), p.get("cadence") or None,
                        int(p["per_week"]) if str(p.get("per_week") or "").strip() else None,
                        p["depends_on"] if "depends_on" in p else "keep", p["habit_id"] if "habit_id" in p else "keep")
        return {"ok": True, "msg": "改好了", "log": f"改阶段任务：{self._title(int(p['id']))}"}

    def do_step_order(self, p, day):
        """副本里的任务拖动排好的新顺序。只认这个副本主线里的编号。"""
        gid = int(p["goal_id"])
        mine = {r[0] for r in self.db.conn.execute("SELECT id FROM tasks WHERE goal_id = ? AND quest = 'main'", (gid,))}
        ids = [int(x) for x in p.get("ids") or [] if int(x) in mine]
        rest = [r[0] for r in self.db.conn.execute("SELECT id FROM tasks WHERE goal_id = ? AND quest = 'main' ORDER BY seq, id", (gid,)) if r[0] not in ids]
        for n, x in enumerate(ids + rest, 1):
            self.db.conn.execute("UPDATE tasks SET seq = ? WHERE id = ?", (n, x))
        self.db.conn.commit()
        return {"ok": True, "msg": "排好了", "log": "调副本任务顺序"}

    def explore_page(self) -> dict:
        """成就那张表是给成就系统参考的，不在这里编辑（她 2026-11-01）。"""
        from .. import explore
        sheets = {k: v for k, v in explore.load(self.store.root).get("sheets", {}).items() if k != "成就"}
        return {"sheets": sheets, "statuses": {k: explore.statuses(k) for k in sheets}, "domestic": list(explore.DOMESTIC)}

    def do_explore_add(self, p, day):
        from .. import explore
        row = explore.add_row(self.store.root, str(p["sheet"]), str(p.get("name") or ""), str(p.get("sub") or ""),
                              str(p.get("status") or "未开始"), str(p.get("visa") or ""))
        name = row.get("国家") or row.get("城市") or row.get("项目")
        return {"ok": True, "msg": f"加上了 {name}", "log": f"探索地图加一处：{p['sheet']} {name}"}

    def do_explore_delete(self, p, day):
        from .. import explore
        row = explore.delete_row(self.store.root, str(p["sheet"]), int(p["i"]))
        name = row.get("国家") or row.get("城市") or row.get("项目") or ""
        return {"ok": True, "msg": f"删掉了 {name}", "undo": {"action": "explore.add", "sheet": p["sheet"], "name": name,
                "sub": row.get("省份") or row.get("省份/区域") or "", "status": row.get("状态") or "未开始", "visa": row.get("签证") or ""},
                "log": f"探索地图删一处：{p['sheet']} {name}"}

    def do_explore_set(self, p, day):
        from .. import explore
        sheet, i = str(p["sheet"]), int(p["i"])
        if p.get("field"):
            row = explore.set_field(self.store.root, sheet, i, str(p["field"]), str(p.get("value") or ""))
            what = f"{p['field']}：{row.get(p['field']) or '清空了'}"
        else:
            row = explore.set_status(self.store.root, sheet, i, str(p.get("status") or ""))
            what = row["状态"]
        name = row.get("国家") or row.get("城市") or row.get("项目") or ""
        return {"ok": True, "msg": f"{name} {what}", "log": f"改探索地图：{sheet} {name} → {what}"}

    def do_task_move(self, p, day):
        """主线一步往前、往后挪（她 2026-11-01：主副本里要能调顺序）。同一个副本里按 seq 换位置。"""
        tid, d = int(p["id"]), (1 if int(p.get("dir", 1)) > 0 else -1)
        row = self.db.conn.execute("SELECT goal_id FROM tasks WHERE id = ? AND quest = 'main'", (tid,)).fetchone()
        if row is None:
            raise ValueError("这一步不在主线里")
        ids = [r[0] for r in self.db.conn.execute("SELECT id FROM tasks WHERE goal_id = ? AND quest = 'main' ORDER BY seq, id", (row["goal_id"],))]
        i = ids.index(tid)
        if not 0 <= i + d < len(ids):
            return {"ok": True, "msg": "已经到头了"}
        ids[i], ids[i + d] = ids[i + d], ids[i]
        for n, x in enumerate(ids, 1):
            self.db.conn.execute("UPDATE tasks SET seq = ? WHERE id = ?", (n, x))
        self.db.conn.commit()
        return {"ok": True, "msg": "挪好了", "undo": {"action": "task.move", "id": tid, "dir": -d}, "log": f"调主线顺序：{self._title(tid)}"}

    def do_task_milestone(self, p, day):
        """标成节点（里程碑）或取消：节点做完多给分，副本进度也按它算。"""
        tid, on = int(p["id"]), bool(p.get("on"))
        self.db.conn.execute("UPDATE tasks SET milestone = ? WHERE id = ?", (1 if on else 0, tid))
        self.db.conn.commit()
        return {"ok": True, "msg": "标成节点了" if on else "不算节点了", "undo": {"action": "task.milestone", "id": tid, "on": "" if on else 1},
                "log": f"{'标节点' if on else '取消节点'}：{self._title(tid)}"}

    def do_plan_replan(self, p, day):
        runner = getattr(self, "replan_runner", None)
        if runner is None:
            return {"ok": False, "msg": "这里重排不了，跟 小伴 说一声“重排今天”"}
        if not runner():
            return {"ok": True, "msg": "已经在重排了，一分钟后刷新看看"}
        return {"ok": True, "msg": "小伴 在按现在的情况重排今天，约一分钟后刷新", "log": "让 AI 重排了今天的计划"}

    def do_task_set(self, p, day):
        self.db.conn.execute("UPDATE tasks SET due = ?, star_day = ?, hide_day = ? WHERE id = ?",
                             (p.get("due"), p.get("star_day"), p.get("hide_day"), int(p["id"])))
        self.db.conn.commit()
        return {"ok": True, "msg": "恢复了", "log": f"撤销：{self._title(int(p['id']))}"}

    def _star(self, tid: int, day: date, on: bool):
        d = day.isoformat()
        if on:
            self.db.conn.execute("UPDATE tasks SET star_day = ?, hide_day = NULL WHERE id = ?", (d, tid))
        else:
            self.db.conn.execute("UPDATE tasks SET star_day = NULL, hide_day = ? WHERE id = ?", (d, tid))

    def do_task_star(self, p, day):
        tid, on = int(p["id"]), bool(p.get("on"))
        title = self._title(tid)
        self._star(tid, day, on)
        self.db.conn.commit()
        return {"ok": True, "msg": "设为今日任务" if on else "移出今日任务",
                "undo": {"action": "task.star", "id": tid, "on": not on},
                "log": ("设为今日任务：" if on else "移出今日任务：") + title}

    def _plan_id(self, day: date) -> int:
        plan = self.db.latest_plan(day.isoformat())
        if plan is None:
            raise ValueError("今天的计划还没排好")
        return plan["id"]

    def _sync(self, day: date, stale=()) -> str:
        """把当天日程同步到苹果日历。服务器上放后台线程，不让她等（2026-10-27 她嫌加日程太慢）。"""
        if self.calendar is None:
            return ""
        plan = self.db.latest_plan(day.isoformat())
        events = [dict(i) for i in plan["items"] if i["kind"] == "event"]

        def run():
            written = self.calendar.sync_day(day, events, stale_uids=list(stale), places=self.places, remind_before=60)
            self._synced_uids.extend(written.items())

        if self._sync_pool is not None:
            def background():
                try:
                    run()
                except Exception as e:
                    print(f"[workbench] 苹果日历同步失败：{type(e).__name__} {e}", flush=True)
            self._sync_pool.submit(background)
            return "，正在同步苹果日历"
        try:
            run()
            self._save_synced()
            return ""
        except Exception as e:
            return f"（苹果日历没同步上：{type(e).__name__}）"

    def _flush_sync(self):
        """改、删日程前等后台同步做完，拿到苹果日历那边的 uid，免得重复或删不掉。"""
        if self._sync_pool is not None:
            try:
                self._sync_pool.submit(lambda: None).result(timeout=30)
            except Exception:
                pass
        self._save_synced()

    def _save_synced(self):
        while self._synced_uids:
            item_id, uid = self._synced_uids.pop(0)
            if self.db.conn.execute("SELECT 1 FROM plan_items WHERE id = ?", (item_id,)).fetchone():
                self.db.set_calendar_uid(item_id, uid)

    def do_event_add(self, p, day):
        title, start, end = str(p["title"]).strip(), str(p["start"]), str(p.get("end") or "")
        datetime.strptime(start, "%H:%M")
        if not end:
            h, m = map(int, start.split(":"))
            end = f"{min(h + 1, 23):02d}:{m:02d}"
        cur = self.db.conn.execute(
            "INSERT INTO plan_items (plan_id, kind, start, end, place, title) VALUES (?, 'event', ?, ?, ?, ?)",
            (self._plan_id(day), start, end, str(p.get("place") or ""), title))
        self.db.conn.commit()
        from ..planner import note_override
        note_override(self.db, day, title, "add", start, end, str(p.get("place") or ""))
        note = self._sync(day)
        return {"ok": True, "id": cur.lastrowid, "msg": "加了日程" + note,
                "undo": {"action": "event.delete", "id": cur.lastrowid}, "log": f"加日程：{start} {title}"}

    def do_event_delete(self, p, day):
        self._flush_sync()
        row = self.db.conn.execute("SELECT * FROM plan_items WHERE id = ? AND kind = 'event'", (int(p["id"]),)).fetchone()
        if row is None:
            raise ValueError("这个日程不在了")
        self.db.conn.execute("DELETE FROM item_results WHERE item_id = ?", (row["id"],))
        self.db.conn.execute("DELETE FROM plan_items WHERE id = ?", (row["id"],))
        self.db.conn.commit()
        from ..planner import note_override
        note_override(self.db, day, row["title"], "delete")
        note = self._sync(day, stale=[row["calendar_uid"]] if row["calendar_uid"] else [])
        return {"ok": True, "msg": "删掉了日程" + note,
                "undo": {"action": "event.add", "title": row["title"], "start": row["start"], "end": row["end"],
                         "place": row["place"]}, "log": f"删日程：{row['start']} {row['title']}"}

    def do_season_open(self, p, day):
        """她点了“按这个开下个赛季”（点两次确认）：结算这一季、建好新副本（ethan/nextseason.py）。"""
        from .. import nextseason
        got = nextseason.open_next(self.db, self.settings, day)
        return {"ok": True, "msg": f"{got['season']}开始了：建好 {len(got['made'])} 个副本" + (f"，{len(got['paused'])} 个老副本先暂停" if got["paused"] else ""),
                "log": f"开了新赛季：{got['season']}"}

    def do_event_release(self, p, day):
        """她说这个日程“按系统的来”：放开她改过的时间，交给 小伴 重排。"""
        from .. import locks
        title = str(p.get("title") or "").strip()
        if not title:
            raise ValueError("哪个日程")
        locks.release_event(self.db, day, title)
        runner = getattr(self, "replan_runner", None)
        started = bool(runner and runner())
        return {"ok": True, "msg": "改动撤销了" + ("，小伴 在重排" if started else ""), "log": f"撤销日程改动：{title}"}

    def do_calendar_delete(self, p, day):
        """删她自己苹果日历上的约（她在“日历 · 改计划”里点了删）。只认今天读到的、不是重复的那几条。"""
        href = str(p.get("href") or "")
        hit = next((b for b in (self._apple_busy(day) or []) if len(b) > 3 and (b[3] or {}).get("href") == href), None)
        if hit is None:
            raise ValueError("这个约不在今天的日历里了")
        if hit[3].get("recurring"):
            return {"ok": False, "msg": "这是重复的约，在日历 App 里删这一次，免得整串都删掉"}
        self.calendar.delete_event(href)
        c = getattr(self, "_busy_cache", None)
        if c and c[0] == day.isoformat():  # 直接从缓存里去掉，不用再等一次 iCloud
            self._busy_cache = (c[0], c[1], [b for b in c[2] if not (len(b) > 3 and (b[3] or {}).get("href") == href)])
        return {"ok": True, "msg": f"从你的苹果日历里删掉了「{hit[2]}」", "log": f"删她日历上的约：{hit[0]} {hit[2]}"}

    def do_future_delete(self, p, day):
        """删掉邮件 / 聊天里记下的那个约：本地删，苹果日历“小伴 计划”里那份也删（她 2026-11-02：两家饭店撞了要删一家）。"""
        r = self.db.conn.execute("SELECT * FROM future_events WHERE id = ?", (int(p["id"]),)).fetchone()
        if r is None:
            raise ValueError("这个日程不在了")
        self.db.conn.execute("DELETE FROM future_events WHERE id = ?", (r["id"],))
        self.db.conn.execute("CREATE TABLE IF NOT EXISTS deleted_future (day TEXT, title TEXT)")
        self.db.conn.execute("INSERT INTO deleted_future (day, title) VALUES (?, ?)", (r["day"], r["title"]))  # 首页时间线据此去掉早上算进去的那条
        self.db.conn.commit()
        note = ""
        if r["uid"] and self.calendar is not None:
            try:
                import urllib.parse
                self.calendar.request("DELETE", urllib.parse.urljoin(self.calendar.ethan_calendar(), f"{r['uid']}.ics"))
                note = "，苹果日历里也删了"
            except Exception as e:
                note = f"，苹果日历里没删掉（{type(e).__name__}），在日历 App 里手动删一下"
        return {"ok": True, "msg": "删掉了" + note, "log": f"删日程：{r['day']} {r['start']} {r['title']}"}

    def do_event_time(self, p, day):
        self._flush_sync()
        row = self.db.conn.execute("SELECT * FROM plan_items WHERE id = ? AND kind = 'event'", (int(p["id"]),)).fetchone()
        if row is None:
            raise ValueError("这个日程不在了")
        start = str(p["start"])
        datetime.strptime(start, "%H:%M")
        h, m = map(int, row["start"].split(":"))
        h2, m2 = map(int, (row["end"] or row["start"]).split(":"))
        length = max(0, (h2 * 60 + m2) - (h * 60 + m))
        sh, sm = map(int, start.split(":"))
        e = min(sh * 60 + sm + length, 23 * 60 + 59)
        self.db.conn.execute("UPDATE plan_items SET start = ?, end = ? WHERE id = ?", (start, f"{e // 60:02d}:{e % 60:02d}", row["id"]))
        self.db.conn.commit()
        from ..planner import note_override  # 重排时照她改的时间（她 2026-11-02：改了训练时间，不知道重排会不会改回去）
        note_override(self.db, day, row["title"], "time", start, f"{e // 60:02d}:{e % 60:02d}")
        note = self._sync(day)
        return {"ok": True, "msg": "改好了" + note, "undo": {"action": "event.time", "id": row["id"], "start": row["start"]},
                "log": f"改日程时间：{row['title']} {row['start']} → {start}"}

    def do_card_make(self, p, day):
        if self.card(day):
            return {"ok": True, "msg": "今天的卡已经有了"}
        if self.card_runner is None:
            return {"ok": False, "msg": "这里画不了今日卡"}
        started = self.card_runner()
        return {"ok": True, "msg": "正在画今天的卡，大约半分钟" if started else "还在画，稍等",
                "log": "让 小伴 现画今日卡" if started else None}

    def do_card_wish(self, p, day):
        """她想要的卡（场景、动作、视角、cosplay）：排进队里，下一张普通卡照这个画；now=1 就马上重画今天（2026-10-30）。"""
        text = str(p.get("text") or "").strip()
        if not text:
            return {"ok": False, "msg": "写一句想要什么样的卡"}
        cards.add_wish(self.db, text, now=bool(p.get("now")))
        if p.get("now"):
            if self.redo_runner is None:
                return {"ok": False, "msg": "这里重画不了，明天照这个画"}
            started = self.redo_runner()
            return {"ok": True, "msg": "正在按你说的重画今天的卡，大约半分钟" if started
                    else "上一张还在画，画完马上按你这条（和之前没画的几条一起）再重画一次",
                    "log": f"今日卡重画：{text[:30]}"}
        return {"ok": True, "msg": "记下了，下一张卡照这个画", "log": f"想要的卡：{text[:30]}"}

    def do_card_wishdrop(self, p, day):
        ok = cards.drop_wish(self.db, int(p["id"]))
        return {"ok": ok, "msg": "删掉了" if ok else "这条已经画过了"}

    def do_rest_take(self, p, day):
        ok, msg = quests.take_rest_day(self.db, day)
        return {"ok": ok, "msg": msg, "log": "开休整日" if ok else None}

    def do_rest_cancel(self, p, day):
        quests.cancel_rest_day(self.db, day)
        return {"ok": True, "msg": "取消了今天的休整", "log": "取消休整日"}

    def do_agent_drop(self, p, day):
        """她叫停一件活：不算失败，不扣分。"""
        try:
            t = agents.drop(self.db, int(p["id"]))
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "好，这件不做了", "log": f"叫停：{t['title']}"}

    def do_agent_misreport(self, p, day):
        """“待你审查”里其实不用她审的（误报）：直接流转到下一步“通过”（2026-10-30 她要）。"""
        if p.get("key"):
            ok = agents.dismiss_session(self.store.root, str(p["key"]))
            return {"ok": ok, "msg": "知道了，是误报，拿掉了" if ok else "这个会话找不到了", "log": "团队页：一个会话标成误报" if ok else None}
        try:
            t = agents.review(self.db, int(p["id"]), "accept", "误报：不用她审，直接流转下一步")
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "是误报，转到“通过”了", "log": f"误报流转：{t['title']}"}

    def do_agent_fail(self, p, day):
        """她看了判失败：记成失败（算进成绩单）。"""
        if p.get("key"):
            agent = str(p.get("agent") or "claude")
            tid = agents.fail_session(self.db, self.store.root, str(p["key"]), str(p.get("title") or ""),
                                      agent if agent in agents.AGENTS else "claude")
            return {"ok": True, "msg": "记成失败了", "log": f"团队页：会话判失败 #{tid}"}
        try:
            t = agents.review(self.db, int(p["id"]), "fail")
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "记成失败了", "log": f"判失败：{t['title']}"}

    def do_agent_archive(self, p, day):
        """她直接归档：失败的收进归档；等审查的不审了，算放弃再归档（2026-10-30 她要）。"""
        try:
            t = agents.archive(self.db, int(p["id"]))
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "归档了", "log": f"归档：{t['title']}"}

    def do_agent_rate(self, p, day):
        r = int(p.get("rating", 0))
        agents.rate(self.db, int(p["id"]), r)
        return {"ok": True, "msg": f"{r} 分，记下了" if r else "评分清掉了"}

    def do_reward_delivered(self, p, day):
        """她在兑换记录里点“兑现了”（小伴 不一定记得写 [惊喜 已兑现]，2026-11-01 她看到全是待兑现）。"""
        ok = quests.deliver(self.db, int(p["id"]), (p.get("note") or "她在工作台确认兑现了")[:200])
        return {"ok": ok, "msg": "记成已兑现" if ok else "这条已经兑现过了", "log": f"兑换 #{p['id']} 已兑现" if ok else None}

    def do_reward_redeem(self, p, day):
        ok, msg = quests.redeem(self.db, int(p["id"]), day)
        return {"ok": ok, "msg": msg, "log": msg if ok else None}

    def do_today_order(self, p, day):
        ids = [int(i) for i in p.get("ids") or []]
        self.db.conn.execute(TODAY_ORDER)
        self.db.conn.execute("DELETE FROM today_order WHERE day = ?", (day.isoformat(),))
        self.db.conn.executemany("INSERT INTO today_order (day, task_id, pos) VALUES (?, ?, ?)",
                                 [(day.isoformat(), t, n) for n, t in enumerate(ids)])
        self.db.conn.commit()
        return {"ok": True, "msg": "按你排的来"}

    def do_prep_star(self, p, day):
        on = bool(p.get("on"))
        prep.set_star(self.db, int(p["id"]), on)
        return {"ok": True, "msg": "点亮了，今天先做这件" if on else "星星取消了"}

    def do_prep_drop(self, p, day):
        """“不要了”先进回收站（2026-10-29 她误点过），能恢复。"""
        title = prep.drop(self.db, int(p["id"]))
        tid = self.db.conn.execute("SELECT MAX(id) FROM prep_trash").fetchone()[0]
        return {"ok": True, "msg": "放进回收站了", "undo": {"action": "prep.restore", "id": tid}, "log": f"不要 小伴 准备：{title}"}

    def do_prep_done(self, p, day):
        ok = prep.mark_done(self.db, int(p["id"]))
        return {"ok": ok, "msg": "这件算做完了" if ok else "这件已经结束了", "log": f"小伴 准备的事直接算完成：{self._title(int(p['id']))}" if ok else None}

    def do_prep_restore(self, p, day):
        try:
            title = prep.restore(self.db, int(p["id"]))
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "放回来了", "log": f"恢复 小伴 准备：{title}"}

    def do_prep_order(self, p, day):
        ids = [int(x) for x in p.get("ids") or []]
        prep.set_order(self.db, ids)
        return {"ok": True, "msg": "顺序记下了，点星的还是最先做", "log": "调整 小伴 准备的顺序"}

    def prep_trash(self) -> list:
        """回收站；以前点“不要了”直接删掉的，按工作台日志捞回来（2026-10-29 她误删了一件）。"""
        try:
            lines = (Path(self.store.root) / LOG).read_text(encoding="utf-8").split("\n")[-500:]
            titles = [json.loads(l)["text"].split("不要 小伴 准备：", 1)[1] for l in lines if "不要 小伴 准备：" in l]
            prep.adopt_legacy(self.db, titles)
        except (OSError, ValueError, KeyError, IndexError):
            pass
        return prep.trash(self.db)


def collection(root):
    return cards.collection(root)
