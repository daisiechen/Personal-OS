import hashlib
import hmac
import json
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from ethan import cards, goals2026, quests
from ethan.core.router import Settings
from ethan.memory import MemoryStore
from ethan.planner import plan_day
from ethan.workbench import auth
from ethan.workbench.api import Workbench, is_night, recent_changes
from ethan.workbench.server import App
from tests.test_planner import FakeCalendar, FakeTravel, Router, good_plan
from tests.test_progress import seed_parents

MONDAY = date(2026, 11, 2)
NOON = datetime(2026, 11, 2, 12, 0)
OWNER = 4242
TOKEN = "123456:" + "A" * 35


def make_demo(root: Path, day: date = MONDAY) -> MemoryStore:
    """一份像样的数据：今年目标、拆好的主线、当天计划（周一有训练）、小伴 在准备的事、头像和今日卡。"""
    store = MemoryStore(root)
    seed_parents(store.db)
    goals2026.apply(store)
    store.db.add_task("想 3 个开场白给她过目", promised_by_ethan=True)
    store.db.add_task("回房东邮件")
    plan_day(store.db, Router(lambda: good_plan(store.db)), day, travel=FakeTravel(), cold_start_from=day)
    Settings(root / "settings.json").set("telegram_owner_chat_id", OWNER)
    Settings(root / "settings.json").set("cold_start_from", day.isoformat())
    av = root / "avatars"
    (av / "library").mkdir(parents=True, exist_ok=True)
    for name in ("avatar-day.jpg", "avatar-night.jpg", "library/S1.png", "library/W2.png", "library/W6.png", "library/E1.png"):
        (av / name).write_bytes(b"\xff\xd8fake")
    return store


class FakeMaker:
    name, model = "openrouter", "x-ai/grok-imagine-image-2.0"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def make(self, prompt, refs=()):
        self.calls.append((prompt, [Path(r).name for r in refs]))
        if self.fail:
            raise RuntimeError("boom")
        return b"\xff\xd8card"


class FakeLedger:
    def __init__(self):
        self.rows = []

    def record(self, backend, model, cost, purpose="chat", **kw):
        self.rows.append((backend, cost, purpose))


def init_data(user_id=OWNER, token=TOKEN, auth_date=None, tamper=False):
    fields = {"auth_date": str(auth_date or int(time.time())), "query_id": "AAE",
              "user": json.dumps({"id": user_id, "first_name": "Mia"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["user"] = json.dumps({"id": 1, "first_name": "x"})
    return urlencode(fields)


class AuthTest(unittest.TestCase):
    def test_init_data_signature_owner_and_age(self):
        self.assertEqual(auth.verify_init_data(init_data(), TOKEN)["id"], OWNER)
        self.assertIsNone(auth.verify_init_data(init_data(tamper=True), TOKEN))
        self.assertIsNone(auth.verify_init_data(init_data(), "999:" + "B" * 35))
        self.assertIsNone(auth.verify_init_data(init_data(auth_date=int(time.time()) - 3 * 86400), TOKEN))
        self.assertIsNone(auth.verify_init_data("", TOKEN))

    def test_session_and_one_time_login(self):
        key = auth.derive_key(TOKEN)
        s = auth.make_session(key, OWNER)
        self.assertTrue(auth.check_session(key, s, OWNER))
        self.assertFalse(auth.check_session(key, s, 1))  # 换了主人就不认
        self.assertFalse(auth.check_session(key, s.replace(".", "x", 1), OWNER))
        self.assertFalse(auth.check_session(key, s, OWNER, now=time.time() + 31 * 86400))
        self.assertFalse(auth.check_session(auth.derive_key("other"), s, OWNER))
        logins = auth.LoginTokens(key)
        t = auth.make_login_token(key)
        self.assertTrue(logins.redeem(t))
        self.assertFalse(logins.redeem(t))  # 只能用一次
        self.assertFalse(logins.redeem(auth.make_login_token(key, now=time.time() - 3600)))


class WorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.cal = FakeCalendar()
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"), calendar=self.cal)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def act(self, action, **p):
        return self.wb.act(action, p, now=NOON)

    def test_week_plan_and_weekly_jobs_off_prep(self):
        """2026-10-31：下周餐单、采购清单、训练每周自动出；小伴 不再记成答应；#172 #173 撤下。"""
        from datetime import date
        from ethan import weekly
        from ethan.core.agent import SYSTEM_JOBS
        w = weekly.get(self.store.db, date(2026, 10, 31))
        self.assertEqual((len(w["this"]["days"]), len(w["this"]["training"])), (7, 5))
        self.assertTrue(w["this"]["shopping"] and w["next"])
        self.assertEqual(weekly.to_tell(self.store.db, date(2026, 11, 1))[0], "week-plan:2026-11-02")  # 周日傍晚说一声
        self.assertTrue(SYSTEM_JOBS.search("直接定好下周七天的餐单和采购清单"))
        self.assertTrue(SYSTEM_JOBS.search("把下周 5 次训练按她的日程排好"))
        self.assertFalse(SYSTEM_JOBS.search("做芬兰 12 月行程方案"))
        import importlib.util
        db = self.store.db
        a = db.add_task("直接定好下周七天的餐单和采购清单", promised_by_ethan=True)
        b = db.add_task("把下周 5 次训练按她的日程排好", promised_by_ethan=True)
        spec = importlib.util.spec_from_file_location("m", Path(__file__).resolve().parent.parent / "migrations" / "20261031_weekly_jobs_off_prep.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m.IDS = {a: ("餐单",), b: ("训练",)}
        self.assertIn("撤下", m.run(self.store))
        self.assertFalse(db.conn.execute("SELECT 1 FROM tasks WHERE id IN (?, ?)", (a, b)).fetchone())

    def test_redeem_remind_not_auto_and_mark_delivered(self):
        """2026-11-01：积分够了只提醒、不替她换；兑换记录她能自己点“兑现了”。"""
        from datetime import datetime
        from ethan import nudges, quests
        db = self.store.db
        quests.seed(db)
        r = min(quests.rewards(db), key=lambda x: x["cost"])
        db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (?, '测试', NULL, '2026-11-01', datetime('now'))", (r["cost"],))
        key, text = nudges._can_redeem(db, datetime(2026, 11, 1, 12), {})
        self.assertIn("不要替她换", text)
        self.assertEqual(db.conn.execute("SELECT COUNT(*) FROM redemptions").fetchone()[0], 0)
        nudges.mark_told(db, [key])
        self.assertIsNone(nudges._can_redeem(db, datetime(2026, 11, 1, 13), {}))
        self.assertTrue(self.act("reward.redeem", id=r["id"])["ok"])
        rid = db.conn.execute("SELECT id FROM redemptions").fetchone()[0]
        self.assertTrue(self.act("reward.delivered", id=rid)["ok"])
        self.assertEqual(db.conn.execute("SELECT status FROM redemptions").fetchone()[0], "已兑现")

    def test_refund_misclicked_redemptions_migration(self):
        import importlib.util
        from ethan import quests
        db = self.store.db
        quests.seed(db)
        rid = min(quests.rewards(db), key=lambda x: x["cost"])["id"]
        db.conn.execute("INSERT INTO points (delta, reason, ref, day, at) VALUES (900, '测试', NULL, '2026-10-29', datetime('now'))")
        for at in ("2026-10-29T07:25:23", "2026-10-30T10:31:41"):
            db.conn.execute("INSERT INTO redemptions (reward_id, cost, status, at) VALUES (?, 300, '待策划', ?)", (rid, at))
        before = quests.totals(db)["balance"]
        spec = importlib.util.spec_from_file_location("m", Path(__file__).resolve().parent.parent / "migrations" / "20261101_refund_misclicks.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("600 分", m.run(self.store))
        self.assertEqual(quests.totals(db)["balance"], before + 600)
        self.assertIn("没有要退的", m.run(self.store))

    def test_me_pending_buttons_reach_resolve(self):
        """2026-10-31 她在待你定点“对”提示“不认识的操作”：定法放 how，不能放 action。"""
        from ethan import lifebase, mepage
        lifebase.ensure(self.store.db)
        ev = lifebase.add_evidence(self.store.db, "chat", "raw:x", "2026-10-30", "她说的")
        fid = lifebase.add_fact(self.store.db, "rhythms", "晚上容易饿到睡不着", [ev])
        pid = mepage.add_question(self.store.db, "最近晚上还是容易饿到睡不着吗？", fid)
        self.assertTrue(self.act("me.resolve", id=pid, how="yes")["ok"])
        self.assertEqual(mepage.pending_count(self.store.db), 0)
        app = (Path(__file__).resolve().parent.parent / "ethan" / "workbench" / "static" / "app.js").read_text(encoding="utf-8")
        block = app[app.index('["me-pick", "me-yes"'):app.index('meAct("me.resolve"')]
        self.assertNotIn("action", block)

    def test_today_tasks_follow_her_drag_order(self):
        ids = [t["id"] for t in self.wb.state(NOON)["today"] if not t["done"]]
        self.assertGreater(len(ids), 2)
        mine = [ids[-1], ids[0]]
        self.assertTrue(self.act("today.order", ids=mine)["ok"])
        self.assertEqual([t["id"] for t in self.wb.state(NOON)["today"]][:2], mine)  # 拖过的在前，没拖的跟在后面

    def test_weigh_habit_only_on_training_days_and_workaholic_band(self):
        """2026-11-01：称体重只在训练日出现；工作狂指数看健康区间，不是越低越好。"""
        import importlib
        from ethan import quests, workaholic
        quests.seed(self.store.db)
        if not self.store.db.conn.execute("SELECT 1 FROM habits WHERE title = '训练前称体重'").fetchone():
            quests.add_habit(self.store.db, "训练前称体重")
        m = importlib.import_module("migrations.20261101_weigh_on_training_days")
        self.assertIn("只在训练日", m.run(self.store))
        h = next(x for x in quests.habits(self.store.db) if x["title"] == "训练前称体重")
        self.assertTrue(quests.due_today(h, MONDAY))
        self.assertFalse(quests.due_today(h, MONDAY + timedelta(days=5)))  # 周六不是训练日
        self.assertEqual([workaholic.band(i)["label"] for i in (10, 30, 55, 80)], ["偏松", "健康", "偏高", "过度"])

    def test_paused_goal_items_not_listed_as_skipped(self):
        """2026-11-01：修好之前排的计划里还有暂停副本的事，“今天没排进”显示时也要滤掉。"""
        db = self.store.db
        gid = db.add_goal("拿下第一个订单", "year", status="paused")
        db.add_task("列出 20 个潜在客户", goal_id=gid)
        brief = json.dumps({"skipped": [{"title": "列出 20 个潜在客户", "why": "今天满了"},
                                        {"title": "写论文大纲", "why": "这个赛季不做"}, {"title": "买菜", "why": "明天去"}]})
        db.save_plan(NOON.date().isoformat(), "model", "{}", brief, "fp", [])
        plan = db.latest_plan(NOON.date().isoformat())
        self.assertEqual([x["title"] for x in self.wb.plan_panel(NOON.date(), plan)["skipped"]], ["买菜"])

    def test_apple_busy_serves_stale_cache_while_refreshing(self):
        """她 2026-11-02：切页面要好久。读 iCloud 要 7–10 秒，缓存过期也先用旧的、后台去刷，不让她等。"""
        import threading
        from datetime import datetime, timedelta
        calls, gate = [], threading.Event()

        class Cal:
            def busy(self, d):
                calls.append(d)
                if len(calls) > 1:
                    gate.wait(5)
                return [("15:00", "17:00", f"约{len(calls)}", {})]
        wb = Workbench(self.store, Settings(self.root / "settings.json"), calendar=Cal(), background=True)
        day = NOON.date()
        self.assertEqual(wb._apple_busy(day)[0][2], "约1")  # 当天头一次：当场读
        wb._busy_cache = (wb._busy_cache[0], datetime.now() - timedelta(minutes=6), wb._busy_cache[2])
        self.assertEqual(wb._apple_busy(day)[0][2], "约1")  # 过期了：先给旧的，不等
        gate.set()
        for _ in range(50):
            if not wb._busy_refreshing:
                break
            threading.Event().wait(0.05)
        self.assertEqual(wb._apple_busy(day)[0][2], "约2")  # 后台刷好了

    def test_plan_shows_apple_calendar_and_mail_bookings(self):
        """她 2026-11-02：两家饭店的约在“日历 · 改计划”里看不到，也删不掉；要和苹果日历对得上。"""
        from ethan import events
        day = NOON.date().isoformat()
        fid = events.add(self.store.db, day, "15:00", "17:00", "The Devonshire 午餐", "Denman St", note="来自邮件")
        self.store.db.conn.execute("UPDATE future_events SET uid = 'ethan-event-1' WHERE id = ?", (fid,))
        deleted = []

        class Cal:
            def busy(self, d):
                return [("15:00", "17:00", "伦敦最火Sunday Roast", {"location": "", "href": "https://cal/home/roast.ics", "recurring": False}),
                        ("08:00", "09:00", "每周例会", {"href": "https://cal/home/weekly.ics", "recurring": True})]

            def delete_event(self, href):
                deleted.append(("DELETE-OWN", href))

            def ethan_calendar(self):
                return "https://cal/ethan/"

            def request(self, method, url, *a, **k):
                deleted.append((method, url))

            def sync_day(self, *a, **k):
                return {}
        self.wb.calendar = Cal()
        ev = self.wb.plan_panel(NOON.date(), {"items": [], "brief": "{}"})["events"]
        titles = {e["title"]: e for e in ev}
        self.assertIn("伦敦最火Sunday Roast", titles)
        self.assertEqual(titles["The Devonshire 午餐"]["clash_with"], ["伦敦最火Sunday Roast"])
        got = self.wb.act("future.delete", {"id": fid}, NOON)
        self.assertTrue(got["ok"])
        self.assertEqual(deleted, [("DELETE", "https://cal/ethan/ethan-event-1.ics")])
        self.assertEqual(events.for_day(self.store.db, NOON.date()), [])
        # 她自己日历上的也能删（她 2026-11-02：我在工作台确认删除了，那肯定就是我要删的）；重复的不在这里删
        self.assertTrue(self.wb.act("calendar.delete", {"href": "https://cal/home/roast.ics"}, NOON)["ok"])
        self.assertIn(("DELETE-OWN", "https://cal/home/roast.ics"), deleted)
        self.assertFalse(self.wb.act("calendar.delete", {"href": "https://cal/home/weekly.ics"}, NOON)["ok"])
        self.assertFalse(self.wb.act("calendar.delete", {"href": "https://evil/x.ics"}, NOON)["ok"])

    def test_home_timeline_follows_deleted_booking(self):
        """她 2026-11-02：日历里删了一家饭店，回到首页时间线没变，还得点“让 AI 重排”。"""
        from ethan import events
        db, day = self.store.db, NOON.date()
        fid = events.add(db, day.isoformat(), "15:00", "17:00", "The Devonshire 午餐", note="来自邮件")
        facts = json.dumps({"busy": [{"title": "The Devonshire 午餐", "start": "15:00", "end": "17:00", "leave": "14:20", "back": "17:40", "place": ""},
                                     {"title": "伦敦最火Sunday Roast", "start": "15:00", "end": "17:00", "leave": "15:00", "back": "17:00"}]})
        db.save_plan(day.isoformat(), "model", facts, "{}", "fp", [])
        text = lambda: " ".join(t["text"] for t in self.wb.state(NOON)["timeline"])
        self.assertIn("Devonshire", text())
        self.wb.act("future.delete", {"id": fid}, NOON)
        self.assertNotIn("Devonshire", text())
        self.assertIn("Sunday Roast", text())  # 读不到她的日历时，她自己的照早上的算

    def test_moved_training_keeps_trip_and_survives_replan(self):
        """她 2026-11-02：训练从 4 点改到 5 点，首页那条点不开了，也不知道重排会不会改回去。"""
        from ethan import planner
        db, day = self.store.db, NOON.date()
        db.save_plan(day.isoformat(), "model", "{}", "{}", "fp",
                     [{"kind": "event", "start": "16:00", "end": "17:15", "title": "训练：上肢", "place": "健身房", "travel_min": 45}])
        iid = db.latest_plan(day.isoformat())["items"][0]["id"]
        self.assertTrue(self.wb.act("event.time", {"id": iid, "start": "17:00"}, NOON)["ok"])
        tl = self.wb.state(NOON)["timeline"]
        leave = next(t for t in tl if "出门" in t["text"])
        self.assertEqual(leave["at"], "16:15")
        self.assertIn("trip", leave)  # 点得开
        items = planner.apply_overrides(db, day, [{"kind": "event", "start": "16:00", "end": "17:15", "title": "训练：上肢",
                                                   "leave": "15:15", "remind": "14:15", "back": "17:40"}])
        self.assertEqual((items[0]["start"], items[0]["leave"], items[0]["back"]), ("17:00", "16:15", "18:40"))

    def test_today_and_edit_page_list_the_same_tasks(self):
        """她 2026-11-02：首页今日任务三条，编辑页两条（截止在以后的主线步骤被编辑页漏掉）；快到期的、邮件来的也进今天。
        11-04 她定：快到期只提前 2 天（5 天时 11-08、11-09 的事都排进了今天）。"""
        db = self.store.db
        gid = db.add_goal("年底前去 3 个新国家", "year", status="active")
        tid = db.add_task("11/12 前没出签就催问", due=(NOON.date() + timedelta(days=10)).isoformat(), goal_id=gid)
        db.conn.execute("UPDATE tasks SET quest = 'main' WHERE id = ?", (tid,))
        soon = db.add_task("两天内回复芬兰使馆补材料", due=(NOON.date() + timedelta(days=2)).isoformat())
        later = db.add_task("下周交的材料", due=(NOON.date() + timedelta(days=4)).isoformat())
        plan = {"items": [{"kind": "todo", "task_id": tid, "priority": "main", "how": "下一步", "title": "催问"}]}
        today = [t["id"] for t in self.wb.today_tasks(NOON.date(), plan)["today"]]
        self.assertIn(tid, today)
        self.assertIn(soon, today)
        self.assertNotIn(later, today)  # 4 天后才截止的不进今天
        db.save_plan(NOON.date().isoformat(), "model", "{}", "{}", "fp", [])
        edit = [t["id"] for t in self.wb.plan_panel(NOON.date(), {"items": plan["items"], "brief": "{}"})["todos"]]
        self.assertIn(tid, edit)

    def test_habit_linked_main_step_hidden_from_plan(self):
        """2026-11-01：“连续一周训练前称体重”改成引用日常，计划和今天的任务里不再单独出现，副本详情里还在。"""
        from ethan import quests, stages
        db = self.store.db
        stages.ensure(db)
        gid = db.add_goal("减脂", "year", status="active")
        tid = db.add_task("连续一周训练前称体重", goal_id=gid)
        db.conn.execute("UPDATE tasks SET quest = 'main' WHERE id = ?", (tid,))
        db.conn.execute("INSERT OR IGNORE INTO habits (title, cadence, target, points) VALUES ('训练前称体重', 'daily', 1, 10)")
        db.conn.execute("UPDATE goals SET status = 'active' WHERE id = ?", (gid,))
        m = __import__("importlib").import_module("migrations.20261101_weigh_step_is_habit")
        self.assertIn("1 条", m.run(self.store))
        plan = {"items": [{"kind": "todo", "task_id": tid, "priority": "main", "how": "下一步", "title": "连续一周训练前称体重"}]}
        self.assertNotIn(tid, [t["id"] for t in self.wb.today_tasks(NOON.date(), plan)["today"]])  # 改之前排的计划里也不显示
        self.assertNotIn(tid, [t["id"] for t in quests.main_quests(db).get(gid, [])])
        self.assertIn(tid, [t["id"] for t in quests.main_quests(db, with_agent_goals=True).get(gid, [])])

    def test_explore_editor_hides_achievements_sheet_and_edits_visa(self):
        """2026-11-01：成就表不进编辑器；国内表没有签证困难；签证内容能改。"""
        (self.root / "explore-map.json").write_text(json.dumps({"sheets": {
            "中国": [{"省份": "安徽省", "城市": "安庆市"}], "欧洲": [{"国家": "芬兰", "签证": "申根", "区域": "北欧"}],
            "成就": [{"成就": "走遍中国", "状态": "进行中"}]}}, ensure_ascii=False))
        page = self.wb.explore_page()
        self.assertEqual(list(page["sheets"]), ["中国", "欧洲"])
        self.assertNotIn("签证困难", page["statuses"]["中国"])
        self.assertTrue(self.wb.act("explore.set", {"sheet": "欧洲", "i": 0, "field": "签证", "value": "申根 90 天"}, NOON)["ok"])
        self.assertTrue(self.wb.act("explore.set", {"sheet": "中国", "i": 0, "status": "已结束"}, NOON)["ok"])
        self.assertFalse(self.wb.act("explore.set", {"sheet": "中国", "i": 0, "status": "签证困难"}, NOON)["ok"])
        self.assertEqual(self.wb.explore_page()["sheets"]["欧洲"][0]["签证"], "申根 90 天")

    def test_reline_rewrites_only_the_line(self):
        """2026-11-01：卡上 小伴 的话规则改了“现在就该”生效：只重写那句话，不重画图。"""
        from ethan import cards
        (self.root / "cards").mkdir(exist_ok=True)
        path = self.root / "cards" / "2026-11-01.json"
        path.write_text(json.dumps({"title": "升级时刻", "line": "按计划一件件来", "shot": {"line": "按计划一件件来", "scene": "arcade"}}))

        class R:
            def complete(self, system, messages, purpose=""):
                assert "小宝" in system and "按计划一件件来" in messages[0]["content"]
                return type("T", (), {"text": "“小宝，今天升级也要先吃饭哦”"})()
        self.assertEqual(cards.reline(self.root, self.store.db, date(2026, 11, 1), R()), "小宝，今天升级也要先吃饭哦")
        got = json.loads(path.read_text())
        self.assertEqual((got["line"], got["shot"]["line"], got["old_line"]), ("小宝，今天升级也要先吃饭哦",) * 2 + ("按计划一件件来",))

    def test_make_up_yesterdays_check_in(self):
        from ethan import quests
        st = self.wb.state(NOON)
        h = st["habits"]["daily"][0]
        self.assertFalse(h["yesterday"])
        got = self.wb.act("habit.check", {"id": h["id"], "yesterday": 1}, NOON)
        self.assertIn("补上了昨天", got["msg"])
        self.assertTrue(quests.checked(self.store.db, h["id"], NOON.date() - timedelta(days=1)))
        self.assertFalse(quests.checked(self.store.db, h["id"], NOON.date()))

    def test_social_log_from_workbench_counts(self):
        """2026-11-01 她问社交的“零次”怎么来的、没有地方填：领域页里自己记一笔。"""
        from ethan import lifelog
        got = self.wb.act("life.log", {"kind": "meet", "who": "小林、阿May", "day": "2026-11-01"}, NOON)
        self.assertTrue(got["ok"])
        self.assertEqual(lifelog.count(self.store.db, "meet", date(2026, 11, 1), date(2026, 11, 2)), 2)
        self.assertFalse(self.wb.act("life.log", {"kind": "meet", "who": "x", "day": "2026-12-01"}, NOON)["ok"])
        self.assertFalse(self.wb.act("life.log", {"kind": "bad", "who": "x"}, NOON)["ok"])

    def test_goal_task_added_with_node_and_dragged_into_order(self):
        """2026-11-01：副本里加的是任务（只写内容、选节点），拖动排序。"""
        db = self.store.db
        gid = db.conn.execute("INSERT INTO goals (title, level, status, updated_at) VALUES ('拖动副本', 'year', 'active', 'x')").lastrowid
        a = self.wb.act("step.add", {"goal_id": gid, "title": "甲"}, NOON)["id"]
        b = self.wb.act("step.add", {"goal_id": gid, "title": "乙", "milestone": 1}, NOON)["id"]
        c = self.wb.act("step.add", {"goal_id": gid, "title": "丙"}, NOON)["id"]
        self.assertFalse(self.wb.act("step.add", {"goal_id": gid, "title": " "}, NOON)["ok"])
        self.assertTrue(self.wb.act("step.order", {"goal_id": gid, "ids": [c, a, b, 99999]}, NOON)["ok"])
        ch = next(x for x in self.wb.quests_page(NOON)["chapters"] if x["goal_id"] == gid)
        self.assertEqual([(s["title"], s["milestone"]) for s in ch["steps"]], [("丙", False), ("甲", False), ("乙", True)])

    def test_main_steps_reorder_and_mark_node(self):
        """2026-11-01 她要在主副本里加节点、调顺序。"""
        db = self.store.db
        gid = db.conn.execute("INSERT INTO goals (title, level, status, updated_at) VALUES ('排序副本', 'year', 'active', '2026-11-01')").lastrowid
        a = self.wb.act("task.add", {"title": "第一步", "goal_id": gid, "when": "later"}, NOON)["id"]
        b = self.wb.act("task.add", {"title": "第二步", "goal_id": gid, "when": "later"}, NOON)["id"]
        self.assertTrue(self.wb.act("task.move", {"id": b, "dir": -1}, NOON)["ok"])
        order = [r[0] for r in db.conn.execute("SELECT id FROM tasks WHERE goal_id = ? ORDER BY seq", (gid,))]
        self.assertEqual(order, [b, a])
        self.assertTrue(self.wb.act("task.milestone", {"id": a, "on": 1}, NOON)["ok"])
        ch = next(c for c in self.wb.quests_page(NOON)["chapters"] if c["goal_id"] == gid)
        self.assertEqual([(s["title"], s["milestone"]) for s in ch["steps"]], [("第二步", False), ("第一步", True)])

    def test_goal_without_steps_can_take_a_first_step(self):
        """2026-11-01 她在 DemoApp 副本里点“加一步”，下拉里没有 DemoApp：还没拆主线的进行中副本也要能挂待办。"""
        db = self.store.db
        gid = db.conn.execute("INSERT INTO goals (title, level, status, updated_at) VALUES ('新副本：试试', 'year', 'active', '2026-11-01')").lastrowid
        db.conn.commit()
        st = self.wb.state(NOON)
        self.assertIn(gid, [g["id"] for g in st["goals"]])
        b = next(x for x in st["bosses"] if x["goal_id"] == gid)
        self.assertEqual((b["steps_done"], b["steps_total"]), (0, 0))

    def test_state_has_everything_the_main_screen_needs(self):
        st = self.wb.state(NOON)
        self.assertEqual(st["profile"]["day_n"], 1)
        self.assertTrue(st["plan_ready"])
        titles = [t["title"] for t in st["today"]]
        self.assertTrue(st["today"][0]["tag"].startswith("★ 减脂"))  # 主副本排最前
        self.assertEqual(len([t for t in st["today"] if t["main"]]), 5)  # 每个副本只有下一步算今天
        self.assertIn("冷启动：财务（小伴 带你做，10–15 分钟）", titles)  # 计划器今天新建的
        self.assertNotIn("回房东邮件", titles)  # 没截止的支线要她自己 ★
        self.assertTrue(st["goals"])
        self.assertTrue(any("训练" in t["text"] for t in st["timeline"]))
        self.assertTrue(st["detail"]["train"].startswith("🏋️ 训练"))  # 早报卡片去掉按钮后，训练和三餐详情放这里
        self.assertIn("meals", st["detail"])
        self.assertTrue(st["meals"]["meals"][0]["items"] and st["meals"]["meals"][0]["steps"])  # 三餐拆成卡片排版
        self.assertTrue(st["training"]["exercises"][0]["cues"])  # 训练详情排成动作卡
        self.assertIn("出发", st["training"]["trip"])
        self.assertEqual(st["prep"][0]["title"], "想 3 个开场白给她过目")
        self.assertTrue(st["bosses"][0]["main"])
        self.assertEqual(len(st["habits"]["daily"]), 5)
        self.assertTrue(any(e["title"].startswith("训练") for e in st["planner"]["events"]))
        self.assertIn("回房东邮件", [t["title"] for t in st["planner"]["todos"]])
        self.assertIsNone(st["card"])
        self.assertFalse(st["night"])

    def test_check_habit_then_undo(self):
        h = self.wb.state(NOON)["habits"]["daily"][0]
        r = self.act("habit.check", id=h["id"])
        self.assertEqual((r["ok"], r["gain"]), (True, 10))
        self.assertEqual(r["undo"], {"action": "habit.uncheck", "id": h["id"]})
        self.assertEqual(self.act("habit.uncheck", id=h["id"])["gain"], -10)
        self.assertEqual(quests.totals(self.store.db)["earned"], 0)
        self.assertEqual(self.act("habit.check", id=h["id"], easy=True)["gain"], 5)

    def test_complete_task_and_undo(self):
        t = self.wb.state(NOON)["today"][0]
        r = self.act("task.done", id=t["id"])
        self.assertEqual(r["gain"], t["points"])
        today = self.wb.state(NOON)["today"]
        self.assertTrue(today[-1]["done"] and today[-1]["id"] == t["id"])  # 做完的今天还在，排到最后
        self.assertFalse(today[0]["done"])
        self.act(**r["undo"])
        self.assertEqual(quests.totals(self.store.db)["earned"], 0)

    def test_add_star_postpone_delete_restore(self):
        n = len(self.wb.state(NOON)["today"])
        r = self.act("task.add", title="买运动袜", when="today")
        st = self.wb.state(NOON)
        self.assertEqual(st["today"][0]["title"], "买运动袜")  # 加到今天就是今日任务，排第一
        self.assertEqual(len(st["today"]), n + 1)  # 不限件数
        self.act("task.postpone", id=r["id"])
        self.assertEqual(len(self.wb.state(NOON)["today"]), n)  # 推走不补别的进来
        self.assertNotIn("买运动袜", [t["title"] for t in self.wb.state(NOON)["today"]])
        self.assertNotIn("买运动袜", [t["title"] for t in self.wb.state(NOON)["planner"]["todos"]])  # 明天的不在今天
        tomorrow = datetime.combine(MONDAY + timedelta(days=1), NOON.time())
        self.assertIn("买运动袜", [t["title"] for t in self.wb.state(tomorrow)["planner"]["todos"]])
        d = self.act("task.delete", id=r["id"])
        self.assertIsNone(self.store.db.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (r["id"],)).fetchone())
        self.act(**d["undo"])
        self.assertIsNotNone(self.store.db.conn.execute("SELECT 1 FROM tasks WHERE id = ?", (r["id"],)).fetchone())

    def test_star_has_no_limit_and_goal_quest(self):
        side = [self.act("task.add", title=f"支线{n}", when="later")["id"] for n in range(8)]
        self.assertTrue(all(self.act("task.star", id=i, on=True)["ok"] for i in side))
        self.assertTrue(set(side) <= {t["id"] for t in self.wb.state(NOON)["today"]})
        gid = self.store.db.conn.execute("SELECT id FROM goals WHERE title LIKE '创业%'").fetchone()[0]
        r = self.act("task.add", title="报价单初稿", goal_id=gid)
        row = self.store.db.conn.execute("SELECT quest, goal_id FROM tasks WHERE id = ?", (r["id"],)).fetchone()
        self.assertEqual((row["quest"], row["goal_id"]), ("main", gid))

    def test_star_carries_over_until_done_or_unstarred(self):
        """她 2026-11-02：点了 ★ 的事没做完，自动留到第二天、星星也留着，直到做完或取消星星。"""
        from datetime import timedelta
        a = self.act("task.add", title="回复导师邮件", when="later")["id"]
        b = self.act("task.add", title="整理照片", when="later")["id"]
        self.act("task.star", id=a, on=True)
        self.act("task.star", id=b, on=True)
        day2, day3 = NOON + timedelta(days=1), NOON + timedelta(days=2)
        today2 = {t["id"]: t for t in self.wb.state(day2)["today"]}
        self.assertTrue(today2[a]["star"] and today2[b]["star"])  # 第二天还在、星星还在
        self.wb.act("task.done", {"id": a}, now=day2)
        self.wb.act("task.star", {"id": b, "on": False}, now=day2)
        today2 = {t["id"]: t for t in self.wb.state(day2)["today"]}
        self.assertNotIn(b, today2)  # 取消星星就不再进今天
        today3 = {t["id"] for t in self.wb.state(day3)["today"]}
        self.assertNotIn(a, today3)  # 做完了，第三天不再出现
        self.assertNotIn(b, today3)

    def test_events_edit_and_sync_calendar(self):
        r = self.act("event.add", title="看牙", start="10:00", place="牙医")
        self.assertTrue(r["ok"])
        self.assertIn("看牙", [t for _, t in self.cal.synced[-1][1]])
        self.act("event.time", id=r["id"], start="11:30")
        ev = next(e for e in self.wb.state(NOON)["planner"]["events"] if e["title"] == "看牙")
        self.assertEqual((ev["start"], ev["end"]), ("11:30", "12:30"))
        self.store.db.set_calendar_uid(r["id"], "uid-x")
        self.act("event.delete", id=r["id"])
        self.assertEqual(self.cal.synced[-1][2], ["uid-x"])  # 旧的从日历删掉

    def test_rest_day_redeem_and_bad_input(self):
        self.assertTrue(self.act("rest.take")["ok"])
        self.assertTrue(self.wb.state(NOON)["habits"]["rest_day"])
        reward = quests.rewards(self.store.db)[0]
        self.assertFalse(self.act("reward.redeem", id=reward["id"])["ok"])
        self.assertFalse(self.act("task.done", id=99999)["ok"])
        self.assertFalse(self.act("nope")["ok"])
        self.assertFalse(self.act("task.add", title="  ")["ok"])

    def test_changes_are_logged_for_ethan(self):
        self.act("task.add", title="买运动袜")
        at = datetime.fromisoformat(recent_changes(self.root, now=NOON.replace(tzinfo=timezone.utc))[-1]["at"])
        self.assertEqual(recent_changes(self.root, now=at)[-1]["text"], "加待办：买运动袜（今日任务）")
        self.assertEqual(recent_changes(self.root, now=at + timedelta(days=2)), [])

    def test_undo_cannot_spend_points_twice(self):
        t = self.wb.state(NOON)["today"][0]
        self.act("task.done", id=t["id"])
        quests.award(self.store.db, 2500 - t["points"], "测试", MONDAY)
        reward = quests.rewards(self.store.db)[0]
        self.assertTrue(self.act("reward.redeem", id=reward["id"])["ok"])  # 余额用光
        r = self.act("task.undo", id=t["id"])
        self.assertFalse(r["ok"])
        self.assertIn("兑换", r["msg"])
        self.assertEqual(quests.totals(self.store.db)["balance"], 0)
        self.assertTrue(self.wb.state(NOON)["today"][-1]["done"])  # 还是完成状态
        self.assertIn(quests.rewards(self.store.db)[0]["note"], quests.TEASERS["小"])

    def test_make_card_on_demand(self):
        self.assertFalse(self.act("card.make")["ok"])  # 没配画卡的就说画不了
        calls = []

        class Runner:
            def __call__(self):
                calls.append(1)
                return len(calls) == 1

            def busy(self):
                return bool(calls)

        self.wb.card_runner = Runner()
        self.assertIn("正在画", self.act("card.make")["msg"])
        self.assertIn("还在画", self.act("card.make")["msg"])
        self.assertTrue(self.wb.state(NOON)["card_making"])

    def test_calendar_sync_in_background(self):
        wb = Workbench(self.store, Settings(self.root / "settings.json"), calendar=self.cal, background=True)
        r = wb.act("event.add", {"title": "看牙", "start": "10:00"}, now=NOON)
        self.assertIn("正在同步苹果日历", r["msg"])
        wb._flush_sync()
        self.assertIn("看牙", [t for _, t in self.cal.synced[-1][1]])
        wb._sync_pool.shutdown()

    def test_team_page_and_rating(self):
        from ethan import agents
        done = agents.record_done(self.store.db, "codex", "补测试", 2, project="Agent 团队", cost_usd=0.5)
        agents.add(self.store.db, "codex", "下一件", size=1)
        page = self.wb.team_page()
        cols = {c["key"]: c["tasks"] for c in page["columns"]}
        self.assertEqual([t["title"] for t in cols["queued"]], ["下一件"])
        self.assertEqual([t["id"] for t in cols["accepted"]], [done])
        self.assertTrue(self.act("agent.rate", id=done, rating=5)["ok"])
        page = self.wb.team_page()  # 评过的从“通过”挪进归档
        self.assertEqual({c["key"]: c["tasks"] for c in page["columns"]}["accepted"], [])
        self.assertEqual([t["id"] for t in page["archive"]], [done])
        board = {r["agent"]: r for r in self.wb.team_page()["scoreboard"]}
        self.assertEqual(board["codex"]["efficiency"], 86)  # 1.73 ÷ 2 点
        self.assertFalse(self.act("agent.rate", id=done, rating=6)["ok"])
        self.assertIsNone(self.wb.team_page()["claude_sessions"]["at"])  # 还没同步过
        agents.save_sessions(self.store.root, [{"title": "空闲的", "state": "completed", "where": "mac", "updated_at": "2026-10-28T09:00:00+00:00"},
                                               {"title": "DemoApp", "state": "working", "where": "cloud", "updated_at": "2026-10-28T08:00:00+00:00"},
                                               {"title": "坏的", "state": "<script>", "where": "x"}])
        cs = self.wb.team_page()["claude_sessions"]
        self.assertEqual([s["title"] for s in cs["sessions"]], ["DemoApp", "空闲的", "坏的"])  # 在干的排最前
        self.assertEqual((cs["sessions"][0]["state_label"], cs["sessions"][0]["where_label"]), ("在干", "云端"))
        self.assertEqual(cs["sessions"][2]["state"], "idle")

    def test_quests_page(self):
        q = self.wb.quests_page(NOON)
        self.assertTrue(q["chapters"][0]["main"])
        self.assertEqual([s["state"] for s in q["chapters"][0]["steps"][:2]], ["now", "locked"])
        self.assertEqual(len(q["dailies"][0]["week"]), 7)
        self.assertEqual([r["cost"] for r in q["rewards"]], [2500, 10000, 30000])

    def test_night(self):
        self.assertTrue(is_night(datetime(2026, 11, 2, 18, 0)))
        self.assertFalse(is_night(datetime(2026, 11, 2, 9, 0)))
        self.assertFalse(is_night(datetime(2026, 6, 2, 20, 0)))


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.app = App(Workbench(self.store, Settings(self.root / "settings.json")), TOKEN, self.root)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, url, headers=None, body=None):
        status, h, data = self.app.handle(method, url, headers or {}, json.dumps(body).encode() if body else b"")
        return status, h, data

    def test_needs_login_except_page(self):
        self.assertEqual(self.call("GET", "/")[0], 200)
        self.assertIn(b"Earth Online", self.call("GET", "/")[2])
        self.assertEqual(self.call("GET", "/static/app.js")[0], 200)
        self.assertEqual(self.call("GET", "/static/../server.py")[0], 404)
        for url in ("/api/state", "/api/quests", "/img/avatar/day"):
            self.assertEqual(self.call("GET", url)[0], 401)
        self.assertEqual(self.call("POST", "/api/act", body={"action": "rest.take"})[0], 401)

    def test_webapp_login_only_owner(self):
        self.assertEqual(self.call("POST", "/api/auth/webapp", body={"initData": init_data(user_id=1)})[0], 401)
        status, h, data = self.call("POST", "/api/auth/webapp", body={"initData": init_data()})
        self.assertEqual(status, 200)
        token = json.loads(data)["token"]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        status, _, data = self.call("GET", "/api/state", {"Authorization": f"Bearer {token}"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["profile"]["level"], 1)
        status, _, data = self.call("POST", "/api/act", {"Cookie": f"eo_session={token}"}, {"action": "rest.take"})
        self.assertTrue(json.loads(data)["ok"])
        self.assertEqual(self.call("GET", "/img/avatar/night", {"Cookie": f"eo_session={token}"})[0], 200)
        self.assertEqual(self.call("GET", f"/img/avatar/night?s={token}", {})[0], 200)  # 图片标签用查询串
        self.assertEqual(self.call("GET", f"/api/state?s={token}", {})[0], 401)  # 别的接口不认查询串

    def test_static_files_are_versioned(self):
        from ethan.workbench import VERSION
        status, headers, body = self.call("GET", "/", {})
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(f'/static/app.js?v={VERSION}"'.encode(), body)
        self.assertIn(f'/static/app.css?v={VERSION}"'.encode(), body)
        self.assertIn("immutable", self.call("GET", f"/static/app.js?v={VERSION}", {})[1]["Cache-Control"])
        self.assertEqual(self.call("GET", "/static/app.js?v=old", {})[1]["Cache-Control"], "no-store")

    def test_map_labels_dont_reuse_task_tag_class(self):
        # 2026-10-28：地图标签用了 .tag，把今日任务的小标签也变成了绝对定位，排版全乱
        import re
        from ethan.workbench.server import STATIC
        css = (STATIC / "app.css").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"(^|\})\s*\.tag\b", css, re.M))
        self.assertIn("padding: env(safe-area-inset-top)", css)  # 加到 iPad 主屏幕后不钻到状态栏下面

    def test_every_exercise_has_a_demo_figure(self):
        import re
        from ethan import training
        from ethan.workbench.server import STATIC
        js = (STATIC / "moves.js").read_text(encoding="utf-8")
        drawn = set(re.findall(r"^  (\w+): \{", js, re.M))
        lib, _ = training.load()
        self.assertEqual(set(lib) - drawn, set())  # 动作库里新加动作，要记得给它画小人
        self.assertIn('/static/moves.js?v=', self.call("GET", "/", {})[2].decode())

    def test_device_link_logs_in_until_revoked(self):
        owner = self.app.owner
        token = auth.make_device_token(self.app.key, 1)
        status, h, body = self.call("GET", f"/d/{token}")
        self.assertEqual(status, 200)
        self.assertIn(b"apple-touch-icon", body)  # 加到主屏幕有图标
        cookie = h["Set-Cookie"].split(";")[0]
        self.assertEqual(self.call("GET", "/api/state", {"Cookie": cookie})[0], 200)
        # 页面里存着的旧令牌不对，也不挡住 cookie
        self.assertEqual(self.call("GET", "/api/state", {"Cookie": cookie, "Authorization": "Bearer bad"})[0], 200)
        self.assertEqual(self.call("GET", "/d/1.x.y")[0], 401)
        self.app.wb.settings.set("workbench_device_ver", 2)  # 撤销
        self.assertEqual(self.call("GET", f"/d/{token}")[0], 401)
        self.assertIsNotNone(owner)
        self.assertEqual(self.call("GET", "/static/icon.png")[0], 200)

    def test_login_link_once(self):
        t = auth.make_login_token(self.app.key)
        status, h, _ = self.call("GET", f"/login?t={t}")
        self.assertEqual(status, 302)
        cookie = h["Set-Cookie"].split(";")[0]
        self.assertEqual(self.call("GET", "/api/quests", {"Cookie": cookie})[0], 200)
        self.assertEqual(self.call("GET", f"/login?t={t}")[0], 401)


class CardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.plan = self.store.db.latest_plan(MONDAY.isoformat())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def fetch(self, code=61):
        payload = json.dumps({"daily": {"weathercode": [code], "temperature_2m_max": [12.4], "temperature_2m_min": [7.6],
                                        "windspeed_10m_max": [20]}}).encode()

        class Resp:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return payload
        self.fetched = getattr(self, "fetched", 0) + 1
        return Resp()

    def test_training_day_card_generated_with_references(self):
        maker, ledger = FakeMaker(), FakeLedger()
        card = cards.make_card(self.root, self.store.db, MONDAY, self.plan, maker=maker, ledger=ledger,
                               fetch=lambda url, timeout: self.fetch())
        self.assertEqual((card["code"], card["rarity"], card["generated"]), ("S1", "普通", True))
        self.assertIn("训练日", card["reason"])
        train = next(i["title"] for i in self.plan["items"] if i["kind"] == "event" and i["title"].startswith("训练"))
        self.assertEqual(card["line"], cards.line_for("S1", train))  # 按今天实际练的写，不写死“练腿”
        self.assertNotIn("练腿", cards.line_for("S1", "训练：体态 + 核心 + 有氧"))
        self.assertEqual(cards.line_for("S1", "训练：体态 + 核心 + 有氧"), "今天练体态 + 核心 + 有氧，练完奖励自己，也奖励我一张照片。")
        self.assertEqual(card["weather"], "雨 8–12°")
        self.assertEqual(maker.calls[0][1][:2], ["avatar-day.jpg", "avatar-night.jpg"])
        self.assertNotIn("S1.png", maker.calls[0][1])  # 图库场景图不再当参考（免得天天同一个构图）
        self.assertIn("Setting:", maker.calls[0][0])
        self.assertEqual(ledger.rows, [("openrouter", 0.09, "daily-card")])
        self.assertTrue((self.root / "cards" / "2026-11-02.jpg").is_file())
        again = cards.make_card(self.root, self.store.db, MONDAY, self.plan, maker=maker)
        self.assertEqual(len(maker.calls), 1)  # 一天只画一张
        self.assertEqual(again["title"], card["title"])
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        self.assertEqual(wb.state(NOON)["card"]["image"], "/img/card/2026-11-02")

    def test_director_uses_her_day_and_wish(self):
        """2026-10-30 她说卡册的动作和视角太固定：按当天的事编镜头；她能点单（含 cosplay），一天用一条。"""
        import json as _json
        from types import SimpleNamespace

        class Director:
            def __init__(self):
                self.briefs = []

            def complete(self, system, messages, purpose="chat"):
                self.briefs.append(messages[0]["content"])
                return SimpleNamespace(text=_json.dumps({"title": "大本钟战士", "line": "今天你是主角", "scene": "on top of Big Ben",
                    "action": "leaping with a wand", "outfit": "sailor senshi costume", "camera": "fisheye lens close-up",
                    "light": "golden hour", "twist": "anime poster"}))
        cards.add_wish(self.store.db, "cos 水手服战士在大本钟顶上")
        maker, d = FakeMaker(), Director()
        card = cards.make_card(self.root, self.store.db, MONDAY + timedelta(days=2), self.plan, maker=maker,
                               fetch=lambda url, timeout: self.fetch(), router=d)
        self.assertEqual(card["title"], "大本钟战士")
        self.assertIn("水手服", d.briefs[0])
        self.assertIn("训练", d.briefs[0])  # 她今天的安排交给导演
        self.assertIn("fisheye", maker.calls[0][0])
        self.assertIn("Her face stays her own", maker.calls[0][0])
        self.assertEqual(cards.wishes(self.store.db), [])  # 用掉了
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        self.assertTrue(wb.act("card.wish", {"text": "背影，雨夜伦敦眼"})["ok"])
        self.assertEqual(len(wb.cards_page()["wishes"]), 1)
        self.assertFalse(wb.act("card.wish", {"text": "x", "now": 1})["ok"])  # 测试里没有重画的 runner
        self.assertTrue(any(c["kind"] == "点单" for c in wb.cards_page()["cards"]))

    def test_redraw_now_merges_wishes_even_on_node_cards_and_restarts(self):
        """2026-11-01：她提了两条“现在重画今天”一直显示排队中——节点卡不用点单。现在“现在重画”的几条合在一起画，节点卡也照画；
        上一张还在画时来的新要求，画完会接着再画。"""
        import json as _json
        from types import SimpleNamespace
        seen = []

        class Director:
            def complete(self, system, messages, purpose="chat"):
                seen.append(messages[0]["content"])
                return SimpleNamespace(text=_json.dumps({"title": "雨夜", "line": "给你", "scene": "s", "action": "a", "outfit": "o",
                                                         "camera": "c", "light": "l", "twist": ""}))
        db = self.store.db
        cards.add_wish(db, "背影，雨夜伦敦眼", now=True)
        cards.add_wish(db, "穿红色风衣", now=True)
        cards.add_wish(db, "明天要海边", now=False)
        old = cards.choose
        cards.choose = lambda *a, **k: {**old(*a, **k), "rarity": "超稀有", "reason": "升级"}
        try:
            card = cards.make_card(self.root, db, MONDAY + timedelta(days=3), self.plan, maker=FakeMaker(),
                                   fetch=lambda url, timeout: self.fetch(), router=Director())
        finally:
            cards.choose = old
        self.assertIn("背影，雨夜伦敦眼；穿红色风衣", seen[0])
        self.assertEqual(card["wish"], "背影，雨夜伦敦眼；穿红色风衣")
        self.assertEqual([w["text"] for w in cards.wishes(db)], ["明天要海边"])
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        runs = []

        class Runner:
            def busy(self):
                return False

            def __call__(self):
                runs.append(1)
                return True
        wb.redo_runner = Runner()
        cards.add_wish(db, "再加一条", now=True)
        wb.cards_page()
        self.assertEqual(len(runs), 1)  # 有没用上的“现在重画”，就接着画

    def test_card_before_start_migration(self):
        import importlib.util, json as _json
        folder = self.root / "cards"
        folder.mkdir(exist_ok=True)
        for d in ("2026-10-27", "2026-10-28"):
            (folder / f"{d}.json").write_text(_json.dumps({"day": d}))
            (folder / f"{d}.jpg").write_bytes(b"x")
        Settings(self.root / "settings.json").set("cold_start_from", "2026-10-28")
        spec = importlib.util.spec_from_file_location("m", Path(__file__).resolve().parent.parent / "migrations" / "20261101_card_before_start.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertIn("2026-10-27", m.run(self.store))
        self.assertFalse((folder / "2026-10-27.json").exists())
        self.assertTrue((folder / "old" / "2026-10-27-before-start.jpg").exists())
        self.assertTrue((folder / "2026-10-28.json").exists())
        # 她 11-01 又说先别删：放回来
        spec = importlib.util.spec_from_file_location("r", Path(__file__).resolve().parent.parent / "migrations" / "20261101_card_restore_before_start.py")
        r = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(r)
        self.assertIn("2026-10-27", r.run(self.store))
        self.assertTrue((folder / "2026-10-27.json").exists() and (folder / "2026-10-27.jpg").exists())
        self.assertFalse((folder / "old" / "2026-10-27-before-start.jpg").exists())
        self.assertEqual(r.run(self.store), "没有要放回的卡")

    def test_wishes_redraw_today_migration(self):
        import importlib.util
        from ethan import cards as _c
        _c.add_wish(self.store.db, "不要总是一个角度")
        self.store.db.conn.execute("UPDATE card_wishes SET created_at = '2026-11-01T09:17:58'")
        spec = importlib.util.spec_from_file_location("w", Path(__file__).resolve().parent.parent / "migrations" / "20261101_wishes_redraw_today.py")
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        self.assertEqual(m.run(self.store), "改成现在重画：1 条")
        self.assertTrue(_c.redo_waiting(self.store.db))

    def test_past_card_shows_her_own_diary_not_ethans_memory(self):
        """2026-11-01 她说日记要用她的口吻写她的一天，不是 小伴 的聊天记忆；能自己改，改过的不再被自动覆盖。"""
        from ethan import diary
        # 卡是早上照前一天画的：周二画的那张，日记本里算周一的（她 2026-11-01）
        cards.make_card(self.root, self.store.db, MONDAY + timedelta(days=1), self.plan, maker=FakeMaker(), fetch=lambda url, timeout: self.fetch())
        self.store.wiki.write(f"journal/{MONDAY}", "记忆", f"# {MONDAY} 的记忆\n\n她去攀岩了，很开心。")
        wb = Workbench(self.store, Settings(self.root / "settings.json"))
        kicked = []
        wb.diary_kick = lambda: kicked.append(1)
        wb.now = lambda: NOON + timedelta(days=2)
        c = next(x for x in wb.cards_page()["cards"] if x["day"] == MONDAY.isoformat())
        self.assertEqual(c["made"], (MONDAY + timedelta(days=1)).isoformat())
        self.assertEqual(c["diary"], "")  # 小伴 的记忆不进日记本
        self.assertTrue(kicked)  # 没写的后台补
        self.assertTrue(wb.act("diary.save", {"day": MONDAY.isoformat(), "text": "今天我去攀岩了。"})["ok"])
        c = next(x for x in wb.cards_page()["cards"] if x["day"] == MONDAY.isoformat())
        self.assertEqual((c["diary"], c["diary_mine"]), ("今天我去攀岩了。", True))
        self.assertEqual(diary.write(self.store, None, MONDAY, force=True), "她自己改过，不动")
        # 存空的 = 不要这篇，删掉让系统重写（她 2026-11-02：11-01 误存了一条空的，那天就一直空着）
        self.assertTrue(wb.act("diary.save", {"day": MONDAY.isoformat(), "text": "  "})["ok"])
        self.assertIsNone(diary.get(self.store.db, MONDAY.isoformat()))
        self.assertIn(MONDAY, diary.missing(self.store, MONDAY + timedelta(days=3)))


    def test_voice_upload_transcribes_in_background(self):
        """2026-11-01 浏览器自带的识别只认出一小截：整段录音传到服务器转。"""
        from ethan import voice
        spawned = []
        job = voice.start(self.root, b"fake-audio", "audio/mp4", spawn=lambda *a, **k: spawned.append(a))
        self.assertTrue(spawned)
        self.assertEqual(voice.result(self.root, job), {"done": False})
        voice.run(self.root, job, transcriber=lambda p: "今天下午去了 Tate")
        self.assertEqual(voice.result(self.root, job), {"done": True, "text": "今天下午去了 Tate"})
        self.assertFalse(list((self.root / "voice").glob(job + ".m4a")))  # 音频转完就删
        with self.assertRaises(ValueError):
            voice.start(self.root, b"", "audio/mp4", spawn=lambda *a, **k: None)
        with self.assertRaises(ValueError):
            voice.result(self.root, "../x")

    def test_diary_written_in_her_voice_from_her_own_words(self):
        from ethan import diary
        self.store.raw.append("telegram", "mia", "今天去 Tate 看了展，腿走断了")
        self.store.raw.append("telegram", "ethan", "辛苦啦，我给你揉揉")
        day = date.fromisoformat(self.store.raw.recent(1)[-1]["at"][:10])
        m = diary.materials(self.store, day)
        self.assertIn("Tate", m)
        self.assertNotIn("揉揉", m)  # 小伴 说的不当素材

        class R:
            def __init__(self): self.seen = []
            def complete(self, system, msgs, purpose=""):
                self.seen.append((system, msgs))
                return type("X", (), {"text": "今天我去了 Tate。"})()
        r = R()
        self.assertTrue(diary.write(self.store, r, day).startswith("写好了"))
        self.assertIn("第一人称", r.seen[0][0])
        self.assertEqual(diary.get(self.store.db, day.isoformat())["source"], "auto")
        self.assertEqual(diary.write(self.store, r, day), "已经有了")
        got = diary.weave(self.store, r, day.isoformat(), "下午还喝了杯咖啡")
        self.assertEqual(got["source"], "her")

    def test_diary_materials_include_itinerary_people_and_lesson(self):
        """她 2026-11-02：日记像流水账，缺行程、思考、学到的、感受。素材补上日程、见了谁、小伴 教的知识点；提示词要求拆出内容、不写对话过程。"""
        from ethan import diary, events, lessons, lifelog
        day = MONDAY
        events.add(self.store.db, day.isoformat(), "15:00", "17:00", "Sunday Roast", "Soho")
        lifelog.ensure(self.store.db)
        lifelog.add(self.store.db, "meet", day, "Lena", "她在工作台记的")
        lessons.ensure(self.store.db)
        self.store.db.conn.execute("INSERT INTO lessons (day, topic, title, gist, taught_at) VALUES (?, '金融历史', '郁金香泡沫', '1637 年荷兰', 'x')",
                                   (day.isoformat(),))
        m = diary.materials(self.store, day)
        for word in ("Sunday Roast", "Lena", "郁金香泡沫"):
            self.assertIn(word, m)
        self.assertIn("不是聊天记录", diary.PROMPT)
        self.assertIn("思考", diary.PROMPT)

    def test_rest_day_rain_and_failure_falls_back_to_library(self):
        tue = MONDAY + timedelta(days=1)
        card = cards.make_card(self.root, self.store.db, tue, None, maker=FakeMaker(fail=True),
                               fetch=lambda url, timeout: self.fetch(61))
        self.assertEqual((card["code"], card["generated"]), ("W6", False))
        self.assertIn("boom", card["error"])
        self.assertTrue((self.root / "cards" / f"{tue}.jpg").is_file())  # 图库那张顶上
        self.assertEqual(card["error"].count("boom"), 3)  # 少带参考图又试了两次（2026-10-31）
        card2 = cards.make_card(self.root, self.store.db, tue + timedelta(days=1), None, maker=FakeMaker(fail=True),
                                fetch=lambda url, timeout: self.fetch(61))
        self.assertNotEqual(card2["fallback"], card["fallback"])  # 最近用过的备用图不再用，免得两天一模一样

    def test_retry_with_fewer_refs_succeeds(self):
        class Picky(FakeMaker):
            def make(self, prompt, refs=()):
                self.calls.append(len(refs))
                if len(refs) > 2:
                    raise RuntimeError("HTTP 400 too many images")
                return b"\xff\xd8card"
        m = Picky()
        card = cards.make_card(self.root, self.store.db, MONDAY + timedelta(days=3), None, maker=m, fetch=lambda url, timeout: self.fetch(61))
        self.assertTrue(card["generated"])
        self.assertEqual(m.calls[-1], 2)

    def test_rarity_rules(self):  # 2026-10-31 她认可：升级是稀有，每满 5 级才超稀有，超稀有 7 天内最多一次
        quests.award(self.store.db, 500, "测试", MONDAY - timedelta(days=1))
        card = cards.choose(self.store.db, MONDAY, None, {})
        self.assertEqual((card["code"], card["rarity"]), ("E1", "稀有"))
        quests.award(self.store.db, 9500, "测试", MONDAY)  # 升到 Lv.5（累计 10000）
        tue = MONDAY + timedelta(days=1)
        self.assertEqual(cards.choose(self.store.db, tue, None, {})["rarity"], "超稀有")
        self.assertEqual(cards.choose(self.store.db, tue, None, {}, recent=[{"rarity": "超稀有"}])["rarity"], "稀有")
        self.assertEqual(cards.choose(self.store.db, tue + timedelta(days=1), None, {})["rarity"], "普通")

    def test_weather_cached_per_day(self):
        cards.weather(self.root, MONDAY, fetch=lambda url, timeout: self.fetch())
        cards.weather(self.root, MONDAY, fetch=lambda url, timeout: self.fetch())
        self.assertEqual(self.fetched, 1)
        self.assertEqual(cards.weather(self.root, MONDAY + timedelta(days=1), fetch=lambda u, timeout: 1 / 0), {})


if __name__ == "__main__":
    unittest.main()


class TaskProgressTest(unittest.TestCase):
    def test_streak_task_shows_count(self):
        """“连续一周训练前称体重”后面显示几分之几，打一次加一次（2026-10-29）。"""
        from ethan import quests
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            db = store.db
            quests.seed(db)
            h = quests.habit_by_title(db, "训练前称体重")
            self.assertEqual(quests.task_progress(db, {"title": "连续一周训练前称体重"}, MONDAY), (0, 5))
            quests.check_habit(db, h["id"], MONDAY)
            self.assertEqual(quests.task_progress(db, {"title": "连续一周训练前称体重"}, MONDAY), (1, 5))
            quests.check_habit(db, h["id"], MONDAY + timedelta(days=1))
            self.assertEqual(quests.task_progress(db, {"title": "连续一周训练前称体重"}, MONDAY + timedelta(days=2)), (2, 5))  # 今天还没打不算断
            self.assertEqual(quests.task_progress(db, {"title": "连续 4 周每周练满 5 次"}, MONDAY), (0, 4))
            self.assertIsNone(quests.task_progress(db, {"title": "测体脂基线"}, MONDAY))
            store.close()


class DiaryMarkTest(unittest.TestCase):
    """她 2026-11-02：日记里写得好的句子想用记号笔标出来。"""

    def test_mark_and_unmark(self):
        from ethan import diary
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            wb = Workbench(store, Settings(Path(d) / "settings.json"))
            day = MONDAY.isoformat()
            diary.save(store.db, day, "今天下雨。\n\n我在图书馆写完了报告，觉得自己比想象中厉害。", source="auto")
            got = wb.act("diary.mark", {"day": day, "text": "觉得自己比想象中厉害"})
            self.assertEqual(got["marks"], ["觉得自己比想象中厉害"])
            self.assertFalse(wb.act("diary.mark", {"day": day, "text": "不在日记里的话"})["ok"])
            self.assertEqual(wb._diary(day)["marks"], ["觉得自己比想象中厉害"])
            self.assertEqual(wb.act("diary.mark", {"day": day, "text": "觉得自己比想象中厉害", "off": 1})["marks"], [])
            store.close()


class DiaryNoteTest(unittest.TestCase):
    """她 2026-11-02：10-26 日记里“中间还跟人聊起店员为什么要问名字，被说观察得挺细”其实是 小伴 在跟别人聊。
    划出这句写批注，小伴 只重写这一句，同时进纠错本。"""

    def test_note_rewrites_only_that_sentence(self):
        from ethan import diary

        class R:
            def complete(self, system, messages, purpose=""):
                assert "批注" in system and "店员" in messages[0]["content"]
                if "删掉吧" in messages[0]["content"]:  # 只是写得不好：删掉，不算记错
                    return type("T", (), {"text": '{"text": "", "memory_error": false, "right": ""}'})()
                return type("T", (), {"text": '{"text": "回家路上我跟 小伴 说起店员为什么要问名字，他说是因为咖啡杯上要写。", '
                                              '"memory_error": true, "right": "聊店员问名字的是 小伴，不是她"}'})()
        with tempfile.TemporaryDirectory() as d:
            store = make_demo(Path(d))
            wb = Workbench(store, Settings(Path(d) / "settings.json"))
            wb.router = R()
            day = MONDAY.isoformat()
            diary.save(store.db, day, "下午去买咖啡。中间还跟人聊起店员为什么要问名字，\n被说观察得挺细。晚上早早睡了。", source="auto")
            got = wb.act("diary.note", {"day": day, "text": "中间还跟人聊起店员为什么要问名字， 被说观察得挺细。",
                                        "note": "这是 小伴 在跟别人聊名字，我说他观察得挺细"})
            self.assertTrue(got["ok"])
            body = diary.get(store.db, day)["body"]
            self.assertIn("回家路上我跟 小伴 说起", body)
            self.assertIn("下午去买咖啡。", body)
            self.assertIn("晚上早早睡了。", body)
            self.assertNotIn("被说观察得挺细", body)
            n = diary.notes(store.db, day)[0]
            self.assertEqual(n["status"], "done")
            self.assertEqual(store.db.conn.execute("SELECT COUNT(*) FROM corrections WHERE entry = 'diary'").fetchone()[0], 1)
            # 她只是觉得写得不好要删：删掉这句，不开纠错单（她 2026-11-02）
            self.assertTrue(wb.act("diary.note", {"day": day, "text": "晚上早早睡了。", "note": "这句没意思，删掉吧"})["ok"])
            self.assertNotIn("早早睡了", diary.get(store.db, day)["body"])
            self.assertEqual(store.db.conn.execute("SELECT COUNT(*) FROM corrections WHERE entry = 'diary'").fetchone()[0], 1)
            store.close()


class SlowModelTest(unittest.TestCase):
    """Codex 评审 11-05：工作台整体一把锁，锁里还在等模型回答，一个慢请求会堵住所有页面。
    等模型时要把锁放开：慢的“问一问”还没回来，别的页面照常打开；回来后照常写结果。"""

    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from ethan.workbench.server import make_handler
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = make_demo(self.root)
        self.app = App(Workbench(self.store, Settings(self.root / "settings.json")), TOKEN, self.root)
        self.entered, self.go, self.calls, self.threads = threading.Semaphore(0), threading.Event(), [], []

        test = self

        class SlowRouter:  # 假模型：进来就等，测试放行才回答
            def complete(self, system, messages, purpose=""):
                test.calls.append(purpose)
                test.entered.release()
                test.go.wait(10)
                return type("T", (), {"text": '{"conflict": false, "why": "不矛盾"}' if purpose == "me-compare" else "慢慢答"})()
        self.app.wb.router = SlowRouter()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))  # 不设 daemon：关的时候等请求线程都走完再关数据库
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.auth = {"Authorization": "Bearer " + auth.make_session(self.app.key, self.app.owner)}

    def tearDown(self):
        self.go.set()
        self.server.shutdown()
        self.server.server_close()
        for t in self.threads:
            t.join(10)
        self.store.close()
        self.tmp.cleanup()

    def req(self, path, body=None, timeout=10):
        from urllib.request import Request, urlopen
        r = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                    headers={**self.auth, "Content-Type": "application/json"})
        with urlopen(r, timeout=timeout) as resp:
            return json.loads(resp.read())

    def slow(self, body, n):
        import threading
        out = {}
        t = threading.Thread(target=lambda: out.setdefault("r", self.req("/api/act", body)), daemon=True)
        t.start()
        self.threads.append(t)
        for _ in range(n):
            self.assertTrue(self.entered.acquire(timeout=5), "假模型没被调到")
        return t, out

    def test_other_pages_not_blocked_while_waiting_for_model(self):
        t, out = self.slow({"action": "me.ask", "question": "我每周练几次"}, 2)  # 问一问：两个一起问
        started = time.monotonic()
        self.assertTrue(self.req("/health", timeout=3)["ok"])
        self.assertIn("profile", self.req("/api/state", timeout=5))
        self.assertTrue(self.req("/api/act", {"action": "rest.take"}, timeout=5)["ok"])
        self.assertLess(time.monotonic() - started, 5)
        self.go.set()
        t.join(10)
        self.assertTrue(out["r"]["ok"])
        self.assertEqual([a["answer"] for a in out["r"]["answers"]], ["慢慢答", "慢慢答"])
        self.assertEqual(self.store.db.conn.execute("SELECT COUNT(*) FROM ask_log").fetchone()[0], 1)

    def test_compare_releases_lock_too(self):
        t, out = self.slow({"action": "me.compare", "a": "一周 5 次", "b": "一周 4 次"}, 1)
        self.assertTrue(self.req("/health", timeout=3)["ok"])
        self.go.set()
        t.join(10)
        self.assertFalse(out["r"]["conflict"])

    def test_weave_keeps_her_edit_made_while_model_was_writing(self):
        from ethan import diary
        day = MONDAY.isoformat()
        diary.save(self.store.db, day, "下午去买咖啡。", source="auto")
        t, out = self.slow({"action": "diary.weave", "day": day, "note": "晚上看了电影"}, 1)
        self.assertTrue(self.req("/api/act", {"action": "diary.save", "day": day, "text": "她自己改的。"}, timeout=3)["ok"])
        self.go.set()
        t.join(10)
        self.assertFalse(out["r"]["ok"])  # 模型是照旧的那篇写的，不能盖掉她刚改的
        self.assertEqual(diary.get(self.store.db, day)["body"], "她自己改的。")



def load_tests(loader, tests, pattern):  # 公开版（scripts/export_public.py 加的）：不带作者的数据改动和调研文档，相关测试跳过
    import inspect
    import unittest as _u
    keep = _u.TestSuite()

    def walk(suite):
        for t in suite:
            if isinstance(t, _u.TestSuite):
                walk(t)
                continue
            try:
                src = inspect.getsource(getattr(t, t._testMethodName))
            except (OSError, TypeError, AttributeError):
                src = ""
            try:
                setup = inspect.getsource(type(t).setUp) if "setUp" in vars(type(t)) else ""
            except (OSError, TypeError):
                setup = ""
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
