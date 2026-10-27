"""调用本机已安装的命令行程序。只返回字符串，失败也不向外抛业务异常。

本阶段用于受控教学实验，不设命令白名单、黑名单或人工审批。
subprocess 不以提权方式启动，命令使用运行 TQAgent 的当前用户权限执行。
"""

import subprocess

TIMEOUT_SECONDS = 30
MAX_OUTPUT = 4000

SCHEMA = {
    "type": "function",
    "function": {
        "name": "RunCli",
        "description": "调用本机已安装的命令行程序，以运行 TQAgent 的当前用户权限执行。传入 command 和参数列表 args。最多等待 30 秒；标准输出和标准错误过长时会截断并标明。返回退出码、标准输出和标准错误。",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "要执行的命令行程序名称或路径，例如 git、python、curl。不要把参数写在这里。",
                },
                "args": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "传给程序的参数列表。每一项是一个独立参数，不要把整条命令写进一项。",
                },
            },
            "required": ["command"],
        },
    },
}


def RunCli(command: str, args: list | None = None) -> str:
    try:
        return _run(command, args)
    except Exception as exc:
        return f"命令执行失败：{exc}"


def _run(command: str, args: list | None) -> str:
    name = str(command or "").strip()
    if not name:
        return "command 不能为空"
    if any(char in name for char in "\r\n\x00"):
        return "command 无效"
    arguments, error = _arguments(args)
    if error:
        return error
    argv = [name, *arguments]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        notice = f"命令执行超时，已在 {TIMEOUT_SECONDS} 秒后终止。"
        err = _decode(exc.stderr or b"")
        stderr = notice if not err else f"{notice}\n{err}"
        return _format("未结束", exc.stdout or b"", stderr.encode("utf-8"))
    except FileNotFoundError:
        return _format("未执行", b"", f"未找到命令行程序：{name}".encode("utf-8"))
    except OSError as exc:
        return _format("未执行", b"", f"命令执行失败：{exc}".encode("utf-8"))
    return _format(completed.returncode, completed.stdout or b"", completed.stderr or b"")


def _arguments(args: list | None) -> tuple[list[str], str]:
    if args is None:
        return [], ""
    if isinstance(args, str) or not isinstance(args, list):
        return [], "参数 args 必须是字符串列表"
    values: list[str] = []
    for item in args:
        if isinstance(item, str):
            if "\x00" in item:
                return [], "参数包含无效字符"
            values.append(item)
            continue
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return [], "参数 args 必须是字符串列表"
        values.append(str(item))
    return values, ""


def _format(code: int | str, stdout: bytes, stderr: bytes) -> str:
    return "\n".join([
        f"退出码：{code}",
        _section("标准输出", _decode(stdout)),
        _section("标准错误", _decode(stderr)),
    ])


def _section(title: str, text: str) -> str:
    if not text:
        return f"{title}：\n（无）"
    shown, note = _clip(text)
    label = f"{title}：{note}" if note else f"{title}："
    return f"{label}\n{shown}"


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace").strip()


def _clip(text: str) -> tuple[str, str]:
    if len(text) <= MAX_OUTPUT:
        return text, ""
    head = MAX_OUTPUT // 4 * 3
    tail = MAX_OUTPUT - head
    omitted = len(text) - head - tail
    note = f"（已截断，原长 {len(text)} 字符，省略中间 {omitted} 字符）"
    return f"{text[:head]}\n{note}\n{text[-tail:]}", note
