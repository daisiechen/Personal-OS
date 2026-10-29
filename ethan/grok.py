"""把 小伴 自己做不了的事（上网、查地图、开网页、预订）交给 Mia 的 Grok Bot。

Grok Bot 是 xAI 的常驻云端 agent，自带浏览器。它在 Routines 里建了一个 webhook
“Claude agent 来信”，小伴 往那里 POST 一条任务就会叫醒它去办（2026-10-26 Mia 同意：
小伴 做不了的直接交给 Grok，不用再问她）。Grok 办完把结果 POST 到 小伴 的收件箱 /grok/result（见 ethan/inbox.py），
聊天里交代的事由 小伴 在聊天里转告她（见 ethan/relay.py，2026-10-27 她定：Grok 只是替 小伴 跑腿）。

环境变量（Mia 用 python3 -m ethan.setup_secrets 录入，不进聊天和仓库）：
  GROK_WEBHOOK_URL            webhook 地址
  GROK_WEBHOOK_SECRET         发送密钥
  GROK_WEBHOOK_SECRET_HEADER  密钥放在哪个请求头，默认 Authorization（值为 Bearer <密钥>）；
                              Grok 那边要求别的请求头时改这里，值就是密钥本身
"""

import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HEADER = "Authorization"
LOG_FILE = "grok-tasks.jsonl"
REPORT_TO = ("办完把结果回报到 小伴 的收件箱 https://ethan.example.com/grok/result（用原来的收件箱密钥），"
             "小伴 会在他和 Mia 的聊天里转告她；不用在 Grok 聊天里另外告诉她，除非要她付钱、登录或做决定。"
             "回报第一行写清来源，三选一：【来源：她的收藏/点赞】【来源：调研任务】【来源：情报收集】。")
START_MARK = "【开工】"  # Grok 开始一件要花时间的活时先报一声，团队页才看得到它在干（2026-10-29 她说 Grok 在干活看板上没显示）
RECEIPT = "【回执 {ref}】"
SOURCE_RULE = "每次回报第一行写清来源，三选一：【来源：她的收藏/点赞】【来源：调研任务】【来源：情报收集】。"  # 2026-10-29 她要分清


class GrokWebhook:
    def __init__(self, url: str, secret: str, header: str = DEFAULT_HEADER, log_dir: Path = None,
                 opener=urllib.request.urlopen, timeout: int = 15):
        self.url = url
        self.secret = secret
        self.header = header or DEFAULT_HEADER
        self.log_path = Path(log_dir) / LOG_FILE if log_dir else None
        self.opener = opener
        self.timeout = timeout

    def __call__(self, task: str, channel: str = "", report_to: str = REPORT_TO) -> bool:
        """发出一条任务，成功返回 True；失败返回 False 并记日志，不抛异常，不影响聊天。"""
        ref = None
        if report_to == REPORT_TO:  # 自带记号的（准备员、开发会话）不加，免得打乱它们的回报格式
            ref = "G" + datetime.now(timezone.utc).strftime("%d%H%M%S")
            report_to = report_to + f"回报第一行最前面写{RECEIPT.format(ref=ref)}，小伴 按它认出是哪件活回来了。"
        body = {"from": "小伴（Mia 的私人助理）", "task": task, "report_to": report_to,
                "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        value = f"Bearer {self.secret}" if self.header.lower() == "authorization" else self.secret
        req = urllib.request.Request(self.url, method="POST",
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers={"Content-Type": "application/json", self.header: value})
        error = None
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 200)
                if status >= 300:
                    error = f"HTTP {status}"
        except Exception as e:  # 网络、4xx/5xx 都算没发出去
            error = str(e)[:200]
        self._log(task, channel, error, ref)
        if error:
            print(f"[grok] 任务没发出去：{error}", flush=True)
        return error is None

    def _log(self, task: str, channel: str, error, ref=None):
        if self.log_path is None:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "channel": channel,
                 "task": task, "ok": error is None, "error": error}
        if ref:
            entry["ref"] = ref
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def build_grok(log_dir: Path = None, env=os.environ):
    """没录入 webhook 地址和密钥时返回 None，小伴 就不会提 Grok。"""
    url, secret = env.get("GROK_WEBHOOK_URL", "").strip(), env.get("GROK_WEBHOOK_SECRET", "").strip()
    if not (url and secret):
        return None
    return GrokWebhook(url, secret, env.get("GROK_WEBHOOK_SECRET_HEADER", "").strip(), log_dir=log_dir)
