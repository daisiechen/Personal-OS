"""工作台里说一段话转文字（日记里“说一段”用浏览器自带的识别，说了很长一段只认出一小截，没法用）。

改成：工作台录完整段音频，传到服务器，用和 Telegram 语音同一个本地 faster-whisper 转（ethan/channels/media.py）。
转写要十几秒到一分钟，放在后台进程里做，不占着工作台；页面每两秒问一次好了没有。
音频转完就删，只留文字结果，一天后也删。
运行（工作台自己调）：python3 -m ethan.voice --data <数据目录> --job <编号>
"""

import argparse
import json
import secrets
import time
from pathlib import Path

MAX_BYTES = 15 * 1024 * 1024  # 大约十几分钟的语音
EXT = {"audio/mp4": ".m4a", "audio/aac": ".m4a", "audio/webm": ".webm", "audio/ogg": ".ogg", "audio/wav": ".wav", "audio/mpeg": ".mp3"}


def _dir(root) -> Path:
    d = Path(root) / "voice"
    d.mkdir(parents=True, exist_ok=True)
    return d


def start(root, data: bytes, ctype: str = "", spawn=None) -> str:
    """存下音频、后台开始转写，返回编号。"""
    if not data:
        raise ValueError("没录到声音")
    if len(data) > MAX_BYTES:
        raise ValueError("太长了，分几段说")
    _cleanup(root)
    job = secrets.token_hex(8)
    ext = EXT.get((ctype or "").split(";")[0].strip().lower(), ".webm")
    (_dir(root) / f"{job}{ext}").write_bytes(data)
    from .bgwork import kick
    kick(root, "voice", ["--job", job], spawn)
    return job


def result(root, job: str) -> dict:
    """{"done": False} 还在转；{"done": True, "text": …} 或 {"done": True, "error": …}。"""
    if not job.isalnum():
        raise ValueError("编号不对")
    f = _dir(root) / f"{job}.json"
    if not f.exists():
        return {"done": False}
    return {"done": True, **json.loads(f.read_text(encoding="utf-8"))}


def run(root, job: str, transcriber=None) -> dict:
    audio = next((p for p in _dir(root).glob(f"{job}.*") if p.suffix != ".json"), None)
    if audio is None:
        out = {"error": "录音找不到了，再说一次"}
    else:
        try:
            if transcriber is None:
                from .channels.media import WhisperTranscriber
                transcriber = WhisperTranscriber()
            text = transcriber(audio)
            out = {"text": text} if text else {"error": "没听清，再说一次"}
        except Exception as e:
            out = {"error": f"转文字没成：{type(e).__name__}"}
        finally:
            audio.unlink(missing_ok=True)
    (_dir(root) / f"{job}.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return out


def _cleanup(root, hours: int = 24):
    cut = time.time() - hours * 3600
    for p in _dir(root).iterdir():
        if p.stat().st_mtime < cut:
            p.unlink(missing_ok=True)


def main():
    p = argparse.ArgumentParser(description="工作台里说的一段话转文字")
    p.add_argument("--data", type=Path, required=True)
    p.add_argument("--job", required=True)
    a = p.parse_args()
    print(run(a.data, a.job), flush=True)


if __name__ == "__main__":
    main()
