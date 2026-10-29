"""结构化记忆：目标树、任务、进度日志、学习条目、人物卡、心愿清单。

目标树是一条链（2026-10-26 Mia 定）：她先定 80 岁时的样子，再倒推每一年，把每年拆到各方面。
  vision 80 岁画像 → long 画像的各个方面 → year 今年的目标 → stage 阶段目标 → tasks 表里的每日待办
"""

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY,
    parent_id INTEGER REFERENCES goals(id),
    level TEXT NOT NULL CHECK (level IN ('vision', 'long', 'year', 'stage', 'task')),
    title TEXT NOT NULL,
    motivation TEXT,
    success_criteria TEXT,
    status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (status IN ('not_started', 'active', 'paused', 'done', 'dropped')),
    next_step TEXT,
    due TEXT,
    domain TEXT,
    source TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS progress_log (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    note TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER REFERENCES goals(id),
    title TEXT NOT NULL,
    due TEXT,
    done INTEGER NOT NULL DEFAULT 0,
    promised_by_ethan INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER REFERENCES goals(id),
    note TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'ethan',
    created_at TEXT NOT NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS learning_items (
    id INTEGER PRIMARY KEY,
    subject TEXT NOT NULL,
    content TEXT NOT NULL,
    note TEXT,
    interval_days INTEGER NOT NULL DEFAULT 1,
    ease REAL NOT NULL DEFAULT 2.5,
    last_review TEXT,
    next_review TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    circle TEXT,
    birthday TEXT,
    how_met TEXT,
    notes TEXT,
    contact_every_days INTEGER,
    last_contact TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS wishes (
    id INTEGER PRIMARY KEY,
    quote TEXT NOT NULL,
    context TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done', 'dropped')),
    created_at TEXT NOT NULL,
    done_at TEXT
);
"""

# 第一阶段（设计 docs/design/mias-os.md 第 4 节）：量化指标、身体记录、每日计划、结果、运行留痕。
# 指标单独成表，不往 goals 加列，免得和 year 层的重建迁移冲突。
PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS goal_metrics (
    goal_id INTEGER PRIMARY KEY REFERENCES goals(id),
    kind TEXT NOT NULL CHECK (kind IN ('weight_avg7', 'count_week', 'count_total', 'funnel', 'launch')),
    target REAL,
    baseline REAL,
    unit TEXT,
    start_date TEXT,
    spec TEXT
);
CREATE TABLE IF NOT EXISTS metric_log (
    id INTEGER PRIMARY KEY,
    goal_id INTEGER NOT NULL REFERENCES goals(id),
    step TEXT,
    value REAL NOT NULL DEFAULT 1,
    note TEXT,
    at TEXT NOT NULL,
    source TEXT
);
CREATE TABLE IF NOT EXISTS body_measures (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    weight REAL,
    bodyfat REAL,
    source TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_plans (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    version INTEGER NOT NULL,
    origin TEXT NOT NULL CHECK (origin IN ('model', 'fallback')),
    facts TEXT NOT NULL,
    brief TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (day, version)
);
CREATE TABLE IF NOT EXISTS plan_items (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES daily_plans(id),
    kind TEXT NOT NULL CHECK (kind IN ('event', 'travel', 'todo')),
    start TEXT,
    end TEXT,
    place TEXT,
    priority TEXT,
    title TEXT NOT NULL,
    goal_id INTEGER REFERENCES goals(id),
    how TEXT,
    smaller TEXT,
    detail TEXT,
    travel_min INTEGER,
    task_id INTEGER REFERENCES tasks(id),
    task_created INTEGER NOT NULL DEFAULT 0,
    calendar_uid TEXT
);
CREATE TABLE IF NOT EXISTS workout_sets (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    exercise TEXT NOT NULL,
    set_no INTEGER NOT NULL,
    weight REAL,
    reps INTEGER NOT NULL,
    pain INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS module_status (
    module TEXT PRIMARY KEY,
    cold_start TEXT NOT NULL CHECK (cold_start IN ('待建档', '进行中', '已完成')),
    note TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS item_results (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES plan_items(id),
    result TEXT NOT NULL CHECK (result IN ('完成', '部分', '没做')),
    reason TEXT,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS habits (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL UNIQUE,
    cadence TEXT NOT NULL CHECK (cadence IN ('daily', 'weekly')),
    target INTEGER NOT NULL DEFAULT 1,
    weekdays TEXT,
    points INTEGER NOT NULL,
    auto TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    sort REAL
);
CREATE TABLE IF NOT EXISTS habit_log (
    id INTEGER PRIMARY KEY,
    habit_id INTEGER NOT NULL REFERENCES habits(id),
    day TEXT NOT NULL,
    at TEXT NOT NULL,
    UNIQUE (habit_id, day)
);
CREATE TABLE IF NOT EXISTS points (
    id INTEGER PRIMARY KEY,
    delta INTEGER NOT NULL,
    reason TEXT NOT NULL,
    ref TEXT,
    day TEXT NOT NULL,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rewards (
    id INTEGER PRIMARY KEY,
    tier TEXT NOT NULL,
    cost INTEGER NOT NULL,
    title TEXT NOT NULL,
    note TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS redemptions (
    id INTEGER PRIMARY KEY,
    reward_id INTEGER NOT NULL REFERENCES rewards(id),
    cost INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('待策划', '已兑现')),
    note TEXT,
    at TEXT NOT NULL,
    done_at TEXT
);
CREATE TABLE IF NOT EXISTS rest_days (
    day TEXT PRIMARY KEY,
    at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    processed INTEGER,
    error TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StructuredDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)  # 工作台多线程收请求，但一次只让一个线程用（见 workbench/server.py）
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.executescript(PLAN_SCHEMA)
        task_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for col, ddl in (("quest", "TEXT"), ("size", "TEXT"), ("milestone", "INTEGER NOT NULL DEFAULT 0"),
                         ("seq", "REAL"), ("draft", "INTEGER NOT NULL DEFAULT 0"),
                         ("hard", "INTEGER NOT NULL DEFAULT 0"), ("star_day", "TEXT"), ("hide_day", "TEXT")):
            if col not in task_cols:  # 2026-10-26 晚：任务系统（主线 / 支线、大小、阶段节点、拆解草稿）
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} {ddl}")
        if "easy" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(habit_log)")}:  # 2026-10-27 简单版打卡
            self.conn.execute("ALTER TABLE habit_log ADD COLUMN easy INTEGER NOT NULL DEFAULT 0")
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(plan_items)")}
        if "task_created" not in cols:  # 2026-10-26 晚：记下哪些待办是计划器自动建的
            self.conn.execute("ALTER TABLE plan_items ADD COLUMN task_created INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    def _migrate(self):
        """给旧库补列：待办和心愿的手动排序（2026-10-22 加入可交互清单）；
        目标层级加上 year（2026-10-26），SQLite 改不了 CHECK，只能重建表。"""
        sql = self.conn.execute("SELECT sql FROM sqlite_master WHERE name = 'goals'").fetchone()[0]
        if "'year'" not in sql:
            self.conn.commit()
            self.conn.execute("PRAGMA foreign_keys = OFF")
            new = SCHEMA.split("CREATE TABLE IF NOT EXISTS progress_log")[0].replace(
                "CREATE TABLE IF NOT EXISTS goals", "CREATE TABLE goals_new")
            self.conn.executescript(f"BEGIN; {new} INSERT INTO goals_new SELECT * FROM goals; "
                                    "DROP TABLE goals; ALTER TABLE goals_new RENAME TO goals; COMMIT;")
            self.conn.execute("PRAGMA foreign_keys = ON")
        for table in ("tasks", "wishes"):
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if "sort" not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN sort REAL")
        if "domain" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(tasks)")}:  # 2026-10-28 待办归到领地
            self.conn.execute("ALTER TABLE tasks ADD COLUMN domain TEXT")
        if "sort" not in {r["name"] for r in self.conn.execute("PRAGMA table_info(goals)")}:  # 2026-10-28 副本排序（她定先后）
            self.conn.execute("ALTER TABLE goals ADD COLUMN sort REAL")
        self.conn.commit()

    def close(self):
        self.conn.close()

    def _insert(self, table: str, **fields) -> int:
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(fields.values())
        )
        self.conn.commit()
        return cur.lastrowid

    # 目标
    def add_goal(self, title, level, parent_id=None, status="not_started", **extra) -> int:
        return self._insert(
            "goals", title=title, level=level, parent_id=parent_id,
            status=status, updated_at=now(), **extra,
        )

    def update_goal(self, goal_id, note=None, source=None, **fields):
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            self.conn.execute(
                f"UPDATE goals SET {sets}, updated_at = ? WHERE id = ?",
                (*fields.values(), now(), goal_id),
            )
        if note:
            self.conn.execute(
                "INSERT INTO progress_log (goal_id, note, source, created_at) VALUES (?, ?, ?, ?)",
                (goal_id, note, source, now()),
            )
        self.conn.commit()

    def get_goal(self, goal_id) -> dict:
        goal = dict(self.conn.execute("SELECT * FROM goals WHERE id = ?", (goal_id,)).fetchone())
        goal["log"] = [
            dict(r) for r in self.conn.execute(
                "SELECT note, source, created_at FROM progress_log WHERE goal_id = ? ORDER BY id",
                (goal_id,),
            )
        ]
        goal["children"] = [
            dict(r) for r in self.conn.execute(
                "SELECT id, title, level, status FROM goals WHERE parent_id = ? ORDER BY id",
                (goal_id,),
            )
        ]
        return goal

    def active_goals(self) -> list:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT id, parent_id, title, level, status, next_step, due, domain FROM goals "
                "WHERE status = 'active' ORDER BY due IS NULL, due, id"
            )
        ]

    def goal_chain(self) -> list:
        """没完成、没放弃的目标，按树的顺序排好，给 小伴 看整条链（带编号，记待办和进展时用）。"""
        rows = [dict(r) for r in self.conn.execute(
            "SELECT id, parent_id, level, title, status, due, next_step, domain FROM goals "
            "WHERE status NOT IN ('done', 'dropped') ORDER BY id")]
        alive = {r["id"] for r in rows}
        children = {}
        for r in rows:
            parent = r["parent_id"] if r["parent_id"] in alive else None
            children.setdefault(parent, []).append(r)
        out = []

        def walk(parent, depth):
            for r in children.get(parent, []):
                out.append({**r, "depth": depth})
                walk(r["id"], depth + 1)
        walk(None, 0)
        return out

    def goal_exists(self, goal_id) -> bool:
        return self.conn.execute("SELECT 1 FROM goals WHERE id = ?", (goal_id,)).fetchone() is not None

    # 小伴 给管理系统的反馈：她的状态、卡点、想调整什么，制定和调整计划时读，用过标记 used_at
    def add_feedback(self, note, goal_id=None, source="ethan") -> int:
        return self._insert("feedback", note=note, goal_id=goal_id, source=source, created_at=now())

    def unused_feedback(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, goal_id, note, source, created_at FROM feedback WHERE used_at IS NULL ORDER BY id")]

    def mark_feedback_used(self, ids):
        self.conn.executemany("UPDATE feedback SET used_at = ? WHERE id = ?", [(now(), i) for i in ids])
        self.conn.commit()

    # 量化指标和身体记录（第一阶段）
    def set_metric(self, goal_id, kind, target=None, baseline=None, unit=None, start_date=None, spec=None):
        self.conn.execute(
            "INSERT OR REPLACE INTO goal_metrics (goal_id, kind, target, baseline, unit, start_date, spec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (goal_id, kind, target, baseline, unit, start_date, spec))
        self.conn.commit()

    def metric(self, goal_id) -> dict:
        r = self.conn.execute("SELECT * FROM goal_metrics WHERE goal_id = ?", (goal_id,)).fetchone()
        return dict(r) if r else None

    def log_metric(self, goal_id, note=None, step=None, value=1.0, source="聊天") -> int:
        return self._insert("metric_log", goal_id=goal_id, step=step, value=value, note=note, at=now(),
                            source=source)

    def metric_entries(self, goal_id, since: str = None) -> list:
        sql, args = "SELECT * FROM metric_log WHERE goal_id = ?", [goal_id]
        if since:
            sql += " AND at >= ?"
            args.append(since)
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY id", args)]

    def add_body_measure(self, day: str, weight=None, bodyfat=None, source="聊天") -> int:
        return self._insert("body_measures", day=day, weight=weight, bodyfat=bodyfat, source=source, at=now())

    def body_measures(self, since_day: str) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM body_measures WHERE day >= ? ORDER BY day, id", (since_day,))]

    # 每日计划与结果（第一阶段）
    def save_plan(self, day: str, origin: str, facts: str, brief: str, fingerprint: str, items: list) -> int:
        version = self.conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM daily_plans WHERE day = ?",
                                    (day,)).fetchone()[0]
        cur = self.conn.execute(
            "INSERT INTO daily_plans (day, version, origin, facts, brief, fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (day, version, origin, facts, brief, fingerprint, now()))
        plan_id = cur.lastrowid
        for it in items:
            self.conn.execute(
                "INSERT INTO plan_items (plan_id, kind, start, end, place, priority, title, goal_id, how, smaller, "
                "detail, travel_min, task_id, task_created) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, it["kind"], it.get("start"), it.get("end"), it.get("place"), it.get("priority"),
                 it["title"], it.get("goal_id"), it.get("how"), it.get("smaller"), it.get("detail"),
                 it.get("travel_min"), it.get("task_id"), int(bool(it.get("task_created")))))
        self.conn.commit()
        return plan_id

    def latest_plan(self, day: str) -> dict:
        r = self.conn.execute("SELECT * FROM daily_plans WHERE day = ? ORDER BY version DESC LIMIT 1",
                              (day,)).fetchone()
        if not r:
            return None
        plan = dict(r)
        plan["items"] = [dict(i) for i in self.conn.execute(
            "SELECT i.*, COALESCE((SELECT result FROM item_results WHERE item_id = i.id ORDER BY id DESC LIMIT 1), "
            "CASE WHEN (SELECT done FROM tasks WHERE id = i.task_id) = 1 THEN '完成' END) AS result "
            "FROM plan_items i WHERE plan_id = ? ORDER BY kind = 'todo', start, priority = 'nice', id", (plan["id"],))]
        return plan

    def set_calendar_uid(self, item_id, uid):
        self.conn.execute("UPDATE plan_items SET calendar_uid = ? WHERE id = ?", (uid, item_id))
        self.conn.commit()

    def item_exists(self, item_id) -> bool:
        return self.conn.execute("SELECT 1 FROM plan_items WHERE id = ?", (item_id,)).fetchone() is not None

    def add_result(self, item_id, result, reason=None) -> int:
        return self._insert("item_results", item_id=item_id, result=result, reason=reason, at=now())

    def completion(self, since_day: str, until_day: str) -> dict:
        """[since, until) 之间每天最新版计划的完成情况（训练、任务等都算）。"""
        rows = self.conn.execute(
            "SELECT p.day, i.id, i.kind, i.title, i.goal_id, "
            "COALESCE((SELECT result FROM item_results WHERE item_id = i.id ORDER BY id DESC LIMIT 1), "
            "CASE WHEN (SELECT done FROM tasks WHERE id = i.task_id) = 1 THEN '完成' END) AS result, "
            "(SELECT reason FROM item_results WHERE item_id = i.id ORDER BY id DESC LIMIT 1) AS reason "
            "FROM daily_plans p JOIN plan_items i ON i.plan_id = p.id "
            "WHERE p.day >= ? AND p.day < ? AND p.version = "
            "(SELECT MAX(version) FROM daily_plans q WHERE q.day = p.day) AND i.kind != 'travel'",
            (since_day, until_day)).fetchall()
        items = [dict(r) for r in rows]
        done = sum(1 for i in items if i["result"] == "完成") + 0.5 * sum(1 for i in items if i["result"] == "部分")
        return {"planned": len(items), "done": done,
                "missed": [{"title": i["title"], "reason": i["reason"]} for i in items if i["result"] in (None, "没做")]}

    # 运行留痕
    def start_job(self, job: str) -> int:
        return self._insert("job_runs", job=job, started_at=now())

    def finish_job(self, run_id, processed=0, error=None):
        self.conn.execute("UPDATE job_runs SET finished_at = ?, processed = ?, error = ? WHERE id = ?",
                          (now(), processed, error, run_id))
        self.conn.commit()

    # 任务
    def add_task(self, title, due=None, goal_id=None, promised_by_ethan=False) -> int:
        return self._insert(
            "tasks", title=title, due=due, goal_id=goal_id,
            promised_by_ethan=int(promised_by_ethan), created_at=now(),
        )

    def complete_task(self, task_id):
        self.conn.execute("UPDATE tasks SET done = 1, done_at = ? WHERE id = ?", (now(), task_id))
        self.conn.commit()

    def open_tasks(self) -> list:
        """没做完的待办，按她手动排的顺序，其次按截止日期。"""
        return [
            dict(r) for r in self.conn.execute(
                "SELECT id, title, due, promised_by_ethan, goal_id FROM tasks WHERE done = 0 "
                "ORDER BY sort IS NULL, sort, due IS NULL, due, id"
            )
        ]

    def delete_task(self, task_id):
        """连同准备员的记录一起删（2026-10-28：prep_work 外键挡着，删有准备记录的待办会报错）；中途出错整体退回。"""
        try:
            self.conn.execute("UPDATE plan_items SET task_id = NULL WHERE task_id = ?", (task_id,))
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'prep_work'").fetchone():
                self.conn.execute("DELETE FROM prep_work WHERE task_id = ?", (task_id,))
            row = self.conn.execute("SELECT title, goal_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if row:  # 记下她删了什么，计划器别再排回来（2026-10-28）
                self.conn.execute("CREATE TABLE IF NOT EXISTS deleted_tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                                  "goal_id INTEGER, at TEXT NOT NULL)")
                self.conn.execute("INSERT INTO deleted_tasks (title, goal_id, at) VALUES (?, ?, ?)",
                                  (row["title"], row["goal_id"], now()))
            self.conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def recently_deleted(self, days: int = 30) -> list:
        if not self.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'deleted_tasks'").fetchone():
            return []
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        return [r["title"] for r in self.conn.execute("SELECT title FROM deleted_tasks WHERE at >= ? ORDER BY id DESC LIMIT 60", (since,))]

    def move_up(self, table: str, item_id: int, ids: list):
        """把 item 在 ids（当前显示顺序）里往前挪一位，并把整组顺序写进 sort。"""
        if table not in ("tasks", "wishes") or item_id not in ids:
            return
        i = ids.index(item_id)
        if i > 0:
            ids[i - 1], ids[i] = ids[i], ids[i - 1]
        for n, row_id in enumerate(ids):
            self.conn.execute(f"UPDATE {table} SET sort = ? WHERE id = ?", (n, row_id))
        self.conn.commit()

    def due_tasks(self, today: date) -> list:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT id, title, due, promised_by_ethan FROM tasks "
                "WHERE done = 0 AND due IS NOT NULL AND due <= ? ORDER BY due, id",
                (today.isoformat(),),
            )
        ]

    # 学习条目（间隔重复，简化版 SM-2）
    def add_learning_item(self, subject, content, note=None, today: date = None) -> int:
        today = today or date.today()
        return self._insert(
            "learning_items", subject=subject, content=content, note=note,
            next_review=(today + timedelta(days=1)).isoformat(), created_at=now(),
        )

    def review_learning_item(self, item_id, remembered: bool, today: date = None):
        today = today or date.today()
        row = self.conn.execute(
            "SELECT interval_days, ease FROM learning_items WHERE id = ?", (item_id,)
        ).fetchone()
        if remembered:
            ease = min(row["ease"] + 0.1, 3.0)
            interval = max(1, round(row["interval_days"] * ease))
        else:
            ease = max(row["ease"] - 0.2, 1.3)
            interval = 1
        self.conn.execute(
            "UPDATE learning_items SET interval_days = ?, ease = ?, last_review = ?, next_review = ? "
            "WHERE id = ?",
            (interval, ease, today.isoformat(), (today + timedelta(days=interval)).isoformat(), item_id),
        )
        self.conn.commit()

    def due_learning_items(self, today: date, limit=5) -> list:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT id, subject, content, note FROM learning_items "
                "WHERE next_review <= ? ORDER BY next_review, id LIMIT ?",
                (today.isoformat(), limit),
            )
        ]

    # 人物卡
    def upsert_person(self, name, **fields) -> int:
        row = self.conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
        if row:
            if fields:
                sets = ", ".join(f"{k} = ?" for k in fields)
                self.conn.execute(
                    f"UPDATE people SET {sets}, updated_at = ? WHERE id = ?",
                    (*fields.values(), now(), row["id"]),
                )
                self.conn.commit()
            return row["id"]
        return self._insert("people", name=name, updated_at=now(), **fields)

    def delete_person(self, name):
        self.conn.execute("DELETE FROM people WHERE name = ?", (name,))
        self.conn.commit()

    def people_to_contact(self, today: date) -> list:
        """超过约定联系间隔的人，以及 7 天内过生日的人（生日格式 MM-DD）。"""
        result = []
        for r in self.conn.execute("SELECT * FROM people ORDER BY id"):
            person = dict(r)
            reasons = []
            if person["contact_every_days"] and person["last_contact"]:
                last = date.fromisoformat(person["last_contact"])
                if (today - last).days >= person["contact_every_days"]:
                    reasons.append("long_no_contact")
            if person["birthday"]:
                month, day = (int(x) for x in person["birthday"].split("-"))
                for year in (today.year, today.year + 1):
                    try:
                        bday = date(year, month, day)
                    except ValueError:  # 2 月 29 日在平年按 3 月 1 日算
                        bday = date(year, 3, 1)
                    if 0 <= (bday - today).days <= 7:
                        reasons.append("birthday_soon")
                        break
            if reasons:
                result.append({"name": person["name"], "reasons": reasons})
        return result

    # 心愿清单
    def add_wish(self, quote, context=None) -> int:
        return self._insert("wishes", quote=quote, context=context, created_at=now())

    def close_wish(self, wish_id, status="done"):
        self.conn.execute(
            "UPDATE wishes SET status = ?, done_at = ? WHERE id = ?", (status, now(), wish_id)
        )
        self.conn.commit()

    def open_wishes(self) -> list:
        return [
            dict(r) for r in self.conn.execute(
                "SELECT id, quote, context, created_at FROM wishes WHERE status = 'open' "
                "ORDER BY sort IS NULL, sort, id"
            )
        ]
