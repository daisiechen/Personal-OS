"""执行保障：模型只出“提议”，格式由代码校验（docs/design/digital-life-spec.md 第 0 节）。

她担心“方案聊得好、执行有偏差”。业界有效的做法是：模型只负责理解，按固定格式输出；代码校验格式、取值、
引用的原话是不是真在原文里；不合格带着错误重试一次，还不合格就丢弃并记下来。每次调用都留痕（llm_calls 表），
写着用的是哪个版本的提示词、哪个模型、输出了什么、过没过校验，出问题能查、能回放、能拿去做题集。

用法：
    data = structured(router, db, task="digest", system=..., user=..., schema=SCHEMA, check=extra_check)
    返回校验通过的 dict / list；两次都不合格返回 None。
schema 是 JSON Schema 的一个子集：type、properties、required、enum、maxLength、minimum、maximum、items、maxItems。
"""

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

PROMPTS = Path(__file__).resolve().parent / "prompts"
SCHEMA_SQL = """CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, task TEXT NOT NULL, prompt_version TEXT, model TEXT, backend TEXT,
    input_ref TEXT, output TEXT, valid INTEGER NOT NULL, errors TEXT, attempt INTEGER NOT NULL DEFAULT 1,
    latency_ms INTEGER, cost_usd REAL)"""
TYPES = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}


def ensure(db):
    db.conn.execute(SCHEMA_SQL)


def load_prompt(name: str) -> tuple:
    """ethan/prompts/<name>.md → (版本号, 正文)。文件第一行写“version: 3”。"""
    text = (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    first, _, rest = text.partition("\n")
    m = re.match(r"version:\s*(\S+)", first.strip())
    return (m.group(1), rest.strip()) if m else ("0", text.strip())


def validate(value, schema: dict, path: str = "$") -> list:
    """返回错误列表（空 = 通过）。"""
    errors = []
    kind = schema.get("type")
    if kind:
        kinds = kind if isinstance(kind, list) else [kind]
        ok = any((value is None) if k == "null" else isinstance(value, TYPES[k]) and not (k in ("integer", "number") and isinstance(value, bool))
                 for k in kinds)
        if not ok:
            return [f"{path} 应该是 {kind}，实际是 {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} 只能是 {schema['enum']}，实际是 {value!r}")
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        errors.append(f"{path} 太长（{len(value)} > {schema['maxLength']}）")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{path} 小于 {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{path} 大于 {schema['maximum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} 缺了")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                errors += validate(value[key], sub, f"{path}.{key}")
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{path} 太多（{len(value)} > {schema['maxItems']}）")
        if "items" in schema:
            for i, item in enumerate(value):
                errors += validate(item, schema["items"], f"{path}[{i}]")
    return errors


def extract_json(text: str):
    """模型回复里的第一个 JSON 对象或数组。"""
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    if not starts:
        raise ValueError("没有找到 JSON")
    start = min(starts)
    end = text.rfind("}" if text[start] == "{" else "]")
    if end < start:
        raise ValueError("JSON 不完整")
    return json.loads(text[start:end + 1])


def quote_in(quote: str, source: str) -> bool:
    """模型引用的原话真在原文里（忽略空白和标点差别），防编造。"""
    norm = lambda s: re.sub(r"[\s，。、,.!?！？：“”\"'()]+", "", s or "")
    q = norm(quote)
    return bool(q) and q in norm(source)


def log(db, task, version, reply, input_ref, output, valid, errors, attempt, started):
    ensure(db)
    db.conn.execute(
        "INSERT INTO llm_calls (at, task, prompt_version, model, backend, input_ref, output, valid, errors, attempt, latency_ms, cost_usd) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), task, version, getattr(reply, "model", None),
         getattr(reply, "backend", None), (input_ref or "")[:200], (output or "")[:20000], 1 if valid else 0,
         "; ".join(errors)[:2000] if errors else None, attempt, int((time.time() - started) * 1000),
         getattr(reply, "cost_usd", None)))
    db.conn.commit()


def structured(router, db, task: str, system: str, user: str, schema: dict, check=None, version: str = "0",
               input_ref: str = "", images=(), purpose: str = None):
    """按 schema 要一个结构化结果。check(data) 返回额外的错误列表（业务规则，比如引用的原话必须在原文里）。"""
    fmt = "\n\n只输出一个 JSON，符合这个格式（JSON Schema）：\n" + json.dumps(schema, ensure_ascii=False)
    prompt = user
    for attempt in (1, 2):
        started = time.time()
        reply = router.complete(system + fmt, [{"role": "user", "content": prompt}], purpose=purpose or task, images=images)
        try:
            data = extract_json(reply.text)
            errors = validate(data, schema) or list(check(data) if check else [])
        except ValueError as e:
            data, errors = None, [str(e)]
        if db is not None:
            log(db, task, version, reply, input_ref, reply.text, not errors, errors, attempt, started)
        if not errors:
            return data
        prompt = user + "\n\n（上一次的输出没通过检查：" + "；".join(errors[:5]) + "。请改正后重新输出。）"
    return None


def recent_failures(db, days: int = 7) -> list:
    """每周抽查：最近没过校验的调用（错题回流进题集用）。"""
    ensure(db)
    since = datetime.now(timezone.utc).timestamp() - days * 86400
    since_iso = datetime.fromtimestamp(since, timezone.utc).isoformat(timespec="seconds")
    return [dict(r) for r in db.conn.execute(
        "SELECT * FROM llm_calls WHERE valid = 0 AND at >= ? ORDER BY id DESC", (since_iso,))]
