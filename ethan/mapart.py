"""领地地图的底图：用 Grok Imagine（OpenRouter）画几张候选，她挑一张（2026-10-28，方案 A）。

只画画面，不画文字：领地名字、健康灯、副本进度由工作台叠在图上，这样以后改名、改颜色都不用重画。
每张约 $0.09，记进花费账（purpose=map-art）。画好的放在 <数据目录>/maps/，不进仓库；她选定后再拷进工作台的静态文件。

运行（服务器，要 OPENROUTER_API_KEY）：python3 -m ethan.mapart --data /home/ethan/data -n 3 [--theme night|day]
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

from .photos import OpenRouter

COST = 0.09
LAYOUT = (
    "A single continent seen from above, filling most of a 16:10 frame, surrounded by sea. "
    "The continent is divided into exactly seven bordering provinces arranged in a ring around a walled royal city "
    "with a small castle at the exact centre; every province touches the central city and its two neighbours. "
    "Clockwise from the top: a teal-cyan province of busy market towns and harbours (career); a bright gold province "
    "of coin fields and a treasury tower (wealth); a rose-pink and amber province of lantern-lit plazas and bridges "
    "(friendship); a small, narrow strip of grey-brown barren rocky unclaimed wasteland on the south-east coast, much "
    "smaller than any province; a deep indigo-violet province of libraries and an observatory (learning); a "
    "wine-crimson province of an old university and scholar's tower (academia); a snowy silver-blue province of "
    "mountains, lakes, a road winding to a lighthouse and a hot-air balloon (life and travel); an emerald-green "
    "province of gardens, a running track and springs (body). Each province has its own clearly different dominant "
    "colour so neighbours never blend together. "
    "Province borders are drawn as fine dotted ink lines, and each province has a clear open area near its middle for "
    "a label. A compass rose in one sea corner.")
RELIEF = (" Strong three-dimensional relief like a painted diorama: raised terrain, mountains and buildings casting "
          "soft shadows, cliffs along the coast, depth and volume.")
STYLES = {
    "night": ("Hand-painted 1990s Japanese anime fantasy world map, like a cel-animation background art board: "
              "night palette, sea in deep midnight blue with fine wave linework, provinces in rich saturated "
              "jewel tones that glow against the dark sea, coastlines and borders in antique gold ink, soft starlight glow, delicate detail, "
              "elegant and grown-up, not childish, not cartoon, no chibi."),
    "day": ("Hand-painted 1990s Japanese anime fantasy world map, like a cel-animation background art board: "
            "warm cream parchment, sea in soft teal watercolour, land in pale ochre and sage, coastlines and borders "
            "in dark gold-brown ink, gentle afternoon light, delicate detail, elegant and grown-up, not childish, "
            "not cartoon, no chibi."),
}
NO_TEXT = " Absolutely no text, letters, numbers, labels or writing anywhere in the image."


def prompt(theme: str, variant: int) -> str:
    angle = [" Bird's-eye view tilted about 30 degrees.", " Isometric view from the south.",
             " Top-down view with dramatic low moonlight from the west."]
    return STYLES[theme] + " " + LAYOUT + RELIEF + angle[variant % len(angle)] + NO_TEXT


def main():
    parser = argparse.ArgumentParser(description="画领地地图候选")
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("-n", type=int, default=3)
    parser.add_argument("--theme", choices=sorted(STYLES), default="night")
    args = parser.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise SystemExit("缺少 OPENROUTER_API_KEY")
    from .cli import build_router
    ledger = build_router(args.data, 50.0).ledger
    maker = OpenRouter(key)
    out = args.data / "maps"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    for i in range(args.n):
        path = out / f"map-{args.theme}-{stamp}-{i + 1}.jpg"
        try:
            path.write_bytes(maker.make(prompt(args.theme, i)))
            ledger.record(maker.name, maker.model, COST, purpose="map-art")
            print(f"画好了 {path}", flush=True)
        except Exception as e:  # 一张失败不影响别的
            print(f"第 {i + 1} 张没画成：{type(e).__name__} {str(e)[:200]}", flush=True)


if __name__ == "__main__":
    main()
