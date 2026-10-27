"""与界面无关的 Agent 核心。

前端只依赖本包发出的事件，不直接调用文件工具或模型接口。
新增界面时，在 TQAgent 下并列增加目录（tui、web、desktop、mobile），
并提供 run(session, model_name)。
"""

__all__ = ["create_session", "load_settings"]


def create_session(settings):
    from core.session import create_session as _create

    return _create(settings)


def load_settings(path=None):
    from core.config import load_settings as _load

    return _load(path)
