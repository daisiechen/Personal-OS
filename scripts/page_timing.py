"""各页面接口的耗时和大小。只读：只发 GET，不改数据。

python3 scripts/page_timing.py <数据目录>
"""

import gzip
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PATHS = sys.argv[2:] or ["/api/state", "/api/quests", "/api/season", "/api/map", "/api/ach", "/api/team", "/api/settings", "/api/me", "/api/me/pending",
         "/api/me/all", "/api/me/manage", "/api/relations", "/api/desk", "/api/cards", "/api/week", "/api/brain?tab=all", "/api/memory"] + \
        [f"/api/territory?k={k}" for k in ("body", "study", "life", "career", "money", "social", "academic")]


def main():
    from ethan.workbench.server import build
    app = build(Path(sys.argv[1]))
    app.authed = lambda headers: True
    for path in PATHS:
        for run in (1, 2):
            t = time.time()
            try:
                status, headers, body = app.handle("GET", path, {}, b"")
            except Exception as e:
                status, body = f"出错 {type(e).__name__}: {str(e)[:60]}", b""
            ms = int((time.time() - t) * 1000)
            if run == 2:
                print(f"{path:28} {status} 第一次 {first:6} ms  第二次 {ms:6} ms  {len(body) // 1024:5} KB  压缩后 {len(gzip.compress(body, 5)) // 1024:4} KB", flush=True)
            first = ms
    app.wb.store.close()


if __name__ == "__main__":
    main()
