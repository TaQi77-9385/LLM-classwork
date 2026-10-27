"""启动入口。按 --ui 加载并列的前端目录。"""

import argparse
import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import ConfigError, load_settings
from core.session import create_session

# 每个前端是与 core、tui 并列的目录，并提供 run(session, model_name)。
FRONTENDS = {
    "tui": "tui.app",
    "web": "web.app",
    "desktop": "desktop.app",
    "mobile": "mobile.app",
}


def launch_ui(name: str, session, model_name: str) -> int:
    try:
        module = importlib.import_module(FRONTENDS[name])
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing == "textual":
            print("缺少 textual。请在 TQAgent 目录执行: pip install -r requirements.txt", file=sys.stderr)
            return 2
        if missing == name or missing.startswith(name + "."):
            print(f"{name} 界面还没有实现。当前可用：tui", file=sys.stderr)
            return 2
        raise
    module.run(session, model_name)
    return 0


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    parser = argparse.ArgumentParser(prog="tqagent", description="本地文件操作 Agent")
    parser.add_argument("--ui", choices=tuple(FRONTENDS), default="tui", help="界面。目前实现了 tui")
    parser.add_argument("--config", type=Path, default=None, help="config.ini 路径")
    args = parser.parse_args(argv)
    try:
        importlib.import_module(FRONTENDS[args.ui])
    except ModuleNotFoundError:
        return launch_ui(args.ui, None, "")
    try:
        settings = load_settings(args.config)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    return launch_ui(args.ui, create_session(settings), settings.model)


if __name__ == "__main__":
    raise SystemExit(main())
