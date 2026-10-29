"""模型后端：Claude 订阅（claude -p）与 OpenRouter 兜底。"""

import base64
import json
import mimetypes
import os
import subprocess
import urllib.request
from dataclasses import dataclass

# 2026-10-27 她定：她的 OpenRouter 账号被 Claude、Gemini、GPT 三家拒（403），兜底改用 Grok
FALLBACK_MODEL = "x-ai/grok-4.7"
OLD_FALLBACK_MODELS = ("anthropic/claude-sonnet-4.5",)


class BackendUnavailable(Exception):
    """后端暂时不可用（限流、未登录、网络、账号异常等），应切换到下一个后端。"""


def _find(name: str) -> str:
    """服务的 PATH 里没有 ~/.local/bin 时也找得到 claude（2026-10-29 线上：工作台点“AI 重排”报找不到 claude，只好用了兜底计划）。"""
    import shutil
    from pathlib import Path
    if os.sep in name or shutil.which(name):
        return name
    for d in (Path.home() / ".local" / "bin", Path("/usr/local/bin"), Path.home() / ".npm-global" / "bin"):
        if (d / name).is_file():
            return str(d / name)
    return name


@dataclass
class Reply:
    text: str
    backend: str
    model: str
    cost_usd: float = 0.0


def _flatten(messages: list) -> str:
    """把历史消息拼成一段对话文本，交给 claude -p 一次性调用。"""
    names = {"user": "Mia", "assistant": "小伴"}
    lines = [f"{names[m['role']]}：{m['content']}" for m in messages]
    return "\n".join(lines) + "\n小伴："


class SubscriptionBackend:
    """通过本机已登录的 Claude Code（Mia 的订阅）调用模型。"""

    name = "subscription"

    def __init__(self, model: str = "sonnet", claude_bin: str = "claude", runner=subprocess.run,
                 timeout: int = 180):
        self.model = model
        self.claude_bin = _find(claude_bin)
        self.runner = runner
        self.timeout = timeout

    def complete(self, system: str, messages: list, images=()) -> Reply:
        env = dict(os.environ)
        env.pop("ANTHROPIC_API_KEY", None)  # 有 API key 时会压过订阅登录
        cmd = [self.claude_bin, "-p", "--output-format", "json", "--model", self.model,
               "--system-prompt", system]
        prompt = _flatten(messages)
        if images:
            # 只开放 Read，且只允许读本轮图片和 PDF 所在目录，读不到密钥等其他文件；PDF 页数多要分几次读
            dirs = sorted({os.path.dirname(os.path.abspath(p)) for p in images})
            cmd += ["--tools", "Read", "--max-turns", "8",
                    "--allowedTools", *[f"Read(/{d}/**)" for d in dirs]]
            listing = "\n".join(os.path.abspath(p) for p in images)
            prompt = f"（Mia 这次发来的图片或文件，先用 Read 打开看：\n{listing}）\n\n" + prompt
        else:
            cmd += ["--tools", ""]
        try:
            proc = self.runner(cmd, input=prompt, capture_output=True, text=True,
                               env=env, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise BackendUnavailable(f"claude 调用失败：{e}") from e
        if proc.returncode != 0:
            raise BackendUnavailable(f"claude 退出码 {proc.returncode}：{proc.stderr[:200]}")
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise BackendUnavailable("claude 输出不是 JSON") from e
        if result.get("is_error") or result.get("subtype") != "success":
            raise BackendUnavailable(f"claude 返回错误：{str(result.get('result'))[:200]}")
        return Reply(text=result["result"].strip(), backend=self.name, model=self.model,
                     cost_usd=result.get("total_cost_usd") or 0.0)


class OpenRouterBackend:
    """按量付费兜底，走 OpenRouter 的 chat completions 接口。"""

    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = FALLBACK_MODEL,
                 opener=urllib.request.urlopen, timeout: int = 120, url: str = None, name: str = None):
        """url / name：直连别家兼容 OpenAI 接口的模型（比如 DeepSeek）时用，见 cli.build_router（2026-10-29 开源准备）。"""
        self.api_key = api_key
        self.model = model
        if url:
            self.URL = url
        if name:
            self.name = name
        self.opener = opener
        self.timeout = timeout

    def complete(self, system: str, messages: list, images=()) -> Reply:
        messages = [dict(m) for m in messages]
        if images and messages and messages[-1]["role"] == "user":
            parts = [{"type": "text", "text": messages[-1]["content"]}]
            for path in images:
                mime = mimetypes.guess_type(path)[0] or "image/jpeg"
                with open(path, "rb") as f:
                    data = f"data:{mime};base64," + base64.b64encode(f.read()).decode()
                if mime == "application/pdf":  # OpenRouter 替不支持 PDF 的模型解析成文字
                    parts.append({"type": "file", "file": {"filename": os.path.basename(path), "file_data": data}})
                else:
                    parts.append({"type": "image_url", "image_url": {"url": data}})
            messages[-1]["content"] = parts
        body = json.dumps({
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            **({"usage": {"include": True}} if "openrouter.ai" in self.URL else {}),  # 只有 OpenRouter 认这个字段
        }).encode("utf-8")
        req = urllib.request.Request(self.URL, data=body, method="POST", headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        try:
            with self.opener(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # 网络、HTTP 4xx/5xx 都视为不可用
            raise BackendUnavailable(f"OpenRouter 调用失败：{e}") from e
        if "error" in data:
            raise BackendUnavailable(f"OpenRouter 返回错误：{data['error']}")
        text = data["choices"][0]["message"]["content"].strip()
        cost = (data.get("usage") or {}).get("cost") or 0.0
        return Reply(text=text, backend=self.name, model=self.model, cost_usd=cost)
