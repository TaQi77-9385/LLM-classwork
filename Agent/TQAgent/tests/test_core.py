import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.cli import RunCli
from core.commands import match_commands
from core.config import (
    ConfigError,
    LlmSettings,
    RuntimeLimits,
    SearchSettings,
    ensure_config,
    get_limits,
    load_search_settings,
    load_settings,
    set_limits,
    write_mock_config,
)
from core.store import derive_title, list_sessions, load_session, save_session, SessionRecord
from core.fetch import HtmlParseError, fetchWeb
from core.context import (
    DEFAULT_CONTEXT_WINDOW,
    archive_note,
    bind_context_root,
    estimate_tokens,
    recallContext,
    save_archive,
)
from core.events import (
    AgentError,
    CompactFinished,
    ContextWarning,
    TextDelta,
    ToolFinished,
    ToolStarted,
    TurnFinished,
)
from core.files import run_tool
from core.llm import LlmDone, LlmText, StreamState, feed
from core.registry import run_tool as run_registered_tool
from core.search import configure, format_references, searchWeb
from core.session import AgentSession, create_session
from core.skills import (
    SkillInfo,
    SkillRegistry,
    bind_skills,
    createSkill,
    discover_skills,
    loadSkill,
    skill_prompt,
)


def call(name, arguments="{}"):
    return {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}}


class FilesystemTests(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            self.assertIn("已创建目录", run_tool(call("createDir", json.dumps({"path": str(root / "box")}))))
            (root / "box" / "keep.txt").write_text("x", encoding="utf-8")
            created = run_tool(call("createFile", json.dumps({
                "directory": str(root / "box"),
                "filename": "note.txt",
                "content": "你好",
            })))
            self.assertIn("已创建", created)
            self.assertEqual(run_tool(call("readFile", json.dumps({"path": str(root / "box" / "note.txt")}))), "你好")
            self.assertIn("纯文本", run_tool(call("getFileType", json.dumps({"path": str(root / "box" / "note.txt")}))))
            listed = run_tool(call("listDir", json.dumps({"path": str(root / "box")})))
            self.assertIn("[文件] note.txt", listed)
            self.assertIn("[文件] keep.txt", listed)
            self.assertIn("已更新", run_tool(call("editFile", json.dumps({
                "path": str(root / "box" / "note.txt"),
                "content": "改过",
            }))))
            self.assertEqual((root / "box" / "note.txt").read_text(encoding="utf-8"), "改过")
            renamed = run_tool(call("renamePath", json.dumps({
                "path": str(root / "box" / "note.txt"),
                "new_path": "renamed.txt",
            })))
            self.assertIn("已重命名", renamed)
            self.assertTrue((root / "box" / "renamed.txt").is_file())
            self.assertIn("已删除文件", run_tool(call("deleteFile", json.dumps({"path": str(root / "box" / "renamed.txt")}))))
            self.assertIn("已删除目录", run_tool(call("deleteDir", json.dumps({"path": str(root / "box")}))))
            self.assertFalse((root / "box").exists())

    def test_rejects_binary_and_bad_names(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            binary = root / "a.bin"
            binary.write_bytes(b"a\x00b")
            self.assertEqual(run_tool(call("readFile", json.dumps({"path": str(binary)}))), "不是纯文本文件")
            self.assertEqual(
                run_tool(call("createFile", json.dumps({"directory": str(root), "filename": "sub/a.txt"}))),
                "文件名不能包含路径",
            )
            self.assertEqual(run_tool(call("missing", "{}")), "未知工具")
            cwd = run_tool(call("getCwd"))
            self.assertTrue(cwd)


class StreamTests(unittest.TestCase):
    def test_feed_assembles_text_and_tool_calls(self):
        state = StreamState()
        first = feed(state, b'data: {"choices":[{"delta":{"content":"\xe4\xbd\xa0"}}]}')
        self.assertEqual(first, "你")
        feed(state, b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"a","type":"function","function":{"name":"get","arguments":""}}]}}]}')
        feed(state, b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}]}')
        self.assertIsNone(feed(state, b"data: [DONE]"))
        self.assertTrue(state.done)
        self.assertEqual(state.answer, "你")
        self.assertEqual(state.calls[0]["function"]["name"], "get")
        self.assertEqual(state.calls[0]["function"]["arguments"], "{}")


class ScriptedClient:
    def __init__(self, rounds):
        self.rounds = rounds
        self.seen = []
        self.seen_kwargs = []

    def stream(self, messages, *, use_tools=True):
        self.seen.append(list(messages))
        self.seen_kwargs.append({"use_tools": use_tools})
        yield from self.rounds[len(self.seen) - 1]


class SessionTests(unittest.TestCase):
    def test_tool_loop_then_final_answer(self):
        tool_call = call("getCwd")
        client = ScriptedClient([
            [LlmText("先看"), LlmDone("先看", [tool_call])],
            [LlmText("好了"), LlmDone("好了", [])],
        ])
        session = AgentSession(client, tool_names=("getCwd",))
        events = list(session.submit("当前目录"))
        self.assertIsInstance(events[0], TextDelta)
        self.assertIsInstance(events[1], ToolStarted)
        self.assertIsInstance(events[2], ToolFinished)
        self.assertIsInstance(events[3], TextDelta)
        self.assertIsInstance(events[4], TurnFinished)
        self.assertEqual(events[4].answer, "好了")
        roles = [item["role"] for item in session.messages]
        self.assertEqual(roles, ["user", "assistant", "tool", "assistant"])
        self.assertEqual(client.seen[0][0]["role"], "user")
        self.assertEqual(client.seen[1][0]["role"], "user")
        self.assertIn(session.messages[2]["content"], events[2].result)

    def test_catalog_includes_search_web(self):
        session = AgentSession(ScriptedClient([]))
        names = [name for name, _ in session.tool_catalog]
        self.assertIn("searchWeb", names)
        self.assertIn("fetchWeb", names)
        self.assertIn("RunCli", names)
        self.assertIn("loadSkill", names)
        self.assertIn("createSkill", names)
        self.assertIn("recallContext", names)
        self.assertEqual(len(names), 16)


class CliTests(unittest.TestCase):
    def test_runs_program_with_argument_list(self):
        seen = {}

        def fake_run(args, capture_output, timeout, check, shell):
            seen["args"] = args
            seen["shell"] = shell
            seen["timeout"] = timeout
            return subprocess.CompletedProcess(args, 0, stdout="分支 main\n".encode("utf-8"), stderr=b"")

        self._patch(fake_run)
        text = RunCli("git", ["status", "-sb"])
        self.assertEqual(seen["args"], ["git", "status", "-sb"])
        self.assertFalse(seen["shell"])
        self.assertEqual(seen["timeout"], 30)
        self.assertIn("退出码：0", text)
        self.assertIn("标准输出：\n分支 main", text)
        self.assertIn("标准错误：\n（无）", text)

    def test_rejects_empty_program_and_string_args(self):
        self.assertEqual(RunCli("  "), "command 不能为空")
        self.assertEqual(RunCli("git", "status"), "参数 args 必须是字符串列表")

    def test_timeout_missing_program_and_stderr(self):
        def explode(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="git", timeout=30)

        self._patch(explode)
        timed_out = RunCli("git", ["status"])
        self.assertIn("退出码：未结束", timed_out)
        self.assertIn("标准输出：", timed_out)
        self.assertIn("命令执行超时", timed_out)

        def missing(*args, **kwargs):
            raise FileNotFoundError("nope")

        self._patch(missing)
        missing_text = RunCli("not-installed")
        self.assertIn("退出码：未执行", missing_text)
        self.assertIn("标准输出：\n（无）", missing_text)
        self.assertIn("未找到命令行程序：not-installed", missing_text)

        self._patch(lambda *args, **kwargs: subprocess.CompletedProcess(args, 1, stdout=b"", stderr="找不到文件".encode("utf-8")))
        text = RunCli("git", ["show"])
        self.assertIn("退出码：1", text)
        self.assertIn("标准输出：\n（无）", text)
        self.assertIn("标准错误：\n找不到文件", text)

    def test_truncates_long_output(self):
        long_text = "行\n" * 5000
        self._patch(lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, stdout=long_text.encode("utf-8"), stderr=b"",
        ))
        text = RunCli("git", ["log"])
        self.assertIn("已截断", text)
        self.assertIn(f"原长 {len(long_text.strip())} 字符", text)
        self.assertLess(len(text), len(long_text))
        self.assertTrue(text.startswith("退出码：0"))

    def _patch(self, fake_run):
        import core.cli as cli_mod
        original = cli_mod.subprocess.run
        cli_mod.subprocess.run = fake_run
        self.addCleanup(setattr, cli_mod.subprocess, "run", original)


class FetchTests(unittest.TestCase):
    PAGE = "https://example.edu.cn/about"

    def test_empty_and_invalid_url(self):
        self.assertEqual(fetchWeb("  "), "页面地址不能为空")
        self.assertIn("URL 无效", fetchWeb("file:///C:/secret.txt"))

    def test_curl_result_becomes_markdown_with_original_url(self):
        html = "<html><body><article><h1>关于</h1><p>学校简介</p></article></body></html>".encode("utf-8")
        seen = {}

        def fake_run(args, capture_output, timeout, check, shell):
            seen["args"] = args
            seen["shell"] = shell
            return subprocess.CompletedProcess(args, 0, stdout=html + b"\n200", stderr=b"")

        def fake_extract(page, url):
            seen["html"] = page
            seen["url"] = url
            return "# 关于\n\n学校简介"

        self._patch(fake_run, fake_extract)
        text = fetchWeb(self.PAGE)
        self.assertIsInstance(seen["args"], list)
        self.assertEqual(seen["args"][0], "curl")
        self.assertEqual(seen["args"][-1], self.PAGE)
        self.assertFalse(seen["shell"])
        self.assertEqual(seen["html"], html)
        self.assertEqual(seen["url"], self.PAGE)
        self.assertIn(f"URL：{self.PAGE}", text)
        self.assertIn("# 关于", text)
        self.assertIn("学校简介", text)

    def test_http_error_timeout_and_missing_curl(self):
        self._patch(lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"missing\n404", stderr=b""))
        self.assertEqual(fetchWeb(self.PAGE), f"网页访问失败（HTTP 404）：{self.PAGE}")
        def explode(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="curl", timeout=30)
        self._patch(explode)
        self.assertIn("网络请求超时", fetchWeb(self.PAGE))
        def missing(*args, **kwargs):
            raise FileNotFoundError("curl")
        self._patch(missing)
        self.assertIn("未找到本地 curl", fetchWeb(self.PAGE))

    def test_connection_failure_and_empty_article(self):
        self._patch(lambda *args, **kwargs: subprocess.CompletedProcess(args, 7, stdout=b"\n000", stderr=b"curl: (7) Failed to connect"))
        self.assertIn("网页访问失败", fetchWeb(self.PAGE))
        self._patch(
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"<html></html>\n200", stderr=b""),
            extract=lambda html, url: "",
        )
        self.assertEqual(fetchWeb(self.PAGE), "正文无法提取。")

    def test_keeps_structure_and_drops_boilerplate(self):
        import core.fetch as fetch_mod
        html = """
        <html><head><title>学院介绍</title></head><body>
        <nav>首页 招生导航</nav>
        <div class="ad">立即购买广告位</div>
        <article>
          <h1>迎新安排</h1>
          <p>新生请于九月报到。</p>
          <ul><li>携带证件</li><li>完成注册</li></ul>
          <table><tr><th>项目</th><th>时间</th></tr><tr><td>报到</td><td>9月1日</td></tr></table>
          <p>详见<a href="https://example.edu.cn/guide">报到指南</a>。</p>
        </article>
        <footer>版权所有 联系我们</footer>
        </body></html>
        """.encode("utf-8")
        text = fetch_mod._extract_markdown(html, self.PAGE)
        self.assertIn("# 迎新安排", text)
        self.assertIn("新生请于九月报到。", text)
        self.assertIn("- 携带证件", text)
        self.assertIn("| 报到 | 9月1日 |", text)
        self.assertIn("[报到指南](https://example.edu.cn/guide)", text)
        self.assertNotIn("招生导航", text)
        self.assertNotIn("立即购买广告位", text)
        self.assertNotIn("版权所有", text)

    def test_html_parse_failure(self):
        self._patch(lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"not html\n200", stderr=b""))
        self.assertEqual(fetchWeb(self.PAGE), "HTML 解析失败，无法从该页面提取正文。")

        def broken(html, url):
            raise HtmlParseError("bad")

        self._patch(
            lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"<html></html>\n200", stderr=b""),
            extract=broken,
        )
        self.assertEqual(fetchWeb(self.PAGE), "HTML 解析失败，无法从该页面提取正文。")

    def _patch(self, fake_run, extract=None):
        import core.fetch as fetch_mod
        original_run = fetch_mod.subprocess.run
        original_extract = fetch_mod._extract_markdown
        fetch_mod.subprocess.run = fake_run
        if extract is not None:
            fetch_mod._extract_markdown = extract
        self.addCleanup(setattr, fetch_mod.subprocess, "run", original_run)
        self.addCleanup(setattr, fetch_mod, "_extract_markdown", original_extract)


class SearchTests(unittest.TestCase):
    def tearDown(self):
        configure(SearchSettings(api_key="", base_url="https://qianfan.baidubce.com", source=Path(".")))

    def test_formats_title_summary_and_url(self):
        text = format_references([
            {"title": "学院官网", "snippet": "招生信息", "url": "https://example.edu.cn"},
        ])
        self.assertIn("1. 学院官网", text)
        self.assertIn("摘要：招生信息", text)
        self.assertIn("URL：https://example.edu.cn", text)

    def test_empty_query(self):
        self.assertEqual(searchWeb("  "), "搜索关键词不能为空")

    def test_missing_key(self):
        configure(SearchSettings(api_key="", base_url="https://qianfan.baidubce.com", source=Path(".")))
        self.assertIn("baidu_qianfan_api_key", searchWeb("成都天气"))

    def test_missing_config_file_is_tool_result(self):
        configure(load_search_settings(Path("Z:/no/such-search.ini")))
        text = run_registered_tool(call("searchWeb", json.dumps({"query": "天气"})))
        self.assertIn("未找到 config.ini", text)

    def test_missing_section_is_tool_result(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.ini"
            path.write_text("[llm]\napi_key = llm-key\nbase_url = http://x\nmodel = m\n", encoding="utf-8")
            configure(load_search_settings(path))
            text = searchWeb("天气")
        self.assertIn("缺少 [search]", text)

    def test_timeout_returns_message(self):
        self._patch_post(lambda *args, **kwargs: (_ for _ in ()).throw(requests.Timeout("timed out")))
        self.assertEqual(searchWeb("天气"), "网络请求超时，百度千帆搜索没有在限定时间内响应。请稍后重试。")

    def test_connection_error_returns_message(self):
        self._patch_post(lambda *args, **kwargs: (_ for _ in ()).throw(requests.ConnectionError("refused")))
        self.assertEqual(searchWeb("天气"), "网络连接失败，无法访问百度千帆搜索服务。请检查网络后重试。")

    def test_unreadable_body_returns_message(self):
        class Response:
            status_code = 502
            text = "<html>bad</html>"

            def json(self):
                raise ValueError("not json")

        self._patch_post(lambda *args, **kwargs: Response())
        self.assertEqual(searchWeb("天气"), "百度千帆返回了无法解析的内容（HTTP 502）。")

    def _patch_post(self, fake):
        configure(SearchSettings(api_key="test-key", base_url="https://qianfan.baidubce.com", source=Path(".")))
        import core.search as search_mod
        original = search_mod.requests.post
        search_mod.requests.post = fake
        self.addCleanup(setattr, search_mod.requests, "post", original)

    def test_posts_query_and_returns_references(self):
        configure(SearchSettings(api_key="test-key", base_url="https://qianfan.baidubce.com", source=Path(".")))
        captured = {}

        class Response:
            status_code = 200
            text = ""

            def json(self):
                return {"references": [{"title": "结果", "content": "摘要正文", "url": "https://a.test/x"}]}

        def fake_post(url, headers, json, timeout):
            captured["url"] = url
            captured["auth"] = headers["Authorization"]
            captured["json"] = json
            captured["timeout"] = timeout
            return Response()

        import core.search as search_mod
        original = search_mod.requests.post
        search_mod.requests.post = fake_post
        try:
            text = searchWeb("北京有哪些旅游景区")
        finally:
            search_mod.requests.post = original
        self.assertEqual(captured["url"], "https://qianfan.baidubce.com/v2/ai_search/web_search")
        self.assertEqual(captured["auth"], "Bearer test-key")
        self.assertEqual(captured["json"]["messages"][0]["content"], "北京有哪些旅游景区")
        self.assertEqual(captured["json"]["search_source"], "baidu_search_v2")
        self.assertIn("摘要：摘要正文", text)
        self.assertIn("URL：https://a.test/x", text)

    def test_reports_api_error(self):
        configure(SearchSettings(api_key="test-key", base_url="https://qianfan.baidubce.com", source=Path(".")))

        class Response:
            status_code = 401
            text = "no"

            def json(self):
                return {"code": 216003, "message": "Authentication error"}

        import core.search as search_mod
        original = search_mod.requests.post
        search_mod.requests.post = lambda *args, **kwargs: Response()
        try:
            text = searchWeb("天气")
        finally:
            search_mod.requests.post = original
        self.assertIn("HTTP 401", text)
        self.assertIn("错误码 216003", text)
        self.assertIn("Authentication error", text)


class ConfigTests(unittest.TestCase):
    def test_search_key_comes_from_baidu_qianfan_api_key(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.ini"
            path.write_text(
                "[llm]\n"
                "api_key = llm-key\n"
                "base_url = https://api.deepseek.com/v1\n"
                "model = deepseek-chat\n"
                "\n"
                "[search]\n"
                "baidu_qianfan_api_key = qf-test-key\n"
                "base_url = https://qianfan.baidubce.com\n",
                encoding="utf-8",
            )
            settings = load_search_settings(path)
            self.assertEqual(settings.api_key, "qf-test-key")
            self.assertNotEqual(settings.api_key, "llm-key")
            self.assertEqual(settings.problem, "")

    def test_empty_search_key_explains_the_item(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "config.ini"
            path.write_text("[search]\nbaidu_qianfan_api_key =\n", encoding="utf-8")
            settings = load_search_settings(path)
            self.assertIn("baidu_qianfan_api_key 为空", settings.problem)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_settings(Path("Z:/no/such-config.ini"))


class SkillDiscoveryTests(unittest.TestCase):
    def test_registers_name_and_description_without_body(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = root / ".agents" / "skills" / "leap-year"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\n"
                "name: leap-year\n"
                "description: >-\n"
                "  判断输入的年份\n"
                "  是否为闰年\n"
                "---\n"
                "# 正文不应被加载\n"
                "name: other\n"
                "这里是完整说明\n",
                encoding="utf-8",
            )
            (root / ".agents" / "skills" / "notes.txt").write_text("不是目录", encoding="utf-8")
            (root / ".agents" / "skills" / "empty-dir").mkdir()
            bad = root / ".agents" / "skills" / "bad-name"
            bad.mkdir()
            (bad / "SKILL.md").write_text("---\nname: wrong\ndescription: 名称和目录不一致\n---\n", encoding="utf-8")
            missing = root / ".agents" / "skills" / "no-description"
            missing.mkdir()
            (missing / "SKILL.md").write_text("---\nname: no-description\n---\n正文\n", encoding="utf-8")

            registry = discover_skills(root)

        self.assertEqual([item.name for item in registry], ["leap-year"])
        info = registry.get("leap-year")
        self.assertEqual(info.description, "判断输入的年份 是否为闰年")
        self.assertNotIn("完整说明", info.description)
        self.assertEqual(info.path, skill)
        self.assertIsNone(registry.get("wrong"))

    def test_missing_skills_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as raw:
            self.assertEqual(len(discover_skills(Path(raw))), 0)

    def test_default_root_is_startup_working_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = root / ".agents" / "skills" / "cwd-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: cwd-skill\ndescription: 来自工作目录\n---\n正文\n",
                encoding="utf-8",
            )
            previous = Path.cwd()
            os.chdir(root)
            try:
                registry = discover_skills()
            finally:
                os.chdir(previous)
        self.assertEqual(registry.get("cwd-skill").description, "来自工作目录")

    def test_session_uses_project_root_from_startup(self):
        import core.search as search_mod

        previous = search_mod._settings
        self.addCleanup(setattr, search_mod, "_settings", previous)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = root / ".agents" / "skills" / "startup-skill"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: startup-skill\ndescription: 启动时注册\n---\n不要读取\n",
                encoding="utf-8",
            )
            settings = LlmSettings("k", "https://example.com/v1", "m", root / "missing.ini")
            session = create_session(settings, root)
        self.assertEqual(session.skills.get("startup-skill").description, "启动时注册")

    def test_prompt_lists_name_and_description(self):
        registry = SkillRegistry((
            SkillInfo("skillname1", "Funtions that skill1 have.", Path("skillname1")),
            SkillInfo("skillname2", "Funtions that skill2 have.", Path("skillname2")),
        ))
        text = skill_prompt(registry)
        self.assertEqual(
            text,
            "You have access to the following skills:\n"
            "-'skillname1':Funtions that skill1 have.\n"
            "-'skillname2':Funtions that skill2 have.\n"
            "Use the 'loadSkill' tool to load the full instructions of a skill when needed."
            "You can also load additional files referenced by a skill.",
        )
        self.assertEqual(skill_prompt(SkillRegistry(())), "")

    def test_system_prompt_is_sent_with_every_model_call(self):
        registry = SkillRegistry((
            SkillInfo("leap-year", "判断输入的年份是否为闰年", Path("leap-year")),
        ))
        tool_call = call("getCwd")
        client = ScriptedClient([
            [LlmText("先看"), LlmDone("先看", [tool_call])],
            [LlmText("好了"), LlmDone("好了", [])],
        ])
        session = AgentSession(client, tool_names=("getCwd",), skills=registry)
        list(session.submit("今年是不是闰年"))
        for sent in client.seen:
            self.assertEqual(sent[0]["role"], "system")
            self.assertIn("-'leap-year':判断输入的年份是否为闰年", sent[0]["content"])
            self.assertIn("loadSkill", sent[0]["content"])
            self.assertEqual(sent[1]["role"], "user")
        self.assertEqual(session.messages[0]["role"], "user")


def _sample_skill(root: Path) -> Path:
    skill = root / ".agents" / "skills" / "leap-year"
    (skill / "scripts").mkdir(parents=True)
    (skill / "other").mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: leap-year\ndescription: 判断闰年\n---\n完整步骤：先看年份\n",
        encoding="utf-8",
    )
    (skill / "readme.txt").write_text("根上的说明", encoding="utf-8")
    (skill / "scripts" / "readme.txt").write_text("嵌套说明", encoding="utf-8")
    (skill / "scripts" / "rule.txt").write_text("四年一闰", encoding="utf-8")
    (skill / "scripts" / "note.txt").write_text("脚本说明", encoding="utf-8")
    (skill / "scripts" / "guide.txt").write_text("唯一指南", encoding="utf-8")
    (skill / "other" / "note.txt").write_text("另一份说明", encoding="utf-8")
    (root / "outside.txt").write_text("项目外的秘密", encoding="utf-8")
    return skill


class LoadSkillTests(unittest.TestCase):
    def test_name_reads_full_skill_and_filename_reads_only_that_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = _sample_skill(root)
            bind_skills(discover_skills(root))
            full = loadSkill("leap-year")
            extra = loadSkill("leap-year", "scripts/rule.txt")
            self.assertIn("完整步骤：先看年份", full)
            self.assertIn("description: 判断闰年", full)
            self.assertEqual(extra, "四年一闰")
            self.assertNotIn("完整步骤", extra)
            self.assertEqual(loadSkill("leap-year", str(skill / "scripts" / "rule.txt")), "四年一闰")
            self.assertEqual(
                loadSkill("leap-year", ".agents/skills/leap-year/scripts/rule.txt"),
                "四年一闰",
            )

    def test_exact_filename_wins_over_nested_search(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _sample_skill(root)
            bind_skills(discover_skills(root))
            self.assertEqual(loadSkill("leap-year", "readme.txt"), "根上的说明")
            self.assertEqual(loadSkill("leap-year", "guide.txt"), "唯一指南")

    def test_multiple_matches_return_candidates_without_contents(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _sample_skill(root)
            bind_skills(discover_skills(root))
            text = loadSkill("leap-year", "note.txt")
        self.assertIn("scripts/note.txt", text)
        self.assertIn("other/note.txt", text)
        self.assertNotIn("脚本说明", text)
        self.assertNotIn("另一份说明", text)

    def test_rejects_paths_outside_the_skill_directory(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _sample_skill(root)
            bind_skills(discover_skills(root))
            outside = loadSkill("leap-year", str(root / "outside.txt"))
            escaped = loadSkill("leap-year", "../outside.txt")
            missing = loadSkill("leap-year", "scripts/missing.txt")
        self.assertEqual(outside, "只能读取该 Skill 目录内的文件")
        self.assertNotIn("秘密", outside)
        self.assertEqual(escaped, "只能读取该 Skill 目录内的文件")
        self.assertEqual(missing, "未找到文件: scripts/missing.txt")
        self.assertEqual(loadSkill("missing"), "未找到 Skill: missing")
        self.assertEqual(loadSkill(" "), "name 不能为空")

    def test_tool_result_enters_context_and_existing_tools_still_run(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            _sample_skill(root)
            registry = discover_skills(root)
            client = ScriptedClient([
                [LlmDone("", [{
                    "id": "c1",
                    "type": "function",
                    "function": {"name": "loadSkill", "arguments": json.dumps({"name": "leap-year"})},
                }])],
                [LlmDone("", [call("getCwd")])],
                [LlmText("完成"), LlmDone("完成", [])],
            ])
            session = AgentSession(client, skills=registry)
            events = list(session.submit("按技能处理"))
            followup = run_registered_tool(call(
                "loadSkill",
                json.dumps({"name": "leap-year", "filename": "scripts/rule.txt"}),
            ))
        self.assertNotIn("完整步骤", client.seen[0][0]["content"])
        loaded = next(item["content"] for item in client.seen[1] if item["role"] == "tool")
        self.assertIn("完整步骤：先看年份", loaded)
        started = [event.name for event in events if isinstance(event, ToolStarted)]
        self.assertEqual(started, ["loadSkill", "getCwd"])
        self.assertEqual(followup, "四年一闰")


class CreateSkillTests(unittest.TestCase):
    def test_writes_skill_md_and_registers_it(self):
        content = (
            "---\n"
            'name: "hello-skill"\n'
            "description: >-\n"
            "  向用户问好\n"
            "  并说明来源\n"
            "---\n"
            "# 问好\n"
            "\n"
            "先说你好。\n"
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bind_skills(SkillRegistry((), root))
            created = createSkill(" hello-skill ", content)
            skill_file = root / ".agents" / "skills" / "hello-skill" / "SKILL.md"
            self.assertIn("已创建 Skill: hello-skill", created)
            self.assertEqual(skill_file.read_text(encoding="utf-8"), content)
            self.assertFalse((skill_file.parent / "scripts").exists())
            self.assertFalse((skill_file.parent / "references").exists())
            found = discover_skills(root)
            self.assertEqual(found.get("hello-skill").description, "向用户问好 并说明来源")
            self.assertIn("先说你好。", loadSkill("hello-skill"))

    def test_refuses_to_overwrite_an_existing_skill(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            skill = root / ".agents" / "skills" / "hello-skill"
            skill.mkdir(parents=True)
            original = "---\nname: hello-skill\ndescription: 原来的\n---\n旧正文\n"
            (skill / "SKILL.md").write_text(original, encoding="utf-8")
            bind_skills(SkillRegistry((), root))
            result = createSkill("hello-skill", "---\nname: hello-skill\ndescription: 新的\n---\n新正文\n")
            self.assertIn("Skill 已存在: hello-skill", result)
            self.assertIn("未覆盖", result)
            self.assertEqual((skill / "SKILL.md").read_text(encoding="utf-8"), original)

            empty = root / ".agents" / "skills" / "empty-skill"
            empty.mkdir()
            occupied = createSkill("empty-skill", "---\nname: empty-skill\ndescription: 占位\n---\n正文\n")
            self.assertIn("未覆盖", occupied)
            self.assertFalse((empty / "SKILL.md").exists())

    def test_rejects_invalid_name_and_document_without_creating_files(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            bind_skills(SkillRegistry((), root))
            valid = "---\nname: hello-skill\ndescription: 说明\n---\n正文\n"
            for bad_name in ("", "Hello", "my_skill", "-bad", "bad-", "中文技能", "a/b", "a" * 65):
                self.assertNotIn("已创建", createSkill(bad_name, valid.replace("hello-skill", bad_name or "hello-skill")))
            mismatch = "---\nname: other-skill\ndescription: 说明\n---\n正文\n"
            self.assertIn("必须是 hello-skill", createSkill("hello-skill", mismatch))
            self.assertIn("skillContext 不能为空", createSkill("hello-skill", "  "))
            self.assertIn("不符合 Agent Skills 规范", createSkill("hello-skill", "# 只有正文\n"))
            self.assertIn("缺少 description", createSkill("hello-skill", "---\nname: hello-skill\n---\n正文\n"))
            self.assertIn("缺少正文", createSkill("hello-skill", "---\nname: hello-skill\ndescription: 说明\n---\n"))
            long_description = "说" * 1025
            self.assertIn(
                "超过 1024",
                createSkill("hello-skill", f"---\nname: hello-skill\ndescription: {long_description}\n---\n正文\n"),
            )
            self.assertFalse((root / ".agents").exists())

    def test_same_turn_prompt_includes_the_new_skill(self):
        content = "---\nname: hello-skill\ndescription: 打招呼\n---\n先问好\n"
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            registry = SkillRegistry((), root)
            client = ScriptedClient([
                [LlmDone("", [call("createSkill", json.dumps({"name": "hello-skill", "skillContext": content}, ensure_ascii=False))])],
                [LlmText("好了"), LlmDone("好了", [])],
            ])
            session = AgentSession(client, skills=registry)
            list(session.submit("创建一个问好技能"))
            self.assertEqual(client.seen[0][0]["role"], "user")
            self.assertIn("-'hello-skill':打招呼", client.seen[1][0]["content"])
            self.assertEqual((root / ".agents" / "skills" / "hello-skill" / "SKILL.md").read_text(encoding="utf-8"), content)


class ContextArchiveTests(unittest.TestCase):
    def test_save_archive_and_recall(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            messages = [
                {"role": "user", "content": "请修改 core/session.py 里的提交逻辑"},
                {
                    "role": "assistant",
                    "content": "准备调用工具",
                    "tool_calls": [call("readFile", json.dumps({"path": "core/session.py"}))],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "def submit(self, user_text): ...",
                    "name": "readFile",
                },
            ]
            archive_id = save_archive(messages, root)
            path = root / ".agents" / "context" / f"{archive_id}.jsonl"
            self.assertTrue(path.is_file())
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 3)
            first = json.loads(lines[0])
            self.assertEqual(first["role"], "user")
            self.assertEqual(first["index"], 0)

            bind_context_root(root)
            hit = recallContext(text="session.py")
            self.assertIn(archive_id, hit)
            self.assertIn("core/session.py", hit)
            miss = recallContext(text="完全不存在的关键词xyz")
            self.assertIn("未找到", miss)
            scoped = recallContext(text="提交逻辑", id=archive_id)
            self.assertIn("提交逻辑", scoped)
            self.assertIn("参数错误", recallContext(text=""))

    def test_estimate_tokens_handles_cjk(self):
        self.assertGreater(estimate_tokens("你好世界"), 0)
        self.assertGreater(estimate_tokens("hello world"), 0)


class CompactSessionTests(unittest.TestCase):
    def test_compact_archives_and_replaces_history(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = ScriptedClient([
                [LlmText("# 摘要\n"), LlmDone("# 摘要\n继续实现 /compact", [])],
            ])
            session = AgentSession(
                client,
                tool_names=("getCwd",),
                tool_schemas=[],
                project_root=root,
                context_window=20_000,
            )
            session.messages = [
                {"role": "user", "content": "实现上下文压缩"},
                {"role": "assistant", "content": "先写 core/context.py"},
            ]
            session.refresh_token_usage()
            events = list(session.compact())
            self.assertTrue(any(isinstance(item, CompactFinished) for item in events))
            finished = next(item for item in events if isinstance(item, CompactFinished))
            archive = root / ".agents" / "context" / f"{finished.archive_id}.jsonl"
            self.assertTrue(archive.is_file())
            self.assertEqual(client.seen_kwargs[0]["use_tools"], False)
            self.assertEqual(len(session.messages), 2)
            self.assertIn("[上下文压缩摘要]", session.messages[0]["content"])
            self.assertIn(finished.archive_id, session.messages[0]["content"])
            self.assertIn("recallContext", session.messages[0]["content"])
            self.assertFalse(session.input_blocked)

    def test_blocks_input_at_eighty_percent_and_allows_compact(self):
        client = ScriptedClient([
            [LlmDone("很长的回答" * 200, [])],
            [LlmDone("# 压缩后的摘要\n下一步继续", [])],
        ])
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = AgentSession(
                client,
                tool_names=("getCwd",),
                tool_schemas=[],
                project_root=root,
                context_window=400,
            )
            events = list(session.submit("请详细说明" * 40))
            self.assertTrue(any(isinstance(item, ContextWarning) for item in events))
            self.assertTrue(session.input_blocked)
            blocked = list(session.submit("再问一句"))
            self.assertTrue(any(isinstance(item, AgentError) for item in blocked))
            self.assertIn("/compact", blocked[0].message)
            compact_events = list(session.compact())
            self.assertTrue(any(isinstance(item, CompactFinished) for item in compact_events))
            self.assertFalse(session.input_blocked)

    def test_compact_keeps_history_when_model_fails(self):
        class BoomClient:
            settings = None

            def stream(self, messages, *, use_tools=True):
                raise RuntimeError("模型挂了")

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            session = AgentSession(BoomClient(), tool_names=("getCwd",), project_root=root)
            original = [{"role": "user", "content": "保留我"}]
            session.messages = list(original)
            events = list(session.compact())
            self.assertTrue(any(isinstance(item, AgentError) for item in events))
            self.assertEqual(session.messages, original)
            archives = list((root / ".agents" / "context").glob("*.jsonl"))
            self.assertEqual(len(archives), 1)

    def test_default_context_window_is_128k(self):
        session = AgentSession(ScriptedClient([]), tool_names=("getCwd",))
        self.assertEqual(session.context_window, DEFAULT_CONTEXT_WINDOW)
        self.assertTrue(session.context_window_is_default)

    def test_archive_note_mentions_recall_tool(self):
        note = archive_note("20261022-153000")
        self.assertIn("20261022-153000", note)
        self.assertIn("recallContext", note)

    def test_recall_context_tool_is_registered(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            archive_id = save_archive(
                [{"role": "user", "content": "查找关键词 alpha-beta"}],
                root,
            )
            bind_context_root(root)
            result = run_registered_tool(call(
                "recallContext",
                json.dumps({"text": "alpha-beta", "id": archive_id}, ensure_ascii=False),
            ))
            self.assertIn("alpha-beta", result)
            self.assertIn(archive_id, result)


class EnsureConfigTests(unittest.TestCase):
    def test_creates_mock_config_in_cwd(self):
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            os.chdir(root)
            try:
                self.assertFalse((root / "config.ini").exists())
                settings, created = ensure_config()
            finally:
                os.chdir(previous)
            self.assertTrue(created)
            self.assertTrue((root / "config.ini").is_file())
            self.assertEqual(settings.model, "deepseek-chat")
            self.assertIn("sk-your-key", settings.api_key)

    def test_limits_come_from_config(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "config.ini"
            write_mock_config(path)
            text = path.read_text(encoding="utf-8")
            text = text.replace("max_model_calls = 8", "max_model_calls = 3")
            path.write_text(text, encoding="utf-8")
            load_settings(path)
            self.assertEqual(get_limits().max_model_calls, 3)
            set_limits(RuntimeLimits())


class SessionStoreTests(unittest.TestCase):
    def test_save_list_and_load(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = SessionRecord(
                id="20260101-120000",
                title="测试会话",
                created_at="2026-01-01T12:00:00",
                updated_at="2026-01-01T12:01:00",
                messages=[{"role": "user", "content": "你好"}],
                events=[{"type": "agents_md", "path": "AGENTS.md"}],
                agents_md_injected=True,
            )
            path = save_session(record, root)
            self.assertTrue(path.is_file())
            listed = list_sessions(root)
            self.assertEqual(len(listed), 1)
            loaded = load_session("20260101-120000", root)
            self.assertEqual(loaded.title, "测试会话")
            self.assertTrue(loaded.agents_md_injected)
            self.assertEqual(derive_title(loaded.messages), "你好")


class AgentsMdTests(unittest.TestCase):
    def test_injects_once_into_message_history(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "AGENTS.md").write_text("# 规则\n始终使用中文。\n", encoding="utf-8")
            client = ScriptedClient([])
            session = AgentSession(client, tool_names=("getCwd",), tool_schemas=[], project_root=root)
            self.assertTrue(session.inject_agents_md() or session.agents_md_injected)
            # create path already injects in some flows; ensure idempotent
            before = list(session.messages)
            self.assertFalse(session.inject_agents_md())
            self.assertEqual(session.messages, before)
            self.assertTrue(session.messages[0]["content"].startswith("[AGENTS.md]"))
            self.assertTrue(any(item.get("type") == "agents_md" for item in session.events))

    def test_create_session_injects_agents_md(self):
        import core.search as search_mod

        previous = search_mod._settings
        self.addCleanup(setattr, search_mod, "_settings", previous)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "AGENTS.md").write_text("用 pytest 验证。\n", encoding="utf-8")
            settings = LlmSettings("k", "https://example.com/v1", "m", root / "missing.ini")
            session = create_session(settings, root, mcp_mode="off")
            self.assertTrue(session.agents_md_injected)
            self.assertIn("用 pytest 验证", session.messages[0]["content"])


class SlashCommandTests(unittest.TestCase):
    def test_match_prefix(self):
        names = [item.name for item in match_commands("/re")]
        self.assertIn("/resume", names)
        self.assertTrue(all(name.startswith("/re") for name in names))


class BoundaryTests(unittest.TestCase):
    def test_core_source_does_not_import_frontends(self):
        for path in (ROOT / "core").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("import textual", text)
            self.assertNotIn("import tui", text)
            self.assertNotIn("import web", text)


if __name__ == "__main__":
    unittest.main()
