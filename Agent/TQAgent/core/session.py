"""一轮用户输入内的工具循环。界面通过事件拿到过程，不参与消息拼装。"""

from collections.abc import Iterator

from core.events import AgentError, TextDelta, ToolFinished, ToolStarted, TurnFinished
from core.llm import ChatClient, ChatError, LlmDone, LlmText
from core.registry import TOOL_NAMES, TOOL_SCHEMAS, run_tool


MAX_MODEL_CALLS = 8


class AgentSession:
    def __init__(self, client: ChatClient, tool_runner=run_tool, tool_names: tuple[str, ...] = TOOL_NAMES):
        self.client = client
        self.tool_names = tuple(tool_names)
        self._run_tool = tool_runner
        self.messages: list[dict] = []
        self._busy = False
        self.max_calls = MAX_MODEL_CALLS
        settings = getattr(client, "settings", None)
        self.model = getattr(settings, "model", "")
        self.base_url = getattr(settings, "base_url", "")
        allowed = set(self.tool_names)
        self.tool_catalog = tuple(
            (item["function"]["name"], item["function"].get("description", ""))
            for item in TOOL_SCHEMAS
            if item["function"]["name"] in allowed
        )

    def reset(self) -> None:
        if self._busy:
            raise RuntimeError("上一轮还在进行")
        self.messages.clear()

    def submit(self, user_text: str) -> Iterator:
        if self._busy:
            yield AgentError("上一轮还在进行")
            return
        self._busy = True
        try:
            yield from self._turn(user_text)
        finally:
            self._busy = False

    def _turn(self, user_text: str) -> Iterator:
        self.messages.append({"role": "user", "content": user_text})
        for _ in range(self.max_calls):
            answer = ""
            calls: list = []
            try:
                for chunk in self.client.stream(self.messages):
                    if isinstance(chunk, LlmText):
                        answer += chunk.text
                        yield TextDelta(chunk.text)
                    elif isinstance(chunk, LlmDone):
                        answer = chunk.answer
                        calls = chunk.calls
            except ChatError as exc:
                yield AgentError(str(exc))
                return
            except Exception as exc:
                yield AgentError(f"请求失败: {exc}")
                return

            if not calls:
                if answer:
                    self.messages.append({"role": "assistant", "content": answer})
                yield TurnFinished(answer)
                return

            self.messages.append({
                "role": "assistant",
                "content": answer or None,
                "tool_calls": calls,
            })
            for call in calls:
                function = call.get("function") or {}
                name = function.get("name") or ""
                arguments = function.get("arguments") or ""
                yield ToolStarted(name, arguments)
                result = self._run_tool(call)
                yield ToolFinished(name, result)
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or "",
                    "content": result,
                })
        yield AgentError(f"已达到本轮 {self.max_calls} 次模型调用上限")


def create_session(settings) -> AgentSession:
    from core.config import load_search_settings
    from core.search import configure

    configure(load_search_settings(getattr(settings, "source", None)))
    return AgentSession(ChatClient(settings, TOOL_SCHEMAS))
