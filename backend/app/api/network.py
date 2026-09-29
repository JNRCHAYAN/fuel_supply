"""Network state and demand history.

CONTRACT.md section 9 rows: `GET /api/v1/network/snapshot`,
`GET /api/v1/network/demand-history`.

`load_snapshot` is defined here because /network/snapshot is the route that
owns the concept; the intelligence and decision routes import it so that one
definition of "the current state of the network" serves the whole API.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from fastapi import APIRouter, Depends, Path, Query

from app.api.schemas import (
    DemandHistoryResponse,
    NetworkSnapshotResponse,
    api_error,
    build_peer_model,
    dependency_unavailable,
    get_logger,
    get_metrics,
    get_simulator_client,
    simulator_guard,
    to_jsonable,
)

logger = get_logger(__name__)

router = APIRouter(tags=["network"])


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


#: The cache keys A2's client stores its last-good reads under (app/sim/client.py,
#: `build_snapshot`). Used only to *age* a manually assembled snapshot; nothing
#: here requires a client to cache under these names.
_ENDPOINT_CACHE_KEYS = (
    "instance",
    "depots",
    "stations",
    "routes",
    "regions",
    "supply-arrivals",
    "events",
    "metrics",
)


def _manual_snapshot_staleness(client: Any) -> tuple[bool, float]:
    """Staleness for a snapshot assembled from the per-endpoint reads.

    `stale` and `age_seconds` are carried by the client's cache-aware
    `build_snapshot`, so a snapshot this module assembles by hand has no
    staleness of its own -- and it must not claim freshness it never measured.
    The one honest signal available here is the breaker: when it is not closed,
    the client's `_read` serves its last-good cache, so the assembled view is
    old by definition. The age is the oldest cached component when the client
    can report one, and 0.0 (unknown age) when it cannot; the flag is the part
    that must never be assumed.
    """
    breaker = getattr(client, "breaker_state", None)
    if not isinstance(breaker, str) or breaker.lower() == "closed":
        return False, 0.0
    ages: list[float] = []
    age_of = getattr(client, "last_good_age", None)
    if callable(age_of):
        for key in _ENDPOINT_CACHE_KEYS:
            try:
                value = age_of(key)
            except Exception:  # pragma: no cover - a cache probe must not fail a request
                continue
            if isinstance(value, (int, float)) and value > 0:
                ages.append(float(value))
    return True, max(ages) if ages else 0.0


async def load_snapshot(
    client: Any,
    *,
    metrics: Any | None = None,
    snapshot_cls: Any | None = None,
) -> Any:
    """Assemble A2's `Snapshot` from the simulator client.

    CONTRACT.md 5.4 gives `SimulatorClient` no one-shot `get_snapshot()`; the
    cache-aware read it does define -- and the *only* read carrying `stale` and
    `age_seconds` -- is `build_snapshot` (app/sim/client.py), so that is what is
    probed for first. A client offering only a one-shot `get_snapshot` is still
    accepted. Only when neither exists does the API assemble the snapshot from
    the reads the contract does define, and that snapshot's staleness is derived
    from the breaker rather than hardcoded fresh.
    """
    for name in ("build_snapshot", "get_snapshot"):
        getter = getattr(client, name, None)
        if callable(getter):
            try:
                return await simulator_guard(
                    name, _maybe_await(getter()), metrics=metrics
                )
            except AttributeError:  # pragma: no cover - defensive
                continue

    instance, depots, stations, routes, regions, arrivals, events, sim_metrics = await asyncio.gather(
        simulator_guard("get_instance", _maybe_await(client.get_instance()), metrics=metrics),
        simulator_guard("get_depots", _maybe_await(client.get_depots()), metrics=metrics),
        simulator_guard("get_stations", _maybe_await(client.get_stations()), metrics=metrics),
        simulator_guard("get_routes", _maybe_await(client.get_routes()), metrics=metrics),
        simulator_guard("get_regions", _maybe_await(client.get_regions()), metrics=metrics),
        simulator_guard(
            "get_supply_arrivals", _maybe_await(client.get_supply_arrivals()), metrics=metrics
        ),
        simulator_guard("get_events", _maybe_await(client.get_events()), metrics=metrics),
        simulator_guard("get_metrics", _maybe_await(client.get_metrics()), metrics=metrics),
    )

    if snapshot_cls is None:
        try:
            from app.sim.models import Snapshot as snapshot_cls  # type: ignore[no-redef]
        except Exception as exc:
            raise dependency_unavailable("app.sim.models.Snapshot", exc) from exc

    stale, age = _manual_snapshot_staleness(client)
    payload = {
        "taken_at": time.monotonic(),
        "tick": getattr(instance, "tick", None),
        "sim_time": getattr(instance, "sim_time", None),
        "status": getattr(instance, "status", None),
        "depots": tuple(depots),
        "stations": tuple(stations),
        "routes": tuple(routes),
        "regions": tuple(regions),
        "supply_arrivals": tuple(arrivals),
        "events": tuple(events),
        "metrics": sim_metrics,
        "stale": stale,
        "age_seconds": age,
    }
    snapshot = build_peer_model("app.sim.models", ("Snapshot",), payload)
    if isinstance(snapshot, dict):
        raise dependency_unavailable("app.sim.models.Snapshot")
    return snapshot


async def load_instance(client: Any, *, snapshot: Any = None) -> Any | None:
    """The `SimInstance` behind a snapshot, or `None` when it cannot be read.

    `Snapshot` (CONTRACT.md 5.2) has no `instance` field, so the client's parser
    drops the `SimInstance` it read while assembling one and `seed`,
    `scenario_id`, `scenario_version` and `tick_minutes` -- the fields that make
    a run reproducible -- would otherwise be unreachable from every response.
    Re-reading it here is provenance rather than network state: an unavailable
    instance degrades to `null`, never to a fabricated one and never to a failed
    snapshot route.
    """
    embedded = getattr(snapshot, "instance", None) if snapshot is not None else None
    if embedded is not None:
        return embedded
    getter = getattr(client, "get_instance", None)
    if not callable(getter):
        return None
    try:
        return await _maybe_await(getter())
    except Exception as exc:  # noqa: BLE001 - provenance must never break the route
        logger.warning("instance_unavailable", error=type(exc).__name__)
        return None


#: A snapshot older than this is presented as stale even if the gateway did not
#: flag it. The simulator advances in multi-minute ticks, so this is a
#: generous window: it exists to stop aged data being read as live, not to
#: second-guess A2's own staleness flag.
SNAPSHOT_FRESHNESS_SECONDS = 30.0


def _snapshot_age(snapshot: Any) -> tuple[bool, float]:
    stale = bool(getattr(snapshot, "stale", False))
    age = getattr(snapshot, "age_seconds", None)
    taken_at = getattr(snapshot, "taken_at", None)
    if not isinstance(age, (int, float)) or age <= 0:
        # `taken_at` is time.monotonic() taken when the snapshot was assembled,
        # so it is only meaningful within this process — which is exactly where
        # the freshness question is being asked.
        if isinstance(taken_at, (int, float)) and taken_at > 0:
            age = max(0.0, time.monotonic() - float(taken_at))
        else:
            age = 0.0
    age = float(age)
    if age > SNAPSHOT_FRESHNESS_SECONDS:
        stale = True
    return stale, age


@router.get(
    "/api/v1/network/snapshot",
    response_model=NetworkSnapshotResponse,
    summary="Instance, depots, stations, routes, regions",
)
async def network_snapshot(
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> NetworkSnapshotResponse:
    snapshot = await load_snapshot(client, metrics=metrics)
    payload = to_jsonable(snapshot)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    stale, age = _snapshot_age(snapshot)
    # `Snapshot` (5.2) carries no `instance`, so it is re-read rather than
    # looked for on the snapshot; an unreadable instance is reported as null.
    instance = to_jsonable(await load_instance(client, snapshot=snapshot))
    return NetworkSnapshotResponse(
        tick=payload.get("tick"),
        sim_time=payload.get("sim_time"),
        status=payload.get("status"),
        stale=stale,
        age_seconds=age,
        instance=instance,
        depots=payload.get("depots") or [],
        stations=payload.get("stations") or [],
        routes=payload.get("routes") or [],
        regions=payload.get("regions") or [],
        supply_arrivals=payload.get("supply_arrivals") or [],
        events=payload.get("events") or [],
        metrics=payload.get("metrics") or {},
        simulated=True,
    )


async def _entity_from_snapshot(
    kind: str,
    collection: str,
    entity_id: str,
    client: Any,
    *,
    metrics: Any,
) -> dict[str, Any]:
    """Resolve one entity out of the snapshot the rest of the API already uses.

    Guide 4.5/4.6 document a per-entity read with a 404 for an unknown id. The
    entity comes from the same snapshot as `/network/snapshot` rather than a
    fresh client method, so an entity can never disagree with the snapshot it
    was read alongside. An id that is not in that snapshot is a typed 404 in the
    house envelope -- never an empty 200 and never a 500.
    """
    snapshot = await load_snapshot(client, metrics=metrics)
    payload = to_jsonable(snapshot)
    rows = payload.get(collection) if isinstance(payload, dict) else None
    for row in rows or []:
        if isinstance(row, dict) and row.get("id") == entity_id:
            stale, age = _snapshot_age(snapshot)
            return {**row, "stale": stale, "age_seconds": age, "simulated": True}
    raise api_error(
        404,
        f"{kind}_not_found",
        f"no {kind} '{entity_id}' in the current network state",
        details={f"{kind}_id": entity_id},
    )


@router.get(
    "/api/v1/depots/{entity_id}",
    response_model=dict[str, Any],
    summary="One depot, resolved from the current snapshot",
)
async def depot_by_id(
    entity_id: str = Path(max_length=100),
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> dict[str, Any]:
    return await _entity_from_snapshot("depot", "depots", entity_id, client, metrics=metrics)


@router.get(
    "/api/v1/stations/{entity_id}",
    response_model=dict[str, Any],
    summary="One station, resolved from the current snapshot",
)
async def station_by_id(
    entity_id: str = Path(max_length=100),
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> dict[str, Any]:
    return await _entity_from_snapshot("station", "stations", entity_id, client, metrics=metrics)


@router.get(
    "/api/v1/network/demand-history",
    response_model=DemandHistoryResponse,
    summary="Proxied, validated demand history",
)
async def demand_history(
    station_id: str | None = Query(default=None, max_length=100),
    fuel_type: str | None = Query(default=None, max_length=20),
    limit: int = Query(default=500, ge=1, le=2000),
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> DemandHistoryResponse:
    """Proxy of `/v1/demand-history`, validated per CONTRACT.md 0.3.

    The simulator's history is external input: it is passed through Pydantic
    (`DemandObservationOut`, extra fields allowed) and an unparseable payload
    raises a typed 502 rather than reaching the console.
    """
    requested_fuel = fuel_type.upper() if fuel_type else None
    rows = await simulator_guard(
        "get_demand_history",
        _maybe_await(client.get_demand_history(station_id=station_id, limit=limit)),
        metrics=metrics,
    )
    rows = to_jsonable(rows) or []
    if not isinstance(rows, list):
        rows = [rows]
    if requested_fuel:
        rows = [
            row
            for row in rows
            if not isinstance(row, dict)
            or str(row.get("fuel_type", "")).upper() == requested_fuel
        ]
    return DemandHistoryResponse(
        station_id=station_id,
        fuel_type=requested_fuel,
        count=len(rows),
        observations=rows,
        simulated=True,
    )
