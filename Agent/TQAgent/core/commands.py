"""斜杠命令定义，供 TUI 补全与帮助共用。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    name: str
    summary: str
    detail: str = ""


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand("/help", "打开帮助", "查看命令与快捷键说明"),
    SlashCommand("/new", "新建会话", "自动保存当前会话后清空历史并开始新会话"),
    SlashCommand("/save", "保存会话", "把当前对话写入 .agents/sessions/"),
    SlashCommand("/resume", "恢复会话", "列出历史会话，上下键选择，回车恢复，Esc 取消"),
    SlashCommand("/compact", "压缩上下文", "归档原始对话并用摘要替换历史"),
    SlashCommand("/clear", "清屏", "清空界面显示，保留模型上下文"),
    SlashCommand("/tools", "列出工具", "显示当前已注册的 Function Calling 工具"),
    SlashCommand("/mcp", "MCP 状态", "显示已连接 MCP 服务器及其工具"),
    SlashCommand("/model", "模型信息", "显示当前模型与接口地址"),
    SlashCommand("/cwd", "工作目录", "显示当前工作目录"),
    SlashCommand("/config", "配置路径", "显示正在使用的 config.ini 路径"),
    SlashCommand("/status", "会话状态", "显示轮次、消息数、上下文占用等"),
    SlashCommand("/quit", "退出程序", "退出 TQAgent"),
)


def match_commands(prefix: str) -> list[SlashCommand]:
    text = (prefix or "").strip().lower()
    if not text.startswith("/"):
        return []
    token = text.split()[0]
    return [item for item in COMMANDS if item.name.startswith(token)]


def help_markdown() -> str:
    # Textual Markdown 对 GFM 表格支持不稳定，改用列表，避免只显示一行且难以滚动阅读
    command_lines = "\n".join(f"- `{item.name}` — {item.summary}" for item in COMMANDS)
    return f"""# TQAgent 帮助

按 **Esc / Enter / q** 可关闭本帮助。

## 使用方式

- 在底部输入框输入问题，**Enter** 发送。
- 输入 `/` 会弹出命令补全面板，可用上下键选择、Tab 补全、Enter 执行。
- 模型需要查看或修改文件时会自动调用工具，工具卡片可点击展开。
- **Esc**：关闭弹窗；生成中则中断；否则关闭命令补全面板。
- **Ctrl+Q** 或输入 `/quit`：退出程序。

## 命令

{command_lines}

上下文占用达到模型窗口的阈值时会提示压缩；达到后暂停普通提问，需先执行 `/compact`。

## 快捷键

- **Enter** — 发送 / 确认
- **Esc** — 关闭弹窗 / 中断生成 / 关闭补全面板
- **Tab** — 补全斜杠命令
- **Ctrl+B** — 显示或隐藏侧栏
- **Ctrl+N** — 新会话
- **Ctrl+S** — 保存会话
- **Ctrl+Q** — 退出
- **F1** — 帮助
""".strip()
