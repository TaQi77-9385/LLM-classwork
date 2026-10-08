"""Input-validation tests for the ``list_pets`` MCP tool.

Every test asserts that:
* the call result has ``is_error=True`` (the SDK 2.x snake_case marker);
* ``content[0].text`` contains the unified error envelope
  ``{"error": {"code": "VALIDATION_ERROR", ...}}``;
* no raw Pydantic or Python traceback leaks to the client.

The in-memory :class:`mcp.Client` is used so the mock upstream handler
is never actually reached — validation fails before the REST client is
called.
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

def _always_ok_handler(_request: httpx.Request) -> httpx.Response:
    """A mock handler that returns a valid empty page for any request."""

    return httpx.Response(200, json=go_envelope(go_pets_data()))


async def _call(mcp_client, arguments: dict[str, Any]) -> tuple[bool, str]:
    """Call ``list_pets`` and return ``(is_error, content_text)``."""

    result = await mcp_client.call_tool("list_pets", arguments)
    text = ""
    if result.content:
        text = getattr(result.content[0], "text", "") or ""
    return result.is_error, text


def _parse_envelope(text: str) -> dict[str, Any]:
    """Extract the JSON error envelope from the SDK-wrapped content.

    The SDK 2.x wraps tool-exception text as
    ``"Error executing tool list_pets: <envelope_json>"``.
    The envelope itself is valid JSON, so we locate the first ``{`` and
    parse from there.
    """

    idx = text.index("{")
    return json.loads(text[idx:])


# ---------------------------------------------------------------------------
# Species
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_species",
    ["dog", "cat", "DOG", "兔兔", "horse", ""],
    ids=["english-dog", "english-cat", "uppercase", "doubled-char", "unknown", "empty"],
)
async def test_species_invalid(make_mcp_client, patched_client, bad_species: str):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"species": bad_species}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        locs = [e["loc"] for e in envelope["error"]["details"].get("validation_errors", [])]
        assert ["species"] in locs


async def test_species_all_valid(make_mcp_client, patched_client):
    """Every species value accepted by the Go backend is accepted here."""

    handler = _always_ok_handler
    patched_client(handler)
    async with make_mcp_client() as mcp_client:
        for sp in ["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]:
            is_error, _ = await _call(mcp_client, {"input": {"species": sp}})
            assert is_error is False, f"species={sp!r} should be valid"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_status",
    ["active", "pending", "DONE", "住院", ""],
    ids=["active", "pending", "uppercase", "partial", "empty"],
)
async def test_status_invalid(make_mcp_client, patched_client, bad_status: str):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"status": bad_status}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        locs = [e["loc"] for e in envelope["error"]["details"].get("validation_errors", [])]
        assert ["status"] in locs


async def test_status_all_valid(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        for st in ["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]:
            is_error, _ = await _call(mcp_client, {"input": {"status": st}})
            assert is_error is False, f"status={st!r} should be valid"


# ---------------------------------------------------------------------------
# sortBy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "bad_sort",
    ["price", "date", "owner", "TOTALCOST", ""],
    ids=["price", "date", "owner", "uppercase", "empty"],
)
async def test_sort_by_invalid(make_mcp_client, patched_client, bad_sort: str):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"sortBy": bad_sort}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_sort_by_all_valid(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        valid = [
            "id", "name", "ownerName", "species", "doctor", "disease",
            "status", "totalCost", "visitCount", "createdAt", "updatedAt",
        ]
        for sb in valid:
            is_error, _ = await _call(mcp_client, {"input": {"sortBy": sb}})
            assert is_error is False, f"sortBy={sb!r} should be valid"


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_order", ["ascending", "descending", "up", "ASC", ""])
async def test_order_invalid(make_mcp_client, patched_client, bad_order: str):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"order": bad_order}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_order_all_valid(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        for od in ["asc", "desc"]:
            is_error, _ = await _call(mcp_client, {"input": {"order": od}})
            assert is_error is False


# ---------------------------------------------------------------------------
# Page / pageSize
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_page", [0, -1, -100], ids=["zero", "negative", "big-negative"])
async def test_page_below_one(make_mcp_client, patched_client, bad_page: int):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"page": bad_page}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        locs = [e["loc"] for e in envelope["error"]["details"].get("validation_errors", [])]
        assert ["page"] in locs


@pytest.mark.parametrize("bad_size", [0, -1, 501, 1000], ids=["zero", "negative", "501", "1000"])
async def test_page_size_out_of_range(make_mcp_client, patched_client, bad_size: int):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"pageSize": bad_size}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_page_size_boundary_values(make_mcp_client, patched_client):
    """pageSize = 1 and pageSize = 500 are the valid boundaries."""

    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        for size in [1, 500]:
            is_error, _ = await _call(mcp_client, {"input": {"pageSize": size}})
            assert is_error is False, f"pageSize={size} should be valid"


async def test_page_bool_rejected(make_mcp_client, patched_client):
    """``true`` / ``false`` are not valid integers even though Python ``bool``
    subclasses ``int`` — our validator rejects them explicitly."""

    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"page": True}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_page_size_bool_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"pageSize": False}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# min / max
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_min", [-0.01, -1, -1000], ids=["tiny-negative", "negative", "big-negative"])
async def test_min_negative(make_mcp_client, patched_client, bad_min: float):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"min": bad_min}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_max_negative(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"max": -1}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_min_greater_than_max(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"min": 500, "max": 100}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        # The cross-field ``@model_validator`` raises a model-level
        # error whose ``loc`` is ``[]`` (no specific field). Check that
        # at least one validation error mentions ``min`` in the message.
        val_errors = envelope["error"]["details"].get("validation_errors", [])
        assert len(val_errors) > 0
        msgs = [str(e.get("msg", "")) for e in val_errors]
        assert any("min" in m.lower() or "max" in m.lower() for m in msgs)


async def test_min_equal_max_ok(make_mcp_client, patched_client):
    """``min == max`` is valid (exact match)."""

    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, _ = await _call(mcp_client, {"input": {"min": 100, "max": 100}})
        assert is_error is False


async def test_min_zero_ok(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, _ = await _call(mcp_client, {"input": {"min": 0}})
        assert is_error is False


# ---------------------------------------------------------------------------
# NaN / Infinity (unit test — cannot be transmitted via JSON-RPC because
# the SDK 2.x serialises arguments to JSON, and standard JSON has no
# representation for NaN/Infinity — they become ``null`` before reaching
# the tool function. The validation is still exercised here at the model
# level to prove the guard works.)
# ---------------------------------------------------------------------------

class TestNaNInfinityUnit:
    """Unit tests for NaN / Infinity rejection in :class:`ListPetsInput`.

    These bypass the MCP pipeline because the SDK 2.x's JSON
    serialisation converts ``float("nan")`` / ``float("inf")`` to
    ``null`` before the tool function is called. The validators
    themselves are tested here directly.
    """

    def test_nan_rejected(self):
        import math
        from pet_hospital_mcp.tools.list_pets import ListPetsInput

        with pytest.raises(Exception) as exc_info:
            ListPetsInput.model_validate({"min": float("nan")})
        errors = exc_info.value.errors()
        assert any("finite" in str(e.get("msg", "")).lower() for e in errors)

    def test_infinity_rejected(self):
        from pet_hospital_mcp.tools.list_pets import ListPetsInput

        with pytest.raises(Exception):
            ListPetsInput.model_validate({"max": float("inf")})

    def test_negative_infinity_rejected(self):
        from pet_hospital_mcp.tools.list_pets import ListPetsInput

        with pytest.raises(Exception):
            ListPetsInput.model_validate({"min": float("-inf")})

    def test_normal_float_accepted(self):
        from pet_hospital_mcp.tools.list_pets import ListPetsInput

        m = ListPetsInput.model_validate({"min": 100.5})
        assert m.min == 100.5


# ---------------------------------------------------------------------------
# Unknown fields
# ---------------------------------------------------------------------------

async def test_unknown_field_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"page": 1, "bogus": "x"}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        locs = [e["loc"] for e in envelope["error"]["details"].get("validation_errors", [])]
        assert ["bogus"] in locs


async def test_multiple_unknown_fields_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"foo": 1, "bar": 2}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Type errors
# ---------------------------------------------------------------------------

async def test_page_string_rejected(make_mcp_client, patched_client):
    """``strict=True`` rejects strings where an int is expected."""

    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"page": "1"}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_min_string_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"min": "100"}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_min_bool_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": {"min": True}})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Non-dict / non-object input
# ---------------------------------------------------------------------------

async def test_non_dict_input_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": 42})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        assert envelope["error"]["details"]["input_type"] == "int"


async def test_string_input_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": "hello"})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


async def test_list_input_rejected(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, text = await _call(mcp_client, {"input": [1, 2, 3]})
        assert is_error is True
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"


# ---------------------------------------------------------------------------
# Empty input (valid — uses defaults)
# ---------------------------------------------------------------------------

async def test_empty_input_uses_defaults(make_mcp_client, patched_client):
    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, _ = await _call(mcp_client, {"input": {}})
        assert is_error is False


async def test_no_input_key_uses_defaults(make_mcp_client, patched_client):
    """When the LLM sends no ``input`` key, it defaults to ``None`` → ``{}``."""

    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        is_error, _ = await _call(mcp_client, {})
        assert is_error is False


# ---------------------------------------------------------------------------
# No traceback leakage
# ---------------------------------------------------------------------------

async def test_no_python_traceback_in_error(make_mcp_client, patched_client):
    """The error content must not contain a Python traceback or HTTPX internals.

    The SDK wraps our :class:`ToolError` text as
    ``"Error executing tool list_pets: <envelope_json>"``. The envelope
    itself contains only the error code, a clean message, and per-field
    details — no Pydantic model names, no URLs, no stack traces.
    """

    patched_client(_always_ok_handler)
    async with make_mcp_client() as mcp_client:
        _, text = await _call(mcp_client, {"input": {"page": 0}})
        assert "Traceback" not in text
        assert "httpx" not in text.lower()
        # The envelope must not contain Pydantic URLs or model names.
        envelope = _parse_envelope(text)
        assert envelope["error"]["code"] == "VALIDATION_ERROR"
        # No pydantic.dev URLs in the details
        details_str = json.dumps(envelope["error"]["details"])
        assert "pydantic.dev" not in details_str
        assert "ListPetsInput" not in text
        assert "ValidationError" not in text
