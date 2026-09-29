"""Simulator scenario control — the operator-facing self-test surface.

CONTRACT.md section 9 rows: `GET /api/v1/admin/faults`, `POST /api/v1/admin/faults`,
`POST /api/v1/admin/simulation/{action}`.

Guide section 7 says the simulator's `/admin/*` API is "designed for organizer
use, but participants will find them invaluable for self-testing". This module
is that self-test surface, widened from the three contract rows to the whole of
guide section 7, so that brief section 22's demonstration script — "a crisis
event occurs", "application or dependency failure is injected", "monitoring
detects failure" — can be driven entirely through *this* backend instead of by
hand against the simulator:

    POST /api/v1/admin/simulation/{action}   run/pause/step/reset (guide 7.2-7.6)
    POST /api/v1/admin/toggle                flip RUNNING <-> PAUSED (guide 7.4)
    POST /api/v1/admin/events                inject a crisis event (guide 7.7, 7.8)
    GET  /api/v1/admin/events                the event timeline   (guide 7.13)
    POST /api/v1/admin/faults                inject a fault       (guide 7.9, 7.10)
    GET  /api/v1/admin/faults                active/recent faults (guide 7.13)
    POST /api/v1/admin/faults/clear          clear every fault    (guide 7.11)
    GET  /api/v1/admin/audit                 the audit log        (guide 7.12)

Guide section 7.5 calls `/admin/step` "the recommended way to drive deterministic
tests", so the whole reproducible sequence — pause, submit allocations, step a
known number of ticks — is reachable from here. Guide section 7.12's audit log is
what makes brief section 20's "decision audit history" demonstrable: it is the
simulator's own ground-truth trail of everything it did, next to ours.

Every route here is an operator action against the **simulator** (CONTRACT.md
section 0.1). Nothing here can reach anything but the published simulation.

Guardrails (brief section 24). The file's existing read/write split is preserved
and made explicit: inspection is GET and never touches simulator state; every
action that changes simulated state is a POST carrying a validated body, and
every response carries `simulated: true`. Nothing outside the simulation is
reachable, and nothing here can create a real purchase or dispatch.

Validation (brief section 18). Event and fault payloads are validated against
the *documented* shapes before they are forwarded. That is not ceremony: the
simulator rejects unknown event types and out-of-range durations with a 422,
which reaches the operator through `simulator_guard` as a 502 `simulator_error`
— a message that names nothing the operator typed. Catching it here turns a
misleading transport failure into a 422 that names the offending field.

Note for the workstream report: `app/api/schemas.py` carries a response model for
each of the three contract rows but none for guide section 7's wider surface, and
that module is owned by another workstream. The shapes below are therefore
declared locally, in the same style (`extra="allow"`, `simulated` defaulted to
true) as the ones they would otherwise live beside.
"""

from __future__ import annotations

import inspect
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from app.api.schemas import (
    AdminFaultCreatedResponse,
    AdminFaultsResponse,
    EventsResponse,
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

_ALLOW_EXTRA = ConfigDict(extra="allow")

# ---------------------------------------------------------------------------
# Guide section 7.8 — the simulator's `EventCreate.type` enum, all six values.
# ---------------------------------------------------------------------------

EventType = Literal[
    "demand_spike",
    "shipment_delay",
    "route_disruption",
    "station_outage",
    "depot_constraint",
    "supply_shortfall",
]

EVENT_TYPES: tuple[str, ...] = (
    "demand_spike",
    "shipment_delay",
    "route_disruption",
    "station_outage",
    "depot_constraint",
    "supply_shortfall",
)

# ---------------------------------------------------------------------------
# Guide section 7.10 — the simulator's `FaultCreate.type` enum, all five values.
# ---------------------------------------------------------------------------

FAULT_TYPES: tuple[str, ...] = (
    "latency",
    "unavailable",
    "error_rate",
    "stale_data",
    "stream_disconnect",
)

#: `FaultRequest` in A2's models requires a duration; the simulator's live
#: `FaultCreate` schema requires it too. Five simulator ticks is long enough to
#: watch the degradation and short enough not to poison a demo.
DEFAULT_FAULT_DURATION_SECONDS = 300

#: Guide section 7.9: `0 < duration_seconds <= 3600`. Enforced here so an
#: out-of-range fault is a 422 naming the field, not a 502 relayed from the
#: simulator.
FAULT_DURATION_MIN_SECONDS = 0
FAULT_DURATION_MAX_SECONDS = 3600

#: Guide section 7.12: `limit` is clamped to [1, 1000] with a default of 200.
AUDIT_LIMIT_DEFAULT = 200
AUDIT_LIMIT_MIN = 1
AUDIT_LIMIT_MAX = 1000

#: Fields of `FaultRequest` itself. Anything else the operator sends (a
#: `depot_id`, a `route_id`, a multiplier) is the fault's own parameterisation
#: and belongs in `parameters`, which is where the simulator's schema reads it.
_FAULT_ENVELOPE_FIELDS = ("type", "duration_seconds", "parameters")

#: Fields of `EventRequest` itself (guide section 7.7). As with faults, anything
#: else the operator sends is the event's own parameterisation and is folded into
#: `parameters`, which is where the simulator's `EventCreate` schema reads it.
_EVENT_ENVELOPE_FIELDS = ("type", "start_tick", "duration_ticks", "parameters")

#: Guide section 7.8, transcribed per event type: the filter lists it reads and
#: the scalars it reads. `type` is checked against this table, so an invented
#: parameter name for a given type is a 422 rather than a silently ignored key —
#: which is the difference between "a spike scoped to R-1" and "a spike across
#: the whole network".
_EVENT_PARAMETER_SHAPES: dict[str, dict[str, tuple[str, ...]]] = {
    "demand_spike": {"lists": ("station_ids", "region_ids"), "scalars": ("multiplier",)},
    "route_disruption": {"lists": ("route_ids",), "scalars": ()},
    "station_outage": {"lists": ("station_ids",), "scalars": ()},
    "depot_constraint": {"lists": ("depot_ids",), "scalars": ()},
    "shipment_delay": {"lists": ("depot_ids", "fuel_types"), "scalars": ("delay_ticks",)},
    "supply_shortfall": {"lists": ("depot_ids", "fuel_types"), "scalars": ("factor",)},
}

#: The defaults guide section 7.8 documents, applied when the operator omits the
#: scalar. Sending them explicitly means the request the simulator receives says
#: what it means, rather than relying on the simulator's own default.
_EVENT_SCALAR_DEFAULTS: dict[str, float] = {
    "multiplier": 1.5,
    "delay_ticks": 2,
    "factor": 0.5,
}

#: `(minimum, exclusive)` for every scalar above. Guide section 7.8 reverses a
#: `demand_spike` by *dividing* by the multiplier, so a multiplier of zero (or a
#: negative one) has no defined reversal and is rejected at the boundary.
_EVENT_SCALAR_BOUNDS: dict[str, tuple[float, bool]] = {
    "multiplier": (0.0, True),
    "delay_ticks": (0.0, False),
    "factor": (0.0, True),
}

#: The singular spellings the wire does **not** have. Guide section 7.8 names
#: every filter in the plural (`region_ids`, `station_ids`, ...); a singular key
#: is silently ignored by the simulator, so an operator who sent one would
#: believe the crisis was scoped to a single region while it actually hit all of
#: them. Rejecting it is the honest answer.
_SINGULAR_PARAMETER_ALIASES: dict[str, str] = {
    "region_id": "region_ids",
    "station_id": "station_ids",
    "route_id": "route_ids",
    "depot_id": "depot_ids",
    "fuel_type": "fuel_types",
}

#: Guide section 7.10: the one documented knob per fault type, and its default.
_FAULT_PARAMETER_DEFAULTS: dict[str, dict[str, Any]] = {
    "latency": {"delay_ms": 500},
    "error_rate": {"rate": 0.25},
}


# ---------------------------------------------------------------------------
# Locally declared models (see the module docstring: `schemas.py` is owned by
# another workstream and carries no model for guide section 7's wider surface).
# ---------------------------------------------------------------------------


class EventIn(BaseModel):
    """Body of `POST /api/v1/admin/events` (guide section 7.7).

    `start_tick >= 0` and `duration_ticks > 0` are the constraints guide section
    7.7 pins on the simulator's own `EventCreate`, and guide section 9 lists
    `duration_ticks=0` among the payloads that must come back as a 422. Enforcing
    them here is what makes that 422 *ours* — a 422 from the simulator would
    reach the operator as a 502 `simulator_error` instead.

    `extra="allow"` follows this file's existing convention for `FaultIn`: an
    event parameter sent at the top level is folded into `parameters` rather than
    dropped, because that is where the simulator's schema reads it.
    """

    type: EventType
    start_tick: int = Field(..., ge=0, description="Tick the event starts on. Guide 7.7: >= 0.")
    duration_ticks: int = Field(
        ...,
        gt=0,
        description="Ticks the event stays ACTIVE. Guide 7.7: > 0; 0 is a 422.",
    )
    parameters: dict[str, Any] = Field(default_factory=dict)
    model_config = _ALLOW_EXTRA


class AdminEventCreatedResponse(BaseModel):
    event: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class AdminAuditResponse(BaseModel):
    """Guide section 7.12: the simulator's ground-truth audit log, id-desc."""

    count: int = 0
    limit: int = AUDIT_LIMIT_DEFAULT
    entries: list[dict[str, Any]] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class AdminFaultsClearedResponse(BaseModel):
    """Guide section 7.11. Idempotent: clearing with no active fault is fine."""

    cleared: bool = True
    result: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class AdminToggleResponse(BaseModel):
    """Guide section 7.4's RUNNING <-> PAUSED flip.

    Its own model rather than `SimulationActionResponse`, whose `action` field is
    pinned to the four contract verbs in `app/api/schemas.py` — a file this
    module does not own.
    """

    action: Literal["toggle"] = "toggle"
    status: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _require_capability(client: Any, method_name: str, action: str) -> Any:
    """Return `client.<method_name>`, or a typed 503 naming the missing piece.

    A capability the client does not implement must not look like a path that
    exists and 404s, nor like an unhandled 500. This is deliberately resolved
    with `getattr` rather than a direct attribute access: `admin_toggle` and
    `admin_get_events` are arriving in the client concurrently with this module,
    and an operator running against an older client should get a typed 503, not
    an AttributeError.
    """
    method = getattr(client, method_name, None)
    if callable(method):
        return method
    raise api_error(
        503,
        "admin_action_unavailable",
        f"simulator client does not implement {method_name}",
        details={"action": action},
    )


def _as_rows(value: Any) -> list[dict[str, Any]]:
    rows = to_jsonable(value) or []
    if not isinstance(rows, list):
        rows = [rows]
    return [row for row in rows if isinstance(row, dict)]


def _as_payload(value: Any) -> dict[str, Any]:
    payload = to_jsonable(value) or {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return payload


def _id_descending(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort id-descending (guide 7.12), tolerating a row whose id is not a number.

    The order is a promise this API makes to the console, so it is applied here
    rather than assumed of the simulator.
    """

    def key(row: Mapping[str, Any]) -> tuple[int, float]:
        try:
            return (0, float(row.get("id") or 0))
        except (TypeError, ValueError):
            return (1, 0.0)

    return sorted(rows, key=key, reverse=True)


def _coerce_whole_seconds(value: Any) -> int | None:
    """A whole number of seconds, or None when the operator sent something else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value)


def _validate_event_parameters(
    event_type: str, parameters: Mapping[str, Any] | None
) -> dict[str, Any]:
    """Check one event's parameters against guide section 7.8.

    The rule that matters most here is the one guide section 7.8 states in a
    single sentence: *"station_ids / region_ids / route_ids / depot_ids are
    filters: if the list is empty, the event applies to all entities of that
    type."* An empty list is therefore **valid input**, not a missing scope, and
    it is forwarded verbatim.
    """
    shape = _EVENT_PARAMETER_SHAPES[event_type]
    given = dict(parameters or {})

    for singular, plural in _SINGULAR_PARAMETER_ALIASES.items():
        if singular in given:
            raise api_error(
                422,
                "invalid_event_parameters",
                f"'{singular}' is not a {event_type} parameter; guide section 7.8 "
                f"spells that filter '{plural}' (a list, empty meaning all)",
                details={"type": event_type, "received": singular, "expected": plural},
            )

    accepted = set(shape["lists"]) | set(shape["scalars"])
    unknown = sorted(set(given) - accepted)
    if unknown:
        raise api_error(
            422,
            "invalid_event_parameters",
            f"{event_type} does not take {', '.join(unknown)}",
            details={
                "type": event_type,
                "unexpected": unknown,
                "accepted": sorted(accepted),
            },
        )

    validated: dict[str, Any] = {}

    for name in shape["lists"]:
        if name not in given:
            continue
        value = given[name]
        if not isinstance(value, list) or any(
            not isinstance(item, str) or not item for item in value
        ):
            raise api_error(
                422,
                "invalid_event_parameters",
                f"'{name}' must be a list of non-empty ids; an empty list means "
                "every entity of that type (guide section 7.8)",
                details={"type": event_type, "parameter": name},
            )
        # Forwarded verbatim: `[]` is the documented "all entities" case.
        validated[name] = list(value)

    for name in shape["scalars"]:
        value = given.get(name, _EVENT_SCALAR_DEFAULTS[name])
        minimum, exclusive = _EVENT_SCALAR_BOUNDS[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise api_error(
                422,
                "invalid_event_parameters",
                f"'{name}' must be a number",
                details={"type": event_type, "parameter": name},
            )
        too_small = value <= minimum if exclusive else value < minimum
        if too_small:
            bound = "greater than" if exclusive else "at least"
            raise api_error(
                422,
                "invalid_event_parameters",
                f"'{name}' must be {bound} {minimum}",
                details={"type": event_type, "parameter": name, "value": value},
            )
        if name == "delay_ticks":
            if not float(value).is_integer():
                raise api_error(
                    422,
                    "invalid_event_parameters",
                    "'delay_ticks' must be a whole number of ticks",
                    details={"type": event_type, "parameter": name, "value": value},
                )
            validated[name] = int(value)
        else:
            validated[name] = float(value)

    return validated


def _validate_fault_parameters(fault_type: str, parameters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fill in guide section 7.10's defaults and bound the documented knobs.

    Unknown keys are passed through: this file's `POST /admin/faults` documents
    folding the operator's own fields into `parameters`, and the simulator's
    `FaultCreate.parameters` is an open object. Only the two knobs guide section
    7.10 actually names are type- and range-checked.
    """
    validated = dict(parameters or {})
    for name, default in _FAULT_PARAMETER_DEFAULTS.get(fault_type, {}).items():
        validated.setdefault(name, default)

    if "delay_ms" in validated:
        delay = validated["delay_ms"]
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
            raise api_error(
                422,
                "invalid_fault_parameters",
                "'delay_ms' must be a non-negative number of milliseconds",
                details={"type": fault_type, "parameter": "delay_ms"},
            )

    if "rate" in validated:
        rate = validated["rate"]
        if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0 <= rate <= 1:
            raise api_error(
                422,
                "invalid_fault_parameters",
                "'rate' must be a probability between 0 and 1",
                details={"type": fault_type, "parameter": "rate"},
            )

    return validated


# ---------------------------------------------------------------------------
# Inspection (GET — never touches simulator state)
# ---------------------------------------------------------------------------


@router.get("/api/v1/admin/faults", response_model=AdminFaultsResponse, summary="Injected faults")
async def list_faults(
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminFaultsResponse:
    """Guide section 7.13: the active/recent faults, for the operator timeline."""
    faults = await simulator_guard(
        "admin_get_faults", _maybe_await(client.admin_get_faults()), metrics=metrics
    )
    rows = _id_descending(_as_rows(faults))
    return AdminFaultsResponse(count=len(rows), faults=rows, simulated=True)


@router.get(
    "/api/v1/admin/events",
    response_model=EventsResponse,
    summary="Injected events (guide section 7.13)",
)
async def list_admin_events(
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> EventsResponse:
    """Guide section 7.13: the last 50 events, id-desc.

    Pairs with `POST /api/v1/admin/events` the way `GET /admin/faults` pairs with
    fault injection: inject a crisis, then read back the timeline the simulator
    recorded for it. `/api/v1/events` shows the *world's* view of events; this
    shows the operator's writes.
    """
    method = _require_capability(client, "admin_get_events", "events")
    events = await simulator_guard("admin_get_events", _maybe_await(method()), metrics=metrics)
    rows = _id_descending(_as_rows(events))
    return EventsResponse(count=len(rows), events=rows, simulated=True)


@router.get(
    "/api/v1/admin/audit",
    response_model=AdminAuditResponse,
    summary="Simulator audit log (decision audit history)",
)
async def get_audit(
    limit: int = Query(
        default=AUDIT_LIMIT_DEFAULT,
        description="Rows to return. Clamped to [1, 1000]; default 200 (guide section 7.12).",
    ),
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminAuditResponse:
    """Guide section 7.12: the ground-truth log of everything the simulator did.

    `limit` is **clamped**, not rejected: guide section 7.12 defines the window as
    [1, 1000] with a default of 200, so an out-of-range value is an operator
    asking for "as much as you have" rather than a malformed request.

    This is what makes brief section 20's "decision audit history" demonstrable:
    the rows carry `simulation.tick`, `allocation.created`, `allocation.arrived`,
    `event.created`, `event.resolved`, `fault.created` and friends, each with the
    tick and sim_time it happened at — the simulator's own record, sitting next
    to our `GET /api/v1/decisions`.
    """
    clamped = max(AUDIT_LIMIT_MIN, min(AUDIT_LIMIT_MAX, int(limit)))
    method = _require_capability(client, "get_audit", "audit")
    entries = await simulator_guard("get_audit", _maybe_await(method(clamped)), metrics=metrics)
    rows = _id_descending(_as_rows(entries))
    return AdminAuditResponse(count=len(rows), limit=clamped, entries=rows, simulated=True)


# ---------------------------------------------------------------------------
# Simulation control (POST — operator actions)
# ---------------------------------------------------------------------------


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
    """Guide sections 7.2-7.6.

    An unknown action is rejected with 422 by the path validator itself, so only
    the four contract verbs can reach the simulator.

    `step` (guide section 7.5) is "the recommended way to drive deterministic
    tests": pause, submit allocations, then step a known number of times and the
    outcome is reproducible. It works while paused, which is exactly what makes
    that sequence possible.
    """
    method = _require_capability(client, f"admin_{action}", action)
    result = await simulator_guard(f"admin_{action}", _maybe_await(method()), metrics=metrics)
    return SimulationActionResponse(action=action, result=_as_payload(result), simulated=True)


@router.post(
    "/api/v1/admin/toggle",
    response_model=AdminToggleResponse,
    summary="Flip RUNNING <-> PAUSED (guide section 7.4)",
)
async def toggle_simulation(
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminToggleResponse:
    """Guide section 7.4's convenience flip, so a UI needs no run/pause bookkeeping.

    Resolved defensively: the client's `admin_toggle` is arriving concurrently
    with this route, and a client without it answers 503 `admin_action_unavailable`
    rather than raising AttributeError.
    """
    method = _require_capability(client, "admin_toggle", "toggle")
    result = await simulator_guard("admin_toggle", _maybe_await(method()), metrics=metrics)
    payload = _as_payload(result)
    status = payload.get("status")
    return AdminToggleResponse(
        action="toggle",
        status=status if isinstance(status, str) else None,
        result=payload,
        simulated=True,
    )


# ---------------------------------------------------------------------------
# Crisis event injection (guide sections 7.7, 7.8)
# ---------------------------------------------------------------------------


@router.post(
    "/api/v1/admin/events",
    response_model=AdminEventCreatedResponse,
    summary="Inject a crisis event (demand spike, disruption, outage, ...)",
)
async def inject_event(
    body: EventIn,
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminEventCreatedResponse:
    """Guide section 7.7. Covers brief section 10's whole crisis table.

    All six documented types are accepted, and each is validated against its own
    parameter shape from guide section 7.8 before it is forwarded:

    * `demand_spike`     — `region_ids[]` / `station_ids[]` / `multiplier` (1.5)
    * `route_disruption` — `route_ids[]`
    * `station_outage`   — `station_ids[]`
    * `depot_constraint` — `depot_ids[]`
    * `shipment_delay`   — `depot_ids[]` / `fuel_types[]` / `delay_ticks` (2)
    * `supply_shortfall` — `depot_ids[]` / `fuel_types[]` / `factor` (0.5)

    The filters follow guide section 7.8's rule: **an empty list means "all
    entities of that type"**, so `{"region_ids": []}` is a network-wide spike and
    is forwarded as such. The singular `region_id` spelling does not exist on the
    wire and is rejected — the simulator would ignore it, turning a
    one-region spike into a network-wide one without telling anyone.
    """
    payload = body.model_dump()
    parameters = dict(payload.get("parameters") or {})
    # Fold the operator's event-specific fields into `parameters`, exactly as
    # `POST /admin/faults` does: `EventRequest` is
    # `{type, start_tick, duration_ticks, parameters}`, so a `region_ids` sent at
    # the top level has to travel inside `parameters` to reach the simulator.
    for key, value in payload.items():
        if key not in _EVENT_ENVELOPE_FIELDS:
            parameters[key] = value
    payload["parameters"] = _validate_event_parameters(payload["type"], parameters)

    request = build_peer_model("app.sim.models", ("EventRequest",), payload)
    if isinstance(request, dict):
        # A2's client does `req.to_payload()`, so a dict would be an
        # AttributeError deep inside the gateway. Name the failure here instead.
        raise api_error(
            503,
            "event_request_unavailable",
            "A2's EventRequest could not be constructed from this event",
            details={"type": payload.get("type")},
        )

    event = await simulator_guard(
        "admin_inject_event",
        _maybe_await(client.admin_inject_event(request)),
        metrics=metrics,
    )
    return AdminEventCreatedResponse(event=_as_payload(event), simulated=True)


# ---------------------------------------------------------------------------
# Fault injection (guide sections 7.9, 7.10, 7.11)
# ---------------------------------------------------------------------------


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
    """Guide section 7.9, over guide section 7.10's five fault types.

    `latency`, `unavailable`, `error_rate`, `stale_data` and `stream_disconnect`
    are the whole vocabulary — brief section 22's "application or dependency
    failure is injected" is one of these. An unknown type is a 422 here rather
    than a 502 relayed from the simulator, so the operator is told what they
    typed instead of being told the simulator broke.

    `duration_seconds` is bounded to guide section 7.9's `0 < x <= 3600`; omitted,
    it defaults to five ticks' worth so the fault expires without a second call.
    """
    payload = body.model_dump()
    # Fold the operator's fault-specific fields into `parameters`: `FaultRequest`
    # is `{type, duration_seconds, parameters}`, so a `depot_id` sent at the top
    # level has to travel inside `parameters` to reach the simulator at all.
    parameters = dict(payload.get("parameters") or {})
    for key, value in payload.items():
        if key not in _FAULT_ENVELOPE_FIELDS:
            parameters[key] = value

    fault_type = payload.get("type")
    if fault_type not in FAULT_TYPES:
        raise api_error(
            422,
            "invalid_fault_type",
            f"'{fault_type}' is not a simulator fault type; guide section 7.10 "
            f"documents {', '.join(FAULT_TYPES)}",
            details={"type": fault_type, "allowed": list(FAULT_TYPES)},
        )

    duration = payload.get("duration_seconds")
    if duration is None:
        duration = DEFAULT_FAULT_DURATION_SECONDS
    seconds = _coerce_whole_seconds(duration)
    if seconds is None or not (FAULT_DURATION_MIN_SECONDS < seconds <= FAULT_DURATION_MAX_SECONDS):
        raise api_error(
            422,
            "invalid_fault_duration",
            "duration_seconds must be a whole number of seconds in "
            f"({FAULT_DURATION_MIN_SECONDS}, {FAULT_DURATION_MAX_SECONDS}]",
            details={"duration_seconds": duration},
        )

    payload["parameters"] = _validate_fault_parameters(fault_type, parameters)
    payload["duration_seconds"] = seconds

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
    return AdminFaultCreatedResponse(fault=_as_payload(fault), simulated=True)


@router.post(
    "/api/v1/admin/faults/clear",
    response_model=AdminFaultsClearedResponse,
    summary="Clear every active fault (guide section 7.11)",
)
async def clear_faults(
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> AdminFaultsClearedResponse:
    """Guide section 7.11: sets `active = false` on every active fault.

    Idempotent — calling it with nothing active is a success, not a 404. It is
    the clean end of a resilience demonstration: inject a fault, watch the
    console degrade, clear the fault, watch it recover.
    """
    method = _require_capability(client, "admin_clear_faults", "clear_faults")
    result = await simulator_guard("admin_clear_faults", _maybe_await(method()), metrics=metrics)
    return AdminFaultsClearedResponse(cleared=True, result=_as_payload(result), simulated=True)
