"""从项目根目录发现并注册 Agent Skill，也可新建 Skill。

项目根目录是程序启动时的工作目录。初始化只读取每个 SKILL.md 开头的
YAML Front Matter，登记 name 和 description，不读取正文。
createSkill 只创建目录和 SKILL.md；附加文件由文件工具继续完成。
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from core.files import readFile

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
_BLOCK = {">", ">-", ">+", "|", "|-", "|+"}
_MAX_FRONT_LINES = 200
_MAX_DESCRIPTION = 1024


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path


class SkillRegistry:
    def __init__(self, skills: tuple[SkillInfo, ...], root: Path | None = None):
        self.skills = skills
        self.root = None if root is None else Path(root)
        self._by_name = {skill.name: skill for skill in skills}

    def get(self, name: str) -> SkillInfo | None:
        return self._by_name.get(name)

    def __iter__(self):
        return iter(self.skills)

    def __len__(self) -> int:
        return len(self.skills)

    def add(self, skill: SkillInfo) -> None:
        if skill.name in self._by_name:
            return
        self.skills = tuple(sorted((*self.skills, skill), key=lambda item: item.name))
        self._by_name[skill.name] = skill


def skill_prompt(skills: SkillRegistry) -> str:
    """把已发现技能的 name 和 description 整理成 System Prompt。没有技能时返回空字符串。"""
    if not skills:
        return ""
    lines = ["You have access to the following skills:"]
    for skill in skills:
        lines.append(f"-'{skill.name}':{skill.description}")
    lines.append(
        "Use the 'loadSkill' tool to load the full instructions of a skill when needed."
        "You can also load additional files referenced by a skill."
    )
    return "\n".join(lines)


SCHEMA = {
    "type": "function",
    "function": {
        "name": "loadSkill",
        "description": "加载指定 Skill 的完整说明。只提供 name 时返回 SKILL.md；同时提供 filename 时只读取该附加文件。",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill 名称"},
                "filename": {
                    "type": "string",
                    "description": "可选。相对 Skill 目录的路径、项目相对路径、绝对路径，或 Skill 目录内的文件名",
                },
            },
            "required": ["name"],
        },
    },
}

CREATE_SCHEMA = {
    "type": "function",
    "function": {
        "name": "createSkill",
        "description": (
            "用户要求创建 Skill 时调用。在当前项目的 .agents/skills/<name>/ 下创建目录并写入 SKILL.md。"
            "name 只能由小写字母、数字和连字符组成，不能以连字符开头或结尾，最长 64 个字符，并作为英文目录名。"
            "skillContext 是 SKILL.md 的完整内容，必须符合 Agent Skills 规范："
            "以 --- 包裹 YAML Frontmatter，其中 name 与参数 name 相同，description 为非空且不超过 1024 字符，Frontmatter 之后必须有正文。"
            "同名 Skill 已存在时只返回提示，不覆盖。"
            "不要用本工具创建 references/、scripts/ 或其他辅助文件，那些请继续调用 createDir 和 createFile。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 名称，同时作为 .agents/skills/ 下的英文目录名。只能包含小写字母、数字和连字符。",
                },
                "skillContext": {
                    "type": "string",
                    "description": "写入 SKILL.md 的完整内容，包含 YAML Frontmatter（name、description）和正文。",
                },
            },
            "required": ["name", "skillContext"],
        },
    },
}

_active: SkillRegistry | None = None


def bind_skills(registry: SkillRegistry) -> None:
    global _active
    _active = registry


def loadSkill(name: str = "", filename: str | None = None) -> str:
    """按需读取 Skill 文件。结果由现有工具循环写回对话，不在启动时进入上下文。"""
    if _active is None:
        return "没有已注册的 Skill"
    skill_name = str(name or "").strip()
    if not skill_name:
        return "name 不能为空"
    skill = _active.get(skill_name)
    if skill is None:
        return f"未找到 Skill: {skill_name}"
    requested = "" if filename is None else str(filename).strip()
    if not requested:
        return readFile(skill.path / "SKILL.md")
    return _read_named(_active.root, skill.path, requested)


def createSkill(name: str = "", skillContext: str = "") -> str:
    """创建 .agents/skills/<name>/SKILL.md。目录已存在时不覆盖。"""
    if not isinstance(name, str):
        return "name 必须是字符串"
    if not isinstance(skillContext, str):
        return "skillContext 必须是字符串"
    skill_name = name.strip()
    name_error = _skill_name_error(skill_name)
    if name_error:
        return name_error
    content_error = _skill_document_error(skill_name, skillContext)
    if content_error:
        return content_error

    root = _project_root()
    target = root / ".agents" / "skills" / skill_name
    if target.exists():
        return _exists_message(skill_name, target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=False, exist_ok=False)
    except FileExistsError:
        return _exists_message(skill_name, target)
    except OSError as exc:
        return f"创建 Skill 失败: {exc}"

    skill_file = target / "SKILL.md"
    try:
        skill_file.write_text(skillContext, encoding="utf-8")
    except OSError as exc:
        _remove_if_empty(target)
        return f"创建 Skill 失败: {exc}"
    _register_created(root, target, skill_file)
    return (
        f"已创建 Skill: {skill_name}，文件: {skill_file}。"
        "如需 references/、scripts/ 或其他辅助文件，请继续使用 createDir 和 createFile。"
    )


def _read_named(project_root: Path | None, skill_dir: Path, raw: str) -> str:
    skill_root = skill_dir.resolve()
    given = Path(os.path.expandvars(raw)).expanduser()
    if given.is_absolute():
        if not _is_inside(skill_root, given):
            return "只能读取该 Skill 目录内的文件"
        if not _is_file_inside(skill_root, given):
            return f"未找到文件: {raw}"
        return readFile(given.resolve())

    skill_file = skill_dir / given
    if not _is_inside(skill_root, skill_file):
        return "只能读取该 Skill 目录内的文件"
    if _is_file_inside(skill_root, skill_file):
        return readFile(skill_file.resolve())

    if project_root is not None and given.parent != Path("."):
        project_file = Path(project_root) / given
        if _is_file_inside(skill_root, project_file):
            return readFile(project_file.resolve())
        if project_file.exists() and not _is_inside(skill_root, project_file):
            return "只能读取该 Skill 目录内的文件"

    if _is_bare_name(raw):
        matches = _find_by_name(skill_root, given.name)
        if len(matches) == 1:
            return readFile(matches[0])
        if len(matches) > 1:
            lines = ["找到多个同名文件，请用 filename 指定其中一个："]
            lines.extend(item.relative_to(skill_root).as_posix() for item in matches)
            return "\n".join(lines)
    return f"未找到文件: {raw}"


def _is_bare_name(raw: str) -> bool:
    if any(char in raw for char in "*?[]"):
        return False
    path = Path(raw)
    return not path.is_absolute() and path.name == raw and raw not in {"", ".", ".."}


def _is_inside(skill_root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(skill_root)
    except (OSError, ValueError):
        return False
    return True


def _is_file_inside(skill_root: Path, candidate: Path) -> bool:
    if not _is_inside(skill_root, candidate):
        return False
    return candidate.resolve().is_file()


def _find_by_name(skill_root: Path, filename: str) -> list[Path]:
    found: list[Path] = []
    seen: set[Path] = set()
    for item in skill_root.rglob(filename):
        if not item.is_file() or item.name != filename:
            continue
        resolved = item.resolve()
        if not _is_inside(skill_root, resolved) or resolved in seen:
            continue
        seen.add(resolved)
        found.append(resolved)
    found.sort(key=lambda path: path.as_posix().lower())
    return found


def discover_skills(root: Path | None = None) -> SkillRegistry:
    """扫描 root/.agents/skills 下的直接子目录。root 为空时使用当前工作目录。"""
    project_root = Path.cwd() if root is None else Path(root)
    skills_dir = project_root / ".agents" / "skills"
    found: list[SkillInfo] = []
    if skills_dir.is_dir():
        for child in skills_dir.iterdir():
            if not child.is_dir():
                continue
            skill_file = child / "SKILL.md"
            if not skill_file.is_file():
                continue
            info = _load_skill(child, skill_file)
            if info is not None:
                found.append(info)
    found.sort(key=lambda item: item.name)
    return SkillRegistry(tuple(found), project_root)


def _load_skill(directory: Path, skill_file: Path) -> SkillInfo | None:
    fields = _front_matter(skill_file)
    if fields is None:
        return None
    name = fields.get("name", "").strip()
    description = fields.get("description", "").strip()
    if not _valid_name(name, directory.name):
        return None
    if not description or len(description) > _MAX_DESCRIPTION:
        return None
    return SkillInfo(name=name, description=description, path=directory)


def _valid_name(name: str, directory_name: str) -> bool:
    return bool(_NAME.fullmatch(name)) and name == directory_name


def _project_root() -> Path:
    if _active is not None and _active.root is not None:
        return Path(_active.root)
    return Path.cwd()


def _skill_name_error(name: str) -> str:
    if not name:
        return "name 不能为空"
    if _NAME.fullmatch(name):
        return ""
    return "Skill 名称无效：只能使用小写字母、数字和连字符，不能以连字符开头或结尾，最长 64 个字符"


def _skill_document_error(name: str, text: str) -> str:
    if not text.strip():
        return "skillContext 不能为空"
    parts = _split_front_matter(text)
    if parts is None:
        return (
            "skillContext 不符合 Agent Skills 规范："
            "必须以 --- 开始和结束的 YAML Frontmatter 开头，并包含 name、description 和正文"
        )
    front, body = parts
    fields = _parse_fields(front)
    front_name = fields.get("name", "").strip()
    if front_name != name:
        shown = front_name or "（空）"
        return f"Frontmatter 中的 name 必须是 {name}，当前为 {shown}"
    description = fields.get("description", "").strip()
    if not description:
        return "Frontmatter 缺少 description"
    if len(description) > _MAX_DESCRIPTION:
        return "description 超过 1024 个字符"
    if not body.strip():
        return "SKILL.md 缺少正文"
    return ""


def _split_front_matter(text: str) -> tuple[str, str] | None:
    lines = text.splitlines(keepends=True)
    if not lines:
        return None
    if lines[0].lstrip("\ufeff").strip() != "---":
        return None
    front: list[str] = []
    closed = False
    index = 1
    for _ in range(_MAX_FRONT_LINES):
        if index >= len(lines):
            break
        line = lines[index]
        index += 1
        if line.strip() == "---":
            closed = True
            break
        front.append(line)
    if not closed:
        return None
    return "".join(front), "".join(lines[index:])


def _exists_message(name: str, target: Path) -> str:
    return f"Skill 已存在: {name}，路径: {target}。未覆盖原有内容。"


def _remove_if_empty(directory: Path) -> None:
    try:
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    except OSError:
        return


def _register_created(project_root: Path, directory: Path, skill_file: Path) -> None:
    if _active is None:
        return
    if _active.root is not None:
        try:
            if Path(_active.root).resolve() != Path(project_root).resolve():
                return
        except OSError:
            return
    info = _load_skill(directory, skill_file)
    if info is not None:
        _active.add(info)


def _front_matter(path: Path) -> dict[str, str] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first = handle.readline().lstrip("\ufeff")
            if first.strip() != "---":
                return None
            lines: list[str] = []
            closed = False
            for _ in range(_MAX_FRONT_LINES):
                line = handle.readline()
                if line == "":
                    break
                if line.strip() == "---":
                    closed = True
                    break
                lines.append(line)
    except OSError:
        return None
    if not closed:
        return None
    return _parse_fields("".join(lines))


def _parse_fields(front: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    lines = front.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line[0] in " \t#":
            index += 1
            continue
        key, sep, raw = line.partition(":")
        if not sep or not key.strip():
            index += 1
            continue
        key = key.strip()
        raw = raw.strip()
        index += 1
        if raw in _BLOCK:
            folded = raw.startswith(">")
            block: list[str] = []
            while index < len(lines) and (not lines[index].strip() or lines[index][0] in " \t"):
                block.append(lines[index].strip())
                index += 1
            parts = [part for part in block if part]
            value = " ".join(parts) if folded else "\n".join(parts)
        else:
            value = _unquote(raw)
        if key in ("name", "description") and key not in fields:
            fields[key] = value.strip()
    return fields


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
