import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.events import TextDelta, TurnFinished
from core.store import SessionRecord

try:
    from tui.app import TQAgentApp
except ModuleNotFoundError as exc:
    TQAgentApp = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


class FakeSession:
    tool_names = ("getCwd", "listDir")
    tool_catalog = (
        ("getCwd", "获取当前工作目录"),
        ("listDir", "列出指定目录下的所有文件和子目录"),
    )
    base_url = "https://api.deepseek.com/v1"
    max_calls = 8
    messages: list = []
    used_tokens = 1200
    context_window = 128_000
    context_window_is_default = True
    input_blocked = False
    mcp_status = "ready"
    mcp_servers = []
    mcp_errors = ()
    session_id = "fake-session"
    agents_md_injected = False
    config_path = "config.ini"
    warn_ratio = 0.80
    events: list = []

    def __init__(self):
        self.calls = []
        self.compact_calls = 0
        self.saved = []
        self._records = []

    def submit(self, text):
        self.calls.append(text)
        self.messages.append({"role": "user", "content": text})
        self.used_tokens += 100
        yield TextDelta("收到" + text)
        yield TurnFinished("收到" + text)

    def compact(self):
        self.compact_calls += 1
        self.used_tokens = 500
        self.input_blocked = False
        self.messages = [{"role": "user", "content": "[上下文压缩摘要]\n摘要"}]
        yield TurnFinished("摘要")

    def save(self, *, rounds=0, tool_calls=0):
        record = SessionRecord(
            id=self.session_id,
            title="saved",
            created_at="t0",
            updated_at="t1",
            messages=list(self.messages),
            rounds=rounds,
            tool_calls=tool_calls,
        )
        self.saved.append(record)
        self._records = [record]
        return Path("fake-save.json")

    def list_saved(self):
        return list(self._records)

    def restore(self, session_id: str):
        for record in self._records:
            if record.id == session_id:
                self.messages = list(record.messages)
                self.session_id = record.id
                return record
        raise FileNotFoundError(session_id)

    def reset(self, *, inject_agents=True):
        self.messages.clear()
        self.used_tokens = 0
        self.input_blocked = False
        self.session_id = "new-session"
        self.agents_md_injected = False

    def load_mcp(self):
        self.mcp_status = "ready"
        self.mcp_servers = [{
            "name": "demo",
            "status": "connected",
            "tools": [("remoteTool", "demo tool")],
            "error": "",
        }]


def _visible(app) -> str:
    parts = []
    for widget in app.query("Static"):
        content = widget.content
        parts.append(getattr(content, "plain", str(content)))
    return "\n".join(parts)


@unittest.skipIf(TQAgentApp is None, f"textual 未安装: {_IMPORT_ERROR}")
class TuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_renders_assistant_text(self):
        session = FakeSession()
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").focus()
            await pilot.press("a", "b", "enter")
            for _ in range(40):
                visible = _visible(app)
                status = str(app.query_one("#statusbar").content)
                if "收到ab" in visible and "就绪" in status:
                    break
                await pilot.pause(0.05)
            visible = _visible(app)
            self.assertIn("2 个工具", visible)
            self.assertIn("你", visible)
            self.assertIn("ab", visible)
            self.assertIn("收到ab", visible)
            self.assertIn("TQAgent", visible)
            self.assertIn("fake-model", visible)
            self.assertIn("getCwd", visible)
            self.assertEqual(session.calls, ["ab"])
            self.assertIn("就绪", str(app.query_one("#statusbar").content))

    async def test_slash_cwd_does_not_call_model(self):
        session = FakeSession()
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").focus()
            await pilot.press("/", "c", "w", "d", "enter")
            await pilot.pause()
            self.assertEqual(session.calls, [])
            self.assertIn(str(Path.cwd()), _visible(app))

    async def test_mcp_connection_error_is_shown(self):
        session = FakeSession()
        session.mcp_errors = ("MCP Server「Bazi-MCP」连接失败：拒绝连接",)
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            visible = _visible(app)
            self.assertIn("Bazi-MCP", visible)
            self.assertIn("拒绝连接", visible)

    async def test_banner_shows_context_usage_with_default_mark(self):
        session = FakeSession()
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            banner = str(app.query_one("#banner").content)
            self.assertIn("上下文", banner)
            self.assertIn("128k", banner)
            self.assertIn("默认", banner)
            status = str(app.query_one("#statusbar").content)
            self.assertIn("上下文", status)

    async def test_compact_command_runs_session_compact(self):
        session = FakeSession()
        session.input_blocked = True
        session.used_tokens = 110_000
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#prompt").focus()
            await pilot.press("/", "c", "o", "m", "p", "a", "c", "t", "enter")
            for _ in range(40):
                if session.compact_calls:
                    break
                await pilot.pause(0.05)
            self.assertEqual(session.compact_calls, 1)
            self.assertFalse(session.input_blocked)

    async def test_slash_panel_appears_for_prefix(self):
        session = FakeSession()
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            box = app.query_one("#prompt")
            box.focus()
            box.value = "/sa"
            box.cursor_position = len(box.value)
            app._update_slash_panel(box.value)
            panel = app.query_one("#slash-panel")
            self.assertFalse(panel.has_class("-hidden"))
            self.assertIn("/save", str(panel.content))

    async def test_sidebar_shows_mcp_section(self):
        session = FakeSession()
        session.mcp_servers = [{
            "name": "Bazi-MCP",
            "status": "connected",
            "tools": [("getBaziDetail", "八字详情")],
            "error": "",
        }]
        app = TQAgentApp(session, "fake-model")
        async with app.run_test() as pilot:
            await pilot.pause()
            visible = _visible(app)
            self.assertIn("MCP", visible)
            self.assertIn("Bazi-MCP", visible)
            self.assertIn("getBaziDetail", visible)


if __name__ == "__main__":
    unittest.main()
