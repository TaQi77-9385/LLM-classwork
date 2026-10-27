"""百度千帆网页搜索。只返回字符串，失败也不向外抛业务异常。"""

import requests

from core.config import SearchSettings

SEARCH_URL_PATH = "/v2/ai_search/web_search"
QUERY_UNIT_LIMIT = 72
TOP_K = 10
SNIPPET_LIMIT = 500

SCHEMA = {
    "type": "function",
    "function": {
        "name": "searchWeb",
        "description": "搜索互联网上的最新信息。传入关键词或问题，返回每条结果的摘要、来源标题和 URL。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词或问题。中文约占两个字符，总长超过 72 时只检索前面部分。",
                },
            },
            "required": ["query"],
        },
    },
}

_settings: SearchSettings | None = None


def configure(settings: SearchSettings) -> None:
    global _settings
    _settings = settings


def searchWeb(query: str) -> str:
    try:
        return _search(query)
    except Exception as exc:
        return _redact(f"搜索失败：{exc}", getattr(_settings, "api_key", ""))


def _search(query: str) -> str:
    text = str(query or "").strip()
    if not text:
        return "搜索关键词不能为空"
    config_error = _config_error(_settings)
    if config_error:
        return config_error
    settings = _settings
    assert settings is not None
    clipped, truncated = _clip_query(text)
    try:
        response = requests.post(
            settings.base_url.rstrip("/") + SEARCH_URL_PATH,
            headers={
                "Authorization": f"Bearer {settings.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "messages": [{"role": "user", "content": clipped}],
                "search_source": "baidu_search_v2",
                "edition": "standard",
                "resource_type_filter": [{"type": "web", "top_k": TOP_K}],
            },
            timeout=(10, 30),
        )
    except requests.Timeout:
        return "网络请求超时，百度千帆搜索没有在限定时间内响应。请稍后重试。"
    except requests.ConnectionError:
        return "网络连接失败，无法访问百度千帆搜索服务。请检查网络后重试。"
    except requests.RequestException as exc:
        reason = _redact(str(exc).splitlines()[0][:200], settings.api_key)
        return f"网络请求失败：{reason}"
    payload = _read_payload(response)
    if payload is None:
        return f"百度千帆返回了无法解析的内容（HTTP {response.status_code}）。"
    if _is_api_error(response, payload):
        return _redact(_api_error_text(response, payload), settings.api_key)
    body = format_references(payload.get("references") or [])
    if truncated:
        return f"查询已截断为：{clipped}\n{body}"
    return body


def _config_error(settings: SearchSettings | None) -> str:
    if settings is None:
        return (
            "未加载搜索配置。"
            "请在 config.ini 的 [search] 中填写 baidu_qianfan_api_key 后重新启动。"
        )
    if settings.problem:
        return settings.problem
    if not settings.api_key:
        return (
            "未配置百度千帆搜索 API Key。"
            "请在 config.ini 的 [search] baidu_qianfan_api_key 中填写后重新启动。"
        )
    return ""


def _read_payload(response) -> dict | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _is_api_error(response, payload: dict) -> bool:
    if response.status_code >= 400:
        return True
    if payload.get("references"):
        return False
    return bool(payload.get("code") or payload.get("error") or payload.get("message"))


def _api_error_text(response, payload: dict) -> str:
    code = payload.get("code") or ""
    message = payload.get("message") or ""
    error = payload.get("error")
    if isinstance(error, dict):
        code = code or error.get("code") or ""
        message = message or error.get("message") or ""
    elif error and not message:
        message = error
    request_id = payload.get("request_id") or payload.get("requestId") or ""
    message = str(message).strip()
    if not message:
        message = (response.text or "").strip().replace("\n", " ")[:200]
    head = f"百度千帆搜索失败（HTTP {response.status_code}"
    if code:
        head += f"，错误码 {code}"
    head += "）"
    text = f"{head}：{message}" if message else f"{head}。"
    if request_id:
        text += f" 请求 ID：{request_id}"
    return text


def _redact(text: str, secret: str) -> str:
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def format_references(references: list) -> str:
    if not references:
        return "没有找到相关结果"
    lines = [f"共 {len(references)} 条结果"]
    for index, item in enumerate(references, 1):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("web_anchor") or "（无标题）").strip()
        url = str(item.get("url") or "").strip() or "（无 URL）"
        summary = str(item.get("snippet") or item.get("content") or "").strip()
        summary = summary.replace("\x04", "").replace("\x05", "")
        if len(summary) > SNIPPET_LIMIT:
            summary = summary[:SNIPPET_LIMIT] + "…"
        lines.append("")
        lines.append(f"{index}. {title}")
        lines.append(f"摘要：{summary or '（无摘要）'}")
        lines.append(f"URL：{url}")
    if len(lines) == 1:
        return "没有找到相关结果"
    return "\n".join(lines)


def _clip_query(text: str) -> tuple[str, bool]:
    used = 0
    kept: list[str] = []
    for char in text:
        width = 2 if ord(char) > 127 else 1
        if used + width > QUERY_UNIT_LIMIT:
            return "".join(kept), True
        kept.append(char)
        used += width
    return text, False
