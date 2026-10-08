"""Logging tests: JSON output format, required fields, and PII redaction.

The tests verify that:
* Tool-call log records carry ``timestamp``, ``tool_name``, ``params``,
  ``status``, and ``duration_ms``.
* ``ownerPhone`` / ``owner_phone``, ``ownerAddr`` / ``owner_addr``,
  and ``chipNo`` / ``chip_no`` are recursively redacted in any
  nested structure (dict, list, tuple).
* The redaction is case-insensitive on the key.
* No complete sensitive value appears in the serialised log line.
"""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import httpx
import pytest

from conftest import go_envelope, go_pets_data
from pet_hospital_mcp.logging_config import JsonFormatter, make_tool_record, redact


# ---------------------------------------------------------------------------
# Unit: redact()
# ---------------------------------------------------------------------------

class TestRedact:
    def test_redacts_owner_phone_camel(self):
        result = redact({"ownerPhone": "13800001111"})
        assert result["ownerPhone"] == "<REDACTED>"

    def test_redacts_owner_phone_snake(self):
        result = redact({"owner_phone": "13800001111"})
        assert result["owner_phone"] == "<REDACTED>"

    def test_redacts_owner_addr(self):
        result = redact({"ownerAddr": "北京市朝阳区"})
        assert result["ownerAddr"] == "<REDACTED>"

    def test_redacts_owner_addr_snake(self):
        result = redact({"owner_addr": "北京市"})
        assert result["owner_addr"] == "<REDACTED>"

    def test_redacts_chip_no_camel(self):
        result = redact({"chipNo": "CHIP-001"})
        assert result["chipNo"] == "<REDACTED>"

    def test_redacts_chip_no_snake(self):
        result = redact({"chip_no": "CHIP-001"})
        assert result["chip_no"] == "<REDACTED>"

    def test_redacts_nested_dict(self):
        result = redact({"params": {"ownerPhone": "secret"}})
        assert result["params"]["ownerPhone"] == "<REDACTED>"

    def test_redacts_nested_list(self):
        result = redact([{"chipNo": "C1"}, {"ownerPhone": "P1"}])
        assert result[0]["chipNo"] == "<REDACTED>"
        assert result[1]["ownerPhone"] == "<REDACTED>"

    def test_redacts_nested_tuple(self):
        result = redact(({"ownerAddr": "addr"},))
        assert result[0]["ownerAddr"] == "<REDACTED>"

    def test_redacts_deeply_nested(self):
        result = redact({
            "a": {
                "b": [
                    {"c": {"ownerPhone": "deep"}},
                ],
            },
        })
        assert result["a"]["b"][0]["c"]["ownerPhone"] == "<REDACTED>"

    def test_preserves_non_sensitive_fields(self):
        result = redact({"name": "小黑", "ownerPhone": "13800001111"})
        assert result["name"] == "小黑"
        assert result["ownerPhone"] == "<REDACTED>"

    def test_case_insensitive_key(self):
        result = redact({"OwnerPhone": "x", "OWNERADDR": "y", "ChIpNo": "z"})
        assert result["OwnerPhone"] == "<REDACTED>"
        assert result["OWNERADDR"] == "<REDACTED>"
        assert result["ChIpNo"] == "<REDACTED>"

    def test_returns_none_for_primitives(self):
        assert redact(42) == 42
        assert redact("hello") == "hello"
        assert redact(None) is None


# ---------------------------------------------------------------------------
# Unit: JsonFormatter
# ---------------------------------------------------------------------------

class TestJsonFormatter:
    def test_emits_json_line(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="hello", args=None, exc_info=None,
        )
        line = formatter.format(record)
        parsed = json.loads(line)
        assert parsed["message"] == "hello"
        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"

    def test_promotes_extra_fields(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="tool call", args=None, exc_info=None,
        )
        record.tool_name = "list_pets"
        record.params = {"ownerPhone": "secret", "page": 1}
        record.status = "ok"
        record.duration_ms = 12.345

        line = formatter.format(record)
        parsed = json.loads(line)

        assert parsed["tool_name"] == "list_pets"
        assert parsed["status"] == "ok"
        assert parsed["duration_ms"] == 12.345
        # The params field must be redacted.
        assert parsed["params"]["ownerPhone"] == "<REDACTED>"
        assert parsed["params"]["page"] == 1

    def test_required_fields_present(self):
        """Every tool-call record must carry the five required fields."""

        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="test.py", lineno=1,
            msg="tool call", args=None, exc_info=None,
        )
        record.tool_name = "list_pets"
        record.params = {"page": 1}
        record.status = "ok"
        record.duration_ms = 5.0

        line = formatter.format(record)
        parsed = json.loads(line)

        for field in ("timestamp", "tool_name", "params", "status", "duration_ms"):
            assert field in parsed, f"missing required field: {field}"


# ---------------------------------------------------------------------------
# Integration: tool call produces a log record with redaction
# ---------------------------------------------------------------------------

async def test_tool_call_log_record_redacted(
    make_mcp_client, patched_client, caplog,
):
    """When ``list_pets`` is called with ``ownerPhone`` in the params,
    the serialised log line must have it redacted.

    Redaction happens in :class:`JsonFormatter.format`, not on the raw
    ``LogRecord`` — the record carries the original params dict, and
    the formatter applies :func:`redact` during serialisation. So we
    check the formatted JSON output, not ``record.params`` directly.
    """

    from pet_hospital_mcp.logging_config import configure_logging, JsonFormatter
    configure_logging("DEBUG")

    phone = "13900007777"
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_mcp_client() as mcp_client:
        await mcp_client.call_tool("list_pets", {
            "input": {"ownerPhone": phone, "page": 1}
        })

    # Find the tool-call log record.
    records = [r for r in caplog.records if getattr(r, "tool_name", None) == "list_pets"]
    assert len(records) > 0, "no tool-call log record found"

    record = records[-1]
    # The raw record.params carries the original value (redaction is
    # applied by the formatter, not in-place).
    assert record.params is not None
    assert "ownerPhone" in record.params

    # The FORMATTED output must have the value redacted.
    formatter = JsonFormatter()
    line = formatter.format(record)
    parsed = json.loads(line)
    assert parsed["params"]["ownerPhone"] == "<REDACTED>"
    assert parsed["params"]["page"] == 1
    # And the raw phone must not appear anywhere in the serialised line.
    assert phone not in line


async def test_sensitive_data_not_in_serialised_log(
    make_mcp_client, patched_client, caplog,
):
    """The serialised log line must not contain the raw phone number."""

    from pet_hospital_mcp.logging_config import configure_logging, JsonFormatter
    configure_logging("DEBUG")

    phone = "13900007777"
    patched_client(lambda _r: httpx.Response(200, json=go_envelope(go_pets_data())))

    async with make_mcp_client() as mcp_client:
        await mcp_client.call_tool("list_pets", {
            "input": {"ownerPhone": phone, "page": 1}
        })

    records = [r for r in caplog.records if getattr(r, "tool_name", None) == "list_pets"]
    assert records
    formatter = JsonFormatter()
    line = formatter.format(records[-1])
    assert phone not in line, f"raw phone number found in log: {line}"
