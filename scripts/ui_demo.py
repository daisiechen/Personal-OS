"""界面检查用的示意工作台（docs/design/system-rules.md 第 6 节）：用测试里的示意数据起一个本机工作台，不碰真实数据。

python3 scripts/ui_demo.py <空目录> [端口]   → 在 127.0.0.1:端口 起服务，登录令牌写到 <目录>/token.txt
"""

import sys
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ethan.clock import LOCAL  # noqa: E402
from ethan.core.router import Settings  # noqa: E402
from ethan.workbench import auth  # noqa: E402
from ethan.workbench.api import Workbench  # noqa: E402
from ethan.workbench.server import App, make_handler  # noqa: E402
from tests.test_workbench import FakeCalendar, make_demo  # noqa: E402


def demo_relations(db, today):
    """“关系”页的示意人物（名字都是编的）。"""
    import json
    from datetime import timedelta
    from ethan import relations
    d = lambda n: (today - timedelta(days=n)).isoformat()
    rows = [("母上大人", 2, 210, 900), ("小林", 34, 40, 300), ("阿哲", 9, 120, 640), ("Mia", 3, 60, 200), ("王老师", 50, 6, 20),
            ("周周", 120, 0, 12), ("Leo", 15, 20, 80), ("室友 Anna", 1, 160, 400), ("陈师姐", 70, 2, 30), ("大鹏", 200, 0, 3),
            ("老爸", 6, 40, 260), ("Sophie", 95, 0, 8), ("Kevin 学长", 40, 4, 25), ("小雨", 22, 18, 120)]
    relations.ingest_stats(db, {"people": [{"key": relations.wx_key(n), "name": n, "last": d(g), "n90": a, "n365": b, "total": b + 40,
                                            "her_share": 0.52, "her_starts": 0.45, "rounds": 40, "groups": ["某商学院 中国同学会"] if i % 3 else []}
                                           for i, (n, g, a, b) in enumerate(rows)]})
    one = lambda name, **kw: db.conn.execute(f"UPDATE people SET {', '.join(k + ' = ?' for k in kw)}, organized_at = ? WHERE name = ?",
                                             (*kw.values(), d(0), name))
    one("母上大人", relation="她妈妈，在杭州", birthday=(today + timedelta(days=5)).strftime("%m-%d"), recent="最近在学太极，问伦敦冷不冷",
        topics=json.dumps(["天气", "羽绒服", "太极"]), draft="妈，今天伦敦下雨了，羽绒服我周末去买～")
    one("小林", relation="大学室友，现在在上海做咨询", circle="挚友", recent="下周三博士答辩", topics=json.dumps(["答辩", "回国"]),
        draft="答辩准备得怎么样啦？周三我给你加油！",
        open_loops=json.dumps([{"kind": "they_asked", "date": d(1), "text": "问你圣诞回不回国", "quote": "你圣诞回不回国？"},
                               {"kind": "she_promised", "date": d(10), "text": "答应寄伦敦的明信片", "quote": "我给你寄明信片"}]),
        dates=json.dumps([{"date": (today + timedelta(days=2)).isoformat(), "what": "博士答辩"}]))
    one("阿哲", relation="某商学院 同学，一起做项目", circle="同学", recent="在找暑期实习", topics=json.dumps(["实习", "项目"]))
    one("王老师", relation="本科导师，写过推荐信", circle="老师", topics=json.dumps(["推荐信", "近况"]))
    db.conn.commit()


def demo_system(root, db):
    """设置页“系统体检”的示意：定时任务记录（一个超时没跑）、部署（挂了一条测试）、微信和人物卡整理到一半（都是编的）。"""
    import json
    from datetime import timedelta, timezone
    from ethan import interviews, jobs, lifebase
    now = datetime.now(timezone.utc)
    for key in jobs.JOBS:
        late = key == "money"
        jobs.record(root, key, True, now - timedelta(hours=26 if late else 0, minutes=7), now - timedelta(hours=26 if late else 0, minutes=6),
                    "ok")
    jobs.mark_start(root, "backlog", now - timedelta(minutes=35))
    (root / "deploy-status.json").write_text(json.dumps({"at": (now - timedelta(minutes=40)).isoformat(timespec="seconds"), "to": "b44d52b",
        "subject": "ontology 3.1 收尾 + 3.2：小伴 聊天按题分流", "result": "失败，已退回原版本", "step": "跑测试", "detail": "FAILED (failures=1)",
        "failed_tests": ["test_mepage.test_undo_correction"]}, ensure_ascii=False))
    (root / "deploy-check.json").write_text(json.dumps({"checked_at": (now - timedelta(minutes=1)).isoformat(timespec="seconds"), "error": ""}))
    lifebase.ensure(db)
    db.conn.execute(interviews.SCHEMA)
    for i in range(12):
        ev = lifebase.add_evidence(db, "interview", f"interview:demo{i}", now.isoformat(),
                                   text=f"【访谈】会话：微信 · 朋友{i}；场合：她的微信聊天（导出）；时间：x；整段对话\n【她】嗯")
        if i < 7:
            db.conn.execute("INSERT INTO interview_digest (evidence_id, at, result) VALUES (?, ?, '{}')", (ev, (now - timedelta(minutes=9)).isoformat()))
    db.conn.commit()
def demo_outside(db):
    """“我”页对外开关的示意：她改过一格、一条“问我”进了待你定（内容都是编的）。"""
    from ethan import lifebase, mepage
    ev = lifebase.add_evidence(db, "chat", "raw:demo:outside", "2026-11-01T09:00:00", "mia：我现在住在伦敦")
    lifebase.add_fact(db, "resources", "现在住在伦敦", [ev], prop="lives_in")
    lifebase.set_policy(db, "partner", "rhythms", "allow")
    mepage.add_disclosure_ask(db, "twin", "她现在人在哪个城市？", "whereabouts", "现在住在伦敦", "Leo")


def demo_feedback_1105(db, today):
    """她 11-05 的反馈要看得到的样子：一件先放着的（红点）、一件排进今天的“其他”、两件归了领域的待办。"""
    from ethan import prep
    db.add_task("和开发会话一起定正式评估的题集", due=today.isoformat())
    parked = db.add_task("整理芬兰行程的备选酒店", promised_by_ethan=True)
    prep.set_pending(db, parked)
    for title, key in (("做小红书第一条笔记", "life"), ("给 LinkedIn 换头像", "career")):
        tid = db.add_task(title)
        db.conn.execute("UPDATE tasks SET domain = ? WHERE id = ?", (key, tid))
    db.conn.commit()


def main():
    root = Path(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8810
    root.mkdir(parents=True, exist_ok=True)
    store = make_demo(root, datetime.now(LOCAL).date())
    demo_relations(store.db, datetime.now(LOCAL).date())
    demo_system(root, store.db)
    demo_outside(store.db)
    demo_feedback_1105(store.db, datetime.now(LOCAL).date())
    settings = Settings(root / "settings.json")
    settings.set("telegram_owner_chat_id", 1)
    wb = Workbench(store, settings, calendar=FakeCalendar())
    app = App(wb, "x" * 40, root)
    (root / "token.txt").write_text(auth.make_session(app.key, app.owner))
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
