"""The MCP server instance, tool registration, and ASGI assembly.

This module wires the project together:

* Constructs a single :class:`mcp.server.MCPServer` instance (the SDK
  2.x class — **not** ``FastMCP``, which is removed in SDK 2.x).
* Registers every tool in :mod:`pet_hospital_mcp.tools` on that
  instance.
* Builds the Streamable HTTP ASGI application in **stateless** mode:
  a fresh transport per request, no session tracking, no
  ``initialize`` handshake, no ``Mcp-Session-Id`` header. This is the
  2026-07-28 protocol's stateless mode.
* Adds a ``/health`` HTTP route via ``@mcp.custom_route`` for
  orchestration probes.

Important: this module exposes a module-level ``mcp`` object so that the
SDK's in-memory ``Client(mcp)`` test path, ``mcp dev``, and ``mcp run``
can all import it. Constructing the ASGI app via
:meth:`build_app` is deferred so tests can choose when side effects
start.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount

from . import __version__
from .config import Settings, load_settings
from .logging_config import configure_logging
from .rest_client import PetHospitalClient
from .tools.list_pets import (
    TOOL_DESCRIPTION,
    TOOL_NAME,
    ListPetsInput,
    ListPetsOutput,
    list_pets as _list_pets_fn,
)

# ---------------------------------------------------------------------------
# Server construction
# ---------------------------------------------------------------------------

#: The single MCPServer instance for this process. Module-level so that
#: ``mcp dev`` / ``mcp run`` and the in-memory ``Client(mcp)`` test
#: transport can import it directly.
mcp: MCPServer = MCPServer(
    "Pet Hospital MCP",
    version=__version__,
    instructions=(
        "Pet Hospital MCP server. Stage 1 exposes one tool: `list_pets`, "
        "which queries the upstream Go Pet Hospital REST API. The server "
        "is stateless Streamable HTTP (2026-07-28 protocol)."
    ),
)

# Holds the Settings used to build the active client; populated lazily
# so that the module can be imported without any I/O.
_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the active :class:`Settings`, loading them on first call."""

    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def set_settings(settings: Settings) -> None:
    """Override the active settings (for tests)."""

    global _settings
    _settings = settings


def get_client() -> PetHospitalClient:
    """Build a fresh :class:`PetHospitalClient` for a single tool call.

    Statelessness extends to the client: each call opens its own
    short-lived HTTP client so requests cannot accidentally share
    connection state. The client is closed by the tool via its own
    ``async with`` block — see :func:`_list_pets_tool`.
    """

    return PetHospitalClient(get_settings())


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def _validate_input(raw: Any) -> ListPetsInput:
    """Validate raw tool arguments into :class:`ListPetsInput`.

    The MCP SDK 2.x is intentionally given a ``dict[str, Any]`` parameter
    type for :func:`_list_pets_tool` (see below). That keeps the JSON
    Schema the LLM sees loose, and gives us full control over validation
    so we can surface *every* invalid input as the unified error envelope
    rather than the SDK's Pydantic-formatted auto-rejection text. The
    Pydantic :class:`ListPetsInput` model carries all the strict
    constraints (enum values, ranges, NaN/Infinity rejection,
    ``min <= max``) and is consulted here.
    """

    from .errors import VALIDATION_ERROR, ToolError

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolError(
            VALIDATION_ERROR,
            "Invalid input: expected a JSON object matching the "
            "list_pets input schema.",
            {"tool": TOOL_NAME, "input_type": type(raw).__name__},
        )

    try:
        return ListPetsInput.model_validate(raw)
    except Exception as exc:
        # Pull the failing field names out of Pydantic's ValidationError
        # without leaking the full Python traceback to the client. In
        # Pydantic 2.x, ``ValidationError.errors()`` returns a list of
        # plain dicts (with keys ``loc``, ``msg``, ``type``, ``input``,
        # ``url``). We extract only ``loc``, ``msg``, and ``type``,
        # deliberately omitting ``url`` (which contains
        # ``errors.pydantic.dev`` — an internal that should not leak)
        # and ``input`` (which might carry user data).
        errors_fn = getattr(exc, "errors", None)
        extracted: list[dict[str, Any]] = []
        if callable(errors_fn):
            try:
                for e in errors_fn():
                    if isinstance(e, dict):
                        extracted.append(
                            {
                                "loc": list(e.get("loc", ())),
                                "msg": str(e.get("msg", "")),
                                "type": str(e.get("type", "")),
                            }
                        )
                    else:  # pragma: no cover - defensive
                        extracted.append(
                            {
                                "loc": list(getattr(e, "loc", ())),
                                "msg": str(getattr(e, "msg", e)),
                                "type": str(getattr(e, "type", "")),
                            }
                        )
            except Exception:  # pragma: no cover - defensive
                pass

        # Count the number of failures for a clean, Pydantic-free message.
        count = len(extracted)
        raise ToolError(
            VALIDATION_ERROR,
            f"Invalid input for list_pets: {count} validation error(s) "
            f"found. See details.validation_errors for per-field info.",
            {"tool": TOOL_NAME, "validation_errors": extracted},
        ) from exc


@mcp.tool(name=TOOL_NAME, description=TOOL_DESCRIPTION)
async def _list_pets_tool(input: Any = None) -> ListPetsOutput:
    """MCP tool wrapper around :func:`list_pets`.

    The parameter is intentionally typed ``Any`` with a ``None`` default
    so the SDK 2.x's own schema validation (which runs **before** this
    function body) can never reject a call: there is no required field
    to be missing and no typed constraint to be violated. That keeps the
    SDK from ever surfacing raw Pydantic validation text to the MCP
    client. Every real validation failure is instead funneled through
    :func:`_validate_input`, which raises :class:`ToolError` whose
    ``__str__`` is the project's unified error envelope
    (``{"error": {"code": "VALIDATION_ERROR", ...}}``). The SDK then
    surfaces that as ``is_error=True`` (the SDK 2.x snake_case failure
    marker — **not** the 1.x ``CallToolResult.isError`` spelling) with
    the envelope JSON embedded in ``content[0].text``.

    The cost is that the JSON Schema the LLM sees for ``input`` is
    intentionally loose (``{"type": "object"}``); the rich per-field
    constraints (allowed enum values, ranges, ``min <= max``) are
    documented inline in :data:`TOOL_DESCRIPTION` instead. Unknown
    fields are still rejected — by :class:`ListPetsInput`'s
    ``extra="forbid"`` inside :func:`_validate_input` — because the raw
    dict is passed through to it untouched.
    """

    from .errors import INTERNAL_ERROR, ToolError  # local import

    validated = _validate_input(input)

    client = get_client()
    try:
        async with client:
            return await _list_pets_fn(validated, client=client)
    except ToolError:
        raise
    except Exception as exc:  # pragma: no cover - defensive last resort
        raise ToolError(
            INTERNAL_ERROR,
            f"Unexpected error while executing list_pets: {type(exc).__name__}",
            {"tool": TOOL_NAME},
        ) from exc


# ---------------------------------------------------------------------------
# /health custom route
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> Response:
    """Lightweight health-check route.

    Returns a 200 with a small JSON body. The check does not depend on
    the upstream Go service being reachable, on purpose: ``/health``
    answers the question "is the MCP server process alive?", not "is
    the whole system healthy?". The upstream liveness is verified by
    actually calling a tool.
    """

    return JSONResponse(
        {
            "status": "ok",
            "service": "pet-hospital-mcp",
            "version": __version__,
            "protocol_version": "2026-07-28",
            "transport": "streamable-http (stateless)",
        }
    )


# ---------------------------------------------------------------------------
# ASGI app assembly
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: Starlette) -> AsyncIterator[None]:
    """Enter the SDK session manager for the lifetime of the host app.

    Mounting the MCP app inside a host Starlette disables the built-in
    lifespan, so the host must enter ``mcp.session_manager.run()`` here.
    """

    async with mcp.session_manager.run():
        yield


def build_app(
    *,
    settings: Settings | None = None,
    stateless: bool = True,
    mount_path: str = "/",
    mcp_path: str | None = None,
    transport_security: Any = None,
) -> Starlette:
    """Assemble the Starlette ASGI app.

    Parameters
    ----------
    settings
        Optional settings override; defaults to :func:`get_settings`.
    stateless
        If True (default), build the MCP app with ``stateless_http=True``
        so each request is independent. This is the project's required
        operating mode (2026-07-28 protocol, no ``Mcp-Session-Id``).
    mount_path
        Where to mount the MCP sub-app inside the host. Default ``/`` so
        the MCP endpoint is at ``/mcp`` (the SDK default).
    mcp_path
        Override the SDK's default ``/mcp`` path. ``None`` keeps the
        default.
    transport_security
        Optional :class:`mcp.server.transport_security.TransportSecuritySettings`
        override. ``None`` (default) keeps the SDK's behavior, which
        auto-enables DNS-rebinding protection when ``host`` is localhost.
        Tests pass an instance with
        ``enable_dns_rebinding_protection=False`` because
        :class:`httpx.ASGITransport` sends a port-less ``Host`` header
        that the default ``localhost:*`` allowlist pattern rejects.
    """

    if settings is not None:
        set_settings(settings)
    else:
        # Make sure settings are loaded before the app starts.
        get_settings()

    kwargs: dict[str, Any] = {"stateless_http": stateless} if stateless else {}
    if mcp_path is not None:
        kwargs["streamable_http_path"] = mcp_path
    if transport_security is not None:
        kwargs["transport_security"] = transport_security

    app = Starlette(
        routes=[Mount(mount_path, app=mcp.streamable_http_app(**kwargs))],
        lifespan=_lifespan,
    )
    return app


#: Default ASGI app for ``uvicorn pet_hospital_mcp.server:app``.
app: Starlette = build_app()


__all__ = [
    "app",
    "build_app",
    "get_client",
    "get_settings",
    "set_settings",
    "mcp",
]
