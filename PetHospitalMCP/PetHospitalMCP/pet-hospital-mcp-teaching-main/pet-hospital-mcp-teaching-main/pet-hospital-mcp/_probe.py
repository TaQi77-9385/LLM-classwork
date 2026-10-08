import asyncio
import json

import httpx

from mcp import Client
from pet_hospital_mcp import server
from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient


def mock_handler(request: httpx.Request) -> httpx.Response:
    print("REQUEST:", request.method, request.url.path, "?", dict(request.url.params))
    return httpx.Response(
        200,
        json={
            "code": 200,
            "message": "ok",
            "data": {
                "items": [],
                "total": 0,
                "page": 1,
                "pageSize": 20,
                "totalPages": 1,
                "totalCost": 0.0,
            },
            "time": "2026-09-16T00:00:00Z",
        },
    )


async def main() -> None:
    settings = Settings(
        mcp_host="127.0.0.1",
        mcp_port=8000,
        mcp_path="/mcp",
        pet_hospital_base_url="http://test",
        backend_timeout_seconds=1.0,
        backend_max_retries=0,
        backend_backoff_seconds=0.0,
        log_level="INFO",
    )
    server.set_settings(settings)
    transport = httpx.MockTransport(mock_handler)
    server.get_client = lambda: PetHospitalClient(settings, transport=transport)

    async with Client(server.mcp, raise_exceptions=False) as c:
        print("--- list_tools ---")
        tools = await c.list_tools()
        for t in tools.tools:
            print("tool:", t.name)
            print("  desc:", (t.description or "")[:80])
            print("  schema:", json.dumps(t.input_schema, ensure_ascii=False))

        print("\n--- Happy path: {input: {page:1, pageSize:20}} ---")
        result = await c.call_tool("list_pets", {"input": {"page": 1, "pageSize": 20}})
        print("is_error:", result.is_error)
        print("structured_content:", result.structured_content)
        for blk in result.content:
            print("content:", type(blk).__name__, (getattr(blk, "text", None) or "")[:200])

        print("\n--- Validation failure: {input: {page:0}} ---")
        result = await c.call_tool("list_pets", {"input": {"page": 0}})
        print("is_error:", result.is_error)
        print("structured_content:", result.structured_content)
        for blk in result.content:
            print("content:", type(blk).__name__, (getattr(blk, "text", None) or "")[:300])

        print("\n--- Unknown field: {input: {bogus:1}} ---")
        result = await c.call_tool("list_pets", {"input": {"bogus": 1}})
        print("is_error:", result.is_error)
        for blk in result.content:
            print("content:", type(blk).__name__, (getattr(blk, "text", None) or "")[:300])

        print("\n--- Empty call: {} ---")
        result = await c.call_tool("list_pets", {})
        print("is_error:", result.is_error)
        for blk in result.content:
            print("content:", type(blk).__name__, (getattr(blk, "text", None) or "")[:200])

        print("\n--- Non-dict input: {input: 42} ---")
        result = await c.call_tool("list_pets", {"input": 42})
        print("is_error:", result.is_error)
        for blk in result.content:
            print("content:", type(blk).__name__, (getattr(blk, "text", None) or "")[:300])


if __name__ == "__main__":
    asyncio.run(main())
