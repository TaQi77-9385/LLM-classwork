"""Backend-failure tests for the ``list_pets`` MCP tool.

Covers:
* Go REST API returning 4xx (not retried).
* Go REST API returning 5xx (retried, then failed).
* Upstream timeout.
* Upstream connection error.
* Upstream returning invalid JSON.
* Upstream returning a valid JSON envelope but with no ``data`` field.
* Upstream returning ``data`` that does not match the expected model.

Every test asserts the call result has ``is_error=True`` and the content
contains the unified error envelope with the expected ``code``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import go_envelope, go_pets_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call(mcp_client) -> tuple[bool, str]:
    result = await mcp_client.call_tool("list_pets", {"input": {}})
    text = ""
    if result.content:
        text = getattr(result.content[0], "text", "") or ""
    return result.is_error, text


def _parse_envelope(text: str) -> dict[str, Any]:
    """Extract the JSON error envelope from the SDK-wrapped content."""

    idx = text.index("{")
    return json.loads(text[idx:])


# ---------------------------------------------------------------------------
# 4xx (not retried — single attempt)
# ---------------------------------------------------------------------------

async def test_backend_400(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(400, json={"error": "bad request"}))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_API_ERROR"
        assert envelope["error"]["details"]["status_code"] == 400


async def test_backend_404(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(404, json={"error": "not found"}))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_API_ERROR"
        assert envelope["error"]["details"]["status_code"] == 404


async def test_backend_422(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(422, json={"error": "validation failed"}))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_API_ERROR"
        assert envelope["error"]["details"]["status_code"] == 422


# ---------------------------------------------------------------------------
# 5xx (retried then failed)
# ---------------------------------------------------------------------------

async def test_backend_500(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(500, json={"error": "internal"}))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_API_ERROR"
        assert envelope["error"]["details"]["status_code"] == 500


async def test_backend_502(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(502, json={"error": "bad gateway"}))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_API_ERROR"


async def test_backend_503(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(503, json={"error": "unavailable"}))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_API_ERROR"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

async def test_backend_timeout(make_mcp_client, patched_client):
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    patched_client(_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_TIMEOUT"


async def test_backend_connect_timeout(make_mcp_client, patched_client):
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connect timeout")

    patched_client(_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        # ConnectTimeout is a subclass of both TimeoutException and
        # NetworkError; the client checks TimeoutException first.
        assert envelope["error"]["code"] in ("BACKEND_TIMEOUT", "BACKEND_UNAVAILABLE")


# ---------------------------------------------------------------------------
# Connection error
# ---------------------------------------------------------------------------

async def test_backend_connect_error(make_mcp_client, patched_client):
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    patched_client(_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_UNAVAILABLE"


async def test_backend_network_error(make_mcp_client, patched_client):
    def _handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.NetworkError("network error")

    patched_client(_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_UNAVAILABLE"


# ---------------------------------------------------------------------------
# Invalid JSON
# ---------------------------------------------------------------------------

async def test_backend_invalid_json(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(200, content=b"not json at all"))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"


async def test_backend_empty_body(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(200, content=b""))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"


# ---------------------------------------------------------------------------
# Envelope shape errors
# ---------------------------------------------------------------------------

async def test_backend_data_null(make_mcp_client, patched_client):
    """Go can return ``data: null`` with an error message — surface as invalid response."""

    patched_client(lambda _r: httpx.Response(
        200,
        json={"code": 200, "message": "internal error", "data": None, "time": "x"},
    ))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"


async def test_backend_data_not_object(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(
        200,
        json={"code": 200, "message": "ok", "data": [1, 2, 3], "time": "x"},
    ))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"
        assert envelope["error"]["details"]["data_type"] == "list"


async def test_backend_envelope_not_object(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(200, json=[1, 2, 3]))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"


# ---------------------------------------------------------------------------
# Data model mismatch
# ---------------------------------------------------------------------------

async def test_backend_items_missing_required_fields(make_mcp_client, patched_client):
    """If a pet in ``items`` is missing required fields (name/ownerName/ownerPhone),
    it fails ListPetsOutput validation → BACKEND_INVALID_RESPONSE."""

    data = go_pets_data(items=[{"id": "1"}], total=1)  # missing name etc.
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"


async def test_backend_items_wrong_type(make_mcp_client, patched_client):
    """``items`` being a string instead of a list is rejected."""

    data = go_pets_data()
    data["items"] = "not a list"
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client)
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "BACKEND_INVALID_RESPONSE"


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

async def test_retryable_status_then_success(make_mcp_client, patched_client):
    """A 503 followed by 200 on the retry succeeds."""

    call_count = {"n": 0}

    def _handler(_request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(200, json=go_envelope(go_pets_data()))

    # Use a client with 1 retry
    from pet_hospital_mcp.config import Settings
    from pet_hospital_mcp.rest_client import PetHospitalClient
    from pet_hospital_mcp import server

    settings = Settings(
        mcp_host="127.0.0.1", mcp_port=0, mcp_path="/mcp",
        pet_hospital_base_url="http://test",
        backend_timeout_seconds=0.5,
        backend_max_retries=1,
        backend_backoff_seconds=0.0,
        log_level="WARNING",
    )
    server.set_settings(settings)
    transport = httpx.MockTransport(_handler)
    client = PetHospitalClient(settings, transport=transport)
    server.get_client = lambda: client  # type: ignore[assignment]

    async with make_mcp_client() as mcp_client:
        result = await mcp_client.call_tool("list_pets", {"input": {}})
        assert result.is_error is False
        assert call_count["n"] == 2


# ---------------------------------------------------------------------------
# No traceback leakage
# ---------------------------------------------------------------------------

async def test_no_traceback_in_backend_error(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(500, json={"error": "internal"}))
    async with make_mcp_client() as mcp_client:
        _, text = await _call(mcp_client)
        assert "Traceback" not in text
        assert "httpx" not in text.lower()


async def test_no_traceback_in_timeout(make_mcp_client, patched_client):
    def _handler(_r: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    patched_client(_handler)
    async with make_mcp_client() as mcp_client:
        _, text = await _call(mcp_client)
        assert "Traceback" not in text
        assert "httpx" not in text.lower()
