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

from fastapi import APIRouter, Depends, Query

from app.api.schemas import (
    DemandHistoryResponse,
    NetworkSnapshotResponse,
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


async def load_snapshot(
    client: Any,
    *,
    metrics: Any | None = None,
    snapshot_cls: Any | None = None,
) -> Any:
    """Assemble A2's `Snapshot` from the simulator client.

    CONTRACT.md 5.4 does not give `SimulatorClient` a `get_snapshot()` method
    even though `Snapshot` (5.2) is the type every engine consumes, so the API
    assembles it from the reads the contract does define. If A2 does expose a
    one-shot `get_snapshot`, that is preferred.
    """
    getter = getattr(client, "get_snapshot", None)
    if callable(getter):
        try:
            return await simulator_guard(
                "get_snapshot", _maybe_await(getter()), metrics=metrics
            )
        except AttributeError:  # pragma: no cover - defensive
            pass

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
        "stale": False,
        "age_seconds": 0.0,
    }
    snapshot = build_peer_model("app.sim.models", ("Snapshot",), payload)
    if isinstance(snapshot, dict):
        raise dependency_unavailable("app.sim.models.Snapshot")
    return snapshot


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
    instance = None
    getter = getattr(snapshot, "instance", None)
    if getter is not None:
        instance = to_jsonable(getter)
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
