"""探索地图（她 2026-11-02 定：以后工作台就是唯一数据源，不再回飞书）。最早是她在飞书维护的表格，11-01 导入了最后一版；
之后加地方、改状态、改签证、删地方都在工作台里做。xlsx 导入的代码留着，只在需要整表重导时用。

存成 <数据目录>/explore-map.json（私人资料，不进仓库）：{"sheets": {表名: [{列名: 值}]}}，领域页的探索地图、成就都读它。
只留用得上的列（地点、区域、项目、签证、状态、星标、成就），票价、备注这些长内容不存。上传前先把旧文件备份一份。
不依赖 openpyxl：xlsx 就是一个 zip，里面是 XML，这里只读单元格的值。
"""

import json
import re
import shutil
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET

FILE = "explore-map.json"
KEEP = {"国家", "省份", "城市", "省份/区域", "项目", "区域", "位置", "签证", "状态", "星标", "成就", "成就内容", "高度", "徒步距离（仅参考）"}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "p": "http://schemas.openxmlformats.org/package/2006/relationships"}
MAX_BYTES = 10 * 1024 * 1024


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def read_xlsx(data: bytes) -> dict:
    """{表名: [[单元格值…]…]}"""
    z = zipfile.ZipFile(BytesIO(data))
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", NS):
            shared.append("".join(t.text or "" for t in si.iter(f"{{{NS['m']}}}t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels")).findall("p:Relationship", NS)}
    out = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).find("m:sheets", NS).findall("m:sheet", NS):
        target = rels[s.get(f"{{{NS['r']}}}id")].lstrip("/")
        path = target if target.startswith("xl/") else "xl/" + target
        rows = []
        for row in ET.fromstring(z.read(path)).iter(f"{{{NS['m']}}}row"):
            vals = {}
            for c in row.findall("m:c", NS):
                t, v = c.get("t"), c.find("m:v", NS)
                if t == "s" and v is not None:
                    val = shared[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(f"{{{NS['m']}}}t"))
                else:
                    val = v.text if v is not None else None
                if val not in (None, ""):
                    vals[_col(c.get("r"))] = val
            if vals:
                rows.append([vals.get(i) for i in range(max(vals) + 1)])
        out[s.get("name")] = rows
    return out


def to_sheets(raw: dict) -> dict:
    sheets = {}
    for name, rows in raw.items():
        if not rows:
            continue
        head = [h or ("国家" if i == 0 else f"c{i}") for i, h in enumerate(rows[0])]
        sheets[name] = [{k: v for k, v in zip(head, r) if v not in (None, "") and k in KEEP} for r in rows[1:]]
        sheets[name] = [r for r in sheets[name] if r]
    return sheets


def save(root, data: bytes) -> dict:
    """上传的 xlsx 换掉旧的探索地图；返回每张表几行。"""
    if not data or len(data) > MAX_BYTES:
        raise ValueError("文件是空的或太大了")
    try:
        sheets = to_sheets(read_xlsx(data))
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ValueError(f"读不了这个文件，要飞书导出的 xlsx（{type(e).__name__}）")
    if not sheets:
        raise ValueError("表格里没有内容")
    path = Path(root) / FILE
    if path.exists():
        shutil.copy2(path, path.with_name(f"{FILE}.bak-{datetime.now():%Y%m%d-%H%M%S}"))
    path.write_text(json.dumps({"sheets": sheets, "uploaded_at": datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False),
                    encoding="utf-8")
    return {name: len(rows) for name, rows in sheets.items()}


STATUSES = ("已结束", "进行中", "未开始", "待二刷", "跳过", "签证困难")
DOMESTIC = ("城市", "中国")  # 国内的表没有签证这回事（她 2026-11-01：“城市和中国里面不需要签证困难的 tag”）
EDITABLE = ("签证",)  # 工作台里能直接改的文字列（她 2026-11-01：签证“内容我可改”）


def statuses(sheet: str) -> list:
    return [s for s in STATUSES if not (sheet in DOMESTIC and s == "签证困难")]


def load(root) -> dict:
    try:
        return json.loads((Path(root) / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"sheets": {}}


def _row(data: dict, sheet: str, i) -> dict:
    rows = data.get("sheets", {}).get(sheet)
    if rows is None or not 0 <= int(i) < len(rows):
        raise ValueError("找不到这一行")
    return rows[int(i)]


def _write(root, data: dict) -> None:
    data["edited_at"] = datetime.now().isoformat(timespec="seconds")
    (Path(root) / FILE).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def set_status(root, sheet: str, i: int, status: str) -> dict:
    """工作台里直接改一行的状态（她 2026-11-01：去过、跳过这些我随时会调，不能每次改完再上传）。没标的就当未开始。"""
    if status not in statuses(sheet):
        raise ValueError("状态只能是：" + "、".join(statuses(sheet)))
    data = load(root)
    row = _row(data, sheet, i)
    row["状态"] = status
    _write(root, data)
    return row


def set_field(root, sheet: str, i: int, field: str, value: str) -> dict:
    """改一行的文字列，现在只有签证（国内的表没有）。"""
    if field not in EDITABLE or sheet in DOMESTIC:
        raise ValueError("这一列不能在这里改")
    data = load(root)
    row = _row(data, sheet, i)
    value = str(value or "").strip()[:200]
    if value:
        row[field] = value
    else:
        row.pop(field, None)
    _write(root, data)
    return row


PRIMARY = {"城市": ("项目", None), "中国": ("城市", "省份")}  # 其他表：国家 + 省份/区域


def add_row(root, sheet: str, name: str, sub: str = "", status: str = "未开始", visa: str = "") -> dict:
    """工作台里加一处（她 2026-11-02：工作台是唯一数据源）。"""
    name = (name or "").strip()[:80]
    if not name:
        raise ValueError("写一下地名")
    if status not in statuses(sheet):
        raise ValueError("状态不对")
    data = load(root)
    rows = data.setdefault("sheets", {}).setdefault(sheet, [])
    key, sub_key = PRIMARY.get(sheet, ("国家", "省份/区域"))
    row = {key: name, "状态": status}
    if sub and sub_key:
        row[sub_key] = sub.strip()[:80]
    if visa and sheet not in DOMESTIC:
        row["签证"] = visa.strip()[:200]
    rows.append(row)
    _write(root, data)
    return row


def delete_row(root, sheet: str, i: int) -> dict:
    data = load(root)
    row = _row(data, sheet, i)
    data["sheets"][sheet].pop(int(i))
    _write(root, data)
    return row
