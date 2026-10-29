import json
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from ethan.core.agent import Ethan, build_system_prompt, load_persona, parse_reply, to_messages
from ethan.core.backends import BackendUnavailable, OpenRouterBackend, Reply, SubscriptionBackend
from ethan.core.ledger import Ledger
from ethan.core.router import (AllBackendsFailed, BudgetExceeded, NeedsSwitchApproval, Router,
                               Settings)
from ethan.core.splitter import limit_bubbles, split_bubbles, typing_delay
from ethan.memory import MemoryStore


class FakeBackend:
    def __init__(self, name, text="好呀", cost=0.0, fail=False):
        self.name, self.text, self.cost, self.fail = name, text, cost, fail
        self.calls = []

    def complete(self, system, messages, images=()):
        self.calls.append((system, messages))
        if self.fail:
            raise BackendUnavailable("rate_limit")
        return Reply(self.text, self.name, f"{self.name}-model", self.cost)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def router(self, primary, fallback=None, budget=50.0):
        return Router(primary, Ledger(self.root / "ledger", budget),
                      Settings(self.root / "settings.json"), fallback=fallback)


class SplitterTest(unittest.TestCase):
    def test_splits_lines_and_sentences(self):
        text = "那就不干了。累成这样，今天被谁折腾了？\n等等，饭吃了没"
        self.assertEqual(split_bubbles(text), ["那就不干了。累成这样，今天被谁折腾了？", "等等，饭吃了没"])
        long_line = "那就不干了。" + "累成这样今天到底是被谁折腾成这样了啊？" * 2
        self.assertEqual(split_bubbles(long_line)[0], "那就不干了")

    def test_long_sentence_breaks_at_commas(self):
        text = "这周推进了两个客户，练了三次课，还把论文框架搭起来了，这叫什么都做不好"
        bubbles = split_bubbles(text, max_len=20)
        self.assertTrue(all(len(b) <= 20 for b in bubbles))
        self.assertEqual("".join(bubbles).replace("，", ""), text.replace("，", ""))

    def test_typing_delay_bounds(self):
        self.assertEqual(typing_delay("好"), 0.5)
        self.assertEqual(typing_delay("很" * 100), 1.5)

    def test_limit_bubbles_merges_extras_into_last(self):
        self.assertEqual(limit_bubbles(["a", "b"]), ["a", "b"])
        self.assertEqual(limit_bubbles(["a", "b", "c", "d", "e"]), ["a", "b", "c，d，e"])


class LedgerTest(TempDirCase):
    def test_budget_status_counts_only_openrouter(self):
        ledger = Ledger(self.root, monthly_budget_usd=10)
        at = datetime(2026, 10, 22, tzinfo=timezone.utc)
        ledger.record("subscription", "sonnet", 30.0, at=at)
        self.assertEqual(ledger.budget_status(at), "ok")
        ledger.record("openrouter", "sonnet", 8.5, at=at)
        self.assertEqual(ledger.budget_status(at), "warn")
        ledger.record("openrouter", "sonnet", 2.0, at=at)
        self.assertEqual(ledger.budget_status(at), "over")
        self.assertEqual(ledger.budget_status(datetime(2026, 11, 1, tzinfo=timezone.utc)), "ok")


class RouterTest(TempDirCase):
    def test_subscription_first(self):
        primary, fallback = FakeBackend("subscription"), FakeBackend("openrouter")
        reply = self.router(primary, fallback).complete("sys", [])
        self.assertEqual(reply.backend, "subscription")
        self.assertEqual(fallback.calls, [])

    def test_no_auto_switch_without_approval(self):
        primary, fallback = FakeBackend("subscription", fail=True), FakeBackend("openrouter")
        with self.assertRaises(NeedsSwitchApproval):
            self.router(primary, fallback).complete("sys", [])
        self.assertEqual(fallback.calls, [])

    def test_switch_after_approval_and_budget_guard(self):
        primary = FakeBackend("subscription", fail=True)
        fallback = FakeBackend("openrouter", cost=6.0)
        router = self.router(primary, fallback, budget=10)
        router.enable_fallback(True)
        self.assertEqual(router.complete("sys", []).backend, "openrouter")
        router.complete("sys", [])
        with self.assertRaises(BudgetExceeded):
            router.complete("sys", [])

    def test_prefers_subscription_again_once_recovered(self):
        primary, fallback = FakeBackend("subscription"), FakeBackend("openrouter")
        router = self.router(primary, fallback)
        router.enable_fallback(True)
        self.assertEqual(router.complete("sys", []).backend, "subscription")

    def test_all_failed_without_fallback(self):
        with self.assertRaises(AllBackendsFailed):
            self.router(FakeBackend("subscription", fail=True)).complete("sys", [])


class SubscriptionBackendTest(unittest.TestCase):
    def fake_run(self, returncode=0, payload=None):
        calls = []

        def run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            stdout = json.dumps(payload) if payload is not None else "oops"
            return subprocess.CompletedProcess(cmd, returncode, stdout, "err")
        return run, calls

    def test_success_and_no_api_key_leak(self):
        run, calls = self.fake_run(payload={"subtype": "success", "is_error": False,
                                            "result": " 早呀 ", "total_cost_usd": 0.01})
        import os
        os.environ["ANTHROPIC_API_KEY"] = "dummy"
        try:
            reply = SubscriptionBackend(runner=run).complete("人设", [{"role": "user", "content": "早"}])
        finally:
            del os.environ["ANTHROPIC_API_KEY"]
        self.assertEqual(reply.text, "早呀")
        cmd, kwargs = calls[0]
        self.assertNotIn("ANTHROPIC_API_KEY", kwargs["env"])
        self.assertIn("--system-prompt", cmd)
        self.assertTrue(kwargs["input"].endswith("Mia：早\n小伴："))

    def test_images_only_allow_reading_their_folder(self):
        run, calls = self.fake_run(payload={"subtype": "success", "is_error": False, "result": "好看"})
        SubscriptionBackend(runner=run).complete(
            "s", [{"role": "user", "content": "看"}], images=["/home/ethan/data/media/a.jpg"])
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[cmd.index("--tools") + 1], "Read")
        self.assertIn("Read(//home/ethan/data/media/**)", cmd)
        self.assertIn("/home/ethan/data/media/a.jpg", kwargs["input"])
        run, calls = self.fake_run(payload={"subtype": "success", "is_error": False, "result": "嗯"})
        SubscriptionBackend(runner=run).complete("s", [{"role": "user", "content": "早"}])
        self.assertEqual(calls[0][0][calls[0][0].index("--tools") + 1], "")

    def test_errors_raise_unavailable(self):
        for run, _ in (self.fake_run(returncode=1, payload={}),
                       self.fake_run(payload={"subtype": "error_during_execution", "is_error": True}),
                       self.fake_run(payload=None)):
            with self.assertRaises(BackendUnavailable):
                SubscriptionBackend(runner=run).complete("s", [])


class OpenRouterBackendTest(TempDirCase):
    class Resp:
        def __init__(self, data):
            self.data = json.dumps(data).encode()

        def read(self):
            return self.data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def test_parses_reply_and_cost(self):
        sent = []

        def opener(req, timeout):
            sent.append(json.loads(req.data))
            return self.Resp({"choices": [{"message": {"content": "在呢"}}], "usage": {"cost": 0.002}})
        reply = OpenRouterBackend("k", opener=opener).complete("人设", [{"role": "user", "content": "在吗"}])
        self.assertEqual((reply.text, reply.cost_usd), ("在呢", 0.002))
        self.assertEqual(sent[0]["messages"][0], {"role": "system", "content": "人设"})

    def test_fallback_is_grok_and_pdf_goes_as_file(self):
        sent = []

        def opener(req, timeout):
            sent.append(json.loads(req.data))
            return self.Resp({"choices": [{"message": {"content": "看了"}}]})
        pdf = Path(self.tmp.name) / "a.pdf"
        pdf.write_bytes(b"%PDF-1.4")
        OpenRouterBackend("k", opener=opener).complete("s", [{"role": "user", "content": "看看"}], images=[str(pdf)])
        self.assertEqual(sent[0]["model"], "x-ai/grok-4.7")
        part = sent[0]["messages"][-1]["content"][1]
        self.assertEqual(part["type"], "file")
        self.assertTrue(part["file"]["file_data"].startswith("data:application/pdf;base64,"))

    def test_old_claude_fallback_setting_is_replaced(self):
        from unittest import mock
        from ethan.cli import build_router
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "k", "ETHAN_OPENROUTER_MODEL": "anthropic/claude-sonnet-4.5"}):
            router = build_router(Path(self.tmp.name), 50)
        self.assertEqual(router.fallback.model, "x-ai/grok-4.7")

    def test_network_error_raises_unavailable(self):
        def opener(req, timeout):
            raise OSError("down")
        with self.assertRaises(BackendUnavailable):
            OpenRouterBackend("k", opener=opener).complete("s", [])


class AgentTest(TempDirCase):
    def test_persona_loads(self):
        self.assertIn("## 示例对话", load_persona())

    def test_persona_stays_in_character_with_a_safety_floor(self):
        persona = load_persona()
        self.assertIn("## 沉浸感", persona)
        self.assertNotIn("被认真问到时，承认自己是 AI", persona)
        self.assertIn("不硬说自己是真人", persona)  # 她认真出戏问、情绪危机或重大决定时温柔说明

    def test_to_messages_merges_consecutive_roles(self):
        records = [{"role": "mia", "text": "在吗"}, {"role": "mia", "text": "好累"},
                   {"role": "ethan", "text": "在"}]
        self.assertEqual(to_messages(records), [
            {"role": "user", "content": "在吗\n好累"}, {"role": "assistant", "content": "在"}])

    def test_system_prompt_includes_brief_not_raw_messages(self):
        ctx = {"today": "2026-10-22", "about_mia": "喜欢绿色",
               "active_goals": [{"title": "创业跑出订单"}], "recent_messages": [{"text": "私密原文"}],
               "wiki_pages": ["about-mia"]}
        prompt = build_system_prompt("人设", ctx)
        self.assertIn("喜欢绿色", prompt)
        self.assertIn("创业跑出订单", prompt)
        self.assertNotIn("私密原文", prompt)

    def test_respond_records_both_sides_and_splits(self):
        store = MemoryStore(self.root / "data")
        backend = FakeBackend("subscription", text="那就不干了\n饭吃了没")
        ethan = Ethan(store, self.router(backend), persona="人设")
        bubbles = ethan.respond("cli", "好累", today=date(2026, 10, 22))
        self.assertEqual(bubbles, ["那就不干了", "饭吃了没"])
        system, messages = backend.calls[0]
        self.assertEqual(messages[-1]["role"], "user")
        self.assertRegex(messages[-1]["content"], r"^【\d{2}-\d{2} \d{2}:\d{2}】好累$")
        log = store.raw.recent(10)
        self.assertEqual([r["role"] for r in log], ["mia", "ethan"])
        self.assertEqual(log[1]["backend"], "subscription")
        store.close()


    def test_topic_preferences_are_hidden_and_remembered(self):
        store = MemoryStore(self.root / "data")
        backend = FakeBackend("subscription", text="好好好不聊狗了\n今天 PPT 写到哪了 [避开 Max 那只狗的细节]")
        ethan = Ethan(store, self.router(backend), persona="人设")
        bubbles = ethan.respond("cli", "不记得叫啥了", today=date(2026, 10, 22))
        self.assertEqual(bubbles, ["好好好不聊狗了", "今天 PPT 写到哪了"])
        page = store.wiki.read("topics")["body"]
        self.assertIn("避开：Max 那只狗的细节（2026-10-22）", page)
        ethan.respond("cli", "嗯", today=date(2026, 10, 23))
        self.assertEqual(store.wiki.read("topics")["body"].count("Max"), 1)
        self.assertIn("Max 那只狗的细节", backend.calls[-1][0])
        self.assertIn("不想聊这个", backend.calls[-1][0])
        store.close()

if __name__ == "__main__":
    unittest.main()


class ConfigurableModelTest(unittest.TestCase):
    def test_openai_compatible_primary_from_env(self):
        """开源准备（2026-10-29）：填了 ETHAN_LLM_* 就直连兼容 OpenAI 接口的模型（比如 DeepSeek），不用 Claude 订阅。"""
        import tempfile
        from pathlib import Path
        from unittest import mock
        from ethan.cli import build_router
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {
                "ETHAN_LLM_BASE_URL": "https://api.deepseek.com/", "ETHAN_LLM_API_KEY": "k", "ETHAN_LLM_MODEL": "deepseek-chat"}):
            r = build_router(Path(d), 5)
            self.assertEqual((r.primary.name, r.primary.URL, r.primary.model),
                             ("api", "https://api.deepseek.com/chat/completions", "deepseek-chat"))
