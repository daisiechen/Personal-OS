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
    tags = {"母上大人": ["家人"], "老爸": ["家人"], "小林": ["大学同学", "上海"], "阿哲": ["某商学院"], "Mia": ["某商学院", "伦敦朋友"],
            "室友 Anna": ["伦敦朋友"], "Leo": ["某商学院"], "陈师姐": ["大学同学"], "王老师": ["老师"], "Kevin 学长": ["大学同学"]}  # 微信标签（11-06）
    relations.ingest_stats(db, {"people": [{"key": relations.wx_key(n), "name": n, "last": d(g), "n90": a, "n365": b, "total": b + 40,
                                            "her_share": 0.52, "her_starts": 0.45, "rounds": 40, "groups": ["某商学院 中国同学会"] if i % 3 else [],
                                            "labels": tags.get(n, [])}
                                           for i, (n, g, a, b) in enumerate(rows)]})
    one = lambda name, **kw: db.conn.execute(f"UPDATE people SET {', '.join(k + ' = ?' for k in kw)}, organized_at = ? WHERE name = ?",
                                             (*kw.values(), d(0), name))
    one("母上大人", relation="她妈妈，在老家", birthday=(today + timedelta(days=5)).strftime("%m-%d"), recent="最近在学太极，问伦敦冷不冷",
        topics=json.dumps(["天气", "羽绒服", "太极"]), draft="妈，今天伦敦下雨了，羽绒服我周末去买～")
    one("小林", relation="大学室友，现在在上海做咨询", circle="挚友", recent="下周三博士答辩", topics=json.dumps(["答辩", "回国"]),
        draft="答辩准备得怎么样啦？周三我给你加油！",
        open_loops=json.dumps([{"kind": "they_asked", "date": d(1), "text": "问你圣诞回不回国", "quote": "你圣诞回不回国？"},
                               {"kind": "she_promised", "date": d(10), "text": "答应寄伦敦的明信片", "quote": "我给你寄明信片"}]),
        dates=json.dumps([{"date": (today + timedelta(days=2)).isoformat(), "what": "博士答辩"}]))
    one("阿哲", relation="某商学院 同学，一起做项目", circle="同学", recent="在找暑期实习", topics=json.dumps(["实习", "项目"]))
    one("王老师", relation="本科导师，写过推荐信", circle="老师", topics=json.dumps(["推荐信", "近况"]))
    db.conn.commit()


def demo_asks_and_gone(db, today):
    """11-06 第一批：首页“小伴 问过你、还没回”的一行（约撞时间，问过一次）和关系页“删掉的人”（名字都是编的）。"""
    from datetime import datetime as _dt, timedelta, timezone
    from ethan import events, nudges, relations
    eid = events.add(db, (today + timedelta(days=3)).isoformat(), "19:00", "21:00", "和 Mia 吃火锅", "Soho", note="来自聊天")
    db.conn.execute("UPDATE future_events SET conflict = ? WHERE id = ?", ("某商学院 校友晚宴", eid))
    db.conn.commit()
    nudges.log_sent(db, f"clash:{eid}", _dt.now(timezone.utc) - timedelta(hours=3))
    nudges.mark_told(db, [f"clash:{eid}"])
    for name in ("大鹏", "Sophie"):
        row = db.conn.execute("SELECT id FROM people WHERE name = ?", (name,)).fetchone()
        if row:
            relations.delete(db, row["id"])


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
            db.conn.execute("INSERT OR IGNORE INTO interview_digest (evidence_id, at, result) VALUES (?, ?, '{}')", (ev, (now - timedelta(minutes=9)).isoformat()))
    db.conn.commit()
def demo_outside(db):
    """“我”页对外开关的示意：她改过一格、一条“问我”进了待你定（内容都是编的）。"""
    from ethan import lifebase, mepage
    ev = lifebase.add_evidence(db, "chat", "raw:demo:outside", "2026-11-01T09:00:00", "mia：我现在住在伦敦")
    lifebase.add_fact(db, "resources", "现在住在伦敦", [ev], prop="lives_in")
    lifebase.set_policy(db, "partner", "rhythms", "allow")
    mepage.add_disclosure_ask(db, "twin", "她现在人在哪个城市？", "whereabouts", "现在住在伦敦", "Leo")
    # 决定 10（11-06）：过了预计日期的临时状态，“我”页全部记忆里标“到期了”
    ev2 = lifebase.add_evidence(db, "chat", "raw:demo:trip", "2026-11-01T09:00:00", "mia：这几天在爱丁堡出差")
    lifebase.add_fact(db, "rhythms", "这几天在爱丁堡出差", [ev2], until="2026-11-03", importance=5)


def demo_desk(db):
    """“邮件和留言”的示意（11-06 决定 6）：要回的、小伴 在起草的算红点；主页助手能回的留言、知道一下的不算（内容都是编的）。"""
    from datetime import timezone
    from ethan import desk
    desk.ensure(db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for key, subj, att, fyi, ds in (("d1", "小组报告周五前交你的部分", 1, 0, "open"), ("d2", "房东：下月起房租调整", 1, 0, "drafting"),
                                    ("d3", "某商学院：交换生签证材料清单更新", 0, 1, "open")):
        db.conn.execute("INSERT INTO mail_items (msg_key, account, sender, subject, received_at, body, status, attention, fyi, reason, "
                        "desk_state, created_at) VALUES (?, 'Gmail', 'Mazen <m@example.com>', ?, ?, '示意正文', 'done', ?, ?, '等你回复', ?, ?)",
                        (key, subj, now, att, fyi, ds, now))
    for at, name, tier, summ in (("t1", "Amy", "fyi", "夸主页的插画好看"), ("t2", "Lena", "reply", "想约你聊合作")):
        db.conn.execute("INSERT INTO visitor_messages (visitor_id, at, received_at, name, intent, message, status, tier, summary, desk_state) "
                        "VALUES ('demo', ?, ?, ?, '其他', ?, 'new', ?, ?, 'open')", (at, now, name, summ, tier, summ))
    db.conn.commit()


def demo_feedback_1105(db, today):
    """的反馈要看得到的样子：一件先放着的（红点）、一件排进今天的“其他”、两件归了领域的待办。"""
    from ethan import prep
    db.add_task("和开发会话一起定正式评估的题集", due=today.isoformat())
    parked = db.add_task("整理芬兰行程的备选酒店", promised_by_ethan=True)
    prep.set_pending(db, parked)
    for title, key in (("做小红书第一条笔记", "life"), ("给 LinkedIn 换头像", "career")):
        tid = db.add_task(title)
        db.conn.execute("UPDATE tasks SET domain = ? WHERE id = ?", (key, tid))
    db.conn.commit()


DEMO_SCORES = {"body": 34, "career": 86, "money": 61, "social": 38, "study": 72, "academic": 55, "life": 66}
DEMO_START = {"body": 52, "career": 74, "money": 58, "social": 47, "study": 63, "academic": 55, "life": 60}
DEMO_PREV = {"健康": 45, "事业": 70, "财务": 66, "社交": 41, "学习": 60, "学术": 50, "生活体验": 71}


def demo_season(store, settings, today):
    """赛季页第二版的示意：开季 24 天、每天有积分、开季那天的分数快照、主副本 4 个里程碑、上一季的结算。
    分数是编的：只在示意工作台里把七项属性换成固定值，真实数据不受影响。"""
    import json
    from datetime import timedelta
    from ethan import attributes, nextseason, stages
    start = today - timedelta(days=24)
    settings.set("season", {"name": "2026 冬季 · 城市篇", "start": start.isoformat(), "end": (today + timedelta(days=55)).isoformat()})
    db = store.db
    for n in range(25):
        db.conn.execute("INSERT INTO points (day, delta, reason, at) VALUES (?, ?, 'demo', 'x')", ((start + timedelta(days=n)).isoformat(), 90 + (n * 37) % 140))
    rid = db.conn.execute("SELECT id FROM rewards ORDER BY id LIMIT 1").fetchone()
    if rid:  # 示意积分大多已经花掉，免得一打开就弹“积分够换惊喜了”
        db.conn.execute("INSERT INTO redemptions (reward_id, cost, status, at) VALUES (?, 3700, '已兑现', 'x')", (rid[0],))
    db.conn.execute(attributes.SNAP_SQL)
    for k, v in DEMO_START.items():
        db.conn.execute("INSERT OR REPLACE INTO attribute_daily (day, key, score, items) VALUES (?, ?, ?, '[]')", (start.isoformat(), k, v))
    gid = db.conn.execute("SELECT id FROM goals WHERE title LIKE '%减脂%' AND level = 'year'").fetchone()[0]
    mids = [stages.add_milestone(db, gid, t, (today + timedelta(days=d)).isoformat()) for t, d in
            (("建立基线", -12), ("一周练满五次", 9), ("中期复测", 24), ("收官称重", 55))]
    db.conn.execute("UPDATE milestones SET status = 'done' WHERE id = ?", (mids[0],))
    for i in range(8):
        t = stages.add_task(db, mids[1], f"第 {i + 1} 次训练")
        if i < 5:
            db.conn.execute("UPDATE tasks SET done = 1 WHERE id = ?", (t,))
    nextseason.ensure(db)
    db.conn.execute("INSERT INTO season_archive (name, start, end, line, body, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    ("2026 秋季 · 城市篇", "2026-09-01", (start - timedelta(days=1)).isoformat(), "开学季，事业和学习起步，社交一直偏少。",
                     json.dumps({"domains": [{"name": k, "now": v} for k, v in DEMO_PREV.items()], "bosses": []}, ensure_ascii=False), "x"))
    db.conn.commit()
    real = attributes.compute

    def fake(db, day, trend=True):
        rows = real(db, day, trend)
        for r in rows:
            r["score"] = DEMO_SCORES[r["key"]]
        return rows
    attributes.compute = fake


def demo_english(db, today):
    """学习页“每周和 Grok 练英语”的示意：几周的练习分钟数，再加一天只打卡没写时长的（按 0 分钟、算一次）。数字是编的。"""
    from datetime import timedelta
    from ethan import english, quests
    english.ensure(db)
    for k, m in ((1, 15), (3, 22), (8, 12), (10, 18), (16, 25), (24, 14)):
        db.conn.execute("INSERT INTO english_sessions (day, minutes, data, at) VALUES (?, ?, '{}', 'x')",
                        ((today - timedelta(days=k)).isoformat(), m))
    h = quests.habit_by_title(db, english.HABIT)
    if h:
        db.conn.execute("INSERT OR IGNORE INTO habit_log (habit_id, day, at, easy) VALUES (?, ?, 'x', 0)",
                        (h["id"], (today - timedelta(days=9)).isoformat()))
    db.conn.commit()


def demo_effort(store):
    """按预计工作量定档（方案 A）的示意：两件好几天的活，再跑一遍补估档的迁移，“日历 · 改计划”顶上出估档清单。"""
    import importlib.util
    from ethan import taskrules
    for title in ("个人管理系统工作台上线", "申请英国人才签证"):
        tid = store.db.add_task(title)
        store.db.conn.execute("UPDATE tasks SET size = 'S' WHERE id = ?", (tid,))
    store.db.conn.commit()
    taskrules.ensure(store.db)
    spec = importlib.util.spec_from_file_location("effort", Path(__file__).resolve().parent.parent / "migrations/20261106_effort_sizes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.run(store)


def main():
    root = Path(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8810
    root.mkdir(parents=True, exist_ok=True)
    store = make_demo(root, datetime.now(LOCAL).date())
    demo_relations(store.db, datetime.now(LOCAL).date())
    demo_system(root, store.db)
    demo_outside(store.db)
    demo_desk(store.db)
    demo_feedback_1105(store.db, datetime.now(LOCAL).date())
    demo_asks_and_gone(store.db, datetime.now(LOCAL).date())
    demo_english(store.db, datetime.now(LOCAL).date())
    demo_effort(store)
    settings = Settings(root / "settings.json")
    settings.set("telegram_owner_chat_id", 1)
    demo_season(store, settings, datetime.now(LOCAL).date())
    wb = Workbench(store, settings, calendar=FakeCalendar())
    app = App(wb, "x" * 40, root)
    (root / "token.txt").write_text(auth.make_session(app.key, app.owner))
    ThreadingHTTPServer(("127.0.0.1", port), make_handler(app)).serve_forever()


if __name__ == "__main__":
    main()
