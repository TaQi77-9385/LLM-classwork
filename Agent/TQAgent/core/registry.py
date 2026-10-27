"""把本地文件、网络搜索、网页抓取和命令行调用合成一份给模型的工具表。"""

import json

from core.cli import SCHEMA as CLI_SCHEMA
from core.cli import RunCli
from core.fetch import SCHEMA as FETCH_SCHEMA
from core.fetch import fetchWeb
from core.files import TOOL_SCHEMAS as FILE_SCHEMAS
from core.files import run_tool as run_file_tool
from core.search import SCHEMA as SEARCH_SCHEMA
from core.search import searchWeb

TOOL_SCHEMAS = [*FILE_SCHEMAS, SEARCH_SCHEMA, FETCH_SCHEMA, CLI_SCHEMA]
TOOL_NAMES = tuple(item["function"]["name"] for item in TOOL_SCHEMAS)

_SEARCH_HANDLERS = {"searchWeb": searchWeb, "fetchWeb": fetchWeb, "RunCli": RunCli}


def run_tool(call):
    name = (call.get("function") or {}).get("name")
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
