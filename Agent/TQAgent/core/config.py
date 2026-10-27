import configparser
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ConfigError(Exception):
    pass


DEFAULT_SEARCH_BASE = "https://qianfan.baidubce.com"


@dataclass(frozen=True)
class LlmSettings:
    api_key: str
    base_url: str
    model: str
    source: Path


@dataclass(frozen=True)
class SearchSettings:
    api_key: str
    base_url: str
    source: Path
    problem: str = ""


def _candidates() -> list[Path]:
    raw: list[Path] = []
    env = os.environ.get("TQAGENT_CONFIG")
    if env:
        raw.append(Path(env))
    raw.append(Path.cwd() / "config.ini")
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


def load_settings(path: Path | None = None) -> LlmSettings:
    if path is not None:
        return _read(Path(path))
    candidates = _candidates()
    for candidate in candidates:
        if candidate.is_file():
            return _read(candidate)
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
    return LlmSettings(
        api_key=section["api_key"].strip(),
        base_url=section["base_url"].strip(),
        model=section["model"].strip(),
        source=path,
    )


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
