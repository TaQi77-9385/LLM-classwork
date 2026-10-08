"""Unified error envelope and ``ToolError`` exception.

The MCP server must surface a single, structured error shape to its clients
regardless of where a failure originates:

.. code-block:: json

    {
      "error": {
        "code": "ERROR_CODE",
        "message": "Human-readable summary",
        "details": {}
      }
    }

This module defines:

* The error-code string constants used across the project.
* ``ToolError``, an exception whose string form is the JSON-serialised
  envelope above. Raising it inside a tool causes the MCP Python SDK 2.x
  to mark the call result with ``is_error=True`` and to place the envelope
  JSON in ``content[0].text`` — exactly the SDK 2.x failure surface (the
  SDK 2.x attribute is snake_case ``is_error``; this project does not use
  the 1.x camelCase ``CallToolResult.isError`` spelling).
* ``format_error`` to build the envelope from raw components, used by
  callers that want the dict form instead of raising.

No HTTPX, Pydantic, or Python stack trace ever leaks through this layer:
the message string is controlled and the optional ``details`` payload is
serialised by the caller.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Error code constants
# ---------------------------------------------------------------------------

#: The caller sent invalid tool arguments (bad type, unknown field, value
#: out of range, conflicting filters, etc.). The model can correct and
#: retry.
VALIDATION_ERROR = "VALIDATION_ERROR"

#: The upstream Go REST API did not respond in time after the configured
#: retries.
BACKEND_TIMEOUT = "BACKEND_TIMEOUT"

#: The upstream Go REST API could not be reached at all (DNS failure,
#: connection refused, etc.).
BACKEND_UNAVAILABLE = "BACKEND_UNAVAILABLE"

#: The upstream Go REST API returned an HTTP error status (4xx or 5xx)
#: after retries were exhausted.
BACKEND_API_ERROR = "BACKEND_API_ERROR"

#: The upstream Go REST API returned a body that could not be parsed as
#: JSON or that did not match the expected data model.
BACKEND_INVALID_RESPONSE = "BACKEND_INVALID_RESPONSE"

#: An unexpected internal failure inside the MCP server. The original
#: exception is logged server-side but never exposed to the client.
INTERNAL_ERROR = "INTERNAL_ERROR"

#: Tuple of every public error code, for tests and validation.
ALL_ERROR_CODES: tuple[str, ...] = (
    VALIDATION_ERROR,
    BACKEND_TIMEOUT,
    BACKEND_UNAVAILABLE,
    BACKEND_API_ERROR,
    BACKEND_INVALID_RESPONSE,
    INTERNAL_ERROR,
)


# ---------------------------------------------------------------------------
# Envelope helpers
# ---------------------------------------------------------------------------


def format_error(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the unified error envelope as a plain dict.

    The envelope is intentionally narrow (only ``code``, ``message`` and
    ``details``) so clients can rely on the shape. ``details`` is optional
    and may carry machine-readable context such as the failing field name
    or the upstream HTTP status.
    """

    if code not in ALL_ERROR_CODES:
        # Defensive: developers adding a new code must register it above.
        raise ValueError(f"Unknown error code: {code!r}")

    envelope: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details) if details else {},
        }
    }
    return envelope


def serialise_error(
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> str:
    """Serialise the envelope to a single JSON string.

    This is what is placed in ``CallToolResult.content[0].text`` when a
    tool raises :class:`ToolError`.
    """

    return json.dumps(
        format_error(code, message, details),
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# ToolError exception
# ---------------------------------------------------------------------------


class ToolError(Exception):
    """Raised by tools to surface a structured error envelope to the client.

    Behaviour in the MCP Python SDK 2.x
    -------------------------------------
    When a tool raises any ``Exception`` that is not ``MCPError``, the SDK
    wraps it into a ``CallToolResult`` with ``is_error=True`` and puts
    ``str(exception)`` into ``content[0].text``. ``structured_content``
    is ``None`` on a failed call. By making ``ToolError.__str__`` return
    the JSON envelope we get both: the SDK 2.x ``is_error=True`` marker
    the spec requires, and the unified error shape this project promises.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if code not in ALL_ERROR_CODES:
            raise ValueError(f"Unknown error code: {code!r}")
        super().__init__(serialise_error(code, message, details))
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details) if details else {}

    def __str__(self) -> str:  # pragma: no cover - trivial, exercised by tests
        return serialise_error(self.code, self.message, self.details)


__all__ = [
    "VALIDATION_ERROR",
    "BACKEND_TIMEOUT",
    "BACKEND_UNAVAILABLE",
    "BACKEND_API_ERROR",
    "BACKEND_INVALID_RESPONSE",
    "INTERNAL_ERROR",
    "ALL_ERROR_CODES",
    "format_error",
    "serialise_error",
    "ToolError",
]
