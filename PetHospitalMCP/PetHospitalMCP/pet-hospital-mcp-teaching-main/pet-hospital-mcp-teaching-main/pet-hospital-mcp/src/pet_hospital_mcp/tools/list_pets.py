"""``list_pets`` — the only MCP tool in Stage 1.

Adapts the Go REST API ``GET /api/v1/pets`` endpoint to MCP. The Go
service supports 13 query parameters; this tool exposes them 1:1 with
strict validation against the real backend's allowed values.

Input validation rules (enforced before the upstream is called)
----------------------------------------------------------------
* ``species``  must be one of the species the Go ``/api/v1/meta`` lists.
* ``status``   must be one of the visit-status enums the Go server uses.
* ``sortBy``   must be one of the Go ``sortFields``.
* ``order``    must be ``asc`` or ``desc``.
* ``page``     must be >= 1.
* ``pageSize`` must be in ``[1, 500]``.
* ``min``      and ``max`` must be non-negative finite numbers, and
               ``min`` must be <= ``max`` when both are provided.
* Unknown fields, ``NaN``, ``Infinity``, ``-Infinity``, and wrong types
  are rejected by Pydantic's strict mode.

Error handling
--------------
* Schema-level failures are auto-rejected by the MCP Python SDK 2.x
  (``is_error=True`` with the Pydantic message in ``content``) — the
  SDK 2.x failure surface, using the snake_case ``is_error`` attribute,
  not the 1.x ``CallToolResult.isError`` spelling.
* Cross-field failures (``min > max``) and every upstream failure are
  wrapped in :class:`~pet_hospital_mcp.errors.ToolError`, whose string
  form is the unified JSON envelope, so the model can read the same
  shape from every backend-side failure.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..errors import (
    BACKEND_INVALID_RESPONSE,
    INTERNAL_ERROR,
    VALIDATION_ERROR,
    ToolError,
)
from ..logging_config import make_tool_record
from ..rest_client import PetHospitalClient

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums — mirror the Go API's /api/v1/meta (see internal/api/api.go:330).
# ---------------------------------------------------------------------------

Species = Literal["犬", "猫", "兔", "鸟", "仓鼠", "爬宠", "其他"]
VisitStatus = Literal["待就诊", "就诊中", "住院中", "已康复", "慢性病随访"]
SortBy = Literal[
    "id",
    "name",
    "ownerName",
    "species",
    "doctor",
    "disease",
    "status",
    "totalCost",
    "visitCount",
    "createdAt",
    "updatedAt",
]
Order = Literal["asc", "desc"]

# ---------------------------------------------------------------------------
# Output models — must accept the Go API's real JSON, including
# ``records`` / ``charges`` being either ``null`` or an array.
# ---------------------------------------------------------------------------


class MedicalRecord(BaseModel):
    """One historical visit record."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    visit_date: str | None = Field(default=None, alias="visitDate")
    doctor: str | None = None
    diagnosis: str | None = None
    symptoms: str | None = Field(default=None, alias="symptoms")
    treatment: str | None = Field(default=None, alias="treatment")
    prescription: list[str] | None = None
    weight_kg: float | None = Field(default=None, alias="weightKg")
    temperature: float | None = None
    follow_up: str | None = Field(default=None, alias="followUp")
    charge: float | None = None
    created_at: str | None = Field(default=None, alias="createdAt")


class Treatment(BaseModel):
    """One billing line item."""

    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    item: str | None = None
    category: str | None = None
    amount: float | None = None
    doctor: str | None = None
    date: str | None = None
    note: str | None = None


class PetSummary(BaseModel):
    """A single pet record as returned by ``GET /api/v1/pets``.

    The Go ``Pet`` model has ``records`` and ``charges`` fields whose
    JSON can be either ``null`` or an array; Pydantic accepts both via
    ``default=None`` and a coercing validator that maps ``None`` -> ``None``
    (kept as ``None`` so callers can distinguish "no records" from "an
    empty list").
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str
    name: str
    species: str | None = None
    breed: str | None = None
    gender: str | None = None
    age_months: int | None = Field(default=None, alias="ageMonths")
    color: str | None = None
    chip_no: str | None = Field(default=None, alias="chipNo")

    owner_name: str = Field(alias="ownerName")
    owner_phone: str = Field(alias="ownerPhone")
    owner_addr: str | None = Field(default=None, alias="ownerAddr")

    doctor: str | None = None
    disease: str | None = None
    status: str | None = None
    allergy: str | None = None
    note: str | None = None

    records: list[MedicalRecord] | None = None
    charges: list[Treatment] | None = None

    total_cost: float | None = Field(default=None, alias="totalCost")
    visit_count: int | None = Field(default=None, alias="visitCount")

    created_at: str | None = Field(default=None, alias="createdAt")
    updated_at: str | None = Field(default=None, alias="updatedAt")

    @field_validator("records", "charges", mode="before")
    @classmethod
    def _coerce_null_arrays(cls, value: Any) -> Any:
        """Accept the Go API's ``null`` for these fields without error."""

        # Pydantic already accepts ``None`` for ``Optional[list[...]]``;
        # this validator exists to make the contract explicit and to
        # reject non-list, non-None values early.
        if value is None:
            return None
        if isinstance(value, list):
            return value
        raise ValueError("expected an array or null")


class ListPetsOutput(BaseModel):
    """Successful return shape for the ``list_pets`` tool.

    Mirrors the ``data`` field of ``GET /api/v1/pets`` from the Go
    REST API (see internal/store/store.go:104, ``Result`` struct).
    """

    model_config = ConfigDict(populate_by_name=True)

    items: list[PetSummary] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = Field(default=20, alias="pageSize")
    total_pages: int = Field(default=1, alias="totalPages")
    total_cost: float = Field(default=0.0, alias="totalCost")

    @field_validator("items", mode="before")
    @classmethod
    def _items_to_list(cls, value: Any) -> Any:
        """Be defensive if the upstream ever returns ``null`` for items."""

        if value is None:
            return []
        return value


# ---------------------------------------------------------------------------
# Input model
# ---------------------------------------------------------------------------


class ListPetsInput(BaseModel):
    """Strictly validated input for the ``list_pets`` MCP tool.

    Only the 13 query parameters the Go API supports are accepted.
    Unknown fields, NaN, Infinity, and wrong types are rejected.
    """

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        use_enum_values=False,
        populate_by_name=True,
    )

    q: str | None = Field(
        default=None,
        description=(
            "Free-text full-text search across pet fields and medical "
            "history. Multiple terms are AND-ed."
        ),
    )
    name: str | None = Field(default=None, description="Filter by pet name (fuzzy).")
    owner_name: str | None = Field(
        default=None,
        alias="ownerName",
        description="Filter by owner name (fuzzy).",
    )
    owner_phone: str | None = Field(
        default=None,
        alias="ownerPhone",
        description="Filter by owner phone (fuzzy).",
    )
    species: Species | None = Field(
        default=None, description="Filter by species (exact match)."
    )
    doctor: str | None = Field(
        default=None, description="Filter by treating doctor (fuzzy)."
    )
    disease: str | None = Field(
        default=None, description="Filter by disease (fuzzy)."
    )
    status: VisitStatus | None = Field(
        default=None, description="Filter by visit status (exact match)."
    )
    min: float | None = Field(
        default=None,
        ge=0,
        description="Lower bound on total spent at the hospital (inclusive).",
    )
    max: float | None = Field(
        default=None,
        ge=0,
        description="Upper bound on total spent at the hospital (inclusive).",
    )
    sort_by: SortBy | None = Field(
        default=None,
        alias="sortBy",
        description="Sort field. Defaults to the upstream's default (id).",
    )
    order: Order | None = Field(
        default=None, description="Sort order, ascending or descending."
    )
    page: int = Field(
        default=1,
        ge=1,
        description="1-indexed page number. Must be >= 1.",
    )
    page_size: int = Field(
        default=20,
        ge=1,
        le=500,
        alias="pageSize",
        description="Number of records per page. Must be in [1, 500].",
    )

    # Reject NaN / Infinity on the numeric fields. ``strict=True`` already
    # rejects strings-as-numbers; this validator adds the finiteness guard
    # because Python floats can carry those values from JSON.
    @field_validator("min", "max", mode="before")
    @classmethod
    def _reject_non_finite(cls, value: Any) -> Any:
        if value is None:
            return None
        # bool is a subtype of int; reject explicitly because passing
        # ``true`` for ``min`` is a type error in spirit.
        if isinstance(value, bool):
            raise ValueError("must be a number, not a boolean")
        if not isinstance(value, (int, float)):
            raise ValueError("must be a number")
        as_float = float(value)
        if not math.isfinite(as_float):
            raise ValueError("must be a finite number (NaN/Infinity rejected)")
        return as_float

    @field_validator("page", "page_size", mode="before")
    @classmethod
    def _reject_bool_for_int(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("must be an integer, not a boolean")
        return value

    @model_validator(mode="after")
    def _check_min_max(self) -> "ListPetsInput":
        if (
            self.min is not None
            and self.max is not None
            and self.min > self.max
        ):
            raise ValueError("min must be <= max")
        return self

    def to_query_params(self) -> dict[str, Any]:
        """Build the query-string dict to forward to ``GET /api/v1/pets``.

        ``None`` and unset fields are omitted so the Go API uses its own
        defaults. The keys use the exact (camelCase) names the Go API
        expects (see ``buildQuery`` in internal/api/api.go:271).
        """

        params: dict[str, Any] = {
            "page": self.page,
            "pageSize": self.page_size,
        }
        if self.q is not None:
            params["q"] = self.q
        if self.name is not None:
            params["name"] = self.name
        if self.owner_name is not None:
            params["ownerName"] = self.owner_name
        if self.owner_phone is not None:
            params["ownerPhone"] = self.owner_phone
        if self.species is not None:
            params["species"] = self.species
        if self.doctor is not None:
            params["doctor"] = self.doctor
        if self.disease is not None:
            params["disease"] = self.disease
        if self.status is not None:
            params["status"] = self.status
        if self.min is not None:
            params["min"] = self.min
        if self.max is not None:
            params["max"] = self.max
        if self.sort_by is not None:
            params["sortBy"] = self.sort_by
        if self.order is not None:
            params["order"] = self.order
        return params


# ---------------------------------------------------------------------------
# Tool entry point
# ---------------------------------------------------------------------------

TOOL_NAME = "list_pets"

TOOL_DESCRIPTION = """List pet records from the Pet Hospital REST API.

Wraps ``GET /api/v1/pets`` on the upstream Go service. Use this tool to
look up pets in the hospital's archive. All 13 query parameters supported
by the backend are accepted and forwarded as-is; nothing private to the
MCP layer is added.

Common scenarios
----------------
* Paginated browse: ``page=1&pageSize=20&sortBy=totalCost&order=desc``.
* Filter by owner: ``ownerName=张三`` or ``ownerPhone=13800001111``.
* Filter by doctor / species / status / disease.
* Range by total spend: ``min=500&max=5000``.
* Full-text across pet fields and medical history: ``q=肠胃炎``.

Return value
------------
A JSON object with ``items``, ``total``, ``page``, ``pageSize``,
``totalPages`` and ``totalCost`` (sum of ``totalCost`` over the matched
set). Each item in ``items`` is the upstream ``Pet`` model; ``records``
and ``charges`` may be ``null`` or an array.

Failures are reported as a structured error envelope:
``{"error": {"code": "...", "message": "...", "details": {}}}``.
""".strip()


async def list_pets(
    input: ListPetsInput,
    *,
    client: PetHospitalClient,
) -> ListPetsOutput:
    """Call the upstream ``GET /api/v1/pets`` and return a typed model.

    The caller (the MCP tool registration in :mod:`pet_hospital_mcp.server`)
    is responsible for constructing the :class:`PetHospitalClient` so the
    function can be exercised in tests with an injected mock transport.
    """

    started = time.perf_counter()
    params = input.to_query_params()
    status_label = "ok"

    try:
        payload = await client.list_pets(params)
    except ToolError:
        status_label = "error"
        raise
    except Exception as exc:  # pragma: no cover - defensive last resort
        status_label = "error"
        raise ToolError(
            INTERNAL_ERROR,
            f"Unexpected error while calling list_pets: {type(exc).__name__}",
            {"tool": TOOL_NAME},
        ) from exc
    finally:
        duration_ms = (time.perf_counter() - started) * 1000
        make_tool_record(
            _log,
            level=logging.INFO if status_label == "ok" else logging.WARNING,
            tool_name=TOOL_NAME,
            params=params,
            status=status_label,
            duration_ms=duration_ms,
        )

    # Validate the upstream payload against our typed model. A structural
    # mismatch here means the Go API changed shape unexpectedly; surface
    # it as BACKEND_INVALID_RESPONSE rather than letting Pydantic's
    # ValidationError leak to the MCP client.
    try:
        output = ListPetsOutput.model_validate(payload)
    except Exception as exc:
        raise ToolError(
            BACKEND_INVALID_RESPONSE,
            f"Upstream response did not match the expected data model: "
            f"{type(exc).__name__}",
            {"tool": TOOL_NAME, "path": "/api/v1/pets"},
        ) from exc

    return output


__all__ = [
    "TOOL_NAME",
    "TOOL_DESCRIPTION",
    "ListPetsInput",
    "ListPetsOutput",
    "PetSummary",
    "MedicalRecord",
    "Treatment",
    "list_pets",
]


# Some linters complain about ``asyncio`` being unused; it is imported
# intentionally as a placeholder for future streaming support (and so
# that removing it would be a deliberate act in stage 2).
_ = asyncio
