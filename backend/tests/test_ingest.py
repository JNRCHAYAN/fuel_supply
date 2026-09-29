"""Tests for the background ingestor (``app/ingest.py``).

The ingestor is the only caller of ``Repository.record_snapshot`` and
``Repository.record_demand_points``; these tests cover the behaviour that makes
it safe to run on a loop against a live simulator:

* snapshots are only written when the tick advances,
* demand history is de-duplicated per ``(station_id, fuel_type)``,
* a simulator or database failure degrades the poll, never the loop,
* the start/stop lifecycle is idempotent and cancels cleanly,
* and the demand it persists actually reaches the forecast path.

Every test builds a real ``Repository`` on a throwaway SQLite file under
pytest's ``tmp_path``; ``backend/data/`` is never touched.  The simulator is a
local stub with the real ``build_snapshot`` / ``get_demand_history`` signatures,
so no test needs a live simulator.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest_asyncio
from sqlalchemy import func, select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings  # noqa: E402
from app.ingest import SnapshotIngestor  # noqa: E402
from app.sim.models import (  # noqa: E402
    DemandObservation,
    Metrics as SimMetrics,
    Snapshot,
)
from app.store.models import DemandPointRow, SnapshotRow  # noqa: E402
from app.store.repository import Repository  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures and fakes
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def repo(tmp_path: Path):
    """A real repository on a fresh on-disk SQLite database, per test."""
    repository = Repository(f"sqlite+aiosqlite:///{tmp_path.joinpath('ingest.db').as_posix()}")
    await repository.init()
    try:
        yield repository
    finally:
        await repository.dispose()


def _make_snapshot(tick: int) -> Snapshot:
    """A real ``Snapshot``, so ``latest_snapshot`` can deserialize it back."""
    return Snapshot(
        taken_at=1000.0 + tick,
        tick=tick,
        sim_time="2026-02-10T12:00:00",
        status="RUNNING",
        depots=(),
        stations=(),
        routes=(),
        regions=(),
        supply_arrivals=(),
        events=(),
        metrics=SimMetrics(
            served_demand_liters=0.0,
            unmet_demand_liters=0.0,
            service_level=1.0,
            allocation_liters=0.0,
            allocation_failures=0,
        ),
    )


def _observation(station_id: str, fuel_type: str, tick: int, liters: float) -> DemandObservation:
    return DemandObservation(
        id=tick * 10,
        station_id=station_id,
        fuel_type=fuel_type,
        tick=tick,
        sim_time="2026-02-10T12:00:00",
        demand_liters=liters,
        served_liters=0.0,
        unmet_liters=liters,
    )


class FakeSimulator:
    """Stub with the real client signatures used by the ingestor."""

    def __init__(self, *, tick: int = 1, demand: list[Any] | None = None) -> None:
        self.tick = tick
        self.demand: list[Any] = list(demand or ())
        self.snapshot_calls = 0
        self.demand_calls = 0
        self.fail_on: set[str] = set()

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail_on:
            raise RuntimeError(f"simulator {name} is unavailable")

    async def build_snapshot(self) -> Snapshot:
        self.snapshot_calls += 1
        self._maybe_fail("build_snapshot")
        return _make_snapshot(self.tick)

    async def get_demand_history(
        self, station_id: str | None = None, limit: int = 500
    ) -> list[Any]:
        self.demand_calls += 1
        self._maybe_fail("get_demand_history")
        return list(self.demand)[-limit:]


async def _count(repository: Repository, model: Any) -> int:
    async with repository.database.session() as session:
        return int((await session.execute(select(func.count()).select_from(model))).scalar_one())


# ---------------------------------------------------------------------------
# 1-3. snapshots persist on tick change only
# ---------------------------------------------------------------------------


async def test_one_poll_persists_exactly_one_snapshot(repo: Repository) -> None:
    simulator = FakeSimulator(tick=5)
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.poll_once()

    assert await _count(repo, SnapshotRow) == 1
    latest = await repo.latest_snapshot()
    assert latest is not None
    assert latest.tick == 5


async def test_repeated_polls_at_an_unchanged_tick_persist_one_row(repo: Repository) -> None:
    simulator = FakeSimulator(tick=7)
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.poll_once()
    await ingestor.poll_once()
    await ingestor.poll_once()

    assert await _count(repo, SnapshotRow) == 1


async def test_an_advanced_tick_persists_a_second_row(repo: Repository) -> None:
    simulator = FakeSimulator(tick=1)
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.poll_once()
    simulator.tick = 2
    await ingestor.poll_once()

    assert await _count(repo, SnapshotRow) == 2
    latest = await repo.latest_snapshot()
    assert latest is not None and latest.tick == 2


# ---------------------------------------------------------------------------
# 4-5. demand history persists and de-duplicates
# ---------------------------------------------------------------------------


async def test_demand_points_are_persisted_oldest_first(repo: Repository) -> None:
    # The simulator returns newest-first; the store must still read oldest-first.
    simulator = FakeSimulator(
        demand=[
            _observation("ST-1", "DIESEL", 3, 30.0),
            _observation("ST-1", "DIESEL", 2, 20.0),
            _observation("ST-1", "DIESEL", 1, 10.0),
        ]
    )
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.poll_once()

    series = await repo.demand_series("ST-1", "DIESEL")
    assert [point.tick for point in series] == [1, 2, 3]
    assert [point.liters for point in series] == [10.0, 20.0, 30.0]


async def test_second_poll_of_the_same_window_does_not_duplicate(repo: Repository) -> None:
    simulator = FakeSimulator(
        demand=[
            _observation("ST-1", "DIESEL", 1, 10.0),
            _observation("ST-1", "DIESEL", 2, 20.0),
            _observation("ST-2", "OCTANE", 1, 5.0),
        ]
    )
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.poll_once()
    after_first = await _count(repo, DemandPointRow)
    await ingestor.poll_once()

    assert await _count(repo, DemandPointRow) == after_first == 3
    assert len(await repo.demand_series("ST-1", "DIESEL")) == 2
    assert len(await repo.demand_series("ST-2", "OCTANE")) == 1


async def test_demand_duplicate_suppression_is_per_series(repo: Repository) -> None:
    """A newer tick on one series must not suppress another series' older tick."""
    simulator = FakeSimulator(
        demand=[
            _observation("ST-1", "DIESEL", 1, 10.0),
            _observation("ST-2", "DIESEL", 1, 10.0),
        ]
    )
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)
    await ingestor.poll_once()

    # ST-1 advances; ST-2 replays its old tick plus a new one.
    simulator.demand = [
        _observation("ST-1", "DIESEL", 2, 20.0),
        _observation("ST-2", "DIESEL", 1, 10.0),
        _observation("ST-2", "DIESEL", 2, 25.0),
    ]
    await ingestor.poll_once()

    assert [p.tick for p in await repo.demand_series("ST-1", "DIESEL")] == [1, 2]
    assert [p.tick for p in await repo.demand_series("ST-2", "DIESEL")] == [1, 2]


async def test_restart_resumes_from_stored_history(repo: Repository) -> None:
    """A fresh ingestor must seed its baseline from the database, not replay."""
    simulator = FakeSimulator(
        demand=[
            _observation("ST-1", "DIESEL", 1, 10.0),
            _observation("ST-1", "DIESEL", 2, 20.0),
        ]
    )
    await SnapshotIngestor(simulator, repo, interval_seconds=0.01).poll_once()

    # Simulate a restart: a brand-new ingestor with no in-memory baseline.
    restart = SnapshotIngestor(simulator, repo, interval_seconds=0.01)
    await restart.poll_once()

    assert await _count(repo, DemandPointRow) == 2


# ---------------------------------------------------------------------------
# 6-7. failures degrade, they do not crash
# ---------------------------------------------------------------------------


async def test_a_simulator_error_does_not_kill_the_loop(repo: Repository) -> None:
    simulator = FakeSimulator(tick=1)
    simulator.fail_on.add("build_snapshot")
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.start()
    await asyncio.sleep(0.05)
    assert ingestor.task is not None and not ingestor.task.done()

    # Heal the simulator; a later poll must succeed on the same loop.
    simulator.fail_on.discard("build_snapshot")
    simulator.tick = 9
    latest = None
    for _ in range(200):  # bounded wait, so a slow machine cannot flake the test
        latest = await repo.latest_snapshot()
        if latest is not None:
            break
        await asyncio.sleep(0.01)

    assert ingestor.task is not None and not ingestor.task.done()
    assert latest is not None and latest.tick == 9

    await ingestor.aclose()


async def test_a_broken_demand_endpoint_does_not_block_snapshots(repo: Repository) -> None:
    simulator = FakeSimulator(tick=4)
    simulator.fail_on.add("get_demand_history")
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.poll_once()

    assert await _count(repo, SnapshotRow) == 1


async def test_an_uninitialized_database_polls_nothing(tmp_path: Path) -> None:
    repository = Repository(f"sqlite+aiosqlite:///{tmp_path.joinpath('raw.db').as_posix()}")
    # Deliberately never ``init()``-ed: the tables do not exist.
    assert repository.database.initialized is False
    simulator = FakeSimulator(tick=1)
    ingestor = SnapshotIngestor(simulator, repository, interval_seconds=0.01)

    await ingestor.poll_once()  # must not raise

    assert simulator.snapshot_calls == 0
    assert simulator.demand_calls == 0
    assert ingestor._unusable_logged is True
    await repository.dispose()


async def test_a_repository_that_raises_is_caught(repo: Repository) -> None:
    class ExplodingRepository:
        database = SimpleNamespace(initialized=True)

        async def record_snapshot(self, snapshot: Any) -> None:
            raise RuntimeError("write failed")

        async def get_demand_history(self) -> None:  # pragma: no cover - not called
            raise AssertionError

    simulator = FakeSimulator(tick=1)
    ingestor = SnapshotIngestor(simulator, ExplodingRepository(), interval_seconds=0.01)

    await ingestor.poll_once()  # must not raise


# ---------------------------------------------------------------------------
# 8. lifecycle
# ---------------------------------------------------------------------------


async def test_start_twice_does_not_create_two_tasks(repo: Repository) -> None:
    simulator = FakeSimulator(tick=1)
    ingestor = SnapshotIngestor(simulator, repo, interval_seconds=0.01)

    await ingestor.start()
    first = ingestor.task
    await ingestor.start()

    assert ingestor.task is first
    assert first is not None

    await ingestor.aclose()
    assert ingestor.task is None
    assert first.done()


async def test_aclose_is_safe_before_start(repo: Repository) -> None:
    ingestor = SnapshotIngestor(FakeSimulator(), repo, interval_seconds=0.01)
    await ingestor.aclose()  # no task; must not raise
    assert ingestor.task is None


# ---------------------------------------------------------------------------
# 9. end to end: ingested demand reaches GET /api/v1/forecast
# ---------------------------------------------------------------------------


def _boot_settings(tmp_path: Path) -> Settings:
    """Boot the real app against a throwaway database and a dead simulator."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        simulator_base_url="http://127.0.0.1:9",
        simulator_timeout_seconds=0.25,
        simulator_max_retries=1,
        circuit_reset_seconds=1,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'e2e.db').as_posix()}",
        deepseek_api_key="",
        llm_enabled=False,
        log_level="WARNING",
    )


def _forecast_snapshot() -> SimpleNamespace:
    return SimpleNamespace(
        tick=500,
        sim_time="2026-02-10T12:00:00",
        status="RUNNING",
        stale=False,
        age_seconds=0.0,
        routes=(),
        stations=(
            SimpleNamespace(
                id="ST-1",
                inventory={"DIESEL": 5000.0, "PETROL": 4000.0},
            ),
        ),
    )


class ForecastStub:
    """Snapshot source for the forecast route; history deliberately empty.

    Returning no history forces ``build_history`` onto the repository fallback,
    which is the path the persisted demand has to travel for the assertion below
    to mean anything.
    """

    async def get_snapshot(self) -> SimpleNamespace:
        return _forecast_snapshot()

    async def get_demand_history(
        self, station_id: str | None = None, limit: int = 500
    ) -> list[Any]:
        return []


def test_forecast_is_driven_by_persisted_demand(tmp_path: Path) -> None:
    """Ingestion writes demand that the forecast endpoint then forecasts on.

    Runs through the real ``app.main.create_app`` lifespan.  TestClient owns the
    event loop the lifespan (and therefore the repository) lives on, so the
    ingestor is driven through ``client.portal`` to keep it on that same loop.
    """
    from fastapi.testclient import TestClient

    from app.api import schemas
    from app.main import create_app

    app = create_app(_boot_settings(tmp_path))
    demand = [
        _observation("ST-1", "DIESEL", tick, 100.0 + tick)
        for tick in range(1, 31)
    ]
    with TestClient(app) as client:
        repository = app.state.repository
        ingestor = SnapshotIngestor(
            FakeSimulator(tick=1, demand=demand), repository, interval_seconds=0.01
        )
        client.portal.call(ingestor.start)
        persisted: list[Any] = []
        for _ in range(40):  # bounded wait: a slow machine must not flake this
            persisted = client.portal.call(repository.demand_series, "ST-1", "DIESEL")
            if persisted:
                break
            client.portal.call(asyncio.sleep, 0.05)
        client.portal.call(ingestor.aclose)

        # The persisted rows are non-empty — the precondition.
        assert persisted, "ingestion persisted no demand; the forecast cannot use it"

        app.dependency_overrides[schemas.get_simulator_client] = lambda: ForecastStub()
        body = client.get("/api/v1/forecast").json()

    assert body["count"] >= 1
    row = next(r for r in body["forecasts"] if r["station_id"] == "ST-1")
    assert row["fuel_type"] == "DIESEL"
    assert any(point["liters"] > 0.0 for point in row["points"]), row["points"]
    assert row["method"] != "fallback"


def test_lifespan_starts_and_stops_the_ingestor(tmp_path: Path) -> None:
    """``app.main`` must actually own the ingestion task, and shut it down."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    # Boots against a dead simulator, so the loop must survive being degraded.
    app = create_app(_boot_settings(tmp_path))
    with TestClient(app):
        ingestor = getattr(app.state, "ingestor", None)
        assert ingestor is not None, "the lifespan did not publish an ingestor"
        assert ingestor.task is not None and not ingestor.task.done()

    # Leaving the context runs ``_shutdown``, which stops the task.
    assert ingestor.task is None
