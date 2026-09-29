"""Live-simulator wire-contract tests (owner: the regression net).

Why this file exists
--------------------
Every other test in this suite stubs the simulator. A stub only ever returns the
shapes its author already believes in, so the whole suite can be green while the
backend is wrong about the *wire*: three real defects were found by hand that no
fake could catch.

* ``build_history`` projected a ``/v1/demand-history`` row by field name and
  dropped **every** row, because the wire spells the measurement
  ``demand_liters`` (guide §4.11) and the engine's ``DemandPoint`` spells it
  ``liters``. The simulator's history -- the source of truth (CONTRACT.md 5.5) --
  was silently discarded on every request.
* ``/api/v1/network/snapshot`` probed ``client.get_snapshot``; the real client
  has ``build_snapshot``, so the cache-aware staleness flag was never consulted
  and ``stale`` was hardcoded ``False``. The test fake *did* define
  ``get_snapshot``, so it passed.
* The event filter was read as a singular ``region_id`` when the guide (§4.9,
  §7.8) documents the plural list ``region_ids``.

This file closes that gap the only way a wire contract can be closed: payloads
captured from the **live simulator** are treated as the committed contract and
piped through the backend's **real** parsers. A wire field that appears, vanishes
or is renamed now fails a test instead of passing one.

Capturing
---------
``tests/fixtures/*.json`` hold the exact bytes the live simulator returned. To
refresh them (with the simulator up)::

    .venv/Scripts/python.exe tests/test_live_simulator_contract.py

Only GETs are issued -- never ``/admin/reset`` -- because the simulator is
shared.

Offline behaviour
-----------------
The contract tests run against the **committed** captures, so the suite works
with the simulator down. Only the one test that re-checks the live source is
marked live-only and skips cleanly when nothing answers on the base URL.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api.intelligence import build_history  # noqa: E402
from app.config import Settings  # noqa: E402
from app.intelligence.detect import AnomalyDetector  # noqa: E402
from app.sim.models import (  # noqa: E402
    Allocation,
    DemandObservation,
    DemandPoint,
    Depot,
    DomainEvent,
    Health,
    Metrics,
    Region,
    Route,
    SimInstance,
    SimulationState,
    Snapshot,
    Station,
    SupplyArrival,
)

# --------------------------------------------------------------------------- #
# Capture harness
# --------------------------------------------------------------------------- #

#: Base URL of the live simulator. Overridable so a differently-mapped port
#: does not require editing this file.
LIVE_BASE_URL = os.environ.get("SIMULATOR_BASE_URL", "http://127.0.0.1:8001")

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: ``(fixture name, path, query)`` for every endpoint the contract pins. This is
#: exactly the defensive-client checklist in guide section 10.
ENDPOINTS: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("health", "/v1/health", {}),
    ("instance", "/v1/instance", {}),
    ("regions", "/v1/regions", {}),
    ("depots", "/v1/depots", {}),
    ("stations", "/v1/stations", {}),
    ("routes", "/v1/routes", {}),
    ("supply-arrivals", "/v1/supply-arrivals", {}),
    ("events", "/v1/events", {}),
    ("allocations", "/v1/allocations", {}),
    ("demand-history", "/v1/demand-history", {"limit": 200}),
    ("metrics", "/v1/metrics", {}),
)


async def capture_live_simulator(base_url: str = LIVE_BASE_URL) -> dict[str, Path]:
    """GET every contracted endpoint and persist the exact response bytes.

    Read-only: no ``/admin/*`` write is ever issued. Returns the fixture paths
    written, keyed by endpoint name.
    """
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    async with httpx.AsyncClient(base_url=base_url, timeout=15.0) as client:
        for name, path, query in ENDPOINTS:
            response = await client.get(path, params=query or None)
            response.raise_for_status()
            target = FIXTURES_DIR / f"{name}.json"
            # ``content`` is the raw bytes; writing them verbatim is what makes
            # the capture a contract rather than a paraphrase.
            target.write_bytes(response.content)
            written[name] = target
    return written


def _capture(name: str) -> Any:
    """Load one committed capture, or skip with an explicit reason."""
    path = FIXTURES_DIR / f"{name}.json"
    if not path.exists():
        pytest.skip(
            f"capture {path.name} is missing; run "
            "`.venv/Scripts/python.exe tests/test_live_simulator_contract.py` "
            "with the simulator up"
        )
    return json.loads(path.read_bytes().decode("utf-8"))


def _simulator_is_up(base_url: str = LIVE_BASE_URL) -> bool:
    try:
        response = httpx.get(f"{base_url}/v1/health", timeout=1.5)
    except httpx.HTTPError:
        return False
    return response.status_code == 200


@pytest.fixture(scope="module")
def live_simulator() -> str:
    """The live base URL, or a clean skip when the simulator is down."""
    if not _simulator_is_up():
        pytest.skip(
            f"live simulator is not reachable at {LIVE_BASE_URL}; live-only "
            "contract checks are skipped (the committed captures still run "
            "offline)"
        )
    return LIVE_BASE_URL


if __name__ == "__main__":
    written = asyncio.run(capture_live_simulator())
    for name, path in written.items():
        print(f"captured {name:<16} -> {path}")


# --------------------------------------------------------------------------- #
# The wire<->model field contract
# --------------------------------------------------------------------------- #

#: One real parser per captured endpoint. `SimulationState` is checked through
#: the nested `health.simulation` block below.
WIRE_MODELS: dict[str, Any] = {
    "health": Health,
    "instance": SimInstance,
    "regions": Region,
    "depots": Depot,
    "stations": Station,
    "routes": Route,
    "supply-arrivals": SupplyArrival,
    "events": DomainEvent,
    "allocations": Allocation,
    "demand-history": DemandObservation,
    "metrics": Metrics,
}


def _model_fields(obj: Any) -> set[str]:
    return {f.name for f in dataclasses.fields(obj)}


def test_every_contracted_capture_is_committed() -> None:
    """The captured wire is committed; a missing fixture is a broken net."""
    missing = [
        f"{name}.json" for name, _, _ in ENDPOINTS if not (FIXTURES_DIR / f"{name}.json").exists()
    ]
    assert not missing, (
        f"missing captures {missing}; run the capture harness with the "
        "simulator up"
    )


@pytest.mark.parametrize("fixture", sorted(WIRE_MODELS))
def test_captured_wire_fields_survive_the_real_parser(fixture: str) -> None:
    """Every wire key maps to a model field, and every field to a wire key.

    Feeding the *real* payload through the *real* ``from_api`` and comparing the
    model's own field set against the wire keys is what makes a silently dropped
    or renamed field fail. A newly added simulator field shows up here as an
    unexpected wire key rather than disappearing into an ignored ``dict``.
    """
    payload = _capture(fixture)
    model = WIRE_MODELS[fixture]

    if isinstance(payload, list):
        samples = [(item, model.from_api(item)) for item in payload]
        if not payload:
            return  # an empty list endpoint still proves the parser accepts []
    else:
        samples = [(payload, model.from_api(payload))]

    for wire, parsed in samples:
        expected = _model_fields(parsed)
        assert expected == set(wire), (
            f"{fixture}: model {model.__name__} fields {sorted(expected)} do not "
            f"match the wire keys {sorted(wire)} "
            f"(dropped={sorted(set(wire) - expected)}, "
            f"invented={sorted(expected - set(wire))})"
        )


def test_health_simulation_block_survives_its_parser() -> None:
    """The nested ``health.simulation`` object is parsed, not swallowed."""
    payload = _capture("health")
    health = Health.from_api(payload)
    assert isinstance(health.simulation, SimulationState)
    assert _model_fields(health.simulation) == set(payload["simulation"])


def test_captured_enum_values_are_recognised_not_tolerated() -> None:
    """A documented status must parse to its enum member, not to a raw string.

    ``to_enum`` deliberately tolerates unknown values; this test proves the
    captured values are *known*, so tolerance is not hiding drift.
    """
    from app.sim.models import (
        AllocationStatus,
        EntityStatus,
        EventStatus,
        InstanceStatus,
        RouteStatus,
        SupplyStatus,
    )

    assert Health.from_api(_capture("health")).simulation.status == InstanceStatus(
        SimInstance.from_api(_capture("instance")).status
    )
    for depot in _capture("depots"):
        assert Depot.from_api(depot).status in set(EntityStatus)
    for station in _capture("stations"):
        assert Station.from_api(station).status in set(EntityStatus)
    for route in _capture("routes"):
        assert Route.from_api(route).status in set(RouteStatus)
    for arrival in _capture("supply-arrivals"):
        assert SupplyArrival.from_api(arrival).status in set(SupplyStatus)
    for allocation in _capture("allocations"):
        assert Allocation.from_api(allocation).status in set(AllocationStatus)
    for event in _capture("events"):
        assert DomainEvent.from_api(event).status in set(EventStatus)


# --------------------------------------------------------------------------- #
# Bug 1 -- `demand_liters` -> `liters`
# --------------------------------------------------------------------------- #


async def test_real_demand_history_projects_to_demand_points() -> None:
    """The wire's ``demand_liters`` must reach ``DemandPoint.liters``.

    Regression for bug 1: the row was projected by field name, ``liters`` was
    missing, ``build_peer_model`` returned a plain dict, and the caller dropped
    *every* row -- so the simulator's history (CONTRACT.md 5.5, the source of
    truth) never reached the engines. Asserting a non-zero count from the REAL
    capture is the assertion that was missing.
    """
    rows = _capture("demand-history")
    assert isinstance(rows, list) and rows, "the demand-history capture is empty"
    observations = [DemandObservation.from_api(row) for row in rows]

    # The alias itself: the wire name and the engine name are bridged.
    assert observations[0].liters == observations[0].demand_liters

    class RealHistoryClient:
        """The real client surface: ``get_demand_history`` returning observations."""

        async def get_demand_history(
            self, station_id: str | None = None, limit: int = 500
        ) -> list[Any]:
            return list(observations)

    history = await build_history(None, RealHistoryClient(), None)
    points = [point for series in history.values() for point in series]

    assert points, "every real demand row was dropped by the projection"
    assert len(points) == len(observations)  # nothing dropped
    assert all(isinstance(point, DemandPoint) for point in points)
    assert all(isinstance(point.liters, float) for point in points)
    assert any(point.liters > 0.0 for point in points)
    assert sum(point.liters for point in points) == pytest.approx(
        sum(observation.demand_liters for observation in observations)
    )
    assert set(history) == {
        (observation.station_id, observation.fuel_type) for observation in observations
    }


# --------------------------------------------------------------------------- #
# Bug 3 -- the documented plural filter list
# --------------------------------------------------------------------------- #

#: Guide §7.8: filter parameters are plural lists. An empty list means "all
#: entities of that type".
DOCUMENTED_FILTER_LISTS: tuple[str, ...] = (
    "region_ids",
    "station_ids",
    "route_ids",
    "depot_ids",
    "fuel_types",
)


def test_captured_event_filters_are_documented_plural_lists() -> None:
    """Every documented filter key in the capture is a list, never a scalar.

    The simulator copies injected ``parameters`` through verbatim, so this pins
    the shape of whatever the wire currently carries and fails the moment a
    plural list becomes a scalar.
    """
    for event in _capture("events"):
        parameters = event.get("parameters") or {}
        for key in DOCUMENTED_FILTER_LISTS:
            if key in parameters:
                assert isinstance(parameters[key], list), (
                    f"event {event.get('id')}: parameters[{key!r}] is "
                    f"{type(parameters[key]).__name__}, not a list "
                    "(guide §4.9/§7.8 document the plural list form)"
                )


def test_detector_matches_the_documented_plural_region_filter() -> None:
    """Bug 3 regression: the detector must key on ``region_ids``, the list.

    The detector used to read a singular ``parameters["region_id"]``, which a
    real event never carries -- every event matched nothing, so
    ``active_event_ids`` was empty and a genuine regional disruption could be
    suppressed. A plural-only event must be counted, and a different region's
    event must not be.

    The world is the REAL captured one; only the events are the documented shape
    (guide §4.9), because the shared simulator's live ``/v1/events`` currently
    carries an off-contract injection that uses a singular key (see the module
    report / ``tests/fixtures/events.json``).
    """
    regions = tuple(Region.from_api(row) for row in _capture("regions"))
    stations = tuple(Station.from_api(row) for row in _capture("stations"))
    depots = tuple(Depot.from_api(row) for row in _capture("depots"))
    instance = SimInstance.from_api(_capture("instance"))
    metrics = Metrics.from_api(_capture("metrics"))

    def documented_event(event_id: int, region_ids: list[str]) -> DomainEvent:
        return DomainEvent.from_api(
            {
                "id": event_id,
                "type": "demand_spike",
                "start_tick": 0,
                "end_tick": 100,
                "status": "ACTIVE",
                "parameters": {"region_ids": region_ids, "multiplier": 1.8},
            }
        )

    dhaka_event = documented_event(101, ["region-dhaka"])
    # The documented wire form is the plural list; the singular spelling bug 3
    # read is not what a real event carries.
    assert "region_ids" in dhaka_event.parameters
    assert "region_id" not in dhaka_event.parameters

    snapshot = Snapshot.assemble(
        instance=instance,
        regions=regions,
        stations=stations,
        depots=depots,
        routes=(),
        supply_arrivals=(),
        events=(dhaka_event, documented_event(202, ["region-chattogram"])),
        metrics=metrics,
    )

    # ``regional_min_entities=1`` isolates the filter behaviour: one active
    # event is enough to raise the region's signal.
    signals = AnomalyDetector(regional_min_entities=1).detect(snapshot=snapshot, history={})
    by_region = {
        signal.entity_id: signal
        for signal in signals
        if signal.kind == "regional_disruption"
    }

    assert "region-dhaka" in by_region, (
        "the detector matched no active event for region-dhaka; a document-shaped "
        "event under the plural `region_ids` list was not counted (bug 3)"
    )
    assert by_region["region-dhaka"].evidence["active_event_ids"] == ["101"]
    assert by_region["region-chattogram"].evidence["active_event_ids"] == ["202"]


def test_the_singular_region_filter_is_not_the_documented_wire_form() -> None:
    """The guide's own §4.9 payload uses the plural list, never ``region_id``."""
    documented = {"region_ids": ["region-dhaka"], "multiplier": 1.8}
    assert isinstance(documented["region_ids"], list)
    assert "region_id" not in documented

    # And the real client parses parameters through unchanged, so the backend's
    # matcher is the only thing that decides the filter shape.
    event = DomainEvent.from_api(
        {
            "id": 7,
            "type": "demand_spike",
            "start_tick": 8,
            "end_tick": 20,
            "status": "ACTIVE",
            "parameters": documented,
        }
    )
    assert event.parameters == documented


# --------------------------------------------------------------------------- #
# Bug 2 -- staleness comes from the client, not a hardcoded False
# --------------------------------------------------------------------------- #


def test_the_real_client_exposes_build_snapshot_not_get_snapshot() -> None:
    """The real surface has ``build_snapshot``; ``get_snapshot`` does not exist.

    Bug 2 was a route probing a method the real client never had. Pinning the
    interface here means a route that probes the phantom method cannot pass.
    """
    from app.sim.client import SimulatorClient

    client = SimulatorClient(
        SimpleNamespace(
            simulator_base_url="http://127.0.0.1:9",
            simulator_timeout_seconds=0.25,
            simulator_max_retries=1,
        )
    )
    assert callable(getattr(client, "build_snapshot", None))
    assert not hasattr(client, "get_snapshot"), (
        "SimulatorClient grew a get_snapshot; the network route should use "
        "build_snapshot (the cache-aware one) instead"
    )


def test_network_snapshot_reports_staleness_from_build_snapshot(tmp_path: Path) -> None:
    """``/api/v1/network/snapshot`` must report the client's own ``stale``.

    Regression for bug 2: the route probed ``client.get_snapshot``, which the
    real client lacks, so it fell through to a hand-assembled payload with
    ``stale`` hardcoded ``False``. The client here mirrors the real surface --
    ``build_snapshot`` only -- and returns a stale snapshot.

    NOTE: written against the correct behaviour. While the fix is unlanded this
    test is red on purpose and names the defect rather than being weakened.
    """
    from fastapi.testclient import TestClient

    from app.api import schemas
    from app.main import create_app

    stale_snapshot = Snapshot.assemble(
        instance=SimInstance(
            id=1,
            scenario_id="baseline",
            scenario_version="1.0",
            seed=12345,
            sim_time="2026-01-01T00:00:00+00:00",
            tick=7,
            tick_minutes=15,
            status="PAUSED",
        ),
        metrics=Metrics(
            served_demand_liters=0.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=0.0,
            allocation_failures=0,
        ),
        taken_at=time.monotonic(),
        stale=True,
        age_seconds=12.5,
    )

    class CachedOnlySimulatorClient:
        """The real client surface: a cache-aware ``build_snapshot`` only."""

        async def build_snapshot(self) -> Snapshot:
            return stale_snapshot

    app = create_app(_offline_settings(tmp_path))
    with TestClient(app) as client:
        app.dependency_overrides[schemas.get_simulator_client] = (
            lambda: CachedOnlySimulatorClient()
        )
        response = client.get("/api/v1/network/snapshot")

    assert response.status_code == 200, (
        "the snapshot route failed against a client exposing only "
        f"build_snapshot: {response.text}"
    )
    body = response.json()
    assert body["stale"] is True, (
        "the snapshot reported stale=False while the client's build_snapshot "
        "said stale=True (bug 2: the route is not using build_snapshot)"
    )
    assert body["age_seconds"] == pytest.approx(12.5)


def _offline_settings(tmp_path: Path) -> Settings:
    """Boot the real app against nothing: a discard port and a throwaway DB."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        simulator_base_url="http://127.0.0.1:9",
        simulator_timeout_seconds=0.25,
        simulator_max_retries=1,
        circuit_reset_seconds=1,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'contract.db').as_posix()}",
        deepseek_api_key="",
        llm_enabled=False,
        log_level="WARNING",
    )


# --------------------------------------------------------------------------- #
# Fixture rot: the capture must be the documented world (guide §8)
# --------------------------------------------------------------------------- #

REGION_IDS = {"region-dhaka", "region-chattogram"}
DEPOT_IDS = {"depot-gazipur", "depot-patiya"}
STATION_IDS = {
    "station-mirpur",
    "station-tongi",
    "station-karnaphuli",
    "station-coxsbazar",
}
ROUTE_IDS = {
    "route-gazipur-mirpur",
    "route-gazipur-tongi",
    "route-patiya-karnaphuli",
    "route-patiya-coxsbazar",
    "route-gazipur-karnaphuli",
    "route-patiya-mirpur",
}
FUEL_TYPES = {"DIESEL", "PETROL", "OCTANE"}

#: Static per guide §8 -- capacities, dispatch limits, profiles, route geometry.
#: Inventory is runtime-mutated and deliberately not pinned.
DEPOT_FACTS = {
    "depot-gazipur": ("region-dhaka", 12000.0, {"DIESEL": 90000.0, "PETROL": 70000.0, "OCTANE": 45000.0}),
    "depot-patiya": ("region-chattogram", 11000.0, {"DIESEL": 85000.0, "PETROL": 65000.0, "OCTANE": 40000.0}),
}
STATION_FACTS = {
    "station-mirpur": ("region-dhaka", "urban_high", {"DIESEL": 15000.0, "PETROL": 14000.0, "OCTANE": 9000.0}),
    "station-tongi": ("region-dhaka", "industrial", {"DIESEL": 18000.0, "PETROL": 9000.0, "OCTANE": 6000.0}),
    "station-karnaphuli": ("region-chattogram", "highway", {"DIESEL": 14000.0, "PETROL": 15000.0, "OCTANE": 9000.0}),
    "station-coxsbazar": ("region-chattogram", "regional", {"DIESEL": 12000.0, "PETROL": 12000.0, "OCTANE": 7000.0}),
}
ROUTE_FACTS = {
    "route-gazipur-mirpur": ("depot-gazipur", "station-mirpur", 2, 7000.0),
    "route-gazipur-tongi": ("depot-gazipur", "station-tongi", 2, 6500.0),
    "route-patiya-karnaphuli": ("depot-patiya", "station-karnaphuli", 2, 7000.0),
    "route-patiya-coxsbazar": ("depot-patiya", "station-coxsbazar", 3, 6000.0),
    "route-gazipur-karnaphuli": ("depot-gazipur", "station-karnaphuli", 4, 5000.0),
    "route-patiya-mirpur": ("depot-patiya", "station-mirpur", 4, 5000.0),
}


def test_the_captured_world_is_the_documented_scenario() -> None:
    """Pin the documented world so a capture from the wrong scenario fails.

    A capture taken against a different scenario would silently weaken every
    contract above, so ids, counts and the static facts of guide §8 are checked.
    """
    regions = _capture("regions")
    assert len(regions) == 2
    assert {row["id"] for row in regions} == REGION_IDS
    assert {row["id"]: row["demand_factor"] for row in regions} == {
        "region-dhaka": 1.00,
        "region-chattogram": 1.08,
    }

    depots = _capture("depots")
    assert len(depots) == 2
    assert {row["id"] for row in depots} == DEPOT_IDS
    for row in depots:
        region_id, dispatch, capacity = DEPOT_FACTS[row["id"]]
        assert row["region_id"] == region_id
        assert row["dispatch_capacity_per_tick"] == dispatch
        assert row["capacity"] == capacity
        assert set(row["inventory"]) == FUEL_TYPES

    stations = _capture("stations")
    assert len(stations) == 4
    assert {row["id"] for row in stations} == STATION_IDS
    for row in stations:
        region_id, profile, capacity = STATION_FACTS[row["id"]]
        assert row["region_id"] == region_id
        assert row["demand_profile"] == profile
        assert row["capacity"] == capacity
        assert set(row["inventory"]) == FUEL_TYPES

    routes = _capture("routes")
    assert len(routes) == 6
    assert {row["id"] for row in routes} == ROUTE_IDS
    for row in routes:
        source, destination, transit, maximum = ROUTE_FACTS[row["id"]]
        assert row["source_depot_id"] == source
        assert row["destination_station_id"] == destination
        assert row["transit_ticks"] == transit
        assert row["max_shipment"] == maximum

    # The three documented fuel types appear on every capacity/inventory block.
    assert all(set(depot["inventory"]) == FUEL_TYPES for depot in depots)
    assert all(set(station["capacity"]) == FUEL_TYPES for station in stations)


# --------------------------------------------------------------------------- #
# Live-only: the committed capture still describes the running simulator
# --------------------------------------------------------------------------- #


async def test_live_simulator_still_serves_the_documented_world(
    live_simulator: str,
) -> None:
    """Re-read the live source and confirm the committed capture is current.

    This is the only test that needs the simulator up; it skips cleanly when the
    base URL is unreachable so the suite stays runnable offline.
    """
    async with httpx.AsyncClient(base_url=live_simulator, timeout=10.0) as client:
        regions = (await client.get("/v1/regions")).json()
        depots = (await client.get("/v1/depots")).json()
        stations = (await client.get("/v1/stations")).json()
        routes = (await client.get("/v1/routes")).json()

    assert {row["id"] for row in regions} == REGION_IDS
    assert {row["id"] for row in depots} == DEPOT_IDS
    assert {row["id"] for row in stations} == STATION_IDS
    assert {row["id"] for row in routes} == ROUTE_IDS
