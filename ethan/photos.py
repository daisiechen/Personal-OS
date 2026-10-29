"""小伴 发照片（2026-10-27 她定）。

两种：
- 生活照：他剧本里的日出、咖啡、跑步路线、晚饭……用 AI 现场生成。照片里不露他的脸（风景、食物、手、背影）。
  他主动发的每天最多 3 张；她要的不算，要多少给多少。
- 网上的图：[发图 网址]，Telegram 直接按网址发。
生图先用 OpenRouter 上的 Grok Imagine（OPENROUTER_API_KEY），没有或失败再试 Google Gemini API（GEMINI_API_KEY），每张记进账本。
2026-10-27 她看了三轮样图定的：Grok 写实，配上“有审美、会撩”的提示词后不再直男；Seedream 偏摆拍。
她的 OpenRouter 账号被 Claude、Gemini、GPT 三家拒（403），Grok、Seedream、Muse 能用。
"""

import base64
import json
import os
import re
import urllib.request
from datetime import date, datetime
from pathlib import Path

PHOTO = re.compile(r"\[照片( 她要的)? ([^\]\n]+)\]")
WEB_IMAGE = re.compile(r"\[发图 (https?://[^\s\]]+)\]")
SELF_PER_DAY = 3
STATE = "photos.json"
STYLE = ("真实的手机照片，但拍照的人有审美、有心思：像一个温柔、有点会撩的男朋友，随手拍给女朋友的生活碎片。"
         "光线有氛围（逆光、窗边自然光、暖色台灯、夜里的灯影倒影），浅景深，构图有留白和故事感，色调柔和偏暖，略带胶片感。"
         "画面里藏一个让人想追问的小细节，比如多出来的第二个杯子、一件明显属于女生的小东西、一张没写字的便签、为两个人准备的痕迹。"
         "画面里不出现任何人脸（可以有手、手臂、肩膀、背影或剪影），手干净好看。不要任何文字、水印和边框。画面内容：")
GEMINI_MODEL = "gemini-2.5-flash-image"
OPENROUTER_MODEL = "x-ai/grok-imagine-image-2.0"
EST_COST = 0.04  # 每张大约多少美元，记账用（Grok Imagine 和 Gemini 都约 0.04）


def parse(text: str) -> dict:
    """{"photos": [(她要的?, 描述)], "image_urls": [网址]}"""
    return {"photos": [(bool(asked), desc.strip()) for asked, desc in PHOTO.findall(text)],
            "image_urls": WEB_IMAGE.findall(text)}


def strip(text: str) -> str:
    return WEB_IMAGE.sub("", PHOTO.sub("", text))


class Gemini:
    name = "gemini"

    def __init__(self, key: str, model: str = GEMINI_MODEL, opener=urllib.request.urlopen):
        self.key, self.model, self.opener = key, model, opener

    def make(self, prompt: str) -> bytes:
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseModalities": ["IMAGE"]}}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps(body).encode(), headers={"x-goog-api-key": self.key, "Content-Type": "application/json"})
        with self.opener(req, timeout=120) as resp:
            data = json.loads(resp.read())
        for part in data["candidates"][0]["content"]["parts"]:
            if part.get("inlineData", {}).get("data"):
                return base64.b64decode(part["inlineData"]["data"])
        raise RuntimeError("没有返回图片")


class OpenRouter:
    name = "openrouter"

    def __init__(self, key: str, model: str = OPENROUTER_MODEL, opener=urllib.request.urlopen):
        self.key, self.model, self.opener = key, model, opener

    def make(self, prompt: str, refs=()) -> bytes:
        """refs：参考图的路径（JPEG/PNG），按顺序放进同一条消息，用来保住人物长相（今日卡用，见 ethan/cards.py）。"""
        content = prompt
        if refs:
            content = [{"type": "text", "text": prompt}] + [
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,"
                                                   + base64.b64encode(Path(r).read_bytes()).decode()}} for r in refs]
        body = {"model": self.model, "modalities": ["image"], "messages": [{"role": "user", "content": content}]}
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        with self.opener(req, timeout=120) as resp:
            data = json.loads(resp.read())
        url = data["choices"][0]["message"]["images"][0]["image_url"]["url"]
        return base64.b64decode(url.split(",", 1)[1])


class Photographer:
    def __init__(self, root: Path, makers: list, ledger=None):
        self.root, self.makers, self.ledger = Path(root), makers, ledger

    def _state(self) -> dict:
        try:
            return json.loads((self.root / STATE).read_text())
        except (OSError, ValueError):
            return {}

    def take(self, wanted: list, day: date) -> tuple:
        """wanted：[(她要的?, 描述)]。返回 (照片文件路径, 要告诉她的系统提醒)。"""
        state = self._state()
        used = state.get(day.isoformat(), 0)
        files, notes = [], []
        for asked, desc in wanted:
            if not asked and used >= SELF_PER_DAY:
                continue  # 主动发的今天够了，悄悄不发
            data, errors = None, []
            for maker in self.makers:
                try:
                    data = maker.make(STYLE + desc)
                    break
                except Exception as e:  # 换下一个服务
                    errors.append(f"{maker.name}: {type(e).__name__} {str(e)[:60]}")
            if data is None:
                if asked:
                    notes.append("【系统提醒】照片没生成出来（" + ("；".join(errors) or "还没配置生图服务") + "）")
                continue
            folder = self.root / "media" / "photos"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{len(files)}.png"
            path.write_bytes(data)
            files.append(str(path))
            if self.ledger is not None:
                self.ledger.record(maker.name, maker.model, EST_COST, purpose="photo")
            if not asked:
                used += 1
        state = {day.isoformat(): used}  # 只留今天
        (self.root / STATE).write_text(json.dumps(state))
        return files, notes


def build(root: Path, ledger=None, env=None):
    env = os.environ if env is None else env
    makers = []
    if env.get("OPENROUTER_API_KEY"):
        makers.append(OpenRouter(env["OPENROUTER_API_KEY"]))
    if env.get("GEMINI_API_KEY"):
        makers.append(Gemini(env["GEMINI_API_KEY"]))
    return Photographer(root, makers, ledger) if makers else None
