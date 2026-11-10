"""界面无关的事件。TUI、Web、Desktop、Mobile 都消费这一组对象。"""

from dataclasses import dataclass


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolStarted:
    name: str
    arguments: str


@dataclass(frozen=True)
class ToolFinished:
    name: str
    result: str


@dataclass(frozen=True)
class TurnFinished:
    answer: str


@dataclass(frozen=True)
class AgentError:
    message: str


@dataclass(frozen=True)
class ContextWarning:
    """上下文占用达到阈值时提示用户执行 /compact。"""

    used: int
    window: int
    ratio: float
    message: str


@dataclass(frozen=True)
class CompactFinished:
    archive_id: str
    summary: str


AgentEvent = (
    TextDelta
    | ToolStarted
    | ToolFinished
    | TurnFinished
    | AgentError
    | ContextWarning
    | CompactFinished
)
