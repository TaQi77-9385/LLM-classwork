"""上下文压缩、归档与历史检索。

设计尽量简单：本地 JSONL 归档 + 关键词全文检索，不依赖向量库。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

DEFAULT_CONTEXT_WINDOW = 128_000
WARN_RATIO = 0.80
TARGET_SUMMARY_TOKENS = 5_000
MAX_RECALL_CHARS = 6_000
MAX_RECALL_HITS = 12

COMPACT_PROMPT = """你是一名 AI Agent 的上下文管理助手。当前会话即将达到上下文窗口限制，需要将历史对话压缩为一份高质量的工作摘要，以便 Agent 在新的上下文中继续执行任务。

请认真阅读提供的历史上下文，提取对后续工作仍然有价值的信息，并生成一份约 5,000 Tokens 的 Markdown 摘要。

摘要应重点保留以下内容：

1. **用户目标与要求**：用户希望完成什么任务？有哪些必须遵守的约束、偏好和验收条件？
2. **已经完成的工作**：哪些任务已经完成？实现了哪些功能？获得了哪些已经验证的结果？
3. **当前工作状态**：Agent 正在处理什么任务？当前进展到哪一步？是否存在尚未解决的问题？
4. **关键技术信息**：保留重要的文件路径、函数名称、接口定义、配置项、执行命令、错误信息和必要的代码片段。
5. **关键决策与原因**：记录用户和 Agent 已经确定的技术方案，以及重要的设计取舍。
6. **下一步任务**：明确接下来应该执行的操作，以及需要继续验证的事项。

请遵守以下要求：

- 优先保留对后续任务有直接影响的信息，删除寒暄、重复讨论、过时的尝试和无关内容。
- 对已经验证的事实与尚未验证的推测作出明确区分。
- 不要将计划完成的任务描述为已经完成。
- 重要的文件路径、函数名称、配置名称和命令应保留准确原文。
- 对失败的尝试，只保留有助于避免重复错误的信息。
- 如果信息存在冲突，应优先保留用户最新确认的要求，并简要说明仍然存在的冲突。
- 如果某些历史细节无法在摘要中完整保留，可简要说明它们已保存到原始上下文归档中，后续可以通过 `recallContext` 工具重新检索。
- 使用清晰的 Markdown 结构输出摘要，不要附加解释或无关内容。

**最终目标：使一个从未阅读过原始对话的 Agent，仅凭这份摘要，就能够理解当前任务、继承关键决策，并继续开展工作。**"""

SCHEMA = {
    "type": "function",
    "function": {
        "name": "recallContext",
        "description": (
            "检索此前 /compact 归档的原始上下文。"
            "当压缩摘要缺少历史细节时调用。"
            "text 为关键词；可选 id 指定归档编号（如 20261022-153000），省略则搜索全部归档。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "要查找的关键词或文本内容",
                },
                "id": {
                    "type": "string",
                    "description": "可选。上下文归档编号，例如 20261022-153000",
                },
            },
            "required": ["text"],
        },
    },
}

_project_root: Path | None = None


def bind_context_root(root: Path | None) -> None:
    """绑定项目根目录，供 recallContext 与归档使用。"""
    global _project_root
    _project_root = None if root is None else Path(root)


def context_dir(root: Path | None = None) -> Path:
    base = Path(root) if root is not None else (_project_root or Path.cwd())
    return base / ".agents" / "context"


def estimate_tokens(text: str) -> int:
    """中英混合粗估：汉字约 1 token/字，其余约 4 字符/token。"""
    if not text:
        return 0
    cjk = 0
    for char in text:
        code = ord(char)
        if (
            0x4E00 <= code <= 0x9FFF
            or 0x3400 <= code <= 0x4DBF
            or 0xF900 <= code <= 0xFAFF
            or 0x3000 <= code <= 0x303F
            or 0xFF00 <= code <= 0xFFEF
        ):
            cjk += 1
    other = len(text) - cjk
    return max(0, cjk + (other + 3) // 4)


def _message_text(message: dict) -> str:
    parts: list[str] = []
    content = message.get("content")
    if content:
        parts.append(str(content))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        parts.append(str(function.get("name") or ""))
        parts.append(str(function.get("arguments") or ""))
    return "\n".join(parts)


def estimate_messages_tokens(messages: list) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_tokens(_message_text(message))
        total += estimate_tokens(str(message.get("role") or ""))
        total += estimate_tokens(str(message.get("tool_call_id") or ""))
    return total


def estimate_session_tokens(messages: list, system_prompt: str = "", tool_schemas: list | None = None) -> int:
    total = estimate_tokens(system_prompt or "")
    total += estimate_messages_tokens(messages)
    if tool_schemas:
        total += estimate_tokens(json.dumps(tool_schemas, ensure_ascii=False))
    return total


def make_archive_id(when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return stamp


def save_archive(messages: list, root: Path | None = None, when: datetime | None = None) -> str:
    """把原始消息完整写入 .agents/context/<id>.jsonl，成功后返回归档编号。"""
    folder = context_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = when or datetime.now()
    archive_id = make_archive_id(stamp)
    path = folder / f"{archive_id}.jsonl"
    suffix = 1
    while path.exists():
        archive_id = f"{make_archive_id(stamp)}-{suffix}"
        path = folder / f"{archive_id}.jsonl"
        suffix += 1

    lines = []
    for index, message in enumerate(messages):
        record = {
            "index": index,
            "role": message.get("role"),
            "content": message.get("content"),
        }
        if "tool_calls" in message:
            record["tool_calls"] = message.get("tool_calls")
        if "tool_call_id" in message:
            record["tool_call_id"] = message.get("tool_call_id")
        if "name" in message:
            record["name"] = message.get("name")
        lines.append(json.dumps(record, ensure_ascii=False))

    tmp = path.with_suffix(".jsonl.tmp")
    try:
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    if not path.is_file():
        raise OSError(f"归档文件未成功写入: {path}")
    return archive_id


def archive_note(archive_id: str, when: datetime | None = None) -> str:
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"此前上下文已于 {stamp} 完成压缩，原始上下文保存在编号为 {archive_id} 的归档文件中。"
        "如需查找历史信息，请使用 recallContext 工具。"
    )


def format_history_for_compact(messages: list) -> str:
    """把消息整理成便于模型阅读的纯文本。"""
    blocks: list[str] = []
    for index, message in enumerate(messages):
        role = message.get("role") or "unknown"
        header = f"### [{index}] {role}"
        body_parts: list[str] = []
        content = message.get("content")
        if content:
            body_parts.append(str(content))
        tool_calls = message.get("tool_calls") or []
        for call in tool_calls:
            function = call.get("function") or {}
            body_parts.append(
                "tool_call "
                f"id={call.get('id') or ''} "
                f"name={function.get('name') or ''}\n"
                f"arguments={function.get('arguments') or ''}"
            )
        if message.get("tool_call_id"):
            body_parts.append(f"tool_call_id={message.get('tool_call_id')}")
        blocks.append(header + "\n" + ("\n".join(body_parts) if body_parts else "（空）"))
    return "\n\n".join(blocks)


def build_compact_messages(history_text: str) -> list[dict]:
    return [
        {"role": "system", "content": COMPACT_PROMPT},
        {
            "role": "user",
            "content": (
                "以下是需要压缩的历史上下文。请按系统说明输出 Markdown 摘要，"
                f"目标长度约 {TARGET_SUMMARY_TOKENS} Tokens。\n\n"
                f"{history_text}"
            ),
        },
    ]


def build_summary_messages(summary: str, note: str) -> list[dict]:
    text = summary.strip()
    if note not in text:
        text = f"{text}\n\n{note}"
    return [
        {
            "role": "user",
            "content": (
                "[上下文压缩摘要]\n"
                "以下摘要替换了此前的完整对话历史。请据此继续完成任务；"
                "若缺少细节，请调用 recallContext 检索原始归档。\n\n"
                f"{text}"
            ),
        },
        {
            "role": "assistant",
            "content": (
                "已接收压缩摘要，将据此继续工作。"
                "如需历史细节，我会使用 recallContext 检索归档。"
            ),
        },
    ]


def list_archive_files(root: Path | None = None, archive_id: str | None = None) -> list[Path]:
    folder = context_dir(root)
    if not folder.is_dir():
        return []
    if archive_id:
        path = folder / f"{archive_id}.jsonl"
        return [path] if path.is_file() else []
    return sorted(folder.glob("*.jsonl"), key=lambda item: item.name, reverse=True)


def _keywords(text: str) -> list[str]:
    parts = [item.strip().lower() for item in re.split(r"\s+", text.strip()) if item.strip()]
    if not parts and text.strip():
        parts = [text.strip().lower()]
    return parts


def _record_blob(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False)


def recallContext(text: str = "", id: str | None = None) -> str:
    """按关键词检索本地上下文归档。"""
    query = str(text or "").strip()
    if not query:
        return "参数错误: text 不能为空"
    archive_id = None if id is None else str(id).strip()
    if id is not None and not archive_id:
        return "参数错误: id 不能为空字符串"

    files = list_archive_files(_project_root, archive_id)
    if archive_id and not files:
        return f"未找到归档: {archive_id}"
    if not files:
        return "还没有上下文归档。请先执行 /compact。"

    keys = _keywords(query)
    hits: list[str] = []
    total_chars = 0
    for path in files:
        current_id = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            hits.append(f"[{current_id}] 读取失败: {exc}")
            continue
        for line_no, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                blob = line
                record = {"raw": line}
            else:
                blob = _record_blob(record)
            lowered = blob.lower()
            if not all(key in lowered for key in keys):
                continue
            index = record.get("index", line_no - 1)
            role = record.get("role", "?")
            snippet = record.get("content")
            if snippet is None:
                snippet = blob
            snippet = str(snippet)
            if len(snippet) > 800:
                snippet = snippet[:800] + "…"
            block = (
                f"### 归档 {current_id} · 第 {index} 条 · role={role}\n"
                f"{snippet}"
            )
            if total_chars + len(block) > MAX_RECALL_CHARS and hits:
                hits.append("…结果已截断，请缩小关键词或指定 id 再查。")
                return "\n\n".join(hits)
            hits.append(block)
            total_chars += len(block)
            if len(hits) >= MAX_RECALL_HITS:
                hits.append("…已达到返回条数上限，请缩小关键词或指定 id 再查。")
                return "\n\n".join(hits)

    if not hits:
        scope = f"归档 {archive_id}" if archive_id else "全部归档"
        return f"在{scope}中未找到与「{query}」匹配的内容。"
    return "\n\n".join(hits)
