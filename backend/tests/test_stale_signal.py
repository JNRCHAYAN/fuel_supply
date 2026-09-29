"""``X-Simulator-Stale`` handling (guide sections 3, 6.4, 7.10 and 10 rule 2).

While a ``stale_data`` fault is active the simulator answers every non-stream
``/v1/*`` GET with ``X-Simulator-Stale: true``. The body is still a perfectly
well-formed 200, which is exactly what makes the signal dangerous: without
honouring the header the client would memoise the value and later serve it as
fresh, and an open breaker would hand it back as "last good" carrying only an
age. These tests pin the two obligations that prevent that -- never cache a
flagged read, and report it as stale -- plus case-insensitive header matching
and the unchanged path when the header is absent.

The SSE stream does not carry the header (guide 6.4), so everything here is the
REST read path.

No test touches the network or the live simulator: the transport is an
``httpx.MockTransport`` and the clock is hand-driven.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import Settings
from app.sim.client import SimulatorClient
from app.sim.models import Depot

BASE_URL = "http://sim.test"

# --------------------------------------------------------------------------- #
# Payloads (copied from the live simulator at :8001)
# --------------------------------------------------------------------------- #

INSTANCE = {
    "id": 1,
    "scenario_id": "baseline",
    "scenario_version": "1.0",
    "seed": 12345,
    "sim_time": "2026-02-10T12:00:00",
    "tick": 3888,
    "tick_minutes": 15,
    "status": "RUNNING",
}

METRICS = {
    "served_demand_liters": 90900.0,
    "unmet_demand_liters": 3680414.477,
    "service_level": 0.024103,
    "allocation_liters": 5000.0,
    "allocation_failures": 0,
}

REGIONS = [{"id": "region-dhaka", "name": "Dhaka Division", "demand_factor": 1.0}]

DEPOTS = [
    {
        "id": "depot-gazipur",
        "name": "Gazipur Depot",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "dispatch_capacity_per_tick": 12000.0,
        "capacity": {"DIESEL": 90000, "PETROL": 70000, "OCTANE": 45000},
        "inventory": {"DIESEL": 85000.0, "PETROL": 70000.0, "OCTANE": 45000.0},
    }
]

STATIONS = [
    {
        "id": "station-mirpur",
        "name": "Mirpur Fuel Station",
        "region_id": "region-dhaka",
        "status": "OPEN",
        "demand_profile": "urban_high",
        "demand_multiplier": 1.0,
        "capacity": {"DIESEL": 15000, "PETROL": 14000, "OCTANE": 9000},
        "inventory": {"DIESEL": 0, "PETROL": 0, "OCTANE": 0},
    }
]

ROUTES = [
    {
        "id": "route-gazipur-mirpur",
        "source_depot_id": "depot-gazipur",
        "destination_station_id": "station-mirpur",
        "transit_ticks": 2,
        "max_shipment": 7000.0,
        "status": "AVAILABLE",
    }
]

SUPPLY_ARRIVALS = [
    {
        "id": "supply-001",
        "depot_id": "depot-gazipur",
        "fuel_type": "DIESEL",
        "quantity": 18000.0,
        "planned_tick": 12,
        "actual_tick": 12,
        "status": "ARRIVED",
    }
]

EVENTS = [
    {
        "id": 1,
        "type": "demand_spike",
        "start_tick": 500,
        "end_tick": 520,
        "status": "RESOLVED",
        "parameters": {"region_id": "region-dhaka", "factor": 1.5},
    }
]

# Every endpoint one snapshot needs, so the aggregation test has a whole world.
SNAPSHOT_ROUTES = {
    "/v1/instance": INSTANCE,
    "/v1/metrics": METRICS,
    "/v1/depots": DEPOTS,
    "/v1/stations": STATIONS,
    "/v1/routes": ROUTES,
    "/v1/regions": REGIONS,
    "/v1/supply-arrivals": SUPPLY_ARRIVALS,
    "/v1/events": EVENTS,
}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class FakeClock:
    """A hand-driven monotonic clock, so ages are asserted exactly."""

    def __init__(self, start: float = 500.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Replaces ``asyncio.sleep``; yields to the loop so nothing can starve."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


def settings(**overrides) -> Settings:
    base = {
        "simulator_base_url": BASE_URL,
        "simulator_timeout_seconds": 10.0,
        "simulator_max_retries": 3,
        "circuit_failure_threshold": 5,
        "circuit_reset_seconds": 30.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def make_client(routes: dict, *, stale: dict[str, str] | None = None, **overrides):
    """Build a client whose transport serves ``routes``.

    ``stale`` maps a path to the value it should carry in the
    ``X-Simulator-Stale`` header, so one test can flag exactly one component of
    a snapshot and leave the rest normal.
    """
    stale = stale or {}

    def handler(request: httpx.Request) -> httpx.Response:
        entry = routes.get(request.url.path)
        if entry is None:
            return httpx.Response(404, json={"detail": {"code": "NOT_FOUND"}})
        headers = {}
        if request.url.path in stale:
            headers["X-Simulator-Stale"] = stale[request.url.path]
        return httpx.Response(200, json=entry, headers=headers)

    return SimulatorClient(
        settings(**overrides),
        None,
        transport=httpx.MockTransport(handler),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )


def _depots_read(client: SimulatorClient):
    """One depots GET through the internal read, to observe the stale flag."""
    return client._read("depots", "/v1/depots", client._list_parser(Depot))


# --------------------------------------------------------------------------- #
# The header is detected, and a flagged value is never cached
# --------------------------------------------------------------------------- #


async def test_a_stale_response_is_reported_stale_and_never_cached():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=DEPOTS, headers={"X-Simulator-Stale": "true"})

    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(handler),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )

    value, stale, age = await _depots_read(client)
    assert value[0].id == "depot-gazipur"
    assert stale is True, "the header must be seen and threaded out of the read"
    assert age == 0.0, "the body did just arrive; only its trustworthiness is in question"
    assert client.last_good("depots") is None, "a flagged value must never enter last-good"
    assert client.cached_keys() == ()
    assert client.breaker_state == "closed", "a flagged 200 answered; it is not a failure"

    # Because nothing was memoised, a second read goes back to the simulator and
    # is flagged again rather than being served from cache as fresh.
    _, stale_again, _ = await _depots_read(client)
    assert stale_again is True
    assert calls["n"] == 2, "a stale value must not be served by the memo"
    await client.aclose()


async def test_the_public_surface_still_returns_the_flagged_body():
    """Staleness is a label, not a failure: the value is still handed back."""
    client = make_client({"/v1/depots": DEPOTS}, stale={"/v1/depots": "true"})
    depots = await client.get_depots()
    assert depots[0].inventory_for("DIESEL") == 85000.0
    assert client.last_good("depots") is None
    await client.aclose()


@pytest.mark.parametrize(
    "name,value",
    [
        ("X-Simulator-Stale", "true"),
        ("x-simulator-stale", "true"),
        ("X-SIMULATOR-STALE", "TRUE"),
        ("X-Simulator-Stale", " 1 "),
    ],
)
async def test_the_stale_header_is_matched_case_insensitively(name, value):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=DEPOTS, headers={name: value})

    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(handler),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    _, stale, _ = await _depots_read(client)
    assert stale is True, f"{name}: {value!r} was not recognised as stale"
    await client.aclose()


async def test_a_non_true_header_value_is_not_stale():
    """Only an affirmative value overrides the normal path."""
    client = make_client({"/v1/depots": DEPOTS}, stale={"/v1/depots": "false"})
    _, stale, _ = await _depots_read(client)
    assert stale is False
    assert client.last_good("depots") is not None
    await client.aclose()


async def test_an_absent_header_leaves_the_normal_path_unchanged():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=DEPOTS)

    clock = FakeClock()
    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=RecordingSleep(),
    )

    _, stale, age = await _depots_read(client)
    assert stale is False
    assert age == 0.0
    assert client.last_good("depots") is not None

    # The memo keeps working exactly as before: no header, no invalidation.
    clock.advance(0.25)
    _, stale_again, aged = await _depots_read(client)
    assert stale_again is False
    assert aged == pytest.approx(0.25)
    assert calls["n"] == 1, "an unflagged value inside the memo window is served from the memo"
    await client.aclose()


# --------------------------------------------------------------------------- #
# Snapshot aggregation
# --------------------------------------------------------------------------- #


async def test_build_snapshot_marks_the_snapshot_stale_when_a_component_is_flagged():
    client = make_client(SNAPSHOT_ROUTES, stale={"/v1/metrics": "true"})

    snapshot = await client.build_snapshot()

    assert snapshot.stale is True, "one flagged component makes the whole view stale"
    assert snapshot.is_degraded is True
    # The body is still usable -- it is labelled, not discarded.
    assert snapshot.metrics.service_level == 0.024103
    assert snapshot.depots[0].id == "depot-gazipur"

    assert client.last_good("metrics") is None
    assert client.last_good("snapshot") is None, "a stale snapshot must not be cached"
    # Untouched components are still cached normally.
    assert client.last_good("depots") is not None
    await client.aclose()


async def test_an_unflagged_snapshot_is_still_assembled_fresh():
    client = make_client(SNAPSHOT_ROUTES)
    snapshot = await client.build_snapshot()
    assert snapshot.stale is False
    assert client.last_good("snapshot") is not None
    await client.aclose()
