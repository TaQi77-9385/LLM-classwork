import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.events import TextDelta, TurnFinished

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

    def __init__(self):
        self.calls = []

    def submit(self, text):
        self.calls.append(text)
        self.messages.append({"role": "user", "content": text})
        yield TextDelta("收到" + text)
        yield TurnFinished("收到" + text)

    def reset(self):
        self.messages.clear()


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


if __name__ == "__main__":
    unittest.main()
