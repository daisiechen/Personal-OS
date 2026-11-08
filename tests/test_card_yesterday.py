"""昨日卡的素材全按昨天取、重画一定换一张：
“为什么昨天那张卡画出来是上肢？上肢不是今天练的吗？小伴 去画卡的时候，是不是提示词拿错了？”
“我刚才点了重新画卡，为什么重画出来的卡跟上一次画出来的卡一模一样？”
“在我点开昨日卡的时候，就给我一个入口让我重画这张卡，不要再让我回到卡册里面去重新画卡。”
"""

import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ethan import cards, diary
from ethan.memory import MemoryStore
from tests.test_workbench import FakeLedger, FakeMaker

YDAY, TODAY = date(2026, 11, 5), date(2026, 11, 6)
DIARY = "下午 14:58 出门练下肢，深蹲做到最后一组腿都在抖。回家路上买了杯热巧克力。"


def shot(title, scene, action, line="昨天练下肢辛苦啦，小宝"):
    return json.dumps({"title": title, "line": line, "scene": scene, "action": action, "outfit": "sporty set",
                       "camera": "low angle", "light": "afternoon", "twist": ""}, ensure_ascii=False)


class Director:
    """导演按顺序吐镜头；记下每次拿到的素材（brief）。"""

    def __init__(self, shots):
        self.shots, self.briefs = list(shots), []

    def complete(self, system, messages, purpose="chat"):
        self.briefs.append((purpose, system, messages[0]["content"]))
        if purpose != "card-director":
            return SimpleNamespace(text="")
        return SimpleNamespace(text=self.shots.pop(0) if len(self.shots) > 1 else self.shots[0])


class OverBudget(FakeLedger):
    def budget_status(self):
        return "over"


class YesterdayCardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        self.db = self.store.db
        (self.root / "avatars" / "library").mkdir(parents=True)
        for name in ("avatar-day.jpg", "library/S1.png", "library/W4.png"):
            (self.root / "avatars" / name).write_bytes(b"\xff\xd8fake")
        # 昨天（11-05）排的、练的是下肢；今天（11-06）的计划是上肢 + 核心
        self.db.save_plan(YDAY.isoformat(), "model", "", "{}", "y", [
            {"kind": "event", "start": "15:00", "end": "16:00", "title": "训练：下肢", "place": "PureGym"},
            {"kind": "todo", "title": "回房东邮件"}])
        self.db.save_plan(TODAY.isoformat(), "model", "", "{}", "t", [
            {"kind": "event", "start": "10:00", "end": "11:00", "title": "训练：上肢 + 核心", "place": "Fitness First"},
            {"kind": "event", "start": "14:00", "title": "签证面谈"}])
        (self.root / "weather.json").write_text(json.dumps({"days": {
            YDAY.isoformat(): {"day": YDAY.isoformat(), "code": 3, "max": 12, "min": 7, "wind": 10},
            TODAY.isoformat(): {"day": TODAY.isoformat(), "code": 61, "max": 9, "min": 4, "wind": 30}}}))
        diary.save(self.db, YDAY.isoformat(), DIARY, source="ethan")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def make(self, router, maker=None, **kw):
        return cards.make_card(self.root, self.db, TODAY, self.db.latest_plan(TODAY.isoformat()), maker=maker or FakeMaker(),
                               router=router, store=self.store, fetch=lambda url, timeout: 1 / 0, **kw)

    # ---------- 1. 素材只按昨天取 ----------
    def test_material_is_yesterdays_only(self):
        mat = cards.yesterday_material(self.db, TODAY, self.root)
        self.assertEqual(mat["day"], YDAY)
        self.assertEqual(mat["train"], "训练：下肢")
        self.assertEqual(mat["weather_text"], "多云 7–12°")  # 昨天的天气，不是今天的雨
        text = cards.material_brief(mat) + cards.material_source(mat)
        self.assertIn("下肢", text)
        self.assertIn("PureGym", text)
        for today_only in ("上肢", "核心", "签证", "Fitness First", "雨"):
            self.assertNotIn(today_only, text)

    def test_director_and_image_prompt_have_no_today_plan(self):
        d = Director([shot("下肢通关", "a gym squat rack", "finishing the last squat, legs shaking")])
        maker = FakeMaker()
        card = self.make(d, maker)
        brief = [b for b in d.briefs if b[0] == "card-director"][0]
        for text in (brief[1] + brief[2], maker.calls[0][0], card["reason"], card["line"]):
            self.assertNotIn("上肢", text)
            self.assertNotIn("签证", text)
        self.assertIn("下肢", brief[2])
        self.assertIn("昨天", brief[1])  # 导演的说明写明画的是昨天
        self.assertNotIn("今天的安排", brief[2])  # 素材里没有今天的安排这一栏
        self.assertIn("昨天练了下肢", card["reason"])
        self.assertEqual(card["weather_day"], YDAY.isoformat())

    def test_fallback_card_without_director_uses_yesterdays_training(self):
        card = cards.make_card(self.root, self.db, TODAY, self.db.latest_plan(TODAY.isoformat()), maker=FakeMaker(),
                               fetch=lambda url, timeout: 1 / 0)
        self.assertEqual(card["code"], "S1")
        self.assertIn("下肢", card["line"])
        self.assertNotIn("上肢", card["line"] + card["reason"])

    def test_planned_but_not_done_is_not_drawn_as_training(self):
        diary.save(self.db, YDAY.isoformat(), "昨天下午一直在家写材料，没出门。", source="ethan")
        card = cards.make_card(self.root, self.db, TODAY, None, maker=FakeMaker(), fetch=lambda url, timeout: 1 / 0)
        self.assertNotEqual(card["code"], "S1")
        self.assertNotIn("练了", card["reason"])

    # ---------- 2. 重画一定换一张 ----------
    def test_redo_runs_director_again_avoids_previous_and_keeps_old(self):
        first = self.make(Director([shot("下肢通关", "a gym squat rack", "finishing the last squat")]))
        ledger, maker = FakeLedger(), FakeMaker()
        d = Director([shot("下肢通关", "a gym squat rack", "finishing the last squat"),  # 第一次又撞了上一版
                      shot("热巧克力", "a rainy-window cafe", "holding a hot chocolate, legs stretched out")])
        card = cards.remake(self.root, self.db, TODAY, None, maker=maker, ledger=ledger, router=d, store=self.store,
                            fetch=lambda url, timeout: 1 / 0)
        directs = [b for b in d.briefs if b[0] == "card-director"]
        self.assertEqual(len(directs), 2)
        self.assertIn("下肢通关", directs[0][2])  # 上一版的卡名和镜头交给了导演，要它避开
        self.assertIn("a gym squat rack", directs[0][2])
        self.assertIn("还是撞了", directs[1][2])
        self.assertNotEqual(card["title"], first["title"])
        self.assertNotEqual(card["shot"]["scene"], first["shot"]["scene"])
        self.assertEqual(len(maker.calls), 1)  # 重新生图
        self.assertNotEqual(maker.calls[0][0], cards.prompt(first))
        self.assertEqual(ledger.rows, [("openrouter", cards.COST, "daily-card")])  # 照生图账本记账
        old = sorted(p.name for p in (self.root / "cards" / "old").iterdir())
        self.assertEqual(len(old), 2)  # 旧的 json 和 jpg 都挪进 cards/old/
        self.assertTrue(any(n.endswith(".json") for n in old) and any(n.endswith(".jpg") for n in old))
        saved = json.loads((self.root / "cards" / f"{TODAY}.json").read_text())
        self.assertEqual(saved["title"], "热巧克力")
        self.assertEqual(saved["redo_of"]["title"], "下肢通关")
        self.assertTrue(cards.redo_note(self.root, TODAY)["ok"])

    def test_redo_failure_keeps_original(self):
        first = self.make(Director([shot("下肢通关", "a gym squat rack", "finishing the last squat")]))

        class Down:
            def complete(self, *a, **k):
                raise RuntimeError("quota")
        card = cards.remake(self.root, self.db, TODAY, None, maker=FakeMaker(), router=Down(), store=self.store,
                            fetch=lambda url, timeout: 1 / 0)
        self.assertTrue(card["redo_failed"])
        saved = json.loads((self.root / "cards" / f"{TODAY}.json").read_text())
        self.assertEqual(saved["title"], first["title"])
        self.assertTrue((self.root / "cards" / f"{TODAY}.jpg").is_file())
        self.assertFalse(cards.waiting(self.root, TODAY))
        self.assertFalse(cards.redo_note(self.root, TODAY)["ok"])
        # 生图全失败：也不拿图库备用图顶掉原来那张真画的
        card = cards.remake(self.root, self.db, TODAY, None, maker=FakeMaker(fail=True), store=self.store,
                            router=Director([shot("热巧克力", "a cafe", "sipping")]), fetch=lambda url, timeout: 1 / 0)
        self.assertEqual(card["redo_failed"], "图没画出来")
        self.assertEqual(json.loads((self.root / "cards" / f"{TODAY}.json").read_text())["title"], first["title"])

    def test_redo_over_budget_does_not_touch_card(self):
        self.make(Director([shot("下肢通关", "a gym squat rack", "finishing the last squat")]))
        maker, d = FakeMaker(), Director([shot("热巧克力", "a cafe", "sipping")])
        card = cards.remake(self.root, self.db, TODAY, None, maker=maker, ledger=OverBudget(), router=d, store=self.store)
        self.assertIn("预算", card["redo_failed"])
        self.assertEqual((maker.calls, d.briefs), ([], []))
        self.assertFalse((self.root / "cards" / "old").exists())

    # ---------- 3. 工作台：点开昨日卡就能重画 ----------
    def test_workbench_redo_action_and_progress(self):
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        self.make(Director([shot("下肢通关", "a gym squat rack", "finishing the last squat")]))
        wb = Workbench(self.store, Settings(self.root / "settings.json"))

        class Runner:
            def __init__(self):
                self.n, self.running = 0, False

            def __call__(self):
                self.n += 1
                self.running = True
                return True

            def busy(self):
                return self.running
        wb.redo_runner = Runner()
        self.assertFalse(wb.card(TODAY)["redoing"])
        r = wb.do_card_redo({}, TODAY)
        self.assertTrue(r["ok"])
        self.assertEqual(wb.redo_runner.n, 1)
        self.assertTrue(wb.card(TODAY)["redoing"])
        wb.redo_runner.running = False
        cards._redo_note(self.root, TODAY, False, "导演没编出来，原来那张留着")
        self.assertIn("原来那张留着", wb.card(TODAY)["redo_msg"])
        self.assertFalse(wb.do_card_redo({}, YDAY)["ok"])  # 那天没有卡：没有可以重画的

    def test_ui_has_one_redo_entry_in_big_card(self):
        app = (Path(__file__).resolve().parents[1] / "ethan" / "workbench" / "static" / "app.js").read_text(encoding="utf-8")
        big = app[app.index('if (S.overlay === "card") {'):app.index('} else if (S.overlay === "plan")')]
        self.assertIn("redoButton(c)", big)  # 打开昨日卡的大图里就有重画
        view = app[app.index('S.overlay === "cardview"'):app.index('S.overlay === "carddiary"')]
        self.assertIn("redoButton(st.card)", view)  # 卡册里看今天那张：同一个按钮
        self.assertEqual(len(re.findall(r'act\("card\.redo"', app)), 1)  # 只有一个处理函数
        self.assertIn('confirmTwice("cardredo"', app)


class HerPhotoRefTest(unittest.TestCase):
    """晚：“我昨天不是给 小伴 发了我自己的照片吗？为什么他在画图的时候……也没有参考我给他发的我自己的照片？”"""

    def setUp(self):
        from ethan import brain
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        av = self.root / "avatars"
        av.mkdir()
        for name in ("avatar-day.jpg", "avatar-night.jpg"):
            (av / name).write_bytes(b"\xff\xd8head")
        (self.root / "media" / YDAY.isoformat()).mkdir(parents=True)
        self.photo = self.root / "media" / YDAY.isoformat() / "selfie.jpg"
        self.photo.write_bytes(b"\xff\xd8her-face")
        (self.root / "media" / YDAY.isoformat() / "notes.pdf").write_bytes(b"%PDF")
        brain.ensure(self.store.db)
        brain._put(self.store.db, "file", "file:abc123", "自拍", source="她发的文件", status="kept",
                   extra={"path": f"media/{YDAY}/selfie.jpg", "doc_type": "photo"})
        brain._put(self.store.db, "file", "file:def456", "合同", source="她发的文件", status="kept",
                   extra={"path": f"media/{YDAY}/notes.pdf", "doc_type": "document"})
        self.store.db.conn.commit()
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))
        ids = {r["ref"]: r["id"] for r in self.store.db.conn.execute("SELECT id, ref FROM brain_items")}
        self.pid, self.doc = ids["file:abc123"], ids["file:def456"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_without_her_photo_refs_are_the_two_heads(self):
        self.assertEqual([r.name for r in cards.references(self.root, "W4")], ["avatar-day.jpg", "avatar-night.jpg"])

    def test_not_used_until_she_marks_it(self):
        item = next(x for x in self.wb.brain_page()["items"] if x["id"] == self.pid)
        self.assertEqual((item["photo"], item["avatar_ref"]), (f"/img/file/{self.pid}", ""))  # 没有看图判断的不自动用
        self.assertNotIn("selfie.jpg", [r.name for r in cards.references(self.root, "W4")])  # 不擅自用她发的照片
        self.assertNotIn("photo", next(x for x in self.wb.brain_page()["items"] if x["id"] == self.doc))
        self.assertFalse(self.wb.act("avatar.ref", {"id": self.doc})["ok"])  # 不是照片的不能设

    def test_marked_photo_goes_into_image_request_first(self):
        r = self.wb.act("avatar.ref", {"id": self.pid})
        self.assertTrue(r["ok"])
        self.assertNotIn("selfie", r.get("log") or "")  # 日志不写照片内容和文件名
        refs = cards.references(self.root, "W4")
        self.assertEqual(refs[0].read_bytes(), b"\xff\xd8her-face")
        self.assertLessEqual(len(refs), 3)
        self.assertEqual(refs[1].name, "avatar-day.jpg")
        self.assertTrue(next(x for x in self.wb.brain_page()["items"] if x["id"] == self.pid)["avatar_ref"])
        maker = FakeMaker()
        cards.make_card(self.root, self.store.db, TODAY, None, maker=maker, fetch=lambda url, timeout: 1 / 0)
        self.assertEqual(maker.calls[0][1][0], refs[0].name)  # 生图请求里带上她的照片
        self.assertEqual(self.wb.photo_file(self.pid), self.photo.resolve())
        self.assertIsNone(self.wb.photo_file(self.doc))
        self.assertTrue(self.wb.act("avatar.ref", {"id": self.pid, "off": 1})["ok"])
        self.assertEqual([r.name for r in cards.references(self.root, "W4")], ["avatar-day.jpg", "avatar-night.jpg"])

    def test_newest_two_of_her_photos(self):
        import os
        refs = self.root / "avatars" / "refs"
        refs.mkdir()
        for i, name in enumerate(("old.jpg", "mid.jpg", "new.jpg")):
            (refs / name).write_bytes(b"x")
            os.utime(refs / name, (1000 + i, 1000 + i))
        self.assertEqual([r.name for r in cards.references(self.root, "W4")], ["new.jpg", "mid.jpg", "avatar-day.jpg"])


class AutoSelfieRefTest(unittest.TestCase):
    """晚：“为什么我还要自己去第二大脑里面把自拍设为形象参考呢？小伴 每天画图的时候，不就应该考虑到这个吗？”
    收照片时看图那一步顺带写“本人照：是/否/拿不准”，是她本人清楚的自拍自动进形象参考；她取消过的永远不用，手动设的最优先。"""

    def setUp(self):
        import os
        from ethan.core.router import Settings
        from ethan.workbench.api import Workbench
        self.os = os
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = MemoryStore(self.root)
        av = self.root / "avatars"
        av.mkdir()
        for name in ("avatar-day.jpg", "avatar-night.jpg"):
            (av / name).write_bytes(b"\xff\xd8head")
        self.day = self.root / "media" / YDAY.isoformat()
        self.day.mkdir(parents=True)
        self.wb = Workbench(self.store, Settings(self.root / "settings.json"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def ingest(self, name: str, seen: str, doc_type: str = "photo", at: int = None) -> str:
        from ethan import files

        class Router:
            def complete(self, system, messages, purpose="chat", images=()):
                if purpose == "file-transcribe":
                    return SimpleNamespace(text=seen)
                return SimpleNamespace(text=json.dumps({"title": name, "summary": "一张照片", "doc_type": doc_type, "tags": []}))

        path = self.day / name
        path.write_bytes(b"\xff\xd8" + name.encode())
        if at:
            self.os.utime(path, (at, at))
        files.ingest(self.store, path, Router())
        return files.sha(path)

    def her(self) -> list:
        return [r.read_bytes()[2:].decode() for r in cards.her_refs(self.root)]

    def test_her_selfie_goes_in_others_do_not(self):
        h = self.ingest("me.jpg", "她在窗边对镜自拍，脸很清楚。\n本人照：是")
        self.ingest("friend.jpg", "一个男生在咖啡馆。\n本人照：否")
        self.ingest("view.jpg", "海边日落，没有人。\n本人照：否")
        self.ingest("blur.jpg", "一个人的侧影，有点糊。\n本人照：拿不准")
        self.ingest("shot.png", "聊天截图，里面有一张自拍。\n本人照：是", doc_type="screenshot")
        self.ingest("old.jpg", "一张照片")  # 没有判断那一行的：不用
        self.assertEqual(self.her(), ["me.jpg"])
        refs = cards.references(self.root, "W4")
        self.assertEqual([r.name for r in refs][:2], [cards.ref_name(h, ".jpg", auto=True), "avatar-day.jpg"])
        text = (self.root / "files" / f"{h}.txt").read_text(encoding="utf-8")
        self.assertNotIn("本人照", text)  # 判断记在 extra，全文不留
        row = self.store.db.conn.execute("SELECT id, extra FROM brain_items WHERE ref = ?", (f"file:{h}",)).fetchone()
        self.assertEqual(json.loads(row["extra"])["selfie"], "yes")
        item = next(x for x in self.wb.brain_page()["items"] if x["id"] == row["id"])
        self.assertEqual(item["avatar_ref"], "auto")

    def test_she_turned_it_off_never_comes_back(self):
        from ethan import files
        h = self.ingest("me.jpg", "自拍。\n本人照：是")
        pid = self.store.db.conn.execute("SELECT id FROM brain_items WHERE ref = ?", (f"file:{h}",)).fetchone()["id"]
        self.assertTrue(self.wb.act("avatar.ref", {"id": pid, "off": 1})["ok"])
        self.assertEqual(self.her(), [])
        self.assertEqual(cards.auto_ref(self.root, self.store.db, self.day / "me.jpg", h), "")  # 锁住：自动流程不再放回
        mod = _migration()
        mod.run(self.store)
        self.assertEqual(self.her(), [])
        self.assertTrue(self.wb.act("avatar.ref", {"id": pid})["ok"])  # 她自己再设：可以，放开锁
        self.assertEqual(self.her(), ["me.jpg"])
        self.assertEqual(cards.ref_state(self.root, h), "manual")
        self.assertIsNotNone(files)

    def test_manual_first_then_newest_and_cap(self):
        a = self.ingest("a.jpg", "本人照：是", at=1000)
        self.ingest("b.jpg", "本人照：是", at=3000)
        self.ingest("c.jpg", "本人照：是", at=2000)
        self.assertEqual(self.her(), ["b.jpg", "c.jpg", "a.jpg"])  # 最新的先（按原照片时间）
        self.assertEqual(len(cards.references(self.root, "W4")), 3)
        self.assertEqual([r.read_bytes()[2:].decode() for r in cards.references(self.root, "W4")[:2]], ["b.jpg", "c.jpg"])
        pid = self.store.db.conn.execute("SELECT id FROM brain_items WHERE ref = ?", (f"file:{a}",)).fetchone()["id"]
        self.wb.act("avatar.ref", {"id": pid})  # 她亲手设的最旧那张：排第一
        refs = cards.references(self.root, "W4")
        self.assertEqual([r.read_bytes()[2:].decode() for r in refs[:2]], ["a.jpg", "b.jpg"])
        self.assertEqual(refs[2].name, "avatar-day.jpg")
        self.assertEqual(len(cards.her_refs(self.root)), 3)  # 自动那份换成手动的，不重复

    def test_backfill_from_existing_descriptions(self):
        from ethan import brain
        brain.ensure(self.store.db)
        rows = {"self.jpg": "她在镜子前自拍，穿白衬衫，脸清楚。", "group.jpg": "四个人的合照，大家在吃饭，有人在自拍。",
                "cat.jpg": "一只猫趴在沙发上。", "none.jpg": ""}
        for i, (name, seen) in enumerate(rows.items()):
            (self.day / name).write_bytes(b"\xff\xd8" + name.encode())
            brain._put(self.store.db, "file", f"file:h{i:015d}", name, source="她发的文件", summary=seen, status="kept",
                       extra={"path": f"media/{YDAY}/{name}", "doc_type": "photo"})
        self.store.db.conn.commit()
        before = {p.name: p.read_bytes() for p in self.day.iterdir()}
        msg = _migration().run(self.store)
        self.assertEqual(self.her(), ["self.jpg"])
        self.assertIn("自动进形象参考 1 张", msg)
        self.assertIn("没有看图描述的 1 张", msg)
        self.assertEqual({p.name: p.read_bytes() for p in self.day.iterdir()}, before)  # 原图不删不改
        _migration().run(self.store)  # 再跑不重复
        self.assertEqual(len(cards.her_refs(self.root)), 1)

    def test_verdict(self):
        self.assertEqual(cards.selfie_verdict("一段描述\n本人照：是"), "yes")
        self.assertEqual(cards.selfie_verdict("本人照：拿不准"), "unsure")
        self.assertEqual(cards.selfie_verdict("本人照: 否"), "no")
        self.assertEqual(cards.selfie_verdict("Mia 的 selfie，脸很清楚"), "yes")
        self.assertEqual(cards.selfie_verdict("两个人在自拍"), "unsure")
        self.assertEqual(cards.selfie_verdict("风景照"), "unsure")


class SaidAndScenesTest(unittest.TestCase):
    """晚同意“加”：昨日卡素材加昨天她在聊天里说的话、昨天她发的场景照。"""

    def setUp(self):
        YesterdayCardTest.setUp(self)
        from ethan import brain, lifebase
        lifebase.ensure(self.db)
        said = [("2026-11-05T09:00:00+00:00", "mia：早上在 Monmouth 喝了杯咖啡，好开心"),
                ("2026-11-05T10:00:00+00:00", "ethan：去公园走走吧，我陪你"),
                ("2026-11-05T11:00:00+00:00", "mia：帮我把待办改成明天"),
                ("2026-11-05T12:00:00+00:00", "mia：中午和朋友吃了火锅，电话 07700 900123 记一下"),
                ("2026-11-05T13:00:00+00:00", "mia：嗯"),
                ("2026-11-04T12:00:00+00:00", "mia：前天去了海边")]
        for i, (at, text) in enumerate(said):
            lifebase.add_evidence(self.db, "chat", f"raw:{i}", at, text)
        brain.ensure(self.db)
        self.files = []
        for i, (day, name, extra) in enumerate([
                (YDAY, "cafe.jpg", {"doc_type": "photo", "scene": "yes"}),
                (YDAY, "dinner.jpg", {"doc_type": "photo"}),  # 老条目：没有“场景照”那一行，是生活照就算
                (YDAY, "lunch.jpg", {"doc_type": "photo", "scene": "yes"}),
                (YDAY, "me.jpg", {"doc_type": "photo", "selfie": "yes", "scene": "yes"}),  # 自拍是形象参考，不当场景
                (YDAY, "shot.png", {"doc_type": "screenshot"}),
                (YDAY, "bill.jpg", {"doc_type": "receipt", "scene": "no"}),
                (date(2026, 11, 4), "beach.jpg", {"doc_type": "photo", "scene": "yes"})]):
            folder = self.root / "media" / day.isoformat()
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(b"\xff\xd8" + name.encode())
            import os
            os.utime(folder / name, (1000 + i, 1000 + i))
            brain._put(self.db, "file", f"file:s{i:015d}", name, source="她发的文件", summary=f"照片 {name} 的描述：画面里有{name[:-4]}",
                       status="kept", happened_at=day.isoformat(), extra={"path": f"media/{day}/{name}", **extra})
        self.db.conn.commit()

    def tearDown(self):
        YesterdayCardTest.tearDown(self)

    make = YesterdayCardTest.make

    def test_her_words_go_to_director_ethans_do_not(self):
        mat = cards.yesterday_material(self.db, TODAY, self.root)
        self.assertEqual(mat["said"][0], "早上在 Monmouth 喝了杯咖啡，好开心")
        joined = " ".join(mat["said"])
        self.assertNotIn("公园", joined)  # 小伴 说的不取
        self.assertNotIn("海边", joined)  # 前天的不取
        self.assertNotIn("待办", joined)  # 交代事的不取
        self.assertNotIn("嗯", mat["said"])
        self.assertNotIn("07700", joined)  # 去掉联系方式
        d = Director([shot("咖啡早晨", "Monmouth coffee bar", "sipping coffee")])
        self.make(d)
        brief = [b for b in d.briefs if b[0] == "card-director"][0][2]
        self.assertIn("Monmouth", brief)
        self.assertNotIn("我陪你", brief)
        self.assertIn("Monmouth", cards.material_source(mat))  # 核对卡面依据时找得到

    def test_said_capped_at_eight(self):
        from ethan import lifebase
        for i in range(12):
            lifebase.add_evidence(self.db, "chat", f"raw:x{i}", f"2026-11-05T15:{i:02d}:00+00:00", f"mia：下午第{i}次去超市买菜")
        self.assertEqual(len(cards.yesterday_material(self.db, TODAY, self.root)["said"]), cards.SAID_MAX)

    def test_cards_may_only_read_her_words(self):
        from ethan import onto_query
        self.assertEqual(onto_query.preferences(self.db, "cards")["status"], "unknown")
        self.assertEqual(onto_query.day_her_words(self.db, "twin", YDAY)["items"], [])  # 对外的没权限

    def test_yesterdays_scene_photos_become_references(self):
        mat = cards.yesterday_material(self.db, TODAY, self.root)
        self.assertEqual([s["path"].name for s in mat["scenes"]], ["lunch.jpg", "dinner.jpg"])  # 最新的先，最多 2 张
        d = Director([shot("午饭", "a hotpot place", "eating hotpot")])
        maker = FakeMaker()
        card = self.make(d, maker)
        refs = maker.calls[0][1]
        self.assertEqual(refs, ["avatar-day.jpg", "lunch.jpg", "dinner.jpg"])  # 脸在前，场景照在后，总共 3 张
        self.assertNotIn("beach.jpg", refs)  # 前天的不进
        self.assertNotIn("me.jpg", refs)
        self.assertIn("last 2 reference photo", maker.calls[0][0])
        self.assertEqual(card["scene_refs"], 2)
        brief = [b for b in d.briefs if b[0] == "card-director"][0][2]
        self.assertIn("画面里有lunch", brief)  # 导演看得到照片描述

    def test_cap_keeps_at_least_one_face(self):
        av = self.root / "avatars"
        (av / "avatar-night.jpg").write_bytes(b"x")
        refs = av / "refs"
        refs.mkdir()
        for n in ("her-a.jpg", "her-b.jpg"):
            (refs / n).write_bytes(b"x")
        scenes = [self.root / "media" / YDAY.isoformat() / n for n in ("cafe.jpg", "dinner.jpg", "lunch.jpg")]
        got = cards.references(self.root, "W4", scenes)
        self.assertEqual(len(got), cards.MAX_REFS)
        self.assertTrue(got[0].name.startswith("her-"))
        self.assertEqual([p.name for p in got[1:]], ["cafe.jpg", "dinner.jpg"])
        self.assertEqual(len(cards.references(self.root, "W4", scenes[:1])), 3)
        self.assertEqual([p.name for p in cards.references(self.root, "W4", scenes[:1])][2], "cafe.jpg")


def _migration():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "migrations" / "20261106_auto_selfie_refs.py"
    spec = importlib.util.spec_from_file_location("m_auto_selfie", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod



class TastefulShotTest(unittest.TestCase):
    def test_body_part_shots_and_restyles_are_tidied(self):
        """从背后贴地拍腿和臀那张“恶心”、卡牌边框把原来的画风带偏。导演没守住时代码兜底。"""
        from ethan import cards
        shot = cards.tidy_shot({"title": "腿后侧觉醒", "line": "y",
                                "scene": "A fantasy RPG character-card illustration set inside an ornate golden frame. Inside, a bright gym with a barbell.",
                                "action": "Caught mid Romanian deadlift, hips pushed back, back flat, hamstrings and glutes subtly glowing. She smiles with focus.",
                                "outfit": "Dark leggings, a black top", "camera": "Low three-quarter rear view from behind, near floor level",
                                "light": "Soft daylight", "twist": "Collectible game character card style with holographic foil"})
        self.assertEqual(shot["camera"], cards.SAFE_CAMERA)
        self.assertEqual(shot["twist"], "")
        for k in ("scene", "action"):
            self.assertNotRegex(shot[k], r"(?i)glute|hips|frame|card")
        self.assertIn("deadlift", shot["action"])
        self.assertIn("She smiles with focus.", shot["action"])
        self.assertIn("gym", shot["scene"])
        p = cards.prompt({"shot": shot, "code": "S1"})
        self.assertIn("keep the same art style", p) if shot["twist"] else self.assertNotIn("Style twist", p)
        self.assertIn("never focuses on body parts", p)
        self.assertNotIn("from behind", " ".join(cards.POSES))
        self.assertIn("不加卡牌边框", cards.DIRECTOR)


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
            import re as _re  # 测试里调的辅助函数（self.xxx( / 模块里的 xxx(）去读迁移脚本的，也算（11-08 两个测试漏过）
            mod = inspect.getmodule(t)
            for name in set(_re.findall(r"(?:self\.)?([A-Za-z_]\w*)\(", src)):
                fn = getattr(type(t), name, None) or getattr(mod, name, None)
                if callable(fn) and not isinstance(fn, type):
                    try:
                        setup += inspect.getsource(fn)
                    except (OSError, TypeError):
                        pass
            if t._testMethodName in ('test_upload_replaces_map_and_reads_new_achievements_sheet', 'test_utf7', 'test_case_insensitive_synonyms_and_source_label') or any(k in src or k in setup for k in ('"migrations"', 'migrations/', 'migrations.', 'import migrations', 'docs/research', 'docs/status.md', 'docs/changelog')):
                continue
            keep.addTest(t)
    walk(tests)
    return keep
