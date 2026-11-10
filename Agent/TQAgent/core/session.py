"""一轮用户输入内的工具循环。界面通过事件拿到过程，不参与消息拼装。"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from core.config import get_limits
from core.context import (
    DEFAULT_CONTEXT_WINDOW,
    WARN_RATIO,
    archive_note,
    bind_context_root,
    build_compact_messages,
    build_summary_messages,
    estimate_session_tokens,
    format_history_for_compact,
    save_archive,
)
from core.events import (
    AgentError,
    CompactFinished,
    ContextWarning,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from core.llm import ChatClient, ChatError, LlmDone, LlmText
from core.registry import TOOL_SCHEMAS, run_tool
from core.skills import SkillRegistry, bind_skills, discover_skills, skill_prompt
from core.store import (
    SessionRecord,
    derive_title,
    list_sessions,
    load_session,
    make_session_id,
    save_session,
)

MAX_MODEL_CALLS = 8
AGENTS_MD_MARKER = "[AGENTS.md]"


class AgentSession:
    def __init__(
        self,
        client: ChatClient,
        tool_runner=run_tool,
        tool_names: tuple[str, ...] | None = None,
        skills: SkillRegistry | None = None,
        tool_schemas: list | None = None,
        mcp_errors: tuple[str, ...] = (),
        project_root: Path | None = None,
        context_window: int | None = None,
        mcp_hub=None,
    ):
        schemas = TOOL_SCHEMAS if tool_schemas is None else tool_schemas
        if tool_names is None:
            tool_names = tuple(item["function"]["name"] for item in schemas)
        self.client = client
        self.tool_names = tuple(tool_names)
        self.tool_schemas = schemas
        self._run_tool = tool_runner
        self.skills = skills if skills is not None else SkillRegistry(())
        self.skill_prompt = skill_prompt(self.skills)
        bind_skills(self.skills)
        self.project_root = Path.cwd() if project_root is None else Path(project_root)
        bind_context_root(self.project_root)
        self.messages: list[dict] = []
        self.events: list[dict] = []
        self._busy = False
        self._compacting = False
        limits = get_limits()
        self.max_calls = int(getattr(limits, "max_model_calls", MAX_MODEL_CALLS) or MAX_MODEL_CALLS)
        self.mcp_errors = tuple(mcp_errors)
        self.mcp_hub = mcp_hub
        self.mcp_status = "idle"
        self.mcp_servers: list[dict] = []
        settings = getattr(client, "settings", None)
        self.model = getattr(settings, "model", "")
        self.base_url = getattr(settings, "base_url", "")
        self.config_path = str(getattr(settings, "source", "") or "")
        configured = context_window
        if configured is None:
            configured = getattr(settings, "context_window", None)
        default_window = int(
            getattr(limits, "context_window_default", DEFAULT_CONTEXT_WINDOW) or DEFAULT_CONTEXT_WINDOW
        )
        if configured and configured > 0:
            self.context_window = int(configured)
            self.context_window_is_default = False
        else:
            self.context_window = default_window
            self.context_window_is_default = True
        self.warn_ratio = float(getattr(limits, "context_warn_ratio", WARN_RATIO) or WARN_RATIO)
        self.used_tokens = 0
        self._warned_ratio = False
        self.session_id = make_session_id()
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.agents_md_injected = False
        self._rebuild_catalog()
        self.refresh_token_usage()

    def _rebuild_catalog(self) -> None:
        allowed = set(self.tool_names)
        self.tool_catalog = tuple(
            (item["function"]["name"], item["function"].get("description", ""))
            for item in self.tool_schemas
            if item["function"]["name"] in allowed
        )

    @property
    def input_blocked(self) -> bool:
        """达到上下文窗口阈值后，禁止普通提示词，直到压缩成功。"""
        return self.usage_ratio >= self.warn_ratio

    @property
    def usage_ratio(self) -> float:
        if self.context_window <= 0:
            return 0.0
        return self.used_tokens / self.context_window

    def reset(self, *, inject_agents: bool = True) -> None:
        if self._busy:
            raise RuntimeError("上一轮还在进行")
        self.messages.clear()
        self.events.clear()
        self._warned_ratio = False
        self.session_id = make_session_id()
        self.created_at = datetime.now().isoformat(timespec="seconds")
        self.agents_md_injected = False
        if inject_agents:
            self.inject_agents_md()
        self.refresh_token_usage()

    def inject_agents_md(self) -> bool:
        """会话首次加载时注入 AGENTS.md，后续轮次直接复用消息历史。"""
        if self.agents_md_injected:
            return False
        if any(
            str(item.get("content") or "").startswith(AGENTS_MD_MARKER)
            for item in self.messages
        ):
            self.agents_md_injected = True
            return False
        path = self.project_root / "AGENTS.md"
        if not path.is_file():
            return False
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return False
        content = (
            f"{AGENTS_MD_MARKER}\n"
            "以下为项目 AGENTS.md 约定，请在整个会话中遵守：\n\n"
            f"{text}"
        )
        self.messages.append({"role": "user", "content": content})
        self.events.append({
            "type": "agents_md",
            "path": str(path),
            "chars": len(text),
            "at": datetime.now().isoformat(timespec="seconds"),
        })
        self.agents_md_injected = True
        self.refresh_token_usage()
        return True

    def refresh_token_usage(self, api_usage: dict | None = None) -> int:
        estimated = estimate_session_tokens(
            self.messages,
            system_prompt=self.skill_prompt,
            tool_schemas=self.tool_schemas,
        )
        used = estimated
        if api_usage:
            prompt = int(api_usage.get("prompt_tokens") or 0)
            completion = int(api_usage.get("completion_tokens") or 0)
            total = int(api_usage.get("total_tokens") or 0)
            api_value = total or (prompt + completion) or prompt
            if api_value > 0:
                used = max(estimated, api_value)
        self.used_tokens = used
        return used

    def apply_mcp(self, hub) -> None:
        """MCP 连接完成后把远程工具并进会话。"""
        from core.registry import TOOL_SCHEMAS as local_schemas

        self.mcp_hub = hub
        self.mcp_errors = tuple(getattr(hub, "errors", ()) or ())
        self.mcp_servers = list(getattr(hub, "server_infos", []) or [])
        if self.mcp_errors and not getattr(hub, "schemas", None):
            self.mcp_status = "error"
        else:
            self.mcp_status = "ready"
        schemas = [*local_schemas, *list(getattr(hub, "schemas", []) or [])]
        self.tool_schemas = schemas
        self.tool_names = tuple(item["function"]["name"] for item in schemas)
        self.client.tools = schemas
        self._run_tool = lambda call, hub=hub: run_tool(call, hub=hub)
        self._rebuild_catalog()
        self.refresh_token_usage()

    def load_mcp(self) -> None:
        """在后台线程中同步连接 MCP（供 TUI worker 调用）。"""
        from core.mcp_client import connect_mcp
        from core.registry import TOOL_NAMES

        self.mcp_status = "loading"
        hub = connect_mcp(self.project_root, reserved=TOOL_NAMES)
        self.apply_mcp(hub)

    def snapshot(self, *, rounds: int = 0, tool_calls: int = 0) -> SessionRecord:
        now = datetime.now().isoformat(timespec="seconds")
        return SessionRecord(
            id=self.session_id,
            title=derive_title(self.messages),
            created_at=self.created_at or now,
            updated_at=now,
            messages=list(self.messages),
            events=list(self.events),
            rounds=rounds,
            tool_calls=tool_calls,
            model=self.model,
            agents_md_injected=self.agents_md_injected,
        )

    def save(self, *, rounds: int = 0, tool_calls: int = 0) -> Path:
        record = self.snapshot(rounds=rounds, tool_calls=tool_calls)
        path = save_session(record, self.project_root)
        self.events.append({
            "type": "save",
            "path": str(path),
            "at": record.updated_at,
        })
        return path

    def restore(self, session_id: str) -> SessionRecord:
        if self._busy:
            raise RuntimeError("上一轮还在进行")
        record = load_session(session_id, self.project_root)
        self.session_id = record.id
        self.created_at = record.created_at
        self.messages = list(record.messages)
        self.events = list(record.events)
        self.agents_md_injected = bool(record.agents_md_injected) or any(
            str(item.get("content") or "").startswith(AGENTS_MD_MARKER)
            for item in self.messages
        )
        self._warned_ratio = False
        self.refresh_token_usage()
        return record

    def list_saved(self) -> list[SessionRecord]:
        return list_sessions(self.project_root)

    def submit(self, user_text: str) -> Iterator:
        if self._busy:
            yield AgentError("上一轮还在进行")
            return
        if self._compacting:
            yield AgentError("正在压缩上下文，请稍候")
            return
        if self.input_blocked:
            yield AgentError(
                "上下文已达到窗口限制的阈值，已暂停接受新提示词。"
                "请先执行 /compact 压缩上下文后再继续。"
            )
            return
        self._busy = True
        try:
            yield from self._turn(user_text)
        finally:
            self._busy = False

    def compact(self) -> Iterator:
        """手动触发上下文压缩。压缩期间不执行任何 Function Calling 工具。"""
        if self._busy:
            yield AgentError("上一轮还在进行，无法压缩")
            return
        if self._compacting:
            yield AgentError("正在压缩上下文")
            return
        if not self.messages:
            yield AgentError("当前没有可压缩的对话历史")
            return
        self._busy = True
        self._compacting = True
        try:
            yield from self._compact()
        finally:
            self._compacting = False
            self._busy = False

    def _turn(self, user_text: str) -> Iterator:
        self.messages.append({"role": "user", "content": user_text})
        self.refresh_token_usage()
        for _ in range(self.max_calls):
            answer = ""
            calls: list = []
            usage = None
            try:
                for chunk in self.client.stream(self._messages_for_model()):
                    if isinstance(chunk, LlmText):
                        answer += chunk.text
                        yield TextDelta(chunk.text)
                    elif isinstance(chunk, LlmDone):
                        answer = chunk.answer
                        calls = chunk.calls
                        usage = chunk.usage
            except ChatError as exc:
                yield AgentError(str(exc))
                self.refresh_token_usage()
                return
            except Exception as exc:
                yield AgentError(f"请求失败: {exc}")
                self.refresh_token_usage()
                return

            if not calls:
                if answer:
                    self.messages.append({"role": "assistant", "content": answer})
                self.refresh_token_usage(usage)
                warning = self._maybe_context_warning()
                if warning is not None:
                    yield warning
                yield TurnFinished(answer)
                return

            self.messages.append({
                "role": "assistant",
                "content": answer or None,
                "tool_calls": calls,
            })
            self.refresh_token_usage(usage)
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
                self.refresh_token_usage()
            warning = self._maybe_context_warning()
            if warning is not None:
                yield warning
        yield AgentError(f"已达到本轮 {self.max_calls} 次模型调用上限")

    def _compact(self) -> Iterator:
        when = datetime.now()
        snapshot = list(self.messages)
        try:
            archive_id = save_archive(snapshot, self.project_root, when)
        except OSError as exc:
            yield AgentError(f"保存原始上下文失败，已中止压缩: {exc}")
            return

        history = format_history_for_compact(snapshot)
        compact_messages = build_compact_messages(history)
        summary = ""
        try:
            for chunk in self.client.stream(compact_messages, use_tools=False):
                if isinstance(chunk, LlmText):
                    summary += chunk.text
                    yield TextDelta(chunk.text)
                elif isinstance(chunk, LlmDone):
                    summary = chunk.answer or summary
        except ChatError as exc:
            yield AgentError(f"压缩失败，已保留原有上下文: {exc}")
            return
        except Exception as exc:
            yield AgentError(f"压缩失败，已保留原有上下文: {exc}")
            return

        summary = (summary or "").strip()
        if not summary:
            yield AgentError("压缩失败，已保留原有上下文: 模型未返回摘要")
            return

        note = archive_note(archive_id, when)
        self.messages = build_summary_messages(summary, note)
        self.events.append({
            "type": "compact",
            "archive_id": archive_id,
            "at": when.isoformat(timespec="seconds"),
        })
        self._warned_ratio = False
        self.refresh_token_usage()
        yield CompactFinished(archive_id, summary)
        yield TurnFinished(summary)

    def _maybe_context_warning(self) -> ContextWarning | None:
        if self.usage_ratio < self.warn_ratio:
            return None
        if self._warned_ratio:
            return None
        self._warned_ratio = True
        percent = self.usage_ratio * 100
        message = (
            f"上下文已使用约 {self._format_k(self.used_tokens)} / "
            f"{self._format_k(self.context_window)}"
            f"（{percent:.1f}%）"
            + ("，默认窗口" if self.context_window_is_default else "")
            + "。请执行 /compact 压缩上下文后再继续。"
        )
        return ContextWarning(self.used_tokens, self.context_window, self.usage_ratio, message)

    @staticmethod
    def _format_k(tokens: int) -> str:
        value = tokens / 1000
        if value >= 100:
            return f"{value:.0f}k"
        if value >= 10:
            return f"{value:.1f}k"
        return f"{value:.2f}k"

    def _messages_for_model(self) -> list[dict]:
        self.skill_prompt = skill_prompt(self.skills)
        if not self.skill_prompt:
            return self.messages
        return [{"role": "system", "content": self.skill_prompt}, *self.messages]


def create_session(
    settings,
    project_root: Path | None = None,
    *,
    mcp_mode: str = "sync",
) -> AgentSession:
    """创建会话。

    mcp_mode:
      - sync: 启动时阻塞连接 MCP（测试默认）
      - async: 先用本地工具进入界面，再由 TUI 后台加载 MCP
      - off: 不连接 MCP
    """
    from core.config import load_search_settings
    from core.mcp_client import McpHub, bind_mcp, connect_mcp
    from core.search import configure

    configure(load_search_settings(getattr(settings, "source", None)))
    root = Path.cwd() if project_root is None else Path(project_root)
    limits = get_limits()

    if mcp_mode == "sync":
        hub = connect_mcp(root)
        schemas = [*TOOL_SCHEMAS, *hub.schemas]
        session = AgentSession(
            ChatClient(settings, schemas),
            tool_runner=lambda call, hub=hub: run_tool(call, hub=hub),
            skills=discover_skills(root),
            tool_schemas=schemas,
            mcp_errors=tuple(hub.errors),
            project_root=root,
            context_window=getattr(settings, "context_window", None),
            mcp_hub=hub,
        )
        session.mcp_servers = list(getattr(hub, "server_infos", []) or [])
        session.mcp_status = "ready" if not hub.errors or hub.schemas else ("error" if hub.errors else "ready")
    else:
        hub = McpHub()
        bind_mcp(hub)
        session = AgentSession(
            ChatClient(settings, list(TOOL_SCHEMAS)),
            tool_runner=lambda call, hub=hub: run_tool(call, hub=hub),
            skills=discover_skills(root),
            tool_schemas=list(TOOL_SCHEMAS),
            mcp_errors=(),
            project_root=root,
            context_window=getattr(settings, "context_window", None),
            mcp_hub=hub,
        )
        session.mcp_status = "loading" if mcp_mode == "async" else "off"

    session.max_calls = int(limits.max_model_calls or MAX_MODEL_CALLS)
    session.inject_agents_md()
    return session
