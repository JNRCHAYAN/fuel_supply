"""SQLAlchemy tables, storage DTOs and the snapshot codec.

Owned by A3.  Tables are exactly the five named in CONTRACT.md section 6:
``snapshots``, ``demand_points``, ``decisions``, ``alerts``, ``llm_calls``.

Contract gap (reported): section 6 names the types ``RecommendationRecord``,
``AlertRecord`` and ``LLMStats`` in the ``Repository`` signatures but never
defines them.  They are storage-side records, so they are defined here and
re-exported from ``app.store.repository``.  ``DECISION_AUDIT`` below records what
``decisions`` keeps so a decision can be reconstructed after the fact
(brief section 20, the recommended audit-history deliverable).
"""

from __future__ import annotations

import collections.abc as cabc
import json
import logging
import types
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from sqlalchemy import Boolean, Float, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.store.db import Base, utc_now

logger = logging.getLogger(__name__)

__all__ = [
    "AlertRecord",
    "AlertRow",
    "DECISION_AUDIT",
    "DecisionRow",
    "DemandPointRow",
    "LLMStats",
    "LlmCallRow",
    "RecommendationRecord",
    "SnapshotRow",
    "deserialize_snapshot",
    "demand_point_type",
    "serialize_snapshot",
    "snapshot_type",
]

#: What a row in ``decisions`` preserves, so the audit trail answers "why?"
#: rather than only "what".  Brief section 20 asks for the decision history to be
#: inspectable.
DECISION_AUDIT: tuple[str, ...] = (
    "recommendation_id",
    "tick",
    "sim_time",
    "station_id",
    "depot_id",
    "route_id",
    "fuel_type",
    "quantity_liters",
    "rationale",
    "constraints",
    "expected_impact",
    "alternatives",
    "confidence",
    "policy",
    "simulated",
    "recorded_at",
    "submitted",
    "allocation_id",
    "outcome_note",
    "outcome_recorded_at",
)


# ---------------------------------------------------------------------------
# ORM tables
# ---------------------------------------------------------------------------


class SnapshotRow(Base):
    """One assembled simulator snapshot (CONTRACT.md section 5.2).

    The scalar columns exist for cheap querying; ``payload`` holds the whole
    snapshot as JSON so ``latest_snapshot`` can return a faithful ``Snapshot``.
    """

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    taken_at: Mapped[float] = mapped_column(Float, nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    sim_time: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    age_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    depot_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    station_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False, default=utc_now)

    __table_args__ = (Index("ix_snapshots_tick", "tick"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<SnapshotRow id={self.id} tick={self.tick} sim_time={self.sim_time!r}>"


class DemandPointRow(Base):
    """One demand observation for a (station, fuel_type) at a tick."""

    __tablename__ = "demand_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    station_id: Mapped[str] = mapped_column(String(128), nullable=False)
    fuel_type: Mapped[str] = mapped_column(String(32), nullable=False)
    tick: Mapped[int] = mapped_column(Integer, nullable=False)
    liters: Mapped[float] = mapped_column(Float, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False, default=utc_now)

    __table_args__ = (
        Index("ix_demand_points_series", "station_id", "fuel_type", "tick"),
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DemandPointRow {self.station_id}/{self.fuel_type} tick={self.tick}>"


class DecisionRow(Base):
    """Decision audit history: the recommendation, why, and what was done with it.

    ``submitted`` is nullable on purpose — ``None`` means "no outcome recorded
    yet", ``False`` means "the operator explicitly did not submit it".
    """

    __tablename__ = "decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recommendation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    tick: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sim_time: Mapped[str | None] = mapped_column(String(64), nullable=True)
    station_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    depot_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    route_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    fuel_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    quantity_liters: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    constraints_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    expected_impact_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    alternatives_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    policy: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    simulated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False, default=utc_now)

    # --- outcome of the human review (brief section 24) ---------------------
    submitted: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=None)
    allocation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, default=None)
    outcome_note: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    outcome_recorded_at: Mapped[float | None] = mapped_column(Float, nullable=True, default=None)

    __table_args__ = (Index("ix_decisions_recorded_at", "recorded_at"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<DecisionRow id={self.id} rec={self.recommendation_id!r} submitted={self.submitted}>"


class AlertRow(Base):
    """A shortage/risk alert (brief section 6).  Mirrors A5's ``RiskSignal``."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False, default="info")
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    entity_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    detected_at_tick: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False, default=utc_now)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AlertRow id={self.id} {self.kind}/{self.severity} active={self.active}>"


class LlmCallRow(Base):
    """One DeepSeek call attempt, successful or not (CONTRACT.md section 8.1).

    Never stores the prompt, the response body or anything derived from the API
    key — only the operational facts A7 reports.
    """

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fallback_used: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False, default=utc_now)

    __table_args__ = (Index("ix_llm_calls_purpose", "purpose"),)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<LlmCallRow id={self.id} purpose={self.purpose!r} ok={self.ok}>"


# ---------------------------------------------------------------------------
# Storage DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RecommendationRecord:
    """A stored decision: the recommendation plus its audit trail.

    ``RecommendationRecord.from_recommendation`` accepts A6's ``Recommendation``
    (CONTRACT.md section 7.3) by attribute, so the storage layer does not have to
    import ``app.intelligence.allocate`` to accept one.
    """

    recommendation_id: str
    station_id: str
    depot_id: str
    route_id: str
    fuel_type: str
    quantity_liters: float
    rationale: str
    tick: int = 0
    sim_time: str | None = None
    policy: str = "optimizer"
    constraints: tuple[str, ...] = ()
    expected_impact: dict[str, float] = field(default_factory=dict)
    alternatives: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0
    simulated: bool = True
    #: database id, filled in by ``list_decisions`` / ``get_decision``
    decision_id: int | None = None
    #: outcome of the human review; ``None`` = not recorded yet
    submitted: bool | None = None
    allocation_id: int | None = None
    outcome_note: str | None = None
    recorded_at: float | None = None
    outcome_recorded_at: float | None = None

    @classmethod
    def from_recommendation(
        cls,
        rec: Any,
        *,
        tick: int = 0,
        sim_time: str | None = None,
        policy: str | None = None,
    ) -> "RecommendationRecord":
        """Build a record from A6's ``Recommendation`` (duck-typed by contract shape)."""
        alternatives = tuple(
            _jsonable(alt) for alt in (getattr(rec, "alternatives", ()) or ())
        )
        constraints = tuple(str(c) for c in (getattr(rec, "constraints", ()) or ()))
        impact = {
            str(k): float(v) for k, v in dict(getattr(rec, "expected_impact", {}) or {}).items()
        }
        return cls(
            recommendation_id=str(getattr(rec, "id", getattr(rec, "recommendation_id", ""))),
            station_id=str(getattr(rec, "station_id", "")),
            depot_id=str(getattr(rec, "depot_id", "")),
            route_id=str(getattr(rec, "route_id", "")),
            fuel_type=str(getattr(rec, "fuel_type", "")),
            quantity_liters=float(getattr(rec, "quantity_liters", 0.0)),
            rationale=str(getattr(rec, "rationale", "")),
            tick=int(tick),
            sim_time=sim_time,
            policy=str(policy if policy is not None else getattr(rec, "policy", "optimizer")),
            constraints=constraints,
            expected_impact=impact,
            alternatives=alternatives,
            confidence=float(getattr(rec, "confidence", 0.0)),
            simulated=bool(getattr(rec, "simulated", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for the API layer."""
        return {
            "id": self.decision_id,
            "recommendation_id": self.recommendation_id,
            "tick": self.tick,
            "sim_time": self.sim_time,
            "station_id": self.station_id,
            "depot_id": self.depot_id,
            "route_id": self.route_id,
            "fuel_type": self.fuel_type,
            "quantity_liters": self.quantity_liters,
            "rationale": self.rationale,
            "constraints": list(self.constraints),
            "expected_impact": dict(self.expected_impact),
            "alternatives": list(self.alternatives),
            "confidence": self.confidence,
            "policy": self.policy,
            "simulated": self.simulated,
            "recorded_at": self.recorded_at,
            "outcome": {
                "submitted": self.submitted,
                "allocation_id": self.allocation_id,
                "note": self.outcome_note,
                "recorded_at": self.outcome_recorded_at,
            },
        }


@dataclass(frozen=True)
class AlertRecord:
    """A stored alert.  ``AlertRecord.from_signal`` accepts A5's ``RiskSignal``."""

    kind: str
    severity: str
    entity_type: str
    entity_id: str
    detected_at_tick: int
    summary: str
    evidence: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    active: bool = True
    alert_id: int | None = None
    recorded_at: float | None = None

    @classmethod
    def from_signal(cls, signal: Any) -> "AlertRecord":
        """Build a record from A5's ``RiskSignal`` (duck-typed by contract shape).

        ``Severity`` is a ``str`` enum; ``.value`` is preferred so the stored text
        is ``"critical"`` and not ``"Severity.CRITICAL"`` on any Python version.
        """
        severity = getattr(signal, "severity", "info")
        return cls(
            kind=str(getattr(signal, "kind", "")),
            severity=str(getattr(severity, "value", severity)),
            entity_type=str(getattr(signal, "entity_type", "")),
            entity_id=str(getattr(signal, "entity_id", "")),
            detected_at_tick=int(getattr(signal, "detected_at_tick", 0)),
            summary=str(getattr(signal, "summary", "")),
            evidence=dict(getattr(signal, "evidence", {}) or {}),
            confidence=float(getattr(signal, "confidence", 0.0)),
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for the API layer."""
        return {
            "id": self.alert_id,
            "kind": self.kind,
            "severity": self.severity,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "detected_at_tick": self.detected_at_tick,
            "summary": self.summary,
            "evidence": dict(self.evidence),
            "confidence": self.confidence,
            "active": self.active,
            "recorded_at": self.recorded_at,
        }


@dataclass(frozen=True)
class LLMStats:
    """Aggregate of ``llm_calls``.  Defined here — see the module docstring."""

    total_calls: int = 0
    ok_calls: int = 0
    failed_calls: int = 0
    fallback_calls: int = 0
    success_rate: float = 0.0
    fallback_rate: float = 0.0
    avg_latency_ms: float = 0.0
    by_purpose: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe view for the API layer."""
        return {
            "total_calls": self.total_calls,
            "ok_calls": self.ok_calls,
            "failed_calls": self.failed_calls,
            "fallback_calls": self.fallback_calls,
            "success_rate": self.success_rate,
            "fallback_rate": self.fallback_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "by_purpose": dict(self.by_purpose),
        }


# ---------------------------------------------------------------------------
# JSON codec
# ---------------------------------------------------------------------------

_PRIMITIVES = (str, int, float, bool)


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, datetime):
        return value.isoformat()
    logger.warning(
        "value of type %s has no JSON form; storing its text form", type(value).__name__
    )
    return str(value)


def _jsonable(value: Any) -> Any:
    """Convert a peer's value into something ``json.dumps`` accepts.

    Snapshots and recommendations come from other workstreams; a surprise type
    must degrade to text with a log line rather than crash the write
    (CONTRACT.md section 0.4).
    """
    if value is None or isinstance(value, _PRIMITIVES):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, cabc.Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(v) for v in value]
    # A peer's record object that is not a dataclass (a namespace, a plain
    # object) still has to survive the write: stringifying it would silently
    # destroy the audit trail.
    attrs = getattr(value, "__dict__", None)
    if (
        isinstance(attrs, dict)
        and attrs
        and not isinstance(value, (type, types.ModuleType, types.FunctionType))
    ):
        return {str(k): _jsonable(v) for k, v in attrs.items() if not str(k).startswith("_")}
    return _json_default(value)


def _dumps(value: Any) -> str:
    return json.dumps(_jsonable(value), separators=(",", ":"), default=_json_default)


def _loads(payload: str) -> Any:
    return json.loads(payload)


def _resolved_hints(cls: type) -> dict[str, Any]:
    try:
        return get_type_hints(cls)
    except Exception:  # noqa: BLE001 - a peer's exotic annotation must not break reads
        logger.warning(
            "could not resolve type hints for %s; falling back to field types",
            cls.__name__,
            exc_info=True,
        )
        return {f.name: f.type for f in fields(cls)}


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _rehydrate(tp: Any, value: Any) -> Any:
    """Rebuild ``value`` as the dataclass type described by the annotation ``tp``.

    Generic by design: A2 owns the nested simulator types (``Depot``, ``Station``,
    ``Metrics``, …) and this code never needs to name them.
    """
    if value is None or tp is Any or tp is None or isinstance(tp, str):
        return value
    origin = get_origin(tp)
    if origin is None:
        if isinstance(tp, type) and is_dataclass(tp) and isinstance(value, dict):
            return _build(tp, value)
        return value
    if origin is Union or origin is types.UnionType:
        for arg in get_args(tp):
            if arg is type(None):
                continue
            return _rehydrate(arg, value)
        return value
    if origin is tuple:
        args = get_args(tp)
        items = _as_sequence(value)
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_rehydrate(args[0], v) for v in items)
        if not args:
            return tuple(items)
        return tuple(_rehydrate(a, v) for a, v in zip(args, items))
    if origin in (list, cabc.Sequence, cabc.Iterable):
        args = get_args(tp)
        inner = args[0] if args else Any
        return [_rehydrate(inner, v) for v in _as_sequence(value)]
    if origin in (set, frozenset, cabc.Set):
        args = get_args(tp)
        inner = args[0] if args else Any
        return origin(_rehydrate(inner, v) for v in _as_sequence(value))
    if origin in (dict, cabc.Mapping):
        args = get_args(tp)
        inner = args[1] if len(args) == 2 else Any
        if not isinstance(value, dict):
            return value
        return {k: _rehydrate(inner, v) for k, v in value.items()}
    return value


def _build(cls: type, data: dict[str, Any]) -> Any:
    hints = _resolved_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _rehydrate(hints.get(f.name, Any), data[f.name])
    return cls(**kwargs)


def snapshot_type() -> type:
    """Return A2's ``Snapshot`` (CONTRACT.md section 5.2).

    Imported at call time so the storage layer is importable while
    ``app/sim/models.py`` is still being written.  A missing module raises
    ``ModuleNotFoundError: No module named 'app.sim.models'`` naming the owner —
    it is never faked here.
    """
    from app.sim.models import Snapshot  # noqa: PLC0415

    return Snapshot


def demand_point_type() -> type:
    """Return A2's ``DemandPoint`` (CONTRACT.md section 5.2)."""
    from app.sim.models import DemandPoint  # noqa: PLC0415

    return DemandPoint


def serialize_snapshot(snap: Any) -> str:
    return _dumps(snap)


def deserialize_snapshot(payload: str) -> Any:
    """Rebuild a ``Snapshot`` from the JSON written by :func:`serialize_snapshot`."""
    snapshot_cls = snapshot_type()
    data = _loads(payload)
    if not isinstance(data, dict):
        raise ValueError(
            f"stored snapshot payload is {type(data).__name__}, expected an object"
        )
    return _build(snapshot_cls, data)
