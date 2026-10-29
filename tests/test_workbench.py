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
    (av / "library").mkdir(parents=True)
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

    def test_today_tasks_follow_her_drag_order(self):
        ids = [t["id"] for t in self.wb.state(NOON)["today"] if not t["done"]]
        self.assertGreater(len(ids), 2)
        mine = [ids[-1], ids[0]]
        self.assertTrue(self.act("today.order", ids=mine)["ok"])
        self.assertEqual([t["id"] for t in self.wb.state(NOON)["today"]][:2], mine)  # 拖过的在前，没拖的跟在后面

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
        quests.award(self.store.db, 300 - t["points"], "测试", MONDAY)
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
        self.assertEqual([r["cost"] for r in q["rewards"]], [300, 800, 2000])

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
        self.assertEqual(cards.line_for("S1", "训练：体态 + 核心 + 有氧"), "今天练体态 + 核心 + 有氧，练完我请你吃好的。")
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

    def test_rest_day_rain_and_failure_falls_back_to_library(self):
        tue = MONDAY + timedelta(days=1)
        card = cards.make_card(self.root, self.store.db, tue, None, maker=FakeMaker(fail=True),
                               fetch=lambda url, timeout: self.fetch(61))
        self.assertEqual((card["code"], card["generated"]), ("W6", False))
        self.assertIn("boom", card["error"])
        self.assertTrue((self.root / "cards" / f"{tue}.jpg").is_file())  # 图库那张顶上

    def test_level_up_is_super_rare(self):
        quests.award(self.store.db, 150, "测试", MONDAY - timedelta(days=1))
        card = cards.choose(self.store.db, MONDAY, None, {})
        self.assertEqual((card["code"], card["rarity"]), ("E1", "超稀有"))

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
