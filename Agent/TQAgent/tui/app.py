"""Textual 前端。布局：对话区、斜杠补全、右侧会话/MCP 栏。"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Footer, Header, Input, Markdown, OptionList, Static
from textual.widgets.option_list import Option

from core.commands import COMMANDS, help_markdown, match_commands
from core.config import get_limits
from core.events import (
    AgentError,
    CompactFinished,
    ContextWarning,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)

_ERROR_PREFIXES = (
    "未知工具",
    "参数错误",
    "参数不是",
    "操作失败",
    "文件不存在",
    "目录不存在",
    "路径不存在",
    "不是纯文本",
    "不是 UTF-8",
    "文件超过",
    "文件名不能",
    "文件已存在",
    "目标已存在",
    "这是目录",
    "Skill 已存在",
    "Skill 名称",
    "skillContext",
    "Frontmatter",
    "SKILL.md 缺少",
    "description 超过",
    "创建 Skill 失败",
    "name 不能为空",
    "name 必须是字符串",
    "MCP 工具执行失败",
)


def _result_limit() -> int:
    return int(getattr(get_limits(), "ui_result_limit", 2000) or 2000)


def _clip(text: str) -> str:
    limit = _result_limit()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（界面已截断，模型收到的是完整结果）"


def _signature(name: str, arguments: str) -> str:
    try:
        data = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return f"{name}({arguments})"
    if not isinstance(data, dict) or not data:
        return f"{name}()"
    inner = ", ".join(f"{key}={value}" for key, value in data.items())
    return f"{name}({inner})"


def _pretty_json(arguments: str) -> str:
    try:
        return json.dumps(json.loads(arguments or "{}"), ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        return arguments or "{}"


def _short_path(path: str, limit: int = 42) -> str:
    if len(path) <= limit:
        return path
    head = max(8, limit // 3)
    return path[:head] + "…" + path[-(limit - head - 1):]


def _host(url: str) -> str:
    if not url:
        return "—"
    return urlparse(url).netloc or url


class UiEvent(Message):
    def __init__(self, event) -> None:
        self.event = event
        super().__init__()


class UiDone(Message):
    pass


class McpUpdated(Message):
    pass


class HelpScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "关闭", show=True, priority=True),
        Binding("enter", "dismiss", "关闭", show=True, priority=True),
        Binding("q", "dismiss", "关闭", show=True, priority=True),
    ]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Markdown(help_markdown())
            yield Static("按 Esc / Enter / q 关闭帮助", id="help-hint")


class ConfigNeededScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "dismiss", "关闭", show=True, priority=True),
        Binding("enter", "dismiss", "关闭", show=True, priority=True),
        Binding("q", "dismiss", "关闭", show=True, priority=True),
    ]

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(id="config-dialog"):
            yield Markdown(
                "# 需要配置\n\n"
                f"当前目录没有可用的配置，已生成示例文件：\n\n`{self.path}`\n\n"
                "请编辑其中的 `api_key`、`base_url`、`model` 等项，保存后**重新启动** TQAgent 才能正常工作。\n\n"
                "按 Enter 或 Esc 关闭本对话框。"
            )


class ResumeScreen(ModalScreen[str | None]):
    BINDINGS = [
        Binding("escape", "cancel", "取消", show=True, priority=True),
        Binding("enter", "choose", "恢复", show=True, priority=True),
        Binding("q", "cancel", "取消", show=True, priority=True),
    ]

    def __init__(self, records: list) -> None:
        super().__init__()
        self.records = records

    def compose(self) -> ComposeResult:
        with Vertical(id="resume-dialog"):
            yield Static("选择要恢复的历史会话（↑↓ 选择，Enter 确认，Esc 取消）", id="resume-title")
            options = []
            for record in self.records:
                label = f"{record.id}  ·  {record.title}  ·  {record.updated_at or record.created_at}"
                options.append(Option(label, id=record.id))
            yield OptionList(*options, id="resume-list")

    def on_mount(self) -> None:
        listing = self.query_one("#resume-list", OptionList)
        if self.records:
            listing.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(str(event.option_id) if event.option_id is not None else None)

    def action_choose(self) -> None:
        listing = self.query_one("#resume-list", OptionList)
        highlighted = listing.highlighted
        if highlighted is None:
            self.dismiss(None)
            return
        option = listing.get_option_at_index(highlighted)
        self.dismiss(str(option.id) if option.id is not None else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class UserLine(Static):
    def __init__(self, text: str) -> None:
        body = Content.assemble(
            ("你\n", "bold #e9c46a"),
            text,
        )
        super().__init__(body, classes="msg")


class AgentLine(Static):
    def __init__(self) -> None:
        self._text = ""
        super().__init__(self._render(), classes="msg")

    def append(self, text: str) -> None:
        self._text += text
        self.update(self._render())

    def _render(self) -> Content:
        return Content.assemble(
            ("TQAgent\n", "bold #4cc9f0"),
            self._text,
        )


class ToolCard(Collapsible):
    def __init__(self, name: str, arguments: str) -> None:
        self.tool_name = name
        self.arguments = arguments
        self.started = time.perf_counter()
        self.body = Static("执行中…", markup=False, classes="tool-body")
        super().__init__(
            self.body,
            title=f"… {_signature(name, arguments)}",
            collapsed=True,
            classes="tool-card",
        )

    def finish(self, result: str) -> None:
        failed = result.startswith(_ERROR_PREFIXES)
        elapsed = max(0, int((time.perf_counter() - self.started) * 1000))
        mark = "✗" if failed else "✓"
        self.title = f"{mark} {_signature(self.tool_name, self.arguments)} · {elapsed} ms"
        if failed:
            self.add_class("-error")
        block = Content.assemble(
            ("参数\n", "bold #adb5bd"),
            _pretty_json(self.arguments) + "\n\n",
            ("结果\n", "bold #adb5bd"),
            _clip(result) or "（无输出）",
        )
        self.body.update(block)


class Transcript(VerticalScroll):
    def plain(self) -> str:
        parts = []
        for widget in self.query(Static):
            content = widget.content
            parts.append(getattr(content, "plain", str(content)))
        return "\n".join(parts)


class TQAgentApp(App):
    TITLE = "TQAgent"
    CSS_PATH = "app.tcss"
    COMMAND_PALETTE_BINDING = "ctrl+p"
    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("l", "clear_screen", "清屏"),
        Binding("n", "new_session", "新会话"),
        Binding("b", "toggle_sidebar", "侧栏"),
        Binding("f1", "help", "帮助", priority=True),
        Binding("ctrl+b", "toggle_sidebar", "侧栏", show=False, priority=True),
        Binding("ctrl+n", "new_session", "新会话", show=False, priority=True),
        Binding("ctrl+s", "save_session", "保存", show=False, priority=True),
        Binding("escape", "interrupt", "中断/关闭", priority=True),
        Binding("ctrl+q", "quit", "退出", show=False, priority=True),
    ]

    def __init__(self, session, model_name: str, *, config_created: bool = False):
        super().__init__()
        self.session = session
        self._model = model_name or getattr(session, "model", "") or "—"
        self.sub_title = self._model
        self._config_created = config_created
        self._assistant: AgentLine | None = None
        self._pending: ToolCard | None = None
        self._busy = False
        self._cancel = False
        self._rounds = 0
        self._tool_calls = 0
        self._last_seconds = 0.0
        self._turn_started = 0.0
        self._status = "就绪"
        self._session_id = getattr(session, "session_id", None) or uuid.uuid4().hex[:8]
        self._worker = None
        self._queue: list[str] = []
        self._slash_matches: list = []
        self._slash_index = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Static("", id="banner")
                yield Transcript(id="transcript")
                yield Static("", id="slash-panel", classes="-hidden")
                yield Input(
                    placeholder="输入问题或 / 命令；Tab 补全；Esc 中断生成",
                    id="prompt",
                )
            yield VerticalScroll(id="sidebar")
        yield Static("", id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        self._session_id = getattr(self.session, "session_id", self._session_id)
        self._paint_banner()
        self._paint_sidebar()
        self._paint_status()
        self._note("在下方输入问题，按 Enter 发送。输入 / 可打开命令补全。")
        if getattr(self.session, "agents_md_injected", False):
            self._note("已加载项目 AGENTS.md，并写入当前会话历史。")
        errors = tuple(getattr(self.session, "mcp_errors", ()) or ())
        if errors:
            self._note("\n".join(errors), error=True)
        self.query_one("#prompt", Input).focus()
        if self._config_created:
            path = getattr(self.session, "config_path", "") or str(Path.cwd() / "config.ini")
            self.push_screen(ConfigNeededScreen(path))
        if getattr(self.session, "mcp_status", "") == "loading":
            self._status = "MCP 加载中"
            self._paint_status()
            self.run_worker(self._load_mcp_worker, thread=True, exclusive=False)

    def _load_mcp_worker(self) -> None:
        try:
            loader = getattr(self.session, "load_mcp", None)
            if loader is not None:
                loader()
        except Exception as exc:
            setattr(self.session, "mcp_status", "error")
            errors = list(getattr(self.session, "mcp_errors", ()) or ())
            errors.append(f"MCP 加载失败：{exc}")
            self.session.mcp_errors = tuple(errors)
        self.post_message(McpUpdated())

    def on_mcp_updated(self, _message: McpUpdated) -> None:
        status = getattr(self.session, "mcp_status", "")
        servers = list(getattr(self.session, "mcp_servers", []) or [])
        errors = tuple(getattr(self.session, "mcp_errors", ()) or ())
        if status == "ready":
            count = sum(len(item.get("tools") or []) for item in servers)
            self._note(f"MCP 已就绪：{len(servers)} 个服务器，{count} 个远程工具。")
        elif errors:
            self._note("\n".join(errors), error=True)
        if not self._busy:
            self._status = "就绪"
        self._paint_banner()
        self._paint_sidebar()
        self._paint_status()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        self._update_slash_panel(event.value)

    def on_key(self, event) -> None:
        panel = self.query_one("#slash-panel", Static)
        if panel.has_class("-hidden") or not self._slash_matches:
            return
        key = getattr(event, "key", "")
        if key == "down":
            self._slash_index = (self._slash_index + 1) % len(self._slash_matches)
            self._render_slash_panel()
            event.prevent_default()
            event.stop()
        elif key == "up":
            self._slash_index = (self._slash_index - 1) % len(self._slash_matches)
            self._render_slash_panel()
            event.prevent_default()
            event.stop()
        elif key == "tab":
            chosen = self._slash_matches[self._slash_index]
            box = self.query_one("#prompt", Input)
            box.value = chosen.name + " "
            box.cursor_position = len(box.value)
            self._hide_slash_panel()
            event.prevent_default()
            event.stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or event.input.disabled:
            return
        if text.startswith("/"):
            name = text.split()[0].lower()
            known = {item.name for item in COMMANDS}
            # Enter 只执行完整命令；Tab 负责补全。避免输入 "/" 就误开 /help
            if name not in known:
                hint = ""
                if self._slash_matches:
                    chosen = self._slash_matches[self._slash_index]
                    hint = f" 可按 Tab 补全为 {chosen.name}。"
                self._note(f"命令不完整：{name}。请输入完整命令（如 /help）后回车。{hint}")
                return
            event.input.value = ""
            self._hide_slash_panel()
            rest = text[len(text.split()[0]):]
            self._command(name + rest)
            return
        event.input.value = ""
        self._hide_slash_panel()
        if self._context_blocked():
            self._note(
                "上下文已达到窗口阈值，已暂停普通提问。请先执行 /compact。",
                error=True,
            )
            self._paint_banner()
            self._paint_status()
            return
        self._show_user(text)
        if self._turn_is_running():
            self._queue.append(text)
            self._paint_status()
            return
        self._start_turn(text)

    def _update_slash_panel(self, value: str) -> None:
        text = value or ""
        if not text.startswith("/") or "\n" in text:
            self._hide_slash_panel()
            return
        token = text.split()[0]
        matches = match_commands(token)
        if not matches:
            self._hide_slash_panel()
            return
        self._slash_matches = matches
        self._slash_index = min(self._slash_index, len(matches) - 1)
        self._render_slash_panel()

    def _render_slash_panel(self) -> None:
        panel = self.query_one("#slash-panel", Static)
        lines = []
        for index, item in enumerate(self._slash_matches):
            mark = "› " if index == self._slash_index else "  "
            lines.append(f"{mark}{item.name:<10} {item.summary}")
        panel.update("命令补全（↑↓ 选择，Tab 填入，Enter 执行）\n" + "\n".join(lines))
        panel.remove_class("-hidden")

    def _hide_slash_panel(self) -> None:
        panel = self.query_one("#slash-panel", Static)
        panel.add_class("-hidden")
        panel.update("")
        self._slash_matches = []
        self._slash_index = 0

    def _turn_is_running(self) -> bool:
        worker = self._worker
        if worker is not None and worker.is_finished:
            self._busy = False
        return self._busy

    def _show_user(self, text: str) -> None:
        transcript = self.query_one(Transcript)
        transcript.mount(UserLine(text))
        transcript.scroll_end(animate=False)

    def _start_turn(self, text: str) -> None:
        self._rounds += 1
        self._busy = True
        self._cancel = False
        self._turn_started = time.perf_counter()
        self._status = "生成中"
        self._assistant = None
        self._pending = None
        self._paint_status()
        self._paint_sidebar()
        self._paint_banner()
        self._worker = self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)

    def _start_compact(self) -> None:
        if self._turn_is_running():
            self._note("当前仍有任务在进行，请稍后再压缩。", error=True)
            return
        compact = getattr(self.session, "compact", None)
        if compact is None:
            self._note("当前会话不支持 /compact。", error=True)
            return
        self._queue.clear()
        self._busy = True
        self._cancel = False
        self._turn_started = time.perf_counter()
        self._status = "压缩中"
        self._assistant = None
        self._pending = None
        self._note("开始压缩上下文：先归档原始对话，再生成摘要。压缩期间不可调用其他工具。")
        self._paint_status()
        self._paint_sidebar()
        self._paint_banner()
        self._worker = self.run_worker(self._compact_turn, thread=True, exclusive=True)

    def _turn(self, text: str) -> None:
        messages = getattr(self.session, "messages", None)
        start = len(messages) if isinstance(messages, list) else None
        cancelled = False
        try:
            for event in self.session.submit(text):
                if self._cancel:
                    cancelled = True
                    self.post_message(UiEvent(AgentError("已中断生成")))
                    break
                self.post_message(UiEvent(event))
        except Exception as exc:
            self.post_message(UiEvent(AgentError(str(exc))))
        finally:
            if cancelled and start is not None and isinstance(messages, list):
                del messages[start:]
            self.post_message(UiDone())

    def _compact_turn(self) -> None:
        try:
            for event in self.session.compact():
                if self._cancel:
                    self.post_message(UiEvent(AgentError("已中断压缩；若未完成替换，原有上下文仍保留。")))
                    break
                self.post_message(UiEvent(event))
        except Exception as exc:
            self.post_message(UiEvent(AgentError(str(exc))))
        finally:
            self.post_message(UiDone())

    def on_ui_event(self, message: UiEvent) -> None:
        self._on_event(message.event)

    def on_ui_done(self, message: UiDone) -> None:
        self._end_turn()

    def _on_event(self, event) -> None:
        transcript = self.query_one(Transcript)
        if isinstance(event, TextDelta):
            self._ensure_agent().append(event.text)
            self._status = "正在回答"
            transcript.scroll_end(animate=False)
        elif isinstance(event, ToolStarted):
            self._assistant = None
            card = ToolCard(event.name, event.arguments)
            self._pending = card
            transcript.mount(card)
            transcript.scroll_end(animate=False)
            self._status = f"执行 {event.name}"
        elif isinstance(event, ToolFinished):
            if self._pending is not None:
                self._pending.finish(event.result)
                self._pending = None
            else:
                card = ToolCard(event.name, "")
                transcript.mount(card)
                card.finish(event.result)
            self._tool_calls += 1
            transcript.scroll_end(animate=False)
        elif isinstance(event, AgentError):
            self._note(event.message, error=True)
        elif isinstance(event, ContextWarning):
            self._note(event.message, error=True)
        elif isinstance(event, CompactFinished):
            self._note(f"压缩完成。归档编号：{event.archive_id}。已用摘要替换历史上下文。")
        elif isinstance(event, TurnFinished):
            if event.answer and self._assistant is None:
                self._ensure_agent().append(event.answer)
        self._paint_status()
        self._paint_sidebar()
        self._paint_banner()

    def _end_turn(self) -> None:
        self._busy = False
        self._cancel = False
        self._assistant = None
        self._pending = None
        if self._turn_started:
            self._last_seconds = time.perf_counter() - self._turn_started
        self._status = "需 /compact" if self._context_blocked() else "就绪"
        box = self.query_one("#prompt", Input)
        box.disabled = False
        box.focus()
        self._paint_status()
        self._paint_sidebar()
        self._paint_banner()
        if self._queue:
            if self._context_blocked():
                self._queue.clear()
                self._note("上下文已达阈值，已清空排队中的提示词。请先 /compact。", error=True)
            else:
                self.call_later(self._pump_queue)

    def _pump_queue(self) -> None:
        if self._turn_is_running() or not self._queue:
            return
        if self._context_blocked():
            self._queue.clear()
            return
        self._start_turn(self._queue.pop(0))

    def _ensure_agent(self) -> AgentLine:
        if self._assistant is None:
            self._assistant = AgentLine()
            self.query_one(Transcript).mount(self._assistant)
        return self._assistant

    def _command(self, text: str) -> None:
        name, _, _rest = text.partition(" ")
        command = name.lower()
        if command == "/help":
            self.action_help()
        elif command == "/clear":
            self.action_clear_screen()
        elif command == "/new":
            self.action_new_session()
        elif command == "/save":
            self.action_save_session()
        elif command == "/resume":
            self.action_resume_session()
        elif command == "/compact":
            self._start_compact()
        elif command == "/quit":
            self.exit()
        elif command == "/tools":
            lines = [f"{tool}  {desc}" for tool, desc in self._catalog()]
            self._note("已注册工具\n" + ("\n".join(lines) if lines else "（无）"))
        elif command == "/mcp":
            self._note(self._mcp_status_text())
        elif command == "/cwd":
            self._note("工作目录\n" + os.getcwd())
        elif command == "/model":
            self._note(f"模型\n{self._model}\n接口\n{getattr(self.session, 'base_url', '') or '—'}")
        elif command == "/config":
            self._note("配置文件\n" + (getattr(self.session, "config_path", "") or "—"))
        elif command == "/status":
            _, value, _ = self._context_meter_parts()
            self._note(
                "会话状态\n"
                f"会话 {self._session_id}\n"
                f"轮次 {self._rounds}\n"
                f"消息 {len(getattr(self.session, 'messages', []) or [])}\n"
                f"工具调用 {self._tool_calls}\n"
                f"上下文 {value}\n"
                f"MCP {getattr(self.session, 'mcp_status', '—')}"
            )
        else:
            known = "、".join(item.name for item in COMMANDS)
            self._note(f"未知命令 {name}。可用命令：{known}", error=True)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_clear_screen(self) -> None:
        if self._busy:
            return
        self.query_one(Transcript).remove_children()
        self._note("已清空界面，对话历史仍保留。")

    def action_save_session(self) -> None:
        if self._busy:
            return
        saver = getattr(self.session, "save", None)
        if saver is None:
            self._note("当前会话不支持保存。", error=True)
            return
        try:
            path = saver(rounds=self._rounds, tool_calls=self._tool_calls)
        except Exception as exc:
            self._note(f"保存失败：{exc}", error=True)
            return
        self._session_id = getattr(self.session, "session_id", self._session_id)
        self._note(f"会话已保存：{path}")
        self._paint_sidebar()

    def action_resume_session(self) -> None:
        if self._busy:
            return
        listing = getattr(self.session, "list_saved", None)
        if listing is None:
            self._note("当前会话不支持恢复。", error=True)
            return
        records = listing()
        if not records:
            self._note("还没有已保存的会话。可先 /save 或 /new（会自动保存）。")
            return

        def on_close(session_id: str | None) -> None:
            if not session_id:
                self._note("已取消恢复。")
                return
            try:
                record = self.session.restore(session_id)
            except Exception as exc:
                self._note(f"恢复失败：{exc}", error=True)
                return
            self.query_one(Transcript).remove_children()
            self._rounds = int(record.rounds or 0)
            self._tool_calls = int(record.tool_calls or 0)
            self._last_seconds = 0.0
            self._session_id = record.id
            self._status = "就绪"
            self._note(f"已恢复会话 {record.id}：{record.title}")
            for message in record.messages:
                role = message.get("role")
                content = str(message.get("content") or "")
                if role == "user" and content:
                    if content.startswith("[AGENTS.md]"):
                        self._note("（已恢复 AGENTS.md 注入消息）")
                    else:
                        self._show_user(content)
                elif role == "assistant" and content:
                    line = AgentLine()
                    line.append(content)
                    self.query_one(Transcript).mount(line)
            self._paint_status()
            self._paint_sidebar()
            self._paint_banner()

        self.push_screen(ResumeScreen(records), on_close)

    def action_new_session(self) -> None:
        if self._busy:
            return
        messages = getattr(self.session, "messages", [])
        if messages:
            saver = getattr(self.session, "save", None)
            if saver is not None:
                try:
                    path = saver(rounds=self._rounds, tool_calls=self._tool_calls)
                    self._note(f"已自动保存上一会话：{path}")
                except Exception as exc:
                    self._note(f"自动保存失败：{exc}", error=True)
        reset = getattr(self.session, "reset", None)
        if reset is not None:
            reset(inject_agents=True)
        self.query_one(Transcript).remove_children()
        self._rounds = 0
        self._tool_calls = 0
        self._last_seconds = 0.0
        self._session_id = getattr(self.session, "session_id", uuid.uuid4().hex[:8])
        self._status = "就绪"
        self._note("已新建会话。")
        if getattr(self.session, "agents_md_injected", False):
            self._note("已重新注入 AGENTS.md。")
        self._paint_status()
        self._paint_sidebar()
        self._paint_banner()

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").toggle_class("-hidden")

    def action_interrupt(self) -> None:
        # 应用级 Esc 带 priority，需先关闭弹层，否则帮助/恢复面板会卡死
        if len(self.screen_stack) > 1:
            top = self.screen
            dismiss = getattr(top, "dismiss", None)
            if callable(dismiss):
                dismiss()
            else:
                self.pop_screen()
            return
        if self._busy:
            self._cancel = True
            self._status = "正在中断"
            self._paint_status()
            return
        if not self.query_one("#slash-panel", Static).has_class("-hidden"):
            self._hide_slash_panel()
            return

    def _note(self, text: str, error: bool = False) -> None:
        style = "bold #e76f51" if error else "bold #adb5bd"
        body = Content.assemble(
            ("错误\n" if error else "提示\n", style),
            text,
        )
        transcript = self.query_one(Transcript)
        transcript.mount(Static(body, classes="msg"))
        transcript.scroll_end(animate=False)

    def _catalog(self) -> tuple:
        catalog = getattr(self.session, "tool_catalog", None)
        if catalog:
            return tuple(catalog)
        return tuple((name, "") for name in getattr(self.session, "tool_names", ()))

    def _context_blocked(self) -> bool:
        return bool(getattr(self.session, "input_blocked", False))

    @staticmethod
    def _format_tokens_k(tokens: int) -> str:
        value = tokens / 1000
        if value >= 100:
            return f"{value:.0f}k"
        if value >= 10:
            return f"{value:.1f}k"
        return f"{value:.2f}k"

    def _context_snapshot(self) -> tuple[int, int, float, bool]:
        used = int(getattr(self.session, "used_tokens", 0) or 0)
        default_window = int(getattr(get_limits(), "context_window_default", 128_000) or 128_000)
        window = int(getattr(self.session, "context_window", default_window) or default_window)
        if window <= 0:
            window = default_window
        ratio = used / window
        is_default = bool(getattr(self.session, "context_window_is_default", True))
        return used, window, ratio, is_default

    def _context_meter_parts(self) -> tuple[str, str, str]:
        used, window, ratio, is_default = self._context_snapshot()
        percent = ratio * 100
        default_mark = "  「默认」" if is_default else ""
        warn_ratio = float(getattr(self.session, "warn_ratio", 0.80) or 0.80)
        label = "上下文"
        value = (
            f"{self._format_tokens_k(used)} / {self._format_tokens_k(window)}"
            f"  ·  {percent:.1f}%{default_mark}"
        )
        if ratio >= warn_ratio:
            style = "bold #e76f51"
        elif ratio >= warn_ratio * 0.75:
            style = "bold #e9c46a"
        else:
            style = "bold #2a9d8f"
        return label, value, style

    def _mcp_status_text(self) -> str:
        status = getattr(self.session, "mcp_status", "idle")
        servers = list(getattr(self.session, "mcp_servers", []) or [])
        errors = tuple(getattr(self.session, "mcp_errors", ()) or ())
        lines = [f"状态：{status}"]
        if not servers and status == "loading":
            lines.append("正在连接 MCP 服务器…")
        for item in servers:
            name = item.get("name") or "?"
            state = item.get("status") or "?"
            tools = item.get("tools") or []
            lines.append(f"- {name} [{state}] · {len(tools)} 工具")
            for tool_name, _desc in tools[:20]:
                lines.append(f"    · {tool_name}")
            if item.get("error"):
                lines.append(f"    ! {item['error']}")
        if errors:
            lines.append("错误：")
            lines.extend(f"- {item}" for item in errors)
        return "MCP\n" + "\n".join(lines)

    def _paint_banner(self) -> None:
        count = len(self._catalog())
        label, value, style = self._context_meter_parts()
        mcp_status = getattr(self.session, "mcp_status", "idle")
        block = Content.assemble(
            (f"TQAgent  ·  {count} 个工具  ·  {self._model}  ·  MCP:{mcp_status}\n", "bold"),
            (f"{label}  ", "bold #f4a261"),
            (value + "\n", style),
            ("工作目录  ", "dim"),
            os.getcwd() + "\n",
            ("/ 命令补全    /resume 恢复    /save 保存    Esc 中断    Ctrl+B 侧栏", "dim"),
        )
        banner = self.query_one("#banner", Static)
        banner.update(block)
        banner.set_class(self._context_blocked(), "-context-warn")

    def _paint_sidebar(self) -> None:
        messages = getattr(self.session, "messages", [])
        message_count = len(messages) if isinstance(messages, list) else 0
        limit = getattr(self.session, "max_calls", 8)
        _, value, _ = self._context_meter_parts()
        local_tools = []
        mcp_names = set()
        for server in getattr(self.session, "mcp_servers", []) or []:
            for tool_name, _desc in server.get("tools") or []:
                mcp_names.add(tool_name)
        for name, desc in self._catalog():
            if name not in mcp_names:
                local_tools.append((name, desc))

        parts: list = [
            ("会话\n", "bold #e9c46a"),
        ]
        for label, item in (
            ("模型", self._model),
            ("接口", _host(getattr(self.session, "base_url", ""))),
            ("目录", _short_path(os.getcwd())),
            ("会话", self._session_id),
            ("轮次", str(self._rounds)),
            ("消息", str(message_count)),
            ("上下文", value),
            ("上限", f"{limit} 次模型调用/轮"),
            ("状态", self._status),
        ):
            parts.append(Content.styled(f"{label:<4}", "dim"))
            parts.append(item + "\n")

        parts.append(("\n本地工具", "bold #e9c46a"))
        parts.append(f"  ·  {len(local_tools)}\n")
        for name, desc in local_tools:
            parts.append(Content.styled(f"• {name}\n", "bold #8ecae6"))
            if desc:
                parts.append(desc + "\n")

        parts.append(("\nMCP", "bold #e9c46a"))
        status = getattr(self.session, "mcp_status", "idle")
        servers = list(getattr(self.session, "mcp_servers", []) or [])
        parts.append(f"  ·  {status}\n")
        if status == "loading" and not servers:
            parts.append(("正在加载…\n", "dim"))
        if not servers and status not in {"loading"}:
            parts.append(("（未连接服务器）\n", "dim"))
        for server in servers:
            name = server.get("name") or "?"
            state = server.get("status") or "?"
            tools = server.get("tools") or []
            parts.append(Content.styled(f"▸ {name} [{state}]\n", "bold #90be6d"))
            if not tools and server.get("error"):
                parts.append(str(server.get("error")) + "\n")
            for tool_name, desc in tools:
                parts.append(Content.styled(f"  • {tool_name}\n", "bold #8ecae6"))
                if desc:
                    short = desc if len(desc) <= 60 else desc[:60] + "…"
                    parts.append(short + "\n")

        info = Content.assemble(*parts)
        sidebar = self.query_one("#sidebar", VerticalScroll)
        existing = sidebar.query("#side-info")
        if existing:
            existing.first().update(info)
        else:
            sidebar.mount(Static(info, id="side-info"))

    def _paint_status(self) -> None:
        elapsed = f"{self._last_seconds:.1f}s" if self._last_seconds else "—"
        if self._busy and self._turn_started:
            elapsed = f"{time.perf_counter() - self._turn_started:.1f}s"
        waiting = f"     排队 {len(self._queue)}" if self._queue else ""
        _, value, _ = self._context_meter_parts()
        mcp = getattr(self.session, "mcp_status", "idle")
        text = (
            f"{self._status}  ·  {self._rounds} 轮     {self._model}"
            f"     上下文 {value}     MCP {mcp}"
            f"     工具 {self._tool_calls} 次     {elapsed}{waiting}"
        )
        self.query_one("#statusbar", Static).update(text)


def run(session, model_name: str, *, config_created: bool = False) -> None:
    TQAgentApp(session, model_name, config_created=config_created).run()
