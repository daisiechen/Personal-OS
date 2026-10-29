import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ethan import domains, goals2026
from ethan.core.backends import Reply
from ethan.icloud import ICloudCalendar, event_ics, parse_events
from ethan.memory import MemoryStore
from ethan.planner import gather_facts, nutrition, plan_day, validate
from tests.test_progress import seed_parents

MONDAY = date(2026, 11, 2)
SUNDAY = date(2026, 11, 1)


def good_plan(db, ethan=()):
    """给所有还没拆的今年目标各拆 6 步（第 3 步是阶段节点），外加 小伴 要准备的事。"""
    need = [r["id"] for r in db.conn.execute(
        "SELECT g.id FROM goals g WHERE g.level = 'year' AND g.status = 'active' "
        "AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.goal_id = g.id AND t.quest = 'main')")]
    deal = db.conn.execute("SELECT id FROM goals WHERE title = '创业拿到第一个订单'").fetchone()[0]
    breakdowns = []
    for gid in need:
        steps = [{"title": f"目标{gid}第{n}步", "size": "SML"[n % 3], "milestone": n == 3} for n in range(1, 7)]
        if gid == deal:
            steps[0]["title"] = "列出 30 个潜在客户"
        breakdowns.append({"goal_id": gid, "steps": steps})
    return json.dumps({"breakdowns": breakdowns, "ethan": [{"title": e} for e in ethan], "events": []},
                      ensure_ascii=False)


class Router:
    """按顺序吐出预设回复；记下每次收到的消息。回复可以是函数，调用时才生成（拿到最新的库）。"""

    def __init__(self, *texts):
        self.texts, self.calls = list(texts), []

    def complete(self, system, messages, purpose="chat", images=()):
        self.calls.append(messages)
        text = self.texts.pop(0)
        if isinstance(text, Exception):
            raise text
        return Reply(text() if callable(text) else text, "subscription", "m", 0.0)


class FakeCalendar:
    def __init__(self):
        self.synced = []

    def sync_day(self, day, items, stale_uids=(), places=None, remind_before=60):
        self.synced.append((day, [(i["kind"], i["title"]) for i in items], list(stale_uids)))
        return {i["id"]: f"uid-{i['id']}" for i in items}


class FakeTravel:
    def minutes(self, origin, dest, day, arrive):
        return {"minutes": 23, "route": "139 路公交", "source": "tfl"}


class PlannerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name))
        self.db = self.store.db
        seed_parents(self.db)
        goals2026.apply(self.store)
        self.deal = self.db.conn.execute("SELECT id FROM goals WHERE title = '创业拿到第一个订单'").fetchone()[0]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def plan(self, *texts, day=MONDAY, **kw):
        texts = texts or (lambda: good_plan(self.db),)
        return plan_day(self.db, Router(*texts), day, travel=FakeTravel(), cold_start_from=MONDAY, **kw)

    def test_nutrition_is_computed_by_code(self):
        n = nutrition(60.0, MONDAY)
        self.assertEqual((n["kcal"], n["protein_g"]), (1650, 96))

    def test_facts_include_training_cold_start_domains_and_quests(self):
        facts = gather_facts(self.db, MONDAY, cold_start_from=MONDAY)
        self.assertEqual(facts["training"]["place"], "健身房")
        self.assertIsNone(gather_facts(self.db, SUNDAY)["training"])
        self.assertEqual(facts["cold_start"]["module"], "财务")
        self.assertEqual(len(facts["domains"]), len(domains.DOMAINS))
        need = {g["title"] for g in facts["quests"]["needs_breakdown"]}
        self.assertIn("创业拿到第一个订单", need)
        self.assertNotIn("年底前去 3 个新地方", need)  # 暂停的不拆

    def test_validator_rules(self):
        facts = gather_facts(self.db, MONDAY, busy=[("09:30", "10:30", "课")])
        ids = {g["id"] for g in facts["goals"]}
        self.assertEqual(validate(json.loads(good_plan(self.db)), facts, ids), [])
        short = json.loads(good_plan(self.db))
        short["breakdowns"][0]["steps"] = short["breakdowns"][0]["steps"][:3]
        self.assertTrue(any("要 6–20 步" in e for e in validate(short, facts, ids)))
        missing = json.loads(good_plan(self.db))
        missing["breakdowns"] = missing["breakdowns"][1:]
        self.assertTrue(any("还没拆解" in e for e in validate(missing, facts, ids)))
        bad_size = json.loads(good_plan(self.db))
        bad_size["breakdowns"][0]["steps"][0]["size"] = "XL"
        self.assertTrue(any("size" in e for e in validate(bad_size, facts, ids)))
        clash = json.loads(good_plan(self.db))
        clash["events"] = [{"start": "15:30", "end": "16:30", "place": "某工作室", "title": "试课"},
                           {"start": "10:00", "end": "11:00", "place": "银行", "title": "办卡"}]
        errors = validate(clash, facts, ids)
        self.assertTrue(any("训练" in e for e in errors))
        self.assertTrue(any("课" in e for e in errors))
        pick = json.loads(good_plan(self.db))
        pick["breakdowns"][0]["steps"][0]["title"] = "从 小伴 给的候选里选定本周的新体验"
        pick["ethan"] = [{"title": "查伦敦体验课，列出时间、价格，供她挑选", "goal_id": None}]
        errors = validate(pick, facts, ids)
        self.assertTrue(any("第 1 步让她挑选" in e for e in errors))
        self.assertTrue(any("小伴 准备的事让她挑选" in e for e in errors))
        ok = json.loads(good_plan(self.db))
        ok["ethan"] = [{"title": "直接替她选好本周的新体验，告诉她时间地点", "goal_id": None}]
        self.assertEqual(validate(ok, facts, ids), [])

    def test_breakdowns_become_draft_main_quests_and_everything_is_listed(self):
        cal = FakeCalendar()
        self.db.add_task("整理照片")
        self.db.add_task("帮她查伦敦 physio", promised_by_ethan=True)
        result = self.plan(lambda: good_plan(self.db, ethan=["想 3 个个人管理系统指标给她审批"]), calendar=cal)
        self.assertEqual(result["origin"], "model")
        event = result["items"][0]
        self.assertEqual((event["kind"], event["start"], event["travel_min"]), ("event", "16:00", 45))
        self.assertEqual([k for k, _ in cal.synced[0][1]], ["event"])
        mains = [i for i in result["items"] if i.get("priority") == "main"]
        self.assertEqual(len(mains), 5 * 6)  # 5 个进行中的今年目标，各 6 步，全部列出
        first = [i for i in mains if i["goal_id"] == self.deal][0]
        self.assertEqual((first["title"], first["how"]), ("列出 30 个潜在客户", "下一步"))
        self.assertTrue(json.loads(first["detail"])["draft"])
        brief = result["brief"]
        deal_line = [m for m in brief["main"] if m["goal"] == "创业拿到第一个订单"][0]
        self.assertEqual((deal_line["done"], deal_line["total"], deal_line["next"], deal_line["draft"]),
                         (0, 6, "列出 30 个潜在客户", True))
        self.assertIn("冷启动：财务（小伴 带你做，10–15 分钟）", brief["side"])
        self.assertIn("整理照片", brief["side"])
        self.assertEqual(sorted(brief["ethan"]), ["帮她查伦敦 physio", "想 3 个个人管理系统指标给她审批"])
        self.assertEqual(brief["game"]["balance"], 0)
        self.assertEqual(len(brief["game"]["daily"]), 5)  # 周一：7 项坚持里 5 项是日常（英语改成周常）

    def test_second_day_does_not_rebreak_and_completed_steps_advance(self):
        self.plan()
        first = self.db.conn.execute("SELECT id FROM tasks WHERE title = '列出 30 个潜在客户'").fetchone()[0]
        from ethan import quests
        self.assertEqual(quests.complete_task(self.db, first, MONDAY), 100)  # good_plan 里第 1 步是中（M）
        tue = self.plan('{"breakdowns": [], "ethan": [], "events": []}', day=date(2026, 11, 3))
        self.assertEqual(tue["origin"], "model")
        deal_line = [m for m in tue["brief"]["main"] if m["goal"] == "创业拿到第一个订单"][0]
        self.assertEqual((deal_line["done"], deal_line["next"]), (1, f"目标{self.deal}第2步"))

    def test_retries_then_fallback_keeps_training_cold_start_and_existing_quests(self):
        self.db.add_task("交论文提纲", due="2026-11-03")
        result = self.plan('{"breakdowns": []}', "不是 JSON", RuntimeError("限流"))
        self.assertEqual(result["origin"], "fallback")
        titles = [i["title"] for i in result["items"]]
        self.assertIn("交论文提纲", titles)
        self.assertTrue(any(t.startswith("训练") for t in titles))
        self.assertTrue(any(t.startswith("冷启动") for t in titles))
        self.assertTrue(result["brief"]["fallback"])

    def test_replan_deletes_old_calendar_events(self):
        cal = FakeCalendar()
        self.plan(calendar=cal)
        self.plan('{"breakdowns": [], "ethan": [], "events": []}', calendar=cal)
        self.assertEqual(len(cal.synced[1][2]), 1)
        self.assertEqual(self.db.latest_plan(MONDAY.isoformat())["version"], 2)

    def test_replan_keeps_ethan_items_but_drops_her_old_auto_todos(self):
        self.plan(lambda: good_plan(self.db, ethan=["只在第一版里的事"]))
        cold = "冷启动：财务（小伴 带你做，10–15 分钟）"
        self.assertTrue({"只在第一版里的事", cold} <= {t["title"] for t in self.db.open_tasks()})
        plan_day(self.db, Router('{"breakdowns": [], "ethan": [], "events": []}'), MONDAY, travel=FakeTravel())
        titles = {t["title"] for t in self.db.open_tasks()}
        self.assertIn("只在第一版里的事", titles)  # 小伴 答应的事重排也留着
        self.assertNotIn(cold, titles)  # 上一版自动加给她、新版不要的待办照样删

    def test_existing_events_block_their_travel_time_too(self):
        class Trip:
            def minutes(self, origin, dest, day, arrive):
                self.dest = dest
                return {"minutes": 25, "route": "bus", "source": "tfl"}

        trip = Trip()
        busy = [("12:00", "13:00", "午饭", {"location": "AB2 3EF London", "geo": None, "travel_min": 37}),
                ("18:00", "19:00", "看展", {"location": "Tate Modern, SE1 9TG", "geo": None, "travel_min": None}),
                ("20:00", "21:00", "线上会", {"location": "", "geo": None, "travel_min": None})]
        facts = gather_facts(self.db, MONDAY, busy=busy, travel=trip)
        lunch, show, call = facts["busy"]
        self.assertEqual((lunch["leave"], lunch["back"]), ("11:00", "13:37"))
        self.assertEqual((show["leave"], show["back"]), ("17:15", "19:25"))
        self.assertEqual(trip.dest, {"postcode": "SE1 9TG"})
        self.assertEqual((call["leave"], call["back"]), ("20:00", "21:00"))
        ids = {g["id"] for g in facts["goals"]}
        clash = json.loads(good_plan(self.db))
        clash["events"] = [{"start": "14:00", "end": "14:30", "place": "银行", "title": "办卡"}]
        self.assertTrue(any("午饭（含来回路上）" in e for e in validate(clash, facts, ids)))
        result = plan_day(self.db, Router(lambda: good_plan(self.db)), MONDAY, busy=busy, travel=trip)
        self.assertIn("12:00–13:00 午饭（路上约 37 分钟：11:00 出门，约 13:37 回来）", result["brief"]["busy"])

    def test_weekend_has_no_training(self):
        result = plan_day(self.db, Router(lambda: good_plan(self.db)), SUNDAY, travel=FakeTravel())
        self.assertFalse([i for i in result["items"] if i["kind"] != "todo"])
        self.assertIsNone(result["brief"]["training"])


class Resp:
    def __init__(self, status, body):
        self.status, self.body = status, body.encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


MULTISTATUS = '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">{}</d:multistatus>'


class FakeICloud:
    def __init__(self, with_ethan=False):
        self.requests = []
        self.with_ethan = with_ethan

    def __call__(self, req, timeout):
        method, url = req.get_method(), req.full_url
        self.requests.append((method, url, req.data))
        if method == "PROPFIND" and url == "https://caldav.icloud.com/":
            body = '<d:response><d:propstat><d:prop><d:current-user-principal><d:href>/123/principal/</d:href>' \
                   '</d:current-user-principal></d:prop></d:propstat></d:response>'
        elif method == "PROPFIND" and url.endswith("/principal/"):
            body = '<d:response><d:propstat><d:prop><c:calendar-home-set>' \
                   '<d:href>https://p1-caldav.icloud.com/123/calendars/</d:href></c:calendar-home-set>' \
                   '</d:prop></d:propstat></d:response>'
        elif method == "PROPFIND":
            cals = [("home", "家庭"), ("work", "工作")] + ([("ethan-x", "小伴 计划")] if self.with_ethan else [])
            body = "".join(
                f'<d:response><d:href>/123/calendars/{h}/</d:href><d:propstat><d:prop><d:displayname>{n}'
                '</d:displayname><d:resourcetype><d:collection/><c:calendar/></d:resourcetype>'
                '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
                '</d:prop></d:propstat></d:response>' for h, n in cals)
            body += '<d:response><d:href>/123/calendars/</d:href><d:propstat><d:prop><d:resourcetype>' \
                    '<d:collection/></d:resourcetype></d:prop></d:propstat></d:response>'
        elif method == "REPORT" and "/work/" in url:
            ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20261102T100000Z\r\nDTEND:20261102T113000Z\r\n"
                   "SUMMARY:Marketing 课\r\nLOCATION:AB2 3EF\\nLondon\\, 英格兰\r\n"
                   "X-APPLE-STRUCTURED-LOCATION;VALUE=URI;X-TITLE=x:geo:51.52,-0.11\r\n"
                   "X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT37M\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nDTSTART;VALUE=DATE:20261102\r\n"
                   "DTEND;VALUE=DATE:20261103\r\nSUMMARY:某人生日\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
            body = f'<d:response><d:propstat><d:prop><c:calendar-data>{ics}</c:calendar-data></d:prop>' \
                   '</d:propstat></d:response>'
        else:
            return Resp(201, "")
        return Resp(207, MULTISTATUS.format(body))


class ICloudTest(unittest.TestCase):
    def test_busy_reads_other_calendars_and_skips_all_day(self):
        fake = FakeICloud()
        cal = ICloudCalendar("me@icloud.com", "abcd-efgh", opener=fake)
        busy = cal.busy(MONDAY)
        self.assertEqual(busy[0][:3], ("10:00", "11:30", "Marketing 课"))  # 11 月已是冬令时，UTC=伦敦
        self.assertEqual(busy[0][3], {"location": "AB2 3EF London, 英格兰", "geo": (51.52, -0.11), "travel_min": 37})
        self.assertEqual(len([r for r in fake.requests if r[0] == "REPORT"]), 2)

    def test_sync_creates_calendar_once_with_apple_travel_time_and_alarm(self):
        fake = FakeICloud()
        cal = ICloudCalendar("me@icloud.com", "pw", opener=fake)
        items = [{"id": 7, "kind": "event", "start": "16:00", "end": "17:15", "place": "健身房", "travel_min": 35,
                  "title": "训练：下肢", "detail": "1. 臀推机：3 组", "how": "", "smaller": ""},
                 {"id": 8, "kind": "event", "start": "19:00", "end": "20:00", "place": "某展",
                  "title": "看展", "detail": "", "how": "", "smaller": ""}]
        gym = {"name": "某健身房", "address": "1 Example Street, London AB1 2CD", "lat": 51.5, "lon": -0.16}
        written = cal.sync_day(MONDAY, items, stale_uids=["ethan-old"], places={"健身房": gym})
        methods = [r[0] for r in fake.requests]
        self.assertEqual(methods.count("MKCALENDAR"), 1)
        self.assertIn("DELETE", methods)
        puts = [r[2].decode() for r in fake.requests if r[0] == "PUT"]
        self.assertIn("X-APPLE-TRAVEL-DURATION;VALUE=DURATION:PT35M", puts[0])
        self.assertIn("X-APPLE-TRAVEL-ADVISORY-BEHAVIOR:AUTOMATIC", puts[0])
        self.assertIn("X-APPLE-STRUCTURED-LOCATION;VALUE=URI;", puts[0])
        self.assertIn(":geo:51.5,-0.16", puts[0])
        self.assertIn("TRIGGER:-PT95M", puts[0])  # 通行 35 分钟 + 出发前 1 小时
        self.assertIn("1. 臀推机", puts[0])
        self.assertIn("DTSTART:20261102T160000Z", puts[0])
        self.assertNotIn("BEGIN:VALARM", puts[1])  # 没算路程的不设提醒
        self.assertIn("LOCATION:某展", puts[1])
        self.assertEqual(written, {7: "ethan-2026-11-02-7", 8: "ethan-2026-11-02-8"})
        fake2 = FakeICloud(with_ethan=True)
        ICloudCalendar("me", "pw", opener=fake2).sync_day(MONDAY, items)
        self.assertNotIn("MKCALENDAR", [r[0] for r in fake2.requests])

    def test_ics_roundtrip_and_tzid(self):
        s = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
        ics = event_ics("u1", s, s.replace(hour=9), "a, b; c")
        self.assertEqual(parse_events(ics)[0][2], "a, b; c")
        summer = parse_events("BEGIN:VEVENT\nDTSTART;TZID=Europe/London:20260701T090000\n"
                              "DTEND;TZID=Europe/London:20260701T100000\nSUMMARY:x\nEND:VEVENT")
        self.assertEqual(summer[0][0].astimezone(timezone.utc).hour, 8)


if __name__ == "__main__":
    unittest.main()
