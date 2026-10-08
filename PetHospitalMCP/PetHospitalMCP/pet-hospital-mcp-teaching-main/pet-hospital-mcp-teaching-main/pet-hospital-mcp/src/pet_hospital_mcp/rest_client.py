"""Async HTTP client for the upstream Go Pet Hospital REST API.

This is the only place in the project that is allowed to import ``httpx``
or to know the wire shape of the Go API. Everything else (tools, server)
talks to the backend through this client's typed methods.

Responsibilities
----------------
* Apply a per-request timeout and bounded retries with exponential
  backoff for transient failures (timeouts and 5xx).
* Translate every HTTPX / JSON / status failure into a structured
  :class:`~pet_hospital_mcp.errors.ToolError`. No HTTPX, Pydantic, or
  Python traceback is ever exposed to the MCP client.
* Keep retryable and non-retryable failures separate: 4xx responses are
  not retried (the request was bad, retrying won't help); only timeouts
  and 5xx are retried.

The client accepts an optional ``transport`` argument so tests can inject
an :class:`httpx.MockTransport` (or an ``ASGITransport``) without ever
touching the real Go service.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Mapping

import httpx

from .config import Settings
from .errors import (
    BACKEND_API_ERROR,
    BACKEND_INVALID_RESPONSE,
    BACKEND_TIMEOUT,
    BACKEND_UNAVAILABLE,
    INTERNAL_ERROR,
    ToolError,
)

_log = logging.getLogger(__name__)

# Statuses that we consider transient and worth retrying.
_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})


class PetHospitalClient:
    """Thin async wrapper around the Go Pet Hospital REST API.

    Instances are cheap to create; the underlying ``httpx.AsyncClient``
    is opened on first call and closed via :meth:`aclose`. For tests,
    pass ``transport=`` to inject a mock transport.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._external_client = client
        self._client: httpx.AsyncClient | None = client

    # -- lifecycle ----------------------------------------------------------

    async def __aenter__(self) -> "PetHospitalClient":
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # noqa: D401
        await self.aclose()

    async def aclose(self) -> None:
        if self._external_client is None and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self._settings.backend_timeout_seconds),
            "base_url": self._settings.pet_hospital_base_url,
            "follow_redirects": False,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        self._client = httpx.AsyncClient(**kwargs)
        return self._client

    # -- public API --------------------------------------------------------

    async def list_pets(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Call ``GET /api/v1/pets`` with the given query parameters.

        Returns the ``data`` field of the Go API's unified response
        envelope on success (i.e. the ``Result`` object).

        Raises
        ------
        ToolError
            On any upstream or parsing failure, with one of the
            ``BACKEND_*`` error codes.
        """

        response = await self._request_with_retries(
            method="GET",
            path="/api/v1/pets",
            params=dict(params),
        )
        return _extract_payload(response, path="/api/v1/pets")

    # -- internals ---------------------------------------------------------

    async def _request_with_retries(
        self,
        *,
        method: str,
        path: str,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        max_attempts = max(1, int(self._settings.backend_max_retries) + 1)
        base_backoff = float(self._settings.backend_backoff_seconds)
        last_error: ToolError | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                client = await self._ensure_client()
                response = await client.request(method, path, params=params)
            except httpx.TimeoutException as exc:
                last_error = ToolError(
                    BACKEND_TIMEOUT,
                    f"Upstream timed out calling {method} {path}: "
                    f"{type(exc).__name__}",
                    {"path": path, "method": method, "attempt": attempt},
                )
                _log.warning(
                    "backend timeout method=%s path=%s attempt=%d/%d",
                    method,
                    path,
                    attempt,
                    max_attempts,
                )
            except (httpx.ConnectError, httpx.NetworkError) as exc:
                last_error = ToolError(
                    BACKEND_UNAVAILABLE,
                    f"Cannot reach upstream at {method} {path}: "
                    f"{type(exc).__name__}",
                    {"path": path, "method": method, "attempt": attempt},
                )
                _log.warning(
                    "backend unreachable method=%s path=%s attempt=%d/%d",
                    method,
                    path,
                    attempt,
                    max_attempts,
                )
            except httpx.HTTPError as exc:
                # Other HTTPX errors are not retryable; surface immediately.
                raise ToolError(
                    BACKEND_UNAVAILABLE,
                    f"HTTP transport error calling {method} {path}: "
                    f"{type(exc).__name__}",
                    {"path": path, "method": method},
                ) from exc
            else:
                if response.status_code < 400:
                    return response

                if (
                    response.status_code in _RETRYABLE_STATUSES
                    and attempt < max_attempts
                ):
                    last_error = _build_api_error(
                        response, path=path, method=method, retryable=True
                    )
                    _log.warning(
                        "backend retryable status=%d path=%s attempt=%d/%d",
                        response.status_code,
                        path,
                        attempt,
                        max_attempts,
                    )
                else:
                    raise _build_api_error(
                        response, path=path, method=method, retryable=False
                    )

            # Back off before the next attempt. Skip the sleep after the
            # final attempt so we don't waste time before raising.
            if attempt < max_attempts:
                await asyncio.sleep(base_backoff * (2 ** (attempt - 1)))

        assert last_error is not None  # noqa: S101 - for type checkers
        raise last_error


def _build_api_error(
    response: httpx.Response,
    *,
    path: str,
    method: str,
    retryable: bool,
) -> ToolError:
    body_preview = _safe_text(response, max_chars=500)
    return ToolError(
        BACKEND_API_ERROR,
        f"Upstream returned HTTP {response.status_code} for {method} {path}",
        {
            "path": path,
            "method": method,
            "status_code": response.status_code,
            "retryable": retryable,
            "body_preview": body_preview,
        },
    )


def _safe_text(response: httpx.Response, *, max_chars: int = 500) -> str:
    """Read up to ``max_chars`` of the response body as text, never raising."""

    try:
        text = response.text
    except Exception:  # pragma: no cover - defensive
        return ""
    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


def _extract_payload(response: httpx.Response, *, path: str) -> dict[str, Any]:
    """Validate the Go API envelope and return its ``data`` field.

    The Go service responds with:

    .. code-block:: json

        { "code": 200, "message": "ok", "data": { ... }, "time": "..." }

    This helper enforces the shape and returns ``data``. Anything that
    doesn't match produces a :class:`ToolError` with code
    :data:`~pet_hospital_mcp.errors.BACKEND_INVALID_RESPONSE`.
    """

    try:
        body_text = response.text
    except Exception as exc:  # pragma: no cover - defensive
        raise ToolError(
            BACKEND_INVALID_RESPONSE,
            f"Could not read response body from {path}: {type(exc).__name__}",
            {"path": path},
        ) from exc

    try:
        envelope = json.loads(body_text)
    except json.JSONDecodeError as exc:
        raise ToolError(
            BACKEND_INVALID_RESPONSE,
            f"Upstream returned invalid JSON for {path}: {exc.msg}",
            {"path": path, "parse_error": exc.msg, "body_preview": body_preview(body_text)},
        ) from exc

    if not isinstance(envelope, dict):
        raise ToolError(
            BACKEND_INVALID_RESPONSE,
            f"Upstream response for {path} is not a JSON object",
            {"path": path, "body_preview": body_preview(body_text)},
        )

    data = envelope.get("data")
    if data is None:
        # The Go API can legitimately return ``data: null`` for failures
        # (it uses ``message`` to describe the error). Distinguish that
        # from a successful empty payload by checking the envelope's
        # ``code``: a 2xx with ``data: null`` is reported as an invalid
        # response because callers expect an object.
        message = envelope.get("message", "")
        raise ToolError(
            BACKEND_INVALID_RESPONSE,
            f"Upstream response for {path} has no data field; "
            f"server message: {message}",
            {"path": path, "envelope_message": message},
        )

    if not isinstance(data, dict):
        raise ToolError(
            BACKEND_INVALID_RESPONSE,
            f"Upstream response for {path} has non-object data",
            {"path": path, "data_type": type(data).__name__},
        )

    return data


def body_preview(text: str, *, max_chars: int = 200) -> str:
    """Truncate a body string for inclusion in error ``details``."""

    if len(text) > max_chars:
        return text[:max_chars] + "..."
    return text


__all__ = ["PetHospitalClient", "body_preview"]


# Make sure INTERNAL_ERROR is re-exported (it is used by callers if they
# need to wrap unexpected exceptions as a ToolError).
__all__.append("INTERNAL_ERROR")  # type: ignore[arg-type]
