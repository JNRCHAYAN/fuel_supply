"""Allocation ledger and cancellation — the two unreachable simulator calls.

CONTRACT.md section 9 rows: `GET /api/v1/allocations`,
`POST /api/v1/allocations/{id}/cancel`. They are the two halves of the operator's
view of *what has actually been dispatched*:

* `GET /api/v1/allocations` is what the integration guide calls "your shipment
  ledger" (guide section 4.10): every allocation this client has created, sorted
  id-desc, each row carrying its `idempotency_key`, its `status` and the tick
  fields (`created_tick`, `departure_tick`, `expected_arrival_tick`,
  `actual_arrival_tick`). Until now `SimulatorClient.get_allocations` had no
  caller, so the ledger was unreachable from the console.
* `POST /api/v1/allocations/{id}/cancel` (guide section 5.5) refunds the depot's
  inventory and marks a **PENDING** allocation CANCELLED. Until now
  `SimulatorClient.cancel_allocation` had no caller either.

Two rules shape the cancellation route:

1.  Cancellation is consequential (brief section 24: "preserve human review for
    consequential simulated decisions"), so it carries the same explicit
    `{"confirm": true}` gate as `POST /api/v1/recommendations/{id}/submit`. A
    request without it is a 400 raised *before* anything is read or written, so
    the guard cannot be bypassed by an id that would otherwise have succeeded.
2.  Guide section 5.4: "Cancellation does NOT free the [idempotency] key. Once
    used, an idempotency_key is permanently occupied." Nothing here creates,
    derives, reuses or retires a key: cancellation is addressed by allocation id
    alone and touches no key.

The simulator's own 404 `ALLOCATION_NOT_FOUND` and 409 `CANNOT_CANCEL` are the
honest answers and must reach the operator unchanged, so both calls go through
`simulator_guard`, which preserves a 4xx the simulator chose (see
`simulator_error_to_http` in `app.api.schemas`).

The response models live here rather than in `app/api/schemas.py` because that
module is owned by another workstream for this change; they follow its
conventions (permissive `extra="allow"`, `simulated: true` on every body).
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import (
    api_error,
    get_logger,
    get_metrics,
    get_simulator_client,
    simulator_guard,
    to_jsonable,
)

logger = get_logger(__name__)

router = APIRouter(tags=["allocations"])

_ALLOW_EXTRA = ConfigDict(extra="allow")

#: Guide section 4.10: `status` ∈ {PENDING, IN_TRANSIT, ARRIVED, FAILED,
#: CANCELLED}. Pinned as a `Literal` rather than a free string so an unknown
#: filter is a 422 naming the allowed values instead of silently returning an
#: empty ledger that looks like "there are no allocations".
AllocationStatus = Literal["PENDING", "IN_TRANSIT", "ARRIVED", "FAILED", "CANCELLED"]

#: The simulator exposes no `limit` on `/v1/allocations` (guide 4.10), so the
#: page is cut here. The default matches the other list routes; the ceiling is
#: the guide's own demand-history ceiling (section 4.11, limit clamped to 2000).
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


# ---------------------------------------------------------------------------
# Response models (guide sections 4.10 and 5.5)
# ---------------------------------------------------------------------------


class AllocationOut(BaseModel):
    """One row of the shipment ledger, field for field per guide section 4.10.

    Every field carries a default so a partial peer payload degrades the display
    rather than failing the request (CONTRACT.md 5.2).
    """

    id: int | None = None
    idempotency_key: str | None = None
    source_depot_id: str | None = None
    destination_station_id: str | None = None
    route_id: str | None = None
    fuel_type: str | None = None
    quantity: float | None = None
    created_tick: int | None = None
    departure_tick: int | None = None
    expected_arrival_tick: int | None = None
    actual_arrival_tick: int | None = None
    status: str | None = None
    failure_reason: str | None = None
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class AllocationsResponse(BaseModel):
    """The ledger: `GET /api/v1/allocations`, sorted id-desc by the simulator."""

    count: int = 0
    status: str | None = None
    limit: int = DEFAULT_LIMIT
    allocations: list[AllocationOut] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class CancelRequest(BaseModel):
    """`confirm` is required and must be literally true.

    `strict=True` matters for the same reason as `SubmitRequest`: without it
    Pydantic v2 would coerce `"yes"` or `1` into `True`, and the operator's
    explicit acknowledgement would stop being explicit.
    """

    confirm: bool = Field(
        default=False,
        strict=True,
        description="Explicit operator acknowledgement. Must be the boolean true; anything else is a 400.",
    )
    model_config = _ALLOW_EXTRA


class CancelResponse(BaseModel):
    """`200` with the updated allocation (guide section 5.5).

    `idempotency_key` travels inside `allocation`, unchanged: it stays occupied
    forever (guide section 5.4), the cancellation merely changes `status`.
    """

    cancelled: bool
    allocation_id: int
    allocation: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get(
    "/api/v1/allocations",
    response_model=AllocationsResponse,
    summary="Shipment ledger (guide section 4.10)",
)
async def list_allocations(
    status: Annotated[AllocationStatus | None, Query(description="Filter to one allocation status")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AllocationsResponse:
    """Proxy of `GET /v1/allocations`, filtered and paged per guide section 4.10.

    The simulator returns the whole ledger, id-desc. The `status` filter and the
    `limit` are applied here, *after* the sort, so a page is still id-desc and
    the filter can never reorder it. Filtering and cutting locally (rather than
    trusting the upstream) is deliberate: the contract gives `get_allocations`
    no query parameters, so this is the only place the page can be cut.
    """
    rows = await simulator_guard(
        "get_allocations", _maybe_await(client.get_allocations()), metrics=metrics
    )
    payload = to_jsonable(rows) or []
    if not isinstance(payload, list):
        payload = [payload]
    rows_out = [row for row in payload if isinstance(row, dict)]
    if status is not None:
        rows_out = [
            row for row in rows_out if str(row.get("status", "")).upper() == status
        ]
    return AllocationsResponse(
        count=len(rows_out[:limit]),
        status=status,
        limit=limit,
        allocations=rows_out[:limit],
        simulated=True,
    )


@router.post(
    "/api/v1/allocations/{allocation_id}/cancel",
    response_model=CancelResponse,
    summary="Cancel a PENDING allocation (guide section 5.5)",
)
async def cancel_allocation(
    allocation_id: Annotated[int, Path(ge=1, description="Allocation id from GET /api/v1/allocations")],
    body: CancelRequest | None = Body(default=None),
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> CancelResponse:
    """Refund a PENDING allocation. Requires an explicit `confirm: true`.

    Brief section 24: a consequential simulated decision keeps its human review,
    so a request without the operator's explicit confirmation is rejected with
    400 *before* the simulator is read or written to.

    The status rule is the simulator's to enforce (guide section 5.5): cancelling
    anything that is not PENDING is its `409 CANNOT_CANCEL`, and an unknown id is
    its `404 ALLOCATION_NOT_FOUND`. Both are passed straight through by
    `simulator_guard` rather than being flattened, because the code in the body
    is what tells the operator whether to re-check the ledger (404) or wait for
    the allocation to depart or arrive (409).

    No `idempotency_key` is sent, derived or retired: guide section 5.4 makes a
    used key permanently occupied, so cancelling must not — and does not — put it
    back into circulation.
    """
    if body is None or body.confirm is not True:
        raise api_error(
            400,
            "confirmation_required",
            "cancellation requires an explicit {'confirm': true} field; "
            "cancelling refunds depot inventory and is never automatic",
            details={"allocation_id": allocation_id},
        )

    allocation = await simulator_guard(
        "cancel_allocation",
        _maybe_await(client.cancel_allocation(allocation_id)),
        metrics=metrics,
    )
    payload = to_jsonable(allocation)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return CancelResponse(
        cancelled=str(payload.get("status", "")).upper() == "CANCELLED",
        allocation_id=allocation_id,
        allocation=payload,
        simulated=True,
    )
