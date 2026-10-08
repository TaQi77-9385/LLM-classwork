# Pet Hospital MCP

A standalone Python MCP (Model Context Protocol) server that exposes the
[Go Pet Hospital REST API](../pet-hospital-mcp-teaching-main) to AI agents.

* **Python**: 3.11+
* **MCP Python SDK**: `mcp==2.0.0`
* **MCP protocol version**: `2026-07-28`
* **Server class**: `mcp.server.MCPServer` (SDK 2.x — **not** `FastMCP`)
* **Transport**: stateless Streamable HTTP (2026-07-28 protocol)
* **Stage**: 1 — only the `list_pets` tool is exposed. Stage 2 tools are
  intentionally **not** implemented (see
  [Stage 2 status](#stage-2-not-implemented)).

The server never modifies the Go service. It only issues HTTP calls to
`GET /api/v1/pets` on the upstream, validates inputs/outputs with
Pydantic, and surfaces a structured error envelope on any failure.

---

## Table of contents

* [Prerequisites](#prerequisites)
* [Install](#install)
* [Start the Go Pet Hospital API first](#start-the-go-pet-hospital-api-first)
* [Start the MCP server](#start-the-mcp-server)
* [Configuration](#configuration)
* [MCP endpoint](#mcp-endpoint)
* [Verifying the server](#verifying-the-server)
  * [`/health`](#health)
  * [Tool discovery and `list_pets` call (SDK 2.x client)]#tool-discovery-and-list_pets-call-sdk-2x-client)
  * [MCP Inspector](#mcp-inspector)
* [The `list_pets` tool](#the-list_pets-tool)
  * [Input schema](#input-schema)
  * [Output schema](#output-schema)
  * [Error envelope](#error-envelope)
* [Logging](#logging)
* [Tests](#tests)
* [Project layout](#project-layout)
* [Stage 2 (not implemented)](#stage-2-not-implemented)

---

## Prerequisites

* Go 1.21+ (only to run the upstream Go Pet Hospital service).
* Python 3.11+.
* `pip` or any equivalent that can install `pyproject.toml`-based projects.

## Install

From this directory (`pet-hospital-mcp/`):

```bash
# 1. create and activate a virtual environment
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate

# 2. install the project (editable) and dev test deps
pip install -e ".[dev]"
```

`pyproject.toml` pins `mcp==2.0.0` and pulls `httpx` and `pydantic`
automatically. `pytest` and `pytest-asyncio` are pulled in by the `dev`
extra.

## Start the Go Pet Hospital API first

The MCP server calls the Go service over HTTP, so the Go service must be
running and seeded **before** the MCP server starts.

From the repository root:

```bash
cd pet-hospital-mcp-teaching-main
go run . -seed
# → Listening on http://127.0.0.1:8080
```

See the Go project's own README for seeding details. The MCP server
defaults to `PET_HOSPITAL_BASE_URL=http://127.0.0.1:8080`.

## Start the MCP server

After install, run either the console script or the module:

```bash
# console script (declared in pyproject.toml)
pet-hospital-mcp

# or: python -m pet_hospital_mcp

# or with uvicorn directly (uses the `app` attribute in server.py):
uvicorn pet_hospital_mcp.server:app --host 127.0.0.1 --port 8000
```

The server listens on `127.0.0.1:8000` by default. It exposes:

* `GET /health` — liveness probe (plain JSON, no upstream dependency).
* `POST /mcp` — stateless Streamable HTTP MCP endpoint (no
  `Mcp-Session-Id`, no `initialize` handshake).

## Configuration

All settings are read from environment variables at startup.

| Variable                         | Default                      | Meaning                                              |
| -------------------------------- | ---------------------------- | ---------------------------------------------------- |
| `MCP_HOST`                       | `127.0.0.1`                  | Hostname to bind the MCP server to.                   |
| `MCP_PORT`                       | `8000`                       | TCP port for the MCP server.                         |
| `PET_HOSPITAL_BASE_URL`          | `http://127.0.0.1:8080`      | Base URL of the upstream Go Pet Hospital REST API.   |
| `PET_HOSPITAL_TIMEOUT_SECONDS`   | `10.0`                       | Per-request timeout for upstream REST calls.         |
| `PET_HOSPITAL_MAX_RETRIES`       | `2`                          | Retries for transient upstream failures (5xx/timeout).|
| `PET_HOSPITAL_BACKOFF_SECONDS`   | `0.25`                       | Base backoff (seconds); exponential growth `*2^n`.   |
| `PET_HOSPITAL_MCP_PATH`          | `/mcp`                       | Path of the Streamable HTTP MCP endpoint.            |
| `LOG_LEVEL`                      | `INFO`                       | Standard logging level.                              |

The MCP server only listens on `127.0.0.1` in the default
teaching configuration. To bind a different host set `MCP_HOST`.

## MCP endpoint

* **URL**: `http://127.0.0.1:8000/mcp` (default `MCP_HOST`/`MCP_PORT`).
* **Protocol version**: `2026-07-28`.
* **Mode**: **stateless** Streamable HTTP.
  * Each HTTP request is independent.
  * No `initialize`/`initialized` handshake is sent or required.
  * No `Mcp-Session-Id` header is sent or returned.
  * No server-side session storage or expiry.
  * No SSE recovery / resume.
* **Server class**: `from mcp.server import MCPServer` (SDK 2.x).

Discovery and invocation follow the 2026-07-28 specification: the client
sends `notifications/initialized` (or nothing, in stateless mode) and
then `tools/list` / `tools/call` JSON-RPC requests over `POST /mcp`.

## Verifying the server

### `/health`

```bash
curl http://127.0.0.1:8000/health
```

Example response:

```json
{
  "status": "ok",
  "service": "pet-hospital-mcp",
  "version": "0.1.0",
  "protocol_version": "2026-07-28",
  "transport": "streamable-http (stateless)"
}
```

### Tool discovery and `list_pets` call (SDK 2.x client)

The SDK 2.x ships `mcp.Client` for in-process use, but for a real
network check use the Streamable HTTP client. A minimal Python snippet
against a running MCP server:

```python
import asyncio, json
from mcp.client.streamable_http import streamablehttp_client
from mcp import Client

async def main():
    async with streamablehttp_client("http://127.0.0.1:8000/mcp") as (read, write, _get_session_id):
        async with Client(read, write) as client:
            tools = await client.list_tools()
            print("tools:", [t.name for t in tools])

            result = await client.call_tool("list_pets", {"input": {
                "species": "犬",
                "sortBy": "totalCost",
                "order": "desc",
                "page": 1,
                "pageSize": 5,
            }})
            print("is_error:", result.is_error)
            print("structured:", result.structured_content)

asyncio.run(main())
```

A raw JSON-RPC equivalent (no SDK dependency):

```bash
# tools/list
curl -sS -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# tools/call list_pets
curl -sS -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"list_pets","arguments":{"input":{"species":"犬","page":1,"pageSize":5}}}}'
```

Stateless mode means there is **no** `Mcp-Session-Id` header to manage.
You can fire any of the above `curl` commands in any order without a
prior `initialize`.

### MCP Inspector

```bash
npx @modelcontextprotocol/inspector \
  --transport http \
  --endpoint http://127.0.0.1:8000/mcp
```

## The `list_pets` tool

Adapts `GET /api/v1/pets` on the Go service. It accepts exactly the 13
query parameters the backend supports — nothing private to the MCP layer
is added.

### Input schema

All parameters are optional. Unknown fields, `NaN`, `Infinity`, and
type-incorrect values are rejected with `VALIDATION_ERROR`.

| Field        | Alias (camelCase) | Type / constraint                                                                 |
| ------------ | ----------------- | --------------------------------------------------------------------------------- |
| `q`          | —                 | Free-text full-text search across pet fields and medical history.                  |
| `name`       | —                 | Filter by pet name (fuzzy).                                                        |
| `ownerName`  | `owner_name`      | Filter by owner name (fuzzy).                                                      |
| `ownerPhone` | `owner_phone`     | Filter by owner phone (fuzzy).                                                     |
| `species`     | —                | One of `犬`, `猫`, `兔`, `鸟`, `仓鼠`, `爬宠`, `其他`.                            |
| `doctor`     | —                 | Filter by treating doctor (fuzzy).                                                |
| `disease`    | —                 | Filter by disease (fuzzy).                                                         |
| `status`     | —                 | One of `待就诊`, `就诊中`, `住院中`, `已康复`, `慢性病随访`.                      |
| `min`        | —                 | Lower bound on `totalCost` (>= 0, finite).                                        |
| `max`        | —                 | Upper bound on `totalCost` (>= 0, finite). `min <= max` when both are given.      |
| `sortBy`     | `sort_by`         | One of `id`, `name`, `ownerName`, `species`, `doctor`, `disease`, `status`, `totalCost`, `visitCount`, `createdAt`, `updatedAt`. |
| `order`      | —                 | `asc` or `desc`.                                                                  |
| `page`       | —                 | 1-indexed page number, must be `>= 1`. Default `1`.                                |
| `pageSize`   | `page_size`       | Page size, must be in `[1, 500]`. Default `20`.                                    |

### Output schema

On success, `CallToolResult.structured_content` is a JSON object
mirroring the Go API's `data`:

```json
{
  "items": [ /* PetSummary[] */ ],
  "total": 1234,
  "page": 1,
  "pageSize": 20,
  "totalPages": 62,
  "totalCost": 98765.0
}
```

Each `PetSummary` item accepts the Go API's real JSON shape, including
`records` and `charges` being either `null` or an array. Unknown fields
on items are ignored (forward-compat with the Go service).

### Error envelope

Every failure — invalid input, upstream timeout, upstream 4xx/5xx,
non-JSON response, internal error — is surfaced as the same JSON
envelope in `content[0].text`, with `is_error=True`:

```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Invalid input for list_pets: 1 validation error(s) found. See details.validation_errors for per-field info.",
    "details": {
      "tool": "list_pets",
      "validation_errors": [
        { "loc": ["species"], "msg": "Input should be '犬', '猫', '兔', '鸟', '仓鼠', '爬宠' or '其他'", "type": "literal_error" }
      ]
    }
  }
}
```

Error codes:

| Code                     | When                                                                   |
| ------------------------ | ---------------------------------------------------------------------- |
| `VALIDATION_ERROR`       | Bad tool input (bad type, unknown field, out-of-range, `min > max`, …).|
| `BACKEND_TIMEOUT`         | Upstream did not respond in time after all retries.                    |
| `BACKEND_UNAVAILABLE`     | Upstream could not be reached (DNS / connection refused / network).    |
| `BACKEND_API_ERROR`       | Upstream returned HTTP 4xx or 5xx after retries were exhausted.       |
| `BACKEND_INVALID_RESPONSE`| Upstream body was not valid JSON or did not match the data model.      |
| `INTERNAL_ERROR`          | Unexpected internal failure; the original exception is logged server-side but never exposed to the client. |

HTTPX, Pydantic, the MCP SDK, and Python stack traces are **never**
exposed to the MCP client.

## Logging

JSON-formatted log lines are emitted to stderr. Each tool-call record
contains at least:

* `timestamp` (ISO-8601 UTC)
* `tool_name`
* `params` (redacted)
* `status` (`ok` or `error`)
* `duration_ms`

Sensitive fields are redacted **recursively**, case-insensitively, in
both camelCase and snake_case:

* `ownerPhone` / `owner_phone`
* `ownerAddr`  / `owner_addr`
* `chipNo`     / `chip_no`

Each is replaced with the literal `"<REDACTED>"`. No complete sensitive
value is ever written to the log.

## Tests

The test suite never touches the real Go backend. Upstream responses are
served by `httpx.MockTransport` handlers under the test's control.

```bash
cd pet-hospital-mcp
pytest -q
```

Expected result:

```
130 passed in ~2s
```

Coverage includes:

* Happy path: correct path/method, all 13 parameters forwarded, default
  `page`/`pageSize`, empty `items`, `items` with pets, `records`/`charges`
  both `null` and array, pagination, snake_case alias acceptance, extra
  fields ignored on items.
* Validation: `species`/`status`/`sortBy`/`order` enum enforcement,
  `page >= 1`, `1 <= pageSize <= 500`, `min`/`max` non-negative and
  `min <= max`, NaN/Infinity rejection, bool rejection on numeric and
  integer fields, unknown-field rejection, non-dict input rejection,
  empty-input default values.
* Backend: 4xx (400/404/422), 5xx (500/502/503), timeouts, connection
  errors, network errors, invalid JSON, empty body, `data: null`,
  non-object `data`, non-object envelope, items missing required
  fields, wrong-typed items, retry-then-success, no traceback leakage.
* Server / protocol: tool registration (name + description), JSON Schema
  shape, `/health` 200 + fields, **no `Mcp-Session-Id`**, **no
  `initialize`** handshake, protocol version `2026-07-28`, tool
  discoverable and callable over the HTTP MCP endpoint, HTTP-level
  validation error path.
* Logging: `redact()` across camelCase/snake_case/nested
  dict/list/tuple/deeply-nested/case-insensitive, `JsonFormatter` JSON
  output and required-field promotion, tool-call record carries the
  required fields with redaction applied to the formatted output.

## Project layout

```
pet-hospital-mcp/
├── pyproject.toml
├── README.md
├── UPGRADE_PROMPT.md
├── src/
│   └── pet_hospital_mcp/
│       ├── __init__.py
│       ├── __main__.py
│       ├── config.py
│       ├── errors.py
│       ├── logging_config.py
│       ├── py.typed
│       ├── rest_client.py
│       ├── server.py
│       └── tools/
│           ├── __init__.py
│           └── list_pets.py
└── tests/
    ├── conftest.py
    ├── test_list_pets_backend.py
    ├── test_list_pets_happy.py
    ├── test_list_pets_validation.py
    ├── test_logging.py
    └── test_server_stateless.py
```

Adding a new tool in stage 2 only requires a new module under
`tools/` that reuses the existing `PetHospitalClient`, `errors`, and
`logging_config`.

## Stage 2 (not implemented)

This delivery is intentionally limited to **Stage 1**: the single
`list_pets` tool. The following are **not** implemented in this stage
and are reserved for a later upgrade:

* Any tool other than `list_pets` (e.g. `get_pet`, `create_pet`,
  `update_pet`, `delete_pet`, medical-records CRUD, charges, batch
  operations, statistics, admin endpoints).
* Any streaming/paginated iterator helpers.
* Any auth, RBAC, CORS, or `Origin` validation.
* Any SDK 1.x / FastMCP compatibility layer.
