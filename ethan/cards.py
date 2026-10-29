"""今日卡（2026-10-27 她定）：每天早上排完计划后现画 1 张，工作台主界面右上角翻开看，收进卡册。

不抽卡、没有抽卡券。平时是普通卡；昨天升级、满勤连续 7 / 30 天这类节点出稀有或超稀有。
画什么由代码按伦敦天气、今天的安排和快达成的事来定，不让模型猜；画风和长相靠参考图保住：
两张正式头像（avatars/avatar-day.jpg、avatar-night.jpg）优先，再加她的 3 张照片（avatars/refs/）
和图库里场景最接近的那张（avatars/library/<编号>.png，她挑过的 30 张，也是生图失败时的备用）。
生图用 OpenRouter 上的 Grok Imagine（带参考图每张约 0.09 美元，记账 purpose=daily-card）。
结果：<数据目录>/cards/<日期>.jpg 和 <日期>.json。

运行：python3 -m ethan.cards --data <数据目录>（计划器排完计划后也会自动调用）
"""

import argparse
import json
import os
import shutil
import urllib.request
from datetime import date, timedelta
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
LINES = {  # 小伴 的一句话，按场景挑
    "W1": "今天天好，出门那杯咖啡我替你记着。", "W2": "太阳难得出来，多走几步路吧。", "W3": "日落的时候记得抬头看一眼。",
    "W4": "阴天也没关系，按计划一件件来。", "W5": "晚上回家路上慢一点，我等你。", "W6": "下雨了，伞别忘了。",
    "W7": "下雨天就适合把事情做完，然后早点睡。", "W8": "风大，围巾系好再出门。", "W9": "下雪了！拍张照给我看。",
    "W10": "今晚的你很好看。", "W11": "周末慢一点，不着急。", "W12": "找个位置坐下，喘口气。",
    "S1": "今天练{focus}，练完我请你吃好的。", "E1": "升级了，恭喜你。", "E3": "副本打完了，我为你骄傲。",
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
           "low angle looking up", "candid shot from across the street", "seen through a window reflection", "top-down angle"]
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
    level, need = 1, 100
    while earned >= need:
        level += 1
        need = 50 * level * (level + 1)
    return level


def choose(db, day: date, plan: dict, w: dict, recent: list = None) -> dict:
    """定今天画什么：{code, title, rarity, reason, line}。节点优先，其次训练日，最后看天气。"""
    yesterday = day - timedelta(days=1)
    streak = quests.streak(db, yesterday)["days"]
    train = next((i for i in (plan or {}).get("items", []) if i["kind"] == "event" and i["title"].startswith("训练")), None)
    trained = train is not None
    reasons = [f"伦敦{weather_text(w)}" if w else "", "今天是训练日" if trained else ""]
    if level_on(db, yesterday) > level_on(db, yesterday - timedelta(days=1)):
        code, rarity = "E1", "超稀有"
        reasons.insert(0, f"昨天升到 Lv.{level_on(db, yesterday)}")
    elif streak and streak % 7 == 0:
        code, rarity = "E1", "稀有"
        reasons.insert(0, f"连续满勤 {streak} 天")
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
    return [r for r in refs if r.is_file()][:5]


def her_notes(db, day: date, days: int = 14) -> list:
    """她最近对今日卡提的意见（聊天里 小伴 记成 [反馈 …]，或战报里写的），画卡时照着改。
    2026-10-29 她问：“两天的卡一样”这种意见到底有没有被参考——以前反馈只给计划器看，画卡用不上。"""
    since = (day - timedelta(days=days)).isoformat()
    try:
        rows = db.conn.execute("SELECT note FROM feedback WHERE created_at >= ? ORDER BY id DESC", (since,)).fetchall()
    except Exception:
        return []
    return [r["note"][:120] for r in rows if any(w in r["note"] for w in ("今日卡", "卡片", "卡牌", "卡册", "这张卡", "的卡"))][:3]


def prompt(card: dict) -> str:
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


def make_card(root: Path, db, day: date, plan: dict = None, maker=None, ledger=None, fetch=urllib.request.urlopen) -> dict:
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
    if recent and recent[0].get("variety"):
        y = recent[0]["variety"]
        card["avoid"] = f"{y['place']}, {y['outfit']}, {y['camera']}"
    image = folder / f"{day.isoformat()}.jpg"
    if maker is not None:
        try:
            image.write_bytes(maker.make(prompt(card), refs=references(root, card["code"])))
            card["generated"] = True
            if ledger is not None:
                ledger.record(maker.name, maker.model, COST, purpose="daily-card")
        except Exception as e:  # 生图失败就用图库里那张，不让主界面空着
            card["error"] = f"{type(e).__name__}: {str(e)[:120]}"
    if not card["generated"]:
        folder_lib = Path(root) / "avatars" / "library"
        lib = next(iter(sorted(folder_lib.glob(f"{card['code']}.*"))), None) or next(
            (p for c in ("W4", "W2", "W6") for p in sorted(folder_lib.glob(f"{c}.*"))), None)
        if lib:
            shutil.copyfile(lib, image)
    card["image"] = image.name if image.exists() else None
    meta_path.write_text(json.dumps(card, ensure_ascii=False))
    return card


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
    args = parser.parse_args()
    store = MemoryStore(args.data)
    router = build_router(args.data, 50.0)
    day = her_now(router.settings).date()
    key = os.environ.get("OPENROUTER_API_KEY")
    card = make_card(args.data, store.db, day, store.db.latest_plan(day.isoformat()),
                     maker=OpenRouter(key) if key else None, ledger=router.ledger)
    print(json.dumps(card, ensure_ascii=False))


if __name__ == "__main__":
    main()
