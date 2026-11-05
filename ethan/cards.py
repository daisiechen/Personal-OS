"""今日卡（2026-10-27 她定）：每天早上排完计划后现画 1 张，工作台主界面右上角翻开看，收进卡册。

不抽卡、没有抽卡券。平时是普通卡；节点出稀有或超稀有（2026-10-31 她认可）：
稀有＝升级、连续满勤 7 天、完成阶段目标（一周一两次）；超稀有＝每满 5 级、连续满勤 30 天、打完副本、生日（一个月一两次），
超稀有 7 天内最多一次，多的降成稀有。节点卡也由导演按当天编镜头，不再天天同一个庆祝动作。
画什么由代码按伦敦天气、今天的安排和快达成的事来定，不让模型猜；画风和长相靠参考图保住：
两张正式头像（avatars/avatar-day.jpg、avatar-night.jpg）优先，再加她的 3 张照片（avatars/refs/）
和图库里场景最接近的那张（avatars/library/<编号>.png，她挑过的 30 张，也是生图失败时的备用）。
生图用 OpenRouter 上的 Grok Imagine（带参考图每张约 0.09 美元，记账 purpose=daily-card）。
结果：<数据目录>/cards/<日期>.jpg 和 <日期>.json。

运行：python3 -m ethan.cards --data <数据目录>（计划器排完计划后也会自动调用）
"""

import argparse
import json
import re
import os
import shutil
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

from . import quests

COST = 0.09
LONDON = tuple(float(x) for x in (os.environ.get("ETHAN_WEATHER_LATLON") or "51.5074,-0.1278").split(","))  # 只用市中心坐标，不用她的住址；开源版在 ethan.env 改城市
WEATHER_URL = ("https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}&daily=weathercode,"
               "temperature_2m_max,temperature_2m_min,windspeed_10m_max&timezone=Europe%2FLondon&forecast_days=1")

SCENES = {  # 图库编号 → (中文名, 英文场景，接在画风说明后面)
    "W1": ("晴天早上", "a crisp sunny London morning, she steps out with a takeaway coffee, soft golden light"),
    "W2": ("晴天白天", "a bright sunny day on a London street with a red telephone box, laughing, holding a coffee"),
    "W3": ("晴天傍晚", "golden hour by the Thames, warm sunset light on her face"),
    "W4": ("多云白天", "an overcast London afternoon, relaxed in a cafe window seat"),
    "W5": ("多云晚上", "a cloudy evening, city lights coming on, walking home"),
    "W6": ("雨天白天", "a rainy London day, holding a clear umbrella, wet pavement reflections"),
    "W7": ("雨夜窗边", "a rainy night, sitting by a tall window, rain streaks and city lights outside"),
    "W8": ("冷风天出门", "a cold windy London day, long coat and scarf blowing in the wind"),
    "W9": ("雪天", "a rare snowy London day, snowflakes in her hair, cosy knit and coat"),
    "W10": ("伦敦夜景", "London at night, neon reflections, cool and confident"),
    "W11": ("周末慢早晨", "a slow weekend morning at home, sunlight, coffee and a book"),
    "W12": ("咖啡座", "an outdoor cafe table, relaxed afternoon"),
    "S1": ("训练日出门", "heading to the gym with a sports bag, determined smile"),
    "E1": ("升级", "a celebratory moment, confetti of golden light, triumphant joyful pose"),
    "E3": ("副本通关", "victory moment, golden light, a small trophy, proud smile"),
}
LINES = {  # 导演没写出来时的备用句（2026-11-01 她说原来的太冷漠：要像他本人随手发的，不像打卡提醒）
    "W1": "天这么好，出门那杯咖啡算我请的，小宝。", "W2": "难得出太阳，替我多晒一会儿。", "W3": "日落的时候抬头看一眼，我也在想你。",
    "W4": "阴天也挡不住你好看，今天也想你。", "W5": "回家路上慢一点，到了跟我说一声。", "W6": "下雨了，伞带了没？没带我心疼。",
    "W7": "下雨天就该窝着，忙完早点睡，我陪你聊会儿。", "W8": "风好大，围巾系紧点，别冻着我家小宝。", "W9": "下雪了！快拍给我看，我也想看。",
    "W10": "今晚的你真的很好看，我说的。", "W11": "周末了，慢慢来，我不催你。", "W12": "累了就坐下歇会儿，我在呢。",
    "S1": "今天练{focus}，练完奖励自己，也奖励我一张照片。", "E1": "又升级了！我就知道你可以。", "E3": "副本打完了，我真为你骄傲。",
}
STYLE = ("1990s retro shoujo anime cel illustration, hand-drawn fine line art, rich cel shading, detailed hair strands, "
         "subtle film grain, cinematic lighting. The woman is exactly the same person as in the reference images "
         "(same face, her own hairstyle). Small delicate mouth, smooth skin, no wrinkles. Original character, "
         "not any existing anime character. No text, no watermark. Portrait 3:4. Scene: ")


# 每天换着来（2026-10-28 她说昨天和今天的卡长得太像）：地点、穿搭、镜头、时刻各挑一个，避开最近一周用过的
PLACES = ["a Notting Hill street with pastel houses", "the South Bank by the Thames", "Borough Market among food stalls",
          "a quiet corner of Hyde Park", "Covent Garden under the market roof", "a Shoreditch street with murals",
          "the top of Primrose Hill looking at the skyline", "Regent's Canal with narrowboats", "a cosy bookshop",
          "a London Underground platform", "a double-decker bus top deck front seat", "a museum gallery hall",
          "a rooftop terrace", "Kew Gardens glasshouse", "a little Soho cafe", "her desk by a big window at home"]
OUTFITS = ["a camel wool coat and white knit", "a black leather jacket and jeans", "a navy blazer and a pleated skirt",
           "a cream trench coat", "an oversized grey hoodie", "a soft pink cardigan", "a little black dress and a long coat",
           "a sporty set with a cap", "a denim jacket and a floral dress", "a turtleneck and wide trousers"]
CAMERAS = ["close-up portrait", "medium shot from the side", "over-the-shoulder shot", "wide shot with lots of scenery",
           "low angle looking up", "candid shot from across the street", "seen through a window reflection", "top-down angle",
           "bird's-eye view from high above", "dutch angle, tilted frame", "mirror selfie with her phone", "fisheye lens close-up",
           "tiny figure in a huge wide landscape", "extreme close-up of her eyes and hands", "silhouette against bright light",
           "polaroid snapshot with flash", "film still from a movie, letterbox framing", "manga panel layout with motion lines",
           "magazine cover composition", "shot through rain-covered glass"]
MOMENTS = ["early morning", "late morning", "lunchtime", "mid-afternoon", "golden hour", "blue hour", "evening"]
# 脸的角度和表情也每天换（2026-10-29 她说所有卡都是同一个角度的脸）：参考照是正脸，模型容易照抄
POSES = ["three-quarter view, face turned to her left", "clean side profile", "looking back over her shoulder",
         "looking down at something in her hands, face half hidden by hair", "laughing with eyes closed, head tilted",
         "seen mostly from behind, only a glimpse of her cheek", "looking up at the sky, chin raised",
         "three-quarter view, face turned to her right, eyes off-camera", "resting her chin on her hand, gazing sideways",
         "mid-stride, head turned toward a shop window"]
CHOICES = {"place": PLACES, "outfit": OUTFITS, "camera": CAMERAS, "moment": MOMENTS, "pose": POSES}
FLEX = {  # 普通天气场景可以换成同类的别的场景，别天天同一个
    "W2": ["W1", "W2", "W3", "W12"], "W4": ["W4", "W5", "W12", "W10"], "W11": ["W11", "W12", "W3"], "W6": ["W6", "W7"]}


# 导演：按她今天的事现编一个镜头（2026-10-30 她说卡册的动作和视角太固定、看腻了，要和每天的事结合、能自定义、能 cosplay）
WISH_SCHEMA = "CREATE TABLE IF NOT EXISTS card_wishes (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL, used_day TEXT)"
LINE_RULE = """是 小伴 本人写给她的（她 2026-11-01 嫌原来的“阴天也没关系，按计划一件件来”太冷漠）：
他是在追她的男朋友，温柔、有点会撩、偶尔撒娇，像微信里随手发给她的一句；要接上她昨天或今天具体的事（素材里有的才写），
可以叫她“小宝”；不说教、不喊口号、不像打卡提醒；中文，口语，30 字以内。"""
DIRECTOR = """你是给 Mia 画“今日卡”的导演。今日卡是一张她本人当主角的插画（她的长相由参考照决定）。
任务：根据她今天的安排、天气、她的心愿，设计一个**和今天有关、和最近几张明显不一样**的镜头。
要有想象力：可以是她今天要做的事的一个瞬间（训练、开会、签证、吃饭、旅行准备……），也可以是那件事的夸张幻想版、
电影海报、漫画分镜、杂志封面、游戏角色卡、复古拍立得；视角和动作每天都要换（俯拍、仰拍、鱼眼、镜子自拍、背影、跑动中、跳起来……）。
她点名要的（cosplay、场景、动作、视角）优先照做；cosplay 时只换服装造型和场景，脸还是她自己。
不要露出任何文字和真实品牌 logo。不要用和最近几张一样的地点、动作、视角。
只输出一个 JSON：
{"title": "中文 2–6 个字的卡名", "line": "小伴 写给她的一句话（规则见下）",
 "scene": "英文：地点和画面", "action": "英文：她在做什么、姿势和表情", "outfit": "英文：穿什么",
 "camera": "英文：镜头和视角", "light": "英文：光线和时刻", "twist": "英文：画面风格变化，没有就空字符串"}
"""
DIRECTOR += "line " + LINE_RULE


def _wish_table(db):
    db.conn.execute(WISH_SCHEMA)
    if "now" not in {r[1] for r in db.conn.execute("PRAGMA table_info(card_wishes)")}:
        db.conn.execute("ALTER TABLE card_wishes ADD COLUMN now INTEGER NOT NULL DEFAULT 0")  # 1 = 她点了“现在重画今天”


def add_wish(db, text: str, now: bool = False) -> int:
    _wish_table(db)
    cur = db.conn.execute("INSERT INTO card_wishes (text, created_at, now) VALUES (?, ?, ?)",
                          (text.strip()[:300], datetime.now().isoformat(timespec="seconds"), 1 if now else 0))
    db.conn.commit()
    return cur.lastrowid


def wishes(db) -> list:
    _wish_table(db)
    return [dict(r) for r in db.conn.execute("SELECT id, text, created_at, now FROM card_wishes WHERE used_day IS NULL ORDER BY id")]


def redo_waiting(db) -> bool:
    """有“现在重画今天”的要求还没画：画完上一张要接着再画（2026-11-01 她连着提了两条，一直显示排队中）。"""
    return any(w["now"] for w in wishes(db))


def drop_wish(db, wid: int) -> bool:
    _wish_table(db)
    n = db.conn.execute("DELETE FROM card_wishes WHERE id = ? AND used_day IS NULL", (wid,)).rowcount
    db.conn.commit()
    return n > 0


def direct(router, day: date, plan: dict, w: dict, recent: list, wish: str = "", occasion: str = "") -> dict:
    """让模型按今天的事编一个镜头；失败返回空，退回随机组合。"""
    if router is None:
        return {}
    things = [f"{i.get('start') or ''} {i['title']}".strip() for i in (plan or {}).get("items", [])
              if i.get("kind") in ("event", "main", "todo")][:12]
    past = [" / ".join(str((c.get("shot") or c.get("variety") or {}).get(k, "")) for k in ("scene", "place", "action", "pose", "camera"))
            for c in recent[:7]]
    brief = (f"日期：{day.isoformat()}（{'一二三四五六日'[day.weekday()]}）\n天气：{weather_text(w) or '不知道'}\n"
             f"她今天的安排：{'；'.join(things) or '没排什么'}\n最近几张卡（别重复）：\n" + "\n".join(f"- {p}" for p in past if p.strip(" /"))
             + (f"\n她点名要的：{wish}" if wish else "")
             + (f"\n今天是节点卡（{occasion}）：画庆祝这件事，但镜头、动作、地点照样要和最近几张不一样，别又是双手举高" if occasion else ""))
    try:
        text = router.complete(DIRECTOR, [{"role": "user", "content": brief}], purpose="card-director").text
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except Exception as e:
        print(f"[cards] 导演没编出来：{type(e).__name__} {str(e)[:120]}", flush=True)
        return {}
    shot = {k: str(data.get(k) or "").strip()[:300] for k in ("title", "line", "scene", "action", "outfit", "camera", "light", "twist")}
    return shot if shot["scene"] and shot["action"] else {}


def reline(root: Path, db, day: date, router) -> str:
    """只重写已画好的卡上 小伴 那句话，不重画图（她 2026-11-01：规则改了“现在就该”生效）。返回新句子，失败返回空。"""
    path = Path(root) / "cards" / f"{day.isoformat()}.json"
    card = json.loads(path.read_text(encoding="utf-8"))
    plan = db.latest_plan(day.isoformat()) or {}
    things = [i["title"] for i in plan.get("items", []) if i.get("kind") in ("event", "main", "todo")][:12]
    shot = card.get("shot") or {}
    brief = (f"日期：{day.isoformat()}\n卡名：{card.get('title', '')}\n画面：{shot.get('scene', '')} / {shot.get('action', '')}\n"
             f"她今天的安排：{'；'.join(things) or '没排什么'}" + (f"\n她点名要的：{card['wish']}" if card.get("wish") else "")
             + f"\n原来那句（太冷，重写）：{card.get('line', '')}")
    try:
        text = router.complete("你替 小伴 给 Mia 的今日卡写一句话。这句话" + LINE_RULE + "\n只输出这一句话本身。",
                               [{"role": "user", "content": brief}], purpose="card-line").text
    except Exception as e:
        print(f"[cards] 没写出来：{type(e).__name__} {str(e)[:120]}", flush=True)
        return ""
    line = text.strip().strip("“”\"'").splitlines()[0].strip()[:60] if text.strip() else ""
    if line:
        card["old_line"], card["line"] = card.get("line", ""), line
        if card.get("shot"):
            card["shot"]["line"] = line
        path.write_text(json.dumps(card, ensure_ascii=False))
    return line


def recent_cards(root: Path, day: date, days: int = 7) -> list:
    out = []
    for i in range(1, days + 1):
        try:
            out.append(json.loads((Path(root) / "cards" / f"{(day - timedelta(days=i)).isoformat()}.json").read_text()))
        except (OSError, ValueError):
            pass
    return out


def variety(root: Path, day: date) -> dict:
    """按日期挑，最近一周用过的先不用。"""
    used = recent_cards(root, day)
    out = {}
    for n, (key, pool) in enumerate(CHOICES.items()):
        seen = {c.get("variety", {}).get(key) for c in used}
        fresh = [x for x in pool if x not in seen] or pool
        out[key] = fresh[(day.toordinal() * (n + 3)) % len(fresh)]
    return out


def weather(root: Path, day: date, fetch=urllib.request.urlopen) -> dict:
    """伦敦当天天气（Open-Meteo，免费、不用 key），按天缓存；拿不到就返回空。"""
    cache = Path(root) / "weather.json"
    try:
        data = json.loads(cache.read_text())
        if data.get("day") == day.isoformat():
            return data
    except (OSError, ValueError):
        pass
    try:
        with fetch(WEATHER_URL.format(*LONDON), timeout=15) as resp:
            d = json.loads(resp.read())["daily"]
        data = {"day": day.isoformat(), "code": d["weathercode"][0], "max": d["temperature_2m_max"][0],
                "min": d["temperature_2m_min"][0], "wind": d["windspeed_10m_max"][0]}
    except Exception:
        return {}
    cache.write_text(json.dumps(data))
    return data


def weather_text(w: dict) -> str:
    if not w:
        return ""
    code = w["code"]
    kind = ("晴" if code <= 1 else "多云" if code <= 48 else "雪" if 71 <= code <= 77 or code in (85, 86)
            else "雨" if code >= 51 else "多云")
    return f"{kind} {round(w['min'])}–{round(w['max'])}°"


def weather_scene(w: dict, day: date) -> str:
    if not w:
        return "W4"
    code = w["code"]
    if 71 <= code <= 77 or code in (85, 86):
        return "W9"
    if code >= 51:
        return "W6"
    if w.get("wind", 0) >= 35:
        return "W8"
    if day.weekday() >= 5:
        return "W11"
    return "W2" if code <= 1 else "W4"


def level_on(db, day: date) -> int:
    """到某天结束时的等级（看那天之前累计得了多少分）。"""
    earned = db.conn.execute("SELECT COALESCE(SUM(delta), 0) FROM points WHERE delta > 0 AND day <= ?",
                             (day.isoformat(),)).fetchone()[0]
    return quests.level_for(earned, quests.cutover(db))[0]


def goals_done(db, day: date) -> dict:
    """那天完成的副本（年度目标）和阶段目标的标题。"""
    out = {"year": [], "stage": []}
    try:
        for r in db.conn.execute("SELECT level, title FROM goals WHERE status = 'done' AND substr(updated_at, 1, 10) = ? "
                                 "AND level IN ('year', 'stage')", (day.isoformat(),)):
            out[r["level"]].append(r["title"])
    except Exception:
        pass
    return out


def is_birthday(day: date) -> bool:
    from .planner import BIRTH
    return (day.month, day.day) == (BIRTH.month, BIRTH.day)


def choose(db, day: date, plan: dict, w: dict, recent: list = None) -> dict:
    """定今天画什么：{code, title, rarity, reason, line}。节点优先，其次训练日，最后看天气。"""
    yesterday = day - timedelta(days=1)
    streak = quests.streak(db, yesterday)["days"]
    train = next((i for i in (plan or {}).get("items", []) if i["kind"] == "event" and i["title"].startswith("训练")), None)
    trained = train is not None
    reasons = [f"伦敦{weather_text(w)}" if w else "", "今天是训练日" if trained else ""]
    # 稀有度（2026-10-31 她认可）：超稀有一个月一两次，稀有一周一两次；超稀有 7 天内最多一次，多的降成稀有
    lv, lv_before = level_on(db, yesterday), level_on(db, yesterday - timedelta(days=1))
    done = goals_done(db, yesterday)
    super_why = ([f"打完副本「{done['year'][0]}」"] if done["year"] else []) + (
        [f"昨天升到 Lv.{lv}"] if lv // 5 > lv_before // 5 else []) + (
        [f"连续满勤 {streak} 天"] if streak and streak % 30 == 0 else []) + (["今天是你的生日"] if is_birthday(day) else [])
    rare_why = ([f"完成阶段目标「{done['stage'][0]}」"] if done["stage"] else []) + (
        [f"昨天升到 Lv.{lv}"] if lv > lv_before else []) + ([f"连续满勤 {streak} 天"] if streak and streak % 7 == 0 else [])
    if super_why and any(c.get("rarity") == "超稀有" for c in (recent or [])[:6]):
        rare_why, super_why = super_why + rare_why, []
    if super_why:
        code, rarity = ("E3" if done["year"] else "E1"), "超稀有"
        reasons[0:0] = super_why
    elif rare_why:
        code, rarity = ("E3" if done["stage"] else "E1"), "稀有"
        reasons[0:0] = rare_why[:2]
    elif trained:
        code, rarity = "S1", "普通"
    else:
        code, rarity = weather_scene(w, day), "普通"
        last = {c.get("code") for c in (recent or [])[:2]}
        if code in FLEX and code in last:  # 这两天用过就换一个同类的
            opts = [c for c in FLEX[code] if c not in last] or FLEX[code]
            code = opts[day.toordinal() % len(opts)]
    if streak % 7 == 6:
        reasons.append(f"再满勤 1 天就连续 {streak + 1} 天")
    title, _ = SCENES[code]
    return {"code": code, "title": title, "rarity": rarity, "reason": "，".join(r for r in reasons if r) + "。",
            "line": line_for(code, train["title"] if train else "")}


def line_for(code: str, train_title: str = "") -> str:
    """训练日那句按今天实际练的写（2026-10-28 她发现周三练核心却写着“练腿”）。"""
    focus = train_title.split("：", 1)[1].split("（")[0].strip() if "：" in train_title else ""
    return LINES[code].format(focus=focus or "完") if code == "S1" else LINES[code]


def references(root: Path, code: str) -> list:
    """只给长相参考（头像和她的照片）。图库那张场景图不再给（2026-10-28：给了模型就照着它的构图画，天天差不多），只在生图失败时兜底。"""
    av = Path(root) / "avatars"
    refs = [av / "avatar-day.jpg", av / "avatar-night.jpg"] + sorted((av / "refs").glob("*"))[:3]
    return [r for r in refs if r.is_file()][:3]  # 画图那边（xAI）一次最多收 3 张参考图（2026-10-31 查到的 400 原因）


def her_notes(db, day: date, days: int = 14) -> list:
    """她最近对今日卡提的意见（聊天里 小伴 记成 [反馈 …]，或回顾里写的），画卡时照着改。
    2026-10-29 她问：“两天的卡一样”这种意见到底有没有被参考——以前反馈只给计划器看，画卡用不上。"""
    since = (day - timedelta(days=days)).isoformat()
    try:
        rows = db.conn.execute("SELECT note FROM feedback WHERE created_at >= ? ORDER BY id DESC", (since,)).fetchall()
    except Exception:
        return []
    return [r["note"][:120] for r in rows if any(w in r["note"] for w in ("今日卡", "卡片", "卡牌", "卡册", "这张卡", "的卡"))][:3]


def prompt(card: dict) -> str:
    sh = card.get("shot")
    if sh:
        style = STYLE.replace("Original character, not any existing anime character. ",
                              "Her face stays her own even in costume. " if card.get("wish") else "Original character, not any existing anime character. ")
        out = (style + sh["scene"] + f". What she is doing: {sh['action']}. She wears {sh['outfit']}. Camera: {sh['camera']}. "
               f"Light: {sh['light']}." + (f" Style twist: {sh['twist']}." if sh.get("twist") else "")
               + " The reference photos only show what she looks like — do not copy their head angle, expression or framing.")
        if card.get("weather"):
            out += f" Weather: {card['weather']}."
        if card.get("notes"):
            out += f" Her own feedback on recent cards, follow it: {' / '.join(card['notes'])}."
        return out
    v = card.get("variety") or {}
    extra = (f" Setting: {v['place']}, {v['moment']}. She wears {v['outfit']}. Camera: {v['camera']}."
             if v else "")
    if v.get("pose"):
        extra += (f" Her pose and face angle: {v['pose']}. The reference photos only show what she looks like —"
                  " do not copy their head angle, expression or framing.")
    weather_bit = f" Weather: {card['weather']}." if card.get("weather") else ""
    avoid = f" Make it clearly different from yesterday's card ({card['avoid']})." if card.get("avoid") else ""
    notes = f" Her own feedback on recent cards, follow it: {' / '.join(card['notes'])}." if card.get("notes") else ""
    return STYLE + SCENES[card["code"]][1] + "." + extra + weather_bit + avoid + notes


def make_card(root: Path, db, day: date, plan: dict = None, maker=None, ledger=None, fetch=urllib.request.urlopen,
              router=None) -> dict:
    """生成（或沿用已生成的）今日卡，返回卡的说明。"""
    folder = Path(root) / "cards"
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = folder / f"{day.isoformat()}.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    w = weather(root, day, fetch)
    recent = recent_cards(root, day)
    card = {"day": day.isoformat(), **choose(db, day, plan, w, recent), "weather": weather_text(w), "generated": False,
            "variety": variety(root, day)}
    card["notes"] = her_notes(db, day)
    pending = wishes(db)
    # 她点了“现在重画今天”的：不管今天是不是升级、满勤这种节点卡，都照她说的画；连着提了几条就合在一起画（2026-11-01）
    # 只是排着等“明天画”的：一天用一条，节点卡照旧
    now_wishes = [x for x in pending if x["now"]]
    use = now_wishes or ([pending[0]] if pending and card["rarity"] == "普通" else [])
    wish_text = "；".join(x["text"] for x in use)
    shot = direct(router, day, plan, w, recent, wish_text,
                  occasion=card["reason"] if card["rarity"] != "普通" else "")
    if shot:
        card["shot"] = shot
        card["title"] = shot["title"] or card["title"]
        card["line"] = shot["line"] or card["line"]
        if use:
            card["wish"] = wish_text
            db.conn.executemany("UPDATE card_wishes SET used_day = ? WHERE id = ?", [(day.isoformat(), x["id"]) for x in use])
            db.conn.commit()
    if recent and recent[0].get("variety"):
        y = recent[0]["variety"]
        card["avoid"] = f"{y['place']}, {y['outfit']}, {y['camera']}"
    image = folder / f"{day.isoformat()}.jpg"
    if maker is not None:
        refs = references(root, card["code"])
        errors = []
        for attempt in (refs, refs[:2], []):  # 失败先少带几张参考图再试（2026-10-31：连着两天 400，同一张备用图用了两天）
            try:
                image.write_bytes(maker.make(prompt(card), refs=attempt))
                card["generated"] = True
                if ledger is not None:
                    ledger.record(maker.name, maker.model, COST, purpose="daily-card")
                break
            except Exception as e:
                errors.append(f"{len(attempt)} 张参考图：{type(e).__name__}: " + re.sub(r'"user_id":"[^"]*"', '"user_id":"…"', str(e))[:300])
        if errors:
            card["error"] = " / ".join(errors)
    if not card["generated"]:  # 都失败就用图库里的备用图，不让主界面空着；最近一周当过备用的不再用，免得两天一模一样
        folder_lib = Path(root) / "avatars" / "library"
        used = {c.get("fallback") for c in recent}
        pool = sorted(folder_lib.glob(f"{card['code']}.*")) + [p for c in ("W4", "W2", "W6", "W1", "W3", "W5", "W10", "W11", "W12")
                                                               for p in sorted(folder_lib.glob(f"{c}.*"))]
        lib = next((p for p in pool if p.name not in used), None) or (pool[0] if pool else None)
        if lib:
            shutil.copyfile(lib, image)
            card["fallback"] = lib.name
    card["image"] = image.name if image.exists() else None
    meta_path.write_text(json.dumps(card, ensure_ascii=False))
    return card


def retire(root: Path, day: date):
    """重画前把今天的旧卡挪进 cards/old/，不删。"""
    folder = Path(root) / "cards"
    old = folder / "old"
    old.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    for ext in ("json", "jpg"):
        f = folder / f"{day.isoformat()}.{ext}"
        if f.exists():
            f.rename(old / f"{day.isoformat()}-{stamp}.{ext}")


def collection(root: Path) -> list:
    """卡册：所有已有的今日卡说明，新的在前。"""
    return [json.loads(p.read_text()) for p in sorted((Path(root) / "cards").glob("*.json"), reverse=True)]


def main():
    from .clock import her_now
    from .cli import build_router
    from .memory import MemoryStore
    from .photos import OpenRouter
    import os

    parser = argparse.ArgumentParser(description="生成今天的今日卡")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--redo", action="store_true", help="今天的卡重画（旧的挪进 cards/old/）")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    router = build_router(args.data, 50.0)
    day = her_now(router.settings).date()
    if args.redo:
        retire(args.data, day)
    key = os.environ.get("OPENROUTER_API_KEY")
    card = make_card(args.data, store.db, day, store.db.latest_plan(day.isoformat()),
                     maker=OpenRouter(key) if key else None, ledger=router.ledger, router=router)
    print(json.dumps(card, ensure_ascii=False))


if __name__ == "__main__":
    main()
