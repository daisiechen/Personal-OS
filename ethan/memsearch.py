"""查记忆 v2（docs/design/digital-life-spec.md 第 5 节，2026-10-30 她认可）。

以前 小伴 每轮只拿她这句话去第二大脑里按两字片段打分，关于她的事实、经历、文件全文、流水、以前的聊天都查不到。
现在所有记忆进一个索引（mem_docs + FTS5 全文索引），按场景查：
1. 索引：事实（只要仍成立的）、事件、第二大脑条目（含文件全文）、证据（她说过的话、流水、Grok 回报等）。
   中文切成两字片段、英文按词，存进 FTS5（不装分词库，服务器零依赖）；每轮查之前增量更新。
2. 查：每个查询词各取前 30，多个查询用 RRF 融合（k = 60）。只用有区分度的片段（库里有、但不超过 2% 的记忆都有），免得“哈哈”“我觉得”把不相干的都拉进来。
3. 过滤：权限级别不超过场景上限；作废、被取代的事实不出；有时间范围按时间筛。
4. 加权：融合分 × 新近度（半衰期 90 天，最低保留一半；事实不衰减）× 重要度（1–5 → 0.8–1.2）。
5. 截取：前 10 条，每条最多 400 字；长文件取最相关的一段。
6. 补查：小伴 记不清时回 [查 关键词 …]，代码查完再让他答，最多 2 轮（见 core/agent.py）。
7. 留痕：每次查询写 retrieval_log。
向量检索（意思相近但字面不同）等服务器条件确认后再加，作为第二路接进同一个 RRF。

命令行：python3 -m ethan.memsearch --data <数据目录> 查询词 [查询词 …]
"""

import argparse
import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timezone
from pathlib import Path

from . import lifebase
from .clock import her_today

SCHEMA = """
CREATE TABLE IF NOT EXISTS mem_docs (
    key TEXT PRIMARY KEY, kind TEXT NOT NULL, label TEXT NOT NULL, title TEXT, body TEXT NOT NULL, at TEXT,
    visibility TEXT NOT NULL DEFAULT 'private', importance INTEGER NOT NULL DEFAULT 3, decay INTEGER NOT NULL DEFAULT 1,
    valid INTEGER NOT NULL DEFAULT 1, hash TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS mem_fts USING fts5(key UNINDEXED, toks);
CREATE TABLE IF NOT EXISTS mem_state (source TEXT PRIMARY KEY, last_id INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS retrieval_log (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, scene TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 1, queries TEXT NOT NULL,
    keys TEXT NOT NULL, latency_ms INTEGER);
"""
LOG_COLUMNS = (("user", "TEXT"), ("via", "TEXT"))  # via：哪道能力题的函数在查（空 = 兜底的自由检索，ontology-plan 3.1）
# 知识类型和读取名单（ontology.md 6.1 节）：检索时按使用者把关
DOC_COLUMNS = (("know", "TEXT"), ("read_by", "TEXT"), ("dim", "TEXT"))  # 谁在查（ethan/memusers.py 登记表），按使用者算正确率
STOP = set("我们你们他们这个那个什么怎么一下一个今天明天就是还是可以不是没有现在然后因为所以但是如果已经的了吗呢吧啊呀嘛哦"
           "记得以前上次之前多少几次时候有没")
# 常见的换说法（模型出的 query_plan 以后会补更多）
SYNONYMS = {"花了": ["消费", "支出"], "钱": ["消费", "支出", "预算"], "睡": ["作息", "睡觉"], "吃": ["饮食", "餐厅"],
            "运动": ["健身", "跑步", "训练"], "医生": ["看病", "医院"], "过敏": ["忌口"], "妈": ["妈妈", "母亲"],
            "爸": ["爸爸", "父亲"], "旅行": ["旅游", "出行"], "去过": ["旅行", "去了"],
            "编程": ["python", "代码"], "技能": ["会用", "能力"],
            # 11-04 她问“读过哪些学校”查不到简历里的“大学、本科、进修”：先补同义说法，语义检索上了再去掉这类手工表
            "学校": ["大学", "学院", "本科", "硕士", "博士", "进修"], "读过": ["本科", "毕业", "就读"], "读书": ["大学", "本科", "就读"],
            "工作": ["任职", "公司", "产品经理"], "上班": ["任职", "公司"], "做过": ["任职", "负责", "项目"]}
SCENES = {"chat": ("private", 10), "os": ("internal", 15), "public": ("public", 5), "work": ("work", 10)}
SNIPPET = 400
COMMON = 0.02  # 超过 2% 的记忆里都有的片段，不拿来判断相关
K = 60
# 问她本人的事（11-04 服务器实测：语义检索打开后搜“她读过哪些学校”，前 3 条都是 Grok 转述的进修计划学校清单，简历整理出的说法没进前 3）：
# 关于她的说法（fact:*）加权，agent 转述（Grok 回报、Grok 情报、agent 回报、agent_relay 的说法）降权；资料、聊天原文不动。
# RRF 名次分差很小（1/61 和 1/70），转述的名单又能命中多个同义词，所以倍数要拉开到 5 倍才稳。
ABOUT_HER = ("她", "我", "mia", "经历", "偏好", "习惯", "喜欢", "讨厌", "爱好", "性格", "背景", "履历", "简历",
             "读过", "做过", "去过", "学过", "住过", "待过")
SELF_BOOST, RELAY_DAMP = 2.0, 0.4


def ensure(db):
    db.conn.executescript(SCHEMA)
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(retrieval_log)")}
    for col, ddl in LOG_COLUMNS:
        if col not in have:
            db.conn.execute(f"ALTER TABLE retrieval_log ADD COLUMN {col} {ddl}")
    have = {r[1] for r in db.conn.execute("PRAGMA table_info(mem_docs)")}
    for col, ddl in DOC_COLUMNS:
        if col not in have:
            db.conn.execute(f"ALTER TABLE mem_docs ADD COLUMN {col} {ddl}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def tokens(text: str, query: bool = False) -> list:
    """中文两字片段 + 英文和数字的词（小写）。查询时去掉常用虚词片段。"""
    out = []
    for run in re.findall(r"[一-鿿]+|[a-z0-9]+", (text or "").lower()):
        if run[0].isascii():
            if len(run) >= 2:
                out.append(run)
            continue
        grams = [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
        out += [g for g in grams if not (query and (g in STOP or all(c in STOP for c in g)))]
    return out


def about_her(queries) -> bool:
    """问句是不是在问她本人（含“她”“我”“Mia”，或问经历、偏好、习惯这类）。"""
    return any(w in (q or "").lower() for q in queries for w in ABOUT_HER)


def _who_said(db, keys) -> tuple:
    """在候选里分出：她的说法（关于她本人、不是 agent 转述的 fact:*）和 agent 转述（evidence 来源 grok / agent、Grok 情报、agent_relay 的说法）。"""
    ids = {p: [int(k.split(":")[1]) for k in keys if k.startswith(p + ":")] for p in ("fact", "ev", "brain")}
    mine, relay = set(), set()

    def rows(sql, nums):
        return db.conn.execute(sql.format(",".join("?" * len(nums))), nums).fetchall() if nums else []
    for r in rows("SELECT * FROM facts WHERE id IN ({})", ids["fact"]):
        sk = r["source_kind"] if "source_kind" in r.keys() else None
        if sk == "agent_relay":
            relay.add(f"fact:{r['id']}")
        elif r["subject"] == "self":
            mine.add(f"fact:{r['id']}")
    relay |= {f"ev:{r['id']}" for r in rows("SELECT id FROM evidence WHERE source IN ('grok', 'agent') AND id IN ({})", ids["ev"])}
    if ids["brain"] and _table(db, "brain_items"):
        relay |= {f"brain:{r['id']}" for r in rows("SELECT id FROM brain_items WHERE kind = 'grok' AND id IN ({})", ids["brain"])}
    return mine, relay


def expand(query: str) -> list:
    extra = [s for word, syns in SYNONYMS.items() if word in query for s in syns]
    return [query] + extra


# ---------- 索引 ----------
def _put(db, key, kind, label, title, body, at, visibility="private", importance=3, decay=True, valid=True, sig=None,
         know=None, read_by=None, dim=None):
    body = (body or "").strip()
    h = sig or hashlib.sha1(json.dumps([title, body, at, visibility, importance, valid] + ([know, read_by, dim] if know or read_by or dim else []),
                                       ensure_ascii=False).encode()).hexdigest()[:16]
    row = db.conn.execute("SELECT hash FROM mem_docs WHERE key = ?", (key,)).fetchone()
    if row and row["hash"] == h:
        return False
    db.conn.execute("INSERT OR REPLACE INTO mem_docs (key, kind, label, title, body, at, visibility, importance, decay, valid, hash, know, read_by, dim) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (key, kind, label, title, body, at, visibility, int(importance or 3), 1 if decay else 0, 1 if valid else 0, h,
                     know, read_by or None, dim))
    db.conn.execute("DELETE FROM mem_fts WHERE key = ?", (key,))
    db.conn.execute("INSERT INTO mem_fts (key, toks) VALUES (?, ?)", (key, " ".join(tokens(f"{title or ''} {body}"))))
    return True


# ---------- 语义（向量）检索：按意思找（她 11-04：“向量语义库真的很需要！！！”，提前做，选 A：服务器上跑小模型） ----------
# 私密内容不出服务器（ontology.md 10.3 节）；模型 BAAI/bge-small-zh-v1.5（中文为主，512 维），用 fastembed（onnx，不要 torch）。
# 默认关着：服务器上装好、实测内存和速度以后，用 python3 -m ethan.memsearch --data … --semantic on 打开（会把已有记忆都算一遍）。
# 向量和原文同等对待：权限照旧在 search 里按 mem_docs 把关；删除时一起删（lifebase.forget）。
VEC_SCHEMA = "CREATE TABLE IF NOT EXISTS mem_vec (key TEXT PRIMARY KEY, hash TEXT NOT NULL, vec BLOB NOT NULL)"
SEM_MODEL = "BAAI/bge-small-zh-v1.5"
SEM_TOP, SEM_MIN = 30, 0.45  # 每次最多取多少条、相似度至少多少才算相关（首版暂定，服务器实测后调）
# 压内存（11-04 服务器实测默认设置峰值 412 MB，超了 350 MB 的线）：单线程、小批、onnx 不预分配内存池；模型放数据目录，重启不丢
SEM_BATCH, SEM_PER_REFRESH, SEM_THREADS = 8, 400, 1  # 平时每次刷新最多补算多少条（不拖慢聊天）；打开时一次算完
_EMBEDDER = None  # 测试里换成假的


def _model_dir(db):
    try:
        path = db.conn.execute("PRAGMA database_list").fetchone()[2]
    except Exception:
        path = ""
    return str(Path(path).parent / "models" / "fastembed") if path else None


class _FastEmbed:
    def __init__(self, cache_dir=None):
        from fastembed import TextEmbedding  # 没装就抛 ImportError，语义检索当没开
        kw = {"threads": SEM_THREADS}
        if cache_dir:
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            kw["cache_dir"] = cache_dir
        try:
            self.model = TextEmbedding(SEM_MODEL, **kw)
        except TypeError:  # 老版本不认这些参数
            self.model = TextEmbedding(SEM_MODEL)

    def embed(self, texts: list) -> list:
        return [list(map(float, v)) for v in self.model.embed(texts, batch_size=SEM_BATCH)]


def measure(db, n: int = 200) -> dict:
    """打开前的实测（ontology-plan 1.2.5）：加载模型、算 n 句编出来的话，报峰值内存（MB）和用时。不读不写记忆。"""
    import resource
    import time
    t0 = time.time()
    emb = _FastEmbed(_model_dir(db))
    t1 = time.time()
    texts = [f"第 {i} 句：她周{i % 7 + 1}早上去游泳，之后在咖啡店写方案。" for i in range(n)]
    vecs = emb.embed(texts)
    t2 = time.time()
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux 上单位是 KB
    return {"load_s": round(t1 - t0, 1), "embed_s": round(t2 - t1, 1), "sentences": n, "dim": len(vecs[0]) if vecs else 0,
            "peak_mb": round(peak), "threads": SEM_THREADS, "batch": SEM_BATCH, "ok": peak <= 350}


def semantic_on(db) -> bool:
    try:
        r = db.conn.execute("SELECT last_id FROM mem_state WHERE source = 'semantic'").fetchone()
    except Exception:
        return False
    return bool(r and r[0])


_LOADING = None  # 后台加载模型的线程（聊天时不等它，先用关键词答）


def _embedder(db, block: bool = True):
    """block=False（聊天、工作台）：模型还没加载就在后台开始加载，这次先返回 None，关键词照常查；
    11-04 打开后 小伴 回消息卡住，就是聊天时现场加载模型、补算几百条向量。命令行和每晚整理用 block=True。"""
    global _EMBEDDER, _LOADING
    if not semantic_on(db):
        return None
    if _EMBEDDER is not None:
        return _EMBEDDER
    if not block:
        if _LOADING is None:
            import threading
            cache = _model_dir(db)

            def load():
                global _EMBEDDER
                try:
                    _EMBEDDER = _FastEmbed(cache)
                except Exception as e:
                    print(f"[memsearch] 语义模型没加载上：{type(e).__name__} {str(e)[:200]}", flush=True)
            _LOADING = threading.Thread(target=load, daemon=True)
            _LOADING.start()
        return None
    try:
        _EMBEDDER = _FastEmbed(_model_dir(db))
    except Exception:
        return None
    return _EMBEDDER


def _pack(vec) -> bytes:
    import array
    return array.array("f", vec).tobytes()


def _unpack(blob) -> list:
    import array
    a = array.array("f")
    a.frombytes(blob)
    return a.tolist()


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def index_vectors(db, limit: int = SEM_PER_REFRESH, block: bool = True) -> int:
    """给还没有向量、或内容改过的记忆补算向量（只算有效的）。返回算了几条。"""
    emb = _embedder(db, block)
    if emb is None:
        return 0
    db.conn.execute(VEC_SCHEMA)
    rows = db.conn.execute("SELECT d.key, d.hash, d.title, d.body FROM mem_docs d LEFT JOIN mem_vec v ON v.key = d.key "
                           "WHERE d.valid = 1 AND (v.key IS NULL OR v.hash != d.hash) LIMIT ?", (limit,)).fetchall()
    done = 0
    for i in range(0, len(rows), SEM_BATCH):
        part = rows[i:i + SEM_BATCH]
        vecs = emb.embed([f"{r['title'] or ''} {r['body']}"[:800] for r in part])
        for r, v in zip(part, vecs):
            db.conn.execute("INSERT OR REPLACE INTO mem_vec (key, hash, vec) VALUES (?, ?, ?)", (r["key"], r["hash"], _pack(_norm(v))))
            done += 1
    db.conn.execute("DELETE FROM mem_vec WHERE key NOT IN (SELECT key FROM mem_docs WHERE valid = 1)")  # 失效、删掉的不留向量
    db.conn.commit()
    return done


SEM_TIMEOUT = 3.0  # 聊天时算一句话的向量最多等几秒；超时这个进程里就不再用语义检索（关键词照常），每晚整理不受影响
_BROKEN = False


def _embed_timeout(emb, text: str):
    """11-04：Telegram 服务里模型加载后卡死，小伴 不回消息。查询放到单独线程里算，超时就放弃，不拖住聊天。"""
    global _BROKEN
    if _BROKEN:
        return None
    import threading
    box = {}

    def run():
        try:
            box["v"] = emb.embed([text])[0]
        except Exception as e:
            box["e"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(SEM_TIMEOUT)
    if t.is_alive() or "e" in box:
        _BROKEN = True
        print(f"[memsearch] 语义向量算不出来（{'超时' if t.is_alive() else type(box['e']).__name__}），这个进程先只用关键词", flush=True)
        return None
    return box["v"]


def similar_keys(db, query: str, top: int = SEM_TOP, min_sim: float = SEM_MIN, prefix: str = None) -> list:
    """按意思最接近 query 的记忆：[(key, 相似度)]，从高到低。语义检索没开或没装时返回空。只给“提议”，权限由调用方照旧把关。
    模型还没加载好（刚启动）时也返回空，不等。"""
    emb = _embedder(db, block=False)
    if emb is None or not (query or "").strip():
        return []
    try:
        rows = db.conn.execute("SELECT key, vec FROM mem_vec" + (" WHERE key LIKE ?" if prefix else ""),
                               ((prefix + "%",) if prefix else ())).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    vec = _embed_timeout(emb, query)
    if vec is None:
        return []
    q = _norm(vec)
    try:
        import numpy as np
        mat = np.frombuffer(b"".join(r["vec"] for r in rows), dtype=np.float32).reshape(len(rows), -1)
        sims = (mat @ np.asarray(q, dtype=np.float32)).tolist()
    except Exception:
        sims = [sum(a * b for a, b in zip(_unpack(r["vec"]), q)) for r in rows]
    ranked = sorted(((r["key"], s) for r, s in zip(rows, sims) if s >= min_sim), key=lambda x: -x[1])
    return ranked[:top]


def set_semantic(db, on: bool) -> dict:
    """打开 / 关掉语义检索。打开时把已有记忆的向量一次算完。"""
    ensure(db)
    db.conn.execute("INSERT OR REPLACE INTO mem_state (source, last_id) VALUES ('semantic', ?)", (1 if on else 0,))
    db.conn.commit()
    if not on:
        return {"semantic": False}
    if _embedder(db) is None:
        db.conn.execute("INSERT OR REPLACE INTO mem_state (source, last_id) VALUES ('semantic', 0)")
        db.conn.commit()
        return {"semantic": False, "error": "fastembed 没装好或模型下载不了，先 pip install --user fastembed"}
    n, started = 0, time.time()
    while True:
        k = index_vectors(db, limit=SEM_BATCH * 8)
        n += k
        if not k:
            break
    return {"semantic": True, "indexed": n, "seconds": round(time.time() - started, 1)}


def _table(db, name) -> bool:
    return bool(db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (name,)).fetchone())


def refresh(db, root=None) -> int:
    """把新增和改过的记忆放进索引，返回更新了几条。事实、事件、第二大脑每次全扫（量小、会改）；证据只追加，按编号增量。"""
    ensure(db)
    lifebase.ensure(db)
    n = 0
    for r in db.conn.execute("SELECT * FROM facts"):
        if r["status"] == "deleted":  # 她要求删掉的：不进索引（lifebase.forget 已经删了索引里的副本）
            continue
        valid = r["valid_to"] is None and r["status"] not in ("invalid", "suspended")  # 停用的（删除后剩下的依据待核）不给
        n += _put(db, f"fact:{r['id']}", "fact", "关于她：" + lifebase.DIM_NAMES.get(r["dimension"], r["dimension"]),
                  None, r["statement"] + (f"（{r['detail']}）" if r["detail"] else ""), r["valid_from"] or r["recorded_at"],
                  r["visibility"], r["importance"], decay=False, valid=valid,
                  know=r["kind"] if "kind" in r.keys() else None, read_by=r["read_by"] if "read_by" in r.keys() else None, dim=r["dimension"])
    for r in db.conn.execute("SELECT * FROM events"):
        people = "、".join(json.loads(r["people"] or "[]"))
        n += _put(db, f"event:{r['id']}", "event", "她经历的事", r["title"],
                  " ".join(x for x in (r["summary"], f"和{people}" if people else "") if x), r["occurred_at"],
                  r["visibility"], r["importance"])
    if _table(db, "brain_items"):
        from .brain import KINDS
        seen = {r["key"]: r["hash"] for r in db.conn.execute("SELECT key, hash FROM mem_docs WHERE kind = 'brain'")}
        for r in db.conn.execute("SELECT * FROM brain_items"):
            sig = hashlib.sha1(f"{r['updated_at']}|{r['status']}|{r['title']}".encode()).hexdigest()[:16]
            if seen.get(f"brain:{r['id']}") == sig:  # 没改过就不重读（文件全文可能很大）
                continue
            extra = json.loads(r["extra"] or "{}")
            dup = r["ref"] == "wiki:about-mia" or r["ref"].startswith("wiki:about/")  # 这几页已经拆成事实和画像
            body = " ".join(x for x in (r["summary"], r["note"], extra.get("ai_summary"), " ".join(json.loads(r["tags"] or "[]")),
                                        (extra.get("place") or {}).get("name"), (extra.get("place") or {}).get("area")) if x)
            if root and extra.get("text_path") and (Path(root) / extra["text_path"]).is_file():  # 文件进全文
                body += "\n" + (Path(root) / extra["text_path"]).read_text(encoding="utf-8")
            n += _put(db, f"brain:{r['id']}", "brain", KINDS.get(r["kind"], r["kind"]), r["title"], body,
                      r["happened_at"] or r["created_at"], "internal" if r["kind"] in ("link", "saved", "knowledge") else "private",
                      4 if r["kind"] == "knowledge" else 3, valid=r["status"] in ("ask", "new", "kept", "archived") and not dup, sig=sig)
    for r in db.conn.execute("SELECT * FROM disclosures"):  # 她逐条批准的对外表达：只给原句，检索时再查一次有没有撤回、过期
        deny = "mode" in r.keys() and r["mode"] == "deny"  # 挡住的那句不是能说的话，不进索引
        n += _put(db, f"disc:{r['id']}", "disclosure", "她同意对外说", None, r["text"], r["created_at"], "public", 3,
                  decay=False, valid=not r["revoked_at"] and not deny, know="disclosure", read_by=r["to_users"], dim=r["dimension"])
    row = db.conn.execute("SELECT last_id FROM mem_state WHERE source = 'evidence'").fetchone()
    last = row["last_id"] if row else 0
    labels = {"chat": "她说过", "correction": "她纠正过", "bank": "流水", "grok": "Grok 回报", "agent": "回报", "email": "邮件", "health": "健康",
              "wechat": "微信", "recording": "录音", "visitor": "访客留言", "social": "社媒", "computer": "电脑"}
    for r in db.conn.execute("SELECT * FROM evidence WHERE id > ? ORDER BY id", (last,)).fetchall():
        last = r["id"]
        if "deleted_at" in r.keys() and r["deleted_at"]:
            continue
        text = r["text"] or ""
        if r["source"] == "file" or r["source_ref"].startswith("wiki:"):  # 文件由第二大脑那份代表；每晚整理的页已经拆成事实
            continue
        if r["source"] == "chat":
            if not text.startswith("mia："):  # 只收她说的话
                continue
            text = text[len("mia："):]
        n += _put(db, f"ev:{r['id']}", "evidence", labels.get(r["source"], r["source"]), None, text, r["occurred_at"],
                  r["visibility"], 2)
    db.conn.execute("INSERT OR REPLACE INTO mem_state (source, last_id) VALUES ('evidence', ?)", (last,))
    db.conn.commit()
    # 聊天时不补算向量（11-04 卡住过）；新记忆的向量由每晚整理补齐（ethan/nightly.py），白天先靠关键词找到
    return n


# ---------- 查 ----------
def _days(at: str, today: date) -> float:
    try:
        return max(0, (today - date.fromisoformat((at or "")[:10])).days)
    except ValueError:
        return 0


def snippet(body: str, qtoks: set, size: int = SNIPPET) -> str:
    """长文取命中最多的一段。"""
    if len(body) <= size:
        return body
    best, best_hits = 0, -1
    for start in range(0, len(body) - size // 2, size // 2):
        hits = sum(1 for t in qtoks if t in body[start:start + size].lower())
        if hits > best_hits:
            best, best_hits = start, hits
    return ("…" if best else "") + body[best:best + size] + "…"


def search(db, queries, scene: str = "chat", limit: int = None, max_level: str = None, kinds=None,
           time_from: str = None, time_to: str = None, round_no: int = 1, today: date = None, log: bool = True,
           user: str = None, recipient: str = None, via: str = None, asked: list = None) -> list:
    """user：登记表里的使用者（ethan/memusers.py），必填；只能看到场景和它自己级别里更严的那一级。
    via：能力题函数（ethan/onto_query.py）里查的写上题号（比如 "q19"）；不写就是兜底检索，“我”页按这个算“按题答 vs 兜底”的比例。
    对外的使用者（合作方、公开两档）不按可见级别，按对外开关（lifebase.disclose）：只给“可以说”的关于她的说法和她批准的原句；
    原始记录、事件、收藏一律不给。传了 asked（列表）的，“问我”那几类的命中记进去（{"key", "category"}），不给内容。"""
    from . import memusers
    who = memusers.need(user, "search")  # 只登记了读某几项的（日记只读写作规范）不能检索
    if lifebase.on_hold(db) and not who.get("all"):
        return []  # 记忆暂停中（恢复后核不清删除记录，她 11-03 选先不用）
    started = time.time()
    ensure(db)
    level, default_limit = SCENES[scene]
    want = max_level or level
    if lifebase.LEVELS.index(who["level"]) > lifebase.LEVELS.index(want):
        want = who["level"]
    allowed = lifebase.LEVELS[lifebase.LEVELS.index(want):]
    limit = limit or default_limit
    today = today or her_today()
    fused, wanted, total = {}, set(), None
    for q in [x for q in queries for x in expand(q)]:
        qt = list(dict.fromkeys(tokens(q, query=True)))
        if not qt:
            continue
        # 有区分度的片段：库里有、但不是到处都有（“哈哈”“我觉”这种不算）。一条记忆至少要命中一个才算相关
        if total is None:
            total = db.conn.execute("SELECT COUNT(*) FROM mem_fts").fetchone()[0]
        df = {t: db.conn.execute("SELECT COUNT(*) FROM mem_fts WHERE mem_fts MATCH ?", (f'"{t}"',)).fetchone()[0] for t in qt}
        keen = {t for t in qt if 0 < df[t] <= max(3, total * COMMON)}
        if not keen:
            continue
        wanted |= keen
        match = " OR ".join(f'"{t}"' for t in sorted(keen))
        rows = db.conn.execute("SELECT key, toks FROM mem_fts WHERE mem_fts MATCH ? ORDER BY bm25(mem_fts) LIMIT 30", (match,)).fetchall()
        rank = 0
        for r in rows:
            rank += 1
            fused[r["key"]] = fused.get(r["key"], 0.0) + 1.0 / (K + rank)
    if semantic_on(db):  # 按意思找的一路：和关键词的结果按名次融合（同一个 K）
        for q in queries:
            for rank, (key, _sim) in enumerate(similar_keys(db, q), start=1):
                fused[key] = fused.get(key, 0.0) + 1.0 / (K + rank)
    if not fused:
        out = []
    else:
        marks = ",".join("?" * len(fused))
        docs = {r["key"]: dict(r) for r in db.conn.execute(f"SELECT * FROM mem_docs WHERE key IN ({marks})", list(fused))}
        mine, relay = _who_said(db, fused) if about_her(queries) else (set(), set())
        out = []
        outside = memusers.external(user)
        for key, base in fused.items():
            d = docs.get(key)
            if not d or not d["valid"] or (kinds and d["kind"] not in kinds):
                continue
            if outside:  # 对外：按三档开关，不按可见级别（ontology-plan 3.3）
                if d.get("know") != "disclosure":
                    f = db.conn.execute("SELECT * FROM facts WHERE id = ?", (int(key.split(":")[1]),)).fetchone() \
                        if key.startswith("fact:") else None
                    mode = lifebase.disclose(db, user, f, recipient=recipient) if f else "deny"
                    if mode == "ask" and asked is not None:
                        asked.append({"key": key, "category": lifebase.category_of(f["dimension"], f["prop"], f["kind"], f["statement"]),
                                      "draft": f["statement"]})
                    if mode != "allow":
                        continue
            elif d["visibility"] not in allowed:
                continue
            elif not memusers.may_read(user, d.get("know"), d.get("read_by"), d.get("dim")):  # 三维权限里的“读取与推理”（ontology.md 6.1 节）
                continue
            if d.get("know") == "disclosure" and not (memusers.USERS[user].get("all")
                                                     or lifebase.disclosure_ok(db, int(key.split(":")[1]), user, recipient=recipient)):
                continue  # 撤回或过期的对外表达，立刻不再说
            day = (d["at"] or "")[:10]
            if (time_from and day and day < time_from) or (time_to and day and day > time_to):
                continue
            recency = 1.0 if not d["decay"] else 0.5 + 0.5 * math.pow(0.5, _days(d["at"], today) / 90)
            score = base * recency * (0.8 + 0.1 * (d["importance"] - 1))
            score *= SELF_BOOST if key in mine else RELAY_DAMP if key in relay else 1.0
            out.append({"key": key, "kind": d["kind"], "know": d.get("know"), "label": d["label"], "title": d["title"], "at": day,
                        "text": snippet(d["body"], wanted), "score": round(score, 6)})
        out.sort(key=lambda x: -x["score"])
        out = out[:limit]
    if log:
        db.conn.execute("INSERT INTO retrieval_log (at, scene, round, queries, keys, latency_ms, user, via) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (_now(), scene, round_no, json.dumps(list(queries), ensure_ascii=False),
                         json.dumps([x["key"] for x in out]), int((time.time() - started) * 1000), user, via))
        db.conn.commit()
    return out


def lines(results: list) -> str:
    """给 小伴 看的样子：一条一行，带来源和日期。"""
    return "\n".join(f"- [{r['label']}{'｜' + r['at'] if r['at'] else ''}] " + ("（我的猜测，只能和她本人讨论，不当事实用）" if r.get("know") == "hypothesis" else "")
                     + (f"{r['title']}：" if r["title"] else "")
                     + " ".join(r["text"].split()) for r in results)


def main():
    parser = argparse.ArgumentParser(description="查记忆")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--scene", choices=sorted(SCENES), default="chat")
    parser.add_argument("--semantic", choices=("on", "off", "measure"),
                        help="打开 / 关掉语义检索（打开时把已有记忆算一遍向量）；measure：用编的 200 句量内存和速度，不碰记忆")
    parser.add_argument("query", nargs="*")
    args = parser.parse_args()
    from .memory import MemoryStore
    store = MemoryStore(args.data)
    try:
        print(f"索引更新 {refresh(store.db, store.root)} 条", flush=True)
        if args.semantic == "measure":
            print(json.dumps(measure(store.db), ensure_ascii=False), flush=True)
        elif args.semantic:
            print(json.dumps(set_semantic(store.db, args.semantic == "on"), ensure_ascii=False), flush=True)
        if args.query:
            print(lines(search(store.db, args.query, args.scene, log=False, user="system")) or "（没找到）", flush=True)
    finally:
        store.close()


if __name__ == "__main__":
    main()
