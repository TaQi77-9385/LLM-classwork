import asyncio
import math

import httpx

from mcp import Client
from pet_hospital_mcp import server
from pet_hospital_mcp.config import Settings
from pet_hospital_mcp.rest_client import PetHospitalClient
from pet_hospital_mcp.server import _validate_input


def handler(req):
    return httpx.Response(200, json={
        "code": 200, "message": "ok",
        "data": {"items": [], "total": 0, "page": 1, "pageSize": 20, "totalPages": 1, "totalCost": 0.0},
        "time": "x",
    })

# Monkey-patch _validate_input to see what it receives
_original = _validate_input
def _spy(raw):
    print(f"_validate_input received: {raw!r} (type={type(raw).__name__})")
    if isinstance(raw, dict):
        for k, v in raw.items():
            print(f"  key={k!r} value={v!r} type={type(v).__name__}")
            if isinstance(v, float):
                print(f"    isfinite={math.isfinite(v)}")
    return _original(raw)

# Patch in the server module
import pet_hospital_mcp.server as srv
srv._validate_input = _spy


async def main():
    settings = Settings(
        mcp_host="127.0.0.1", mcp_port=0, mcp_path="/mcp",
        pet_hospital_base_url="http://test",
        backend_timeout_seconds=0.5, backend_max_retries=0,
        backend_backoff_seconds=0.0, log_level="WARNING",
    )
    server.set_settings(settings)
    server.get_client = lambda: PetHospitalClient(settings, transport=httpx.MockTransport(handler))

    async with Client(server.mcp, raise_exceptions=False) as c:
        result = await c.call_tool("list_pets", {"input": {"min": float("nan")}})
        print("is_error:", result.is_error)
        for blk in result.content:
            print("text:", (getattr(blk, "text", "") or "")[:200])

asyncio.run(main())
