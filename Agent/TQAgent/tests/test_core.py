import json
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
from core.config import ConfigError, SearchSettings, load_search_settings, load_settings
from core.fetch import HtmlParseError, fetchWeb
from core.events import TextDelta, ToolFinished, ToolStarted, TurnFinished
from core.files import run_tool
from core.llm import LlmDone, LlmText, StreamState, feed
from core.registry import run_tool as run_registered_tool
from core.search import configure, format_references, searchWeb
from core.session import AgentSession


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

    def stream(self, messages):
        self.seen.append(list(messages))
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
        self.assertIn(session.messages[2]["content"], events[2].result)

    def test_catalog_includes_search_web(self):
        session = AgentSession(ScriptedClient([]))
        names = [name for name, _ in session.tool_catalog]
        self.assertIn("searchWeb", names)
        self.assertIn("fetchWeb", names)
        self.assertIn("RunCli", names)
        self.assertEqual(len(names), 13)


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


class BoundaryTests(unittest.TestCase):
    def test_core_source_does_not_import_frontends(self):
        for path in (ROOT / "core").rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("import textual", text)
            self.assertNotIn("import tui", text)
            self.assertNotIn("import web", text)


if __name__ == "__main__":
    unittest.main()
