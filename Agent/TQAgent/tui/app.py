"""Textual 前端。布局对齐参考页：对话区、可展开工具卡片、右侧会话栏。"""

import json
import os
import time
import uuid
from urllib.parse import urlparse

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Footer, Header, Input, Markdown, Static

from core.events import AgentError, TextDelta, ToolFinished, ToolStarted, TurnFinished

_RESULT_LIMIT = 2000
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
)

_HELP = """
# TQAgent 帮助

## 使用方式

- 在底部输入框输入问题，**Enter** 发送。
- 模型需要查看或修改文件时会自动调用工具，工具卡片可点击展开。
- **Esc** 中断当前生成；已完成的问答才会写入历史。

## 命令

| 命令 | 作用 |
| --- | --- |
| /help | 打开本帮助 |
| /clear | 清空界面（保留历史） |
| /new | 新建会话（重置历史） |
| /tools | 列出已注册工具 |
| /cwd | 显示工作目录 |
| /model | 显示模型与接口 |
| /quit | 退出 |

## 快捷键

| 按键 | 作用 |
| --- | --- |
| Enter | 发送 |
| Esc | 中断生成 |
| Ctrl+B | 显示或隐藏侧栏 |
| Ctrl+N | 新会话 |
| F1 | 帮助 |
| q / l / n / b | 退出、清屏、新会话、侧栏（焦点不在输入框时） |
""".strip()


def _clip(text: str) -> str:
    if len(text) <= _RESULT_LIMIT:
        return text
    return text[:_RESULT_LIMIT] + "\n…（界面已截断，模型收到的是完整结果）"


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


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss", "关闭", show=False)]

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="help-dialog"):
            yield Markdown(_HELP)


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
        Binding("escape", "interrupt", "中断", priority=True),
    ]

    def __init__(self, session, model_name: str):
        super().__init__()
        self.session = session
        self._model = model_name or getattr(session, "model", "") or "—"
        self.sub_title = self._model
        self._assistant: AgentLine | None = None
        self._pending: ToolCard | None = None
        self._busy = False
        self._cancel = False
        self._rounds = 0
        self._tool_calls = 0
        self._last_seconds = 0.0
        self._turn_started = 0.0
        self._status = "就绪"
        self._session_id = uuid.uuid4().hex[:8]
        self._worker = None
        self._queue: list[str] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="main"):
                yield Static("", id="banner")
                yield Transcript(id="transcript")
                yield Input(
                    placeholder="输入问题后 Enter 发送；/help 查看命令；Esc 中断生成",
                    id="prompt",
                )
            yield VerticalScroll(id="sidebar")
        yield Static("", id="statusbar")
        yield Footer()

    def on_mount(self) -> None:
        self._paint_banner()
        self._paint_sidebar()
        self._paint_status()
        self._note("在下方输入问题，按 Enter 发送。上一条回答结束前，下一条会先显示在这里并排队。")
        self.query_one("#prompt", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or event.input.disabled:
            return
        event.input.value = ""
        if text.startswith("/"):
            self._command(text)
            return
        self._show_user(text)
        if self._turn_is_running():
            self._queue.append(text)
            self._paint_status()
            return
        self._start_turn(text)

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
        self._worker = self.run_worker(lambda: self._turn(text), thread=True, exclusive=True)

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
        elif isinstance(event, TurnFinished):
            if event.answer and self._assistant is None:
                self._ensure_agent().append(event.answer)
        self._paint_status()
        self._paint_sidebar()

    def _end_turn(self) -> None:
        self._busy = False
        self._cancel = False
        self._assistant = None
        self._pending = None
        if self._turn_started:
            self._last_seconds = time.perf_counter() - self._turn_started
        self._status = "就绪"
        box = self.query_one("#prompt", Input)
        box.disabled = False
        box.focus()
        self._paint_status()
        self._paint_sidebar()
        if self._queue:
            self.call_later(self._pump_queue)

    def _pump_queue(self) -> None:
        if self._turn_is_running() or not self._queue:
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
        elif command == "/quit":
            self.exit()
        elif command == "/tools":
            lines = [f"{tool}  {desc}" for tool, desc in self._catalog()]
            self._note("已注册工具\n" + ("\n".join(lines) if lines else "（无）"))
        elif command == "/cwd":
            self._note("工作目录\n" + os.getcwd())
        elif command == "/model":
            self._note(f"模型\n{self._model}\n接口\n{getattr(self.session, 'base_url', '') or '—'}")
        else:
            self._note(f"未知命令 {name}。输入 /help 查看命令。", error=True)

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_clear_screen(self) -> None:
        if self._busy:
            return
        self.query_one(Transcript).remove_children()
        self._note("已清空界面，对话历史仍保留。")

    def action_new_session(self) -> None:
        if self._busy:
            return
        reset = getattr(self.session, "reset", None)
        if reset is not None:
            reset()
        self.query_one(Transcript).remove_children()
        self._rounds = 0
        self._tool_calls = 0
        self._last_seconds = 0.0
        self._session_id = uuid.uuid4().hex[:8]
        self._status = "就绪"
        self._note("已新建会话。")
        self._paint_status()
        self._paint_sidebar()

    def action_toggle_sidebar(self) -> None:
        self.query_one("#sidebar").toggle_class("-hidden")

    def action_interrupt(self) -> None:
        if self._busy:
            self._cancel = True
            self._status = "正在中断"
            self._paint_status()

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

    def _paint_banner(self) -> None:
        count = len(self._catalog())
        block = Content.assemble(
            (f"TQAgent  ·  {count} 个工具  ·  {self._model}\n", "bold"),
            ("工作目录  ", "dim"),
            os.getcwd() + "\n",
            ("Enter 发送    /help 命令    Esc 中断生成    Ctrl+B 侧栏    Ctrl+N 新会话", "dim"),
        )
        self.query_one("#banner", Static).update(block)

    def _paint_sidebar(self) -> None:
        messages = getattr(self.session, "messages", [])
        message_count = len(messages) if isinstance(messages, list) else 0
        limit = getattr(self.session, "max_calls", 8)
        info = Content.assemble(
            ("会话\n", "bold #e9c46a"),
            *[
                part
                for label, value in (
                    ("模型", self._model),
                    ("接口", _host(getattr(self.session, "base_url", ""))),
                    ("目录", _short_path(os.getcwd())),
                    ("会话", self._session_id),
                    ("轮次", str(self._rounds)),
                    ("消息", str(message_count)),
                    ("上限", f"{limit} 次模型调用/轮"),
                    ("状态", self._status),
                )
                for part in (Content.styled(f"{label:<4}", "dim"), value + "\n")
            ],
            ("\n工具", "bold #e9c46a"),
            f"  ·  {len(self._catalog())}\n",
            *[
                part
                for name, desc in self._catalog()
                for part in (
                    Content.styled(f"• {name}\n", "bold #8ecae6"),
                    (desc + "\n") if desc else "",
                )
            ],
        )
        sidebar = self.query_one("#sidebar", VerticalScroll)
        existing = sidebar.query("#side-info")
        block = Static(info, id="side-info")
        if existing:
            existing.first().update(info)
        else:
            sidebar.mount(block)

    def _paint_status(self) -> None:
        elapsed = f"{self._last_seconds:.1f}s" if self._last_seconds else "—"
        if self._busy and self._turn_started:
            elapsed = f"{time.perf_counter() - self._turn_started:.1f}s"
        waiting = f"     排队 {len(self._queue)}" if self._queue else ""
        text = f"{self._status}  ·  {self._rounds} 轮     {self._model}     工具 {self._tool_calls} 次     {elapsed}{waiting}"
        self.query_one("#statusbar", Static).update(text)


def run(session, model_name: str) -> None:
    TQAgentApp(session, model_name).run()
