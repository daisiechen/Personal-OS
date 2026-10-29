"""语音合成：沿用旧版配置，OpenRouter 的 MiniMax speech-2.8-hd + Mia 定制的 小伴_way 音色。"""

import json
import os
import urllib.request
from pathlib import Path

from .ledger import Ledger

URL = "https://openrouter.ai/api/v1/audio/speech"


class OpenRouterTTS:
    def __init__(self, api_key: str, model: str = "minimax/speech-2.8-hd", voice: str = "小伴_way",
                 opener=urllib.request.urlopen, timeout: int = 60, ledger: Ledger = None,
                 usd_per_1k_chars: float = 0.1):
        self.api_key, self.model, self.voice = api_key, model, voice
        self.opener, self.timeout = opener, timeout
        self.ledger = ledger
        # 语音接口只返回音频、不返回费用，按字数估算记账，与聊天共用 OpenRouter 月度预算
        self.usd_per_1k_chars = usd_per_1k_chars

    def __call__(self, text: str):
        """返回 mp3 字节；失败或本月预算用完返回 None，由调用方退回文字。"""
        text = text[:500]
        if self.ledger is not None and self.ledger.budget_status() == "over":
            return None
        body = json.dumps({"model": self.model, "input": text, "voice": self.voice,
                           "response_format": "mp3", "speed": 1.0}).encode("utf-8")
        req = urllib.request.Request(URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"})
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                audio = resp.read()
        except Exception as e:
            print(f"[tts] 合成失败：{e}", flush=True)
            return None
        if len(audio) < 1000:  # 太小说明返回的是错误信息
            return None
        if self.ledger is not None:
            self.ledger.record("openrouter", self.model, len(text) / 1000 * self.usd_per_1k_chars, "tts")
        return audio


def build_tts(data: Path = None, budget: float = 50.0):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    return OpenRouterTTS(key, model=os.environ.get("ETHAN_TTS_MODEL", "minimax/speech-2.8-hd"),
                         voice=os.environ.get("ETHAN_TTS_VOICE", "小伴_way"),
                         ledger=Ledger(Path(data) / "ledger", budget) if data else None,
                         usd_per_1k_chars=float(os.environ.get("ETHAN_TTS_USD_PER_1K_CHARS", "0.1")))
