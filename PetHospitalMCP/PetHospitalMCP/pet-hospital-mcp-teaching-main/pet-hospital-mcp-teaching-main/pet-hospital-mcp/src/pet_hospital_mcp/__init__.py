"""Pet Hospital MCP — SDK 2.x server exposing the Go Pet Hospital REST API.

Package layout:

* ``config``           — environment-driven configuration.
* ``errors``           — unified error envelope + ``ToolError`` exception.
* ``logging_config``   — JSON logging with recursive PII redaction.
* ``rest_client``      — async httpx wrapper around the Go REST API.
* ``server``           — the ``MCPServer`` instance, tool registration, ASGI app.
* ``tools``            — one module per MCP tool. Stage 1 ships only ``list_pets``.
"""

from __future__ import annotations

__all__: list[str] = ["__version__", "server"]

__version__ = "0.1.0"


def __getattr__(name: str):  # PEP 562: lazy import to avoid side effects at import time.
    if name == "server":
        # Use the absolute-import form so we do not re-enter this
        # ``__getattr__`` (``from . import server`` would call back into
        # ``_handle_fromlist`` and recurse).
        import pet_hospital_mcp.server as _server

        return _server
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
