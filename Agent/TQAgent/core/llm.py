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


@dataclass(frozen=True)
class LlmText:
    text: str


@dataclass(frozen=True)
class LlmDone:
    answer: str
    calls: list


def feed(state: StreamState, line: bytes) -> str | None:
    if not line or not line.startswith(b"data: "):
        return None
    data = line[6:]
    if data == b"[DONE]":
        state.done = True
        return None
    try:
        payload = json.loads(data)
        choice = payload["choices"][0]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
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

    def stream(self, messages: list):
        url = self.settings.base_url.rstrip("/") + "/chat/completions"
        try:
            resp = requests.post(
                url,
                headers={"Authorization": "Bearer " + self.settings.api_key},
                json={
                    "model": self.settings.model,
                    "messages": messages,
                    "tools": self.tools,
                    "stream": True,
                },
                stream=True,
                timeout=(10, 30),
            )
            resp.raise_for_status()
        except requests.RequestException as exc:
            detail = ""
            response = getattr(exc, "response", None)
            if response is not None and getattr(response, "text", ""):
                detail = " " + response.text[:300]
            raise ChatError(f"模型请求失败: {exc}{detail}") from exc

        state = StreamState()
        try:
            for line in resp.iter_lines():
                text = feed(state, line)
                if text:
                    yield LlmText(text)
                if state.done:
                    break
        finally:
            resp.close()
        yield LlmDone(state.answer, [state.calls[i] for i in sorted(state.calls)])
