---
name: officecli
description: 调用项目内的 officecli-win-x64.exe 读取、创建和修改 Word（.docx）、Excel（.xlsx）、PowerPoint（.pptx）。用户要处理 Office 文档、表格、幻灯片，或提到 docx、xlsx、pptx、Word、Excel、PowerPoint、officecli 时，先用 loadSkill 加载本技能，再用 RunCli 调用该 exe，不要改用 python-docx。
---

# officecli

用项目里的 `officecli-win-x64.exe` 处理 `.docx`、`.xlsx`、`.pptx`。不需要安装 Office。

本机没有名为 `officecli` 的命令。不要执行 `where officecli`，也不要改用 python-docx、openpyxl、python-pptx。可执行文件就在本技能目录：

`.agents/skills/officecli/officecli-win-x64.exe`

## 怎么调用

只用 `RunCli`。

- `command`：上面的 exe 路径。优先用相对于项目根目录的路径；若当前目录不是项目根，先 `getCwd`，再拼成绝对路径。
- `args`：字符串列表，每一项是一个参数。每次都在最后加上 `--json`。
- 不要把整条命令写进 `command`，也不要套 `cmd /c` 或 PowerShell。

`RunCli` 不经过 shell。路径、方括号和 `$` 直接作为单独参数传入，不要再加引号。

若返回「未找到命令行程序：officecli」，说明 `command` 写成了命令名。改成 `officecli-win-x64.exe` 的路径再试，不要换别的库。

新建 Word 文件：

- command: `.agents/skills/officecli/officecli-win-x64.exe`
- args: `["create", "report.docx", "--json"]`

读取一段：

- args: `["get", "report.docx", "/body/p[1]", "--depth", "2", "--json"]`

文件参数用绝对路径，或相对于当前工作目录的路径。

## 两个不同的「加载」

- TQAgent 的 `loadSkill`：加载本技能，或读取同目录的 `OfficeCli操作说明.md`。
- exe 的 `load_skill` 子命令：加载某一类文档的专用规则。这是 `RunCli` 的参数，不是 TQAgent 的工具名。

一份成品只加载一个最具体的规则，不要叠加。先运行，再按它打印的规则做：

- args: `["load_skill", "<name>", "--json"]`

| 场景 | name |
| --- | --- |
| 通用 Word：报告、信函、备忘、方案 | `word` |
| 期刊 / 论文 / 学位论文 | `academic-paper` |
| 通用 PPT | `pptx` |
| 仅融资路演（seed / A–C 轮） | `pitch-deck` |
| Morph 动画 PPT / 3D Morph | `morph-ppt` / `morph-ppt-3d` |
| 通用 Excel：公式、透视、台账 | `excel` |
| 财务模型、情景预测 | `financial-model` |
| KPI / 分析仪表盘 | `data-dashboard` |

同一会话里已经加载过的规则不必再加载。若正文还要遵守别的已加载技能（例如公司内部通知的标题和落款），先按那个技能定稿，再用本技能写入文件。

## 工作顺序

优先高层，不够再用下一层：**L1 读取 → L2 DOM 编辑 → L3 原始 XML**。

1. 已有文件：先 `view` / `get` / `query`，确认结构和路径，再改。
2. 新文件：`create <file>`，类型由扩展名决定。`create` 不覆盖已有文件。
3. 改内容用 `add` / `set` / `remove` / `move` / `swap`。多步修改用一次 `batch`。
4. 属性名、枚举、路径不确定时先查 help，不要猜。
   - args: `["help", "docx", "paragraph", "--json"]`
   - `word`→`docx`，`excel`→`xlsx`，`ppt` / `powerpoint`→`pptx`
   - 动词：`add`、`set`、`get`、`query`、`remove`
5. 改完用 `validate <file>`。要看文本时用 `view <file> text` 或 `view <file> outline`。
6. 连续操作同一文件时先 `open <file>`，结束时 `close <file>`。交给 Word、Excel、PowerPoint 或其他程序读取之前，必须先 `save` 或 `close`。

## 参数约定

- 属性一律用 `--prop` 加 `key=value`，例如两项参数 `"--prop"` 和 `"text=摘要"`。不要写 `--name`。
- 路径从 1 开始：`/body/p[3]` 是第三段。`--index` 从 0 开始。Excel 的 `add --type row` 或 `col` 例外，`--index` 从 1 开始。
- 有稳定 ID 时优先用 `@id=`、`@paraId=`、`@name=`，不要依赖会变化的序号。
- PPT 的 `shape[1]` 通常是标题占位符，正文形状从 `shape[2]` 起。
- 退出码：`0` 成功，`1` 失败，`2` 有警告。同时看 JSON 里的 `success` 和 `warnings`。

## 常用命令

参数按顺序放进 `args`，并带上 `--json`。

```text
create report.docx
get report.docx /body/p[1] --depth 2
view data.xlsx outline
add report.docx /body --type paragraph --prop text=摘要 --prop style=Heading1
set data.xlsx /Sheet1/A1 --prop value=姓名 --prop bold=true
add slides.pptx / --type slide --prop title=Q4
batch data.xlsx --commands [{"command":"set","path":"/Sheet1/A1","props":{"value":"姓名","bold":"true"}}]
```

## 详细参考

命令全集、元素类型、batch、raw-set、resident 模式和踩坑表在同目录 `OfficeCli操作说明.md`。需要具体语法时，用 `loadSkill` 读取该文件的对应章节。属性名仍以 `help` 的输出为准。
