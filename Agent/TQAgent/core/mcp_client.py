"""连接当前项目目录里的 MCP Server，并把它们的工具并进 Function Calling。

配置文件是启动时工作目录下的 .mcp.json，不读取源代码所在目录。
支持 stdio 与 Streamable HTTP（type 可为 http、streamable-http 或 streamable_http）。
某个服务器连不上时只记录错误，本地工具和其他服务器继续可用。
"""

import asyncio
import atexit
import json
import os
import re
import sys
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = ".mcp.json"
CONNECT_TIMEOUT = 30
CALL_TIMEOUT = 60
MAX_OUTPUT = 8000

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HTTP_TYPES = {"http", "streamablehttp"}


@dataclass(frozen=True)
class ServerSpec:
    name: str
    transport: str
    command: str = ""
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    cwd: str | None = None
    url: str = ""
    headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ToolRoute:
    server: str
    tool: str


def expose_name(server: str, tool: str, taken: set[str]) -> str:
    """工具名与本地工具或其他服务器冲突时，加上服务器名前缀。"""
    if _NAME.fullmatch(tool) and tool not in taken:
        return tool
    return _fit(f"{_token(server)}__{_token(tool)}", taken)


def format_tool_result(result) -> str:
    """把 MCP 工具结果收成一段文本，保留正文、结构化数据和错误。"""
    is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
    text = _content_text(getattr(result, "content", None))
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    extra = ""
    if structured is not None and not _same_json(text, structured):
        extra = _structured_text(structured)
    if is_error:
        detail = text or "服务器没有返回错误说明"
        if extra and extra not in detail:
            detail = f"{detail}\n{extra}"
        return _clip(f"MCP 工具执行失败：{detail}")
    parts: list[str] = []
    if text:
        parts.append(text)
    if extra and extra not in text:
        parts.append(extra if not text else "结构化数据：\n" + extra)
    if not parts:
        return "（无输出）"
    return _clip("\n\n".join(parts))


def load_mcp_config(path: Path, project_root: Path | None = None) -> tuple[list[ServerSpec], list[str]]:
    """解析 .mcp.json。配置问题写进错误列表，不抛给启动流程。"""
    root = Path.cwd() if project_root is None else Path(project_root)
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return [], [f"无法读取 {path}：{exc}"]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], [f"{path} 不是合法 JSON：{exc}"]
    if not isinstance(data, dict):
        return [], [f"{path} 必须是 JSON 对象"]
    servers = data.get("mcpServers")
    if servers is None:
        return [], [f"{path} 缺少 mcpServers"]
    if not isinstance(servers, dict):
        return [], [f"{path} 的 mcpServers 必须是 JSON 对象"]
    specs: list[ServerSpec] = []
    errors: list[str] = []
    for name, entry in servers.items():
        spec, error = _parse_server(str(name), entry, root)
        if error:
            errors.append(error)
        elif spec is not None:
            specs.append(spec)
    return specs, errors


class McpHub:
    """在后台事件循环里保持各个 MCP 连接，供同步的 run_tool 调用。"""

    def __init__(self) -> None:
        self.schemas: list[dict] = []
        self.routes: dict[str, ToolRoute] = {}
        self.errors: list[str] = []
        self.server_infos: list[dict] = []
        self._clients: dict = {}
        self._queues: dict[str, asyncio.Queue] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._closed = False

    def fail(self, message: str) -> None:
        self.errors.append(message)
        print(message, file=sys.stderr)

    def mark_server(self, name: str, status: str, tools: list | None = None, error: str = "") -> None:
        tools = list(tools or [])
        for index, item in enumerate(self.server_infos):
            if item.get("name") == name:
                self.server_infos[index] = {
                    "name": name,
                    "status": status,
                    "tools": tools,
                    "error": error,
                }
                return
        self.server_infos.append({
            "name": name,
            "status": status,
            "tools": tools,
            "error": error,
        })

    def owns(self, name: str) -> bool:
        return name in self.routes

    def start(self) -> None:
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            return
        started = threading.Event()

        def main() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.call_soon(started.set)
            try:
                loop.run_forever()
            finally:
                try:
                    loop.close()
                except Exception:
                    pass

        self._thread = threading.Thread(target=main, name="tqagent-mcp", daemon=True)
        self._thread.start()
        if not started.wait(5):
            raise RuntimeError("MCP 事件循环没有启动")

    def submit(self, coro, timeout: float):
        loop = self._loop
        if loop is None or self._closed:
            coro.close()
            raise RuntimeError("MCP 事件循环未启动")
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=timeout)
        except TimeoutError as exc:
            future.cancel()
            if str(exc).strip():
                raise
            raise TimeoutError(f"超时（{int(timeout)} 秒）") from exc

    def connect_one(self, spec: ServerSpec):
        return self.submit(self._connect_one(spec), CONNECT_TIMEOUT + 5)

    def add_tools(self, server: str, tools: list, taken: set[str]) -> None:
        exposed_tools: list[tuple[str, str]] = []
        for tool in tools:
            original = str(_field(tool, "name", "") or "").strip()
            if not original:
                self.fail(f"MCP Server「{server}」返回了没有名称的工具，已跳过")
                continue
            exposed = expose_name(server, original, taken)
            taken.add(exposed)
            self.routes[exposed] = ToolRoute(server, original)
            description = _description(server, tool, exposed)
            self.schemas.append({
                "type": "function",
                "function": {
                    "name": exposed,
                    "description": description,
                    "parameters": _parameters(tool),
                },
            })
            exposed_tools.append((exposed, description))
        self.mark_server(server, "connected", exposed_tools)

    def invoke(self, exposed: str, arguments: dict) -> str:
        route = self.routes.get(exposed)
        if route is None:
            return "未知工具"
        if self._loop is None or self._closed:
            return f"MCP 工具执行失败：MCP Server「{route.server}」未连接"
        try:
            result = self.submit(
                self._request(route.server, route.tool, arguments),
                CALL_TIMEOUT + 10,
            )
        except Exception as exc:
            return f"MCP 工具执行失败：{_error_text(exc)}"
        return format_tool_result(result)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None or thread is threading.current_thread():
            return

        async def shutdown() -> None:
            for stop in list(self._stops.values()):
                stop.set()
            pending = [task for task in self._tasks.values() if not task.done()]
            if not pending:
                return
            _, still = await asyncio.wait(pending, timeout=5)
            for task in still:
                task.cancel()
            if still:
                await asyncio.wait(still, timeout=3)

        try:
            asyncio.run_coroutine_threadsafe(shutdown(), loop).result(timeout=10)
        except Exception:
            pass
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        thread.join(timeout=2)

    async def _connect_one(self, spec: ServerSpec):
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        stop = asyncio.Event()
        queue: asyncio.Queue = asyncio.Queue()
        self._stops[spec.name] = stop
        self._queues[spec.name] = queue
        task = asyncio.create_task(self._hold(spec, ready, stop, queue), name=f"mcp-{spec.name}")
        self._tasks[spec.name] = task
        task.add_done_callback(_consume_task)
        try:
            return await asyncio.wait_for(ready, CONNECT_TIMEOUT)
        except TimeoutError:
            stop.set()
            task.cancel()
            raise TimeoutError(f"连接超时（{CONNECT_TIMEOUT} 秒）")
        except Exception:
            stop.set()
            if not task.done():
                task.cancel()
            raise

    async def _hold(self, spec: ServerSpec, ready, stop: asyncio.Event, queue: asyncio.Queue) -> None:
        reply = None
        try:
            async with _open(spec) as client:
                tools = await _list_tools(client)
                self._clients[spec.name] = client
                if ready.done():
                    return
                ready.set_result(tools)
                while not stop.is_set():
                    try:
                        tool_name, arguments, reply = await asyncio.wait_for(queue.get(), 0.2)
                    except TimeoutError:
                        continue
                    try:
                        result = await client.call_tool(
                            tool_name,
                            arguments,
                            read_timeout_seconds=CALL_TIMEOUT,
                        )
                    except Exception as exc:
                        if not reply.done():
                            reply.set_exception(exc)
                    else:
                        if not reply.done():
                            reply.set_result(result)
                    reply = None
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            elif reply is not None and not reply.done():
                reply.set_exception(exc)
        finally:
            self._clients.pop(spec.name, None)
            pending = []
            if reply is not None:
                pending.append(reply)
            while True:
                try:
                    pending.append(queue.get_nowait()[2])
                except asyncio.QueueEmpty:
                    break
            reason = RuntimeError(f"MCP Server「{spec.name}」已断开")
            for item in pending:
                if item is not None and not item.done():
                    item.set_exception(reason)

    async def _request(self, server: str, tool: str, arguments: dict):
        if server not in self._clients or server not in self._queues:
            raise RuntimeError(f"MCP Server「{server}」未连接")
        reply = asyncio.get_running_loop().create_future()
        await self._queues[server].put((tool, arguments, reply))
        try:
            return await asyncio.wait_for(reply, CALL_TIMEOUT + 5)
        except TimeoutError:
            raise TimeoutError(f"调用超时（{CALL_TIMEOUT} 秒）")


_hub: McpHub | None = None


def bind_mcp(hub: McpHub) -> None:
    global _hub
    previous = _hub
    _hub = hub
    if previous is not None and previous is not hub:
        previous.close()


def current_hub() -> McpHub | None:
    return _hub


def connect_mcp(project_root: Path | None = None, reserved: Iterator[str] | None = None) -> McpHub:
    """读取项目根目录的 .mcp.json 并连接其中的服务器。"""
    if reserved is None:
        from core.registry import TOOL_NAMES

        reserved = TOOL_NAMES
    root = Path.cwd() if project_root is None else Path(project_root)
    hub = McpHub()
    path = root / CONFIG_NAME
    specs: list[ServerSpec] = []
    if path.is_file():
        specs, errors = load_mcp_config(path, root)
        for message in errors:
            hub.fail(message)
    if specs:
        try:
            hub.start()
        except Exception as exc:
            hub.fail(f"MCP 客户端启动失败：{_error_text(exc)}")
            specs = []
    taken = set(reserved)
    for spec in specs:
        hub.mark_server(spec.name, "connecting")
        try:
            tools = hub.connect_one(spec)
        except Exception as exc:
            message = f"MCP Server「{spec.name}」连接失败：{_error_text(exc)}"
            hub.fail(message)
            hub.mark_server(spec.name, "failed", error=message)
            continue
        hub.add_tools(spec.name, tools, taken)
    bind_mcp(hub)
    return hub


def _stdio_errlog():
    """子进程的 stderr 必须是操作系统文件。测试或界面替换 sys.stderr 时退回真实错误流。"""
    stream = sys.stderr
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        return sys.__stderr__
    return stream


def _close_bound() -> None:
    hub = _hub
    if hub is not None:
        hub.close()


atexit.register(_close_bound)


@asynccontextmanager
async def _open(spec: ServerSpec):
    try:
        from mcp import Client, StdioServerParameters
    except ImportError as exc:
        raise RuntimeError("未安装 MCP SDK。请执行 pip install mcp") from exc
    if spec.transport == "stdio":
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=spec.command,
            args=list(spec.args),
            env=dict(spec.env) if spec.env else None,
            cwd=spec.cwd,
        )
        async with Client(stdio_client(params, errlog=_stdio_errlog())) as client:
            yield client
        return
    headers = dict(spec.headers) if spec.headers else None
    if not headers:
        async with Client(spec.url) as client:
            yield client
        return
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    timeout = httpx2.Timeout(30.0, read=300.0)
    async with httpx2.AsyncClient(headers=headers, timeout=timeout) as http:
        transport = streamable_http_client(spec.url, http_client=http)
        async with Client(transport) as client:
            yield client


async def _list_tools(client) -> list:
    tools = []
    cursor = None
    seen: set[str] = set()
    while True:
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools or [])
        cursor = getattr(page, "next_cursor", None) or getattr(page, "nextCursor", None)
        if not cursor or cursor in seen or len(seen) >= 100:
            return tools
        seen.add(cursor)


def _parse_server(name: str, entry, root: Path) -> tuple[ServerSpec | None, str]:
    label = f"MCP Server「{name or '未命名'}」"
    if not str(name).strip():
        return None, f"{label}配置无效：名称不能为空"
    if not isinstance(entry, dict):
        return None, f"{label}配置无效：必须是 JSON 对象"
    raw_type = entry.get("type", "")
    if raw_type is None:
        raw_type = ""
    if not isinstance(raw_type, str):
        return None, f"{label}配置无效：type 必须是字符串"
    kind = raw_type.strip().lower().replace("-", "").replace("_", "")
    if kind in {"", "stdio"}:
        transport = "stdio"
    elif kind in _HTTP_TYPES:
        transport = "http"
    elif kind == "sse":
        return None, f"{label}配置无效：暂不支持 SSE，请改用 streamable_http"
    else:
        return None, f"{label}配置无效：不支持的类型 {raw_type}"

    command = entry.get("command", "")
    url = entry.get("url", "")
    has_command = isinstance(command, str) and bool(command.strip())
    has_url = isinstance(url, str) and bool(url.strip())
    if not raw_type.strip() and has_command and has_url:
        return None, f"{label}配置无效：同时写了 command 和 url，请设置 type 为 stdio 或 streamable_http"
    if not raw_type.strip() and has_url and not has_command:
        transport = "http"

    reasons: list[str] = []
    missing: list[str] = []
    command_text = ""
    args: tuple[str, ...] = ()
    env: dict[str, str] | None = None
    cwd = None
    url_text = ""
    headers: dict[str, str] | None = None

    if transport == "stdio":
        if not has_command:
            reasons.append("缺少 command")
        else:
            command_text, absent = _expand(str(command).strip())
            missing.extend(absent)
            if not command_text.strip():
                reasons.append("缺少 command")
        args, arg_error = _parse_args(entry.get("args"), missing)
        if arg_error:
            reasons.append(arg_error)
        env, env_error, absent = _string_map(entry.get("env"), "env")
        if env_error:
            reasons.append(env_error)
        missing.extend(absent)
        cwd = _parse_cwd(entry.get("cwd"), root, reasons, missing)
        if cwd is None and "cwd" not in entry:
            cwd = str(root.resolve())
    else:
        if not isinstance(url, str) or not url.strip():
            reasons.append("缺少 url")
        else:
            url_text, absent = _expand(url.strip())
            missing.extend(absent)
            url_error = _check_url(url_text)
            if url_error:
                reasons.append(url_error)
        headers, header_error, absent = _string_map(entry.get("headers"), "headers")
        if header_error:
            reasons.append(header_error)
        missing.extend(absent)

    if missing:
        reasons.append("环境变量未设置：" + "、".join(dict.fromkeys(missing)))
    if reasons:
        return None, f"{label}配置无效：{'；'.join(reasons)}"
    return ServerSpec(
        name=name,
        transport=transport,
        command=command_text.strip(),
        args=args,
        env=tuple(sorted((env or {}).items())),
        cwd=cwd,
        url=url_text,
        headers=tuple(sorted((headers or {}).items())),
    ), ""


def _parse_args(value, missing: list[str]) -> tuple[tuple[str, ...], str]:
    if value is None:
        return (), ""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return (), "args 必须是字符串列表"
    expanded = []
    for item in value:
        text, absent = _expand(item)
        missing.extend(absent)
        expanded.append(text)
    return tuple(expanded), ""


def _parse_cwd(value, root: Path, reasons: list[str], missing: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        reasons.append("cwd 必须是字符串")
        return None
    text, absent = _expand(value.strip())
    missing.extend(absent)
    path = Path(text)
    if not path.is_absolute():
        path = root / path
    return str(path.resolve())


def _string_map(value, label: str) -> tuple[dict[str, str] | None, str, list[str]]:
    if value is None:
        return None, "", []
    if not isinstance(value, dict):
        return None, f"{label} 必须是 JSON 对象", []
    mapped: dict[str, str] = {}
    missing: list[str] = []
    for key, item in value.items():
        if not isinstance(key, str) or not key.strip():
            return None, f"{label} 的键必须是非空字符串", missing
        if isinstance(item, bool) or not isinstance(item, str):
            return None, f"{label}.{key} 必须是字符串", missing
        text, absent = _expand(item)
        missing.extend(absent)
        mapped[key] = text
    return mapped, "", missing


def _expand(value: str) -> tuple[str, list[str]]:
    missing: list[str] = []

    def repl(match: re.Match) -> str:
        key = match.group(1)
        if key not in os.environ:
            missing.append(key)
            return match.group(0)
        return os.environ[key]

    return _VAR.sub(repl, value), missing


def _check_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "url 必须是 http 或 https 地址"
    return ""


def _description(server: str, tool, exposed: str) -> str:
    text = str(_field(tool, "description", "") or "").strip()
    title = str(_field(tool, "title", "") or "").strip()
    if title and title not in text:
        text = f"{title}。{text}" if text else title
    note = f"MCP 服务器 {server}"
    original = str(_field(tool, "name", "") or "")
    if exposed != original:
        note += f"，原名 {original}"
    if text:
        return f"{text}（{note}）"
    return note


def _parameters(tool) -> dict:
    raw = _field(tool, "input_schema", None)
    if raw is None:
        raw = _field(tool, "inputSchema", None)
    if hasattr(raw, "model_dump"):
        raw = raw.model_dump(by_alias=True, exclude_none=True)
    if not isinstance(raw, dict):
        return {"type": "object", "properties": {}}
    schema = dict(raw)
    schema.pop("$schema", None)
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def _field(obj, name: str, default):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        content = [content]
    chunks = []
    for block in content:
        piece = _block_text(block)
        if piece:
            chunks.append(piece)
    return "\n".join(chunks)


def _block_text(block) -> str:
    if isinstance(block, str):
        return block
    kind = str(_field(block, "type", "") or "")
    if kind == "text" or (not kind and _field(block, "text", None) is not None):
        return str(_field(block, "text", "") or "")
    if kind == "image":
        mime = _field(block, "mime_type", None) or _field(block, "mimeType", None) or "image"
        data = _field(block, "data", "") or ""
        return f"[图片 {mime}，数据长度 {len(data)}]"
    if kind == "audio":
        mime = _field(block, "mime_type", None) or _field(block, "mimeType", None) or "audio"
        return f"[音频 {mime}]"
    if kind == "resource":
        return _resource_text(_field(block, "resource", None))
    if kind == "resource_link":
        uri = _field(block, "uri", "") or ""
        label = str(_field(block, "name", "") or "")
        text = f"[资源 {uri}]"
        return f"{text} {label}".strip()
    text = _field(block, "text", None)
    if isinstance(text, str):
        return text
    return ""


def _resource_text(resource) -> str:
    if resource is None:
        return ""
    text = _field(resource, "text", None)
    if isinstance(text, str) and text:
        return text
    uri = _field(resource, "uri", "") or ""
    if uri:
        return f"[资源 {uri}]"
    return "[二进制资源]"


def _structured_text(value) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _same_json(text: str, structured) -> bool:
    if not text or structured is None:
        return False
    try:
        return json.loads(text) == structured
    except (json.JSONDecodeError, TypeError, ValueError):
        return False


def _clip(text: str) -> str:
    if len(text) <= MAX_OUTPUT:
        return text
    note = f"\n（已截断，原长 {len(text)} 字符）"
    keep = MAX_OUTPUT - len(note)
    if keep < 1:
        return text[:MAX_OUTPUT]
    return text[:keep] + note


def _token(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value).strip("_")
    return cleaned or "mcp"


def _fit(name: str, taken: set[str]) -> str:
    base = _token(name)[:64]
    if _NAME.fullmatch(base) and base not in taken:
        return base
    number = 2
    while number < 10000:
        suffix = f"_{number}"
        candidate = base[: 64 - len(suffix)] + suffix
        if _NAME.fullmatch(candidate) and candidate not in taken:
            return candidate
        number += 1
    return f"mcp_{len(taken)}"[:64]


def _error_text(exc: BaseException) -> str:
    nested = getattr(exc, "exceptions", None)
    if isinstance(nested, tuple) and nested:
        return "；".join(part for part in (_error_text(item) for item in nested) if part)
    text = str(exc).strip()
    return text or exc.__class__.__name__


def _consume_task(task: asyncio.Task) -> None:
    if task.cancelled():
        return
    try:
        task.exception()
    except Exception:
        return
