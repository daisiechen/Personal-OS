import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ethan import agents, codex_runner
from ethan.inbox import Inbox
from ethan.memory import MemoryStore

CLOUD = "cloud-token"
AUTH = {"Authorization": f"Bearer {CLOUD}"}

# 假 Codex：在当前目录写一个文件，像 codex exec --json 一样输出用量和最后一条消息
FAKE_CODEX = r'''
import json, sys, pathlib
prompt = sys.argv[-1]
mode = "ok"
for m in ("QUOTA", "BREAK", "NOTHING"):
    if m in prompt:
        mode = m
if "BADFLAG" in prompt:
    print("error: unexpected argument", file=sys.stderr); sys.exit(2)
if mode == "QUOTA":
    print("You've hit your usage limit. Try again in 3 hours.", file=sys.stderr); sys.exit(1)
if mode != "NOTHING":
    pathlib.Path("hello.txt").write_text("broken" if mode == "BREAK" else "hi")
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1200, "output_tokens": 300}}))
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "写好了 hello.txt"}}))
'''


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class QueueAndScoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "data")
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_review_reject_then_accept_counts_rework(self):
        tid = agents.add(self.db, "codex", "写测试", spec="补上测试", size=2)
        agents.update(self.db, tid, status="review")
        agents.review(self.db, tid, "reject", "漏了边界情况")
        t = agents.get(self.db, tid)
        self.assertEqual((t["status"], t["reworks"]), ("queued", 1))
        self.assertIn("漏了边界情况", t["spec"])
        agents.update(self.db, tid, status="review")
        agents.review(self.db, tid, "accept")
        self.assertEqual(agents.get(self.db, tid)["status"], "accepted")
        with self.assertRaises(ValueError):
            agents.review(self.db, tid, "accept")  # 已经通过的不能再审
        with self.assertRaises(ValueError):
            agents.add(self.db, "codex", "x", size=4)  # 大小只能是 1/2/3/5/8

    def test_scoreboard_compares_same_size_only(self):
        # 小活：codex 一次过、便宜；大活：claude 返工一次，只和别的 8 点活比
        for cost in (1.0, 1.0):
            agents.record_done(self.db, "codex", "小活", 1, cost_usd=cost, work_seconds=60)
        agents.record_done(self.db, "claude", "大活", 8, cost_usd=10, work_seconds=3600, reworks=1)
        board = {r["agent"]: r for r in agents.scoreboard(self.db)}
        self.assertEqual(board["codex"]["done"], 2)
        self.assertEqual(board["codex"]["first_pass"], 100)
        # 质量 1、成本速度都等于中位数（1/1.5）、没评价 0.5 → 每点 0.4+0.133+0.133+0.1
        self.assertEqual(board["codex"]["efficiency"], 77)
        self.assertEqual(board["claude"]["first_pass"], 0)
        self.assertEqual(board["claude"]["efficiency"], 57)  # 返工一次质量减半，但不因活大吃亏
        self.assertIsNone(board["grok"]["efficiency"])

    def test_own_work_shows_running_then_finishes(self):
        tid = agents.start(self.db, "claude", "领地地图设计稿", size=3, project="工作台")
        self.assertEqual(agents.get(self.db, tid)["status"], "running")
        self.assertEqual(agents.finish(self.db, tid, "review")["status"], "review")  # 等她看
        t = agents.finish(self.db, tid, "accepted")
        self.assertEqual(t["status"], "accepted")
        self.assertIsNotNone(t["reviewed_at"])
        with self.assertRaises(ValueError):
            agents.finish(self.db, tid)

    def test_rating_counts(self):
        tid = agents.record_done(self.db, "codex", "小活", 1)
        agents.rate(self.db, tid, 5)
        self.assertEqual({r["agent"]: r for r in agents.scoreboard(self.db)}["codex"]["efficiency"], 87)
        with self.assertRaises(ValueError):
            agents.rate(self.db, tid, -1)  # 2026-10-29 起是 1–5 分

    def test_unrated_for_7_days_defaults_to_3(self):
        from datetime import datetime, timedelta, timezone
        tid = agents.record_done(self.db, "codex", "小活", 1)
        self.assertEqual(agents.auto_rate(self.db), 0)  # 刚通过的不动
        self.assertEqual(agents.auto_rate(self.db, datetime.now(timezone.utc) + timedelta(days=8)), 1)
        self.assertEqual(self.db.conn.execute("SELECT rating FROM agent_tasks WHERE id = ?", (tid,)).fetchone()[0], 3)


class CodexRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        origin, self.repo = root / "origin.git", root / "repo"
        git("init", "-q", "--bare", "-b", "main", str(origin))
        git("clone", "-q", str(origin), str(self.repo))
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)
        fake = root / "fake_codex.py"
        fake.write_text(FAKE_CODEX)
        self.cmd = [sys.executable, str(fake)]
        self.work = root / "work"
        self.work.mkdir()
        self.store = MemoryStore(root / "data")
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def run_once(self):
        return codex_runner.run_once(self.db, self.repo, self.work, cmd=self.cmd)

    def test_does_work_verifies_and_waits_for_review(self):
        self.assertIsNone(self.run_once())  # 没活
        tid = agents.add(self.db, "codex", "写 hello", spec="新建 hello.txt", verify="grep -q hi hello.txt", size=1)
        self.assertEqual(self.run_once(), "review")
        t = agents.get(self.db, tid)
        self.assertIn("hello.txt", t["patch"])
        self.assertEqual((t["verify_ok"], t["tokens_in"], t["tokens_out"]), (1, 1200, 300))
        self.assertIn("写好了 hello.txt", t["log"])
        self.assertFalse(list(self.work.iterdir()))  # 副本用完删掉
        self.assertNotIn("hello.txt", [p.name for p in self.repo.iterdir()])  # 不碰线上目录

    def test_failed_verify_and_empty_change(self):
        a = agents.add(self.db, "codex", "BREAK", verify="grep -q hi hello.txt")
        self.assertEqual(self.run_once(), "failed")
        self.assertEqual(agents.get(self.db, a)["verify_ok"], 0)
        b = agents.add(self.db, "codex", "NOTHING")
        self.assertEqual(self.run_once(), "failed")
        self.assertIn("没有改动", agents.get(self.db, b)["log"])

    def test_codex_crash_before_work_is_environment_not_failure(self):
        tid = agents.add(self.db, "codex", "BADFLAG")
        self.assertEqual(self.run_once(), "queued")
        t = agents.get(self.db, tid)
        self.assertIn("unexpected argument", t["log"])
        self.assertEqual(t["reworks"], 0)

    def test_retry_failed_without_rework(self):
        tid = agents.add(self.db, "codex", "BREAK", verify="grep -q hi hello.txt")
        self.run_once()
        self.assertEqual(agents.retry(self.db, tid)["status"], "queued")
        self.assertEqual(agents.get(self.db, tid)["reworks"], 0)
        with self.assertRaises(ValueError):
            agents.retry(self.db, tid)

    def test_quota_puts_task_back_for_later(self):
        tid = agents.add(self.db, "codex", "QUOTA")
        self.assertEqual(self.run_once(), "queued")
        t = agents.get(self.db, tid)
        self.assertEqual(t["status"], "queued")
        self.assertGreater(t["retry_after"], agents.now())
        self.assertIsNone(self.run_once())  # 没到点不领


class InboxAgentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "data")
        self.inbox = Inbox(self.store, {"grok": "g", "cloud": CLOUD})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, path, body, headers=AUTH):
        return self.inbox.handle(path, headers, json.dumps(body).encode())

    def test_map_images_need_token_and_stay_in_folder(self):
        maps = Path(self.store.root) / "maps"
        maps.mkdir()
        (maps / "map-1.jpg").write_bytes(b"\xff\xd8jpg")
        self.assertEqual(self.inbox.handle_get("/cloud/maps/map-1.jpg", {})[0], 401)
        status, out = self.inbox.handle_get("/cloud/maps/map-1.jpg", AUTH)
        self.assertEqual((status, out["base64"]), (200, "/9hqcGc="))
        self.assertEqual(self.inbox.handle_get("/cloud/maps/../ethan.sqlite", AUTH)[0], 404)
        self.assertEqual(self.inbox.handle_get("/cloud/maps/nope.jpg", AUTH)[0], 404)

    def test_enqueue_list_get_review(self):
        self.assertEqual(self.post("/cloud/agent/tasks", {"title": "x"}, {})[0], 401)
        status, out = self.post("/cloud/agent/tasks", {"title": "补测试", "spec": "s", "size": 2, "project": "工作台"})
        self.assertEqual(status, 200)
        tid = out["id"]
        status, out = self.inbox.handle_get("/cloud/agent/tasks?status=queued", AUTH)
        self.assertEqual([t["id"] for t in out["tasks"]], [tid])
        self.assertNotIn("patch", out["tasks"][0])
        agents.update(self.store.db, tid, status="review", patch="diff")
        self.assertEqual(self.inbox.handle_get(f"/cloud/agent/tasks/{tid}", AUTH)[1]["task"]["patch"], "diff")
        status, out = self.post(f"/cloud/agent/tasks/{tid}/review", {"decision": "accept"})
        self.assertEqual(out["task"]["status"], "accepted")
        self.assertEqual(self.post("/cloud/agent/tasks", {"title": "x", "size": 4})[0], 400)
        self.assertEqual(self.post("/cloud/agent/done", {"title": "我做的", "size": 3})[0], 200)
        board = self.inbox.handle_get("/cloud/agent/scoreboard", AUTH)[1]["scoreboard"]
        self.assertEqual({r["agent"]: r["done"] for r in board}["claude"], 1)
        self.assertEqual(self.post("/cloud/agent/sessions", {"sessions": []}, {})[0], 401)
        status, out = self.post("/cloud/agent/sessions", {"sessions": [{"title": "DemoApp", "state": "working"}]})
        self.assertEqual((status, out["saved"]), (200, 1))
        self.assertEqual(agents.load_sessions(self.store.root)["sessions"][0]["title"], "DemoApp")


if __name__ == "__main__":
    unittest.main()


class LiveWorkTest(unittest.TestCase):
    """2026-10-29 她说团队页漏活、失败怎么判：她叫停的算放弃不扣分；小伴 派给 Grok 还没回的算在干。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "data"
        self.store = MemoryStore(self.root)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_drop_is_not_failure(self):
        tid = agents.add(self.db, "codex", "DemoApp 首页", size=3)
        self.assertEqual(agents.drop(self.db, tid)["status"], "dropped")
        row = next(r for r in agents.scoreboard(self.db) if r["agent"] == "codex")
        self.assertEqual((row["failed"], row["open"]), (0, 0))
        with self.assertRaises(ValueError):
            agents.drop(self.db, tid)

    def test_grok_dispatch_matched_with_reports(self):
        now = datetime.now(timezone.utc)
        at = lambda m: (now - timedelta(minutes=m)).isoformat(timespec="seconds")
        (self.root / "grok-tasks.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            {"at": at(90), "task": "查芬兰机票", "ok": True}, {"at": at(30), "task": "查 parkrun", "ok": True}]) + "\n")
        (self.root / "inbox").mkdir(exist_ok=True)
        (self.root / "inbox" / f"{now.date().isoformat()}.jsonl").write_text(
            json.dumps({"at": at(60), "path": "/grok/result", "text": "芬兰机票三个选项"}, ensure_ascii=False) + "\n")
        live = agents.grok_live(self.root)
        self.assertEqual([x["title"] for x in live["running"]], ["查 parkrun"])
        self.assertEqual([(x["title"], x["asked"]) for x in live["done"]], [("芬兰机票三个选项", True)])

    def test_grok_matched_by_receipt_and_self_started(self):
        """2026-10-29：按回执号配对，收藏日报不抵掉在干的；她在 Grok 里直接交代的，Grok 报【开工】就显示在干。"""
        now = datetime.now(timezone.utc)
        at = lambda m: (now - timedelta(minutes=m)).isoformat(timespec="seconds")
        (self.root / "grok-tasks.jsonl").write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in [
            {"at": at(90), "task": "查芬兰机票", "ok": True, "ref": "G1"}, {"at": at(80), "task": "查 parkrun", "ok": True, "ref": "G2"}]) + "\n")
        (self.root / "inbox").mkdir(exist_ok=True)
        (self.root / "inbox" / f"{now.date().isoformat()}.jsonl").write_text("".join(
            json.dumps({"at": at(m), "path": "/grok/result", "text": t}, ensure_ascii=False) + "\n" for m, t in [
                (70, "【收藏日报】今天 3 条"), (60, "【回执 G2】parkrun 周六 9 点"), (50, "【开工】比较三家健身房"),
                (40, "【开工】找芬兰观光团")]))
        live = agents.grok_live(self.root)
        self.assertEqual([x["title"] for x in live["running"]], ["查芬兰机票", "比较三家健身房", "找芬兰观光团"])
        self.assertIn("parkrun 周六 9 点", [x["title"] for x in live["done"]])
        with (self.root / "inbox" / f"{now.date().isoformat()}.jsonl").open("a") as f:
            f.write(json.dumps({"at": at(10), "path": "/grok/result", "text": "三家健身房对比好了"}, ensure_ascii=False) + "\n")
        self.assertEqual([x["title"] for x in agents.grok_live(self.root)["running"]], ["查芬兰机票", "找芬兰观光团"])


class StaleCodexTest(LiveWorkTest):
    def test_running_too_long_becomes_failed(self):
        tid = agents.add(self.db, "codex", "做到一半断了", size=2)
        old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
        agents.update(self.db, tid, status="running", started_at=old)
        fresh = agents.add(self.db, "codex", "刚开始", size=1)
        agents.update(self.db, fresh, status="running", started_at=agents.now())
        self.assertEqual(codex_runner.recover_stale(self.db), [tid])
        self.assertEqual(agents.get(self.db, tid)["status"], "failed")
        self.assertEqual(agents.get(self.db, fresh)["status"], "running")


class SelfStartedExpiryTest(unittest.TestCase):
    def test_self_started_running_too_long_is_failed(self):
        import tempfile as _t
        from ethan.memory import MemoryStore
        with _t.TemporaryDirectory() as d:
            store = MemoryStore(Path(d))
            tid = agents.start(store.db, "claude", "做到一半没收尾")
            self.assertEqual(agents.expire_self_started(store.db), [])
            self.assertEqual(agents.expire_self_started(store.db, datetime.now(timezone.utc) + timedelta(hours=13)), [tid])
            self.assertEqual(agents.get(store.db, tid)["status"], "failed")
            store.close()
