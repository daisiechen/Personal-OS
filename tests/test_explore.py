import json
import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

from ethan import explore, territory


def tiny_xlsx(sheets: dict) -> bytes:
    """不靠 openpyxl 拼一个最小的 xlsx：每张表第一行是表头，值用内联字符串。"""
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        names = list(sheets)
        z.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                   'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                   + "".join(f'<sheet name="{n}" sheetId="{i + 1}" r:id="rId{i + 1}"/>' for i, n in enumerate(names)) + "</sheets></workbook>")
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   + "".join(f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml"/>' for i in range(len(names))) + "</Relationships>")
        for i, n in enumerate(names):
            rows = "".join("<row>" + "".join(f'<c r="{chr(65 + j)}{k + 1}" t="inlineStr"><is><t>{v}</t></is></c>' for j, v in enumerate(r) if v)
                           + "</row>" for k, r in enumerate(sheets[n]))
            z.writestr(f"xl/worksheets/sheet{i + 1}.xml", f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>')
    return buf.getvalue()


class ExploreTest(unittest.TestCase):
    """2026-11-01 她更新了探索地图：设置页上传 xlsx，旧的先备份；新的“成就”表代替“Goal”表。"""

    def test_upload_replaces_map_and_reads_new_achievements_sheet(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / explore.FILE).write_text(json.dumps({"sheets": {"Goal": []}}))
            data = tiny_xlsx({"欧洲": [["", "签证", "状态"], ["芬兰", "申根", "已结束"], ["芬兰", "申根", "进行中"]],
                              "成就": [["成就", "状态", "成就内容"], ["人生体验", "已结束", "三山五岳"], ["人生体验", "未开始", "拥抱南极企鹅"],
                                       ["网球", "未开始", "比赛排名越来越高"]]})
            got = explore.save(root, data)
            self.assertEqual(got, {"欧洲": 2, "成就": 3})
            self.assertTrue(list(root.glob(explore.FILE + ".bak-*")))  # 旧的备份了
            saved = json.loads((root / explore.FILE).read_text())["sheets"]
            self.assertEqual(saved["欧洲"][0], {"国家": "芬兰", "签证": "申根", "状态": "已结束"})  # 签证留着，她要在工作台里看和改
            st = territory.explore_stats(root)
            self.assertEqual((st["countries"], st["goals_done"], st["goals_total"]), (1, 1, 2))
            with self.assertRaises(ValueError):
                explore.save(root, b"not a zip")
            # 工作台里直接改状态（她 2026-11-01：去过、跳过随时会调）
            explore.set_status(root, "欧洲", 1, "已结束")
            self.assertEqual(territory.explore_stats(root)["countries"], 2)
            with self.assertRaises(ValueError):  # 没有“没标”这个状态（她 2026-11-01），没标的就是未开始
                explore.set_status(root, "欧洲", 1, "")
            # 签证内容能改；国内的表没有签证，也没有“签证困难”
            explore.set_field(root, "欧洲", 1, "签证", "申根 90 天")
            self.assertEqual(explore.load(root)["sheets"]["欧洲"][1]["签证"], "申根 90 天")
            with self.assertRaises(ValueError):
                explore.set_field(root, "中国", 0, "签证", "x")
            with self.assertRaises(ValueError):
                explore.set_field(root, "欧洲", 1, "状态", "x")
            self.assertNotIn("签证困难", explore.statuses("中国"))
            # 工作台是唯一数据源（她 2026-11-02）：能加、能删
            explore.add_row(root, "欧洲", "葡萄牙", "里斯本", visa="申根")
            self.assertEqual(explore.load(root)["sheets"]["欧洲"][-1], {"国家": "葡萄牙", "状态": "未开始", "省份/区域": "里斯本", "签证": "申根"})
            explore.add_row(root, "中国", "大理", "云南省")
            self.assertEqual(explore.load(root)["sheets"]["中国"][-1]["城市"], "大理")
            gone = explore.delete_row(root, "欧洲", len(explore.load(root)["sheets"]["欧洲"]) - 1)
            self.assertEqual(gone["国家"], "葡萄牙")
            self.assertIn("签证困难", explore.statuses("欧洲"))
            with self.assertRaises(ValueError):
                explore.set_status(root, "欧洲", 9, "已结束")
            with self.assertRaises(ValueError):
                explore.set_status(root, "欧洲", 0, "随便")


if __name__ == "__main__":
    unittest.main()



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
