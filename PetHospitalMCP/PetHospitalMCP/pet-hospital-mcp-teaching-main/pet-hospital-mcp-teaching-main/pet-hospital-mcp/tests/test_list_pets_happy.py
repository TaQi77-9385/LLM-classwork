"""Happy-path tests for the ``list_pets`` MCP tool.

Verifies that:
* All 13 filter / sort / pagination parameters are forwarded to the
  upstream ``GET /api/v1/pets`` endpoint with the correct camelCase
  names.
* The Go API's response envelope is parsed into the typed
  :class:`ListPetsOutput` model and returned as structured content.
* ``records`` / ``charges`` being ``null`` or an array are both
  accepted (the Go API can return either).
* The default ``page=1`` / ``pageSize=20`` is sent when no overrides
  are provided.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from conftest import go_envelope, go_pet, go_pets_data


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _capture_handler(captured: dict[str, Any]):
    """Return a mock handler that records the request and returns a valid page."""

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json=go_envelope(go_pets_data()),
        )

    return _handler


async def _call_ok(mcp_client, arguments: dict[str, Any]) -> dict[str, Any]:
    """Call ``list_pets`` and assert success, returning structured content."""

    result = await mcp_client.call_tool("list_pets", arguments)
    assert result.is_error is False, f"unexpected error: {[getattr(c, 'text', c) for c in result.content]}"
    assert result.structured_content is not None
    return result.structured_content


# ---------------------------------------------------------------------------
# Request path & method
# ---------------------------------------------------------------------------

async def test_gets_correct_path_and_method(make_mcp_client, patched_client):
    captured: dict[str, Any] = {}
    patched_client(_capture_handler(captured))

    async with make_mcp_client() as mcp_client:
        await _call_ok(mcp_client, {"input": {}})

    assert captured["method"] == "GET"
    assert captured["path"] == "/api/v1/pets"


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

async def test_defaults_page_and_page_size(make_mcp_client, patched_client):
    captured: dict[str, Any] = {}
    patched_client(_capture_handler(captured))

    async with make_mcp_client() as mcp_client:
        await _call_ok(mcp_client, {"input": {}})

    assert captured["params"]["page"] == "1"
    assert captured["params"]["pageSize"] == "20"
    # No other filter params should be sent.
    assert "q" not in captured["params"]
    assert "name" not in captured["params"]
    assert "species" not in captured["params"]


# ---------------------------------------------------------------------------
# All 13 parameters forwarded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "field,key,value",
    [
        ("q", "q", "肠胃炎"),
        ("name", "name", "小黑"),
        ("owner_name", "ownerName", "张三"),
        ("owner_phone", "ownerPhone", "13800001111"),
        ("species", "species", "犬"),
        ("doctor", "doctor", "李医生"),
        ("disease", "disease", "肠胃炎"),
        ("status", "status", "住院中"),
        ("sort_by", "sortBy", "totalCost"),
        ("order", "order", "desc"),
    ],
    ids=["q", "name", "ownerName", "ownerPhone", "species", "doctor",
         "disease", "status", "sortBy", "order"],
)
async def test_single_param_forwarded(make_mcp_client, patched_client, field: str, key: str, value: str):
    captured: dict[str, Any] = {}
    patched_client(_capture_handler(captured))

    async with make_mcp_client() as mcp_client:
        await _call_ok(mcp_client, {"input": {field: value}})

    assert captured["params"][key] == value


async def test_all_params_forwarded(make_mcp_client, patched_client):
    """All 13 parameters in one call, all forwarded with correct names."""

    captured: dict[str, Any] = {}
    patched_client(_capture_handler(captured))

    async with make_mcp_client() as mcp_client:
        await _call_ok(mcp_client, {
            "input": {
                "q": "发烧",
                "name": "小白",
                "ownerName": "李四",
                "ownerPhone": "13900002222",
                "species": "猫",
                "doctor": "王医生",
                "disease": "感冒",
                "status": "就诊中",
                "min": 100,
                "max": 5000,
                "sortBy": "totalCost",
                "order": "asc",
                "page": 2,
                "pageSize": 50,
            }
        })

    params = captured["params"]
    assert params["q"] == "发烧"
    assert params["name"] == "小白"
    assert params["ownerName"] == "李四"
    assert params["ownerPhone"] == "13900002222"
    assert params["species"] == "猫"
    assert params["doctor"] == "王医生"
    assert params["disease"] == "感冒"
    assert params["status"] == "就诊中"
    assert params["min"] == "100.0"
    assert params["max"] == "5000.0"
    assert params["sortBy"] == "totalCost"
    assert params["order"] == "asc"
    assert params["page"] == "2"
    assert params["pageSize"] == "50"


# ---------------------------------------------------------------------------
# Snake_case aliases
# ---------------------------------------------------------------------------

async def test_snake_case_aliases_accepted(make_mcp_client, patched_client):
    """The input model accepts snake_case as well as camelCase (populate_by_name=True)."""

    captured: dict[str, Any] = {}
    patched_client(_capture_handler(captured))

    async with make_mcp_client() as mcp_client:
        await _call_ok(mcp_client, {
            "input": {
                "owner_name": "张三",
                "owner_phone": "13800001111",
                "sort_by": "name",
                "page_size": 10,
            }
        })

    assert captured["params"]["ownerName"] == "张三"
    assert captured["params"]["ownerPhone"] == "13800001111"
    assert captured["params"]["sortBy"] == "name"
    assert captured["params"]["pageSize"] == "10"


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

async def test_empty_items(make_mcp_client, patched_client):
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    assert result["items"] == []
    assert result["total"] == 0
    assert result["page"] == 1
    assert result["pageSize"] == 20
    assert result["totalPages"] == 1
    assert result["totalCost"] == 0.0


async def test_items_with_pets(make_mcp_client, patched_client):
    pet = go_pet(id="p1", name="小黑", total_cost=250.0)
    data = go_pets_data(items=[pet], total=1, total_cost=250.0)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == "p1"
    assert result["items"][0]["name"] == "小黑"
    assert result["total"] == 1
    assert result["totalCost"] == 250.0


async def test_records_null(make_mcp_client, patched_client):
    """Go can return ``records: null`` — must parse without error."""

    pet = go_pet(records=None)
    data = go_pets_data(items=[pet], total=1)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    assert result["items"][0]["records"] is None


async def test_records_array(make_mcp_client, patched_client):
    pet = go_pet(records=[
        {
            "id": "r1",
            "visitDate": "2026-01-15",
            "doctor": "李医生",
            "diagnosis": "肠胃炎",
            "symptoms": "呕吐",
            "treatment": "输液",
            "prescription": ["甲硝唑"],
            "weightKg": 5.2,
            "temperature": 38.5,
            "followUp": "一周后复查",
            "charge": 200.0,
            "createdAt": "2026-01-15T10:00:00Z",
        }
    ])
    data = go_pets_data(items=[pet], total=1, total_cost=200.0)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    records = result["items"][0]["records"]
    assert isinstance(records, list)
    assert len(records) == 1
    assert records[0]["doctor"] == "李医生"
    assert records[0]["charge"] == 200.0


async def test_charges_null(make_mcp_client, patched_client):
    pet = go_pet(charges=None)
    data = go_pets_data(items=[pet], total=1)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    assert result["items"][0]["charges"] is None


async def test_charges_array(make_mcp_client, patched_client):
    pet = go_pet(charges=[
        {"id": "c1", "item": "挂号", "category": "诊查", "amount": 50.0, "doctor": "李医生", "date": "2026-01-15", "note": ""},
        {"id": "c2", "item": "输液", "category": "治疗", "amount": 150.0, "doctor": "李医生", "date": "2026-01-15", "note": "2瓶"},
    ])
    data = go_pets_data(items=[pet], total=1, total_cost=200.0)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    charges = result["items"][0]["charges"]
    assert isinstance(charges, list)
    assert len(charges) == 2
    assert charges[0]["item"] == "挂号"
    assert charges[1]["amount"] == 150.0


async def test_multiple_pets_pagination(make_mcp_client, patched_client):
    pets = [
        go_pet(id=f"p{i}", name=f"宠物{i}", owner_name=f"主人{i}", owner_phone=f"130000{i:04d}", total_cost=float(i) * 10)
        for i in range(5)
    ]
    data = go_pets_data(items=pets, total=100, page=2, page_size=5, total_pages=20, total_cost=1000.0)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {"page": 2, "pageSize": 5}})
    assert len(result["items"]) == 5
    assert result["total"] == 100
    assert result["page"] == 2
    assert result["pageSize"] == 5
    assert result["totalPages"] == 20
    assert result["totalCost"] == 1000.0


async def test_extra_fields_in_pet_ignored(make_mcp_client, patched_client):
    """Extra unknown fields in pet objects are ignored (extra='ignore')."""

    pet = go_pet()
    pet["unknownField"] = "should be ignored"
    data = go_pets_data(items=[pet], total=1)
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(data)))

    async with make_mcp_client() as mcp_client:
        result = await _call_ok(mcp_client, {"input": {}})
    assert "unknownField" not in result["items"][0]
