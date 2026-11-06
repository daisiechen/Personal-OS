"""模型后端：Claude 订阅（claude -p）与 OpenRouter 兜底。"""

import base64
import json
import mimetypes
import os
import subprocess
import urllib.request
from dataclasses import dataclass

# 她的 OpenRouter 账号被 Claude、Gemini、GPT 三家拒（403），兜底改用 Grok
FALLBACK_MODEL = "x-ai/grok-4.7"
OLD_FALLBACK_MODELS = ("anthropic/claude-sonnet-4.5",)


class BackendUnavailable(Exception):
    """后端暂时不可用（限流、未登录、网络、账号异常等），应切换到下一个后端。

    rate_limit：订阅报错时 Claude Code 带的额度情况（rate_limit_event），没有就是 None（ethan/core/quota.py 用）。"""

    def __init__(self, msg="", rate_limit=None):
        super().__init__(msg)
        self.rate_limit = rate_limit


def _find(name: str) -> str:
    """服务的 PATH 里没有 ~/.local/bin 时也找得到 claude（线上：工作台点“AI 重排”报找不到 claude，只好用了兜底计划）。"""
    import shutil
    from pathlib import Path
    if os.sep in name or shutil.which(name):
        return name
    for d in (Path.home() / ".local" / "bin", Path("/usr/local/bin"), Path.home() / ".npm-global" / "bin"):
        if (d / name).is_file():
            return str(d / name)
    return name


class SystemPrompt(str):
    """系统提示词，多带一个 fixed：开头那段每轮一字不差的长度（字符数，agent.build_system_prompt 给）。

    能显式标缓存的后端（OpenRouter 上的 Claude）在这里切一刀、给前一段加 cache_control；
    claude -p 没有标缓存的参数（2026-11 的 --help 里没有），只靠“固定的在前”让它自带的缓存多命中。
    拼接、切片后变回普通 str，fixed 自然丢掉，不会切错位置。"""

    fixed = 0

    @classmethod
    def join(cls, fixed: str, rest: list) -> "SystemPrompt":
        s = cls("\n\n".join([fixed, *rest]))
        s.fixed = len(fixed)
        return s


@dataclass
class Reply:
    text: str
    backend: str
    model: str
    cost_usd: float = 0.0
    # token 用量（额度预算）：拿不到就是 None，账本记空。input 不含缓存读写的部分
    input_tokens: int = None
    output_tokens: int = None
    cache_read_tokens: int = None
    cache_write_tokens: int = None
    rate_limit: dict = None  # claude -p 流里最后一条 rate_limit_event 的 rate_limit_info（isUsingOverage 等），拿不到 None

    def usage(self) -> dict:
        return {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens,
                "cache_read_tokens": self.cache_read_tokens, "cache_write_tokens": self.cache_write_tokens}


def _int(v):
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _parse_stream(stdout: str):
    """claude -p 的输出 → (result, rate_limit_info)。stream-json 是一行一个事件；老格式（整段一个 JSON）也认。"""
    try:
        whole = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        whole = None
    if isinstance(whole, dict):
        return whole, None
    result, info = None, None
    for line in (stdout or "").split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "result":
            result = ev
        elif ev.get("type") == "rate_limit_event" and isinstance(ev.get("rate_limit_info"), dict):
            info = ev["rate_limit_info"]
    return result, info


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
        # 提示词写进临时文件再传路径（11-04：记忆多了，整段放在命令行参数里超过系统上限，“Argument list too long”，小伴 回不了）
        import tempfile
        tmp = tempfile.NamedTemporaryFile("w", suffix=".txt", prefix="ethan-sys-", delete=False, encoding="utf-8")
        tmp.write(system)
        tmp.close()
        try:
            return self._run(env, system, tmp.name, messages, images)
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    def _run(self, env, system, sys_file, messages, images, use_file: bool = True) -> Reply:
        # stream-json（要配 --verbose）多一条 rate_limit_event：在不在用付费额度、额度满没满、几点恢复（quota.py）
        cmd = [self.claude_bin, "-p", "--output-format", "stream-json", "--verbose", "--model", self.model,
               *(["--system-prompt-file", sys_file] if use_file else ["--system-prompt", system])]
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
        result, info = _parse_stream(proc.stdout)
        if proc.returncode != 0:
            if use_file and "system-prompt-file" in (proc.stderr or "") and len(system.encode("utf-8")) < 100_000:
                return self._run(env, system, sys_file, messages, images, use_file=False)  # 老版本不认这个参数
            said = str((result or {}).get("result") or "")[:200]
            raise BackendUnavailable(f"claude 退出码 {proc.returncode}：{(proc.stderr or '')[:200]} {said}".strip(), info)
        if result is None:
            raise BackendUnavailable("claude 输出不是 JSON", info)
        if result.get("is_error") or result.get("subtype") != "success":
            raise BackendUnavailable(f"claude 返回错误：{str(result.get('result'))[:200]}", info)
        u = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        return Reply(text=result["result"].strip(), backend=self.name, model=self.model,
                     cost_usd=result.get("total_cost_usd") or 0.0,
                     input_tokens=_int(u.get("input_tokens")), output_tokens=_int(u.get("output_tokens")),
                     cache_read_tokens=_int(u.get("cache_read_input_tokens")),
                     cache_write_tokens=_int(u.get("cache_creation_input_tokens")), rate_limit=info)


class OpenRouterBackend:
    """按量付费兜底，走 OpenRouter 的 chat completions 接口。"""

    name = "openrouter"
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key: str, model: str = FALLBACK_MODEL,
                 opener=urllib.request.urlopen, timeout: int = 120, url: str = None, name: str = None):
        """url / name：直连别家兼容 OpenAI 接口的模型（比如 DeepSeek）时用，见 cli.build_router（开源准备）。"""
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
            "messages": [{"role": "system", "content": self._system(system)}, *messages],
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
        u = data.get("usage") or {}
        cost = u.get("cost") or 0.0
        d = u.get("prompt_tokens_details") or {}
        read, write, prompt = _int(d.get("cached_tokens")), _int(d.get("cache_write_tokens")), _int(u.get("prompt_tokens"))
        inp = None if prompt is None else max(0, prompt - (read or 0) - (write or 0))  # 和 claude -p 一样：input 不含缓存部分
        return Reply(text=text, backend=self.name, model=self.model, cost_usd=cost, input_tokens=inp,
                     output_tokens=_int(u.get("completion_tokens")), cache_read_tokens=read, cache_write_tokens=write)

    def _system(self, system):
        """OpenRouter 上的 Claude 要显式标缓存：固定段末尾加 cache_control（别家自动按前缀缓存，原样给字符串）。"""
        fixed = getattr(system, "fixed", 0)
        if not (fixed and self.model.startswith("anthropic/") and "openrouter.ai" in self.URL and fixed < len(system)):
            return str(system)
        return [{"type": "text", "text": str(system[:fixed]), "cache_control": {"type": "ephemeral"}},
                {"type": "text", "text": str(system[fixed:])}]
