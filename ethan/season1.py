"""赛季 1 数据重整：按 docs/design/territories.md（2026-10-27 晚她逐块确认）改领地、副本、主线、日常和待办。

先预览再执行：不带 --apply 只打印要改的清单，不动数据库；带 --apply 才改。可重复运行，已经改过的会跳过。
按标题找目标和任务，不依赖编号。
运行：python3 -m ethan.season1 --data <数据目录> [--apply] [--cancel-test-redemption] [--fresh-start 2026-10-28]
"""

import argparse
from pathlib import Path

from .memory import MemoryStore

# 旧领地名 → 七块领地（专注不再是领地，只当复盘指标）
DOMAIN_RENAME = {"健康": "身体", "休闲": "生活体验", "旅行": "生活体验", "感情": "社交"}
TERRITORIES = ["身体", "事业", "财务", "社交", "学习", "学术", "生活体验"]

SELF_MEDIA = "自媒体：example.com 上线，小红书和 Instagram 稳定运营，LinkedIn 主页装修好"
ACADEMIC = "学术：定下 毕业论文方向，写出一页研究计划，摸清进修计划要准备什么"
NEW_GOALS = [  # (标题, 父目标, 领地, 主线步骤 [(标题, 大小, 阶段节点)])
    (SELF_MEDIA, "做出自己的小事业", "事业", [
        ("审定 example.com 的内容大纲（小伴 起草）", "S", False),
        ("example.com 个人主页上线", "M", True),
        ("定好小红书和 Instagram 的账号定位，各发出第一篇", "S", False),
        ("小红书和 Instagram 每周各发 2 条，连续 4 周", "L", True),
        ("LinkedIn 主页装修好", "M", False)]),
    (ACADEMIC, "继续深造", "学术", [
        ("审阅 小伴 整理的进修计划清单（材料、考试、时间线、目标学校和导师要求）", "S", False),
        ("定下 毕业论文方向（研究方向待定）", "M", True),
        ("写出一页研究计划（论文、发表、进修共用）", "L", True)]),
]
# 她的待办（标题, 截止日）
NEW_TODOS = [
    ("接 Monzo：在 Monzo 里授权一次", None),
    ("设好招行消费短信自动转发（按 小伴 发的步骤，在 iPhone 快捷指令里点几下）", None),
    ("导出微信聊天记录，做一次重要的人分析", None),
    ("约年度体检", "2027-07-15"),
]
# 小伴 替她准备的（挂在哪个目标下）
NEW_PREP = [
    ("起草 example.com 的结构和文案初稿（形象、作品集、项目、生活态度、各平台账号、研究、正在探索的项目），给她审", SELF_MEDIA),
    ("整理小红书和 Instagram 的选题库，每周 10 个，给她挑", SELF_MEDIA),
    ("整理进修计划清单：材料、考试、时间线、目标学校和导师要求", ACADEMIC),
    ("每周找好 2–3 篇研究相关论文，写好摘要，留给她的研究时间", ACADEMIC),
    ("查清学校能用的资源（讲座、图书馆数据库），挑 2–3 件具体的给她", None),
    ("让 Grok 按人文目标调研几个想去的国家，签证下来后挑 3 个地方给她", "年底前去 3 个新地方"),
    ("起草 Grok 的“练英语模式”设定，给她粘贴进 Grok", None),
]
# 过时的事（标题里含这些字的没做完的任务）
OBSOLETE = ["看日落", "海边", "万圣夜活动", "活动转票", "附近的网球场", "网球初学者体验课", "对比丹麦和芬兰",
            "今天重新申请单独的签证", "试一次网球体验课"]
# DemoApp 只留要她拍板的步骤，开发交给 agent
FANCY_AGENT_STEPS = ["定出核心用户流程", "完成核心流程的第一个可用版本", "修复主要问题", "根据反馈修改",
                     "准备上线所需的页面", "按上线清单逐项检查"]
GOAL_EDITS = [  # (现标题, 改成, 字段)
    ("常年保持健康体态", {"title": "常年保持健康体态"}),
    ("年底前去 3 个新地方", {"status": "active"}),
    ("创业拿到第一个订单", {"status": "paused", "next_step": "挪到赛季 2；赛季 1 只做前期准备"}),
    ("DemoApp 上线", {"title": "DemoApp 上线（测试项目）"}),
    ("个人管理系统上线", {"domain": "生活体验"}),
]
STEP_RENAMES = [("对比四项体验，选出主爱好并告诉 小伴",
                 "对比试过的体验，选出 2–3 个长期爱好（健身和普拉提、舞蹈，第三个再找）")]
ENGLISH_OLD = "练英语：和 小伴 说 3 句"
ENGLISH_NEW = "英语陪练（Grok）"


def _goal(db, title):
    r = db.conn.execute("SELECT * FROM goals WHERE title = ?", (title,)).fetchone()
    return dict(r) if r else None


def plan(db, cancel_test_redemption: bool = False, fresh_start: str = None, settings=None) -> list:
    """返回 [(说明, 执行函数)]；只列真的要改的。"""
    ch = []

    def add(desc, fn):
        ch.append((desc, fn))

    for old, new in DOMAIN_RENAME.items():
        n = db.conn.execute("SELECT COUNT(*) FROM goals WHERE domain = ?", (old,)).fetchone()[0]
        if n:
            add(f"领地改名：{old} → {new}（{n} 个目标）",
                lambda old=old, new=new: db.conn.execute("UPDATE goals SET domain = ? WHERE domain = ?", (new, old)))

    for title, fields in GOAL_EDITS:
        g = _goal(db, title) or _goal(db, fields.get("title", ""))
        if g is None:
            continue
        todo = {k: v for k, v in fields.items() if g.get(k) != v}
        if todo:
            add(f"改目标「{g['title']}」：" + "，".join(f"{k} → {v}" for k, v in todo.items()),
                lambda gid=g["id"], todo=todo: db.update_goal(gid, source="2026-10-27 领地重整", **todo))

    for title, parent, domain, steps in NEW_GOALS:
        if _goal(db, title):
            continue
        p = _goal(db, parent)

        def make(title=title, p=p, domain=domain, steps=steps):
            gid = db.add_goal(title, "year", parent_id=p["id"] if p else None, status="active", due="2026-12-31",
                              domain=domain, source="2026-10-27 领地重整")
            for n, (st, size, ms) in enumerate(steps, 1):
                db.conn.execute(
                    "INSERT INTO tasks (goal_id, title, done, promised_by_ethan, created_at, quest, size, milestone, seq, draft) "
                    "VALUES (?, ?, 0, 0, datetime('now'), 'main', ?, ?, ?, 1)", (gid, st, size, int(ms), n))
        add(f"新副本「{title}」（{domain}），主线 {len(steps)} 步：" + "；".join(s for s, _, _ in steps), make)

    fancy = _goal(db, "DemoApp 上线") or _goal(db, "DemoApp 上线（测试项目）")
    if fancy:
        rows = db.conn.execute("SELECT id, title FROM tasks WHERE goal_id = ? AND quest = 'main' AND done = 0",
                               (fancy["id"],)).fetchall()
        drop = [r for r in rows if any(k in r["title"] for k in FANCY_AGENT_STEPS)]
        if drop:
            add("DemoApp 主线去掉交给 agent 的 " + str(len(drop)) + " 步：" + "；".join(r["title"] for r in drop),
                lambda ids=[r["id"] for r in drop]: [db.delete_task(i) for i in ids])

    for old, new in STEP_RENAMES:
        r = db.conn.execute("SELECT id FROM tasks WHERE title = ?", (old,)).fetchone()
        if r:
            add(f"改主线步骤：{old} → {new}",
                lambda tid=r["id"], new=new: db.conn.execute("UPDATE tasks SET title = ? WHERE id = ?", (new, tid)))

    gone = db.conn.execute("SELECT id, title, promised_by_ethan FROM tasks WHERE done = 0 AND (" +
                           " OR ".join("title LIKE ?" for _ in OBSOLETE) + ")", [f"%{k}%" for k in OBSOLETE]).fetchall()
    if gone:
        add("删掉过时的 " + str(len(gone)) + " 件：" + "；".join(("〔小伴〕" if r["promised_by_ethan"] else "") + r["title"]
                                                       for r in gone),
            lambda ids=[r["id"] for r in gone]: [db.delete_task(i) for i in ids])

    existing = {r["title"] for r in db.conn.execute("SELECT title FROM tasks")}
    for title, due in NEW_TODOS:
        if title not in existing:
            add(f"加待办：{title}" + (f"（{due} 到期）" if due else ""),
                lambda title=title, due=due: db.add_task(title, due=due))
    for title, goal in NEW_PREP:
        if title not in existing:
            def prep(title=title, goal=goal):
                g = _goal(db, goal) if goal else None
                db.add_task(title, goal_id=g["id"] if g else None, promised_by_ethan=True)
            add(f"加 小伴 准备：{title}", prep)

    stale = db.conn.execute("SELECT COUNT(*) FROM tasks WHERE done = 0 AND (hide_day IS NOT NULL OR "
                            "(quest = 'main' AND (due IS NOT NULL OR star_day IS NOT NULL)))").fetchone()[0]
    if stale:
        add(f"清掉试用时留下的推迟和隐藏标记（{stale} 件，主线步骤不设截止日）",
            lambda: (db.conn.execute("UPDATE tasks SET hide_day = NULL WHERE done = 0"),
                     db.conn.execute("UPDATE tasks SET due = NULL, star_day = NULL WHERE done = 0 AND quest = 'main'")))

    h = db.conn.execute("SELECT * FROM habits WHERE title = ? AND active = 1", (ENGLISH_OLD,)).fetchone()
    if h and db.conn.execute("SELECT 1 FROM habits WHERE title = ?", (ENGLISH_NEW,)).fetchone():
        add(f"停用日常「{ENGLISH_OLD}」，换成周常「{ENGLISH_NEW}」：每周 5 次，达标 +50（打卡记录保留）",
            lambda hid=h["id"]: db.conn.execute("UPDATE habits SET active = 0 WHERE id = ?", (hid,)))
    elif h:
        add(f"日常「{ENGLISH_OLD}」改成周常「{ENGLISH_NEW}」：每周 5 次，达标 +50",
            lambda hid=h["id"]: db.conn.execute(
                "UPDATE habits SET title = ?, cadence = 'weekly', target = 5, points = 50, weekdays = NULL WHERE id = ?",
                (ENGLISH_NEW, hid)))
    if not db.conn.execute("SELECT 1 FROM habits WHERE title = '每天出门'").fetchone():
        add("加日常「每天出门」：按手机定位自动算，先不开（接上定位后再开）",
            lambda: db.conn.execute("INSERT INTO habits (title, cadence, target, points, auto, active, sort) "
                                    "VALUES ('每天出门', 'daily', 1, 10, 'outing', 0, 7)"))

    if cancel_test_redemption:
        r = db.conn.execute("SELECT id, cost FROM redemptions WHERE status = '待策划' ORDER BY id LIMIT 1").fetchone()
        if r:
            add(f"撤销试用时那次兑换（退回 {r['cost']} 分），余额回到正数",
                lambda rid=r["id"]: db.conn.execute("DELETE FROM redemptions WHERE id = ?", (rid,)))
    if fresh_start:  # 2026-10-27 她说“系统从明天开始计算”：试用期的积分、兑换、打卡、休整日清零
        counts = {t: db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("points", "redemptions", "habit_log", "rest_days")}
        if any(counts.values()):
            add("清零试用记录，从 " + fresh_start + " 开始算：" + "，".join(f"{t} {n} 条" for t, n in counts.items() if n),
                lambda: [db.conn.execute(f"DELETE FROM {t}") for t in counts])
        if settings is not None and settings.get("cold_start_from") != fresh_start:
            add(f"系统第 1 天改为 {fresh_start}（原来 {settings.get('cold_start_from')}）",
                lambda: settings.set("cold_start_from", fresh_start))
    return ch


def apply(store: MemoryStore, cancel_test_redemption: bool = False, fresh_start: str = None, settings=None) -> list:
    changes = plan(store.db, cancel_test_redemption, fresh_start, settings)
    for _, fn in changes:
        fn()
    store.db.conn.commit()
    return [d for d, _ in changes]


def main():
    parser = argparse.ArgumentParser(description="赛季 1 数据重整（先预览）")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--apply", action="store_true", help="真的改；不带只预览")
    parser.add_argument("--cancel-test-redemption", action="store_true", help="撤销试用时那次兑换")
    parser.add_argument("--fresh-start", metavar="YYYY-MM-DD", help="清零试用期的积分、兑换、打卡，系统从这天算第 1 天")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    from .core.router import Settings
    settings = Settings(args.data / "settings.json")
    if args.apply:
        done = apply(store, args.cancel_test_redemption, args.fresh_start, settings)
        print(f"改好了 {len(done)} 项：")
        print("\n".join(f"- {d}" for d in done))
    else:
        print("预览（没有改数据）：")
        print("\n".join(f"- {d}" for d, _ in plan(store.db, args.cancel_test_redemption, args.fresh_start, settings))
              or "没有要改的")
    store.close()


if __name__ == "__main__":
    main()
