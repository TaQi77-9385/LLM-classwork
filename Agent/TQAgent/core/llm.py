"""OpenAI 兼容的流式 Chat Completions。不打印，也不依赖界面。"""

import json
from dataclasses import dataclass, field

import requests

from core.config import LlmSettings


class ChatError(Exception):
    pass


@dataclass
class StreamState:
    answer: str = ""
    calls: dict = field(default_factory=dict)
    done: bool = False
    usage: dict | None = None


@dataclass(frozen=True)
class LlmText:
    text: str


@dataclass(frozen=True)
class LlmDone:
    answer: str
    calls: list
    usage: dict | None = None


def feed(state: StreamState, line: bytes) -> str | None:
    if not line or not line.startswith(b"data: "):
        return None
    data = line[6:]
    if data == b"[DONE]":
        state.done = True
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise ChatError(f"无法解析模型响应: {exc}") from exc

    usage = payload.get("usage")
    if isinstance(usage, dict):
        state.usage = usage

    choices = payload.get("choices") or []
    if not choices:
        # 仅含 usage 的收尾块是正常的
        if isinstance(usage, dict):
            return None
        raise ChatError("无法解析模型响应: 缺少 choices")
    try:
        choice = choices[0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatError(f"无法解析模型响应: {exc}") from exc

    delta = choice.get("delta") or {}
    if not isinstance(delta, dict):
        delta = {}
    emitted = None
    text = delta.get("content")
    if text:
        state.answer += text
        emitted = text
    for call in delta.get("tool_calls") or []:
        item = state.calls.setdefault(call.get("index", 0), {
            "id": "",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if call.get("id"):
            item["id"] = call["id"]
        if call.get("type"):
            item["type"] = call["type"]
        fn = call.get("function") or {}
        if fn.get("name"):
            item["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            item["function"]["arguments"] += fn["arguments"]
    if choice.get("finish_reason"):
        state.done = True
    return emitted


class ChatClient:
    def __init__(self, settings: LlmSettings, tools: list):
        self.settings = settings
        self.tools = tools

    def stream(self, messages: list, *, use_tools: bool = True):
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        body = {
            "model": self.settings.model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if use_tools and self.tools:
            body["tools"] = self.tools

        resp = self._open_stream(url, body)
        state = StreamState()
        try:
            for line in resp.iter_lines():
                text = feed(state, line)
                if text:
                    yield LlmText(text)
        finally:
            resp.close()
        yield LlmDone(
            state.answer,
            [state.calls[i] for i in sorted(state.calls)],
            state.usage,
        )

    def _open_stream(self, url: str, body: dict):
        headers = {"Authorization": "Bearer " + self.settings.api_key}
        try:
            timeout = (
                int(getattr(self.settings, "connect_timeout", 10) or 10),
                int(getattr(self.settings, "read_timeout", 120) or 120),
            )
            resp = requests.post(url, headers=headers, json=body, stream=True, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.RequestException as exc:
            response = getattr(exc, "response", None)
            can_retry = (
                "stream_options" in body
                and response is not None
                and response.status_code in {400, 422}
            )
            if not can_retry:
                detail = ""
                if response is not None and getattr(response, "text", ""):
                    detail = " " + response.text[:300]
                raise ChatError(f"模型请求失败: {exc}{detail}") from exc
            body = dict(body)
            body.pop("stream_options", None)
            try:
                timeout = (
                    int(getattr(self.settings, "connect_timeout", 10) or 10),
                    int(getattr(self.settings, "read_timeout", 120) or 120),
                )
                resp = requests.post(url, headers=headers, json=body, stream=True, timeout=timeout)
                resp.raise_for_status()
                return resp
            except requests.RequestException as retry_exc:
                detail = ""
                response = getattr(retry_exc, "response", None)
                if response is not None and getattr(response, "text", ""):
                    detail = " " + response.text[:300]
                raise ChatError(f"模型请求失败: {retry_exc}{detail}") from retry_exc
