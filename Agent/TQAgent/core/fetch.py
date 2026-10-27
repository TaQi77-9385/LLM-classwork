"""用本地 curl 获取网页，再用 trafilatura 抽出 Markdown。只返回字符串。"""

import subprocess
from urllib.parse import urlparse

MAX_BYTES = 2 * 1024 * 1024
MAX_CHARS = 8000
CURL_TIMEOUT = 30

SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetchWeb",
        "description": "获取指定网页的正文 Markdown 和原始 URL。保留标题、段落、列表、表格和链接，去掉导航、广告和页脚。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "以 http:// 或 https:// 开头的网页地址",
                },
            },
            "required": ["url"],
        },
    },
}

_CURL_ERRORS = {
    6: "网页访问失败：无法解析该网页的主机名。",
    7: "网页访问失败：无法连接到该页面。请检查地址或网络后重试。",
    28: "网络请求超时，没有在限定时间内打开该页面。请稍后重试。",
    35: "网页访问失败：建立安全连接失败。",
    47: "网页访问失败：重定向次数过多，已停止获取。",
    52: "网页访问失败：服务器没有返回内容。",
    56: "网页访问失败：连接在传输过程中中断。",
}


class HtmlParseError(Exception):
    """HTML 无法解析。由 fetchWeb 转成工具结果，不向调用方抛出。"""


def fetchWeb(url: str) -> str:
    try:
        return _fetch(url)
    except Exception as exc:
        return f"获取页面失败：{exc}"


def _fetch(url: str) -> str:
    address = str(url or "").strip()
    if not address:
        return "页面地址不能为空"
    parsed = urlparse(address)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return "URL 无效。请提供以 http:// 或 https:// 开头的网页地址。"
    try:
        completed = subprocess.run(
            [
                "curl",
                "-L",
                "-sS",
                "--max-time",
                str(CURL_TIMEOUT),
                "-A",
                "TQAgent/1.0",
                "-w",
                "\n%{http_code}",
                "--",
                address,
            ],
            capture_output=True,
            timeout=CURL_TIMEOUT + 5,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return "网络请求超时，没有在限定时间内打开该页面。请稍后重试。"
    except FileNotFoundError:
        return "未找到本地 curl 程序，无法获取网页。"
    except OSError as exc:
        return f"网页访问失败：{exc}"
    body, status = _split_status(completed.stdout or b"")
    if completed.returncode != 0 and status in (None, 0):
        return _curl_failure(completed)
    if status is not None and status >= 400:
        return f"网页访问失败（HTTP {status}）：{address}"
    if len(body) > MAX_BYTES:
        return "页面过大，已超过 2MB，无法提取正文。"
    if not body.strip():
        return "正文无法提取。"
    try:
        markdown = _extract_markdown(body, address)
    except HtmlParseError:
        return "HTML 解析失败，无法从该页面提取正文。"
    except ImportError:
        return "未安装 trafilatura。请先执行 pip install trafilatura 后重新启动。"
    if not markdown.strip():
        return "正文无法提取。"
    if len(markdown) > MAX_CHARS:
        markdown = markdown[:MAX_CHARS] + "…"
    return f"URL：{address}\n\n{markdown}"


def _extract_markdown(html: bytes, url: str) -> str:
    if b"<" not in html[:1000].lower():
        raise HtmlParseError("页面内容不是 HTML")
    import trafilatura
    try:
        text = trafilatura.extract(
            html,
            url=url,
            output_format="markdown",
            favor_precision=True,
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_formatting=True,
            include_links=True,
            deduplicate=True,
        )
    except Exception as exc:
        raise HtmlParseError("trafilatura 无法解析该 HTML") from exc
    return (text or "").strip()


def _split_status(raw: bytes) -> tuple[bytes, int | None]:
    head, sep, last = raw.rpartition(b"\n")
    token = (last if sep else raw).strip()
    if token.isdigit() and len(token) == 3:
        return (head if sep else b""), int(token)
    return raw, None


def _curl_failure(completed: subprocess.CompletedProcess) -> str:
    message = _CURL_ERRORS.get(completed.returncode)
    if message:
        return message
    detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip().splitlines()
    reason = detail[-1][:200] if detail else f"curl 退出码 {completed.returncode}"
    return f"网页访问失败：{reason}"
