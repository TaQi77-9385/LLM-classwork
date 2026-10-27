"""本地文件工具。只返回字符串，失败也不向外抛业务异常。"""

import json
import mimetypes
import os
import shutil
from pathlib import Path

MAX_TEXT_BYTES = 1024 * 1024

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "getCwd",
            "description": "获取当前工作目录",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listDir",
            "description": "列出指定目录下的所有文件和子目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "目录路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "getFileType",
            "description": "获取指定文件的类型，包括扩展名、MIME 和是否为纯文本",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "readFile",
            "description": "获取指定文件的内容，仅支持纯文本文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "createFile",
            "description": "在指定目录下按文件名创建新的纯文本文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {"type": "string", "description": "目录路径"},
                    "filename": {"type": "string", "description": "文件名，不能包含路径分隔符"},
                    "content": {"type": "string", "description": "初始内容，可省略"},
                },
                "required": ["directory", "filename"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "editFile",
            "description": "用新内容覆盖指定纯文本文件的全部内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "content": {"type": "string", "description": "写入的全部新内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "createDir",
            "description": "创建新目录",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "新目录路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deleteDir",
            "description": "删除目录及其全部内容，目录可以非空",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "要删除的目录路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "deleteFile",
            "description": "删除指定文件",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "文件路径"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "renamePath",
            "description": "重命名文件或目录。new_path 为文件名时留在原目录，为路径时移动到该位置",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "原文件或目录路径"},
                    "new_path": {"type": "string", "description": "新名称或新路径"},
                },
                "required": ["path", "new_path"],
            },
        },
    },
]


def _as_path(value):
    return Path(os.path.expandvars(str(value))).expanduser()


def _plain_text_error(path):
    if not path.is_file():
        return "文件不存在"
    size = path.stat().st_size
    if size > MAX_TEXT_BYTES:
        return "文件超过 1MB，不按纯文本处理"
    data = path.read_bytes()
    if b"\x00" in data:
        return "不是纯文本文件"
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return "不是 UTF-8 纯文本文件"
    return ""


def getCwd():
    return os.getcwd()


def listDir(path):
    target = _as_path(path)
    if not target.is_dir():
        return f"目录不存在: {path}"
    lines = []
    for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if item.is_dir():
            kind = "目录"
        elif item.is_file():
            kind = "文件"
        else:
            kind = "其他"
        lines.append(f"[{kind}] {item.name}")
    return "\n".join(lines) if lines else "(空目录)"


def getFileType(path):
    target = _as_path(path)
    if not target.exists():
        return f"路径不存在: {path}"
    if target.is_dir():
        return "目录"
    mime = mimetypes.guess_type(target.name)[0] or "未知"
    suffix = target.suffix or "(无扩展名)"
    text_error = _plain_text_error(target)
    kind = "纯文本" if not text_error else text_error
    return f"扩展名: {suffix}; MIME: {mime}; {kind}"


def readFile(path):
    target = _as_path(path)
    error = _plain_text_error(target)
    if error:
        return error
    return target.read_text(encoding="utf-8")


def createFile(directory, filename, content=""):
    name = str(filename)
    if name != Path(name).name or name in {"", ".", ".."}:
        return "文件名不能包含路径"
    folder = _as_path(directory)
    if not folder.is_dir():
        return f"目录不存在: {directory}"
    target = folder / name
    if target.exists():
        return f"文件已存在: {target}"
    target.write_text(content or "", encoding="utf-8")
    return f"已创建: {target}"


def editFile(path, content):
    target = _as_path(path)
    error = _plain_text_error(target)
    if error:
        return error
    target.write_text("" if content is None else str(content), encoding="utf-8")
    return f"已更新: {target}"


def createDir(path):
    target = _as_path(path)
    target.mkdir(parents=False, exist_ok=False)
    return f"已创建目录: {target}"


def deleteDir(path):
    target = _as_path(path)
    if not target.is_dir():
        return f"目录不存在: {path}"
    shutil.rmtree(target)
    return f"已删除目录: {target}"


def deleteFile(path):
    target = _as_path(path)
    if target.is_dir():
        return "这是目录，请使用 deleteDir"
    if not target.is_file():
        return f"文件不存在: {path}"
    target.unlink()
    return f"已删除文件: {target}"


def renamePath(path, new_path):
    source = _as_path(path)
    if not source.exists():
        return f"路径不存在: {path}"
    raw_new = str(new_path)
    dest = _as_path(raw_new)
    if not Path(raw_new).expanduser().is_absolute() and not os.path.splitdrive(raw_new)[0]:
        dest = source.parent / raw_new
    if dest.exists():
        return f"目标已存在: {dest}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)
    return f"已重命名: {source} -> {dest}"


HANDLERS = {
    "getCwd": getCwd,
    "listDir": listDir,
    "getFileType": getFileType,
    "readFile": readFile,
    "createFile": createFile,
    "editFile": editFile,
    "createDir": createDir,
    "deleteDir": deleteDir,
    "deleteFile": deleteFile,
    "renamePath": renamePath,
}

TOOL_NAMES = tuple(HANDLERS)


def run_tool(call):
    name = (call.get("function") or {}).get("name")
    handler = HANDLERS.get(name)
    if handler is None:
        return "未知工具"
    try:
        args = json.loads((call.get("function") or {}).get("arguments") or "{}")
    except json.JSONDecodeError as exc:
        return f"参数不是合法 JSON: {exc}"
    try:
        return str(handler(**args))
    except TypeError as exc:
        return f"参数错误: {exc}"
    except OSError as exc:
        return f"操作失败: {exc}"
