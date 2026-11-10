"""读取 config.ini。缺失时在当前目录生成带 mock 值的完整配置。"""

from __future__ import annotations

import configparser
import os
import sys
from dataclasses import dataclass, fields, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigError(Exception):
    pass


DEFAULT_SEARCH_BASE = "https://qianfan.baidubce.com"

MOCK_CONFIG_TEXT = """[llm]
api_key = sk-your-key
base_url = https://api.deepseek.com/v1
model = deepseek-chat
; 模型上下文窗口（Token）。省略或留空时按 [agent] context_window_default 处理
context_window = 128000
; 请求超时（秒）
connect_timeout = 10
read_timeout = 120

[search]
baidu_qianfan_api_key = bce-v3/your-qianfan-key
base_url = https://qianfan.baidubce.com

[agent]
max_model_calls = 8
context_window_default = 128000
context_warn_ratio = 0.80
target_summary_tokens = 5000

[limits]
cli_timeout = 30
cli_max_output = 4000
fetch_timeout = 30
fetch_max_bytes = 2097152
fetch_max_chars = 8000
file_max_text_bytes = 1048576
mcp_connect_timeout = 30
mcp_call_timeout = 60
mcp_max_output = 8000
recall_max_chars = 6000
recall_max_hits = 12
ui_result_limit = 2000
search_connect_timeout = 10
search_read_timeout = 30
search_top_k = 10
search_snippet_limit = 500
search_query_unit_limit = 72
"""


@dataclass(frozen=True)
class LlmSettings:
    api_key: str
    base_url: str
    model: str
    source: Path
    context_window: int | None = None
    connect_timeout: int = 10
    read_timeout: int = 120


@dataclass(frozen=True)
class SearchSettings:
    api_key: str
    base_url: str
    source: Path
    problem: str = ""


@dataclass(frozen=True)
class RuntimeLimits:
    max_model_calls: int = 8
    context_window_default: int = 128_000
    context_warn_ratio: float = 0.80
    target_summary_tokens: int = 5_000
    cli_timeout: int = 30
    cli_max_output: int = 4_000
    fetch_timeout: int = 30
    fetch_max_bytes: int = 2 * 1024 * 1024
    fetch_max_chars: int = 8_000
    file_max_text_bytes: int = 1024 * 1024
    mcp_connect_timeout: int = 30
    mcp_call_timeout: int = 60
    mcp_max_output: int = 8_000
    recall_max_chars: int = 6_000
    recall_max_hits: int = 12
    ui_result_limit: int = 2_000
    search_connect_timeout: int = 10
    search_read_timeout: int = 30
    search_top_k: int = 10
    search_snippet_limit: int = 500
    search_query_unit_limit: int = 72


_limits = RuntimeLimits()


def get_limits() -> RuntimeLimits:
    return _limits


def set_limits(limits: RuntimeLimits) -> RuntimeLimits:
    global _limits
    _limits = limits
    _apply_module_limits(limits)
    return _limits


def _apply_module_limits(limits: RuntimeLimits) -> None:
    """把运行时参数同步到各模块模块级变量，避免到处传参。"""
    try:
        import core.cli as cli

        cli.TIMEOUT_SECONDS = limits.cli_timeout
        cli.MAX_OUTPUT = limits.cli_max_output
    except Exception:
        pass
    try:
        import core.fetch as fetch

        fetch.CURL_TIMEOUT = limits.fetch_timeout
        fetch.MAX_BYTES = limits.fetch_max_bytes
        fetch.MAX_CHARS = limits.fetch_max_chars
    except Exception:
        pass
    try:
        import core.files as files

        files.MAX_TEXT_BYTES = limits.file_max_text_bytes
    except Exception:
        pass
    try:
        import core.mcp_client as mcp

        mcp.CONNECT_TIMEOUT = limits.mcp_connect_timeout
        mcp.CALL_TIMEOUT = limits.mcp_call_timeout
        mcp.MAX_OUTPUT = limits.mcp_max_output
    except Exception:
        pass
    try:
        import core.context as context

        context.DEFAULT_CONTEXT_WINDOW = limits.context_window_default
        context.WARN_RATIO = limits.context_warn_ratio
        context.TARGET_SUMMARY_TOKENS = limits.target_summary_tokens
        context.MAX_RECALL_CHARS = limits.recall_max_chars
        context.MAX_RECALL_HITS = limits.recall_max_hits
    except Exception:
        pass
    try:
        import core.search as search

        search.TOP_K = limits.search_top_k
        search.SNIPPET_LIMIT = limits.search_snippet_limit
        search.QUERY_UNIT_LIMIT = limits.search_query_unit_limit
        search.CONNECT_TIMEOUT = limits.search_connect_timeout
        search.READ_TIMEOUT = limits.search_read_timeout
    except Exception:
        pass
    try:
        import core.session as session

        session.MAX_MODEL_CALLS = limits.max_model_calls
    except Exception:
        pass


def _candidates() -> list[Path]:
    raw: list[Path] = []
    env = os.environ.get("TQAGENT_CONFIG")
    if env:
        raw.append(Path(env))
    raw.append(Path.cwd() / "config.ini")
    if getattr(sys, "frozen", False):
        raw.append(Path(sys.executable).resolve().parent / "config.ini")
    raw.append(ROOT / "config.ini")
    unique: list[Path] = []
    seen: set[Path] = set()
    for item in raw:
        key = item.resolve() if item.parent.exists() else item
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def default_config_path() -> Path:
    return Path.cwd() / "config.ini"


def write_mock_config(path: Path | None = None) -> Path:
    target = default_config_path() if path is None else Path(path)
    target.write_text(MOCK_CONFIG_TEXT, encoding="utf-8")
    return target


def ensure_config(path: Path | None = None) -> tuple[LlmSettings, bool]:
    """加载配置。当前目录没有 config.ini 时自动生成 mock 配置。

    返回 (settings, created)。created=True 表示刚写入了新文件，需要提示用户修改后重启。
    """
    if path is not None:
        explicit = Path(path)
        if not explicit.is_file():
            raise ConfigError(f"配置文件不存在: {explicit}")
        settings = _read(explicit)
        set_limits(_read_limits(explicit))
        return settings, False

    cwd_config = default_config_path()
    created = False
    if not cwd_config.is_file():
        write_mock_config(cwd_config)
        created = True
        settings = _read(cwd_config)
        set_limits(_read_limits(cwd_config))
        return settings, created

    settings = _read(cwd_config)
    set_limits(_read_limits(cwd_config))
    return settings, False


def load_settings(path: Path | None = None) -> LlmSettings:
    if path is not None:
        settings = _read(Path(path))
        set_limits(_read_limits(Path(path)))
        return settings
    candidates = _candidates()
    for candidate in candidates:
        if candidate.is_file():
            settings = _read(candidate)
            set_limits(_read_limits(candidate))
            return settings
    listed = "\n".join(str(item) for item in candidates)
    raise ConfigError(
        "未找到 config.ini。请复制 config.example.ini 为 config.ini，或使用 --config 指定路径。\n"
        + listed
    )


def _read(path: Path) -> LlmSettings:
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    if "llm" not in parser:
        raise ConfigError(f"{path} 缺少 [llm] 段")
    section = parser["llm"]
    missing = [key for key in ("api_key", "base_url", "model") if not section.get(key, "").strip()]
    if missing:
        raise ConfigError(f"{path} 缺少配置项: {', '.join(missing)}")
    window_raw = section.get("context_window", "").strip()
    context_window = None
    if window_raw:
        try:
            context_window = int(window_raw)
        except ValueError as exc:
            raise ConfigError(f"{path} 的 context_window 必须是整数") from exc
        if context_window <= 0:
            raise ConfigError(f"{path} 的 context_window 必须大于 0")
    connect_timeout = _int(section, "connect_timeout", 10, path)
    read_timeout = _int(section, "read_timeout", 120, path)
    return LlmSettings(
        api_key=section["api_key"].strip(),
        base_url=section["base_url"].strip(),
        model=section["model"].strip(),
        source=path,
        context_window=context_window,
        connect_timeout=connect_timeout,
        read_timeout=read_timeout,
    )


def _read_limits(path: Path) -> RuntimeLimits:
    parser = configparser.ConfigParser()
    parser.read(path, encoding="utf-8")
    base = RuntimeLimits()
    values: dict = {}
    mapping = {
        "agent": {
            "max_model_calls": int,
            "context_window_default": int,
            "context_warn_ratio": float,
            "target_summary_tokens": int,
        },
        "limits": {
            "cli_timeout": int,
            "cli_max_output": int,
            "fetch_timeout": int,
            "fetch_max_bytes": int,
            "fetch_max_chars": int,
            "file_max_text_bytes": int,
            "mcp_connect_timeout": int,
            "mcp_call_timeout": int,
            "mcp_max_output": int,
            "recall_max_chars": int,
            "recall_max_hits": int,
            "ui_result_limit": int,
            "search_connect_timeout": int,
            "search_read_timeout": int,
            "search_top_k": int,
            "search_snippet_limit": int,
            "search_query_unit_limit": int,
        },
    }
    known = {item.name for item in fields(RuntimeLimits)}
    for section_name, keys in mapping.items():
        if not parser.has_section(section_name):
            continue
        section = parser[section_name]
        for key, caster in keys.items():
            if key not in known:
                continue
            raw = section.get(key, "").strip()
            if not raw:
                continue
            try:
                values[key] = caster(raw)
            except ValueError as exc:
                raise ConfigError(f"{path} 的 [{section_name}] {key} 格式无效") from exc
    return replace(base, **values)


def _int(section, key: str, default: int, path: Path) -> int:
    raw = section.get(key, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{path} 的 {key} 必须是整数") from exc
    if value <= 0:
        raise ConfigError(f"{path} 的 {key} 必须大于 0")
    return value


def load_search_settings(path: Path | None = None) -> SearchSettings:
    """读取 [search]。缺少密钥时不中断启动，由 searchWeb 把原因返回给模型。"""
    if path is None:
        for candidate in _candidates():
            if candidate.is_file():
                path = candidate
                break
    source = Path(path or "")
    if path is None or not source.is_file():
        return SearchSettings(
            api_key="",
            base_url=DEFAULT_SEARCH_BASE,
            source=source,
            problem="未找到 config.ini，无法读取百度千帆搜索配置。",
        )
    parser = configparser.ConfigParser()
    parser.read(source, encoding="utf-8")
    if not parser.has_section("search"):
        return SearchSettings(
            api_key="",
            base_url=DEFAULT_SEARCH_BASE,
            source=source,
            problem="config.ini 缺少 [search] 段，请添加 baidu_qianfan_api_key。",
        )
    section = parser["search"]
    api_key = str(section.get("baidu_qianfan_api_key", "")).strip()
    base_url = str(section.get("base_url", "")).strip() or DEFAULT_SEARCH_BASE
    problem = ""
    if not api_key:
        problem = (
            "config.ini 的 [search] baidu_qianfan_api_key 为空。"
            "请打开 https://console.bce.baidu.com/iam/#/iam/apikey/list 复制 API Key 后填入，并重新启动。"
        )
    return SearchSettings(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        source=source,
        problem=problem,
    )
