"""What `/api/v1/network/*` must never hide from an operator.

Three things the snapshot surface has to expose honestly, asserted through
HTTP rather than through the helpers that build it:

* that a reading is the last good answer rather than a live one (`stale`,
  `age_seconds`) -- including on the assembly path where no client method
  supplies a staleness flag;
* the `SimInstance` fields that make a run reproducible (`seed`, `scenario_id`,
  `scenario_version`, `tick_minutes`), which will be silently absent if the
  snapshot is the only source;
* one depot / one station by id, with a typed 404 for an unknown id.
"""

from __future__ import annotations

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

SNAPSHOT_PATH = "/api/v1/network/snapshot"


def _snapshot(*, stale: bool, age_seconds: float, taken_at: float = 0.0):
    return SimpleNamespace(
        taken_at=taken_at,
        tick=TICK,
        sim_time="2026-09-29T10:00:00Z",
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


class CachedSnapshotClient(FakeSimulatorClient):
    """The real client's surface: the cache-aware read is `build_snapshot`."""

    def __init__(self, *, stale: bool, age_seconds: float, **kwargs) -> None:
        super().__init__(**kwargs)
        self._stale = stale
        self._age = age_seconds

    async def build_snapshot(self):
        return _snapshot(stale=self._stale, age_seconds=self._age)


class UnreadableInstanceClient(CachedSnapshotClient):
    """A snapshot is available; the instance read is not."""

    async def get_instance(self):
        raise RuntimeError("instance read failed")


class AgedCacheClient(FakeSimulatorClient):
    """No cache-aware read at all, breaker open, and a reportable cache age."""

    def __init__(self, *, age: float | None) -> None:
        super().__init__(breaker_state="open")
        self._age = age

    def last_good_age(self, key: str) -> float | None:
        return self._age


# ---------------------------------------------------------------------------
# Staleness end to end
# ---------------------------------------------------------------------------


def test_a_stale_snapshot_says_so_on_the_wire() -> None:
    body = client(
        build_app(client=CachedSnapshotClient(stale=True, age_seconds=12.5))
    ).get(SNAPSHOT_PATH).json()
    assert body["stale"] is True
    assert body["age_seconds"] == 12.5
    # The data is still there -- degraded is not empty.
    assert len(body["depots"]) == len(DEPOTS)


def test_a_fresh_snapshot_is_not_relabelled_stale() -> None:
    body = client(
        build_app(client=CachedSnapshotClient(stale=False, age_seconds=0.0))
    ).get(SNAPSHOT_PATH).json()
    assert body["stale"] is False
    assert body["age_seconds"] == 0.0


def test_assembled_snapshot_with_an_open_breaker_is_not_called_fresh() -> None:
    """The hand-assembled path has no staleness flag of its own.

    When the breaker is open the client's per-endpoint reads serve their cache,
    so a snapshot built from them is old whether or not anything says so.
    """
    body = client(build_app(client=AgedCacheClient(age=42.0))).get(SNAPSHOT_PATH).json()
    assert body["stale"] is True
    assert body["age_seconds"] == 42.0

    # No age to report -- still not fresh.
    unknown = client(build_app(client=AgedCacheClient(age=None))).get(SNAPSHOT_PATH).json()
    assert unknown["stale"] is True


def test_assembled_snapshot_with_a_closed_breaker_is_fresh() -> None:
    body = client(build_app(client=FakeSimulatorClient())).get(SNAPSHOT_PATH).json()
    assert body["stale"] is False
    assert body["age_seconds"] == 0.0


def test_staleness_reaches_the_per_entity_reads_too() -> None:
    body = client(
        build_app(client=CachedSnapshotClient(stale=True, age_seconds=3.0))
    ).get("/api/v1/depots/DP-1").json()
    assert body["stale"] is True
    assert body["age_seconds"] == 3.0


# ---------------------------------------------------------------------------
# Instance provenance
# ---------------------------------------------------------------------------


def test_instance_fields_are_reachable_from_the_snapshot_response() -> None:
    """Guide 4.2: these are what make a run reproducible."""
    body = client(build_app()).get(SNAPSHOT_PATH).json()
    instance = body["instance"]
    assert instance is not None, "the SimInstance must not be silently dropped"
    assert instance["id"] == 1
    assert instance["scenario_id"] == "scenario-a"
    assert instance["scenario_version"] == "1.0"
    assert instance["seed"] == 7
    assert instance["tick_minutes"] == 5
    assert instance["status"] == "RUNNING"


def test_instance_is_null_not_fabricated_when_it_cannot_be_read() -> None:
    response = client(
        build_app(client=UnreadableInstanceClient(stale=False, age_seconds=0.0))
    ).get(SNAPSHOT_PATH)
    assert response.status_code == 200
    body = response.json()
    assert body["instance"] is None
    # The snapshot itself is unaffected by a failed provenance read.
    assert body["tick"] == TICK
    assert len(body["stations"]) == len(STATIONS)


# ---------------------------------------------------------------------------
# Per-entity reads (guide 4.5 / 4.6)
# ---------------------------------------------------------------------------


def test_depot_by_id_returns_that_depot() -> None:
    body = client(build_app()).get("/api/v1/depots/DP-1").json()
    assert body["id"] == "DP-1"
    assert body["name"] == "Depot One"
    assert body["dispatch_capacity_per_tick"] == 5000.0


def test_station_by_id_returns_that_station() -> None:
    body = client(build_app()).get("/api/v1/stations/ST-2").json()
    assert body["id"] == "ST-2"
    assert body["demand_profile"] == "highway"
    assert body["inventory"]["DIESEL"] == 300.0


def test_an_unknown_depot_is_a_typed_404() -> None:
    response = client(build_app()).get("/api/v1/depots/depot-nowhere")
    assert response.status_code == 404
    assert error_code(response) == "depot_not_found"
    assert response.json()["detail"]["simulated"] is True


def test_an_unknown_station_is_a_typed_404() -> None:
    response = client(build_app()).get("/api/v1/stations/station-nowhere")
    assert response.status_code == 404
    assert error_code(response) == "station_not_found"


def test_a_depot_id_is_not_a_station_id() -> None:
    """The two namespaces are separate: a depot id must not resolve as a station."""
    response = client(build_app()).get("/api/v1/stations/DP-1")
    assert response.status_code == 404
    assert error_code(response) == "station_not_found"
