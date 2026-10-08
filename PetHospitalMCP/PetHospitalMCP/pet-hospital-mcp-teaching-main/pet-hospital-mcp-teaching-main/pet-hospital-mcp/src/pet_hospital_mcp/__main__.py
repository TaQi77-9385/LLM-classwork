"""``python -m pet_hospital_mcp`` — start the MCP server.

Reads settings from the environment, configures JSON logging, then
starts the SDK 2.x ``MCPServer`` in **stateless Streamable HTTP** mode
on the configured host/port. The protocol version is 2026-07-28.

This is the long-running entry point used by the ``pet-hospital-mcp``
console script declared in ``pyproject.toml``.
"""

from __future__ import annotations

import logging
import sys

from .config import load_settings
from .logging_config import configure_logging
from .server import build_app


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    log = logging.getLogger("pet_hospital_mcp.__main__")

    # Build the Starlette app now (stateless by default) so that running
    # ``uvicorn pet_hospital_mcp.server:app`` and ``python -m
    # pet_hospital_mcp`` produce the same behaviour.
    app = build_app(settings=settings, stateless=True)

    import uvicorn

    log.info(
        "starting pet-hospital-mcp",
        extra={
            "host": settings.mcp_host,
            "port": settings.mcp_port,
            "mcp_path": settings.mcp_path,
            "backend": settings.pet_hospital_base_url,
            "protocol_version": "2026-07-28",
        },
    )

    # ``mcp_path`` is the SDK's ``streamable_http_path``. We pass it through
    # to ``streamable_http_app`` via ``build_app``; here we only need to
    # tell uvicorn about the host app. The mount path inside
    # ``build_app`` is ``/`` so the MCP endpoint is at ``mcp_path``.
    config = uvicorn.Config(
        app,
        host=settings.mcp_host,
        port=settings.mcp_port,
        log_level=settings.log_level.lower(),
        access_log=False,  # we have our own JSON logging
    )
    server = uvicorn.Server(config)
    try:
        server.run()
    except KeyboardInterrupt:
        log.info("received keyboard interrupt, shutting down")
        return 0
    except Exception as exc:  # pragma: no cover - last-resort logging
        log.exception("uvicorn server crashed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
