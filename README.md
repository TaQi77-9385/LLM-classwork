# LLM · Agent 实践合集

本仓库是围绕 **大模型应用 / Agent / MCP** 的一组教学实践代码，包含从「最简对话脚本」到
「带工具调用的 Agent 循环」，再到「MCP 服务器」与「完整 REST 服务」的递进式示例。

> 说明：`Agent/Practice01`、`Agent/Practice02`、`Agent/TQAgent` 中的 `config.ini` 存放 API Key，
> 已在各自 `.gitignore` 中排除，请勿提交；示例 Key 请自行替换为你自己的。

---

## 目录导航

| 目录 | 内容 | 技术栈 |
| --- | --- | --- |
| [`Agent/TQAgent`](#agenttqagent--终端本地-agent) | **分层终端 Agent：16 个工具、技能、MCP、上下文压缩** | Python · Textual · Function Calling · MCP |
| [`Agent/Practice01`](#agentpractice01--最小对话与实验循环) | 最简流式对话、日期工具调用实验 | Python · requests · SSE |
| [`Agent/Practice02`](#agentpractice02--本地文件操作-agent) | **本地文件操作 Agent（10 个工具）** | Python · Function Calling · SSE |
| [`anythingLLMWebPage`](#anythingllmwebpage--网页上传文档) | 网页上传文档到 AnythingLLM 并向量化 | 原生 HTML / JS · Fetch |
| [`anythingLLMMCP`](#anythingllmmcp--mcp-服务器) | 读取 AnythingLLM 工作区文件信息的 MCP 服务器 | Python 标准库 · JSON-RPC |
| [`PetHospitalMCP/PetHospitalMCP`](#pethospitalmcppethospitalmcp--宠物医院-rest-api) | Go 标准库实现的宠物医院 REST API | Go 1.22+ · 内嵌网页 |

---

## Agent/TQAgent · 终端本地 Agent

`Agent/TQAgent` 是 `Practice02` 的分层重构版：`core/` 负责配置、流式请求、工具循环、
工具实现、技能、MCP 客户端、上下文压缩与会话存储，`tui/` 用 Textual 提供终端界面。

- **16 个本地工具**：10 个文件/目录操作 + 网页搜索、网页正文提取、命令行执行、
  技能加载/创建、历史上下文检索；MCP 工具连接后动态追加
- **技能**：`.agents/skills/<name>/SKILL.md`，启动时只注入名称与描述，按需加载正文
- **MCP 客户端**：stdio 与 Streamable HTTP，配置项目根目录的 `.mcp.json`，单服务器失败不影响其他工具
- **长期规则**：读取项目根目录 `AGENTS.md` 并注入会话
- **会话能力**：保存 / 恢复、上下文压缩、归档检索

```bash
cd Agent/TQAgent
pip install -r requirements.txt
python main.py
```

配置项、工具参数、技能格式、MCP 配置与快捷键说明见
[`Agent/TQAgent/README.md`](Agent/TQAgent/README.md)。

---

## Agent/Practice02 · 本地文件操作 Agent

**核心文件**：`chat-tq-tool-file0ps.py`

一个命令行 Agent：把用户自然语言请求交给 LLM，由 LLM 通过 **Function Calling**
选择并调用本地文件系统工具，执行结果回灌给模型后输出自然语言答复。

### 架构图（Mermaid）

架构图以 Mermaid 文本形式保存在：

```
Agent/Practice02/chat-tq-tool-file0ps-architecture.mmd
```

内含 4 张图，可直接用 VS Code 的 Markdown Preview Mermaid 插件、`mermaid-cli`
或 <https://mermaid.live> 渲染：

1. **整体分层与依赖** —— 配置层 / 工具声明层 / 工具实现层 / 调度层 / 通信层 / 主流程
2. **多轮时序图** —— 一次用户请求内 `ask → tool_calls → run_tool → 再次 ask` 的完整往返
3. **单轮状态机** —— `Idle → Requesting → Streaming → Parsed → Dispatching → Executing`
4. **工具能力分组** —— 只读 / 创建 / 修改 / 破坏性操作四组的守卫规则

### 运行方式

```bash
cd Agent/Practice02
# 1. 准备配置（config.ini 不入库，需自行创建）
cat > config.ini <<'EOF'
[llm]
api_key = 你的 API Key
base_url = https://api.deepseek.com/v1
model = deepseek-chat
EOF

# 2. 安装依赖并运行（任意 OpenAI 兼容接口均可）
pip install requests
python chat-tq-tool-file0ps.py
```

执行后按提示输入需求即可，例如：

```
你: 看一下当前目录有哪些文件，把 README 前 20 行读出来
你: 在当前目录新建 notes/todo.txt，写三条待办
```

### 代码结构

| 段落 | 关键符号 | 职责 |
| --- | --- | --- |
| 配置 | `cfg` / `llm` / `MAX_TEXT_BYTES` | 读取 `config.ini`，定义 1MB 纯文本上限 |
| 工具声明 | `tools` | 10 个工具的 JSON Schema，随每次请求发给模型 |
| 路径与校验 | `_as_path()` / `_plain_text_error()` | 展开 `~` 与环境变量；判断是否为可读纯文本 |
| 工具实现 | `getCwd` `listDir` `getFileType` `readFile` `createFile` `editFile` `createDir` `deleteDir` `deleteFile` `renamePath` | 全部是纯函数，统一返回 `str`，失败也返回可读文案而非抛异常 |
| 注册与调度 | `HANDLERS` / `run_tool()` | 名称分发、`json.loads` 解析参数、`TypeError`/`OSError` 转文案 |
| 通信 | `ask(messages)` | `stream=True` 请求，逐行解析 SSE，边打印 `content` 边按 `index` 拼接 `tool_calls` |
| 主流程 | `__main__` | 外层单轮输入 + 内层 `while True` 工具循环，直到模型不再请求工具 |

### 10 个工具一览

| 类别 | 工具 | 说明 |
| --- | --- | --- |
| 只读 | `getCwd` | 获取当前工作目录 |
| 只读 | `listDir(path)` | 列出目录内容，目录在前、按名称小写排序 |
| 只读 | `getFileType(path)` | 返回扩展名 / MIME / 是否纯文本 |
| 只读 | `readFile(path)` | 读取纯文本内容（≤1MB，无 NUL，UTF-8） |
| 写 | `createFile(directory, filename, content?)` | 创建新文件，目标已存在则拒绝 |
| 写 | `editFile(path, content)` | 全量覆盖写（仅纯文本） |
| 写 | `createDir(path)` | 创建单层目录，父目录必须存在 |
| 写 | `renamePath(path, new_path)` | 改名或移动，目标已存在则拒绝 |
| 删 | `deleteFile(path)` | 删除文件，传目录会提示改用 `deleteDir` |
| 删 | `deleteDir(path)` | `shutil.rmtree` 删除目录及其全部内容 |

### 设计要点

- **工具即 Schema**：`tools` 中的描述同时是人类可读文档和模型决策依据，因此每条都写明参数与语义。
- **错误即数据**：工具层不抛业务异常，把所有失败转成中文文案交给模型，让模型有机会自行纠错重试。
- **纯函数 + 注册表**：新增工具只需「写函数 → 进 `HANDLERS` → 补 Schema」三步，与 `Practice01` 的
  `if/else` 分发相比更易扩展。
- **流式友好**：`answer` 边收边打印；`tool_calls` 分片按 `index` 累加，兼容 OpenAI 兼容接口的碎片化返回。

### 安全提醒

`deleteDir` / `deleteFile` 会真实修改本地磁盘，且模型可能误判路径。建议在独立目录或虚拟机中试用，
不要把重要目录暴露在该脚本的当前工作目录下。

---

## Agent/Practice01 · 最小对话与实验循环

| 文件 | 说明 |
| --- | --- |
| `chat.py` | 最简流式对话脚本 |
| `chat-tq-loop-history.py` | 带 `getDate` 单一工具的循环版，演示 `tool_calls → role=tool → 再请求` 的完整闭环 |

`Practice01` 是 `Practice02` 的“单工具版本”：两者 `ask()` 的流式解析逻辑一致，差别在于
`Practice02` 用注册表 `HANDLERS` 替代了 `if/else` 分支，并把工具扩展到 10 个。

```bash
cd Agent/Practice01
pip install requests
python chat-tq-loop-history.py
```

---

## anythingLLMWebPage · 网页上传文档

`index.html` 单文件工具：填写 API Key、选择文档，即可上传到 AnythingLLM 的**第一个工作区**并触发向量化。

- 先 `GET /api/v1/workspaces` 取首个工作区 `slug`
- 再 `POST /api/v1/document/upload`，`FormData` 携带 `file` 与 `addToWorkspaces`
- 默认后端 `http://localhost:3001`，需在 AnythingLLM 中创建 Developer API Key

```bash
# 直接用浏览器打开 index.html，或起个静态服务器
python -m http.server 8080
```

---

## anythingLLMMCP · MCP 服务器

`server.py` 用标准库实现一个 MCP（Model Context Protocol）服务器，暴露单个工具
`first_workspace_files`：读取 AnythingLLM 第一个工作区的文件清单。

- 传输方式：Streamable HTTP，端点 `POST /mcp`（`GET /mcp` 为旧版 SSE 长连接）
- 同时兼容两代协议：
  - 旧版（`2025-03-26` ~ `2025-11-25`）：`initialize` 握手 + `Mcp-Session-Id` + `GET` 长连接、`DELETE` 销毁会话
  - 新版（`2026-07-28`）：无握手、无会话、无 `GET` 长连接；校验 `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` 头
- 支持 `ping`、`tools/list`、`tools/call`，新版额外支持 `server/discover`

```bash
export ANYTHINGLLM_BASE=http://127.0.0.1:3001/api
export ANYTHINGLLM_API_KEY=你的AnythingLLM开发Key
export PORT=8765
python anythingLLMMCP/server.py
# MCP 端点： http://127.0.0.1:8765/mcp
```

---

## PetHospitalMCP/PetHospitalMCP · 宠物医院 REST API

Go 标准库实现的本地宠物医院管理系统，**零第三方依赖**，网页界面通过 `embed` 内嵌进可执行文件，
数据存放在单个自研格式的文件 `data/pet.db`（append-only 日志 + CRC32 + 内存索引 + 自动压实）。

- 启动后访问 `http://127.0.0.1:8080/` 即为网页操作界面
- 提供 29 个 REST 接口：档案 CRUD、全文检索、病历 / 收费追加、批量、导出（JSON/CSV）、统计
- 迁移只需 `可执行文件 + data/pet.db` 两个文件

```bash
cd PetHospitalMCP/PetHospitalMCP/pet-hospital-mcp-teaching-main/pet-hospital-mcp-teaching-main
go run . -seed -count 2000     # 灌入模拟数据并启动
```

详细接口与数据模型见该目录下的 `README.md`。

---

## 环境要求汇总

| 项目 | 运行时 | 依赖 |
| --- | --- | --- |
| TQAgent | Python 3.10+ | `requests`、`textual`、`trafilatura`、`mcp`；`fetchWeb` 需 `curl` |
| Practice01 / Practice02 | Python 3.8+ | `requests`；`getDate` 工具需 PowerShell（Windows） |
| anythingLLMWebPage | 现代浏览器 | 需可访问的 AnythingLLM 实例 |
| anythingLLMMCP | Python 3.8+ | 仅标准库；需 AnythingLLM Developer API Key |
| PetHospitalMCP | Go 1.22+ | 仅标准库 |

## 许可与声明

代码仅用于教学与本地实验：明文 HTTP、无鉴权、明文存放 API Key。请勿直接暴露到公网，
上线前务必补充鉴权、密钥管理与最小权限控制。
