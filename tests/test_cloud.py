import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from ethan.cloud import CloudClient, main


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.body


class CloudClientTest(unittest.TestCase):
    def setUp(self):
        self.seen = []

        def opener(req, timeout):
            self.seen.append((req, timeout))
            return FakeResponse(b'{"ok": true}')

        self.client = CloudClient("test-token", "https://example.test/", opener=opener)

    def test_get_headers_and_no_body(self):
        self.assertEqual(self.client.results(), {"ok": True})
        req, timeout = self.seen[0]
        self.assertEqual(req.full_url, "https://example.test/cloud/grok/results")
        self.assertEqual(req.get_method(), "GET")
        self.assertIsNone(req.data, "GET 请求不应带正文")
        self.assertEqual(req.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(req.get_header("User-agent"), "Ethan-cloud/1.0")
        self.assertEqual(timeout, 20)

    def test_post_json_body(self):
        self.assertEqual(self.client.send("查一下"), {"ok": True})
        req, _ = self.seen[0]
        self.assertEqual(req.full_url, "https://example.test/cloud/grok")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(req.data.decode("utf-8")), {"task": "查一下"})

    def test_http_error_returns_json_or_status(self):
        for body, expected in [(b'{"ok": false, "error": "denied"}', {"ok": False, "error": "denied"}),
                               (b"not json", {"ok": False, "error": "HTTP 403"})]:
            with self.subTest(body=body):
                def opener(req, timeout):
                    raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(body))

                client = CloudClient("test-token", opener=opener)
                self.assertEqual(client.results(), expected)


class CloudMainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patcher = patch("ethan.cloud.CloudClient")
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)
        self.client = self.client_class.return_value
        self.client._call.return_value = {"ok": True}

    def run_main(self, argv, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(argv, env={"ETHAN_CLOUD_TOKEN": "test-token"} if env is None else env)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_missing_token(self):
        code, _, stderr = self.run_main(["results"], env={})
        self.assertEqual(code, 2)
        self.assertIn("缺少 ETHAN_CLOUD_TOKEN", stderr)
        self.client_class.assert_not_called()

    def test_agent_add_reads_spec_file(self):
        spec = "完成客户端测试\n保留中文要求。\n"
        path = self.root / "要求.md"
        path.write_text(spec, encoding="utf-8")
        code, stdout, _ = self.run_main(["agent-add", "--title", "补测试", "--spec-file", str(path),
                                         "--size", "2"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"ok": True})
        self.client._call.assert_called_once_with("POST", "/cloud/agent/tasks", {
            "agent": "codex", "title": "补测试", "spec": spec, "verify": "", "size": 2, "project": ""})
        self.assertIsInstance(self.client._call.call_args.args[2]["size"], int, "任务大小应为整数")

    def test_agent_review_reject_with_note(self):
        code, _, _ = self.run_main(["agent-review", "12", "reject", "--note", "意见"])
        self.assertEqual(code, 0)
        self.client._call.assert_called_once_with("POST", "/cloud/agent/tasks/12/review",
                                                  {"decision": "reject", "note": "意见"})

    def test_agent_get_writes_patch_and_replaces_output(self):
        patch_text = "diff --git a/示例 b/示例\n+新增内容\n"
        self.client._call.return_value = {"ok": True, "task": {"id": 12, "patch": patch_text}}
        path = self.root / "12.patch"
        code, stdout, _ = self.run_main(["agent-get", "12", "--patch-out", str(path)])
        self.assertEqual(code, 0)
        self.client._call.assert_called_once_with("GET", "/cloud/agent/tasks/12")
        self.assertEqual(path.read_text(encoding="utf-8"), patch_text)
        self.assertEqual(json.loads(stdout)["task"]["patch"], f"（已写到 {path}）")
        self.assertNotIn(patch_text, stdout, "输出不应再包含补丁原文")
        self.assertNotIn("+新增内容", stdout)

    def test_agent_list_filters_status(self):
        code, _, _ = self.run_main(["agent-list", "--status", "review"])
        self.assertEqual(code, 0)
        self.client._call.assert_called_once_with("GET", "/cloud/agent/tasks?status=review")


if __name__ == "__main__":
    unittest.main()


class SessionsSnapshotTest(unittest.TestCase):
    def test_snapshot_skips_archived_and_old_and_labels_where(self):
        from datetime import datetime, timezone
        from ethan.cloud import sessions_snapshot
        now = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)
        raw = {"ccr": {"data": [
            {"title": "DemoApp 开发-云端", "session_status": "SESSION_STATUS_RUNNING", "status_bucket": "SESSION_STATUS_BUCKET_WORKING",
             "environment_kind": "anthropic_cloud", "updated_at": "2026-10-28T08:54:01Z", "task_summary": "跑 iOS 构建"},
            {"title": "DemoApp 开发-本地", "session_status": "SESSION_STATUS_IDLE", "status_bucket": "SESSION_STATUS_BUCKET_COMPLETED",
             "environment_kind": "bridge", "origin": "claude_code_cli", "updated_at": "2026-10-28T08:39:03Z",
             "post_turn_summary": {"recent_action": "走查完成"}},
            {"title": "导出文档", "session_status": "SESSION_STATUS_IDLE", "status_bucket": "SESSION_STATUS_BUCKET_COMPLETED",
             "environment_kind": "bridge", "origin": "claude_code_mcp_seed", "updated_at": "2026-10-28T08:36:09Z"},
            {"title": "归档的", "session_status": "SESSION_STATUS_ARCHIVED", "updated_at": "2026-10-28T08:00:00Z"},
            {"title": "很久以前", "session_status": "SESSION_STATUS_IDLE", "updated_at": "2026-10-20T08:00:00Z"}]}}
        out = sessions_snapshot(raw, now)
        self.assertEqual([(s["title"], s["state"], s["where"]) for s in out],
                         [("DemoApp 开发-云端", "working", "cloud"), ("DemoApp 开发-本地", "completed", "mac"),
                          ("导出文档", "completed", "server")])
        self.assertEqual(out[1]["summary"], "走查完成")
