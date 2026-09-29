"""Simulator scenario control — the demo surface for the resilience story.

CONTRACT.md section 9 rows: `GET /api/v1/admin/faults`, `POST /api/v1/admin/faults`,
`POST /api/v1/admin/simulation/{action}`.

Every route here is an operator action against the **simulator** (CONTRACT.md
section 0.1). Nothing here can reach anything but the published simulation.

Note for the workstream report: `SimulatorClient.admin_clear_faults` exists in
CONTRACT.md 5.4 but has no route in the section 9 table, so it is deliberately
not exposed here rather than inventing a path.
"""

from __future__ import annotations

import inspect
from typing import Any, Literal

from fastapi import APIRouter, Depends, Request

from app.api.schemas import (
    AdminFaultCreatedResponse,
    AdminFaultsResponse,
    FaultIn,
    SimulationActionResponse,
    api_error,
    build_peer_model,
    get_logger,
    get_metrics,
    get_simulator_client,
    simulator_guard,
    to_jsonable,
)

logger = get_logger(__name__)

router = APIRouter(tags=["admin"])

SimulationAction = Literal["run", "pause", "step", "reset"]

#: `FaultRequest` in A2's models requires a duration; the simulator's live
#: `FaultCreate` schema defaults it too. Five simulator ticks is long enough to
#: watch the degradation and short enough not to poison a demo.
DEFAULT_FAULT_DURATION_SECONDS = 300

#: Fields of `FaultRequest` itself. Anything else the operator sends (a
#: `depot_id`, a `route_id`, a multiplier) is the fault's own parameterisation
#: and belongs in `parameters`, which is where the simulator's schema reads it.
_FAULT_ENVELOPE_FIELDS = ("type", "duration_seconds", "parameters")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


@router.get("/api/v1/admin/faults", response_model=AdminFaultsResponse, summary="Injected faults")
async def list_faults(
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminFaultsResponse:
    faults = await simulator_guard(
        "admin_get_faults", _maybe_await(client.admin_get_faults()), metrics=metrics
    )
    rows = to_jsonable(faults) or []
    if not isinstance(rows, list):
        rows = [rows]
    rows = [row for row in rows if isinstance(row, dict)]
    return AdminFaultsResponse(count=len(rows), faults=rows, simulated=True)


@router.post(
    "/api/v1/admin/faults",
    response_model=AdminFaultCreatedResponse,
    summary="Inject a fault (demonstration of the resilience path)",
)
async def inject_fault(
    body: FaultIn,
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminFaultCreatedResponse:
    payload = body.model_dump()
    # Fold the operator's fault-specific fields into `parameters`: `FaultRequest`
    # is `{type, duration_seconds, parameters}`, so a `depot_id` sent at the top
    # level has to travel inside `parameters` to reach the simulator at all.
    parameters = dict(payload.get("parameters") or {})
    for key, value in payload.items():
        if key not in _FAULT_ENVELOPE_FIELDS:
            parameters[key] = value
    payload["parameters"] = parameters
    payload.setdefault("duration_seconds", DEFAULT_FAULT_DURATION_SECONDS)
    if payload.get("duration_seconds") is None:
        payload["duration_seconds"] = DEFAULT_FAULT_DURATION_SECONDS
    request = build_peer_model("app.sim.models", ("FaultRequest",), payload)
    if isinstance(request, dict):
        # A2's client does `req.to_payload()`, so a dict would be an
        # AttributeError deep inside the gateway. Name the failure here instead.
        raise api_error(
            503,
            "fault_request_unavailable",
            "A2's FaultRequest could not be constructed from this fault",
            details={"type": payload.get("type")},
        )
    fault = await simulator_guard(
        "admin_inject_fault",
        _maybe_await(client.admin_inject_fault(request)),
        metrics=metrics,
    )
    fault_payload = to_jsonable(fault) or {}
    if not isinstance(fault_payload, dict):
        fault_payload = {"value": fault_payload}
    return AdminFaultCreatedResponse(fault=fault_payload, simulated=True)


@router.post(
    "/api/v1/admin/simulation/{action}",
    response_model=SimulationActionResponse,
    summary="run / pause / step / reset the simulation",
)
async def simulation_action(
    action: SimulationAction,
    request: Request,
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> SimulationActionResponse:
    """An unknown action is rejected with 422 by the path validator itself."""
    method = getattr(client, f"admin_{action}", None)
    if not callable(method):
        # A typed 503 names the missing capability; it must not look like a href
        # that exists and 404s, nor like an unhandled 500.
        raise api_error(
            503,
            "admin_action_unavailable",
            f"simulator client does not implement admin_{action}",
            details={"action": action},
        )
    result = await simulator_guard(
        f"admin_{action}", _maybe_await(method()), metrics=metrics
    )
    payload = to_jsonable(result) or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return SimulationActionResponse(action=action, result=payload, simulated=True)
