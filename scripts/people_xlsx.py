"""人物卡整表的 Excel 这一头（在她 Mac 上跑，要 openpyxl；服务器那一头是 ethan/people_sheet.py）。

python3 scripts/people_xlsx.py out 卡.json 人物卡.xlsx    # 生成给她填的表：下拉选重要程度、关系圈、提醒间隔；隐藏页“原值”存导出时的值
python3 scripts/people_xlsx.py back 人物卡.xlsx 改.json   # 读她填好的表，只挑出改过的格子
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ethan.people_sheet import COLUMNS, STARS  # noqa: E402
from ethan.relations import CIRCLES  # noqa: E402

WIDTH = {"id": 8, "name": 16, "importance": 10, "circle": 9, "every": 12, "relation": 26, "how_met": 30, "recent": 36, "topics": 20,
         "birthday": 10, "notes": 26, "labels": 16, "last_contact": 11, "n365": 9, "source": 8}
EVERY = ["默认", "14", "30", "90", "180", "365", "永不"]


def out(src: Path, dst: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation
    rows = json.loads(src.read_text(encoding="utf-8"))
    wb = Workbook()
    ws = wb.active
    ws.title = "人物卡"
    ws.append([h for _, h, _ in COLUMNS])
    for r in rows:
        ws.append([r[k] for k, _, _ in COLUMNS])
    grey, head = PatternFill("solid", fgColor="EEEEEE"), PatternFill("solid", fgColor="DDE6F7")
    for i, (k, _, ok) in enumerate(COLUMNS, 1):
        col = get_column_letter(i)
        ws.column_dimensions[col].width = WIDTH.get(k, 14)
        ws[f"{col}1"].font, ws[f"{col}1"].fill = Font(bold=True), head
        if not ok:
            for c in ws[col][1:]:
                c.fill = grey
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=False)
    n = len(rows) + 1
    letter = {k: get_column_letter(i) for i, (k, _, _) in enumerate(COLUMNS, 1)}
    # 下拉的选项放在隐藏页里引用（直接写在公式里，逗号和中文在有的 Excel 里会出错）
    opts = wb.create_sheet("选项")
    for i, vals in enumerate((list(STARS.values()), list(CIRCLES), EVERY), 1):
        for j, v in enumerate(vals, 1):
            opts.cell(row=j, column=i, value=v)
    opts.sheet_state = "hidden"
    for k, col, size in (("importance", "A", len(STARS)), ("circle", "B", len(CIRCLES)), ("every", "C", len(EVERY))):
        dv = DataValidation(type="list", formula1=f"=选项!${col}$1:${col}${size}", allow_blank=True,
                            showErrorMessage=k != "every", errorTitle="从下拉里选", error="从下拉里选一个")
        ws.add_data_validation(dv)
        dv.add(f"{letter[k]}2:{letter[k]}{n}")
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{n}"
    orig = wb.create_sheet("原值")  # 导出时的值：读回时对比，只写她改过的格子
    orig.append([k for k, _, _ in COLUMNS])
    for r in rows:
        orig.append([r[k] for k, _, _ in COLUMNS])
    orig.sheet_state = "hidden"
    note = wb.create_sheet("怎么填", 0)
    for line in ("怎么填（填完存一下，发回给开发会话导回去）",
                 "",
                 "1. 重要程度、关系圈、多少天提醒一次：点格子，从下拉里选。",
                 "   重要程度：★★★ 默认 30 天提醒一次；★★☆ 默认 90 天；★☆☆ 和 ☆☆☆（没星）默认永不提醒。",
                 "   多少天提醒一次：选“默认”就按重要程度；也可以直接写天数，或者选“永不”。",
                 "   关系圈：家人、挚友、朋友、同学、同事、合作、老师、服务（中介、客服、导游这类）、其他。",
                 "2. 关系、怎么认识的、对方近况、话题、备注、生日：能写就写，写不了空着，以后跟 小伴 聊着补。",
                 "   这几项 小伴 以后整理会参考你写的，再按你们后来的聊天调整。",
                 "3. 只有你改过的格子会写回去，没动的不碰。灰色的几列是看的，不用改。",
                 "4. 想删一个人、合并两个人，还是在工作台卡片上做，表里不处理。",
                 "",
                 "表按近一年聊得多少排，前面的是常联系的；可以用表头的筛选。"):
        note.append([line])
    note.column_dimensions["A"].width = 100
    wb.active = 1
    wb.save(dst)
    print(f"写好了 {len(rows)} 行 → {dst}")


def back(src: Path, dst: Path):
    from openpyxl import load_workbook
    wb = load_workbook(src, data_only=True)
    keys = [k for k, _, _ in COLUMNS]
    norm = lambda v: "" if v is None else v.strftime("%m-%d") if hasattr(v, "strftime") else str(int(v)) if isinstance(v, float) and v.is_integer() else str(v).strip()

    def birthday(v):  # Excel 把“2/23”这类认成日期，存成日期或天数序号（46076）：换回 MM-DD
        from datetime import date, timedelta
        if hasattr(v, "strftime"):
            return v.strftime("%m-%d")
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit() and 20000 < int(v) < 80000):
            return (date(1899, 12, 30) + timedelta(days=int(float(v)))).strftime("%m-%d")
        return norm(v)
    orig = {}
    for r in wb["原值"].iter_rows(min_row=2, values_only=True):
        d = dict(zip(keys, r))
        if d.get("id") is not None:
            orig[int(d["id"])] = d
    changed = []
    for r in wb["人物卡"].iter_rows(min_row=2, values_only=True):
        d = dict(zip(keys, r))
        if d.get("id") is None:
            continue
        old = orig.get(int(d["id"]), {})
        for k, _, ok in COLUMNS:
            new = birthday(d.get(k)) if k == "birthday" else norm(d.get(k))
            if ok and new != norm(old.get(k)):
                changed.append({"id": int(d["id"]), "name": norm(old.get("name")) or norm(d.get("name")), "key": k,
                                "old": norm(old.get(k)), "new": new})
    dst.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
    print(f"改过的格子 {len(changed)} 个，涉及 {len({c['id'] for c in changed})} 人 → {dst}")


if __name__ == "__main__":
    {"out": out, "back": back}[sys.argv[1]](Path(sys.argv[2]), Path(sys.argv[3]))
