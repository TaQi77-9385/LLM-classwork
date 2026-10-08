"""JSON logging configuration with recursive PII redaction.

This module standardises on the Python standard library ``logging``
package plus a small ``json``-formatted handler so that logs are easy to
ingest and never leak sensitive data.

Redacted fields (matched recursively, case-insensitively, also in
snake_case form):

* ``ownerPhone`` / ``owner_phone``
* ``ownerAddr``  / ``owner_addr``
* ``chipNo``     / ``chip_no``

Each is replaced with the literal string ``"<REDACTED>"``. The redactor
walks dicts and lists recursively so nested models (e.g. a pet payload
embedded in a log record's ``params`` field) are sanitised in place.

Required log fields
-------------------
Every tool-call record carries:

* ``timestamp``      ISO-8601, UTC.
* ``tool_name``      Name of the MCP tool, e.g. ``list_pets``.
* ``params``          Redacted input parameters of the call.
* ``status``         ``ok`` or ``error``.
* ``duration_ms``    Wall-clock duration of the tool call in milliseconds.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Iterable, Mapping

# ---------------------------------------------------------------------------
# PII redaction
# ---------------------------------------------------------------------------

# Keys are matched case-insensitively. Both camelCase and snake_case
# variants are listed explicitly so the matcher is O(1) per key.
_SENSITIVE_KEYS: frozenset[str] = frozenset(
    {
        "ownerphone",
        "owner_phone",
        "owneraddr",
        "owner_addr",
        "chipno",
        "chip_no",
    }
)

_REDACTED = "<REDACTED>"


def _redact_value(value: Any) -> Any:
    """Recursively redact sensitive keys in ``value``.

    Returns a new structure when ``value`` is a container; primitives are
    returned unchanged.
    """

    if isinstance(value, Mapping):
        return {
            str(k): _REDACTED if _is_sensitive(k) else _redact_value(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


def _is_sensitive(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    return key.lower() in _SENSITIVE_KEYS


def redact(payload: Any) -> Any:
    """Public entry point: return a redacted deep copy of ``payload``."""

    return _redact_value(payload)


# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Format ``LogRecord`` as a single-line JSON object.

    The base ``LogRecord`` fields (``name``, ``level``, ``msg``) are
    promoted to top-level keys, and any ``extra`` attributes attached via
    ``logger.info(..., extra={...})`` are merged in. Sensitive fields
    are redacted before serialisation.
    """

    # Standard fields that should not also appear under "extra".
    _RESERVED: frozenset[str] = frozenset(
        {
            "name",
            "msg",
            "args",
            "levelname",
            "levelno",
            "pathname",
            "filename",
            "module",
            "exc_info",
            "exc_text",
            "stack_info",
            "lineno",
            "funcName",
            "created",
            "msecs",
            "relativeCreated",
            "thread",
            "threadName",
            "processName",
            "process",
            "message",
            "timestamp",
            "tool_name",
            "params",
            "status",
            "duration_ms",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        # Render the message via the base Formatter so %-substitution works.
        message = record.getMessage()
        payload: dict[str, Any] = {
            "timestamp": getattr(record, "timestamp", None)
            or datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }

        # Promote known structured fields (added via ``extra=``).
        for field in ("tool_name", "params", "status", "duration_ms"):
            value = getattr(record, field, None)
            if value is None:
                continue
            payload[field] = redact(value)

        # Carry any other extras through, redacted.
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = redact(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


def configure_logging(level: str | int = "INFO") -> logging.Logger:
    """Configure the root logger to emit JSON to stderr.

    Idempotent: calling it more than once replaces the existing handler
    rather than stacking them.
    """

    root = logging.getLogger()
    # Remove existing handlers we previously installed (identified by
    # ``__name__`` on the handler) to keep ``configure_logging`` idempotent.
    for handler in list(root.handlers):
        if getattr(handler, "_pet_hospital_mcp", False):
            root.removeHandler(handler)

    handler = logging.StreamHandler(stream=sys.stderr)
    handler._pet_hospital_mcp = True  # type: ignore[attr-defined]
    handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    return root


def make_tool_record(
    logger: logging.Logger,
    *,
    level: int,
    tool_name: str,
    params: Mapping[str, Any],
    status: str,
    duration_ms: float,
    message: str = "",
) -> None:
    """Emit a single tool-call log record with the required fields."""

    logger.log(
        level,
        message or f"tool={tool_name} status={status} duration_ms={duration_ms}",
        extra={
            "tool_name": tool_name,
            "params": dict(params),
            "status": status,
            "duration_ms": round(duration_ms, 3),
        },
    )


__all__ = [
    "JsonFormatter",
    "configure_logging",
    "make_tool_record",
    "redact",
]
