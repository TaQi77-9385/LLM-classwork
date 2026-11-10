"""把本地工具和已连接的 MCP 工具合成一份给模型的工具表。"""

import json

from core.cli import SCHEMA as CLI_SCHEMA
from core.cli import RunCli
from core.context import SCHEMA as CONTEXT_SCHEMA
from core.context import recallContext
from core.fetch import SCHEMA as FETCH_SCHEMA
from core.fetch import fetchWeb
from core.files import HANDLERS as FILE_HANDLERS
from core.files import TOOL_SCHEMAS as FILE_SCHEMAS
from core.files import run_tool as run_file_tool
from core.mcp_client import current_hub
from core.search import SCHEMA as SEARCH_SCHEMA
from core.search import searchWeb
from core.skills import CREATE_SCHEMA
from core.skills import SCHEMA as SKILL_SCHEMA
from core.skills import createSkill, loadSkill

TOOL_SCHEMAS = [
    *FILE_SCHEMAS,
    SEARCH_SCHEMA,
    FETCH_SCHEMA,
    CLI_SCHEMA,
    SKILL_SCHEMA,
    CREATE_SCHEMA,
    CONTEXT_SCHEMA,
]
TOOL_NAMES = tuple(item["function"]["name"] for item in TOOL_SCHEMAS)

_SEARCH_HANDLERS = {
    "searchWeb": searchWeb,
    "fetchWeb": fetchWeb,
    "RunCli": RunCli,
    "loadSkill": loadSkill,
    "createSkill": createSkill,
    "recallContext": recallContext,
}
_LOCAL = set(_SEARCH_HANDLERS) | set(FILE_HANDLERS)


def run_tool(call, hub=None):
    name = (call.get("function") or {}).get("name")
    active = current_hub() if hub is None else hub
    if name not in _LOCAL and active is not None and active.owns(name):
        return _run_mcp(active, call)
    handler = _SEARCH_HANDLERS.get(name)
    if handler is None:
        return run_file_tool(call)
    try:
        args = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        return f"参数不是合法 JSON: {exc}"
    try:
        return str(handler(**args))
    except TypeError as exc:
        return f"参数错误: {exc}"
    except Exception as exc:
        return f"工具执行失败：{exc}"


def _run_mcp(hub, call):
    name = (call.get("function") or {}).get("name")
    try:
        args = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        return f"参数不是合法 JSON: {exc}"
    if not isinstance(args, dict):
        return "参数错误: MCP 工具参数必须是 JSON 对象"
    return hub.invoke(name, args)
