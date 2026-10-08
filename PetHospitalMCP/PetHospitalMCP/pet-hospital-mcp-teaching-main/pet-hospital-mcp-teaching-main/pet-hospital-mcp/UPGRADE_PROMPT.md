# UPGRADE_PROMPT — Stage 2 of the Pet Hospital MCP server

> **Scope of this file.** This is a self-contained specification that a
> developer (or AI agent) can use to extend the Stage 1 server with
> additional MCP tools. It assumes the Stage 1 deliverable is already
> in place: the `pet_hospital_mcp/` package with `list_pets`, the
> `PetHospitalClient`, the unified error envelope, JSON logging with
> recursive PII redaction, the `MCPServer` (SDK 2.x) instance, and the
> stateless Streamable HTTP ASGI app.
>
> **Do not start Stage 2 until Stage 1 has been reviewed and accepted.**

## 0. Non-negotiable invariants (carry over from Stage 1)

These **must not** be regressed by Stage 2 work:

1. **Python 3.11+**, **`mcp==2.0.0`**, protocol version **`2026-07-28`**.
2. Use **`mcp.server.MCPServer`** (SDK 2.x). Do **not** import or use
   `mcp.server.fastmcp.FastMCP` (removed in SDK 2.x).
3. **Stateless Streamable HTTP** model — no `initialize`, no
   `Mcp-Session-Id`, no session storage, no SSE recovery. Each HTTP
   request is independent.
4. The Go Pet Hospital REST API is the **only** business backend. Do
   not modify it. The MCP server only talks to it over HTTP via
   `PetHospitalClient`.
5. Every tool failure surfaces the **unified error envelope**:
   `{"error": {"code": "...", "message": "...", "details": {}}}`. No
   HTTPX/Pydantic/SDK/Python stack trace may leak to the MCP client.
6. Every tool call logs a JSON record with `timestamp`, `tool_name`,
   `params` (redacted), `status`, `duration_ms`. Sensitive fields
   (`ownerPhone`/`owner_phone`, `ownerAddr`/`owner_addr`,
   `chipNo`/`chip_no`) are recursively redacted, case-insensitively.
7. No backwards-compatibility with SDK 1.x / FastMCP is provided or
   required.
8. No auth, RBAC, CORS, or `Origin` validation (teaching scenario).

## 1. Goal of Stage 2

Extend the MCP server so AI agents can drive the full Pet Hospital REST
API surface — not just listing pets. Specifically, add one MCP tool per
upstream REST endpoint, reusing the Stage 1 infrastructure.

## 2. Required new tools

The Go REST API exposes (see `internal/api/api.go` for the canonical
list). For Stage 2, add a tool for each of the following endpoints.
**Tool names must be `snake_case`.** Do not add adapter-private
business parameters; the tool's input schema must be a 1:1 mapping of
the upstream REST parameters.

### 2.1 Pet CRUD

| Tool              | Upstream                | Notes                                                                                          |
| ----------------- | ----------------------- | ---------------------------------------------------------------------------------------------- |
| `get_pet`         | `GET /api/v1/pets/{id}` | Returns the full `Pet` (with `records`, `charges`).                                            |
| `create_pet`      | `POST /api/v1/pets`     | Body is a new `Pet`. Returns the created record.                                               |
| `update_pet`      | `PUT /api/v1/pets/{id}` | Body is a full `Pet` (or patch shape — match the Go API's actual contract). Returns updated.    |
| `delete_pet`      | `DELETE /api/v1/pets/{id}` | Returns the deleted pet or a confirmation.                                                    |

### 2.2 Medical records

| Tool                        | Upstream                                 | Notes                                                |
| --------------------------- | ---------------------------------------- | ---------------------------------------------------- |
| `list_medical_records`      | `GET /api/v1/pets/{id}/records`          | Paginated list.                                      |
| `create_medical_record`     | `POST /api/v1/pets/{id}/records`         | Body is a `MedicalRecord`.                           |
| `get_medical_record`        | `GET /api/v1/pets/{id}/records/{recordId}` | Single record.                                     |
| `update_medical_record`     | `PUT /api/v1/pets/{id}/records/{recordId}` | Full or patch per the Go API.                      |
| `delete_medical_record`      | `DELETE /api/v1/pets/{id}/records/{recordId}` | Deletion.                                       |

### 2.3 Charges / treatments

| Tool                  | Upstream                                | Notes                                  |
| --------------------- | --------------------------------------- | -------------------------------------- |
| `list_charges`        | `GET /api/v1/pets/{id}/charges`         | Paginated list.                         |
| `create_charge`       | `POST /api/v1/pets/{id}/charges`        | Body is a `Treatment`.                 |
| `delete_charge`       | `DELETE /api/v1/pets/{id}/charges/{chargeId}` | Deletion.                         |

### 2.4 Batch / admin / stats

| Tool                     | Upstream                              | Notes                                                                  |
| ------------------------ | ------------------------------------- | ---------------------------------------------------------------------- |
| `batch_get_pets`         | `POST /api/v1/pets/batch`             | Accepts an array of IDs. Returns the matching pets.                    |
| `batch_delete_pets`      | `POST /api/v1/pets/batch-delete`      | Accepts an array of IDs. Returns a summary.                            |
| `get_stats`              | `GET /api/v1/stats`                   | Aggregates (counts by species/status, revenue, etc.).                 |
| `seed_pets`              | `POST /admin/seed?count=N`            | Teaching-only seed endpoint. Rate-limit / guard if appropriate.       |
| `health_check`          | `GET /health` on the **Go** service   | Distinct from the MCP server's own `/health`. Reports upstream liveness. |

> Verify each upstream path against the Go service's actual router
> (`internal/api/api.go`) before coding the tool. Do **not** invent
> endpoints.

## 3. Per-tool requirements (apply to every new tool)

1. **Module layout.** One module per tool under
   `src/pet_hospital_mcp/tools/` (e.g. `get_pet.py`,
   `create_medical_record.py`). Each module exports:
   * `TOOL_NAME` (snake_case string)
   * `TOOL_DESCRIPTION` (multi-line string, covering purpose,
     parameters, applicable scenarios, return value, and failure shape)
   * `<ToolName>Input` (Pydantic `BaseModel`, strict, `extra="forbid"`)
   * `<ToolName>Output` (Pydantic `BaseModel`, tolerant — `extra="ignore"`
     on nested models so the Go API can add fields without breaking us)
   * `async def <tool_name>(input, *, client) -> <ToolName>Output`
2. **Registration.** Register the tool in `server.py` with
   `@mcp.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION)`. Use the
   Stage 1 pattern: parameter typed `Any = None`, validation inside the
   function via a `_validate_input` helper that raises `ToolError` on
   failure (so the SDK's own schema-level rejection never surfaces raw
   Pydantic text).
3. **Input validation.**
   * Use Pydantic 2.x `ConfigDict(extra="forbid", strict=True,
     populate_by_name=True)`.
   * Enum-valued fields (species, status, sortBy, order, …) must use
     `Literal[...]` with the exact allowed values the Go `GET
     /api/v1/meta` returns.
   * Numeric fields must reject `NaN`/`Infinity` via a
     `@field_validator(mode="before")` (see `list_pets.py`).
   * Cross-field constraints (e.g. `min <= max`) use
     `@model_validator(mode="after")`.
   * `bool` must be rejected where a number/int is expected (see
     Stage 1's `_reject_bool_for_int` / `_reject_non_finite`).
   * Unknown fields, wrong types, and non-dict input must yield
     `VALIDATION_ERROR`.
4. **Output validation.** Validate the upstream payload with the
   Pydantic output model. A structural mismatch is a
   `BACKEND_INVALID_RESPONSE` — never a Pydantic ValidationError leak.
5. **Error handling.** All upstream failures go through
   `PetHospitalClient`, which already maps them to the right
   `BACKEND_*` code. Wrap any unexpected exception as `INTERNAL_ERROR`.
6. **Logging.** Use `make_tool_record(...)` exactly as `list_pets`
   does; pass the redacted `params` dict.
7. **REST client.** Add a method per endpoint to `PetHospitalClient`
   (e.g. `get_pet(id)`, `create_pet(body)`, …). Reuse the
   `_request_with_retries` and `_extract_payload` helpers. Do not
   bypass the retry/timeout/error-translation logic.

## 4. Tests (required)

For every new tool, add a test module under `tests/` that covers:

* Happy path — correct upstream path/method, all parameters forwarded,
  default values, output model accepts the Go API's real shape
  (including `records`/`charges` being `null` or array).
* Input validation failures (every enum, range, cross-field, NaN,
  Infinity, bool, unknown field, non-dict).
* Upstream 4xx (400/404/422), 5xx (500/502/503), timeout, connection
  error, network error, invalid JSON, empty body, `data: null`,
  non-object `data`, envelope non-object, items missing required
  fields, wrong-typed items.
* Retry-then-success.
* No HTTPX/Pydantic/SDK/Python stack trace in the client-facing error
  envelope.

Reuse the Stage 1 fixtures in `tests/conftest.py`:
`make_mock_transport`, `patched_client`, `make_mcp_client`,
`make_http_client`, `go_envelope`, `go_pets_data`, `go_pet`,
`jsonrpc_request`, `mcp_headers`. Add new `go_*` data builders only
when the upstream shape is genuinely new.

Each new tool must also have at least one HTTP-level test (using
`make_http_client`) verifying:

* The tool is discoverable via `tools/list` over `POST /mcp`.
* The tool is callable via `tools/call` over `POST /mcp`.
* No `Mcp-Session-Id` is sent or returned.
* No `initialize` handshake is required.
* Protocol version reported by the server is `2026-07-28`.

## 5. Documentation updates

1. Update `README.md`:
   * Append a section per new tool: input schema, output schema, error
     envelope, and a concrete `curl` / Python SDK 2.x client example.
   * Bump the "Stage" line at the top to "Stage 2".
   * Replace the "Stage 2 (not implemented)" section with a "Stage 2
     status" table listing the new tools.
2. Update `pyproject.toml`'s `version` (e.g. `0.2.0`) and
   `__version__` in `src/pet_hospital_mcp/__init__.py`.
3. Keep this file (`UPGRADE_PROMPT.md`) as the historical record of the
   Stage 2 scope; do not delete it.

## 6. Acceptance checklist

* `cd pet-hospital-mcp && pytest -q` passes with the extended suite.
* `GET /health` still returns 200 with `protocol_version: 2026-07-28`.
* `POST /mcp` `tools/list` lists every Stage 2 tool by `snake_case` name.
* A raw `curl` `tools/call` against each new tool returns either the
  expected `structured_content` or the unified error envelope.
* No `Mcp-Session-Id` is ever sent or returned.
* No `initialize`/`initialized` handshake is ever required.
* Logs for every new tool carry `timestamp`, `tool_name`, `params`
  (redacted), `status`, `duration_ms`.
* No HTTPX/Pydantic/SDK/Python traceback appears in any client-facing
  tool result.
* The Go Pet Hospital service was not modified.

## 7. Out of scope for Stage 2

* Auth, RBAC, CORS, `Origin` validation.
* SDK 1.x / FastMCP compatibility.
* Streaming / paginated iterator helpers (unless a tool genuinely needs
  them — in that case, add a `stream: bool` flag and document it).
* Any new external dependency beyond `httpx` and `pydantic`.
* Modifying the Go Pet Hospital service.
