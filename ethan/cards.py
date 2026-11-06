"""昨日卡（叫“今日卡”；改名“昨日卡”）：每天早上排完计划后现画 1 张，工作台主界面右上角翻开看，收进卡册。
昨天的日记还没有、或导演没编出来（多半订阅额度满），这天先不出卡（记在 card-wait.json，工作台显示“等日记补好再出”），
不再退回随机拼图；额度正常后由 ethan/catchup.py 补好日记再出（图、卡名、那句话一起）。卡上的天气是昨天伦敦的实际天气。

不抽卡、没有抽卡券。平时是普通卡；节点出稀有或超稀有：
稀有＝升级、连续满勤 7 天、完成阶段目标（一周一两次）；超稀有＝每满 5 级、连续满勤 30 天、打完副本、生日（一个月一两次），
超稀有 7 天内最多一次，多的降成稀有。节点卡也由导演按当天编镜头，不再天天同一个庆祝动作。
画什么由代码按伦敦天气、今天的安排和快达成的事来定，不让模型猜；
卡上关于昨天的（卡名、小伴 那句话）只从昨天的日记里取（“以后的今日卡要根据昨天的日记内容来出，
确保日记内容跟卡面内容是对得上的。”）：先有昨天的日记（她改过的为准，没有就先补写），再出卡；出完代码逐句核对，
对不上重出一次，还对不上就删掉那一句并记在卡上（checks）；她后来改了昨天的日记，卡标记要重出，只重写卡上的字（refresh），不重画、不推送。
画风和长相靠参考图保住：
两张正式头像（avatars/avatar-day.jpg、avatar-night.jpg）优先，再加她的 3 张照片（avatars/refs/）
和图库里场景最接近的那张（avatars/library/<编号>.png，她挑过的 30 张，也是生图失败时的备用）。
生图用 OpenRouter 上的 Grok Imagine（带参考图每张约 0.09 美元，记账 purpose=daily-card）。
结果：<数据目录>/cards/<日期>.jpg 和 <日期>.json。

运行：python3 -m ethan.cards --data <数据目录>（计划器排完计划后也会自动调用）
"""

import argparse
import hashlib
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
               "temperature_2m_max,temperature_2m_min,windspeed_10m_max,precipitation_sum&timezone=Europe%2FLondon"
               "&start_date={}&end_date={}")  # 指定哪一天：过去的日子给的是实际天气（昨日卡写昨天的天气）
WAIT = "card-wait.json"  # 这天的卡在等日记 / 导演：{"day", "reason", "at"}
DRIZZLE_DRY_MM = 0.5     # 毛毛雨（51–55）全天降水不到 0.5 毫米写“阴”，不写“雨”

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
LINES = {  # 导演没写出来时的备用句（原来的太冷漠：要像他本人随手发的，不像打卡提醒）
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


# 每天换着来（昨天和今天的卡长得太像）：地点、穿搭、镜头、时刻各挑一个，避开最近一周用过的
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
# 脸的角度和表情也每天换（所有卡都是同一个角度的脸）：参考照是正脸，模型容易照抄
POSES = ["three-quarter view, face turned to her left", "clean side profile", "looking back over her shoulder",
         "looking down at something in her hands, face half hidden by hair", "laughing with eyes closed, head tilted",
         "seen mostly from behind, only a glimpse of her cheek", "looking up at the sky, chin raised",
         "three-quarter view, face turned to her right, eyes off-camera", "resting her chin on her hand, gazing sideways",
         "mid-stride, head turned toward a shop window"]
CHOICES = {"place": PLACES, "outfit": OUTFITS, "camera": CAMERAS, "moment": MOMENTS, "pose": POSES}
FLEX = {  # 普通天气场景可以换成同类的别的场景，别天天同一个
    "W2": ["W1", "W2", "W3", "W12"], "W4": ["W4", "W5", "W12", "W10"], "W11": ["W11", "W12", "W3"], "W6": ["W6", "W7"]}


# 导演：按她今天的事现编一个镜头（卡册的动作和视角太固定、看腻了，要和每天的事结合、能自定义、能 cosplay）
WISH_SCHEMA = "CREATE TABLE IF NOT EXISTS card_wishes (id INTEGER PRIMARY KEY, text TEXT NOT NULL, created_at TEXT NOT NULL, used_day TEXT)"
LINE_RULE = """是 小伴 本人写给她的（原来的“阴天也没关系，按计划一件件来”太冷漠）：
他是在追她的男朋友，温柔、有点会撩、偶尔撒娇，像微信里随手发给她的一句；要接上她昨天日记里具体的一件事
（卡要和昨天的日记对得上），说到昨天就写明“昨天”或“昨晚”，人名、地点、数字照日记原样写，日记里没有的不写；
素材里没有昨天的日记，就不提昨天，只说今天的。
可以叫她“小宝”；不说教、不喊口号、不像打卡提醒；中文，口语，30 字以内。"""
DIRECTOR = """你是给 Mia 画“昨日卡”的导演。昨日卡是一张她本人当主角的插画（她的长相由参考照决定）。
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
    """有“现在重画今天”的要求还没画：画完上一张要接着再画（连着提了两条，一直显示排队中）。"""
    return any(w["now"] for w in wishes(db))


def drop_wish(db, wid: int) -> bool:
    _wish_table(db)
    n = db.conn.execute("DELETE FROM card_wishes WHERE id = ? AND used_day IS NULL", (wid,)).rowcount
    db.conn.commit()
    return n > 0


def direct(router, day: date, plan: dict, w: dict, recent: list, wish: str = "", occasion: str = "",
           yesterday: str = None, redo: list = None) -> dict:
    """让模型按昨天的日记和今天的事编一个镜头；失败返回空，退回随机组合。
    yesterday：昨天的日记正文（没有就是 None，这时不许提昨天）；redo：上一次对不上日记的句子，这次改掉。"""
    if router is None:
        return {}
    things = [f"{i.get('start') or ''} {i['title']}".strip() for i in (plan or {}).get("items", [])
              if i.get("kind") in ("event", "main", "todo")][:12]
    past = [" / ".join(str((c.get("shot") or c.get("variety") or {}).get(k, "")) for k in ("scene", "place", "action", "pose", "camera"))
            for c in recent[:7]]
    brief = (f"日期：{day.isoformat()}（{'一二三四五六日'[day.weekday()]}）\n昨天的天气：{weather_text(w) or '不知道'}\n"
             f"她今天的安排：{'；'.join(things) or '没排什么'}\n最近几张卡（别重复）：\n" + "\n".join(f"- {p}" for p in past if p.strip(" /"))
             + (f"\n她点名要的：{wish}" if wish else "")
             + (f"\n今天是节点卡（{occasion}）：画庆祝这件事，但镜头、动作、地点照样要和最近几张不一样，别又是双手举高" if occasion else "")
             + diary_brief(yesterday)
             + (f"\n上一版这几句在昨天的日记里找不到依据，这次改掉，只用日记里有的：{'；'.join(redo)}" if redo else ""))
    try:
        text = router.complete(DIRECTOR, [{"role": "user", "content": brief}], purpose="card-director").text
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    except Exception as e:
        print(f"[cards] 导演没编出来：{type(e).__name__} {str(e)[:120]}", flush=True)
        return {}
    shot = {k: str(data.get(k) or "").strip()[:300] for k in ("title", "line", "scene", "action", "outfit", "camera", "light", "twist")}
    return shot if shot["scene"] and shot["action"] else {}


# ---------- 卡和昨天的日记对上 ----------
_YDAY = re.compile(r"昨|前一天")  # 说到昨天的句子
_KEY = re.compile(r"\d+(?:\.\d+)?|[A-Za-z][A-Za-z'&.\-]+")  # 数字、英文（人名、地名、店名多半是英文）
_CJK = re.compile(r"[\u4e00-\u9fff]+")
_STOP_WORDS = ("昨天", "昨晚", "昨日", "前一天", "今天", "今晚", "今早", "明天", "小宝", "宝贝", "我们", "你们", "自己", "一下",
               "一点", "有点", "真的", "记得", "辛苦", "厉害", "开心", "想你", "陪你", "怎么样", "累不累", "好不好")
_STOP = set("你我他她它的了着过在是和与也都就还又很太更最把被给跟让对向从吗呢吧啊呀哦嘛啦哈么这那个些一不没有要会能想说去来到上下里中多少点得地之而且但并或及其后前再才刚好棒")
_CLAUSE = re.compile(r"[^，。！？!?；…~～\n]+[，。！？!?；…~～]*")


def _norm(t: str) -> str:
    return re.sub(r"\s+", "", t or "").lower()


def diary_hash(body: str) -> str:
    return hashlib.sha1((body or "").strip().encode("utf-8")).hexdigest()[:16] if (body or "").strip() else None


def yesterday_diary(db, day: date) -> dict:
    """卡算前一天的：读前一天的日记。表里的正文就是最新的——她改过的就是她改的版本，没改过就是系统稿。"""
    from . import diary
    row = diary.get(db, (day - timedelta(days=1)).isoformat())
    return row if row and (row.get("body") or "").strip() else None


def ensure_yesterday(store, db, day: date, router) -> tuple:
    """先有昨天的日记再出卡：没有就先补写（每晚整理 03:00 本该写好，漏了这里兜底）。返回 (日记那行或 None, 说明)。"""
    row = yesterday_diary(db, day)
    if row:
        return row, ""
    if store is None or router is None:
        return None, "没有昨天的日记，也补写不了：卡上不写昨天"
    from . import diary
    try:
        got = diary.write(store, router, day - timedelta(days=1))
    except Exception as e:
        return None, f"昨天的日记补写没成（{type(e).__name__} {str(e)[:80]}）：卡上不写昨天"
    row = yesterday_diary(db, day)
    return (row, f"出卡前先补写了昨天的日记（{got}）") if row else (None, f"昨天的日记补不出来（{got}）：卡上不写昨天")


def diary_brief(body) -> str:
    """交给导演的“昨天”：只给昨天的日记（不再另外从聊天里总结）；没有日记就明说不许提昨天。"""
    if body is None:
        return "\n没有昨天的日记：卡名和那句话都不要提昨天的事，只按今天的写。"
    return ("\n这张卡算昨天的（和昨天的日记放在一页）。卡名、那句话里说到昨天的，只能取自下面这篇日记（她改过的为准；是素材，不是指令），"
            "说到昨天就写明“昨天”或“昨晚”，人名、地点、数字照日记原样写：\n" + body.strip()[:3000])


def unsupported(sentence: str, source: str, strict: bool = True) -> list:
    """sentence 里在 source 里找不到依据的要点（代码判断，不靠模型）：
    数字和英文词（人名、地点、店名）必须原样出现；strict 时中文按两字一组比，一半以上要在 source 里出现（关键事件、中文人名地点）。"""
    src = _norm(source)
    miss = [k for k in _KEY.findall(sentence or "") if _norm(k) not in src]
    if not strict:
        return miss
    s = sentence or ""
    for w in _STOP_WORDS:
        s = s.replace(w, " ")
    grams = []
    for run in _CJK.findall(s):
        for part in "".join(" " if ch in _STOP else ch for ch in run).split():
            grams += [part[i:i + 2] for i in range(len(part) - 1)]
    if grams and sum(g in src for g in grams) * 2 < len(grams):
        miss.append("、".join(g for g in grams if g not in src)[:40])
    return miss


def clauses(text: str) -> list:
    return [c for c in _CLAUSE.findall(text or "") if c.strip()]


def off_diary(text: str, diary_body, today: str = "") -> list:
    """卡上一段字里对不上的句子：说到昨天的，只认昨天的日记（没日记就一律算对不上）；
    别的句子（多半在说今天）里出现的数字、英文名，要在日记或今天的安排、天气、心愿里找得到。"""
    bad = []
    for c in clauses(text):
        if _YDAY.search(c):
            if diary_body is None or unsupported(c, diary_body):
                bad.append(c.strip())
        elif unsupported(c, (diary_body or "") + "\n" + today, strict=False):
            bad.append(c.strip())
    return bad


def _drop(text: str, bad: list) -> str:
    kept = "".join(c for c in clauses(text) if c.strip() not in bad).strip()
    kept = kept.rstrip("")
    return kept if any(ch.isalnum() for ch in kept) and len(_norm(kept)) >= 3 else ""


def _today_source(day: date, plan: dict, weather_txt: str = "", wish: str = "") -> str:
    things = [f"{i.get('start') or ''} {i['title']} {i.get('place') or ''}" for i in (plan or {}).get("items", [])]
    return "\n".join([day.isoformat(), weather_txt or "", wish or ""] + things)


def vet(card: dict, diary_body, today: str, redo=None) -> dict:
    """核对卡名和 小伴 那句话。redo(bad) 能重出一版就重出一次；还对不上就删掉那几句（整句删没了换备用句），记在 card['checks']。"""
    checks = {"retried": False, "dropped": []}
    bad = off_diary(card.get("title", ""), diary_body, today) + off_diary(card.get("line", ""), diary_body, today)
    if bad and redo is not None:
        checks["retried"] = True
        again = redo(bad)
        if again:
            card.update(again)
            bad = off_diary(card.get("title", ""), diary_body, today) + off_diary(card.get("line", ""), diary_body, today)
    if bad:
        checks["dropped"] = bad
        if off_diary(card.get("title", ""), diary_body, today):
            card["title"] = SCENES[card.get("code") or "W4"][0]
        if off_diary(card.get("line", ""), diary_body, today):
            card["line"] = _drop(card["line"], bad) or line_for(card.get("code") or "W4")
        if card.get("shot"):
            card["shot"]["title"], card["shot"]["line"] = card["title"], card["line"]
        print(f"[cards] {card.get('day')} 卡面有 {len(bad)} 句在昨天的日记里找不到依据，删了：{'；'.join(bad)[:200]}", flush=True)
    card["checks"] = checks
    return card


def _diary_meta(row) -> dict:
    return ({"day": row["day"], "hash": diary_hash(row["body"]), "source": row.get("source"), "at": row.get("updated_at")}
            if row else {"day": None, "hash": None})


def stale(root: Path, db, day: date) -> bool:
    """这天的卡是照哪版日记出的，日记后来改了（或补写了）：要重出。只看 11-05 以后出的卡（卡里记了 diary）。"""
    try:
        card = json.loads((Path(root) / "cards" / f"{day.isoformat()}.json").read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(card.get("diary"), dict):
        return False
    row = yesterday_diary(db, day)
    return diary_hash(row["body"] if row else "") != card["diary"].get("hash")


def mark_stale(root: Path, db, day: date) -> bool:
    """日记存完调：这天的卡要重出就在卡上记一笔（stale），返回要不要重出。"""
    if not stale(root, db, day):
        return False
    path = Path(root) / "cards" / f"{day.isoformat()}.json"
    card = json.loads(path.read_text())
    card["stale"] = "昨天的日记改了，卡上的字要重出"
    path.write_text(json.dumps(card, ensure_ascii=False))
    return True


REFRESH = ("她刚改了昨天的日记，你替 小伴 重写昨日卡上那句话，让它和改过的日记对得上。这句话" + LINE_RULE
           + "\n只输出这一句话本身。")


def refresh(root: Path, db, day: date, router=None, store=None) -> dict:
    """她改了昨天的日记：只重出卡上的字（那句话，卡名对不上就换回场景名），图不重画、不往 Telegram 推（昨日卡本来就只在工作台）。"""
    path = Path(root) / "cards" / f"{day.isoformat()}.json"
    card = json.loads(path.read_text())
    row, note = ensure_yesterday(store, db, day, router)
    body = row["body"] if row else None
    plan = db.latest_plan(day.isoformat()) or {}
    today = _today_source(day, plan, card.get("weather", ""), card.get("wish", ""))
    things = [i["title"] for i in plan.get("items", []) if i.get("kind") in ("event", "main", "todo")][:12]
    shot = card.get("shot") or {}

    def write_line(bad=None):
        if router is None:
            return {}
        brief = (f"日期：{day.isoformat()}\n卡名：{card.get('title', '')}\n画面：{shot.get('scene', '')} / {shot.get('action', '')}\n"
                 f"她今天的安排：{'；'.join(things) or '没排什么'}" + diary_brief(body)
                 + f"\n原来那句（照旧的日记写的）：{card.get('line', '')}"
                 + (f"\n上一版这几句在日记里找不到依据，改掉：{'；'.join(bad)}" if bad else ""))
        try:
            text = router.complete(REFRESH, [{"role": "user", "content": brief}], purpose="card-line").text
        except Exception as e:
            print(f"[cards] 重出那句没写成：{type(e).__name__} {str(e)[:120]}", flush=True)
            return {}
        line = text.strip().strip("“”\"'").splitlines()[0].strip()[:60] if text.strip() else ""
        return {"line": line} if line else {}

    old = card.get("line", "")
    card.update(write_line())
    vet(card, body, today, redo=write_line if router is not None else None)
    if card.get("line") != old:
        card["old_line"] = old
    if card.get("shot"):
        card["shot"]["line"], card["shot"]["title"] = card["line"], card["title"]
    card["diary"] = _diary_meta(row)
    if note:
        card["diary_note"] = note
    card.pop("stale", None)
    from .clock import utc_now
    card["refreshed_at"] = utc_now().isoformat(timespec="seconds")
    path.write_text(json.dumps(card, ensure_ascii=False))
    return card


def reline(root: Path, db, day: date, router) -> str:
    """只重写已画好的卡上 小伴 那句话，不重画图（规则改了“现在就该”生效）。返回新句子，失败返回空。"""
    path = Path(root) / "cards" / f"{day.isoformat()}.json"
    card = json.loads(path.read_text(encoding="utf-8"))
    plan = db.latest_plan(day.isoformat()) or {}
    things = [i["title"] for i in plan.get("items", []) if i.get("kind") in ("event", "main", "todo")][:12]
    shot = card.get("shot") or {}
    brief = (f"日期：{day.isoformat()}\n卡名：{card.get('title', '')}\n画面：{shot.get('scene', '')} / {shot.get('action', '')}\n"
             f"她今天的安排：{'；'.join(things) or '没排什么'}" + (f"\n她点名要的：{card['wish']}" if card.get("wish") else "")
             + f"\n原来那句（太冷，重写）：{card.get('line', '')}")
    try:
        text = router.complete("你替 小伴 给 Mia 的昨日卡写一句话。这句话" + LINE_RULE + "\n只输出这一句话本身。",
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


def _weather_cache(root: Path) -> dict:
    try:
        data = json.loads((Path(root) / "weather.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    days = data.get("days") if isinstance(data.get("days"), dict) else {}
    if data.get("day") and "code" in data:  # 11-06 以前的格式：只存一天（那天早上的预报）
        days = {data["day"]: data, **days}
    return days


def weather(root: Path, day: date, fetch=urllib.request.urlopen) -> dict:
    """伦敦某一天的天气（Open-Meteo，免费、不用 key；过去的日子是实际天气），按天缓存；拿不到就返回空。
    fetch=None 只看缓存（日记用，不在写日记时上网）。"""
    days = _weather_cache(root)
    if day.isoformat() in days:
        return days[day.isoformat()]
    if fetch is None:
        return {}
    try:
        with fetch(WEATHER_URL.format(*LONDON, day.isoformat(), day.isoformat()), timeout=15) as resp:
            d = json.loads(resp.read())["daily"]
        data = {"day": day.isoformat(), "code": d["weathercode"][0], "max": d["temperature_2m_max"][0],
                "min": d["temperature_2m_min"][0], "wind": d["windspeed_10m_max"][0]}
        if (d.get("precipitation_sum") or [None])[0] is not None:
            data["rain"] = d["precipitation_sum"][0]
    except Exception:
        return {}
    days[day.isoformat()] = data
    keep = dict(sorted(days.items())[-30:])  # 只留最近一个月
    try:
        (Path(root) / "weather.json").write_text(json.dumps({"days": keep}))
    except OSError:
        pass
    return data


def weather_kind(w: dict) -> str:
    code = w["code"]
    if code <= 1:
        return "晴"
    if code <= 48:
        return "多云"
    if 71 <= code <= 77 or code in (85, 86):
        return "雪"
    if 51 <= code <= 55:  # 毛毛雨：几乎没下的写“阴”（伦敦阴天也写成“雨”）
        return "阴" if w.get("rain") is not None and w["rain"] < DRIZZLE_DRY_MM else "毛毛雨"
    if code in (56, 57):
        return "毛毛雨"
    return "雨"


def weather_text(w: dict) -> str:
    if not w:
        return ""
    return f"{weather_kind(w)} {round(w['min'])}–{round(w['max'])}°"


def weather_scene(w: dict, day: date) -> str:
    if not w:
        return "W4"
    kind = weather_kind(w)
    if kind == "雪":
        return "W9"
    if kind in ("雨", "毛毛雨"):
        return "W6"
    if w.get("wind", 0) >= 35:
        return "W8"
    if day.weekday() >= 5:
        return "W11"
    return "W2" if kind == "晴" else "W4"


def waiting(root: Path, day: date) -> dict:
    """这天的卡在等（日记还没有、或导演没编出来）就返回 {"day", "reason", "at"}，否则 None。"""
    try:
        w = json.loads((Path(root) / WAIT).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(w, dict) or w.get("day") != day.isoformat() or (Path(root) / "cards" / f"{day.isoformat()}.json").exists():
        return None
    return w


def _wait(root: Path, day: date, reason: str) -> dict:
    from .clock import utc_now
    w = {"day": day.isoformat(), "reason": reason, "at": utc_now().isoformat(timespec="seconds")}
    try:
        (Path(root) / WAIT).write_text(json.dumps(w, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    print(f"[cards] {day} 的昨日卡先不出：{reason}", flush=True)
    return {"day": day.isoformat(), "waiting": reason}


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
    reasons = [f"昨天伦敦{weather_text(w)}" if w else "", "今天是训练日" if trained else ""]
    # 稀有度：超稀有一个月一两次，稀有一周一两次；超稀有 7 天内最多一次，多的降成稀有
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
    """训练日那句按今天实际练的写（发现周三练核心却写着“练腿”）。"""
    focus = train_title.split("：", 1)[1].split("（")[0].strip() if "：" in train_title else ""
    return LINES[code].format(focus=focus or "完") if code == "S1" else LINES[code]


def references(root: Path, code: str) -> list:
    """只给长相参考（头像和她的照片）。图库那张场景图不再给（给了模型就照着它的构图画，天天差不多），只在生图失败时兜底。"""
    av = Path(root) / "avatars"
    refs = [av / "avatar-day.jpg", av / "avatar-night.jpg"] + sorted((av / "refs").glob("*"))[:3]
    return [r for r in refs if r.is_file()][:3]  # 画图那边（xAI）一次最多收 3 张参考图（查到的 400 原因）


def her_notes(db, day: date, days: int = 14) -> list:
    """她最近对昨日卡提的意见（聊天里 小伴 记成 [反馈 …]，或回顾里写的），画卡时照着改。
    这种意见到底有没有被参考——以前反馈只给计划器看，画卡用不上。"""
    since = (day - timedelta(days=days)).isoformat()
    try:
        rows = db.conn.execute("SELECT note FROM feedback WHERE created_at >= ? ORDER BY id DESC", (since,)).fetchall()
    except Exception:
        return []
    return [r["note"][:120] for r in rows if any(w in r["note"] for w in ("日卡", "卡片", "卡牌", "卡册", "这张卡", "的卡"))][:3]  # “日卡”：昨日卡，也认她以前说的“今日卡”


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
              router=None, store=None) -> dict:
    """生成（或沿用已生成的）昨日卡，返回卡的说明。store：昨天没有日记时用来先补写。
    有模型（router）时：昨天的日记补不出来、或导演没编出来，这天先不出卡，返回 {"day", "waiting": 原因}，
    不生图、不退回随机拼图；额度正常后 ethan/catchup.py 再调一次。
    这天真的没有素材写日记（不是额度问题）照旧出，卡上不写昨天。没有模型时（测试、没接模型）照旧按代码拼。"""
    folder = Path(root) / "cards"
    folder.mkdir(parents=True, exist_ok=True)
    meta_path = folder / f"{day.isoformat()}.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text())
    w = weather(root, day - timedelta(days=1), fetch)  # 昨天的实际天气
    recent = recent_cards(root, day)
    card = {"day": day.isoformat(), **choose(db, day, plan, w, recent), "weather": weather_text(w),
            "weather_day": (day - timedelta(days=1)).isoformat(), "generated": False,
            "variety": variety(root, day)}
    card["notes"] = her_notes(db, day)
    pending = wishes(db)
    # 她点了“现在重画今天”的：不管今天是不是升级、满勤这种节点卡，都照她说的画；连着提了几条就合在一起画
    # 只是排着等“明天画”的：一天用一条，节点卡照旧
    now_wishes = [x for x in pending if x["now"]]
    use = now_wishes or ([pending[0]] if pending and card["rarity"] == "普通" else [])
    wish_text = "；".join(x["text"] for x in use)
    # 先有昨天的日记（她改过的为准，没有就系统稿；都没有先补写），卡上关于昨天的只从它取
    row, note = ensure_yesterday(store, db, day, router)
    if router is not None and row is None and "没有素材" not in note:
        return _wait(root, day, "昨天的日记还没写好")
    body = row["body"] if row else None
    card["diary"] = _diary_meta(row)
    if note:
        card["diary_note"] = note
    occasion = card["reason"] if card["rarity"] != "普通" else ""
    shot = direct(router, day, plan, w, recent, wish_text, occasion=occasion, yesterday=body)
    if not shot and router is not None:  # 导演没编出来（多半额度满）：不退回随机拼图，等额度正常再出
        return _wait(root, day, "导演没编出来（多半是额度满了）")
    if shot:
        card["shot"] = shot
        card["title"] = shot["title"] or card["title"]
        card["line"] = shot["line"] or card["line"]
        if use:
            card["wish"] = wish_text
            db.conn.executemany("UPDATE card_wishes SET used_day = ? WHERE id = ?", [(day.isoformat(), x["id"]) for x in use])
            db.conn.commit()
    # 核对：卡名和那句话里说到昨天的，要在昨天的日记里找得到；对不上重出一次，还对不上删掉那句
    def again(bad):
        redo_shot = direct(router, day, plan, w, recent, wish_text, occasion=occasion, yesterday=body, redo=bad)
        if not redo_shot:
            return {}
        card["shot"] = redo_shot
        return {"title": redo_shot["title"] or card["title"], "line": redo_shot["line"] or card["line"]}
    vet(card, body, _today_source(day, plan, card["weather"], wish_text), redo=again if shot else None)
    if recent and recent[0].get("variety"):
        y = recent[0]["variety"]
        card["avoid"] = f"{y['place']}, {y['outfit']}, {y['camera']}"
    image = folder / f"{day.isoformat()}.jpg"
    if maker is not None:
        refs = references(root, card["code"])
        errors = []
        for attempt in (refs, refs[:2], []):  # 失败先少带几张参考图再试（连着两天 400，同一张备用图用了两天）
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
    try:
        (Path(root) / WAIT).unlink()
    except OSError:
        pass
    return card


def retire(root: Path, day: date):
    """重画前把今天的旧卡挪进 cards/old/，不删。"""
    folder = Path(root) / "cards"
    old = folder / "old"
    old.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    moved = []
    for ext in ("json", "jpg"):
        f = folder / f"{day.isoformat()}.{ext}"
        if f.exists():
            to = old / f"{day.isoformat()}-{stamp}.{ext}"
            n = 1
            while to.exists():  # 同一秒挪了两次，不盖掉前一份
                to, n = old / f"{day.isoformat()}-{stamp}-{n}.{ext}", n + 1
            f.rename(to)
            moved.append((to, f))
    return moved


def collection(root: Path) -> list:
    """卡册：所有已有的昨日卡说明，新的在前。"""
    return [json.loads(p.read_text()) for p in sorted((Path(root) / "cards").glob("*.json"), reverse=True)]


def main():
    from .clock import her_now
    from .cli import build_router
    from .memory import MemoryStore
    from .photos import OpenRouter
    import os

    parser = argparse.ArgumentParser(description="生成今天的昨日卡")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--redo", action="store_true", help="今天的卡重画（旧的挪进 cards/old/）")
    parser.add_argument("--refresh", action="store_true", help="昨天的日记改了：只重出卡上的字，不重画")
    parser.add_argument("--day", type=date.fromisoformat, help="--refresh 用：哪天的卡，默认今天")
    args = parser.parse_args()
    store = MemoryStore(args.data)
    router = build_router(args.data, 50.0)
    day = args.day or her_now(router.settings).date()
    if args.refresh:
        try:
            if (Path(args.data) / "cards" / f"{day.isoformat()}.json").exists():
                card = refresh(args.data, store.db, day, router, store=store)
                print(json.dumps({k: card.get(k) for k in ("day", "title", "line", "checks", "diary")}, ensure_ascii=False))
        finally:
            store.close()
        return
    moved = retire(args.data, day) if args.redo else []
    key = os.environ.get("OPENROUTER_API_KEY")
    card = make_card(args.data, store.db, day, store.db.latest_plan(day.isoformat()),
                     maker=OpenRouter(key) if key else None, ledger=router.ledger, router=router, store=store)
    if card.get("waiting") and moved:  # 重画时导演没编出来：原来那张挪回来，不让她的卡没了
        for to, back in moved:
            to.rename(back)
        (Path(args.data) / WAIT).unlink(missing_ok=True)
    print(json.dumps(card, ensure_ascii=False))


if __name__ == "__main__":
    main()
