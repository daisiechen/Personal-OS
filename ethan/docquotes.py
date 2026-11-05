"""仓库文档里记下的她的原话，整理后交给 小伴 的记忆（ontology-plan 1.4，数据源 S12）。

只在开发会话里跑的一次性整理脚本：
- 扫 docs/**/*.md，找“她 X-X 说 / 她原话 / 她定”后面引号里的话；必须带日期或写明“原话”（没日期的多半是举例）。
- 用规则先粗筛：含“代码、页面、按钮、上线、系统……”的算对系统提的要求，不交（一次决定不是性格）；
  但话里明说她自己的习惯、喜好、过往的（“我懒得打字”“不像我的写作习惯”）保留。
- 算“文档转述”：每条写清出自哪份文档、哪天；去掉联系方式和住址样式。
- 每条一个指纹（原话的 sha1），交过的记在 ~/.config/ethan/docquotes-sent.json（不进仓库），以后只交新增的。

用法：
  python3 -m ethan.docquotes --dry-run [--json 候选.json]   打印候选和条数，不发
  python3 -m ethan.docquotes --send                         交给 小伴（要 ETHAN_CLOUD_TOKEN；没有就只生成文件）
  加 --wide 放宽收（她 11-04 说“放宽收一遍”）：
  - 没写日期也收：日期取所在小节标题里的日期（“## 11-03 …”也算），没有就写“未注明日期”；
  - 不带“她”字、但引号里是第一人称（含“我”）的也收：同一行或前一行有“她 / Mia”，或文档本身是记她需求的（HER_DOCS）；
    引号紧前面是 小伴 / agent / 系统在说的不算；
  - 仍然排除：对系统提的要求（SYSTEM_WORDS，话里明说她自己的照旧保留）、问系统的问句、不到 8 字的点头、
    举例（引号前是“比如 / 例如 / 像”）、提示词和测试数据文件（SKIP_DOCS）、代码块里的。
交出去的格式“问：（出处）\\n答：原话”，服务器那边按原话核对（ethan/interviews.py）。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SENT_FILE = Path.home() / ".config" / "ethan" / "docquotes-sent.json"
SESSION = "开发会话（仓库文档）"
SCENE = "仓库文档里记下的她的原话（文档转述）"
CHUNK = 50000  # 访谈接口一次最多 60000 字（ethan/interviews.py MAX_CHARS），留余量

# 她 + 至多 30 个字的修饰（日期、“跟 小伴”……）+ 说/原话/定/问… + 引号里的话；后面紧跟的引号段（“…”，“…”）算同一句
_DATE = r"(?<!\d)(?:20\d{2}-\d{1,2}-\d{1,2}|1[0-2]-\d{1,2}|0?[1-9]-\d{1,2}|\d{1,2}月\d{1,2}日)(?![\d-])"
QUOTE = re.compile(r"她(?P<pre>[^。“”\n]{0,30}?)(?P<verb>说|原话|定|问|提|讲|写|回|答|觉得|评价|[：:])(?P<mid>[^“”。\n]{0,8})"
                   r"“(?P<q>[^”\n]{2,600})”(?P<more>(?:[，、,和 ]{1,3}“[^”\n]{2,600}”)*)")
DATE = re.compile(_DATE)
HEADING_DATE = re.compile(r"^#{1,6}\s.*?(20\d{2}-\d{2}-\d{2})")
MORE = re.compile(r"“([^”\n]{2,600})”")
ANY_QUOTE = re.compile(r"“([^”\n]{2,600})”")
HEADING_ANY_DATE = re.compile(r"^#{1,6}\s.*?(" + _DATE + ")")

# --wide 用
HER_CTX = re.compile(r"她|Mia")  # 同一行或前一行提到她，才算她说的
HER_DOCS = ("docs/design/mias-os.md",)  # 整份记她需求的文档
SKIP_DOCS = ("docs/design/capability-questions.md",)  # 检验题、提示词、样例数据，不是她说的
EXAMPLE = re.compile(r"(?:(?:比如|例如|譬如|举例|例[：:])[^。；\n]{0,40}|像[^“”。；\n]{0,4})$")  # 引号前（或“她说”前）是举例
OTHER_SPEAKER = re.compile(r"(?:小伴|他|Grok|Codex|Claude|agent|Agent|系统|助手|模型|bot|Bot|提示词)[^“”。；她\n]{0,6}"
                           r"(?:说|回|答|问|写|讲|提示|会说|应该说|改成)[^“”。；\n]{0,4}[：:]?$")
LABEL_HEAD = re.compile(r"(?:进了|进|在|叫|叫做|名为|点|显示|按钮|回答|回复|答复|写着|标成|标)\s*$")  # 引号里是名字、按钮、系统的回答
EXAMPLE_TAIL = re.compile(r"^（[^）\d她]{1,6}）")  # “这件事暂停到下周”（暂停）：给例子贴的类型标签
SAMPLE_MARK = re.compile(r"示意|样例|示例|案例|编的")  # 标题或“（示意……）”“**样例 1**”这类行之后到下一个标题都是编的例子

# 对系统提的要求（粗筛排除）
SYSTEM_WORDS = ("代码", "页面", "按钮", "接口", "上线", "测试", "提示词", "会话", "小伴 要", "小伴要", "系统",
                "工作台", "合并", "部署", "agent", "Agent", "功能", "服务器", "仓库", "界面", "详情页", "ontology",
                "数据库", "迁移", "文档", "方案", "执行", "版本", "前端", "模块", "字段", "Codex", "Claude", "Grok",
                "第 1", "第1", "先做", "做吧", "接着做", "设置", "App", "提醒", "待办", "副本", "积分", "记忆", "跑",
                "做完", "全做", "做了", "改版", "计划器", "弹窗", "搜索", "我页", "大脑", "能力题", "语义库",
                "接入", "对账", "监控", "编排", "翻页", "这次", "授权", "统计", "review", "逻辑", "排进")
# 话里明说她自己的（习惯、喜好、过往），即使带上面的词也保留
SELF_WORDS = ("习惯", "喜欢", "讨厌", "性格", "以前", "小时候", "审美", "懒得", "拖到", "起不来", "我这个人", "我一般",
              "我从来", "我总是", "我经常", "我平时", "像我", "好看", "难受", "头疼")
MIN_CHARS = 8  # 太短的（“可以”“同意”“好”）多半是点头，不是关于她的话

# 联系方式和住址样式：交之前去掉
PRIVATE = [re.compile(p) for p in (r"[\w.+-]+@[\w-]+\.[\w.]+", r"(?<![\d-])\+?\d(?:[ \-]?\d){9,13}(?![\d-])",
                                   r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", r"\S{0,12}(?:路|街|弄|号楼|室|Road|Street)\s*\d+\S*")]


def _plain_len(s: str) -> int:
    return len(re.sub(r"[\s，。、！？…,.!?：:；;“”\"'（）()]", "", s))


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)


def fingerprint(quote: str) -> str:
    return hashlib.sha1(_norm(quote).encode("utf-8")).hexdigest()


def redact(s: str) -> str:
    for p in PRIVATE:
        s = p.sub("［已去掉］", s)
    return s


def is_system_request(quote: str) -> bool:
    """粗筛：对系统提的要求返回 True；话里明说她自己的习惯、喜好、过往的不算。"""
    if quote.rstrip("。！!…").endswith(("？", "?", "吗", "么")) or "她" in quote:
        return True  # 她问系统的问题不是关于她的说法；带“她”的是文档自己的转述，不是她的话
    if any(w in quote for w in SELF_WORDS):
        return False
    return "你" in quote or any(w in quote for w in SYSTEM_WORDS)  # 对 agent 说“你……”的多半是交代事


def _date(pre: str, mid: str, heading: str) -> str:
    m = DATE.search(pre + mid)
    if not m:
        return heading or "未注明日期"
    d = m.group(0)
    if "月" in d:
        mo, dd = re.findall(r"\d+", d)
        return f"2026-{int(mo):02d}-{int(dd):02d}"
    parts = d.split("-")
    if len(parts) == 2:  # 文档里的 11-03 都是 2026 年（项目 2026-10 开始）
        return f"2026-{int(parts[0]):02d}-{int(parts[1]):02d}"
    return f"{parts[0]}-{int(parts[1]):02d}-{int(parts[2]):02d}"


def _sentence(line: str, start: int, end: int) -> str:
    """原话所在的那一句（按句号切），太长截到 160 字。"""
    left = max(line.rfind("。", 0, start), line.rfind("；", 0, start)) + 1
    right = line.find("。", end)
    s = line[left:right + 1 if right >= 0 else len(line)].strip(" -*>|")
    return s if len(s) <= 160 else s[:157] + "…"


def _heading_date(line: str, wide: bool):
    if not wide:
        h = HEADING_DATE.match(line)
        return h.group(1) if h else None
    h = HEADING_ANY_DATE.match(line)
    return _date(h.group(1), "", "") if h else None


def scan_text(text: str, path: str, wide: bool = False) -> list:
    """一份文档里的全部候选（还没粗筛）。wide=True 按 --wide 放宽。"""
    out, heading, prev, fenced, sample = [], "", "", False, False
    if wide and path in SKIP_DOCS:
        return out
    for n, line in enumerate(text.splitlines(), 1):
        if wide:
            st = line.strip()
            if st.startswith("```"):
                fenced = not fenced
            if st.startswith("#"):
                sample = bool(SAMPLE_MARK.search(st))
            elif st.startswith(("（", "(", "**")) and SAMPLE_MARK.search(st[:12]):
                sample = True
            if fenced or sample or st.startswith("```"):
                prev = line
                continue
        hd = _heading_date(line, wide)
        if hd:
            heading = hd
        taken = []
        for m in QUOTE.finditer(line):
            pre, mid = m.group("pre"), m.group("mid")
            before = line[max(0, m.start() - 20):m.start()]  # “（2026-10-29 她说‘…’”：日期写在“她”前面
            before = before[before.rfind("。") + 1:]
            if wide:
                if (EXAMPLE.search(line[:m.start()]) or EXAMPLE.search(line[:m.start("q") - 1]) or mid.startswith("的")
                        or EXAMPLE_TAIL.match(line[m.end():]) or line[m.end():].startswith("的")
                        or OTHER_SPEAKER.search(line[:m.start("q") - 1]) or line[:m.start("q") - 1].endswith(("（", "("))):
                    continue  # “比如她说‘又错了’”：举例；“她说的‘想去、想试’”：说的是一类话
            elif not (DATE.search(pre + mid) or DATE.search(before) or "原话" in pre + m.group("verb") + mid):
                continue  # 没日期也没写“原话”的，多半是举例（“比如她说‘又错了’”）
            parts = [m.group("q")] + MORE.findall(m.group("more") or "")
            quote = "……".join(p.strip() for p in parts)
            taken.append((m.start("q") - 1, m.end()))
            out.append({"path": path, "line": n, "date": _date(before + pre, mid, heading), "quote": quote,
                        "context": _sentence(line, m.start(), m.end())})
        if wide and not line.lstrip().startswith("|") and (HER_CTX.search(line) or HER_CTX.search(prev) or path in HER_DOCS):
            for m in ANY_QUOTE.finditer(line):
                q = m.group(1)
                if "我" not in q or any(a <= m.start() < b for a, b in taken):
                    continue
                head = line[:m.start()]
                if (EXAMPLE.search(head) or OTHER_SPEAKER.search(head) or LABEL_HEAD.search(head)
                        or head.endswith(("（", "(")) or EXAMPLE_TAIL.match(line[m.end():]) or "/" in q
                        or line[m.end():].startswith("的")):
                    continue  # 举例、小伴 / agent / 系统说的、名字和按钮、括号里的例子、“一个‘…’的项目”这种修饰语
                before = head[-30:]
                before = before[before.rfind("。") + 1:]
                out.append({"path": path, "line": n, "date": _date(before, "", heading), "quote": q.strip(),
                            "context": _sentence(line, m.start(), m.end())})
        prev = line
    return out


def scan(root: Path = ROOT, wide: bool = False) -> list:
    rows = []
    for md in sorted((root / "docs").rglob("*.md")):
        rows += scan_text(md.read_text(encoding="utf-8"), str(md.relative_to(root)), wide)
    return rows


def candidates(rows: list) -> list:
    """粗筛 + 去重：同一句话在几份文档里都有的，留第一处，其他出处记在 also。"""
    seen, out = {}, []
    for r in rows:
        q = r["quote"]
        if _plain_len(q) < MIN_CHARS or is_system_request(q):
            continue
        fp = fingerprint(q)
        if fp in seen:
            seen[fp].setdefault("also", []).append(f"{r['path']}:{r['line']}")
            continue
        item = dict(r, quote=redact(q), context=redact(r["context"]), fp=fp)
        seen[fp] = item
        out.append(item)
    # 一条是另一条的节选（changelog 常只引半句）：留长的，节选的出处记进 also
    keep = []
    for i in out:
        longer = next((j for j in out if j is not i and _norm(i["quote"]) in _norm(j["quote"])
                       and len(_norm(j["quote"])) > len(_norm(i["quote"]))), None)
        if longer:
            longer.setdefault("also", []).append(f"{i['path']}:{i['line']}")
        else:
            keep.append(i)
    return keep


def load_sent(path: Path = SENT_FILE) -> set:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")).get("sent", []))
    except (OSError, ValueError):
        return set()


def save_sent(fps: set, path: Path = SENT_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sent": sorted(fps)}, ensure_ascii=False, indent=1), encoding="utf-8")


def fresh(items: list, sent: set) -> list:
    return [i for i in items if i["fp"] not in sent]


def handoff_text(items: list) -> str:
    return "\n\n".join(f"问：（{i['path']}，{i['date']}，文档转述）\n答：{i['quote']}" for i in items)


def chunks(items: list, size: int = CHUNK) -> list:
    out, cur, n = [], [], 0
    for i in items:
        block = len(handoff_text([i])) + 2
        if cur and n + block > size:
            out.append(cur)
            cur, n = [], 0
        cur.append(i)
        n += block
    return out + ([cur] if cur else [])


def send(items: list, env=os.environ, sent_path: Path = SENT_FILE, out_dir: Path = None, client=None) -> dict:
    """按 docs/operations/interview-handoff.md 交；没有 ETHAN_CLOUD_TOKEN 就只生成文件、不发。"""
    out_dir = Path(out_dir or tempfile.gettempdir())
    files = []
    for k, part in enumerate(chunks(items), 1):
        f = out_dir / f"docquotes-handoff-{k}.md"
        f.write_text(handoff_text(part), encoding="utf-8")
        files.append((f, part))
    token = env.get("ETHAN_CLOUD_TOKEN", "").strip()
    if not token:
        return {"sent": 0, "files": [str(f) for f, _ in files],
                "note": "缺少 ETHAN_CLOUD_TOKEN，只生成了文件、没有发；有钥匙后用 "
                        f"python3 -m ethan.cloud interview --session \"{SESSION}\" --scene \"{SCENE}\" --file <文件> 交"}
    if client is None:
        from ethan.cloud import CloudClient, DEFAULT_URL
        client = CloudClient(token, env.get("ETHAN_URL", DEFAULT_URL))
    done, sent, errors = 0, load_sent(sent_path), []
    for f, part in files:
        res = client.interview(SESSION, SCENE, f.read_text(encoding="utf-8"))
        if res.get("ok"):
            sent |= {i["fp"] for i in part}
            save_sent(sent, sent_path)  # 交成功一批记一批，中途失败不会把没交的记成交过
            done += len(part)
            f.unlink()  # 临时文件用完删掉
        else:
            errors.append(f"{f}: {res.get('error', res)}")
    return {"sent": done, "files": [str(f) for f, _ in files if f.exists()], "errors": errors}


def by_doc(items: list) -> dict:
    out = {}
    for i in items:
        out[i["path"]] = out.get(i["path"], 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def main(argv=None, env=os.environ) -> int:
    p = argparse.ArgumentParser(description="仓库文档里她的原话交给 小伴 的记忆（ontology-plan 1.4）")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true", help="只打印候选和条数")
    g.add_argument("--send", action="store_true", help="交给 小伴（要 ETHAN_CLOUD_TOKEN）")
    p.add_argument("--json", type=Path, help="候选清单写到这个 JSON 文件（别放进仓库）")
    p.add_argument("--sent-file", type=Path, default=SENT_FILE)
    p.add_argument("--root", type=Path, default=ROOT)
    p.add_argument("--wide", action="store_true", help="放宽收：没日期的、不带“她”字的第一人称原话也收")
    args = p.parse_args(argv)
    rows = scan(args.root, args.wide)
    items = fresh(candidates(rows), load_sent(args.sent_file))
    if args.json:
        args.json.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.dry_run:
        for i in items:
            print(f"[{i['path']}:{i['line']} {i['date']}] {i['quote']}")
        print(f"\n扫到 {len(rows)} 处引用，粗筛后新增候选 {len(items)} 条")
        for doc, n in by_doc(items).items():
            print(f"  {doc}: {n}")
        return 0
    if not items:
        print("没有新增的候选，不用交")
        return 0
    res = send(items, env, args.sent_file)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if not res.get("errors") else 1


if __name__ == "__main__":
    sys.exit(main())
