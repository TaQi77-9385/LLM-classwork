import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import LlmSettings
from core.events import ToolFinished
from core.llm import LlmDone, LlmText
from core.mcp_client import (
    bind_mcp,
    connect_mcp,
    expose_name,
    format_tool_result,
    load_mcp_config,
)
from core.registry import TOOL_NAMES, TOOL_SCHEMAS, run_tool
from core.session import AgentSession, create_session
from core.skills import SkillRegistry

# 联调用例只依赖本地 .mcp.json；未配置或服务不可达时跳过，不把私有端点写进仓库。
TEST_HTTP_URL = "https://example.com/mcp"

SERVER_SOURCE = textwrap.dedent(
    '''
    import os
    import sys

    from mcp.server import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError

    mcp = MCPServer("demo")

    @mcp.tool()
    def add(a: int, b: int) -> int:
        """把两个整数相加。"""
        return a + b

    @mcp.tool()
    def getCwd() -> str:
        """应避免与本地工具重名。"""
        return "from-mcp"

    @mcp.tool()
    def where() -> str:
        """返回工作目录。"""
        return os.getcwd()

    @mcp.tool()
    def token() -> str:
        """返回令牌。"""
        return os.environ.get("TQ_MCP_TOKEN", "")

    @mcp.tool()
    def boom(message: str) -> str:
        """必定失败。"""
        raise ToolError(message)

    if __name__ == "__main__":
        if len(sys.argv) > 1 and sys.argv[1] == "--http":
            mcp.run(transport="streamable-http", host="127.0.0.1", port=int(sys.argv[2]))
        else:
            mcp.run(transport="stdio")
    '''
)


def call(name, arguments="{}"):
    return {"id": "call-1", "type": "function", "function": {"name": name, "arguments": arguments}}


class ScriptedClient:
    def __init__(self, rounds):
        self.rounds = rounds
        self.seen = []

    def stream(self, messages):
        self.seen.append(list(messages))
        yield from self.rounds[len(self.seen) - 1]


class McpConfigTests(unittest.TestCase):
    def test_streamable_http_alias_and_stdio_command(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / ".mcp.json"
            path.write_text(json.dumps({
                "mcpServers": {
                    "Bazi-MCP": {
                        "type": "streamable_http",
                        "url": TEST_HTTP_URL,
                    },
                    "local": {
                        "command": "python",
                        "args": ["server.py"],
                        "env": {"TQ_MCP_TOKEN": "${TQ_MCP_TOKEN}"},
                    },
                }
            }, ensure_ascii=False), encoding="utf-8")
            os.environ["TQ_MCP_TOKEN"] = "secret-token"
            specs, errors = load_mcp_config(path, root)
        self.assertEqual(errors, [])
        by_name = {item.name: item for item in specs}
        self.assertEqual(by_name["Bazi-MCP"].transport, "http")
        self.assertEqual(by_name["Bazi-MCP"].url, TEST_HTTP_URL)
        self.assertEqual(by_name["local"].transport, "stdio")
        self.assertEqual(by_name["local"].command, "python")
        self.assertEqual(by_name["local"].args, ("server.py",))
        self.assertEqual(dict(by_name["local"].env), {"TQ_MCP_TOKEN": "secret-token"})
        self.assertEqual(Path(by_name["local"].cwd), root.resolve())

    def test_url_without_type_is_streamable_http(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / ".mcp.json"
            path.write_text(json.dumps({
                "mcpServers": {"remote": {"url": "http://127.0.0.1:9/mcp"}}
            }), encoding="utf-8")
            specs, errors = load_mcp_config(path, root)
        self.assertEqual(errors, [])
        self.assertEqual(specs[0].transport, "http")

    def test_bad_entries_do_not_drop_valid_ones(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / ".mcp.json"
            path.write_text(json.dumps({
                "mcpServers": {
                    "old": {"type": "sse", "url": "http://127.0.0.1:9/sse"},
                    "both": {"command": "python", "url": "http://127.0.0.1:9/mcp"},
                    "local": {"type": "stdio", "command": "python", "args": "server.py"},
                    "remote": {"type": "streamable-http", "url": "http://127.0.0.1:9/mcp"},
                }
            }), encoding="utf-8")
            specs, errors = load_mcp_config(path, root)
        self.assertEqual([item.name for item in specs], ["remote"])
        self.assertEqual(len(errors), 3)
        self.assertTrue(any("SSE" in item for item in errors))
        self.assertTrue(any("同时写了 command 和 url" in item for item in errors))
        self.assertTrue(any("args 必须是字符串列表" in item for item in errors))

    def test_invalid_json_and_missing_servers_are_errors(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            broken = root / ".mcp.json"
            broken.write_text("{", encoding="utf-8")
            specs, errors = load_mcp_config(broken, root)
            self.assertEqual(specs, [])
            self.assertIn("不是合法 JSON", errors[0])
            broken.write_text("{}", encoding="utf-8")
            specs, errors = load_mcp_config(broken, root)
        self.assertEqual(specs, [])
        self.assertIn("缺少 mcpServers", errors[0])

    def test_missing_environment_variable_skips_that_server(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / ".mcp.json"
            path.write_text(json.dumps({
                "mcpServers": {
                    "remote": {
                        "type": "streamable_http",
                        "url": "http://127.0.0.1:9/mcp",
                        "headers": {"Authorization": "Bearer ${TQ_MCP_MISSING_TOKEN}"},
                    }
                }
            }), encoding="utf-8")
            os.environ.pop("TQ_MCP_MISSING_TOKEN", None)
            specs, errors = load_mcp_config(path, root)
        self.assertEqual(specs, [])
        self.assertIn("TQ_MCP_MISSING_TOKEN", errors[0])

    def test_names_are_prefixed_only_when_they_collide(self):
        taken = {"getCwd", "add"}
        self.assertEqual(expose_name("Bazi-MCP", "get_bazi", taken), "get_bazi")
        self.assertEqual(expose_name("Bazi-MCP", "getCwd", taken), "Bazi-MCP__getCwd")
        self.assertEqual(expose_name("calc2", "add", taken), "calc2__add")
        self.assertEqual(expose_name("Bazi MCP", "排盘", set()), "Bazi_MCP__mcp")
        self.assertTrue(expose_name("Bazi MCP", "排盘", {"Bazi_MCP__mcp"}).startswith("Bazi_MCP__mcp_"))

    def test_result_keeps_text_structured_data_and_errors(self):
        ok = format_tool_result(SimpleNamespace(
            is_error=False,
            content=[SimpleNamespace(type="text", text="5")],
            structured_content={"result": 5},
        ))
        self.assertIn("5", ok)
        self.assertIn("结构化数据", ok)
        self.assertIn('"result": 5', ok)
        failed = format_tool_result(SimpleNamespace(
            is_error=True,
            content=[SimpleNamespace(type="text", text="计算失败")],
            structured_content=None,
        ))
        self.assertEqual(failed, "MCP 工具执行失败：计算失败")
        image = format_tool_result(SimpleNamespace(
            is_error=False,
            content=[
                SimpleNamespace(type="text", text="图已生成"),
                SimpleNamespace(type="image", mime_type="image/png", data="NOT-IN-OUTPUT-BASE64"),
                SimpleNamespace(type="resource", resource=SimpleNamespace(text="正文", uri="file://a")),
            ],
            structured_content=None,
        ))
        self.assertIn("图已生成", image)
        self.assertIn("image/png", image)
        self.assertIn("正文", image)
        self.assertNotIn("NOT-IN-OUTPUT-BASE64", image)


class McpClientTests(unittest.TestCase):
    def setUp(self):
        self._token = os.environ.get("TQ_MCP_TOKEN")
        os.environ["TQ_MCP_TOKEN"] = "secret-token"

    def tearDown(self):
        bind_mcp_empty()
        if self._token is None:
            os.environ.pop("TQ_MCP_TOKEN", None)
        else:
            os.environ["TQ_MCP_TOKEN"] = self._token
        proc = getattr(self, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    def test_stdio_tools_join_function_calls_and_keep_local_tools(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            script = _write_server(root)
            (root / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "missing": {"command": "tqagent-mcp-not-installed"},
                    "calc": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(script)],
                        "env": {"TQ_MCP_TOKEN": "${TQ_MCP_TOKEN}"},
                    },
                }
            }, ensure_ascii=False), encoding="utf-8")
            captured = io.StringIO()
            with redirect_stderr(captured):
                hub = connect_mcp(root)
            try:
                self.assertTrue(any("missing" in item and "连接失败" in item for item in hub.errors))
                self.assertIn("连接失败", captured.getvalue())
                names = [item["function"]["name"] for item in hub.schemas]
                self.assertIn("add", names)
                self.assertIn("calc__getCwd", names)
                self.assertNotIn("getCwd", names)
                added = run_tool(call("add", json.dumps({"a": 2, "b": 3})), hub=hub)
                self.assertIn("5", added)
                self.assertIn("result", added)
                self.assertIn("from-mcp", run_tool(call("calc__getCwd"), hub=hub))
                where = run_tool(call("where"), hub=hub).splitlines()[0].strip()
                self.assertEqual(Path(where).resolve(), root.resolve())
                self.assertIn("secret-token", run_tool(call("token"), hub=hub))
                failed = run_tool(call("boom", json.dumps({"message": "计算失败"})), hub=hub)
                self.assertTrue(failed.startswith("MCP 工具执行失败："))
                self.assertIn("计算失败", failed)
                self.assertIn("参数不是合法 JSON", run_tool(call("add", "{"), hub=hub))
                self.assertIn("参数错误", run_tool(call("add", "[]"), hub=hub))
                local = run_tool(call("getCwd"), hub=hub)
                self.assertNotIn("from-mcp", local)
                self.assertTrue(local.strip())
                self.assertIn("RunCli", TOOL_NAMES)
                self.assertIn("loadSkill", TOOL_NAMES)
                self.assertIn("fetchWeb", TOOL_NAMES)
            finally:
                bind_mcp_empty()

    def test_streamable_http_headers_and_agent_loop(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as raw:
            root = Path(raw)
            script = _write_server(root)
            port = _free_port()
            log_path = root / "http.log"
            log = log_path.open("w", encoding="utf-8")
            self.proc = subprocess.Popen(
                [sys.executable, str(script), "--http", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=log,
                cwd=root,
            )
            try:
                _wait_port(port, self.proc, log_path)
                (root / ".mcp.json").write_text(json.dumps({
                    "mcpServers": {
                        "remote": {
                            "type": "streamable_http",
                            "url": f"http://127.0.0.1:{port}/mcp",
                            "headers": {"X-Test": "tqagent"},
                        }
                    }
                }), encoding="utf-8")
                hub = connect_mcp(root)
                self.assertEqual(hub.errors, [], hub.errors)
                client = ScriptedClient([
                    [LlmDone("", [call("add", json.dumps({"a": 20, "b": 22}))])],
                    [LlmText("是 42"), LlmDone("是 42", [])],
                ])
                schemas = [*TOOL_SCHEMAS, *hub.schemas]
                session = AgentSession(
                    client,
                    tool_runner=lambda item, hub=hub: run_tool(item, hub=hub),
                    skills=SkillRegistry(()),
                    tool_schemas=schemas,
                )
                events = list(session.submit("算 20+22"))
                finished = next(item for item in events if isinstance(item, ToolFinished))
                self.assertIn("42", finished.result)
                self.assertIn("42", client.seen[1][2]["content"])
                names = [name for name, _ in session.tool_catalog]
                self.assertIn("add", names)
                self.assertIn("RunCli", names)
                self.assertIn("loadSkill", names)
            finally:
                bind_mcp_empty()
                log.close()
                proc = self.proc
                self.proc = None
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()

    def test_empty_project_does_not_read_the_source_tree_config(self):
        import core.search as search_mod

        previous = search_mod._settings
        self.addCleanup(setattr, search_mod, "_settings", previous)
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = LlmSettings("k", "https://example.com/v1", "m", root / "missing.ini")
            session = create_session(settings, root)
        names = [name for name, _ in session.tool_catalog]
        self.assertEqual(len(names), len(TOOL_NAMES))
        self.assertIn("RunCli", names)
        self.assertIn("fetchWeb", names)
        self.assertIn("loadSkill", names)
        self.assertEqual(session.mcp_errors, ())

    def test_workspace_bazi_config_is_usable(self):
        import core.search as search_mod

        previous = search_mod._settings
        self.addCleanup(setattr, search_mod, "_settings", previous)
        config_path = ROOT / ".mcp.json"
        if not config_path.is_file():
            self.skipTest("本机没有 .mcp.json，跳过真实 MCP 服务联调")
        config = json.loads(config_path.read_text(encoding="utf-8"))
        entry = (config.get("mcpServers") or {}).get("Bazi-MCP") or {}
        if not str(entry.get("url") or "").strip():
            self.skipTest(".mcp.json 未配置 Bazi-MCP 端点")
        settings = LlmSettings("k", "https://example.com/v1", "m", ROOT / "missing.ini")
        session = create_session(settings, ROOT)
        if session.mcp_errors:
            self.skipTest("MCP 服务不可达：" + "；".join(session.mcp_errors))
        names = [name for name, _ in session.tool_catalog]
        remote = [name for name in names if name not in set(TOOL_NAMES)]
        self.assertTrue(remote, names)
        self.assertIn("RunCli", names)
        self.assertIn("fetchWeb", names)
        self.assertIn("loadSkill", names)
        self.assertIn("getBaziDetail", remote)
        self.assertIn("getSolarTimes", remote)
        self.assertIn("getChineseCalendar", remote)
        self.assertTrue(any("Bazi-MCP" in desc for _, desc in session.tool_catalog))
        calendar = session._run_tool(call(
            "getChineseCalendar",
            json.dumps({"solarDatetime": "2008-03-01T13:00:00+08:00"}, ensure_ascii=False),
        ))
        self.assertIn("农历", calendar)
        self.assertNotIn("MCP 工具执行失败", calendar)
        detail = session._run_tool(call(
            "getBaziDetail",
            json.dumps({"solarDatetime": "2008-03-01T13:00:00+08:00", "gender": 1}, ensure_ascii=False),
        ))
        self.assertIn("八字", detail)
        self.assertIn("戊子", detail)
        rejected = session._run_tool(call("getBaziDetail", json.dumps({"gender": 1})))
        self.assertTrue(rejected.startswith("MCP 工具执行失败："))
        self.assertIn("solarDatetime", rejected)
        local = run_tool(call("getCwd"))
        self.assertTrue(local.strip())
        self.assertNotIn("八字", local)


def bind_mcp_empty():
    from core.mcp_client import McpHub

    bind_mcp(McpHub())


def _write_server(root: Path) -> Path:
    path = root / "server.py"
    path.write_text(SERVER_SOURCE, encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_port(port: int, proc: subprocess.Popen, log_path: Path) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(_tail(log_path))
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise AssertionError(_tail(log_path))


def _tail(path: Path) -> str:
    if not path.is_file():
        return "HTTP MCP 没有开始监听"
    return path.read_text(encoding="utf-8", errors="replace")[-2000:]


if __name__ == "__main__":
    unittest.main()
