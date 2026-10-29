"""工作台的数据和操作。页面每次刷新拿一份完整的 state；她的每个操作走 act()，改完立即生效，能撤销。

数字都由代码从数据库算（和 Telegram 今日卡片同一套：quests、year_progress、计划的时间线），不经过模型。
她在工作台上改了什么记进 <数据目录>/workbench-log.jsonl，小伴 下一轮聊天就知道（见 recent_changes）。
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import achievements, autodone, finance, jobs, agents, brain, brief_card, cards, meals, places_log, prep, quests, review, territory, training, worktime
from ..clock import her_now, DEFAULT_TZ
from ..planner import goal_card
from ..progress import year_progress

MAIN_BOSS = "减脂"  # 本赛季主副本（2026-10-27 她定），今日任务优先排它
SEASON = {"name": "2026 冬季 · 城市篇", "start": "2026-10-13", "end": "2026-12-31"}
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


class Workbench:
    def __init__(self, store, settings, calendar=None, places=None, background=False, card_runner=None, replan_runner=None):
        """background：苹果日历放到后台线程同步（服务器上开，测试里关）；card_runner()：现画今日卡，返回是否开始画了。"""
        self.store, self.db, self.settings = store, store.db, settings
        self.calendar, self.places = calendar, places or {}
        self.card_runner, self.replan_runner = card_runner, replan_runner
        self._sync_pool = ThreadPoolExecutor(max_workers=1) if background and calendar is not None else None
        self._synced_uids = []  # 后台同步写回的 (日程 id, uid)，下次请求时在主线程存进数据库

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
        season_end = date.fromisoformat(SEASON["end"])
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
            "profile": {
                "level": g["level"], "title": g["title"], "earned": g["earned"], "balance": g["balance"],
                "next_level_at": g["next_level_at"], "streak": g["streak"]["days"],
                "day_n": (day - date.fromisoformat(start)).days + 1 if start else None,
                "next_reward": {"title": nxt["title"], "cost": nxt["cost"], "gap": nxt["cost"] - g["balance"]} if nxt else None,
            },
            "season": {**SEASON, "days_left": max(0, (season_end - day).days),
                       "pct": round(100 * sum(fracs) / len(fracs)) if fracs else 0,
                       "line": f"主副本{main['name']}：{main['status'] or main['line']}" if main else ""},
            "timeline": brief.get("timeline", []),
            "plan_ready": plan is not None,
            "detail": {k: brief_card.detail(plan, k) for k in ("train", "meals")} if plan else {},
            "meals": meals.parse_text(brief.get("meals_text")),
            "training": self.training_today(day, plan),
            **self.today_tasks(day, plan),
            "habits": {"daily": [{**h, "easy": self._easy(h["id"], day)} for h in g["daily"]],
                       "weekly": self._weekly(g, day), "rest_day": quests.is_rest_day(db, day),
                       "rest_left": quests.rest_days_left(db, day)},
            "bosses": bosses,
            "prep_trash": self.prep_trash(),
            "prep_today": prep.today_order(db),
            "prep_routines": [r["title"] for r in db.conn.execute("SELECT title FROM tasks WHERE done = 0 AND promised_by_ethan = 1 ORDER BY id")
                              if prep.ROUTINE.search(r["title"])],
            "prep": [{k: x.get(k) for k in ("id", "title", "goal", "season", "state", "state_label", "summary", "decision", "star")}
                     | {"result": (prep.detail(db, x["id"]) or {}).get("result")} for x in prep.queue(db)],
            "card": card,
            "card_making": bool(self.card_runner and getattr(self.card_runner, "busy", lambda: False)()),
            "planner": self.plan_panel(day, plan),
            "goals": self.goal_options(),
        }

    def _weekly(self, g: dict, day: date) -> list:
        """周常：达标后还能接着做（超额每次 +20），today 是今天做过没有（点了就撤销今天这次）。"""
        return [{**h, "today": quests.checked(self.db, h["id"], day)} for h in g["weekly"]]

    def _easy(self, habit_id: int, day: date) -> bool:
        r = self.db.conn.execute("SELECT easy FROM habit_log WHERE habit_id = ? AND day = ?",
                                 (habit_id, day.isoformat())).fetchone()
        return bool(r and r["easy"])

    def bosses(self, day: date) -> list:
        mains = quests.main_quests(self.db)
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
            if frac is None and steps:
                frac = round(sum(s["done"] for s in steps) / len(steps), 2)
            nxt = next((s for s in steps if not s["done"]), None)
            out.append({"goal_id": p["goal_id"], "name": c["name"], "line": c["line"], "status": c["status"],
                        "frac": frac, "main": MAIN_BOSS in c["name"], "next": nxt["title"] if nxt else None,
                        "comment": review.get(str(p["goal_id"])), "due": dues.get(p["goal_id"]), "title": titles.get(p["goal_id"]),
                        "target": (metrics.get(p["goal_id"]) or {}).get("target"), "unit": (metrics.get(p["goal_id"]) or {}).get("unit")})
        return sorted(out, key=lambda b: not b["main"])

    def goal_options(self) -> list:
        goals = {r["id"]: r["title"] for r in self.db.conn.execute("SELECT id, title FROM goals")}
        return [{"id": gid, "name": goals.get(gid, "").split("：")[0]} for gid in quests.main_quests(self.db)]

    def today_tasks(self, day: date, plan) -> dict:
        """今日任务（不限件数，2026-10-27 她定）：没做完的在前（★ 的、主副本减脂的主线、别的主线、支线），做完的排最后。"""
        db, d = self.db, day.isoformat()
        due = {r["id"] for r in db.conn.execute("SELECT id FROM tasks WHERE due IS NOT NULL AND due <= ?", (d,))}
        # 计划里列着所有没做完的事；算“今天”的只有：每个副本的下一步、截止到今天的、计划器今天新建的（如冷启动），加上她标 ★ 的
        ids = [i["task_id"] for i in (plan or {}).get("items", [])
               if i["kind"] == "todo" and i.get("task_id") and i.get("priority") != "ethan"
               and ((i.get("priority") == "main" and i.get("how") == "下一步") or i.get("task_created") or i["task_id"] in due)]
        ids += [r["id"] for r in db.conn.execute("SELECT id FROM tasks WHERE star_day = ?", (d,))]
        rows = {r["id"]: dict(r) for r in db.conn.execute(
            f"SELECT t.*, g.title AS goal FROM tasks t LEFT JOIN goals g ON g.id = t.goal_id "
            f"WHERE t.id IN ({','.join('?' * len(ids)) or 'NULL'}) AND t.promised_by_ethan = 0 "
            "AND (t.goal_id IS NULL OR COALESCE(g.status, 'active') = 'active' OR t.star_day = ?)", (*ids, d))}  # 暂停副本的步骤不自动进今天，她点了 ★ 照样进
        done_today = {r[0] for r in db.conn.execute("SELECT ref FROM points WHERE day = ? AND ref LIKE 'task:%'", (d,))}
        tasks = [rows[i] for i in dict.fromkeys(ids) if i in rows and rows[i].get("hide_day") != d
                 and (not rows[i]["done"] or f"task:{i}" in done_today or (rows[i]["done_at"] or "")[:10] >= d)]

        db.conn.execute(TODAY_ORDER)
        pos = {r[0]: r[1] for r in db.conn.execute("SELECT task_id, pos FROM today_order WHERE day = ?", (d,))}

        def rank(t):  # 她拖过的按她排的（2026-10-29 她要今日任务也能拖），没拖过的跟在后面按原来的规矩
            return (bool(t["done"]), t["id"] not in pos, pos.get(t["id"], 0), t.get("star_day") != d,
                    not (t.get("quest") == "main" and MAIN_BOSS in (t.get("goal") or "")), t.get("quest") != "main")
        tasks.sort(key=rank)
        return {"today": [self._task(t, d) for t in tasks]}

    def _task(self, t: dict, d: str) -> dict:
        main = t.get("quest") == "main"
        tag = ("★ " if main and MAIN_BOSS in (t.get("goal") or "") else "") + ((t.get("goal") or "").split("：")[0] if main else "支线")
        return {"id": t["id"], "title": t["title"], "done": bool(t["done"]), "points": quests.task_points(t),
                "tag": tag, "main": main, "hard": bool(t.get("hard")), "star": t.get("star_day") == d,
                "progress": None if t["done"] else quests.task_progress(self.db, t, date.fromisoformat(d))}

    def plan_panel(self, day: date, plan) -> dict:
        d = day.isoformat()
        events = []
        for i in (plan or {}).get("items", []):
            if i["kind"] != "event" or not i.get("start"):
                continue
            leave = None
            if i.get("travel_min"):
                h, m = map(int, i["start"].split(":"))
                t = h * 60 + m - i["travel_min"]
                leave = f"{t // 60:02d}:{t % 60:02d}"
            events.append({"id": i["id"], "start": i["start"], "end": i.get("end"), "title": i["title"],
                           "place": i.get("place") or "", "leave": leave})
        today_ids = {t["id"] for t in self.today_tasks(day, plan)["today"]}
        todos = [dict(r) for r in self.db.conn.execute(
            "SELECT t.id, t.title, t.due, t.quest, t.star_day, g.title AS goal FROM tasks t "
            "LEFT JOIN goals g ON g.id = t.goal_id WHERE t.done = 0 AND t.promised_by_ethan = 0 "
            "AND (COALESCE(t.quest, 'side') != 'main' OR t.id IN (%s)) AND (t.due IS NULL OR t.due <= ?) "
            "ORDER BY t.sort IS NULL, t.sort, t.id" % (",".join(map(str, today_ids)) or "NULL"), (d,))]
        for t in todos:
            t["star"] = t["id"] in today_ids
            t["sub"] = (t.get("goal") or "").split("：")[0] if t.get("quest") == "main" else "支线"
        skipped = (json.loads(plan["brief"]).get("skipped") or []) if plan else []
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

    def territory_page(self, key: str, now: datetime = None) -> dict:
        now = now or self.now()
        day = now.date()
        if key == territory.WASTE:
            _, tasks = self._territory_parts(day)
            rows = [t for t in tasks if t["territory"] is None]
            return {"key": key, "name": "荒原", "tasks": [{"id": t["id"], "title": t["title"], "due": t["due"],
                                                         "guess": territory.guess(t["title"])} for t in rows],
                    "options": [{"key": k, "name": n} for k, n, _ in territory.TERRITORIES]}
        if key not in territory.NAME:
            raise ValueError("没有这个区域")
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
        if key == "money":  # 每一笔都看得到（2026-10-29 她说财务页看不到刚才那笔）
            out["transactions"] = finance.recent(self.db, 30)
            out["budget"] = territory.budget_plan(self.db, day)
            out["fx"] = finance.fx_rate(self.db)
        if key == "body":
            out["weights"] = territory._weights(self.db, day, 28)
        if key == "life":
            out["explore"] = territory.explore_stats(self.store.root)
        return out

    # ---------- 战报、设置 ----------
    def review_page(self, now: datetime = None) -> dict:
        day = (now or self.now()).date()
        start = max(SEASON["start"], self.settings.get("cold_start_from") or SEASON["start"])  # 她说从 10-28 开始算
        bosses, _ = self._territory_parts(day)
        return review.page(self.db, self.store.root, day, {**SEASON, "start": start}, bosses)

    def do_review_feedback(self, p, day):
        note = str(p.get("note", "")).strip()
        if not note:
            return {"ok": False, "msg": "写一句再发"}
        self.db.add_feedback("战报反馈：" + note[:500])
        return {"ok": True, "msg": "收到，下周排计划照这个来", "log": f"给战报写了反馈：{note[:60]}"}

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
            {"name": "Mac 工作时长", "ok": worktime.has_data(db), "detail": "事业区域的工作时长上限", "fix": "Mac 上跑一次 python3 -m ethan.worktime --install"},
            {"name": "小红书 / Instagram 账号", "ok": False, "detail": "发帖数、粉丝", "fix": "等接账号"}]
        plan = db.latest_plan(day.isoformat())
        try:
            dep = json.loads((root / "deploy-status.json").read_text())
        except (OSError, ValueError):
            dep = {}
        hooks = agents.load_sessions(root).get("at") or ""
        health = [
            {"name": "每日计划", "ok": plan is not None, "detail": "今天的计划排好了" if plan else "今天还没排（每天 06:30）"},
            {"name": "今日卡", "ok": (root / "cards" / f"{day.isoformat()}.json").exists(), "detail": "今天的卡画好了" if (root / "cards" / f"{day.isoformat()}.json").exists() else "今天还没画"},

            {"name": "自动部署", "ok": bool(dep) and str(dep.get("result", "")).startswith("已上线"),  # autodeploy 写的是“已上线 / 失败，……”
             "detail": (f"{fmt(dep.get('at', ''))} · {str(dep.get('to', ''))[:7]} · {dep.get('result', '')}" if dep else "看不到部署记录")},
            {"name": "Claude 会话报到", "ok": bool(hooks), "detail": f"最近一次 {fmt(hooks)}" if hooks else "还没有会话报到"},
            {"name": "Grok 回报", "ok": bool(latest("inbox")), "detail": f"最近一次 {latest('inbox') or '没有'}"},
            *jobs.health(root)]  # 各定时任务：上次成功没有、该跑没跑（ethan/jobs.py）
        return {"prefs": {"proactive": self.settings.get("proactive_enabled", True) is not False,
                          "quiet": self.settings.get("quiet_hours", "00:00-08:00"), "timezone": self.settings.get("timezone", DEFAULT_TZ)},
                "connections": conns, "health": health}

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
    def cards_page(self) -> dict:
        out = []
        for c in cards.collection(self.store.root):
            if c.get("image"):
                out.append({k: c.get(k) for k in ("day", "title", "rarity", "reason", "line", "weather")}
                           | {"image": f"/img/card/{c['day']}",
                              "kind": {"E": "节点", "S": "训练"}.get((c.get("code") or "W")[0], "日常")})
        return {"cards": out}

    def do_ach_seen(self, p, day):
        achievements.mark_shown(self.db, [str(k) for k in p.get("keys") or []])
        return {"ok": True}

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
        out["main"] = sum(1 for x in prep.queue(self.db) if x["state"] == "ready")  # 小伴 准备好、等她过目的
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

    def do_prep_revise(self, p, day):
        ok = prep.revise(self.db, int(p["id"]), str(p.get("note", "")))
        return {"ok": ok, "msg": "收到，小伴 会按你的意见重做" if ok else "写一句要怎么改", "log": "让 小伴 改准备的东西" if ok else None}

    def do_brain_purge(self, p, day):
        ok = brain.purge(self.db, int(p["id"]))
        return {"ok": ok, "msg": "永久删除了" if ok else "只能删回收站里的"}

    def do_task_domain(self, p, day):
        """待办归到哪块领地（荒原里确认 小伴 猜的，或改掉）；waste 表示先放荒原。"""
        tid, key = int(p["id"]), str(p.get("key", ""))
        if key not in territory.NAME and key != territory.WASTE:
            raise ValueError("没有这个区域")
        title = self._title(tid)
        self.db.conn.execute("UPDATE tasks SET domain = ? WHERE id = ?", (key if key in territory.NAME else "waste", tid))
        self.db.conn.commit()
        name = territory.NAME.get(key, "荒原")
        return {"ok": True, "msg": f"归到{name}了", "log": f"待办归到{name}：{title}"}

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
                ("failed", "失败 · 放弃（7 天）", "status IN ('failed', 'dropped') AND finished_at >= ?", (week,))):
            rows = db.conn.execute(f"SELECT {', '.join(agents.LIST_FIELDS)}, review_note, spec FROM agent_tasks "
                                   f"WHERE {sql} ORDER BY id DESC LIMIT 30", args)
            cols.append({"key": key, "label": label, "tasks": [dict(r) for r in rows]})
        archive = [dict(r) for r in db.conn.execute(
            f"SELECT {', '.join(agents.LIST_FIELDS)}, review_note, spec FROM agent_tasks WHERE (status = 'accepted' AND rating IS NOT NULL) "
            "OR (status IN ('failed', 'dropped') AND finished_at < ?) ORDER BY COALESCE(reviewed_at, finished_at) DESC LIMIT 100", (week,))]
        # 系统之外也在干的活（2026-10-29 她说团队页漏了很多）：Claude 会话（钩子报到）、准备员（算 小伴 的）、Grok（派出去还没回的）
        sessions = agents.load_sessions(self.store.root)
        live = [{"agent": x.get("agent", "claude"), "title": x["title"],
                 "sub": f"会话 · {x['where_label']}", "at": x["updated_at"],
                 "state": "running" if x["state"] == "working" else "review", "summary": x.get("summary", ""),
                 "status_label": x.get("state_label", ""), "detail": x.get("summary", ""), "detail_label": "它最近说的一段"}
                for x in sessions.get("sessions", []) if x["state"] in ("working", "blocked", "review_ready")]
        # 排队 = 资源不够轮不上（准备员每次做 2 件、每天有上限；Codex 等额度）或者前置没做完；不放“等她审批”的（2026-10-29 她定）
        waiting = 0
        today = set(prep.today_ids(db))
        for x in prep.queue(db):
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
        goals = {r["id"]: r["title"] for r in db.conn.execute("SELECT id, title FROM goals")}
        chapters = []
        for gid, steps in quests.main_quests(db).items():
            nxt = next((s["id"] for s in steps if not s["done"]), None)
            chapters.append({"goal_id": gid, "name": goals.get(gid, "").split("：")[0],
                             "main": MAIN_BOSS in goals.get(gid, ""),
                             "steps": [{"id": s["id"], "title": s["title"], "points": quests.task_points(s),
                                        "state": "done" if s["done"] else "now" if s["id"] == nxt else "locked",
                                        "hard": bool(s.get("hard")), "milestone": bool(s["milestone"]),
                                        "draft": bool(s["draft"]),
                                        "progress": None if s["done"] else quests.task_progress(db, s, day)} for s in steps]})
        chapters.sort(key=lambda c: not c["main"])
        week = [day - timedelta(days=n) for n in range(6, -1, -1)]
        dailies = [{"id": h["id"], "title": h["title"],
                    "week": [quests.checked(db, h["id"], d) for d in week]}
                   for h in quests.habits(db) if h["cadence"] == "daily"]
        g = quests.today_summary(db, day)
        return {
            "chapters": chapters,
            "side": [{"id": q["id"], "title": q["title"], "points": quests.task_points(q), "progress": quests.task_progress(db, q, day)}
                     for q in quests.side_quests(db)],
            "later": [{"id": q["id"], "title": q["title"], "goal": q["goal"].split("：")[0], "points": quests.task_points(q)}
                      for q in quests.later_quests(db)],
            "dailies": dailies, "weekly": self._weekly(g, day), "streak": g["streak"],
            "rest_day": quests.is_rest_day(db, day), "rest_left": quests.rest_days_left(db, day),
            "rewards": [{**r, "affordable": g["balance"] >= r["cost"]} for r in quests.rewards(db)],
            "balance": g["balance"],
            "pace": round(db.conn.execute(  # 最近 7 天平均每天挣多少分，商店用来估“还要几天”
                "SELECT COALESCE(SUM(delta), 0) FROM points WHERE delta > 0 AND day > ? AND day <= ?",
                ((day - timedelta(days=7)).isoformat(), day.isoformat())).fetchone()[0] / 7),
            "history": [dict(r) for r in db.conn.execute(
                "SELECT x.at, x.status, x.cost, x.note, w.title FROM redemptions x JOIN rewards w ON w.id = x.reward_id "
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
        try:
            if action == "task.undo":
                redo = ("task_done", {"id": p["id"]})
            elif action == "habit.uncheck":
                redo = ("habit_check", {"id": p["id"], "easy": self._easy(int(p["id"]), day)})
            out = handler(p, day)
        except (KeyError, ValueError, TypeError) as e:
            return {"ok": False, "msg": f"参数不对：{e}"}
        if redo and quests.totals(self.db)["balance"] < 0:  # 分已经拿去兑换了，撤销会让余额变负：退回去，不让撤（2026-10-27）
            getattr(self, "do_" + redo[0])(redo[1], day)
            return {"ok": False, "msg": "这些分已经用来兑换了，撤销不了"}
        if out.get("ok") and out.get("log"):
            self.log(out.pop("log"), now)
        out.pop("log", None)
        return out

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
        gain = quests.check_habit(self.db, int(p["id"]), day, easy=bool(p.get("easy")))
        return {"ok": True, "gain": gain, "msg": f"+{gain}" if gain else "今天已经打过卡了",
                "undo": {"action": "habit.uncheck", "id": p["id"]},
                "log": f"打卡：{title}" + ("（简单版）" if p.get("easy") else "")}

    def do_habit_uncheck(self, p, day):
        title = self._habit(int(p["id"]))
        back = quests.uncheck_habit(self.db, int(p["id"]), day)
        return {"ok": True, "gain": -back, "msg": f"已撤销 −{back}", "log": f"撤销打卡：{title}"}

    def do_task_done(self, p, day):
        title = self._title(int(p["id"]))
        gain = quests.complete_task(self.db, int(p["id"]), day)
        return {"ok": True, "gain": gain, "msg": f"+{gain}" if gain else "完成了",
                "undo": {"action": "task.undo", "id": p["id"]}, "log": f"完成：{title}"}

    def do_task_undo(self, p, day):
        title = self._title(int(p["id"]))
        back = quests.uncomplete_task(self.db, int(p["id"]))
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
        self.db.conn.commit()
        return {"ok": True, "msg": "推到明天了", "undo": {"action": "task.set", "id": tid, "due": old["due"],
                                                       "star_day": old["star_day"], "hide_day": None},
                "log": f"推到明天：{title}"}

    def do_money_ignore(self, p, day):
        on = bool(p.get("on"))
        finance.ignore(self.db, int(p["id"]), on)
        return {"ok": True, "msg": "这一笔不算了" if on else "算回来了", "undo": {"action": "money.ignore", "id": p["id"], "on": "" if on else 1}}

    def do_money_budget(self, p, day):
        """每月预算：只按人民币，定了就锁住这个月，改的从下个月起生效（2026-10-29 她定：预算是要遵守的）。"""
        v = str(p.get("amount") or "").replace(",", "").replace("¥", "").strip()
        try:
            amount = float(v)
        except ValueError:
            return {"ok": False, "msg": "预算要填数字"}
        if amount <= 0:
            return {"ok": False, "msg": "预算要大于 0"}
        month = territory.set_budget(self.db, amount, day)
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
        due = p.get("due") or None
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
        note = self._sync(day, stale=[row["calendar_uid"]] if row["calendar_uid"] else [])
        return {"ok": True, "msg": "删掉了日程" + note,
                "undo": {"action": "event.add", "title": row["title"], "start": row["start"], "end": row["end"],
                         "place": row["place"]}, "log": f"删日程：{row['start']} {row['title']}"}

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

    def do_rest_take(self, p, day):
        ok, msg = quests.take_rest_day(self.db, day)
        return {"ok": ok, "msg": msg, "log": "开休整日" if ok else None}

    def do_agent_drop(self, p, day):
        """她叫停一件活：不算失败，不扣分。"""
        try:
            t = agents.drop(self.db, int(p["id"]))
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        return {"ok": True, "msg": "好，这件不做了", "log": f"叫停：{t['title']}"}

    def do_agent_rate(self, p, day):
        r = int(p.get("rating", 0))
        agents.rate(self.db, int(p["id"]), r)
        return {"ok": True, "msg": f"{r} 分，记下了" if r else "评分清掉了"}

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
