"""时间约定（docs/design/system-rules.md 第 5 节）。

- 她的“今天”只从 ethan/clock.py 取（her_today / her_now）：date.today() 是服务器的日期（UTC），夏令时凌晨 0–1 点会算成前一天。
- 不带时区的 datetime.now() 只允许用来算时长、打文件名这类不涉及“哪一天”的地方；数量只减不增，新代码用 clock 里的。
"""

import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ethan import clock

ROOT = Path(__file__).resolve().parent.parent / "ethan"
NAIVE_NOW_LIMIT = 19  # 2026-11-02 盘点时的数量，只许往下改


def _scan(pattern: str) -> list:
    out = []
    for f in ROOT.rglob("*.py"):
        if f.name == "clock.py":
            continue
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(pattern, line) and not line.lstrip().startswith("#"):
                out.append(f"{f.relative_to(ROOT.parent)}:{n}")
    return out


class TimeRulesTest(unittest.TestCase):
    def test_no_server_local_today(self):
        self.assertEqual(_scan(r"\bdate\.today\(\)"), [], "用 clock.her_today()，不要用服务器日期")

    def test_naive_now_only_shrinks(self):
        found = _scan(r"datetime\.now\(\)")
        self.assertLessEqual(len(found), NAIVE_NOW_LIMIT, "新代码用 clock.her_now() / utc_now()：\n" + "\n".join(found))

    def test_her_today_uses_her_timezone(self):
        # 夏令时 0:30 = UTC 前一天 23:30：服务器日期还是前一天，她的今天已经是新的一天
        late = datetime(2026, 7, 1, 23, 30, tzinfo=timezone.utc)
        self.assertEqual(clock.her_today(None, late).isoformat(), "2026-07-02")
