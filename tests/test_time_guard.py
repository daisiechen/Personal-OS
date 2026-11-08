"""时间守卫（全系统模块化第三批“时间只补守卫”）：她的“今天”只从 ethan/clock.py 取（system-rules 第 5 节）。

- ethan/ 下不许用 SQL 的 date('now')（UTC 的今天，夏令时 0–1 点差一天）；要“她今天的”用 clock.her_day_start_utc / her_today 传参。
- SQL 的 datetime('now'...) 只许用来写时间戳或算相对时长（多少分钟 / 天以内），白名单每个文件写原因。
- 不带时区的 datetime.now() / datetime.today() / date.today() / 无参 .astimezone() 不许出现，白名单每个文件写原因（都不是算她的“今天”）。
- 决定 9：作息统计和工作时长一样按凌晨 5 点分天，常量只在 clock.DAY_START_HOUR。
- “现在清吧”：“本周一 / 这周日”只在 clock.week_start / week_end / season_week 算，别处不许自己写
  x - timedelta(days=x.weekday())；不许写死 "Europe/London"，她的时区一律从 clock 取（her_tz / tz_name / LOCAL）。
"""

import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import clock

ROOT = Path(__file__).resolve().parent.parent

SQL_NOW_OK = {  # 文件 → 原因（datetime('now'...)）
    "ethan/lifebase.py": "触发器里写记录时间（UTC 时间戳）",
    "ethan/nudges.py": "nudge_once 写发送时间（UTC 时间戳）",
    "ethan/autodone.py": "task_checks 写判定时间（UTC 时间戳）",
    "ethan/domains.py": "module_status 写更新时间（UTC 时间戳）",
    "ethan/planner.py": "plan_overrides 写改动时间（UTC 时间戳）",
    "ethan/season1.py": "一次性导入写时间戳",
    "ethan/mail.py": "卡住 30 分钟回收、归档最近 N 天：相对时长，和她的日界无关",
    "ethan/desk.py": "“知道一下”最近 7 天：相对时长，和她的日界无关",
}
NAIVE_OK = {  # 文件 → 原因（不带时区的 now）
    "ethan/clock.py": "说明文字里提到 date.today()",
    "ethan/workbench/api.py": "内存缓存的计时，只算隔了几秒",
    "ethan/cards.py": "图片文件名里的时分秒、写入时间（不当日期用）",
    "ethan/desk.py": "llm.log 的开始时间，只算耗时",
    "ethan/photos.py": "照片记录时间和文件名，按 3 天窗口比，差一小时无所谓",
    "ethan/explore.py": "上传时间和备份文件名（不当日期用）",
    "ethan/events.py": "写入时间（不当日期用）",
}
# 自己算周一 / 周日：timedelta(days=x.weekday()) 或 timedelta(days=6 - x.weekday())
WEEK_CALC = re.compile(r"timedelta\(\s*days\s*=\s*(6\s*-\s*)?[\w.()]+\.weekday\(\)\s*\)")
LONDON_OK = {  # 文件 → 原因（"Europe/London" 字面量）
    "ethan/clock.py": "唯一的默认值（ETHAN_TZ 没设时）",
    "ethan/jobs.py": "systemd 定时器那一行要和 deploy/systemd/*.timer 一字不差（tests/test_jobs.py 对着查），定时器文件是静态的",
    "ethan/whereabouts.py": "时区 → 城市名的对照表（显示“她在哪”），不是算时间",
}
NAIVE = re.compile(r"datetime\.now\(\)|datetime\.today\(\)|date\.today\(\)|\.astimezone\(\)")


def _files():
    for p in sorted((ROOT / "ethan").rglob("*.py")):
        yield p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")


class TimeGuardTest(unittest.TestCase):
    def test_no_sql_date_now(self):
        bad = [f"{path}:{i}" for path, src in _files() if path != "ethan/clock.py"
               for i, line in enumerate(src.splitlines(), 1) if re.search(r"""\bdate\(\s*['"]now""", line)]
        self.assertEqual(bad, [], "SQL 的 date('now') 是 UTC 的今天：用 clock.her_day_start_utc / her_today 传参")

    def test_sql_datetime_now_only_whitelisted(self):
        bad = [f"{path}:{i}" for path, src in _files() if path not in SQL_NOW_OK
               for i, line in enumerate(src.splitlines(), 1) if re.search(r"""datetime\(\s*['"]now""", line)]
        self.assertEqual(bad, [], "datetime('now') 只许写时间戳 / 算相对时长，进白名单要写原因")

    def test_no_naive_now(self):
        bad = [f"{path}:{i}: {line.strip()[:80]}" for path, src in _files() if path not in NAIVE_OK
               for i, line in enumerate(src.splitlines(), 1) if NAIVE.search(line)]
        self.assertEqual(bad, [], "\n".join(bad))

    def test_week_start_only_in_clock(self):
        bad = [f"{path}:{i}: {line.strip()[:80]}" for path, src in _files() if path != "ethan/clock.py"
               for i, line in enumerate(src.splitlines(), 1) if WEEK_CALC.search(line)]
        self.assertEqual(bad, [], "“本周一 / 这周日”用 clock.week_start / week_end / season_week：\n" + "\n".join(bad))

    def test_no_hardcoded_london(self):
        bad = [f"{path}:{i}" for path, src in _files() if path not in LONDON_OK
               for i, line in enumerate(src.splitlines(), 1) if "Europe/London" in line]
        self.assertEqual(bad, [], "她的时区从 clock 取（her_tz / tz_name / LOCAL），别写死 Europe/London")

    def test_week_helpers(self):
        from datetime import date
        self.assertEqual(clock.week_start(date(2026, 11, 8)).isoformat(), "2026-11-02")  # 周日 → 本周一
        self.assertEqual(clock.week_start(date(2026, 11, 2)).isoformat(), "2026-11-02")
        self.assertEqual(clock.week_end(date(2026, 11, 2)).isoformat(), "2026-11-08")
        self.assertEqual(clock.season_week(date(2026, 11, 2), date(2026, 10, 27)), 2)  # 周二开季，下周一是第 2 周
        self.assertEqual(clock.season_week(date(2026, 10, 27), date(2026, 10, 27)), 1)
        # 不给 day 按她的今天：UTC 周一 00:30 在冬令时还是周一（同一天）；UTC 周日 23:30 在夏令时已是周一
        self.assertEqual(clock.week_start(now=datetime(2026, 6, 28, 23, 30, tzinfo=timezone.utc)).isoformat(), "2026-06-29")
        self.assertEqual(clock.tz_name({"timezone": "Asia/Tokyo"}), "Asia/Tokyo")

    def test_one_day_start(self):
        """工作时长、工作狂指数、作息统计的“几点分天”都从 clock 取，别处不再写 5 点 / 4 点的常量。"""
        from ethan import onto_query, workaholic, worktime
        self.assertEqual(clock.DAY_START_HOUR, 5)
        self.assertEqual(worktime.DAY_START, "05:00")
        self.assertEqual(workaholic.MORNING.hour, 5)
        self.assertEqual(onto_query.DAY_START_MIN, 5 * 60)
        for path in ("ethan/worktime.py", "ethan/workaholic.py", "ethan/onto_query.py"):
            src = (ROOT / path).read_text(encoding="utf-8")
            self.assertNotRegex(src, r'DAY_START\s*=\s*"0\d:00"|MORNING\s*=\s*time\(\d|DAY_START_MIN\s*=\s*\d', path)

    def test_work_day(self):
        self.assertEqual(clock.work_day(datetime(2026, 11, 3, 4, 59)).isoformat(), "2026-11-02")
        self.assertEqual(clock.work_day(datetime(2026, 11, 3, 5, 0)).isoformat(), "2026-11-03")

    def test_her_day_start_utc_summer_time(self):
        # 夏令时 7 月 1 日 00:30（UTC 6 月 30 日 23:30）：她的今天从 UTC 6-30 23:00 起
        now = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(clock.her_day_start_utc(now=now), "2026-06-30T23:00:00+00:00")


class DeskTodayTest(unittest.TestCase):
    """邮件台“今天发出去的”按她的时区（以前 SQL 取 UTC 的今天，夏令时 0–1 点发的会第二天才消失 / 提早消失）。"""

    def test_sent_today_by_her_timezone(self):
        from ethan import desk
        from ethan.memory import MemoryStore
        with tempfile.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            try:
                desk.ensure(store.db)
                cols = {r[1] for r in store.db.conn.execute("PRAGMA table_info(outbox)")}
                vals = {"to_addr": "a@b.c", "subject": "s", "draft": "x", "desk_state": "sent", "sent_at": "2026-06-30T23:10:00+00:00",
                        "created_at": "2026-06-30T22:00:00+00:00"}
                vals = {k: v for k, v in vals.items() if k in cols}
                store.db.conn.execute(f"INSERT INTO outbox ({', '.join(vals)}) VALUES ({', '.join('?' * len(vals))})", tuple(vals.values()))
                store.db.conn.commit()
                at = datetime(2026, 6, 30, 23, 30, tzinfo=timezone.utc)  # 本地 7-1 00:30，UTC 还是 6-30
                self.assertEqual([x["kind"] for x in desk.items(store.db, now=at)], ["out"])  # 本地 00:10 发的算今天
                later = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)  # 本地 7-2 00:30：昨天发的不再留着
                self.assertEqual(desk.items(store.db, now=later), [])
            except sqlite3.OperationalError as e:
                self.fail(str(e))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
