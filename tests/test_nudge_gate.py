"""主动消息只有一个关口（全系统模块化第一批，docs/design/nudges.md）。

- 登记表 ethan/nudge_registry.py 的 RULES 是真正执行的规则表：免打扰、刚聊过、连着没回、只问一次、间隔、时段只在 proactive.gate 里判断。
- 所有主动消息（含 Grok 回报转告、DemoApp 举报提醒、手动总览）都先过 gate；守卫：没登记、绕过关口直接发的测试不过。
- 她的决定 1、2、3：Grok 回报夜里到的早上 8 点后再说；DemoApp 紧急照发、常规守免打扰；约撞时间 / 计划撞了 / 路线排不开只问一次，没回挂首页。
"""

import ast
import functools
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from ethan import events, nudge_registry, nudges, proactive
from ethan.memory import MemoryStore

ROOT = Path(__file__).resolve().parent.parent / "ethan"


def london(day, h, m=0):  # 2026-11 伦敦是冬令时，等于 UTC
    return datetime(2026, 11, day, h, m, tzinfo=timezone.utc)


@functools.lru_cache(maxsize=None)
def _funcs(path: Path) -> dict:
    """{函数名: 源码}（含方法；嵌套的算在最外层的函数里）。

    按行切，不用 ast.get_source_segment：它每次都把整个文件重新切行，几百个函数就是平方级（服务器上一分多钟）。"""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out = {}
    for node in ast.walk(ast.parse("".join(lines))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.setdefault(node.name, "".join(lines[node.lineno - 1:node.end_lineno]))
    return out


class RegistryIsTheRuleTableTest(unittest.TestCase):
    def test_every_message_kind_has_an_executable_rule(self):
        kinds = {r[0] for r in nudge_registry.MESSAGES if r[2].startswith("主动消息")}
        self.assertEqual(sorted(kinds - set(nudge_registry.RULES)), [])
        named = {r[0] for r in nudge_registry.MESSAGES}
        self.assertEqual(sorted(set(nudge_registry.RULES) - named), [], "规则表里的每一类都要在 MESSAGES 写名字、触发、去哪")

    def test_rule_fields_are_known_values(self):
        for p, r in nudge_registry.RULES.items():
            self.assertIn(r.recent, (None, *proactive.RECENT), p)
            self.assertIn(r.unanswered, (None, "today", "last"), p)
            self.assertIn(r.once, (None, "day", "ever"), p)
            if r.window:
                self.assertTrue(all(re.fullmatch(r"\d{2}:\d{2}", x) for x in r.window), p)
            different = (not r.quiet or r.recent != "her1h" or r.unanswered != "today" or not r.switch)
            self.assertTrue(r.why or not different, f"{p} 和默认规矩不一样，要写原因")

    def test_once_prefixes_come_from_the_table(self):
        self.assertEqual(set(proactive.ONCE_PREFIXES), {p for p, r in nudge_registry.RULES.items() if r.once == "ever"})
        for p in ("clash:", "plan-clash:", "route:", "promise:", "meet:"):
            self.assertIn(p, proactive.ONCE_PREFIXES)

    def test_keys_proactive_builds_are_registered(self):
        src = (ROOT / "proactive.py").read_text(encoding="utf-8") + (ROOT / "fancy_moderation.py").read_text(encoding="utf-8") \
            + (ROOT / "report.py").read_text(encoding="utf-8")
        built = set(re.findall(r'f"([a-z-]+:)\{', src)) | {s.name for s in proactive.SLOTS} | {"brief", "m-"}
        built -= {"evening:"}  # nudges 里的，test_nudge_registry 管
        self.assertEqual(sorted(k for k in built if not nudge_registry.registered(k + "x" if k.endswith(":") else k)), [])

    def test_windows_only_in_the_table(self):
        """时段的数字只写在登记表里：检查函数不再自己写“10 <= 几点 < 21”。"""
        for f in ("nudges.py", "prep.py", "weekend.py"):
            src = (ROOT / f).read_text(encoding="utf-8")
            self.assertIsNone(re.search(r"\(\d+ <= now_local\.hour < \d+\)", src), f)
            self.assertNotIn("PROMISE_HOURS", src)
        for name in ("CHAT_WINDOW", "CHAT_IDLE", "PERK_HOURS", "OUTING", "SIT_GAP", "PREP_GAP_HOURS", "DIGEST_AT"):
            self.assertNotIn(name + " =", (ROOT / "nudges.py").read_text(encoding="utf-8"))
        self.assertNotIn("WINDOW = (", (ROOT / "weekend.py").read_text(encoding="utf-8"))


class OneGateGuardTest(unittest.TestCase):
    """守卫：真正把消息发给她的地方只有这几处，主动的都过 gate。"""
    SENDS = re.compile(r"\breach_out\(|(?<!quests)\.deliver\(|\"sendMessage\"|send_brief_card\(|self\.notify\(")
    # (文件, 函数) → 为什么可以在这里发
    ALLOWED = {
        ("proactive.py", "_send"): "主动消息真正发出去的唯一一处（say / relay_grok 调它，调之前过了 gate）",
        ("proactive.py", "tick"): "早报后面紧跟的昨日卡图片：早报那条已经过了 gate",
        ("fancy_moderation.py", "handle"): "DemoApp 提醒：发之前 self._gate()",
        ("fancy_moderation.py", "flush_held"): "DemoApp 免打扰里存着的：补发之前 self._gate()",
        ("fancy_moderation.py", "send"): "telegram_notifier 里真正调 Bot API 的那一下（只被上面两处调用）",
        ("fancy_moderation.py", "telegram_notifier"): "同上（send 是它里面的函数）",
        ("report.py", "main"): "手动总览：发之前 gate(overview:…)",
        ("core/agent.py", "reach_out"): "定义本身",
    }
    GATED = {("fancy_moderation.py", "handle"): "self._gate(", ("fancy_moderation.py", "flush_held"): "self._gate(",
             ("report.py", "main"): "gate(", ("proactive.py", "tick"): "gate(\"brief\""}

    def test_proactive_sends_only_through_the_gate(self):
        found = {}
        for path in ROOT.rglob("*.py"):
            rel = str(path.relative_to(ROOT))
            if rel.startswith("channels/"):
                continue  # 聊天渠道：她发来消息后的回复、命令和按钮（她先开口的），以及【系统提醒】；主动的 Grok 转告见下一个测试
            for name, body in _funcs(path).items():
                if self.SENDS.search(body or ""):
                    found[(rel, name)] = body
        self.assertEqual(sorted(set(found) - set(self.ALLOWED)), [], "新的主动发送要走 proactive.say / gate，或写进白名单并说明原因")
        for (rel, name), mark in self.GATED.items():
            body = found[(rel, name)]
            self.assertIn(mark, body, f"{rel} {name} 发之前要过 gate")
            self.assertLess(body.index(mark), self.SENDS.search(body).start(), f"{rel} {name}：gate 要在发之前")

    def test_channels_do_not_start_conversations_on_their_own(self):
        for path in (ROOT / "channels").glob("*.py"):
            src = path.read_text(encoding="utf-8")
            self.assertNotIn("reach_out(", src, path.name)  # 主动开口（Grok 回报转告）走 proactive.relay_grok
        tg = _funcs(ROOT / "channels/telegram.py")["relay_grok"]
        self.assertIn("proactive import relay_grok", tg)

    def test_rules_judged_only_in_gate(self):
        src = (ROOT / "proactive.py").read_text(encoding="utf-8")
        funcs = _funcs(ROOT / "proactive.py")
        users = [n for n, b in funcs.items() if "in_quiet(" in (b or "") and n not in ("in_quiet", "gate")]
        del src
        self.assertEqual(users, [], "免打扰只在 gate 里判断")
        src = "\n".join(b for b in funcs.values() if b)  # 只看代码，不看注释和文档串里的旧写法
        self.assertNotIn('startswith("evening:") and why in', src)  # 以前收工提醒的硬写例外
        self.assertNotIn("CHAT_IDLE", src)
        for f in ("nudges.py", "prep.py", "weekend.py", "meetprep.py"):
            body = "\n".join(b for b in _funcs(ROOT / f).values() if b)
            body = re.sub(r'"""[\s\S]*?"""', "", body)
            self.assertIsNone(re.search(r"key in done or (nudges\.)?told\(|told\(db, key\) or key in done", body), f)
        for p in ("nudges.py", "prep.py"):
            self.assertNotIn("MAX(at) FROM nudge_once WHERE key LIKE", (ROOT / p).read_text(encoding="utf-8"), p)


class GateBehaviourTest(unittest.TestCase):
    def setUp(self):
        from ethan.core.router import Settings
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "data")
        self.db = self.store.db
        self.settings = Settings(Path(self.tmp.name) / "settings.json")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def gate(self, key, now, records=(), done=None):
        return proactive.gate(key, now, self.settings, records, self.db, done or {})

    def test_order_and_reasons(self):
        self.assertEqual(self.gate("新花样:1", london(6, 12)), "没登记")
        self.assertEqual(self.gate("follow:x", london(6, 3)), "免打扰")
        self.assertEqual(self.gate("perk:x", london(6, 9)), "不在时段")
        her = [{"role": "mia", "at": london(6, 11, 40).isoformat(), "text": "在吗"}]
        self.assertEqual(self.gate("follow:x", london(6, 12), her), "刚聊过")
        self.assertIsNone(self.gate("evening:2026-11-06:remind", london(6, 21, 5), her))  # 收工提醒刚聊过也说
        self.settings.set("proactive_enabled", False)
        self.assertEqual(self.gate("follow:x", london(6, 12)), "主动消息已暂停")
        self.assertIsNone(self.gate("fancy-urgent:x", london(6, 3)))  # 紧急举报不管开关、不管免打扰

    def test_once_and_gap(self):
        self.assertIsNone(self.gate("clash:1", london(6, 12)))
        nudges.mark_told(self.db, ["clash:1"])
        self.assertEqual(self.gate("clash:1", london(7, 12)), "说过了")  # 换一天也不再问
        self.assertEqual(self.gate("follow:a", london(6, 12), done={"follow:a": "已发送"}), "说过了")
        nudges.log_sent(self.db, "prep-ask:3", london(6, 12))
        self.assertEqual(self.gate("prep-ask:4", london(6, 13)), "隔得太近")
        self.assertIsNone(self.gate("prep-ask:4", london(6, 14, 1)))
        nudges.log_sent(self.db, "sit", london(6, 12))
        self.assertEqual(self.gate("sit", london(6, 13, 30)), "隔得太近")
        self.assertIsNone(self.gate("sit", london(6, 14, 0)))

    def test_unanswered_today_and_last(self):
        rec = [{"role": "ethan", "kind": "proactive", "at": london(6, h).isoformat(), "text": "嗨"} for h in (9, 10, 11)]
        self.assertEqual(self.gate("follow:x", london(6, 15), rec), "还没回")
        self.assertEqual(self.gate("afternoon", london(6, 15), rec[-1:]), "上一条还没回")
        self.assertIsNone(self.gate("brief", london(6, 8, 30), rec))  # 早报不看没回


class TickReplayTest(unittest.TestCase):
    """整天回放：久坐两次隔 2 小时（被挡时喝水照样轮得到）；约撞时间只问一次，没回就挂在首页。"""

    def setUp(self):
        from ethan.core.agent import Ethan
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router, Settings
        from tests.test_review_p2b import Backend, Channel
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = MemoryStore(root / "data")
        self.db = self.store.db
        self.ethan = Ethan(self.store, Router(Backend(), Ledger(root / "ledger"), Settings(root / "settings.json")), persona="人设")
        self.channel = Channel()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def only(self, *names):
        return mock.patch.multiple(nudges, **{c.__name__: (lambda *a: None) for c in nudges.checks() if c.__name__ not in names})

    def test_sitting_gap_lets_water_through(self):
        from ethan import worktime
        worktime.ensure(self.db)
        now = london(6, 14, 32)
        worktime.record(self.db, [worktime.slot_of(now + timedelta(minutes=40) - timedelta(minutes=5 * k)) for k in range(1, 30)], "mac")
        with self.only("_sitting", "_water"):
            first = proactive.tick(self.ethan, self.channel, now=now)
            self.assertIn("sit：已发送", first)
            later = proactive.tick(self.ethan, self.channel, now=london(6, 15, 5))
        self.assertIn("water-pm：已发送", later)  # 久坐还在 2 小时间隔里，被关口挡住，喝水照样发

    def test_clash_asked_once_then_hangs_on_home(self):
        from ethan.workbench.api import Workbench
        eid = events.add(self.db, "2099-01-02", "15:00", "17:00", "The Devonshire 午餐", "Denman St", note="来自邮件")
        self.db.conn.execute("UPDATE future_events SET conflict = ? WHERE id = ?", ("伦敦最火Sunday Roast", eid))
        self.db.conn.commit()
        self.assertEqual(nudges.hints(self.db, london(6, 0).date()), [])  # 还没问过：不挂
        with self.only("_clash"):
            self.assertIn(f"clash:{eid}：已发送", proactive.tick(self.ethan, self.channel, now=london(6, 13)))
            self.assertNotIn(f"clash:{eid}", proactive.tick(self.ethan, self.channel, now=london(7, 13)))
        got = nudges.hints(self.db, london(7, 0).date())
        self.assertEqual([h["key"] for h in got], [f"clash:{eid}"])
        self.assertIn("The Devonshire 午餐", got[0]["text"])
        # 工作台首页拿得到，点 × 就不再挂
        wb = Workbench(self.store, self.ethan.router.settings)
        with mock.patch.object(Workbench, "now", lambda self: london(7, 9)):
            self.assertEqual([h["key"] for h in wb.hints(london(7, 0).date())], [f"clash:{eid}"])
            self.assertTrue(wb.act("hint.off", {"key": f"clash:{eid}"})["ok"])
            self.assertEqual(wb.hints(london(7, 0).date()), [])
        self.assertFalse(nudges.dismiss_hint(self.db, "sit")["ok"])  # 只有登记了 hint 的几类能点掉

    def test_hint_stays_until_settled(self):
        """她回过话也照样挂，约不再撞了才收。"""
        eid = events.add(self.db, "2099-01-02", "15:00", "17:00", "午餐", "", note="")
        self.db.conn.execute("UPDATE future_events SET conflict = ? WHERE id = ?", ("别的约", eid))
        self.db.conn.commit()
        nudges.log_sent(self.db, f"clash:{eid}", london(6, 13))
        nudges.mark_told(self.db, [f"clash:{eid}"])
        self.store.raw.append("telegram", "mia", "嗯", at=london(6, 14))  # 她回过一句
        self.assertEqual(len(nudges.hints(self.db, london(6, 0).date())), 1)  # 她回过话：照样挂
        self.assertEqual(len(nudges.hints(self.db, london(9, 0).date())), 1)  # 过了几天还撞着：照样挂
        self.db.conn.execute("UPDATE future_events SET conflict = NULL WHERE id = ?", (eid,))
        self.db.conn.commit()
        self.assertEqual(nudges.hints(self.db, london(9, 0).date()), [])  # 了结了（不再撞）：收起


class GrokRelayQuietTest(unittest.TestCase):
    """1：Grok 回报转告守免打扰，夜里到的早上 8 点后再说。"""

    def test_night_result_waits_until_morning(self):
        from ethan.channels.telegram import TelegramChannel
        from ethan.core.agent import Ethan
        from ethan.core.ledger import Ledger
        from ethan.core.router import Router, Settings
        from tests.test_relay import write
        from tests.test_stage1 import Backend
        from tests.test_telegram import FakeAPI
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = MemoryStore(root / "data")
            try:
                write(store.root, [("2026-10-27T01:50:00+00:00", "telegram", "查一下周末的展")],
                      [("2026-10-27T02:13:31+00:00", "【展览】三个都还有票")])
                router = Router(Backend("查到啦\n三个都还有票"), Ledger(root / "l"), Settings(root / "s.json"))
                ch = TelegramChannel(FakeAPI([]), Ethan(store, router, persona="人设"), sleep=lambda s: None)
                ch.settings.set("telegram_owner_chat_id", 42)
                self.assertIn("免打扰", ch.relay_grok(now=datetime(2026, 10, 27, 3, 0, tzinfo=timezone.utc)))
                self.assertEqual(ch.api.sent_texts(), [])
                ch.relay_grok(now=datetime(2026, 10, 27, 8, 10, tzinfo=timezone.utc))  # 本地 08:10
                self.assertEqual(ch.api.sent_texts(), ["查到啦", "三个都还有票"])
                ch.relay_grok(now=datetime(2026, 10, 27, 8, 20, tzinfo=timezone.utc))
                self.assertEqual(len(ch.api.sent_texts()), 2)  # 不重复转告
            finally:
                store.close()


class DemoAppQuietTest(unittest.TestCase):
    """2：紧急的照发；早晚两次常规的守免打扰，挡住的先存着、结束后补发。"""

    def setUp(self):
        from ethan.fancy_moderation import Alerts
        from tests.test_fancy_moderation import FakeTelegram
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.tg = FakeTelegram()
        self.alerts = Alerts(self.root, self.tg)

    def tearDown(self):
        self.tmp.cleanup()

    def body(self, **over):
        from tests.test_fancy_moderation import alert
        return alert(**over)

    def test_urgent_goes_out_at_night(self):
        night = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
        code, out = self.alerts.handle(self.body(kind="urgent", count=1, reason="safety"), now=night)
        self.assertTrue(out["sent"])
        self.assertEqual(len(self.tg.sent), 1)

    def test_routine_held_until_quiet_ends(self):
        night = datetime(2026, 11, 6, 3, 0, tzinfo=timezone.utc)
        code, out = self.alerts.handle(self.body(kind="morning"), now=night)
        self.assertEqual((code, out["sent"]), (200, False))
        self.assertIn("免打扰", out["why"])
        self.assertEqual(self.tg.sent, [])
        self.assertEqual(self.alerts.flush_held(datetime(2026, 11, 6, 7, 50, tzinfo=timezone.utc)), [])
        self.assertEqual(self.alerts.flush_held(datetime(2026, 11, 6, 8, 5, tzinfo=timezone.utc)), ["morning"])
        self.assertEqual(len(self.tg.sent), 1)
        self.assertEqual(self.alerts.flush_held(datetime(2026, 11, 6, 8, 15, tzinfo=timezone.utc)), [])  # 只补一次
        log = [json.loads(l)["result"] for l in (self.root / "fancy-moderation.jsonl").read_text().splitlines()]
        self.assertEqual(log, ["held", "sent (held)"])

    def test_stale_held_alert_dropped(self):
        self.alerts.handle(self.body(kind="evening", dueWithin6h=2), now=datetime(2026, 11, 6, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(self.alerts.flush_held(datetime(2026, 11, 6, 15, 0, tzinfo=timezone.utc)), [])  # 存了 14 小时以上：数字过时，作废
        self.assertEqual(self.tg.sent, [])


if __name__ == "__main__":
    unittest.main()
