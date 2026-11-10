# TQAgent · 终端本地 Agent

TQAgent 是 `Agent/Practice02` 的分层重构版：把原来单文件里的配置、工具声明、工具实现、
工具循环、会话状态和界面拆成 `core/` 与 `tui/` 两层，在保留「模型返回 `tool_calls` →
本地执行 → 结果回填 → 再次请求」主循环的基础上，增加了技能（Skill）、MCP 客户端、
`AGENTS.md` 长期规则、会话持久化与上下文压缩。

界面为 Textual 终端应用（TUI），模型通过任意 OpenAI 兼容的 `/chat/completions`
流式接口调用。

---

## 目录结构

```
Agent/TQAgent/
├── main.py                 # 启动入口：读配置 → 建会话 → 加载界面
├── config.example.ini      # 配置模板（真实 config.ini 不入库）
├── .mcp.example.json       # MCP 配置模板（真实 .mcp.json 不入库）
├── requirements.txt
├── core/
│   ├── config.py           # config.ini 解析、默认值、运行时限额
│   ├── llm.py              # 流式请求与 SSE 增量解析（文本 + tool_calls）
│   ├── session.py          # 单轮工具循环、AGENTS.md 注入、压缩入口
│   ├── registry.py         # 本地工具表 + MCP 工具合并与分发
│   ├── files.py            # 10 个文件/目录工具
│   ├── cli.py              # RunCli：调用本机命令行程序
│   ├── fetch.py            # fetchWeb：curl 抓取 + trafilatura 转 Markdown
│   ├── search.py           # searchWeb：百度千帆 AI 搜索
│   ├── skills.py           # Skill 发现、注入、加载与创建
│   ├── mcp_client.py       # MCP 客户端：stdio / Streamable HTTP
│   ├── context.py          # 上下文估算、归档、压缩摘要、历史检索
│   ├── store.py            # 会话保存 / 列出 / 恢复
│   ├── commands.py         # 斜杠命令与帮助文本
│   └── events.py           # 界面消费的事件类型
├── tui/
│   ├── app.py              # Textual 主界面、命令、弹窗、后台任务
│   └── app.tcss            # 界面样式
├── tests/                  # unittest 用例（核心逻辑 / TUI / MCP）
└── .agents/
    └── skills/             # 技能目录（SKILL.md + 附加文件）
```

---

## 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上（代码使用 `X | None`、`tuple[str, ...]` 等语法） |
| 第三方库 | `requests`、`textual`、`trafilatura`、`mcp`（版本见 `requirements.txt`） |
| 系统命令 | `curl` 需在 `PATH` 中（`fetchWeb` 使用）；终端需支持 UTF-8 |

`fetchWeb` 在 Windows 10/11 自带的 `curl.exe` 上可直接工作；若提示找不到 curl，请安装后重试。

---

## 安装与启动

```bash
cd Agent/TQAgent

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate

pip install -r requirements.txt
python main.py
```

首次启动时，如果当前目录没有 `config.ini`，程序会按模板生成一份带占位值的配置并提示补全；
填入真实的 `api_key` 后重新启动即可。

```bash
# 指定配置文件
python main.py --config D:\path\to\config.ini
```

`--ui` 预留了 `tui / web / desktop / mobile` 四个界面入口，目前只实现了 `tui`，
选择其他值会提示「还没有实现」。

> 注意：TQAgent 把**启动时的工作目录**视为项目根目录。`AGENTS.md`、`.agents/skills/`、
> `.agents/sessions/`、`.agents/context/` 和 `.mcp.json` 都从这个目录读取。

---

## 配置

配置查找顺序：

1. 环境变量 `TQAGENT_CONFIG` 指向的文件；
2. 当前工作目录的 `config.ini`；
3. 打包运行时，可执行文件同目录的 `config.ini`；
4. 源码目录（`Agent/TQAgent`）的 `config.ini`。

### `[llm]`

| 键 | 说明 |
| --- | --- |
| `api_key` | 模型服务密钥 |
| `base_url` | OpenAI 兼容接口根地址，例如 `https://api.deepseek.com/v1` |
| `model` | 模型名 |
| `context_window` | 模型上下文窗口（Token），留空时用 `[agent] context_window_default` |
| `connect_timeout` / `read_timeout` | 连接 / 读取超时（秒） |

### `[search]`

| 键 | 说明 |
| --- | --- |
| `baidu_qianfan_api_key` | 百度千帆 AI 搜索 API Key；留空时 `searchWeb` 会把缺失原因返回给模型，不影响其他工具 |
| `base_url` | 默认 `https://qianfan.baidubce.com` |

### `[agent]`

| 键 | 说明 |
| --- | --- |
| `max_model_calls` | 单轮用户请求内最多请求模型多少次，默认 8 |
| `context_window_default` | 未配置 `context_window` 时使用的窗口大小，默认 128000 |
| `context_warn_ratio` | 上下文告警比例，默认 0.80 |
| `target_summary_tokens` | 压缩摘要的目标长度，默认 5000 |

### `[limits]`

工具与上下文的硬性限制，完整键见 `config.example.ini`，包括：
`cli_timeout`、`cli_max_output`、`fetch_timeout`、`fetch_max_bytes`、`fetch_max_chars`、
`file_max_text_bytes`、`mcp_connect_timeout`、`mcp_call_timeout`、`mcp_max_output`、
`recall_max_chars`、`recall_max_hits`、`ui_result_limit`、`search_*` 等。

`config.ini` 含密钥，已在 `.gitignore` 中排除；请以 `config.example.ini` 为模板自行创建。

---

## 工具

工具声明集中在 `core/files.py`、`core/search.py`、`core/fetch.py`、`core/cli.py`、
`core/skills.py`、`core/context.py`，由 `core/registry.py` 合并成一份工具表发给模型。
本地共 16 个工具，MCP 工具在连接成功后动态追加。

### 文件与目录（10 个）

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `getCwd` | 无 | 返回进程当前工作目录 |
| `listDir` | `path` | 列出目录项，目录在前、按名称排序 |
| `getFileType` | `path` | 返回扩展名、MIME、是否 UTF-8 纯文本 |
| `readFile` | `path` | 读取 UTF-8 纯文本；超过 1MB 或含 NUL 时拒绝 |
| `createFile` | `directory`, `filename`, `content?` | 在已存在目录中创建文件；已存在则拒绝；文件名不能带路径 |
| `editFile` | `path`, `content` | 覆盖写入纯文本文件 |
| `createDir` | `path` | 创建单层目录，父目录必须存在 |
| `deleteDir` | `path` | 递归删除目录及其内容 |
| `deleteFile` | `path` | 删除文件；传入目录时提示改用 `deleteDir` |
| `renamePath` | `path`, `new_path` | 文件/目录改名或移动；目标存在则拒绝 |

### 网络与命令

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `searchWeb` | `query` | 调百度千帆 `POST /v2/ai_search/web_search`，返回标题、摘要、URL；查询超长会截断 |
| `fetchWeb` | `url` | 优先用本机 `curl` 抓取，`trafilatura` 提取正文 Markdown；限 2MB / 8000 字符 |
| `RunCli` | `command`, `args` | `subprocess` 直接执行（`shell=False`），30 秒超时，输出超长时首尾截断并标注 |

### 技能与上下文

| 工具 | 参数 | 行为 |
| --- | --- | --- |
| `loadSkill` | `name`, `filename?` | 读取 `SKILL.md` 或技能目录内的附加文件 |
| `createSkill` | `name`, `skillContext` | 在 `.agents/skills/<name>/` 写入 `SKILL.md`，校验命名与 Frontmatter，已存在不覆盖 |
| `recallContext` | `text`, `id?` | 检索 `/compact` 归档的原始上下文，按关键词匹配并限制返回长度 |

工具层统一返回字符串：参数错误、文件不存在、网络失败、上游错误码都会转成可读文案回填给模型，
由模型决定是否重试或换一种做法。

---

## 技能（Skill）

技能目录固定为 `<项目根>/.agents/skills/<name>/`，每个技能至少包含一个 `SKILL.md`：

```markdown
---
name: leap-year
description: 判断公历年份是否为闰年。用户询问某年是不是闰年时使用。
---

# 闰年判断
...正文规则...
```

- 启动时只扫描一级子目录，读取 `SKILL.md` 开头 YAML Frontmatter 的 `name` 与 `description`，
  不读取正文；`name` 必须与目录名一致，只能由小写字母、数字和连字符组成，最长 64 字符，
  `description` 不超过 1024 字符。
- 已发现的技能只把「名称 + 描述」写进系统提示，模型需要时再调用 `loadSkill` 读取正文，
  实现渐进加载。
- `loadSkill` 读取附加文件时只允许技能目录内的路径（拒绝目录穿越），单文件仍受
  `file_max_text_bytes` 限制；只给文件名时会在技能目录内查找唯一同名文件。
- `createSkill` 会校验 Frontmatter 与正文，重复名称直接返回提示，不覆盖已有技能；
  `references/`、`scripts/` 等附加文件继续用 `createDir`、`createFile` 创建。

当前仓库自带示例技能：

| 技能 | 用途 |
| --- | --- |
| `leap-year` | 闰年判断规则 |
| `notice` | 公司内部通知的标题与落款规范 |
| `mini-encryption` | 轻量文本加密/解密流程 |
| `officecli` | 调用 officecli 处理 `.docx` / `.xlsx` / `.pptx` |

> `officecli` 依赖的技能目录内可执行文件 `officecli-win-x64.exe` 体积约 33MB，未纳入仓库；
> 如需使用该技能，请把对应的 `officecli-win-x64.exe` 放回 `.agents/skills/officecli/`。
> 其余技能只依赖说明文件，可直接使用。

---

## AGENTS.md 长期规则

- 启动时读取项目根目录的 `AGENTS.md`，以 `[AGENTS.md]` 开头的用户消息写入会话历史，
  并在系统提示之外作为长期约定生效；缺失该文件时静默跳过，不影响启动。
- 只注入一次：后续轮次复用消息历史；`/new` 新建会话时会重新注入。
- 会话恢复（`/resume`）会保留注入标记，避免重复注入，也不会丢失原有规则。
- 当前实现使用的文件名是 `AGENTS.md`（不接受其他别名），规则内容不做截断。

---

## MCP 客户端

配置文件为项目根目录的 `.mcp.json`，格式与常见 MCP 客户端一致：

```json
{
  "mcpServers": {
    "local": {
      "type": "stdio",
      "command": "python",
      "args": ["server.py"],
      "env": {"API_KEY": "${API_KEY}"}
    },
    "remote": {
      "type": "streamable_http",
      "url": "https://example.com/mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN}"}
    }
  }
}
```

- 传输方式：`stdio`（子进程管道）与 Streamable HTTP（`type` 可写
  `http`、`streamable-http`、`streamable_http`；不写 `type` 时按是否有 `url` 推断）。
- `args`、`env`、`headers` 中的 `${VAR}` 从环境变量展开；缺失变量会给出明确配置错误。
- 远程工具会转换成本地 Function Calling schema 后并入可调用集合；与本地工具或其他服务器
  重名时自动加 `服务器名__工具名` 前缀，来源可通过 `/mcp` 查看。
- 单个服务器连接失败只记录错误，本地工具和其他服务器继续可用；TUI 先加载本地工具，
  MCP 在后台连接，完成后自动刷新工具表。
- 默认连接 / 调用超时分别为 30 / 60 秒，输出上限 8000 字符，可在 `[limits]` 调整。

模板见 `.mcp.example.json`；真实 `.mcp.json` 可能含私有端点或令牌，已加入 `.gitignore`，不入库。

---

## 会话、上下文与压缩

| 内容 | 位置 |
| --- | --- |
| 会话文件 | `<项目根>/.agents/sessions/<session_id>.json` |
| 压缩归档 | `<项目根>/.agents/context/<archive_id>.jsonl` |

- `/save` 保存当前会话（id、标题、消息、事件、轮次、工具调用数、模型、AGENTS.md 状态）。
- `/resume` 列出历史会话供选择恢复。
- `/new` 先自动保存当前会话，再清空历史并重新注入 `AGENTS.md`。
- `/compact` 先把原始消息完整写入归档，再请求模型生成约 5000 Token 的摘要替换历史；
  摘要缺少细节时模型可用 `recallContext` 检索归档。
- 上下文按中英混合规则粗估 Token；达到 `context_warn_ratio`（默认 80%）后提示压缩，
  并暂停接受新的普通提问，直到压缩完成。

---

## TUI 命令与快捷键

| 命令 | 说明 |
| --- | --- |
| `/help` | 命令与快捷键帮助 |
| `/new` | 新建会话（自动保存旧的） |
| `/save` | 保存会话 |
| `/resume` | 恢复历史会话 |
| `/compact` | 压缩上下文 |
| `/clear` | 清屏（保留模型上下文） |
| `/tools` | 列出当前注册的工具 |
| `/mcp` | 查看 MCP 连接状态与服务器工具 |
| `/model` | 当前模型与接口地址 |
| `/cwd` / `/config` / `/status` | 工作目录 / 配置文件 / 会话状态 |
| `/quit` | 退出 |

`Enter` 发送，`Esc` 关闭弹窗或中断生成，`Tab` 补全命令，`Ctrl+B` 侧栏，
`Ctrl+N` 新会话，`Ctrl+S` 保存，`Ctrl+Q` 退出，`F1` 帮助。

---

## 测试

```bash
cd Agent/TQAgent
python -m unittest discover -s tests -v
```

覆盖文件工具、SSE 解析与工具循环、上下文估算/压缩、技能发现与创建、会话保存恢复、
MCP 配置解析、stdio/HTTP 连接、工具名冲突、失败隔离以及 TUI 组件。

其中「真实 Bazi-MCP 联调」用例依赖本机 `.mcp.json` 与可访问的外部 MCP 服务；
未配置或服务不可达时自动 `skip`，不影响其余用例。

---

## 安全说明

- 文件工具可以读写、删除**进程有权限访问的任意路径**，没有目录白名单；`deleteDir`
  会递归删除，`RunCli` 会直接执行本机程序，不设命令白名单或人工审批。
- 请只在独立测试目录或虚拟机中运行，不要把它指向重要数据目录。
- 密钥保存在 `config.ini` 与 `.mcp.json`，两者均已排除在版本库外；不要提交真实密钥。
- MCP 服务器由第三方提供时，其工具行为与数据安全由对应服务器负责。
