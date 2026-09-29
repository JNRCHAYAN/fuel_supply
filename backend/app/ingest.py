"""Background ingestion: poll the simulator, persist what it returns.

The gap this module closes
--------------------------
``Repository.record_snapshot`` and ``Repository.record_demand_points``
(CONTRACT.md section 6) are the only write paths into the ``snapshots`` and
``demand_points`` tables, but nothing in the contract assigns anyone the job of
calling them.  The lifespan builds the :class:`SimulatorClient` and starts its
SSE hint stream; the API reads through both collaborators.  Between them sits
the hole: a service that answers every request with a 200 and persists nothing,
so ``GET /api/v1/forecast`` forecasts an empty series and every table stays at
zero rows.

:class:`SnapshotIngestor` is that missing caller.  It runs one asyncio task that
polls the simulator on an interval and writes what it finds:

* **Snapshots, but only when the tick advanced.**  The simulator steps about
  eight times a second, so recording unconditionally would insert thousands of
  near-identical rows a minute.  The tick of the last persisted snapshot is
  remembered and an unchanged tick is skipped.  The first poll always records.
* **Demand history, de-duplicated per ``(station_id, fuel_type)``.**  The
  simulator's ``/v1/demand-history`` is a sliding window, so the same ticks come
  back on every poll.  The highest persisted tick is tracked per series and
  re-seeded from the database on first sight of a series, so a restart does not
  re-insert the whole window.

Degrade, never crash (brief section 11)
---------------------------------------
Every poll is wrapped.  A simulator outage, a decode error, a database error or
an open circuit breaker is logged and the loop continues on the next interval.
A repository that is absent or whose database never initialised polls nothing
and says so once, rather than hammering a database that is already known broken.

This module owns the *schedule* only.  The simulator client owns resilience
(timeouts, retries, the breaker, the last-good cache) and the repository owns
the storage API; neither is reimplemented here.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Any

import structlog

__all__ = ["DEFAULT_DEMAND_LIMIT", "DEFAULT_INTERVAL_SECONDS", "SnapshotIngestor"]

log = structlog.get_logger(__name__)

#: Poll cadence.  Two seconds is fast enough that a tick change is picked up
#: almost immediately and slow enough that the database write rate stays sane.
DEFAULT_INTERVAL_SECONDS = 2.0

#: How many demand observations to request per poll.  The simulator answers with
#: the newest slice of its history, so this is a window not a total; the API's
#: forecaster reads a comparable window from the same endpoint.
DEFAULT_DEMAND_LIMIT = 1500

#: Baseline tick used for a series that has no rows yet.  ``-1`` (rather than
#: ``0``) means a legitimate tick ``0`` is not mistaken for "already stored".
_EMPTY_BASELINE = -1


def _text(value: Any) -> str:
    """A plain string for an enum member or anything string-like.

    The simulator models use ``class FuelType(str, Enum)``; ``str(member)``
    renders as ``"FuelType.DIESEL"`` rather than ``"DIESEL"`` on modern Python,
    which would store a fuel type the forecaster can never look up.  The enum's
    ``value`` is the wire spelling, so that is what is used.
    """
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _observation_fields(observation: Any) -> tuple[str, str, int, float] | None:
    """Project a demand observation onto ``(station_id, fuel_type, tick, liters)``.

    Accepts A2's ``DemandObservation`` (whose field is ``demand_liters``, with a
    ``liters`` alias), a plain ``DemandPoint``, or a 4-tuple.  A record missing a
    required field is skipped by the caller rather than guessed at.
    """
    if isinstance(observation, (tuple, list)):
        if len(observation) < 4:
            return None
        station_id, fuel_type, tick, liters = observation[:4]
    else:
        station_id = getattr(observation, "station_id", None)
        fuel_type = getattr(observation, "fuel_type", None)
        tick = getattr(observation, "tick", None)
        liters = getattr(observation, "liters", None)
        if liters is None:
            liters = getattr(observation, "demand_liters", None)
    if station_id is None or fuel_type is None or tick is None:
        return None
    try:
        return (_text(station_id), _text(fuel_type), int(tick), float(liters or 0.0))
    except (TypeError, ValueError):
        return None


class SnapshotIngestor:
    """One background task that polls the simulator and persists the results."""

    def __init__(
        self,
        simulator: Any,
        repository: Any,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        metrics: Any = None,
        *,
        demand_limit: int = DEFAULT_DEMAND_LIMIT,
    ) -> None:
        self._simulator = simulator
        self._repository = repository
        self._interval = max(0.0, float(interval_seconds))
        self._demand_limit = max(1, int(demand_limit))
        #: Accepted for symmetry with the other collaborators and deliberately
        #: unused: ``Metrics`` declares no ingestion hook, and inventing one (or
        #: calling an unrelated one) would be worse than recording nothing.  See
        #: ``app/observability/metrics.py``.
        self._metrics = metrics

        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        #: Last ``snapshot.tick`` written, so an unchanged tick is not re-stored.
        self._last_snapshot_tick: int | None = None
        #: ``(station_id, fuel_type)`` -> highest persisted tick.
        self._max_demand_tick: dict[tuple[str, str], int] = {}
        #: Whether the "repository is unusable" line has already been logged.
        self._unusable_logged = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def task(self) -> asyncio.Task[None] | None:
        """The running task, or ``None``.  Exposed so callers can inspect it."""
        return self._task

    async def start(self) -> None:
        """Create the polling task.  Idempotent: a second call is a no-op."""
        if self._task is not None and not self._task.done():
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="snapshot-ingestor")
        log.info("ingest.started", interval_seconds=self._interval)

    async def aclose(self) -> None:
        """Cancel the polling task and wait for it to finish.

        ``CancelledError`` is swallowed: shutdown cancelling the loop is the
        expected way for it to end, not a failure to report.
        """
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 - shutdown must never raise
            log.warning("ingest.task_error", error_type=type(exc).__name__)
        log.info("ingest.stopped")

    async def stop(self) -> None:
        """Alias for :meth:`aclose`, for callers that use ``stop`` naming."""
        await self.aclose()

    # -- one poll ----------------------------------------------------------

    async def poll_once(self) -> None:
        """Poll the simulator once and persist what it returns.

        Never raises.  Each phase is guarded independently so a failing snapshot
        read does not also suppress demand ingestion, and vice versa.
        """
        if not self._repository_usable():
            return
        for phase, step in (
            ("snapshot", self._persist_snapshot),
            ("demand", self._persist_demand),
        ):
            try:
                await step()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - degrade, never crash
                log.warning(
                    "ingest.phase_failed",
                    phase=phase,
                    error_type=type(exc).__name__,
                )

    # -- phases ------------------------------------------------------------

    async def _persist_snapshot(self) -> None:
        snapshot = await self._simulator.build_snapshot()
        tick = getattr(snapshot, "tick", None)
        try:
            tick_value = int(tick) if tick is not None else None
        except (TypeError, ValueError):
            tick_value = None

        if tick_value is not None and tick_value == self._last_snapshot_tick:
            return

        await self._repository.record_snapshot(snapshot)
        self._last_snapshot_tick = tick_value
        log.debug("ingest.snapshot_recorded", tick=tick_value)

    async def _persist_demand(self) -> None:
        history = await self._simulator.get_demand_history(limit=self._demand_limit)
        if not history:
            return

        grouped: dict[tuple[str, str], list[tuple[str, str, int, float]]] = {}
        for observation in history:
            fields = _observation_fields(observation)
            if fields is None:
                continue
            grouped.setdefault((fields[0], fields[1]), []).append(fields)

        new_points: list[tuple[str, str, int, float]] = []
        for (station_id, fuel_type), rows in grouped.items():
            baseline = await self._baseline_tick(station_id, fuel_type)
            if baseline is None:
                # The stored history could not be read, so re-inserting the
                # whole window would risk duplicates.  Skip this series until
                # the next poll can determine where it left off.
                continue
            for station, fuel, tick, liters in rows:
                if tick > baseline:
                    new_points.append((station, fuel, tick, liters))

        if not new_points:
            return

        count = await self._repository.record_demand_points(new_points)
        for station_id, fuel_type, tick, _liters in new_points:
            key = (station_id, fuel_type)
            current = self._max_demand_tick.get(key)
            self._max_demand_tick[key] = tick if current is None else max(current, tick)
        log.info(
            "ingest.demand_recorded",
            rows=count,
            series=len(grouped),
        )

    async def _baseline_tick(self, station_id: str, fuel_type: str) -> int | None:
        """Highest tick already stored for a series.

        In-memory once known; seeded from the database the first time a series is
        seen so a restart resumes instead of replaying the whole window.
        """
        key = (station_id, fuel_type)
        if key in self._max_demand_tick:
            return self._max_demand_tick[key]
        try:
            series = await self._repository.demand_series(station_id, fuel_type, limit=1)
        except Exception as exc:  # noqa: BLE001 - the caller decides what to do
            log.warning(
                "ingest.demand_baseline_failed",
                station_id=station_id,
                fuel_type=fuel_type,
                error_type=type(exc).__name__,
            )
            return None
        baseline = _EMPTY_BASELINE
        if series:
            try:
                baseline = int(getattr(series[-1], "tick", _EMPTY_BASELINE))
            except (TypeError, ValueError):
                baseline = _EMPTY_BASELINE
        self._max_demand_tick[key] = baseline
        return baseline

    # -- the loop ----------------------------------------------------------

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must outlive a bad poll
                log.warning("ingest.poll_failed", error_type=type(exc).__name__)
            try:
                await asyncio.sleep(self._interval)
            except asyncio.CancelledError:
                raise

    # -- repository usability ---------------------------------------------

    def _repository_usable(self) -> bool:
        """False when there is nothing to write to; logs that fact once."""
        repository = self._repository
        if repository is None:
            self._log_unusable("repository_not_configured")
            return False
        database = getattr(repository, "database", None)
        if database is not None and not getattr(database, "initialized", False):
            self._log_unusable("database_not_initialized")
            return False
        return True

    def _log_unusable(self, reason: str) -> None:
        if self._unusable_logged:
            return
        self._unusable_logged = True
        log.warning("ingest.repository_unusable", reason=reason)
