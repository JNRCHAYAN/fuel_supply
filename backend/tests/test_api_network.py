"""`/api/v1/network/snapshot` and `/api/v1/network/demand-history`."""

from __future__ import annotations

import time
from types import SimpleNamespace

from test_api_support import (
    DEPOTS,
    REGIONS,
    ROUTES,
    STATIONS,
    TICK,
    FakeSimulatorClient,
    build_app,
    client,
    error_code,
)


def _snapshot(*, stale: bool = False, age_seconds: float = 0.0, taken_at: float = 0.0):
    return SimpleNamespace(
        taken_at=taken_at,
        tick=TICK,
        sim_time="2026-09-29T09:55:00Z",
        status="RUNNING",
        depots=DEPOTS,
        stations=STATIONS,
        routes=ROUTES,
        regions=REGIONS,
        supply_arrivals=(),
        events=(),
        metrics={"service_level": 0.9},
        stale=stale,
        age_seconds=age_seconds,
    )


class RichSimulatorClient(FakeSimulatorClient):
    """Adds the two surfaces the plain fake leaves out: the cache-aware
    snapshot the *real* client defines (`build_snapshot`), and a populated
    demand history."""

    def __init__(self, *, stale: bool = False, **kwargs) -> None:
        super().__init__(**kwargs)
        self.stale = stale

    async def get_demand_history(self, station_id=None, limit=500):
        rows = [
            {
                "station_id": sid,
                "fuel_type": fuel,
                "tick": TICK - i,
                "liters": 90.0 + i,
            }
            for sid in ("ST-1", "ST-2")
            for fuel in ("DIESEL", "PETROL")
            for i in range(5)
        ]
        if station_id:
            rows = [r for r in rows if r["station_id"] == station_id]
        return rows[:limit]

    async def build_snapshot(self):
        # app/sim/client.py: `build_snapshot` is the method the real
        # SimulatorClient exposes; there is no `get_snapshot` to probe for.
        return _snapshot(stale=self.stale, age_seconds=12.5 if self.stale else 0.0)


class OneShotSnapshotClient(FakeSimulatorClient):
    """A client offering only a one-shot `get_snapshot` and no cache-aware read:
    the fallback the route must still accept."""

    async def get_snapshot(self):
        return _snapshot()


# ---------------------------------------------------------------------------
# /api/v1/network/snapshot
# ---------------------------------------------------------------------------


def test_snapshot_returns_the_whole_network() -> None:
    body = client(build_app()).get("/api/v1/network/snapshot").json()
    assert body["tick"] == TICK
    assert body["sim_time"] == "2026-09-29T10:00:00Z"
    assert body["status"] == "RUNNING"
    assert len(body["depots"]) == len(DEPOTS)
    assert len(body["stations"]) == len(STATIONS)
    assert len(body["routes"]) == len(ROUTES)
    assert len(body["regions"]) == len(REGIONS)
    assert body["supply_arrivals"][0]["status"] == "SCHEDULED"
    assert body["metrics"]["service_level"] == 0.95
    assert body["simulated"] is True
    assert body["stale"] is False


def test_snapshot_reports_staleness_from_the_last_good_cache() -> None:
    """CONTRACT.md 5.5: a degraded snapshot is served marked stale, not faked.

    `build_snapshot` is the real client's cache-aware read (app/sim/client.py) —
    the *only* method that carries `stale`/`age_seconds`. Probing for a
    `get_snapshot` that does not exist made this route hardcode "fresh", so this
    test fails against that code.
    """
    body = client(build_app(client=RichSimulatorClient(stale=True))).get(
        "/api/v1/network/snapshot"
    ).json()
    assert body["stale"] is True
    assert body["age_seconds"] == 12.5


def test_snapshot_prefers_the_cache_aware_build_snapshot_when_the_client_has_one() -> None:
    body = client(build_app(client=RichSimulatorClient())).get(
        "/api/v1/network/snapshot"
    ).json()
    assert body["sim_time"] == "2026-09-29T09:55:00Z"
    assert body["stale"] is False


def test_snapshot_falls_back_to_a_one_shot_get_snapshot() -> None:
    """A client with only the one-shot getter is still accepted."""
    body = client(build_app(client=OneShotSnapshotClient())).get(
        "/api/v1/network/snapshot"
    ).json()
    assert body["sim_time"] == "2026-09-29T09:55:00Z"
    assert body["status"] == "RUNNING"


def test_snapshot_marks_an_aged_snapshot_stale_even_if_the_client_does_not() -> None:
    """The console must not present aged data as live."""
    snapshot = _snapshot(taken_at=time.monotonic() - 600.0)

    class AgedClient(FakeSimulatorClient):
        async def build_snapshot(self):
            return snapshot

    body = client(build_app(client=AgedClient())).get("/api/v1/network/snapshot").json()
    assert body["stale"] is True
    assert body["age_seconds"] > 0


def test_snapshot_failure_is_a_typed_502() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_routes"}
    response = client(build_app(client=broken)).get("/api/v1/network/snapshot")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"


# ---------------------------------------------------------------------------
# /api/v1/network/demand-history
# ---------------------------------------------------------------------------


def test_demand_history_passes_the_rows_through_validated() -> None:
    body = client(build_app(client=RichSimulatorClient())).get(
        "/api/v1/network/demand-history"
    ).json()
    assert body["count"] == 20
    assert body["observations"][0]["station_id"] in ("ST-1", "ST-2")
    assert body["simulated"] is True


def test_demand_history_honours_the_station_filter() -> None:
    body = client(build_app(client=RichSimulatorClient())).get(
        "/api/v1/network/demand-history", params={"station_id": "ST-1"}
    ).json()
    assert body["count"] == 10
    assert {row["station_id"] for row in body["observations"]} == {"ST-1"}


def test_demand_history_honours_the_fuel_type_filter() -> None:
    body = client(build_app(client=RichSimulatorClient())).get(
        "/api/v1/network/demand-history", params={"fuel_type": "diesel"}
    ).json()
    assert body["fuel_type"] == "DIESEL"
    assert {row["fuel_type"] for row in body["observations"]} == {"DIESEL"}


def test_demand_history_is_empty_not_an_error_when_there_is_no_history() -> None:
    body = client(build_app()).get("/api/v1/network/demand-history").json()
    assert body["count"] == 0
    assert body["observations"] == []


def test_demand_history_failure_is_a_typed_502() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_demand_history"}
    response = client(build_app(client=broken)).get("/api/v1/network/demand-history")
    assert response.status_code == 502
    assert error_code(response) == "simulator_error"
