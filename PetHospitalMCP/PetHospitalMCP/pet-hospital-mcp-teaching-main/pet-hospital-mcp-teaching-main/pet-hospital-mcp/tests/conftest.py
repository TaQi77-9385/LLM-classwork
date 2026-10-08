"""Shared pytest fixtures for the Pet Hospital MCP test suite.

Key design points
-----------------
* No test ever touches the real Go backend. The upstream is always an
  :class:`httpx.MockTransport` handler that the test controls.
* The in-memory SDK 2.x :class:`mcp.Client` is used for protocol-level
  tests (tool discovery, calling, validation). It bypasses HTTP
  entirely so we can test the server's logic directly.
* For wire-level tests (``/health``, the ``/mcp`` HTTP endpoint, and
  the "no ``Mcp-Session-Id`` required" assertion) we mount the ASGI
  app behind an :class:`httpx.ASGITransport` and send raw JSON-RPC.
* The ``patched_client`` fixture monkey-patches
  ``server.get_client`` so every tool call uses a mock transport
  instead of the real upstream.

Fixture strategy
----------------
The MCP SDK 2.x ``Client`` uses ``anyio`` TaskGroups internally, which
require the ``async with`` entry and exit to happen in the same task.
Async-generator fixtures (``@pytest.fixture`` + ``yield`` on an ``async``
function) run the fixture body in a different task from the test body
under ``pytest-asyncio``, which triggers "Attempted to exit cancel scope
in a different task than it was entered in". To avoid this, all async
context managers (MCP Client, httpx ASGI client) are provided as
**factory** fixtures (regular sync fixtures returning an
``@asynccontextmanager``); the test calls ``async with factory() as x:``
inside its own task.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
import pytest_asyncio

# Importing ``server`` triggers MCPServer construction + tool registration;
# do it once at module level so all tests share the same instance.
from pet_hospital_mcp import server
from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@pytest.fixture
def test_settings() -> Settings:
    """Settings with a short timeout and zero retries for fast tests."""

    return Settings(
        mcp_host="127.0.0.1",
        mcp_port=0,  # not used — we never bind a socket
        mcp_path="/mcp",
        pet_hospital_base_url="http://test-backend.local",
        backend_timeout_seconds=0.5,
        backend_max_retries=0,
        backend_backoff_seconds=0.0,
        log_level="WARNING",
    )


# ---------------------------------------------------------------------------
# Go API response helpers
# ---------------------------------------------------------------------------

def go_envelope(
    data: Any,
    *,
    code: int = 200,
    message: str = "ok",
) -> dict[str, Any]:
    """Build the Go API's ``{code, message, data, time}`` envelope."""

    return {
        "code": code,
        "message": message,
        "data": data,
        "time": "2026-09-16T00:00:00Z",
    }


def go_pets_data(
    *,
    items: list[dict[str, Any]] | None = None,
    total: int = 0,
    page: int = 1,
    page_size: int = 20,
    total_pages: int = 1,
    total_cost: float = 0.0,
) -> dict[str, Any]:
    """Build the ``data`` object that ``GET /api/v1/pets`` returns."""

    return {
        "items": items if items is not None else [],
        "total": total,
        "page": page,
        "pageSize": page_size,
        "totalPages": total_pages,
        "totalCost": total_cost,
    }


def go_pet(
    *,
    id: str = "pet-1",
    name: str = "小黑",
    species: str = "犬",
    owner_name: str = "张三",
    owner_phone: str = "13800001111",
    owner_addr: str | None = "北京市朝阳区",
    chip_no: str | None = "CHIP-001",
    records: list[dict[str, Any]] | None = None,
    charges: list[dict[str, Any]] | None = None,
    total_cost: float = 0.0,
) -> dict[str, Any]:
    """Build a single pet dict as the Go API would return it."""

    pet: dict[str, Any] = {
        "id": id,
        "name": name,
        "species": species,
        "ownerName": owner_name,
        "ownerPhone": owner_phone,
        "totalCost": total_cost,
    }
    if owner_addr is not None:
        pet["ownerAddr"] = owner_addr
    if chip_no is not None:
        pet["chipNo"] = chip_no
    # Go can send null or array for records/charges — exercise both.
    pet["records"] = records
    pet["charges"] = charges
    return pet


# ---------------------------------------------------------------------------
# Mock-transport fixtures
# ---------------------------------------------------------------------------

MockHandler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_mock_transport() -> Callable[[MockHandler], httpx.MockTransport]:
    """Return a factory that builds an :class:`httpx.MockTransport`."""

    def _factory(handler: MockHandler) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    return _factory


@pytest.fixture
def patched_client(
    test_settings: Settings,
    make_mock_transport,
) -> (
    Callable[[MockHandler], PetHospitalClient]
    | Callable[[], None]
):
    """Yield a factory that patches ``server.get_client`` with a mock.

    Usage::

        def test_foo(patched_client):
            client = patched_client(lambda req: httpx.Response(200, json=...))
            # server.get_client() now returns a client backed by the mock

    Cleanup (restoring the original ``get_client``) runs at fixture
    teardown via ``yield``.
    """

    original_get_client = server.get_client
    server.set_settings(test_settings)

    def _patch(handler: MockHandler) -> PetHospitalClient:
        transport = make_mock_transport(handler)
        client = PetHospitalClient(test_settings, transport=transport)
        server.get_client = lambda: client  # type: ignore[assignment]
        return client

    yield _patch

    server.get_client = original_get_client  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# In-memory MCP client helper (factory — see module docstring)
# ---------------------------------------------------------------------------

@pytest.fixture
def make_mcp_client():
    """Return an async context manager factory that creates an in-memory
    SDK 2.x :class:`mcp.Client`.

    Usage::

        async def test_foo(make_mcp_client, patched_client):
            patched_client(handler)
            async with make_mcp_client() as c:
                result = await c.call_tool("list_pets", {...})

    The ``async with`` lives inside the test's own task/loop, avoiding the
    "exit cancel scope in a different task" error that async-generator
    fixtures cause with anyio's TaskGroup.
    """

    from mcp import Client

    @asynccontextmanager
    async def _factory():
        async with Client(server.mcp, raise_exceptions=False) as c:
            yield c

    return _factory


# ---------------------------------------------------------------------------
# ASGI app + raw HTTP client (factory — see module docstring)
# ---------------------------------------------------------------------------

@pytest.fixture
def make_http_client(test_settings: Settings):
    """Return an async context manager factory that builds an
    ``httpx.AsyncClient`` backed by an ASGI transport (no socket).

    Usage::

        async def test_foo(make_http_client, patched_client):
            patched_client(handler)
            async with make_http_client() as c:
                resp = await c.get("/health")
    """

    from mcp.server.transport_security import TransportSecuritySettings
    from pet_hospital_mcp.server import build_app, mcp

    server.set_settings(test_settings)
    # Disable DNS-rebinding protection for tests: httpx.ASGITransport
    # sends a port-less ``Host: localhost`` header that the SDK's default
    # ``localhost:*`` allowlist pattern rejects with 421 Misdirected
    # Request. Production keeps the default (protection enabled).
    app = build_app(
        settings=test_settings,
        stateless=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    @asynccontextmanager
    async def _factory() -> AsyncIterator[httpx.AsyncClient]:
        # The stateless Streamable HTTP app requires the session manager's
        # task group to be running. In a real deployment the Starlette
        # lifespan does this; with ``httpx.ASGITransport`` (which skips
        # lifespans) we enter it manually here.
        async with mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as c:
                yield c

    return _factory


# ---------------------------------------------------------------------------
# JSON-RPC helpers for raw HTTP tests
# ---------------------------------------------------------------------------

def jsonrpc_request(method: str, params: Any, *, req_id: int = 1) -> dict[str, Any]:
    """Build a single JSON-RPC 2.0 request object."""

    return {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}


def mcp_headers(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    """HTTP headers for a Streamable HTTP MCP request (stateless mode).

    In stateless mode no ``Mcp-Session-Id`` header is sent — the test
    suite asserts on this absence explicitly.
    """

    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if extra:
        headers.update(extra)
    return headers
