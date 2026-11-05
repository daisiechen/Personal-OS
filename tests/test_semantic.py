"""语义检索（她 11-04 提前做、选 A）：按意思找、和关键词融合、权限照旧、删除一起删、没开时什么都不变。用假的向量模型测。"""

import tempfile
import unittest
from pathlib import Path

from ethan import lifebase, memsearch, mepage
from ethan.memory import MemoryStore

# 假模型：几个“意思”各占一维，词落在哪一维由下表定
TOPICS = [("大学", "本科", "学校", "读书", "进修", "就读", "高校"), ("猫", "宠物"), ("跑步", "训练", "健身")]


class Fake:
    def embed(self, texts):
        return [[float(sum(w in t for w in words)) + 0.01 for words in TOPICS] for t in texts]


class SemanticTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        lifebase.ensure(self.db)
        self._old = memsearch._EMBEDDER
        memsearch._EMBEDDER = Fake()
        self._mirror = lifebase.MIRROR_DIR
        lifebase.MIRROR_DIR = self.root / "state"
        self.ev = lifebase.add_evidence(self.db, "file", "file:cv", "2026-11-03", "简历")
        self.school = lifebase.add_fact(self.db, "abilities", "在某理工大学读软件工程本科", [self.ev])
        self.cat = lifebase.add_fact(self.db, "relationships", "养了两只橘猫", [self.ev])
        self.db.conn.commit()

    def tearDown(self):
        memsearch._EMBEDDER = self._old
        lifebase.MIRROR_DIR = self._mirror
        self.store.close()
        self.tmp.cleanup()

    def test_off_by_default_nothing_changes(self):
        memsearch.refresh(self.db)
        self.assertFalse(memsearch.semantic_on(self.db))
        self.assertEqual(memsearch.similar_keys(self.db, "念过哪所高校"), [])
        self.assertEqual(memsearch.search(self.db, ["念过哪所高校"], user="ethan", log=False), [])

    def test_meaning_found_after_on(self):
        memsearch.refresh(self.db)
        got = memsearch.set_semantic(self.db, True)
        self.assertTrue(got["semantic"])
        keys = [r["key"] for r in memsearch.search(self.db, ["念过哪所高校"], user="ethan", log=False)]
        self.assertEqual(keys[0], f"fact:{self.school}")  # 字面不同、意思对上
        self.assertNotIn(f"fact:{self.cat}", keys)
        texts = [x["text"] for x in mepage.all_memory(self.db, "念过哪所高校")["items"]]
        self.assertIn("在某理工大学读软件工程本科", texts)

    def test_permissions_still_apply_and_forget_removes_vectors(self):
        memsearch.refresh(self.db)
        memsearch.set_semantic(self.db, True)
        # 对外的按开关（ontology-plan 3.3）：学历技能公开档可以说，按意思也找得到；感情家人那类照样拿不到
        self.assertEqual([r["key"] for r in memsearch.search(self.db, ["念过哪所高校"], "public", user="twin", log=False)], [f"fact:{self.school}"])
        self.assertEqual(memsearch.search(self.db, ["宠物"], "public", user="twin", log=False), [])
        lifebase.forget(self.db, [self.ev], root=self.root)
        memsearch.refresh(self.db)
        n = self.db.conn.execute("SELECT COUNT(*) FROM mem_vec WHERE key LIKE 'fact:%'").fetchone()[0]
        self.assertEqual(n, 0)

    def test_not_installed_stays_off(self):
        memsearch._EMBEDDER = None
        import ethan.memsearch as m
        orig = m._FastEmbed
        m._FastEmbed = lambda: (_ for _ in ()).throw(ImportError("no fastembed"))
        try:
            got = memsearch.set_semantic(self.db, True)
        finally:
            m._FastEmbed = orig
        self.assertFalse(got["semantic"])
        self.assertFalse(memsearch.semantic_on(self.db))


if __name__ == "__main__":
    unittest.main()


class ChatDoesNotWaitTest(unittest.TestCase):
    """11-04 打开后 小伴 回消息卡住：聊天时不能现场等模型加载、不能补算大批向量。"""

    def test_search_does_not_block_on_model_load(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            db = store.db
            lifebase.ensure(db)
            memsearch.ensure(db)
            db.conn.execute("INSERT OR REPLACE INTO mem_state (source, last_id) VALUES ('semantic', 1)")
            gate = threading.Event()

            class Slow:
                def __init__(self, cache_dir=None):
                    gate.wait(5)

                def embed(self, texts):
                    return [[1.0] for _ in texts]
            old, old_e, old_l = memsearch._FastEmbed, memsearch._EMBEDDER, memsearch._LOADING
            memsearch._FastEmbed, memsearch._EMBEDDER, memsearch._LOADING = Slow, None, None
            try:
                t = time.time()
                self.assertEqual(memsearch.similar_keys(db, "念过哪所高校"), [])
                memsearch.refresh(db)
                self.assertLess(time.time() - t, 2)
            finally:
                gate.set()
                memsearch._LOADING.join(5)
                memsearch._FastEmbed, memsearch._EMBEDDER, memsearch._LOADING = old, old_e, old_l
                store.close()

    def test_hanging_embed_times_out_and_falls_back(self):
        import threading
        import time
        with tempfile.TemporaryDirectory() as tmp:
            store = MemoryStore(Path(tmp))
            db = store.db
            lifebase.ensure(db)
            memsearch.ensure(db)
            db.conn.execute("INSERT OR REPLACE INTO mem_state (source, last_id) VALUES ('semantic', 1)")
            db.conn.execute(memsearch.VEC_SCHEMA)
            db.conn.execute("INSERT INTO mem_vec (key, hash, vec) VALUES ('fact:1', 'x', ?)", (memsearch._pack([1.0]),))
            gate = threading.Event()

            class Hang:
                def embed(self, texts):
                    gate.wait(10)
                    return [[1.0]]
            old_e, old_t, old_b = memsearch._EMBEDDER, memsearch.SEM_TIMEOUT, memsearch._BROKEN
            memsearch._EMBEDDER, memsearch.SEM_TIMEOUT, memsearch._BROKEN = Hang(), 0.2, False
            try:
                t = time.time()
                self.assertEqual(memsearch.similar_keys(db, "念过哪所高校"), [])
                self.assertLess(time.time() - t, 2)
                self.assertTrue(memsearch._BROKEN)  # 这个进程以后不再试，直接用关键词
            finally:
                gate.set()
                memsearch._EMBEDDER, memsearch.SEM_TIMEOUT, memsearch._BROKEN = old_e, old_t, old_b
                store.close()
