"""仓库文档里她的原话（ontology-plan 1.4，ethan/docquotes.py）：粗筛、去重、没有钥匙不发。"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ethan import docquotes as dq

DOC = """## 2026-11-03
- 主动问：她 11-03 说“他可以多问一点，我选择性的回答，主要是我懒得打字或者发语音”。
- 计划器：她 11-03 说“好，对账过了就切计划器吧”。
- 改版：她 11-01 说“把任务结构改版一起做了，页面也一起改”。
- 问系统：她 11-04 问“这个为什么是垃圾？”
- 点头：她 11-03 说“可以”。
- 举例：比如她说“训练我喜欢早上去，晚上起不来去不了”。
- 头像（她原话：“都不像，1的嘴太大了，还有法令纹”）。
- 半句：她 11-03 说“他可以多问一点，我选择性的回答”。
- 联系方式：她 11-02 说“我以前常用的邮箱是 someone@example.com，电话 +44 7700 900123”。
"""


class FilterTest(unittest.TestCase):
    def items(self):
        return dq.candidates(dq.scan_text(DOC, "docs/x.md"))

    def test_system_requests_excluded_about_her_kept(self):
        quotes = [i["quote"] for i in self.items()]
        self.assertTrue(any("懒得打字" in q for q in quotes))  # 她的习惯，留下
        self.assertTrue(any("法令纹" in q for q in quotes))  # 写明“原话”、没日期也收
        for bad in ("计划器", "改版", "垃圾", "训练我喜欢"):  # 系统要求、问系统的、太短的、没日期的举例
            self.assertFalse(any(bad in q for q in quotes), bad)
        self.assertFalse(any(q == "可以" for q in quotes))

    def test_rule_words(self):
        self.assertTrue(dq.is_system_request("这个页面的按钮要改一下"))
        self.assertTrue(dq.is_system_request("你先把能力题改写好给我"))
        self.assertTrue(dq.is_system_request("这边基本结束了吗"))
        self.assertFalse(dq.is_system_request("我现在跟 小伴 沟通太少了，主要是我懒得打字"))  # 明说自己的习惯，带“小伴”也留
        self.assertFalse(dq.is_system_request("都不像，1的嘴太大了"))

    def test_source_date_and_handoff_format(self):
        item = next(i for i in self.items() if "懒得打字" in i["quote"])
        self.assertEqual((item["path"], item["line"], item["date"]), ("docs/x.md", 2, "2026-11-03"))
        text = dq.handoff_text([item])
        self.assertEqual(text, f"问：（docs/x.md，2026-11-03，文档转述）\n答：{item['quote']}")

    def test_excerpt_merged_into_longer(self):
        items = [i for i in self.items() if "选择性的回答" in i["quote"]]
        self.assertEqual(len(items), 1)
        self.assertIn("懒得打字", items[0]["quote"])
        self.assertEqual(items[0]["also"], ["docs/x.md:9"])

    def test_contacts_removed(self):
        item = next(i for i in self.items() if "邮箱" in i["quote"])
        self.assertNotIn("example.com", item["quote"])
        self.assertNotIn("7700", item["quote"])
        self.assertEqual(dq.redact("她 2026-10-27 说"), "她 2026-10-27 说")  # 日期不当电话


class SentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.sent = self.tmp / "sent.json"
        self.items = dq.candidates(dq.scan_text(DOC, "docs/x.md"))

    def test_fingerprint_dedup(self):
        self.assertEqual(dq.fingerprint("我 懒得打字"), dq.fingerprint("我懒得打字"))
        dq.save_sent({self.items[0]["fp"]}, self.sent)
        left = dq.fresh(self.items, dq.load_sent(self.sent))
        self.assertEqual(len(left), len(self.items) - 1)
        self.assertNotIn(self.items[0]["fp"], [i["fp"] for i in left])

    def test_no_token_no_send(self):
        class Boom:
            def interview(self, *a):
                raise AssertionError("没有钥匙不该发")
        res = dq.send(self.items, env={}, sent_path=self.sent, out_dir=self.tmp, client=Boom())
        self.assertEqual(res["sent"], 0)
        self.assertIn("ETHAN_CLOUD_TOKEN", res["note"])
        self.assertTrue(Path(res["files"][0]).read_text(encoding="utf-8").startswith("问：（"))
        self.assertFalse(self.sent.exists())  # 没交就不记成交过

    def test_send_records_fingerprints(self):
        calls = []

        class Fake:
            def interview(self, session, scene, text):
                calls.append((session, scene, text))
                return {"ok": True, "evidence": 1}
        res = dq.send(self.items, env={"ETHAN_CLOUD_TOKEN": "t"}, sent_path=self.sent, out_dir=self.tmp, client=Fake())
        self.assertEqual(res["sent"], len(self.items))
        self.assertEqual(calls[0][:2], (dq.SESSION, dq.SCENE))
        self.assertEqual(set(json.loads(self.sent.read_text())["sent"]), {i["fp"] for i in self.items})
        self.assertEqual(res["files"], [])  # 交完临时文件删掉

    def test_failed_send_not_recorded(self):
        class Fail:
            def interview(self, *a):
                return {"ok": False, "error": "HTTP 401"}
        res = dq.send(self.items, env={"ETHAN_CLOUD_TOKEN": "t"}, sent_path=self.sent, out_dir=self.tmp, client=Fail())
        self.assertEqual(res["sent"], 0)
        self.assertTrue(res["errors"])
        self.assertEqual(dq.load_sent(self.sent), set())

    def test_chunks_respect_limit(self):
        many = [dict(self.items[0], fp=str(k)) for k in range(50)]
        parts = dq.chunks(many, size=300)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(dq.handoff_text(p)) <= 300 for p in parts))


WIDE_DOC = """前言没有日期：她说“我平时一个人吃饭比较多”。
## 11-03 工作台反馈
- 没写日期：她说“我每天早上先看日历再起床”。
- 同一行：Mia 那天聊到“我小时候在海边长大，所以喜欢海”。
- 前一行提到她：
  “我出门一般不带伞，淋雨也无所谓”
- 系统要求：她说“把这个页面的按钮改大一点吧”。
- 话里说自己：她说“工作台我平时都是晚上才打开看的”。
- 问系统：她说“我这个任务为什么又没了？”
- 点头：她说“我同意”。
- 举例：比如她说“我喜欢早上去训练场”。
- 例如：例如“我最近都睡得很晚”这种话。
- 像：像“我想把这件事做完再睡”这类。
- 小伴 说的：她问了以后，小伴 回“我已经帮你记下来了这件事”。
- 括号例子：她的决定有好几种（“我明天要把这些全部推迟”）。

无关一行：这里“我不该被收进来的一句很长的话”。
```
她说“代码块里我写的示例句子不算”
```
## 样例（示意，细节是编的）
- 她说：“我做事喜欢拖到最后一刻才动手”。
"""


class WideTest(unittest.TestCase):
    def quotes(self, doc=WIDE_DOC, path="docs/x.md"):
        return {i["quote"]: i for i in dq.candidates(dq.scan_text(doc, path, wide=True))}

    def test_default_unchanged(self):
        rows = dq.scan_text(WIDE_DOC, "docs/x.md")
        self.assertFalse(any("早上先看日历" in r["quote"] for r in rows))  # 不加 --wide，没日期的照旧不收
        self.assertFalse(any("海边" in r["quote"] for r in rows))

    def test_no_date_uses_heading_or_unmarked(self):
        q = self.quotes()
        self.assertEqual(q["我每天早上先看日历再起床"]["date"], "2026-11-03")  # 小节标题“## 11-03 …”
        self.assertEqual(q["我平时一个人吃饭比较多"]["date"], "未注明日期")

    def test_first_person_without_her(self):
        q = self.quotes()
        self.assertIn("我小时候在海边长大，所以喜欢海", q)  # 同一行有 Mia
        self.assertIn("我出门一般不带伞，淋雨也无所谓", q)  # 前一行提到她
        self.assertNotIn("我不该被收进来的一句很长的话", q)  # 上下文没有她
        her_doc = self.quotes("## 2026-11-01\n- 打卡：“我总是忘了打卡，出门了也没点”\n", "docs/design/mias-os.md")
        self.assertEqual(her_doc["我总是忘了打卡，出门了也没点"]["date"], "2026-11-01")  # 记她需求的文档整份算

    def test_exclusions_kept(self):
        q = " | ".join(self.quotes())
        self.assertIn("工作台我平时都是晚上才打开看的", q)  # 带系统词但话里说自己的习惯，留
        for bad in ("按钮改大", "又没了", "我同意", "训练场", "睡得很晚", "做完再睡", "帮你记下来", "全部推迟",
                    "代码块", "拖到最后"):  # 系统要求、问句、点头、比如 / 例如 / 像、别人说的、括号例子、代码块、样例小节
            self.assertNotIn(bad, q, bad)

    def test_skip_docs(self):
        self.assertEqual(dq.scan_text(WIDE_DOC, dq.SKIP_DOCS[0], wide=True), [])  # 检验题、提示词文件不收

    def test_main_wide_skips_sent(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "docs").mkdir()
        (tmp / "docs" / "x.md").write_text(WIDE_DOC, encoding="utf-8")
        sent = tmp / "sent.json"
        dq.save_sent({dq.fingerprint("我每天早上先看日历再起床")}, sent)
        out = tmp / "c.json"
        with contextlib.redirect_stdout(io.StringIO()):
            code = dq.main(["--dry-run", "--wide", "--root", str(tmp), "--sent-file", str(sent),
                            "--json", str(out)])
        self.assertEqual(code, 0)
        got = [i["quote"] for i in json.loads(out.read_text(encoding="utf-8"))]
        self.assertIn("我小时候在海边长大，所以喜欢海", got)
        self.assertNotIn("我每天早上先看日历再起床", got)  # 交过的指纹照旧跳过


if __name__ == "__main__":
    unittest.main()
