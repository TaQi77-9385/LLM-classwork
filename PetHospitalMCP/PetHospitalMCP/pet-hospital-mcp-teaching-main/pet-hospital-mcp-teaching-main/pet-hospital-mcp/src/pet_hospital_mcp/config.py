"""Configuration for the Pet Hospital MCP server.

All settings are read from environment variables, with safe defaults
suitable for a local teaching deployment on 127.0.0.1.

Environment variables
---------------------
MCP_HOST
    Hostname to bind the MCP Streamable HTTP server to. Default ``127.0.0.1``.
MCP_PORT
    TCP port for the MCP server. Default ``8000``.
PET_HOSPITAL_BASE_URL
    Base URL of the upstream Go Pet Hospital REST API. Default
    ``http://127.0.0.1:8080``. The MCP server never modifies the Go
    service, only calls it over HTTP.
PET_HOSPITAL_TIMEOUT_SECONDS
    Per-request timeout for upstream REST calls. Default ``10.0``.
PET_HOSPITAL_MAX_RETRIES
    Number of retries for transient upstream failures (timeouts and 5xx).
    Default ``2``.
PET_HOSPITAL_BACKOFF_SECONDS
    Base (initial) backoff in seconds for retry; exponential growth is
    ``backoff * 2 ** attempt``. Default ``0.25``.
PET_HOSPITAL_MCP_PATH
    Path of the Streamable HTTP MCP endpoint. Default ``/mcp``.
LOG_LEVEL
    Standard logging level. Default ``INFO``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@dataclass(frozen=True, slots=True)
class Settings:
    """Immutable snapshot of the server configuration."""

    mcp_host: str
    mcp_port: int
    mcp_path: str
    pet_hospital_base_url: str
    backend_timeout_seconds: float
    backend_max_retries: int
    backend_backoff_seconds: float
    log_level: str

    def with_overrides(self, **overrides: object) -> "Settings":
        """Return a new ``Settings`` with the given fields replaced.

        Useful in tests to inject overrides without touching the process
        environment.
        """

        replaced = {
            "mcp_host": self.mcp_host,
            "mcp_port": self.mcp_port,
            "mcp_path": self.mcp_path,
            "pet_hospital_base_url": self.pet_hospital_base_url,
            "backend_timeout_seconds": self.backend_timeout_seconds,
            "backend_max_retries": self.backend_max_retries,
            "backend_backoff_seconds": self.backend_backoff_seconds,
            "log_level": self.log_level,
        }
        replaced.update(overrides)
        return Settings(**replaced)  # type: ignore[arg-type]


def load_settings() -> Settings:
    """Read settings from the process environment at call time."""

    return Settings(
        mcp_host=_env_str("MCP_HOST", "127.0.0.1"),
        mcp_port=_env_int("MCP_PORT", 8000),
        mcp_path=_env_str("PET_HOSPITAL_MCP_PATH", "/mcp"),
        pet_hospital_base_url=_env_str(
            "PET_HOSPITAL_BASE_URL", "http://127.0.0.1:8080"
        ).rstrip("/"),
        backend_timeout_seconds=_env_float("PET_HOSPITAL_TIMEOUT_SECONDS", 10.0),
        backend_max_retries=_env_int("PET_HOSPITAL_MAX_RETRIES", 2),
        backend_backoff_seconds=_env_float("PET_HOSPITAL_BACKOFF_SECONDS", 0.25),
        log_level=_env_str("LOG_LEVEL", "INFO").upper(),
    )
