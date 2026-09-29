"""Tests for the storage workstream (A3) — CONTRACT.md section 6.

Every test builds its own database under pytest's ``tmp_path`` (or an in-memory
SQLite URL); nothing here ever touches ``backend/data/``.

A2 owns ``app.sim.models`` (``Snapshot``, ``DemandPoint``) and A6 owns
``app.intelligence.allocate`` (``Recommendation``).  Tests that need those types
use ``pytest.importorskip`` so a missing peer module is *reported as a skip by
name* rather than faked.
"""

from __future__ import annotations

import collections.abc as cabc
import sys
import time
import types
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import Any, Union, get_args, get_origin, get_type_hints

import pytest
import pytest_asyncio
from sqlalchemy import inspect as sa_inspect

# Make ``app.*`` importable regardless of where pytest was invoked from or
# whether A1's pytest configuration has landed yet.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.store.models import (  # noqa: E402
    AlertRecord,
    LLMStats,
    RecommendationRecord,
    demand_point_type,
    snapshot_type,
)
from app.store.db import Database  # noqa: E402
from app.store.repository import Repository  # noqa: E402

REQUIRED_TABLES = {"snapshots", "demand_points", "decisions", "alerts", "llm_calls"}


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path: Path):
    """A repository on a fresh on-disk SQLite database, per test."""
    database = Repository(f"sqlite+aiosqlite:///{tmp_path.joinpath('store.db').as_posix()}")
    await database.init()
    try:
        yield database
    finally:
        await database.dispose()


async def test_database_creates_its_parent_directory(tmp_path: Path) -> None:
    """A fresh checkout has no ``backend/data/``.

    SQLite will not create a missing directory: the engine raises
    ``OperationalError: unable to open database file`` on the first connection.
    That failure is caught upstream and reported as a degraded dependency, so
    the symptom is not a crash -- it is a service that serves 200s while
    persisting nothing at all.
    """
    target = tmp_path / "nested" / "data" / "fuel.db"
    assert not target.parent.exists()

    database = Database(f"sqlite+aiosqlite:///{target.as_posix()}")
    try:
        await database.init()
        assert target.exists(), "the database file was never created"
        assert database.initialized
    finally:
        await database.dispose()


async def _table_names(repository: Repository) -> set[str]:
    async with repository.engine.connect() as conn:
        return set(await conn.run_sync(lambda sync_conn: sa_inspect(sync_conn).get_table_names()))


def _blank(tp: Any) -> Any:
    """A type-appropriate placeholder, used to synthesise A2's dataclasses."""
    if tp is Any or tp is None:
        return None
    origin = get_origin(tp)
    if origin is Union or origin is types.UnionType:
        for arg in get_args(tp):
            if arg is not type(None):
                return _blank(arg)
        return None
    if origin is tuple:
        return ()
    if origin in (list, cabc.Sequence, cabc.Iterable):
        return []
    if origin in (dict, cabc.Mapping):
        return {}
    if origin in (set, frozenset, cabc.Set):
        return set()
    if origin is not None:
        return None
    if tp is str:
        return ""
    if tp is bool:
        return False
    if tp is int:
        return 0
    if tp is float:
        return 0.0
    if isinstance(tp, type) and is_dataclass(tp):
        return _make(tp)
    return None


def _make(cls: type, overrides: dict[str, Any] | None = None) -> Any:
    """Build an instance of a peer's frozen dataclass without knowing its fields."""
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if overrides and f.name in overrides:
            kwargs[f.name] = overrides[f.name]
        else:
            kwargs[f.name] = _blank(hints.get(f.name, Any))
    return cls(**kwargs)


def _nested_type(cls: type, field_name: str) -> Any:
    hint = get_type_hints(cls)[field_name]
    args = get_args(hint)
    assert args, f"{cls.__name__}.{field_name} is not parameterised: {hint!r}"
    return args[0]


def _sample_recommendation() -> types.SimpleNamespace:
    """A stand-in carrying A6's ``Recommendation`` shape (CONTRACT 7.3)."""
    return types.SimpleNamespace(
        id="rec-7-1",
        station_id="ST-1",
        depot_id="DP-1",
        route_id="RT-1",
        fuel_type="DIESEL",
        quantity_liters=1200.5,
        rationale="ST-1 runs dry in 3 ticks; DP-1 is the closest depot with stock.",
        constraints=("route max_shipment", "depot inventory"),
        expected_impact={"risk_before": 0.8, "risk_after": 0.2},
        alternatives=(
            types.SimpleNamespace(
                depot_id="DP-2",
                route_id="RT-9",
                quantity_liters=900.0,
                reason_not_chosen="longer transit",
                score=0.41,
            ),
        ),
        confidence=0.72,
        simulated=True,
    )


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_creates_every_contract_table(repo: Repository) -> None:
    tables = await _table_names(repo)
    assert REQUIRED_TABLES <= tables, f"missing tables: {REQUIRED_TABLES - tables}"


@pytest.mark.asyncio
async def test_second_init_is_idempotent(tmp_path: Path) -> None:
    repository = Repository(f"sqlite+aiosqlite:///{tmp_path.joinpath('idem.db').as_posix()}")
    try:
        await repository.init()
        before = await _table_names(repository)
        await repository.init()
        await repository.init()
        after = await _table_names(repository)
        assert before == after == REQUIRED_TABLES

        # and the database is still usable after the repeated init
        await repository.record_demand_point("ST-1", "DIESEL", 5, 10.0)
        series = await repository.demand_series("ST-1", "DIESEL")
        assert [p.tick for p in series] == [5]
    finally:
        await repository.dispose()


@pytest.mark.asyncio
async def test_in_memory_database_is_usable(tmp_path: Path) -> None:
    """``sqlite+aiosqlite:///:memory:`` must work — it needs StaticPool to share
    one connection between create_all and the queries."""
    repository = Repository("sqlite+aiosqlite:///:memory:")
    try:
        await repository.init()
        assert REQUIRED_TABLES <= await _table_names(repository)
        await repository.record_demand_point("ST-1", "PETROL", 1, 42.0)
        series = await repository.demand_series("ST-1", "PETROL")
        assert [(p.tick, p.liters) for p in series] == [(1, 42.0)]
    finally:
        await repository.dispose()


@pytest.mark.asyncio
async def test_repository_uses_the_given_url_and_writes_nowhere_else(
    repo: Repository, tmp_path: Path
) -> None:
    assert str(repo.engine.url).startswith("sqlite+aiosqlite:///")
    assert tmp_path.name in str(repo.engine.url)


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_latest_snapshot_is_none_on_an_empty_database(repo: Repository) -> None:
    assert await repo.latest_snapshot() is None


@pytest.mark.asyncio
async def test_record_and_read_back_a_snapshot(repo: Repository) -> None:
    sim = pytest.importorskip("app.sim.models", reason="A2 (app/sim/models.py) not present")
    Snapshot = sim.Snapshot
    depot_cls = _nested_type(Snapshot, "depots")
    station_cls = _nested_type(Snapshot, "stations")

    depot = _make(depot_cls, {"id": "DP-1", "name": "North Depot", "region_id": "R-1"})
    station = _make(station_cls, {"id": "ST-1", "name": "Central Station", "region_id": "R-1"})
    snapshot = _make(
        Snapshot,
        {
            "taken_at": time.monotonic(),
            "tick": 42,
            "sim_time": "2024-05-01T09:30:00Z",
            "status": "RUNNING",
            "depots": (depot,),
            "stations": (station,),
            "stale": False,
            "age_seconds": 1.5,
        },
    )

    await repo.record_snapshot(snapshot)
    loaded = await repo.latest_snapshot()

    assert loaded is not None
    assert loaded.tick == 42
    assert loaded.sim_time == "2024-05-01T09:30:00Z"
    assert loaded.status == "RUNNING"
    assert loaded.stale is False
    assert loaded.age_seconds == pytest.approx(1.5)

    # nested simulator types survive the round trip with their real types
    assert isinstance(loaded, Snapshot)
    assert isinstance(loaded.depots[0], depot_cls)
    assert isinstance(loaded.stations[0], station_cls)
    assert loaded.depots[0].id == "DP-1"
    assert loaded.depots[0].name == "North Depot"
    assert loaded.stations[0].id == "ST-1"
    assert loaded.depots == snapshot.depots
    assert loaded.stations == snapshot.stations
    assert loaded.metrics == snapshot.metrics
    assert loaded == snapshot


@pytest.mark.asyncio
async def test_latest_snapshot_returns_the_newest_of_several(repo: Repository) -> None:
    sim = pytest.importorskip("app.sim.models", reason="A2 (app/sim/models.py) not present")
    for tick, status in ((1, "RUNNING"), (2, "PAUSED"), (3, "RUNNING")):
        await repo.record_snapshot(
            _make(sim.Snapshot, {"tick": tick, "status": status, "sim_time": f"t{tick}"})
        )
    loaded = await repo.latest_snapshot()
    assert loaded is not None
    assert (loaded.tick, loaded.status) == (3, "RUNNING")


@pytest.mark.asyncio
async def test_stale_snapshot_keeps_its_degraded_markers(repo: Repository) -> None:
    sim = pytest.importorskip("app.sim.models", reason="A2 (app/sim/models.py) not present")
    await repo.record_snapshot(_make(sim.Snapshot, {"tick": 9, "stale": True, "age_seconds": 30.0}))
    loaded = await repo.latest_snapshot()
    assert loaded is not None and loaded.stale is True
    assert loaded.age_seconds == pytest.approx(30.0)


def test_snapshot_type_comes_from_a2() -> None:
    sim = pytest.importorskip("app.sim.models", reason="A2 (app/sim/models.py) not present")
    assert snapshot_type() is sim.Snapshot
    assert demand_point_type() is sim.DemandPoint


# ---------------------------------------------------------------------------
# demand history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demand_series_filters_orders_and_limits(repo: Repository) -> None:
    pytest.importorskip("app.sim.models", reason="A2 (app/sim/models.py) not present")
    # inserted out of order, and mixed across stations and fuels
    for tick, liters in ((3, 30.0), (1, 10.0), (5, 50.0), (2, 20.0), (4, 40.0)):
        await repo.record_demand_point("ST-1", "DIESEL", tick, liters)
    await repo.record_demand_point("ST-1", "PETROL", 1, 999.0)
    await repo.record_demand_point("ST-2", "DIESEL", 1, 888.0)

    series = await repo.demand_series("ST-1", "DIESEL")
    assert [p.tick for p in series] == [1, 2, 3, 4, 5], "series must be chronological"
    assert [p.liters for p in series] == [10.0, 20.0, 30.0, 40.0, 50.0]
    assert all(p.station_id == "ST-1" and p.fuel_type == "DIESEL" for p in series)
    assert all(isinstance(p, demand_point_type()) for p in series)

    # limit keeps the NEWEST points, still oldest-first
    limited = await repo.demand_series("ST-1", "DIESEL", limit=3)
    assert [p.tick for p in limited] == [3, 4, 5]

    assert await repo.demand_series("ST-1", "OCTANE") == []
    assert await repo.demand_series("ST-9", "DIESEL") == []
    assert await repo.demand_series("ST-1", "DIESEL", limit=0) == []


@pytest.mark.asyncio
async def test_record_demand_points_bulk_and_the_gateway_shape(repo: Repository) -> None:
    con = pytest.importorskip("app.sim.models", reason="A2 (app/sim/models.py) not present")
    points = [
        con.DemandPoint(station_id="ST-1", fuel_type="DIESEL", tick=t, liters=float(t))
        for t in (1, 2, 3)
    ]
    assert await repo.record_demand_points(points) == 3
    assert await repo.record_demand_points([]) == 0
    series = await repo.demand_series("ST-1", "DIESEL")
    assert [p.tick for p in series] == [1, 2, 3]
    assert all(isinstance(p, con.DemandPoint) for p in series)


# ---------------------------------------------------------------------------
# decisions — the audit history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decision_record_read_back_and_outcome(repo: Repository) -> None:
    rec = RecommendationRecord.from_recommendation(
        _sample_recommendation(), tick=7, sim_time="2024-05-01T09:30:00Z", policy="optimizer"
    )
    decision_id = await repo.record_decision(rec)
    assert decision_id > 0

    stored = await repo.get_decision(decision_id)
    assert stored is not None
    # why the decision was made, not just what it was
    assert stored.recommendation_id == "rec-7-1"
    assert stored.station_id == "ST-1"
    assert stored.depot_id == "DP-1"
    assert stored.route_id == "RT-1"
    assert stored.fuel_type == "DIESEL"
    assert stored.quantity_liters == pytest.approx(1200.5)
    assert "runs dry in 3 ticks" in stored.rationale
    assert stored.constraints == ("route max_shipment", "depot inventory")
    assert stored.expected_impact == {"risk_before": 0.8, "risk_after": 0.2}
    assert stored.confidence == pytest.approx(0.72)
    assert stored.policy == "optimizer"
    assert stored.simulated is True
    assert stored.tick == 7 and stored.sim_time == "2024-05-01T09:30:00Z"
    assert len(stored.alternatives) == 1
    assert stored.alternatives[0]["depot_id"] == "DP-2"
    assert stored.alternatives[0]["reason_not_chosen"] == "longer transit"

    # outcome: not recorded yet, then the operator submits it
    assert stored.submitted is None
    assert stored.allocation_id is None
    assert stored.outcome_recorded_at is None

    await repo.record_decision_outcome(
        decision_id, submitted=True, allocation_id=4242, note="approved by operator"
    )
    updated = await repo.get_decision(decision_id)
    assert updated is not None
    assert updated.submitted is True
    assert updated.allocation_id == 4242
    assert updated.outcome_note == "approved by operator"
    assert updated.outcome_recorded_at is not None
    # the recommendation itself is untouched by the outcome write
    assert updated.recommendation_id == stored.recommendation_id
    assert updated.rationale == stored.rationale


@pytest.mark.asyncio
async def test_decision_outcome_can_record_a_refusal(repo: Repository) -> None:
    decision_id = await repo.record_decision(
        RecommendationRecord(
            recommendation_id="rec-1",
            station_id="ST-2",
            depot_id="DP-3",
            route_id="RT-4",
            fuel_type="PETROL",
            quantity_liters=100.0,
            rationale="test",
        )
    )
    await repo.record_decision_outcome(
        decision_id, submitted=False, allocation_id=None, note="operator declined"
    )
    stored = await repo.get_decision(decision_id)
    assert stored is not None
    assert stored.submitted is False and stored.allocation_id is None
    assert stored.outcome_note == "operator declined"


@pytest.mark.asyncio
async def test_list_decisions_orders_newest_first_with_paging(repo: Repository) -> None:
    for i in range(5):
        await repo.record_decision(
            RecommendationRecord(
                recommendation_id=f"rec-{i}",
                station_id="ST-1",
                depot_id="DP-1",
                route_id="RT-1",
                fuel_type="DIESEL",
                quantity_liters=float(i),
                rationale=f"r{i}",
            )
        )
    everything = await repo.list_decisions()
    assert [d.recommendation_id for d in everything] == [
        "rec-4",
        "rec-3",
        "rec-2",
        "rec-1",
        "rec-0",
    ]
    assert [d.recommendation_id for d in await repo.list_decisions(limit=2)] == ["rec-4", "rec-3"]
    assert [d.recommendation_id for d in await repo.list_decisions(limit=2, offset=2)] == [
        "rec-2",
        "rec-1",
    ]
    assert await repo.list_decisions(limit=0) == []
    assert await repo.list_decisions(offset=99) == []


@pytest.mark.asyncio
async def test_get_decision_unknown_id_returns_none(repo: Repository) -> None:
    assert await repo.get_decision(12345) is None


@pytest.mark.asyncio
async def test_recording_an_outcome_for_an_unknown_decision_raises(repo: Repository) -> None:
    with pytest.raises(LookupError):
        await repo.record_decision_outcome(
            999, submitted=True, allocation_id=1, note="should not be silently dropped"
        )


@pytest.mark.asyncio
async def test_record_decision_accepts_a6s_recommendation(repo: Repository) -> None:
    allocate = pytest.importorskip(
        "app.intelligence.allocate", reason="A6 (app/intelligence/allocate.py) not present"
    )
    rec = getattr(allocate, "Recommendation", None)
    if rec is None:
        pytest.skip("A6 does not expose Recommendation yet")
    alternative = getattr(allocate, "Alternative", None)
    alternatives = ()
    if alternative is not None:
        alternatives = (
            alternative(
                depot_id="DP-2",
                route_id="RT-9",
                quantity_liters=5.0,
                reason_not_chosen="lower priority",
                score=0.1,
            ),
        )
    recommendation = rec(
        id="rec-1",
        station_id="ST-1",
        depot_id="DP-1",
        route_id="RT-1",
        fuel_type="DIESEL",
        quantity_liters=10.0,
        rationale="because",
        constraints=("route max_shipment",),
        expected_impact={"risk_before": 0.5, "risk_after": 0.1},
        alternatives=alternatives,
        confidence=0.5,
    )
    record = RecommendationRecord.from_recommendation(recommendation, tick=11)
    decision_id = await repo.record_decision(record)
    stored = await repo.get_decision(decision_id)
    assert stored is not None
    assert stored.recommendation_id == "rec-1"
    assert stored.constraints == ("route max_shipment",)
    assert stored.confidence == pytest.approx(0.5)
    assert len(stored.alternatives) == 1
    assert stored.alternatives[0]["route_id"] == "RT-9"


def test_recommendation_record_to_dict_is_json_ready() -> None:
    import json

    record = RecommendationRecord.from_recommendation(_sample_recommendation(), tick=3)
    payload = record.to_dict()
    assert json.loads(json.dumps(payload))["outcome"]["submitted"] is None
    assert payload["constraints"] == ["route max_shipment", "depot inventory"]


# ---------------------------------------------------------------------------
# alerts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_alert_active_only_filtering(repo: Repository) -> None:
    active_a = await repo.record_alert(
        AlertRecord(
            kind="demand_anomaly",
            severity="critical",
            entity_type="station",
            entity_id="ST-1",
            detected_at_tick=10,
            summary="demand 4x median",
            evidence={"median": 10.0, "observed": 40.0},
            confidence=0.9,
            active=True,
        )
    )
    active_b = await repo.record_alert(
        AlertRecord(
            kind="route_bottleneck",
            severity="warning",
            entity_type="route",
            entity_id="RT-2",
            detected_at_tick=11,
            summary="route constrained",
            active=True,
        )
    )
    resolved = await repo.record_alert(
        AlertRecord(
            kind="inventory_drop",
            severity="info",
            entity_type="depot",
            entity_id="DP-1",
            detected_at_tick=9,
            summary="slow drain",
            active=False,
        )
    )
    assert len({active_a, active_b, resolved}) == 3

    everything = await repo.list_alerts()
    assert [a.alert_id for a in everything] == [resolved, active_b, active_a]

    active_only = await repo.list_alerts(active_only=True)
    assert [a.alert_id for a in active_only] == [active_b, active_a]
    assert all(a.active for a in active_only)

    assert [a.alert_id for a in await repo.list_alerts(limit=1, active_only=True)] == [active_b]
    assert await repo.list_alerts(limit=0) == []
    assert (await repo.list_alerts(active_only=True, limit=99))[0].kind == "route_bottleneck"


@pytest.mark.asyncio
async def test_alert_round_trips_its_evidence(repo: Repository) -> None:
    alert_id = await repo.record_alert(
        AlertRecord(
            kind="supply_shortfall",
            severity="serious",
            entity_type="depot",
            entity_id="DP-9",
            detected_at_tick=3,
            summary="below reorder point",
            evidence={"shortfall_liters": 250.5, "nested": {"a": 1}},
            confidence=0.4,
        )
    )
    stored = (await repo.list_alerts())[0]
    assert stored.alert_id == alert_id
    assert stored.evidence == {"shortfall_liters": 250.5, "nested": {"a": 1}}
    assert stored.confidence == pytest.approx(0.4)
    assert stored.active is True


@pytest.mark.asyncio
async def test_alert_from_a5_risk_signal(repo: Repository) -> None:
    detect = pytest.importorskip(
        "app.intelligence.detect", reason="A5 (app/intelligence/detect.py) not present"
    )
    signal_cls = getattr(detect, "RiskSignal", None)
    severity_cls = getattr(detect, "Severity", None)
    if signal_cls is None or severity_cls is None:
        pytest.skip("A5 does not expose RiskSignal/Severity yet")
    signal = signal_cls(
        kind="demand_anomaly",
        severity=severity_cls.CRITICAL,
        entity_type="station",
        entity_id="ST-1",
        detected_at_tick=12,
        summary="spike",
        evidence={"z": 6.0},
        confidence=0.95,
    )
    await repo.record_alert(AlertRecord.from_signal(signal))
    stored = (await repo.list_alerts())[0]
    assert stored.severity == "critical", "the str-enum must be stored as its value"
    assert stored.kind == "demand_anomaly"
    assert stored.entity_id == "ST-1"
    assert stored.evidence == {"z": 6.0}


# ---------------------------------------------------------------------------
# LLM telemetry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_stats_on_an_empty_table(repo: Repository) -> None:
    stats = await repo.llm_stats()
    assert stats == LLMStats()
    assert stats.total_calls == 0
    assert stats.success_rate == 0.0 and stats.fallback_rate == 0.0
    assert stats.avg_latency_ms == 0.0
    assert stats.by_purpose == {}


@pytest.mark.asyncio
async def test_llm_stats_aggregates_mixed_ok_and_fallback_rows(repo: Repository) -> None:
    rows = [
        {"purpose": "explain_recommendation", "model": "deepseek-chat", "ok": True, "latency_ms": 100, "fallback_used": False},
        {"purpose": "explain_recommendation", "model": "deepseek-chat", "ok": True, "latency_ms": 200, "fallback_used": False},
        {"purpose": "summarize_network", "model": "deepseek-chat", "ok": False, "latency_ms": 400, "fallback_used": True},
        {"purpose": "investigate", "model": "deepseek-chat", "ok": True, "latency_ms": 300, "fallback_used": False},
        {"purpose": "investigate", "model": "deepseek-chat", "ok": False, "latency_ms": 500, "fallback_used": True},
    ]
    for row in rows:
        await repo.record_llm_call(**row)

    stats = await repo.llm_stats()
    assert stats.total_calls == 5
    assert stats.ok_calls == 3
    assert stats.failed_calls == 2
    assert stats.fallback_calls == 2
    assert stats.success_rate == pytest.approx(0.6)
    assert stats.fallback_rate == pytest.approx(0.4)
    assert stats.avg_latency_ms == pytest.approx(300.0)
    assert stats.by_purpose == {
        "explain_recommendation": 2,
        "investigate": 2,
        "summarize_network": 1,
    }
    assert stats.to_dict()["total_calls"] == 5


@pytest.mark.asyncio
async def test_llm_calls_survive_across_repository_instances(tmp_path: Path) -> None:
    """The stats are read back by a different process in the real deployment."""
    url = f"sqlite+aiosqlite:///{tmp_path.joinpath('persist.db').as_posix()}"
    first = Repository(url)
    await first.init()
    await first.record_llm_call(
        purpose="investigate", model="deepseek-chat", ok=False, latency_ms=250, fallback_used=True
    )
    await first.dispose()

    second = Repository(url)
    await second.init()
    stats = await second.llm_stats()
    await second.dispose()
    assert stats.total_calls == 1
    assert stats.failed_calls == 1 and stats.fallback_calls == 1
    assert stats.avg_latency_ms == pytest.approx(250.0)


@pytest.mark.asyncio
async def test_dispose_is_safe_to_call_twice(repo: Repository) -> None:
    await repo.dispose()
    await repo.aclose()
