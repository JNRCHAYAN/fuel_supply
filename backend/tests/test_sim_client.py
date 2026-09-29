"""`SimulatorClient` tests (CONTRACT.md 5.4 and 5.5).

Every test stubs the transport with ``httpx.MockTransport``; none of them touch
the network, and none of them require the live simulator at :8001 to be up.

Timing is fully controlled: the breaker's clock is a hand-driven stub and the
client's ``sleep`` is replaced, so retry/backoff behaviour is asserted exactly
rather than waited for.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.config import Settings
from app.sim.client import SSEFrameDecoder, SimulatorClient
from app.sim.errors import SimulatorError
from app.sim.models import AllocationRequest, EventRequest, FaultRequest

BASE_URL = "http://sim.test"

# --------------------------------------------------------------------------- #
# Live payloads (copied from :8001)
# --------------------------------------------------------------------------- #

HEALTH = {"status": "ok", "database": "ok", "simulation": {"status": "RUNNING", "tick": 3888}}

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

ALLOCATION = {
    "id": 1,
    "idempotency_key": "smoke-verify-001",
    "source_depot_id": "depot-gazipur",
    "destination_station_id": "station-mirpur",
    "route_id": "route-gazipur-mirpur",
    "fuel_type": "DIESEL",
    "quantity": 5000.0,
    "created_tick": 480,
    "departure_tick": 480,
    "expected_arrival_tick": 482,
    "actual_arrival_tick": 482,
    "status": "ARRIVED",
    "failure_reason": None,
}

DEMAND_HISTORY = [
    {
        "id": 49212,
        "station_id": "station-coxsbazar",
        "fuel_type": "OCTANE",
        "tick": 4100,
        "sim_time": "2026-02-12T17:00:00",
        "demand_liters": 53.255,
        "served_liters": 0.0,
        "unmet_liters": 53.255,
    }
]

FAULT = {
    "id": 1,
    "type": "latency",
    "start_wall_time": "2026-09-29T05:05:55.143004+00:00",
    "end_wall_time": "2026-09-29T05:05:56.143004+00:00",
    "active": True,
    "parameters": {},
}

# Every endpoint one snapshot needs, so build_snapshot tests have a whole world.
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

ENVELOPE_503 = {"error": {"code": "FAULT_INJECTED", "message": "error_rate fault active"}}
ENVELOPE_404 = {"detail": {"code": "NOT_FOUND"}}
ENVELOPE_422 = {
    "detail": [
        {
            "type": "int_parsing",
            "loc": ["query", "limit"],
            "msg": "Input should be a valid integer",
            "input": "notanint",
        }
    ]
}


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class FakeClock:
    def __init__(self, start: float = 500.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingSleep:
    """Replaces ``asyncio.sleep`` so retries are instant but still observable.

    It yields to the event loop (``sleep(0)``) rather than returning outright.
    That matters: a retry loop whose backoff never suspends would starve the
    loop, so a cancellation or a ``wait_for`` timeout could never be delivered
    and the test would hang instead of failing.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        await asyncio.sleep(0)


class RecordingMetrics:
    """Stands in for A9's ``Metrics``; records only the hooks the gateway uses."""

    def __init__(self) -> None:
        self.breaker_states: list[tuple[str, str]] = []
        self.calls: list[tuple[str, tuple]] = []

    def set_breaker(self, component: str, state: str) -> None:
        self.breaker_states.append((component, state))
        self.calls.append(("set_breaker", (component, state)))

    def record_sim_failure(self, *args) -> None:
        self.calls.append(("record_sim_failure", args))

    def record_sim_retry(self, *args) -> None:
        self.calls.append(("record_sim_retry", args))

    def record_sim_stale_read(self, *args) -> None:
        self.calls.append(("record_sim_stale_read", args))


class ExplodingMetrics:
    """A metrics backend that is down must not take requests down with it."""

    def __getattr__(self, name: str):
        def _boom(*args, **kwargs):
            raise RuntimeError("metrics backend is down")

        return _boom


def settings(**overrides) -> Settings:
    """Real ``Settings``, with the sim values pinned so a stray .env cannot leak in."""
    base = {
        "simulator_base_url": BASE_URL,
        "simulator_timeout_seconds": 10.0,
        "simulator_max_retries": 3,
        "circuit_failure_threshold": 5,
        "circuit_reset_seconds": 30.0,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def handler_for(routes: dict):
    """Build a MockTransport handler from a ``path -> response`` mapping.

    A value may be a payload (returns 200 JSON), an ``httpx.Response`` (returned
    as-is), or an ``Exception`` instance (raised, to simulate a transport
    failure).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        entry = routes.get(request.url.path)
        if entry is None:
            return httpx.Response(404, json=ENVELOPE_404)
        if isinstance(entry, Exception):
            raise entry
        if callable(entry):
            return entry(request)
        if isinstance(entry, httpx.Response):
            return entry
        return httpx.Response(200, json=entry)

    return handler


def make_client(routes: dict | None = None, *, clock=None, sleep=None, metrics=None, **overrides):
    transports = handler_for(routes if routes is not None else {})
    return SimulatorClient(
        settings(**overrides),
        metrics,
        transport=httpx.MockTransport(transports),
        clock=clock or FakeClock(),
        sleep=sleep or RecordingSleep(),
    )


class CountingHandler:
    """Wraps a handler and counts requests, so "was it retried?" is decidable."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.path}")
        return self._handler(request)

    @property
    def count(self) -> int:
        return len(self.calls)


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


async def test_get_health_happy_path():
    client = make_client({"/v1/health": HEALTH})
    health = await client.get_health()
    assert health.status == "ok"
    assert health.simulation.tick == 3888
    assert client.breaker_state == "closed"
    await client.aclose()


async def test_get_instance_regions_depots_stations_routes():
    client = make_client(
        {
            "/v1/instance": INSTANCE,
            "/v1/regions": REGIONS,
            "/v1/depots": DEPOTS,
            "/v1/stations": STATIONS,
            "/v1/routes": ROUTES,
        }
    )
    assert (await client.get_instance()).scenario_id == "baseline"
    assert (await client.get_regions())[0].name == "Dhaka Division"
    assert (await client.get_depots())[0].inventory_for("DIESEL") == 85000.0
    assert (await client.get_stations())[0].demand_profile == "urban_high"
    assert (await client.get_routes())[0].max_shipment == 7000.0
    await client.aclose()


async def test_get_supply_arrivals_events_metrics_allocations():
    client = make_client(
        {
            "/v1/supply-arrivals": SUPPLY_ARRIVALS,
            "/v1/events": EVENTS,
            "/v1/metrics": METRICS,
            "/v1/allocations": [ALLOCATION],
        }
    )
    assert (await client.get_supply_arrivals())[0].status == "ARRIVED"
    assert (await client.get_events())[0].parameters["factor"] == 1.5
    assert (await client.get_metrics()).service_level == 0.024103
    assert (await client.get_allocations())[0].idempotency_key == "smoke-verify-001"
    await client.aclose()


async def test_get_demand_history_sends_the_query_parameters():
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=DEMAND_HISTORY)

    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(handler),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    observations = await client.get_demand_history("station-mirpur", limit=25)
    assert observations[0].demand_liters == 53.255
    assert seen[0].params["station_id"] == "station-mirpur"
    assert seen[0].params["limit"] == "25"
    await client.aclose()


async def test_demand_history_cache_is_keyed_by_the_query():
    """A cached page must never answer a different question."""
    calls: list[httpx.URL] = []
    routes: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        entry = routes.get(request.url.path)
        if entry is None:
            return httpx.Response(404, json=ENVELOPE_404)
        return entry(request)

    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=RecordingSleep(),
    )
    routes["/v1/demand-history"] = lambda r: httpx.Response(200, json=DEMAND_HISTORY)
    await client.get_demand_history("station-a", limit=10)

    # Open the breaker so every further read has to come from cache.
    routes["/v1/health"] = lambda r: httpx.Response(503, json=ENVELOPE_503)
    with pytest.raises(SimulatorError):
        await client.get_health()
    assert client.breaker_state == "open"

    # A *different* question has no cached answer, so it must raise rather than
    # be served station-a's rows.
    with pytest.raises(SimulatorError) as caught:
        await client.get_demand_history("station-b", limit=10)
    assert caught.value.kind == "circuit_open"
    assert not any("station-b" in str(url) for url in calls)

    # A different *limit* is also a different question.
    with pytest.raises(SimulatorError):
        await client.get_demand_history("station-a", limit=999)

    # The original question is still answerable from cache.
    cached = await client.get_demand_history("station-a", limit=10)
    assert cached[0].station_id == "station-coxsbazar"
    await client.aclose()


# --------------------------------------------------------------------------- #
# Retry policy (CONTRACT.md 5.5: 5xx and transport only, never 4xx)
# --------------------------------------------------------------------------- #


async def test_4xx_is_not_retried():
    counter = CountingHandler(lambda request: httpx.Response(404, json=ENVELOPE_404))
    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=sleep
    )

    with pytest.raises(SimulatorError) as caught:
        await client.get_health()

    assert counter.count == 1, "a 4xx must be raised on the first response"
    assert sleep.delays == [], "no backoff on a 4xx"
    assert caught.value.status_code == 404
    assert caught.value.code == "NOT_FOUND"
    assert caught.value.retryable is False
    assert client.breaker_state == "closed", "a 4xx must not trip the breaker"
    await client.aclose()


async def test_422_is_not_retried():
    counter = CountingHandler(lambda request: httpx.Response(422, json=ENVELOPE_422))
    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert counter.count == 1
    assert caught.value.kind == "validation"
    assert caught.value.code == "VALIDATION_ERROR"
    await client.aclose()


async def test_demand_history_limit_defaults_to_200_and_is_clamped():
    """Guide 4.11: ``limit`` is clamped to ``[1, 2000]`` and defaults to 200.

    The clamp is applied client-side, so a nonsense limit buys a defined page
    rather than the simulator's 422 -- the old behaviour forwarded ``-1`` and
    encoded the rejection. An explicit in-range limit is forwarded untouched:
    the ingestor and the intelligence layer both pass 1500.

    ``ttl=0`` disables the memo, because ``-1`` and ``0`` clamp to the *same*
    cache key and the second call would otherwise be a memo hit, not a request.
    """
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=DEMAND_HISTORY)

    client = SimulatorClient(
        settings(simulator_cache_ttl_seconds=0),
        None,
        transport=httpx.MockTransport(handler),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    await client.get_demand_history()
    await client.get_demand_history(limit=-1)
    await client.get_demand_history(limit=0)
    await client.get_demand_history(limit=99_999)
    await client.get_demand_history(limit=1500)

    assert [url.params["limit"] for url in seen] == [
        "200",
        "1",
        "1",
        "2000",
        "1500",
    ]
    await client.aclose()


async def test_5xx_is_retried_then_raises():
    counter = CountingHandler(lambda request: httpx.Response(503, json=ENVELOPE_503))
    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(simulator_max_retries=3),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=sleep,
    )

    with pytest.raises(SimulatorError) as caught:
        await client.get_health()

    assert counter.count == 3, "the configured attempt budget must be spent"
    assert len(sleep.delays) == 2, "no sleep after the final attempt"
    assert caught.value.status_code == 503
    assert caught.value.kind == "fault"
    assert caught.value.retryable is True
    await client.aclose()


async def test_backoff_grows_exponentially():
    counter = CountingHandler(lambda request: httpx.Response(500, json=ENVELOPE_503))
    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(simulator_max_retries=4),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=sleep,
    )
    with pytest.raises(SimulatorError):
        await client.get_health()
    await client.aclose()

    assert len(sleep.delays) == 3
    # 0.25, 0.5, 1.0 with up to 10% jitter.
    for index, delay in enumerate(sleep.delays):
        nominal = 0.25 * (2**index)
        assert nominal <= delay <= nominal * 1.1, sleep.delays
    assert sleep.delays[0] < sleep.delays[1] < sleep.delays[2]


async def test_a_5xx_that_recovers_is_transparent():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json=ENVELOPE_503)
        return httpx.Response(200, json=HEALTH)

    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=sleep
    )
    health = await client.get_health()
    assert health.status == "ok"
    assert calls["n"] == 2
    assert len(sleep.delays) == 1
    # A recovered request leaves the breaker closed and a fresh cache entry.
    assert client.breaker_state == "closed"
    assert client.last_good("health") is not None
    await client.aclose()


async def test_transport_error_is_retried():
    counter = CountingHandler(handler_for({"/v1/health": httpx.ConnectError("connection refused")}))
    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(simulator_max_retries=3),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=sleep,
    )
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert counter.count == 3
    assert caught.value.kind == "transport"
    assert caught.value.status_code is None
    assert caught.value.retryable is True
    await client.aclose()


async def test_a_non_transport_http_error_is_not_retried():
    """A non-transport ``httpx.HTTPError`` is marked non-retryable, so it must raise.

    ``httpx.DecodingError`` is an ``HTTPError`` but *not* a ``TransportError``,
    so it lands in the branch that sets ``retryable=False``. That branch has to
    raise on the spot: falling through would spend the whole attempt budget
    repeating an error that repetition cannot fix, contradicting the module's
    stated policy.
    """
    counter = CountingHandler(
        handler_for({"/v1/health": httpx.DecodingError("bad framing")})
    )
    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(simulator_max_retries=3),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=sleep,
    )
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert counter.count == 1, "a non-retryable error must not be retried"
    assert sleep.delays == [], "no backoff after an immediate raise"
    assert caught.value.kind == "transport"
    assert caught.value.retryable is False
    await client.aclose()


async def test_timeout_is_retried_and_reported_as_transport():
    counter = CountingHandler(handler_for({"/v1/health": httpx.ReadTimeout("timed out")}))
    client = SimulatorClient(
        settings(simulator_max_retries=2),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert counter.count == 2
    assert caught.value.kind == "transport"
    await client.aclose()


async def test_non_json_success_body_is_a_decode_error_and_is_retried():
    counter = CountingHandler(lambda request: httpx.Response(200, text="<html>oops</html>"))
    client = SimulatorClient(
        settings(simulator_max_retries=2),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert counter.count == 2
    assert caught.value.kind == "decode"
    await client.aclose()


# --------------------------------------------------------------------------- #
# Writes, including the verified 201-on-replay quirk
# --------------------------------------------------------------------------- #


def allocation_request() -> AllocationRequest:
    return AllocationRequest(
        idempotency_key="smoke-verify-001",
        source_depot_id="depot-gazipur",
        destination_station_id="station-mirpur",
        route_id="route-gazipur-mirpur",
        fuel_type="DIESEL",
        quantity=5000.0,
    )


async def test_create_allocation_accepts_201_on_idempotency_replay():
    """Verified against the live simulator: a replay answers **201**, not 200.

    The integration guide documents 200. The simulator de-duplicates correctly
    (only one allocation is created) and only the status code differs, so any
    2xx is accepted as success.
    """
    counter = CountingHandler(lambda request: httpx.Response(201, json=ALLOCATION))
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=RecordingSleep()
    )
    allocation = await client.create_allocation(allocation_request())
    assert allocation.id == 1
    assert counter.count == 1, "a 201 is a success, not something to retry"
    await client.aclose()


async def test_create_allocation_also_accepts_the_documented_200():
    counter = CountingHandler(lambda request: httpx.Response(200, json=ALLOCATION))
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=RecordingSleep()
    )
    assert (await client.create_allocation(allocation_request())).id == 1
    await client.aclose()


async def test_create_allocation_sends_the_full_body():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(201, json=ALLOCATION)

    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=RecordingSleep()
    )
    await client.create_allocation(allocation_request())
    assert seen == [
        {
            "idempotency_key": "smoke-verify-001",
            "source_depot_id": "depot-gazipur",
            "destination_station_id": "station-mirpur",
            "route_id": "route-gazipur-mirpur",
            "fuel_type": "DIESEL",
            "quantity": 5000.0,
        }
    ]
    await client.aclose()


async def test_create_allocation_4xx_is_not_retried():
    counter = CountingHandler(
        lambda request: httpx.Response(409, json={"detail": {"code": "DUPLICATE", "message": "no"}})
    )
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=RecordingSleep()
    )
    with pytest.raises(SimulatorError) as caught:
        await client.create_allocation(allocation_request())
    assert counter.count == 1
    assert caught.value.status_code == 409
    await client.aclose()


async def test_a_write_is_never_served_from_cache():
    routes: dict = {"/v1/health": HEALTH}
    counter = CountingHandler(handler_for(routes))
    clock = FakeClock()
    client = SimulatorClient(
        # ttl=0: this test reasons about network attempts, so it opts out of the
        # memo rather than racing the clock against it.
        settings(
            circuit_failure_threshold=1,
            simulator_max_retries=1,
            simulator_cache_ttl_seconds=0,
        ),
        None,
        transport=httpx.MockTransport(counter),
        clock=clock,
        sleep=RecordingSleep(),
    )
    await client.get_health()  # cache a value and keep the breaker closed
    assert client.last_good("health") is not None

    # Now make the simulator unhealthy so the breaker opens.
    routes["/v1/health"] = lambda request: httpx.Response(503, json=ENVELOPE_503)
    with pytest.raises(SimulatorError):
        await client.get_health()
    assert client.breaker_state == "open"

    # The *read* still degrades to its cached value...
    assert (await client.get_health()).status == "ok"

    # ...but a write has nothing to fall back on: it must fail loudly rather
    # than pretend an allocation was submitted.
    with pytest.raises(SimulatorError) as caught:
        await client.create_allocation(allocation_request())
    assert caught.value.kind == "circuit_open"
    await client.aclose()


async def test_admin_writes_and_reads():
    routes = {
        "/admin/run": INSTANCE,
        "/admin/pause": {**INSTANCE, "status": "PAUSED"},
        "/admin/toggle": {**INSTANCE, "status": "PAUSED"},
        "/admin/step": {"tick": 3889},
        "/admin/reset": {"status": "reset"},
        "/admin/events": EVENTS[0],
        "/admin/faults": FAULT,
        "/admin/faults/clear": {"status": "cleared"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/admin/faults" and request.method == "GET":
            return httpx.Response(200, json=[FAULT])
        if request.url.path == "/admin/events" and request.method == "GET":
            return httpx.Response(200, json=[EVENTS[0]])
        return httpx.Response(200, json=routes[request.url.path])

    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=RecordingSleep()
    )

    assert (await client.admin_run()).is_running is True
    assert (await client.admin_pause()).status == "PAUSED"
    assert (await client.admin_toggle()).status == "PAUSED"
    assert (await client.admin_step())["tick"] == 3889
    assert (await client.admin_reset())["status"] == "reset"
    assert (await client.admin_inject_event(EventRequest("demand_spike", 10, 5))).id == 1
    assert (await client.admin_inject_fault(FaultRequest("latency", 1))).type == "latency"
    assert (await client.admin_clear_faults())["status"] == "cleared"
    assert (await client.admin_get_faults())[0].active is True
    # Guide 7.13: the read side of /admin/events returns a list, unlike the
    # single-event injector that shares the path.
    assert (await client.admin_get_events())[0].id == 1
    await client.aclose()


# --------------------------------------------------------------------------- #
# Circuit breaker integration
# --------------------------------------------------------------------------- #


async def test_breaker_opens_after_the_threshold_and_then_raises_without_a_cache():
    counter = CountingHandler(lambda request: httpx.Response(503, json=ENVELOPE_503))
    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=2, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(counter),
        clock=clock,
        sleep=RecordingSleep(),
    )

    with pytest.raises(SimulatorError):
        await client.get_health()
    assert client.breaker_state == "closed", "one failure is below the threshold"

    with pytest.raises(SimulatorError):
        await client.get_health()
    assert client.breaker_state == "open"
    calls_before = counter.count

    # Open with nothing cached: raise, do not fabricate.
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert caught.value.kind == "circuit_open"
    assert "refusing to fabricate" in caught.value.message
    assert counter.count == calls_before, "no request may be made while the breaker is open"
    await client.aclose()


async def test_breaker_half_opens_after_the_reset_window():
    state = {"fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503, json=ENVELOPE_503)
        return httpx.Response(200, json=HEALTH)

    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=2, circuit_reset_seconds=30, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=RecordingSleep(),
    )

    for _ in range(2):
        with pytest.raises(SimulatorError):
            await client.get_health()
    assert client.breaker_state == "open"

    clock.advance(29.0)
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert caught.value.kind == "circuit_open"

    # The simulator recovers while the breaker is open.
    state["fail"] = False
    clock.advance(1.0)
    assert client.breaker_state == "half_open"

    health = await client.get_health()
    assert health.status == "ok"
    assert client.breaker_state == "closed", "a successful trial closes it"
    await client.aclose()


async def test_a_failed_half_open_trial_reopens_the_breaker():
    counter = CountingHandler(lambda request: httpx.Response(503, json=ENVELOPE_503))
    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, circuit_reset_seconds=30, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(counter),
        clock=clock,
        sleep=RecordingSleep(),
    )

    with pytest.raises(SimulatorError):
        await client.get_health()
    assert client.breaker_state == "open"

    clock.advance(30.0)
    with pytest.raises(SimulatorError):
        await client.get_health()  # the trial, which fails
    assert client.breaker_state == "open"
    await client.aclose()


async def test_last_good_cache_is_served_when_the_breaker_is_open():
    state = {"fail": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["fail"]:
            return httpx.Response(503, json=ENVELOPE_503)
        return httpx.Response(200, json=DEPOTS)

    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=RecordingSleep(),
    )

    fresh = await client.get_depots()
    assert fresh[0].id == "depot-gazipur"
    cached = client.last_good("depots")
    assert cached is not None
    assert cached[1] == clock.now

    state["fail"] = True
    clock.advance(9.0)
    with pytest.raises(SimulatorError):
        await client.get_depots()
    assert client.breaker_state == "open"

    # Now the read degrades to the cached value instead of failing.
    degraded = await client.get_depots()
    assert degraded == fresh
    assert client.last_good_age("depots") == pytest.approx(9.0)
    await client.aclose()


async def test_last_good_returns_none_when_nothing_was_ever_cached():
    client = make_client({"/v1/depots": DEPOTS})
    assert client.last_good("depots") is None
    assert client.last_good("never-called") is None
    await client.aclose()


async def test_an_invalid_payload_counts_towards_the_breaker():
    """A 200 that does not match the model is a simulator fault, not ours."""
    counter = CountingHandler(lambda request: httpx.Response(200, json={"nonsense": True}))
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    # /v1/depots expects an array; an object is a shape violation.
    with pytest.raises(SimulatorError) as caught:
        await client.get_depots()
    assert caught.value.kind == "invalid_payload"
    assert client.breaker_state == "open"
    await client.aclose()


async def test_a_4xx_settles_a_half_open_trial_instead_of_wedging_the_breaker():
    """A 4xx is not an unhealthy failure, but it must still resolve the trial.

    Without this, the half-open trial stays "in flight" forever after a 4xx and
    the breaker refuses every request for the rest of the process's life.
    """
    state = {"mode": "fail"}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["mode"] == "fail":
            return httpx.Response(503, json=ENVELOPE_503)
        return httpx.Response(404, json=ENVELOPE_404)

    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, circuit_reset_seconds=30, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(handler),
        clock=clock,
        sleep=RecordingSleep(),
    )

    with pytest.raises(SimulatorError):
        await client.get_health()
    assert client.breaker_state == "open"

    clock.advance(30.0)
    state["mode"] = "not-found"
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert caught.value.status_code == 404

    # The trial is resolved and the breaker is usable again, not stuck open.
    assert client.breaker_state == "closed"
    with pytest.raises(SimulatorError) as caught:
        await client.get_health()
    assert caught.value.status_code == 404, "the request is attempted again"
    await client.aclose()


# --------------------------------------------------------------------------- #
# Snapshot assembly and staleness
# --------------------------------------------------------------------------- #


async def test_build_snapshot_assembles_a_fresh_view():
    client = make_client(SNAPSHOT_ROUTES)
    snapshot = await client.build_snapshot()

    assert snapshot.tick == 3888
    assert snapshot.sim_time == "2026-02-10T12:00:00"
    assert snapshot.status == "RUNNING"
    assert snapshot.stale is False
    assert snapshot.age_seconds == 0.0
    assert snapshot.depots[0].inventory_for("DIESEL") == 85000.0
    assert snapshot.stations[0].id == "station-mirpur"
    assert snapshot.routes[0].max_shipment == 7000.0
    assert snapshot.regions[0].demand_factor == 1.0
    assert snapshot.supply_arrivals[0].quantity == 18000.0
    assert snapshot.events[0].type == "demand_spike"
    assert snapshot.metrics.service_level == 0.024103
    assert client.last_good("snapshot") is not None
    await client.aclose()


async def test_build_snapshot_goes_stale_with_an_age_when_the_simulator_fails():
    routes = dict(SNAPSHOT_ROUTES)
    clock = FakeClock()
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(handler_for(routes)),
        clock=clock,
        sleep=RecordingSleep(),
    )

    fresh = await client.build_snapshot()
    assert fresh.stale is False

    clock.advance(17.5)
    routes["/v1/metrics"] = lambda request: httpx.Response(503, json=ENVELOPE_503)

    stale = await client.build_snapshot()
    assert stale.stale is True, "a degraded snapshot must say so"
    assert stale.age_seconds == pytest.approx(17.5)
    assert stale.is_degraded is True
    assert stale.tick == fresh.tick
    assert stale.depots == fresh.depots
    await client.aclose()


async def test_build_snapshot_marks_stale_when_components_come_from_cache():
    """Staleness aggregates: the age reported is the oldest piece in the view."""
    routes = dict(SNAPSHOT_ROUTES)
    clock = FakeClock()
    client = SimulatorClient(
        # ttl=0: the point here is the breaker opening on a real attempt, which
        # a memo hit would never make.
        settings(
            circuit_failure_threshold=1,
            simulator_max_retries=1,
            simulator_cache_ttl_seconds=0,
        ),
        None,
        transport=httpx.MockTransport(handler_for(routes)),
        clock=clock,
        sleep=RecordingSleep(),
    )
    await client.build_snapshot()

    # Opening the breaker makes every per-endpoint read serve its own cache, so
    # the assembled snapshot is stale even though nothing raised this time.
    routes["/v1/metrics"] = lambda request: httpx.Response(503, json=ENVELOPE_503)
    with pytest.raises(SimulatorError):
        await client.get_metrics()
    assert client.breaker_state == "open"

    clock.advance(12.0)
    degraded = await client.build_snapshot()
    assert degraded.stale is True
    assert degraded.age_seconds == pytest.approx(12.0)
    # Still a complete, usable view -- it is the *same* data, honestly labelled.
    assert degraded.depots and degraded.stations and degraded.routes
    assert client.last_good("snapshot") is not None
    await client.aclose()


async def test_build_snapshot_raises_with_no_cache_and_an_open_breaker():
    """Degrade, never fabricate: with nothing cached there is no honest answer."""
    counter = CountingHandler(lambda request: httpx.Response(503, json=ENVELOPE_503))
    client = SimulatorClient(
        settings(circuit_failure_threshold=1, simulator_max_retries=1),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    with pytest.raises(SimulatorError) as caught:
        await client.build_snapshot()
    assert caught.value.status_code == 503
    assert client.last_good("snapshot") is None

    with pytest.raises(SimulatorError) as caught:
        await client.build_snapshot()
    assert caught.value.kind == "circuit_open"
    await client.aclose()


# --------------------------------------------------------------------------- #
# Metrics hooks are optional and non-fatal
# --------------------------------------------------------------------------- #


async def test_metrics_hooks_are_called_when_present():
    metrics = RecordingMetrics()
    routes = {"/v1/health": HEALTH}
    client = SimulatorClient(
        # ttl=0: the failure hooks only fire on a request that reaches the
        # simulator, which a memo hit never does.
        settings(
            circuit_failure_threshold=1,
            simulator_max_retries=1,
            simulator_cache_ttl_seconds=0,
        ),
        metrics,
        transport=httpx.MockTransport(handler_for(routes)),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    await client.get_health()
    assert ("simulator", "closed") in metrics.breaker_states

    routes["/v1/health"] = lambda request: httpx.Response(503, json=ENVELOPE_503)
    with pytest.raises(SimulatorError):
        await client.get_health()
    assert ("simulator", "open") in metrics.breaker_states
    assert any(name == "record_sim_failure" for name, _ in metrics.calls)
    await client.aclose()


async def test_a_broken_metrics_backend_does_not_break_a_request():
    client = SimulatorClient(
        settings(),
        ExplodingMetrics(),
        transport=httpx.MockTransport(handler_for({"/v1/health": HEALTH})),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    health = await client.get_health()
    assert health.status == "ok"
    await client.aclose()


async def test_client_works_with_a_settings_stub():
    """The gateway reads settings defensively; A1's class is not a hard import."""
    from types import SimpleNamespace

    stub = SimpleNamespace(
        simulator_base_url=BASE_URL,
        simulator_timeout_seconds=1.0,
        simulator_max_retries=1,
        circuit_failure_threshold=2,
        circuit_reset_seconds=5.0,
    )
    client = SimulatorClient(
        stub,
        None,
        transport=httpx.MockTransport(handler_for({"/v1/health": HEALTH})),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )
    assert (await client.get_health()).status == "ok"
    await client.aclose()


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #


def test_sse_frame_decoder_handles_a_well_formed_frame():
    decoder = SSEFrameDecoder()
    frames = []
    for line in ["event: simulation.tick", 'data: {"tick": 1}', ""]:
        frames.extend(decoder.feed(line))
    assert frames == [("simulation.tick", '{"tick": 1}')]


def test_sse_frame_decoder_ignores_the_live_connection_banner():
    """The live stream opens with `: connected` before any event."""
    decoder = SSEFrameDecoder()
    assert decoder.feed(": connected") == []
    frames = decoder.feed("")  # a bare keepalive must not dispatch anything
    assert frames == []


def test_sse_frame_decoder_joins_multi_line_data():
    decoder = SSEFrameDecoder()
    decoder.feed("event: simulator.notice")
    decoder.feed("data: line one")
    decoder.feed("data: line two")
    assert decoder.feed("") == [("simulator.notice", "line one\nline two")]


def test_sse_frame_decoder_ignores_id_and_retry_and_unnamed_events_are_not_dispatched():
    decoder = SSEFrameDecoder()
    decoder.feed("id: 42")
    decoder.feed("retry: 3000")
    assert decoder.feed("") == []

    # An `event:` with no `data:` never dispatches (SSE spec).
    decoder.feed("event: simulation.tick")
    assert decoder.feed("") == []


def test_sse_frame_decoder_flushes_a_trailing_frame_without_a_blank_line():
    decoder = SSEFrameDecoder()
    decoder.feed("event: simulation.tick")
    decoder.feed('data: {"tick": 9}')
    assert decoder.close() == [("simulation.tick", '{"tick": 9}')]


STREAM_TEXT = (
    ": connected\n"
    "\n"
    "event: simulation.tick\n"
    "data: {\"tick\": 3888, \"sim_time\": \"2026-02-10T12:00:00\"}\n"
    "\n"
    "event: inventory.updated\n"
    'data: {"station_id": "station-mirpur"}\n'
    "\n"
    "event: brand.new\n"
    'data: {"anything": true}\n'
    "\n"
)


async def test_stream_yields_events_from_the_wire():
    counter = CountingHandler(lambda request: httpx.Response(200, text=STREAM_TEXT))
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=RecordingSleep()
    )

    stream = client.stream()
    received = []
    for _ in range(3):
        received.append(await asyncio.wait_for(stream.__anext__(), timeout=5))
    await stream.aclose()

    assert [e.name for e in received] == [
        "simulation.tick",
        "inventory.updated",
        "brand.new",
    ]
    assert received[0].data == {"tick": 3888, "sim_time": "2026-02-10T12:00:00"}
    assert received[1].data == {"station_id": "station-mirpur"}
    assert received[0].received_at > 0
    # An unfamiliar event name is carried, not dropped.
    assert received[2].is_known is False
    await client.aclose()


async def test_stream_reconnects_with_backoff_after_a_drop():
    """The first connection delivers; the second drops; the third recovers."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 2:
            raise httpx.ConnectError("stream dropped")
        return httpx.Response(200, text='event: simulation.tick\ndata: {"tick": 1}\n\n')

    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=sleep
    )

    stream = client.stream()
    first = await asyncio.wait_for(stream.__anext__(), timeout=5)
    # The next connection fails; the generator backs off and reconnects.
    second = await asyncio.wait_for(stream.__anext__(), timeout=5)
    await stream.aclose()

    assert first.name == "simulation.tick"
    assert second.name == "simulation.tick"
    assert calls["n"] == 3, "one deliver, one drop, one recovery"
    assert sleep.delays, "a dropped stream must back off before reconnecting"
    await client.aclose()


async def test_stream_survives_a_5xx_and_reconnects_with_backoff():
    """A failing stream response becomes a typed error and a retry, not a crash."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text='{"error": {"code": "FAULT", "message": "down"}}')
        return httpx.Response(200, text='event: simulation.tick\ndata: {"tick": 1}\n\n')

    sleep = RecordingSleep()
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(handler), clock=FakeClock(), sleep=sleep
    )

    stream = client.stream()
    # Nothing is yielded by the failing connection; the event below can only
    # come from the reconnected one.
    event = await asyncio.wait_for(stream.__anext__(), timeout=5)
    await stream.aclose()

    assert event.name == "simulation.tick"
    assert calls["n"] == 2
    assert sleep.delays, "an error response on the stream must back off before retrying"
    await client.aclose()


async def test_start_registers_a_consumer_and_dispatch_calls_subscribers():
    counter = CountingHandler(lambda request: httpx.Response(200, text=STREAM_TEXT))
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=RecordingSleep()
    )

    seen: list[str] = []
    arrived = asyncio.Event()

    async def on_event(event):
        seen.append(event.name)
        if len(seen) >= 3:
            arrived.set()

    unsubscribe = client.subscribe(on_event)
    await client.start()
    await asyncio.wait_for(arrived.wait(), timeout=5)

    assert seen[:3] == ["simulation.tick", "inventory.updated", "brand.new"]

    unsubscribe()
    assert client._subscribers == []
    await client.aclose()


async def test_unsubscribe_is_idempotent_and_dispatch_survives_a_bad_subscriber():
    client = make_client({})

    async def explode(event):
        raise RuntimeError("subscriber is broken")

    good: list[str] = []

    async def ok(event):
        good.append(event.name)

    off_bad = client.subscribe(explode)
    client.subscribe(ok)

    from app.sim.models import StreamEvent

    await client._dispatch(StreamEvent(name="simulation.tick", data={}, received_at=1.0))
    assert good == ["simulation.tick"], "a broken subscriber must not silence the others"

    off_bad()
    off_bad()  # second call is a no-op, not an error
    assert len(client._subscribers) == 1
    await client.aclose()


async def test_start_is_idempotent_and_aclose_stops_the_consumer():
    counter = CountingHandler(lambda request: httpx.Response(200, text=STREAM_TEXT))
    client = SimulatorClient(
        settings(), None, transport=httpx.MockTransport(counter), clock=FakeClock(), sleep=RecordingSleep()
    )
    await client.start()
    task = client._stream_task
    await client.start()
    assert client._stream_task is task, "start must not spawn a second consumer"

    await client.aclose()
    assert client._stream_task is None
    assert task is not None and task.done()


async def test_aclose_is_safe_without_start():
    client = make_client({})
    await client.aclose()
    await client.aclose()


async def test_client_context_manager_closes():
    async with make_client({"/v1/health": HEALTH}) as client:
        assert (await client.get_health()).status == "ok"
    assert client._client is None or client._client.is_closed


async def test_the_client_never_reaches_the_network():
    """A guard on this module: nothing here may reach the live simulator.

    ``make_client`` always injects a ``MockTransport``, so this asserts the
    harness itself is honest rather than re-testing the client.
    """
    client = make_client({"/v1/health": HEALTH})
    assert isinstance(client._transport, httpx.MockTransport)
    await client.aclose()


# --------------------------------------------------------------------------- #
# Request coalescing -- the fix for the load-test collapse
# --------------------------------------------------------------------------- #


async def test_concurrent_reads_of_one_endpoint_collapse_into_one_request():
    """Fifty simultaneous operators must not become fifty simulator requests.

    Measured, not hypothetical. The graded load test ran fifty operators, whose
    every request reads the same eight endpoints. The simulator's database pool
    holds fifteen connections; it exhausted, every request then blocked for
    thirty seconds on the pool, and the container was left wedged and
    ``unhealthy`` with a failing streak of 22 health checks. Readers that arrive
    while a fetch is in flight must await that fetch rather than start another.
    """
    counter = CountingHandler(handler_for({"/v1/stations": STATIONS}))
    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(counter),
        clock=FakeClock(),
        sleep=RecordingSleep(),
    )

    results = await asyncio.gather(*[client.get_stations() for _ in range(50)])

    assert counter.count == 1, f"50 concurrent readers made {counter.count} requests"
    assert all(len(r) == len(results[0]) for r in results)
    await client.aclose()


async def test_a_repeat_read_inside_the_memo_window_is_not_refetched():
    counter = CountingHandler(handler_for({"/v1/stations": STATIONS}))
    clock = FakeClock()
    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(counter),
        clock=clock,
        sleep=RecordingSleep(),
    )

    first = await client.get_stations()
    clock.advance(0.25)
    second = await client.get_stations()

    assert counter.count == 1, "a read inside the memo window went to the network"
    assert len(second) == len(first)
    # The value genuinely came from the simulator moments ago, so it is aged,
    # not degraded: age is reported, staleness is not claimed.
    assert client.last_good_age("stations") == pytest.approx(0.25)
    await client.aclose()


async def test_a_read_after_the_memo_window_reaches_the_simulator_again():
    counter = CountingHandler(handler_for({"/v1/stations": STATIONS}))
    clock = FakeClock()
    client = SimulatorClient(
        settings(),
        None,
        transport=httpx.MockTransport(counter),
        clock=clock,
        sleep=RecordingSleep(),
    )

    await client.get_stations()
    clock.advance(client.memo_seconds + 0.01)
    await client.get_stations()

    assert counter.count == 2, "an expired memo must not be served"
    await client.aclose()
