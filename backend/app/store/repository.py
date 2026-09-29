"""``Repository`` — the storage API for the whole backend (CONTRACT.md section 6).

Owned by A3.  Every method in section 6 is implemented here with the contract's
name and parameter list, plus a few operational extras (``dispose``, ``ping``,
``record_demand_points``) that the API lifespan and the scheduler need.

Reads that must not take the API down (a malformed JSON column, an empty table)
degrade with a log line rather than an exception; writes never swallow an error.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Iterable

from sqlalchemy import case, func, select

from app.store.db import Database, utc_now
from app.store.models import (
    AlertRecord,
    AlertRow,
    DecisionRow,
    DemandPointRow,
    LLMStats,
    LlmCallRow,
    RecommendationRecord,
    SnapshotRow,
    demand_point_type,
    deserialize_snapshot,
    serialize_snapshot,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, A2 owns these at runtime
    from app.sim.models import DemandPoint, Snapshot

logger = logging.getLogger(__name__)

__all__ = [
    "AlertRecord",
    "LLMStats",
    "RecommendationRecord",
    "Repository",
]


def _coerce_url(value: Any) -> str | None:
    """Accept a URL string, A1's ``Settings``, or ``None`` (settings/default)."""
    if value is None or isinstance(value, str):
        return value
    url = getattr(value, "database_url", None)
    return url if isinstance(url, str) else str(value)


def _loads(text: str | None, default: Any) -> Any:
    """Read a JSON column, degrading to ``default`` on malformed content."""
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        logger.warning("stored JSON column is malformed; substituting default", exc_info=True)
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


def _record_from_row(row: DecisionRow) -> RecommendationRecord:
    return RecommendationRecord(
        recommendation_id=row.recommendation_id,
        station_id=row.station_id,
        depot_id=row.depot_id,
        route_id=row.route_id,
        fuel_type=row.fuel_type,
        quantity_liters=row.quantity_liters,
        rationale=row.rationale,
        tick=row.tick,
        sim_time=row.sim_time,
        policy=row.policy,
        constraints=tuple(str(c) for c in _loads(row.constraints_json, [])),
        expected_impact={str(k): float(v) for k, v in _loads(row.expected_impact_json, {}).items()},
        alternatives=tuple(_loads(row.alternatives_json, [])),
        confidence=row.confidence,
        simulated=row.simulated,
        decision_id=row.id,
        submitted=row.submitted,
        allocation_id=row.allocation_id,
        outcome_note=row.outcome_note,
        recorded_at=row.recorded_at,
        outcome_recorded_at=row.outcome_recorded_at,
    )


def _alert_from_row(row: AlertRow) -> AlertRecord:
    return AlertRecord(
        kind=row.kind,
        severity=row.severity,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        detected_at_tick=row.detected_at_tick,
        summary=row.summary,
        evidence=_loads(row.evidence_json, {}),
        confidence=row.confidence,
        active=row.active,
        alert_id=row.id,
        recorded_at=row.recorded_at,
    )


class Repository:
    """Async storage for snapshots, demand history, decisions, alerts and LLM calls."""

    def __init__(
        self,
        database_url: Any = None,
        *,
        database: Database | None = None,
        engine: Any = None,
        echo: bool = False,
    ) -> None:
        if isinstance(database_url, Database):
            database, database_url = database_url, None
        self._database = database or Database(_coerce_url(database_url), echo=echo, engine=engine)

    # -- lifecycle ---------------------------------------------------------

    @property
    def database(self) -> Database:
        return self._database

    @property
    def engine(self) -> Any:
        return self._database.engine

    async def init(self) -> None:
        """Create the tables.  Idempotent (CONTRACT.md section 6)."""
        await self._database.init()

    async def dispose(self) -> None:
        """Release the connection pool.  Called from the API lifespan."""
        await self._database.dispose()

    async def aclose(self) -> None:
        """Alias for :meth:`dispose`, for callers that use ``aclose`` naming."""
        await self.dispose()

    async def ping(self) -> bool:
        """Cheap database health probe for ``/api/v1/status``."""
        return await self._database.ping()

    # -- snapshots ---------------------------------------------------------

    async def record_snapshot(self, snap: "Snapshot") -> None:
        """Persist an assembled snapshot, including its stale/last-good flags."""
        payload = serialize_snapshot(snap)
        row = SnapshotRow(
            taken_at=float(getattr(snap, "taken_at", 0.0) or 0.0),
            tick=int(getattr(snap, "tick", 0) or 0),
            sim_time=str(getattr(snap, "sim_time", "") or ""),
            status=str(getattr(snap, "status", "") or ""),
            stale=bool(getattr(snap, "stale", False)),
            age_seconds=float(getattr(snap, "age_seconds", 0.0) or 0.0),
            depot_count=len(getattr(snap, "depots", ()) or ()),
            station_count=len(getattr(snap, "stations", ()) or ()),
            payload=payload,
            recorded_at=utc_now(),
        )
        async with self._database.session() as session:
            session.add(row)

    async def latest_snapshot(self) -> "Snapshot | None":
        """The most recently recorded snapshot, or ``None`` when none exist."""
        stmt = select(SnapshotRow).order_by(SnapshotRow.id.desc()).limit(1)
        async with self._database.session() as session:
            row = (await session.execute(stmt)).scalars().first()
        if row is None:
            return None
        return deserialize_snapshot(row.payload)

    # -- demand history ----------------------------------------------------

    async def record_demand_point(
        self, station_id: str, fuel_type: str, tick: int, liters: float
    ) -> None:
        """Record one demand observation."""
        await self.record_demand_points([(station_id, fuel_type, int(tick), float(liters))])

    async def record_demand_points(self, points: Iterable[Any]) -> int:
        """Record many observations in one transaction; returns the row count.

        Accepts A2's ``DemandPoint`` objects or ``(station_id, fuel_type, tick,
        liters)`` tuples.  Not in CONTRACT.md section 6 — added because the
        gateway fetches up to 500 points per station and one transaction per
        point is wasteful.
        """
        rows = [self._demand_row(p) for p in points]
        if not rows:
            return 0
        async with self._database.session() as session:
            session.add_all(rows)
        return len(rows)

    @staticmethod
    def _demand_row(point: Any) -> DemandPointRow:
        if isinstance(point, (tuple, list)):
            station_id, fuel_type, tick, liters = point
        else:
            station_id = getattr(point, "station_id")
            fuel_type = getattr(point, "fuel_type")
            tick = getattr(point, "tick")
            liters = getattr(point, "liters")
        return DemandPointRow(
            station_id=str(station_id),
            fuel_type=str(fuel_type),
            tick=int(tick),
            liters=float(liters),
            recorded_at=utc_now(),
        )

    async def demand_series(
        self, station_id: str, fuel_type: str, limit: int = 500
    ) -> list["DemandPoint"]:
        """The newest ``limit`` observations, returned oldest-first.

        The forecasters consume a chronological series, so the window is taken
        from the tail of the history and then reversed into ascending tick order.
        """
        if limit <= 0:
            return []
        stmt = (
            select(DemandPointRow)
            .where(
                DemandPointRow.station_id == station_id,
                DemandPointRow.fuel_type == fuel_type,
            )
            .order_by(DemandPointRow.tick.desc(), DemandPointRow.id.desc())
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = list((await session.execute(stmt)).scalars().all())
        rows.reverse()
        point_cls = demand_point_type()
        return [
            point_cls(
                station_id=r.station_id,
                fuel_type=r.fuel_type,
                tick=r.tick,
                liters=r.liters,
            )
            for r in rows
        ]

    # -- decisions (audit history) ----------------------------------------

    async def record_decision(self, rec: RecommendationRecord) -> int:
        """Persist a recommendation and why it was made; returns the decision id.

        The stored row carries the rationale, the binding constraints, the
        expected impact, the alternatives that were considered, the confidence
        and the policy that produced it, so the decision can be reconstructed
        later (brief section 20).
        """
        row = DecisionRow(
            recommendation_id=rec.recommendation_id,
            tick=rec.tick,
            sim_time=rec.sim_time,
            station_id=rec.station_id,
            depot_id=rec.depot_id,
            route_id=rec.route_id,
            fuel_type=rec.fuel_type,
            quantity_liters=rec.quantity_liters,
            rationale=rec.rationale,
            constraints_json=_dumps(list(rec.constraints)),
            expected_impact_json=_dumps(dict(rec.expected_impact)),
            alternatives_json=_dumps(list(rec.alternatives)),
            confidence=rec.confidence,
            policy=rec.policy,
            simulated=rec.simulated,
            recorded_at=rec.recorded_at if rec.recorded_at is not None else utc_now(),
            submitted=rec.submitted,
            allocation_id=rec.allocation_id,
            outcome_note=rec.outcome_note,
            outcome_recorded_at=rec.outcome_recorded_at,
        )
        async with self._database.session() as session:
            session.add(row)
            await session.flush()
            decision_id = int(row.id)
        return decision_id

    async def record_decision_outcome(
        self,
        decision_id: int,
        *,
        submitted: bool,
        allocation_id: int | None,
        note: str | None,
    ) -> None:
        """Attach the human-review outcome to a stored decision.

        Raises ``LookupError`` for an unknown id: silently discarding an outcome
        would lose the audit trail, and the API turns this into a 404.
        """
        async with self._database.session() as session:
            row = await session.get(DecisionRow, decision_id)
            if row is None:
                raise LookupError(f"no decision with id {decision_id}")
            row.submitted = submitted
            row.allocation_id = allocation_id
            row.outcome_note = note
            row.outcome_recorded_at = utc_now()

    async def list_decisions(
        self, limit: int = 50, offset: int = 0
    ) -> list[RecommendationRecord]:
        """Decision audit history, newest first."""
        if limit <= 0:
            return []
        stmt = (
            select(DecisionRow)
            .order_by(DecisionRow.id.desc())
            .offset(max(offset, 0))
            .limit(limit)
        )
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_record_from_row(r) for r in rows]

    async def get_decision(self, decision_id: int) -> RecommendationRecord | None:
        """One decision with its outcome, or ``None`` for an unknown id."""
        async with self._database.session() as session:
            row = await session.get(DecisionRow, decision_id)
        return None if row is None else _record_from_row(row)

    # -- alerts ------------------------------------------------------------

    async def record_alert(self, alert: AlertRecord) -> int:
        """Persist a risk alert / shortage alert; returns the alert id."""
        row = AlertRow(
            kind=alert.kind,
            severity=alert.severity,
            entity_type=alert.entity_type,
            entity_id=alert.entity_id,
            detected_at_tick=alert.detected_at_tick,
            summary=alert.summary,
            evidence_json=_dumps(dict(alert.evidence)),
            confidence=alert.confidence,
            active=alert.active,
            recorded_at=alert.recorded_at if alert.recorded_at is not None else utc_now(),
        )
        async with self._database.session() as session:
            session.add(row)
            await session.flush()
            alert_id = int(row.id)
        return alert_id

    async def list_alerts(
        self, limit: int = 50, active_only: bool = False
    ) -> list[AlertRecord]:
        """Alerts, newest first.  ``active_only`` filters to unresolved ones."""
        if limit <= 0:
            return []
        stmt = select(AlertRow)
        if active_only:
            stmt = stmt.where(AlertRow.active.is_(True))
        stmt = stmt.order_by(AlertRow.id.desc()).limit(limit)
        async with self._database.session() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_alert_from_row(r) for r in rows]

    # -- LLM telemetry -----------------------------------------------------

    async def record_llm_call(
        self,
        *,
        purpose: str,
        model: str,
        ok: bool,
        latency_ms: int,
        fallback_used: bool,
    ) -> None:
        """Record one LLM attempt (CONTRACT.md section 8.1).

        Only operational facts are stored — never the prompt, the response or
        anything derived from the API key.
        """
        row = LlmCallRow(
            purpose=purpose,
            model=model,
            ok=ok,
            latency_ms=int(latency_ms),
            fallback_used=fallback_used,
            recorded_at=utc_now(),
        )
        async with self._database.session() as session:
            session.add(row)

    async def llm_stats(self) -> LLMStats:
        """Aggregate the LLM call log: counts, rates and mean latency."""
        summary = select(
            func.count(LlmCallRow.id),
            func.coalesce(func.sum(case((LlmCallRow.ok.is_(True), 1), else_=0)), 0),
            func.coalesce(func.sum(case((LlmCallRow.ok.is_(False), 1), else_=0)), 0),
            func.coalesce(func.sum(case((LlmCallRow.fallback_used.is_(True), 1), else_=0)), 0),
            func.coalesce(func.avg(LlmCallRow.latency_ms), 0.0),
        )
        by_purpose_stmt = (
            select(LlmCallRow.purpose, func.count(LlmCallRow.id))
            .group_by(LlmCallRow.purpose)
            .order_by(LlmCallRow.purpose)
        )
        async with self._database.session() as session:
            total, ok_calls, failed_calls, fallback_calls, avg_latency = (
                await session.execute(summary)
            ).one()
            by_purpose = {
                str(purpose): int(count)
                for purpose, count in (await session.execute(by_purpose_stmt)).all()
            }
        total = int(total or 0)
        ok_calls = int(ok_calls or 0)
        fallback_calls = int(fallback_calls or 0)
        return LLMStats(
            total_calls=total,
            ok_calls=ok_calls,
            failed_calls=int(failed_calls or 0),
            fallback_calls=fallback_calls,
            success_rate=(ok_calls / total) if total else 0.0,
            fallback_rate=(fallback_calls / total) if total else 0.0,
            avg_latency_ms=float(avg_latency or 0.0),
            by_purpose=by_purpose,
        )
