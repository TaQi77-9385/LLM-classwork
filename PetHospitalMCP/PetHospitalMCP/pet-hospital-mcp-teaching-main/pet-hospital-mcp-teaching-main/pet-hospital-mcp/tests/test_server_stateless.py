"""Server-level tests: tool registration, JSON Schema, ``/health``,
stateless Streamable HTTP, and tool discovery/callability over the HTTP
MCP endpoint.

These tests verify the requirements that are specific to the SDK 2.x
and the 2026-07-28 protocol:

* The tool is registered with the correct snake_case name.
* The tool's JSON Schema is present (even though it is intentionally
  loose — the rich constraints live in the description).
* ``/health`` returns 200 with the expected fields.
* The server does **not** send the old ``initialize`` handshake — the
  in-memory Client uses the 2026-07-28 discover flow.
* The server does **not** require or return ``Mcp-Session-Id`` (the
  stateless mode means every request is independent).
* The tool can be discovered and called through the raw HTTP MCP
  endpoint (``POST /mcp``).
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import go_envelope, go_pets_data, jsonrpc_request, mcp_headers


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

async def test_tool_registered_with_correct_name(make_mcp_client):
    async with make_mcp_client() as mcp_client:
        tools = await mcp_client.list_tools()
        names = [t.name for t in tools.tools]
        assert "list_pets" in names
        # Stage 1 has exactly one tool.
        assert len(names) == 1


async def test_tool_has_description(make_mcp_client):
    async with make_mcp_client() as mcp_client:
        tools = await mcp_client.list_tools()
        tool = next(t for t in tools.tools if t.name == "list_pets")
        assert tool.description is not None
        assert len(tool.description) > 50
        # The description must mention the Go endpoint and the parameters.
        desc = tool.description.lower()
        assert "/api/v1/pets" in tool.description
        assert "page" in desc
        assert "pagesize" in desc
        assert "species" in desc
        assert "status" in desc
        assert "sortby" in desc
        assert "order" in desc


async def test_tool_json_schema_present(make_mcp_client):
    """The inputSchema must be a valid JSON Schema object."""

    async with make_mcp_client() as mcp_client:
        tools = await mcp_client.list_tools()
        tool = next(t for t in tools.tools if t.name == "list_pets")
        schema = tool.input_schema
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        assert "properties" in schema
        # The schema is intentionally loose (the ``input`` property accepts
        # any value); the rich constraints are in the description.
        assert "input" in schema["properties"]


async def test_tool_output_schema_present(make_mcp_client):
    """A structured tool must expose an output schema."""

    async with make_mcp_client() as mcp_client:
        tools = await mcp_client.list_tools()
        tool = next(t for t in tools.tools if t.name == "list_pets")
        # SDK 2.x exposes the output schema if the return type is a Pydantic
        # model. The exact attribute name varies; check both.
        output_schema = getattr(tool, "output_schema", None) or getattr(tool, "outputSchema", None)
        assert output_schema is not None
        assert isinstance(output_schema, dict)


# ---------------------------------------------------------------------------
# /health endpoint (raw HTTP)
# ---------------------------------------------------------------------------

async def test_health_returns_200(make_http_client):
    async with make_http_client() as http_client:
        response = await http_client.get("/health")
        assert response.status_code == 200


async def test_health_body_fields(make_http_client):
    async with make_http_client() as http_client:
        response = await http_client.get("/health")
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "pet-hospital-mcp"
        assert "version" in body
        assert body["protocol_version"] == "2026-07-28"
        assert "stateless" in body["transport"].lower() or "stateless" in body["transport"]


# ---------------------------------------------------------------------------
# Stateless protocol: no Mcp-Session-Id required
# ---------------------------------------------------------------------------

async def test_mcp_endpoint_accepts_request_without_session_id(
    make_http_client, patched_client
):
    """A POST to ``/mcp`` without any ``Mcp-Session-Id`` header must be
    accepted (stateless mode)."""

    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_http_client() as http_client:
        req = jsonrpc_request("tools/list", {})
        response = await http_client.post(
            "/mcp",
            json=req,
            headers=mcp_headers(),
        )
        # 200 or 202 — both are valid for stateless mode. The key is that
        # the request is NOT rejected with 4xx for missing session id.
        assert response.status_code < 400, (
            f"stateless mode should accept requests without Mcp-Session-Id, "
            f"got {response.status_code}: {response.text[:300]}"
        )


async def test_no_mcp_session_id_in_response(
    make_http_client, patched_client
):
    """The server must not return an ``Mcp-Session-Id`` header in stateless mode."""

    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_http_client() as http_client:
        req = jsonrpc_request("tools/list", {})
        response = await http_client.post(
            "/mcp",
            json=req,
            headers=mcp_headers(),
        )
        assert response.status_code < 400
        # The response may or may not carry the header depending on the
        # SDK version, but in stateless mode it should NOT be present (or
        # if present, it should be empty — meaning no session was created).
        session_id = response.headers.get("mcp-session-id")
        assert session_id is None or session_id == "", (
            f"stateless mode must not return Mcp-Session-Id, got: {session_id!r}"
        )


# ---------------------------------------------------------------------------
# Tool discovery over HTTP MCP endpoint
# ---------------------------------------------------------------------------

async def test_http_discover_list_pets(make_http_client, patched_client):
    """``tools/list`` over HTTP must surface ``list_pets``."""

    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_http_client() as http_client:
        req = jsonrpc_request("tools/list", {})
        response = await http_client.post(
            "/mcp",
            json=req,
            headers=mcp_headers(),
        )
        assert response.status_code < 400, response.text[:300]

        body = _parse_jsonrpc_response(response)
        tool_names = [t["name"] for t in body["result"]["tools"]]
        assert "list_pets" in tool_names


async def test_http_call_list_pets(make_http_client, patched_client):
    """``tools/call`` over HTTP must execute ``list_pets`` successfully."""

    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_http_client() as http_client:
        req = jsonrpc_request(
            "tools/call",
            {"name": "list_pets", "arguments": {"input": {"page": 1, "pageSize": 20}}},
        )
        response = await http_client.post(
            "/mcp",
            json=req,
            headers=mcp_headers(),
        )
        assert response.status_code < 400, response.text[:300]

        body = _parse_jsonrpc_response(response)
        result = body["result"]
        assert result["isError"] is False
        # The structured content is in result["structuredContent"]
        sc = result.get("structuredContent")
        assert sc is not None
        assert sc["page"] == 1
        assert sc["pageSize"] == 20


async def test_http_call_list_pets_validation_error(make_http_client, patched_client):
    """A validation failure over HTTP returns ``isError: true`` with the envelope."""

    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_http_client() as http_client:
        req = jsonrpc_request(
            "tools/call",
            {"name": "list_pets", "arguments": {"input": {"page": 0}}},
        )
        response = await http_client.post(
            "/mcp",
            json=req,
            headers=mcp_headers(),
        )
        assert response.status_code < 400, response.text[:300]

        body = _parse_jsonrpc_response(response)
        result = body["result"]
        assert result["isError"] is True
        text = result["content"][0]["text"]
        # Extract the envelope
        idx = text.index("{")
        envelope = json.loads(text[idx:])
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# No old initialize handshake (in-memory)
# ---------------------------------------------------------------------------

async def test_no_initialize_handshake(make_mcp_client):
    """The SDK 2.x in-memory Client uses the 2026-07-28 discover flow,
    not the old ``initialize`` + ``initialized`` handshake.

    We verify this indirectly: the Client connects successfully and can
    list tools without any session establishment step. If the old
    protocol were in use, the Client would need to send ``initialize``
    first, and without it, ``list_tools`` would fail."""

    async with make_mcp_client() as mcp_client:
        tools = await mcp_client.list_tools()
        assert len(tools.tools) == 1
        assert tools.tools[0].name == "list_pets"


async def test_protocol_version_2026_07_28(make_mcp_client):
    """The negotiated protocol version must be ``2026-07-28``."""

    async with make_mcp_client() as mcp_client:
        # The Client exposes the server's protocol version after connection.
        pv = mcp_client.protocol_version
        assert pv == "2026-07-28", f"expected 2026-07-28, got {pv}"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _parse_jsonrpc_response(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON-RPC response, handling both plain JSON and SSE."""

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # Parse the SSE stream: lines starting with ``data:`` contain JSON.
        text = response.text
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[len("data:"):].strip()
                if payload:
                    return json.loads(payload)
        raise ValueError(f"no data line in SSE response: {text[:300]}")
    else:
        return response.json()
