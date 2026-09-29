"""Forecasting, risk detection, recommendations, explanations and submission.

CONTRACT.md section 9 rows owned by this module:
`GET /api/v1/forecast`, `GET /api/v1/risk`, `GET /api/v1/recommendations`,
`GET /api/v1/recommendations/{id}/explanation`,
`POST /api/v1/recommendations/{id}/submit`.

The API layer composes: it fetches state through A2, reads history through A3,
runs the three pure engines (A4/A5/A6), and asks A7 to explain what the engines
already decided. No engine is asked to touch the network or the database
(CONTRACT.md section 7).

`POST .../submit` is the only route in the whole API that causes a write against
the simulator, and it requires an explicit `confirm: true` — brief section 24's
"preserve human review for consequential simulated decisions", made structural.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Iterable, Mapping, Sequence

from fastapi import APIRouter, Body, Depends, Query

from app.api.network import load_snapshot
from app.api.schemas import (
    ExplanationResponse,
    ForecastPointOut,
    ForecastResponse,
    RecommendationOut,
    RecommendationsResponse,
    RiskResponse,
    RiskSignalOut,
    StationForecastOut,
    StockoutRiskOut,
    SubmitRequest,
    SubmitResponse,
    api_error,
    build_peer_model,
    dependency_unavailable,
    get_allocator,
    get_detector,
    get_explanation_service,
    get_forecaster,
    get_logger,
    get_metrics,
    get_repository,
    get_simulator_client,
    record_failure,
    simulator_guard,
    to_jsonable,
)

logger = get_logger(__name__)

router = APIRouter(tags=["intelligence"])

# Cost bounds. The engines are cheap, but the number of (station, fuel) pairs
# must be bounded so a large scenario cannot turn one GET into an N+1 storm.
MAX_STATIONS = 60
HISTORY_LIMIT = 1500


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _call(method: Any, *args: Any, **kwargs: Any) -> Any:
    return await _maybe_await(method(*args, **kwargs))


# ---------------------------------------------------------------------------
# Composition helpers (shared with events.py)
# ---------------------------------------------------------------------------


async def build_history(
    snapshot: Any,
    client: Any,
    repository: Any,
    *,
    metrics: Any | None = None,
) -> dict[tuple[str, str], list[Any]]:
    """`(station_id, fuel_type) -> [DemandPoint]`, for the engines.

    The simulator is the source of truth (CONTRACT.md 5.5), so its
    `/v1/demand-history` is read first in a single call. The snapshot store is
    the fallback, which is what keeps detection and forecasting working while
    the simulator circuit is open.
    """
    history: dict[tuple[str, str], list[Any]] = {}
    if client is not None:
        try:
            rows = await asyncio.wait_for(
                _call(client.get_demand_history, limit=HISTORY_LIMIT), timeout=15.0
            )
        except Exception as exc:  # noqa: BLE001 - degrade to the store, log it
            record_failure(
                logger,
                metrics,
                event="demand_history_unavailable",
                code="simulator_error",
                detail={"exc": type(exc).__name__},
            )
            rows = None
        for row in to_jsonable(rows) or []:
            point = build_peer_model("app.sim.models", ("DemandPoint",), row or {})
            if isinstance(point, dict):
                continue
            key = (getattr(point, "station_id", None), getattr(point, "fuel_type", None))
            if key[0] is None or key[1] is None:
                continue
            history.setdefault(key, []).append(point)
    if history:
        return history

    station_ids = [
        getattr(s, "id", None) for s in (getattr(snapshot, "stations", ()) or ())
    ]
    station_ids = [s for s in station_ids if s][:MAX_STATIONS]
    fuels = sorted(
        {
            fuel
            for station in (getattr(snapshot, "stations", ()) or ())
            for fuel in (to_jsonable(getattr(station, "inventory", {})) or {})
        }
    ) or ["DIESEL", "PETROL", "OCTANE"]

    async def _series(station_id: str, fuel_type: str) -> Any:
        try:
            return await _call(repository.demand_series, station_id, fuel_type, limit=500)
        except Exception as exc:  # noqa: BLE001 - a missing series is not fatal
            record_failure(
                logger,
                metrics,
                event="demand_series_unavailable",
                code="repository_error",
                detail={"exc": type(exc).__name__},
            )
            return []

    if station_ids:
        results = await asyncio.gather(
            *[_series(sid, fuel) for sid in station_ids for fuel in fuels]
        )
        for (sid, fuel), points in zip(
            [(sid, fuel) for sid in station_ids for fuel in fuels], results
        ):
            if points:
                history[(sid, fuel)] = list(points)
    return history


def _station_index(snapshot: Any) -> dict[str, Any]:
    return {
        getattr(station, "id", None): station
        for station in (getattr(snapshot, "stations", ()) or ())
        if getattr(station, "id", None)
    }


def _min_transit_ticks(snapshot: Any, station_id: str) -> int | None:
    ticks = [
        getattr(route, "transit_ticks", None)
        for route in (getattr(snapshot, "routes", ()) or ())
        if getattr(route, "destination_station_id", None) == station_id
        and str(getattr(route, "status", "")).upper() == "AVAILABLE"
    ]
    ticks = [t for t in ticks if isinstance(t, (int, float))]
    return int(min(ticks)) if ticks else None


async def detect_signals(
    snapshot: Any,
    history: Mapping[tuple[str, str], Sequence[Any]],
    detector: Any,
) -> list[Any]:
    """A5's signals for the current snapshot. Empty list on no evidence."""
    if detector is None:
        raise dependency_unavailable("AnomalyDetector")
    result = await _call(detector.detect, snapshot=snapshot, history=history)
    return list(result or [])


class RiskOfStockout:
    """Small holder so /forecast and /risk share one forecast pass."""

    __slots__ = ("forecasts", "risks")

    def __init__(self, forecasts: dict[tuple[str, str], Any], risks: dict[tuple[str, str], Any]) -> None:
        self.forecasts = forecasts
        self.risks = risks


async def compute_forecasts(
    snapshot: Any,
    history: Mapping[tuple[str, str], Sequence[Any]],
    forecaster: Any,
    *,
    horizon_ticks: int = 8,
) -> RiskOfStockout:
    """Forecast and stockout risk for every (station, fuel) pair we know of.

    Stations with no history are still forecast (CONTRACT.md 7.1 requires the
    forecaster to work with few or zero points), which is what keeps the console
    useful immediately after a reset.
    """
    if forecaster is None:
        raise dependency_unavailable("DemandForecaster")
    stations = _station_index(snapshot)
    keys: list[tuple[str, str]] = [k for k in history.keys() if k[0] in stations] if stations else list(history.keys())
    if not keys:
        keys = []
        for station_id in list(stations)[:MAX_STATIONS]:
            inventory = to_jsonable(getattr(stations[station_id], "inventory", {})) or {}
            for fuel in sorted(inventory) or ["DIESEL", "PETROL", "OCTANE"]:
                keys.append((station_id, fuel))

    forecasts: dict[tuple[str, str], Any] = {}
    risks: dict[tuple[str, str], Any] = {}
    for key in keys[: MAX_STATIONS * 3]:
        station_id, fuel_type = key
        points = list(history.get(key) or [])
        try:
            forecast = await _call(forecaster.forecast, points, horizon_ticks=horizon_ticks)
        except Exception as exc:  # noqa: BLE001 - one bad series must not fail the page
            record_failure(
                logger,
                getattr(forecaster, "metrics", None),
                event="forecast_failed",
                code="forecast_error",
                detail={"exc": type(exc).__name__},
            )
            continue
        forecasts[key] = forecast
        station = stations.get(station_id)
        inventory = to_jsonable(getattr(station, "inventory", {})) or {}
        liters = inventory.get(fuel_type)
        if isinstance(liters, (int, float)):
            try:
                risks[key] = await _call(
                    forecaster.stockout_risk,
                    inventory_liters=float(liters),
                    forecast=forecast,
                    transit_ticks=_min_transit_ticks(snapshot, station_id),
                )
            except Exception as exc:  # noqa: BLE001
                record_failure(
                    logger,
                    None,
                    event="stockout_risk_failed",
                    code="forecast_error",
                    detail={"exc": type(exc).__name__},
                )
    return RiskOfStockout(forecasts, risks)


def _forecast_points(payload: Mapping[str, Any], fallback_liters: float = 0.0) -> list[ForecastPointOut]:
    points: list[ForecastPointOut] = []
    for point in payload.get("points") or []:
        if not isinstance(point, dict):
            continue
        tick = point.get("tick")
        liters = point.get("liters")
        if not isinstance(tick, (int, float)) or not isinstance(liters, (int, float)):
            # A malformed point is dropped, never allowed to blank the page.
            continue
        lower = point.get("lower")
        upper = point.get("upper")
        points.append(
            ForecastPointOut(
                tick=int(tick),
                liters=float(liters),
                lower=float(lower) if isinstance(lower, (int, float)) else float(liters),
                upper=float(upper) if isinstance(upper, (int, float)) else float(liters),
            )
        )
    return points


def _to_station_forecast(
    key: tuple[str, str], result: Any, risk: Any, inventory_liters: float | None
) -> StationForecastOut:
    payload = to_jsonable(result) or {}
    if not isinstance(payload, dict):
        payload = {}
    points = _forecast_points(payload)
    risk_out = None
    if risk is not None:
        risk_payload = to_jsonable(risk) or {}
        risk_out = StockoutRiskOut(
            probability=risk_payload.get("probability", 0.0),
            ticks_to_stockout=risk_payload.get("ticks_to_stockout"),
            confidence=risk_payload.get("confidence", 0.0),
            basis=str(risk_payload.get("basis", "")),
        )
    return StationForecastOut(
        station_id=key[0],
        fuel_type=key[1],
        method=str(payload.get("method", "unknown")),
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        fallback_used=bool(payload.get("fallback_used", False)),
        points=points,
        stockout_risk=risk_out,
        inventory_liters=inventory_liters,
    )


def _to_recommendation(rec: Any) -> RecommendationOut:
    payload = to_jsonable(rec) or {}
    if not isinstance(payload, dict):
        raise dependency_unavailable("Recommendation")
    return RecommendationOut(
        id=str(payload.get("id", "")),
        station_id=str(payload.get("station_id", "")),
        depot_id=str(payload.get("depot_id", "")),
        route_id=str(payload.get("route_id", "")),
        fuel_type=str(payload.get("fuel_type", "")),
        quantity_liters=float(payload.get("quantity_liters", 0.0) or 0.0),
        rationale=str(payload.get("rationale", "")),
        constraints=[str(c) for c in payload.get("constraints") or []],
        expected_impact={
            str(k): float(v)
            for k, v in (payload.get("expected_impact") or {}).items()
            if isinstance(v, (int, float))
        },
        alternatives=[a for a in payload.get("alternatives") or [] if isinstance(a, dict)],
        confidence=float(payload.get("confidence", 0.0) or 0.0),
        decision_id=payload.get("decision_id"),
        simulated=True,
    )


def _engine_policy(engine: Any) -> str | None:
    for name in ("policy", "last_policy", "active_policy", "fallback_policy"):
        value = getattr(engine, name, None)
        if isinstance(value, str):
            return value
        if callable(value):
            try:
                resolved = value()
            except Exception:  # pragma: no cover - defensive
                continue
            if isinstance(resolved, str):
                return resolved
    return None


def _snapshot_tick(snapshot: Any) -> int | None:
    tick = getattr(snapshot, "tick", None)
    return int(tick) if isinstance(tick, (int, float)) else None


async def _generate(
    client: Any,
    repository: Any,
    forecaster: Any,
    detector: Any,
    allocator: Any,
    metrics: Any,
    *,
    budget_liters: float | None = None,
    horizon_ticks: int = 8,
) -> tuple[Any, list[Any], RiskOfStockout, list[Any]]:
    """One composition pass: snapshot -> signals -> forecasts -> recommendations."""
    snapshot = await load_snapshot(client, metrics=metrics)
    history = await build_history(snapshot, client, repository, metrics=metrics)
    signals = await detect_signals(snapshot, history, detector)
    computed = await compute_forecasts(snapshot, history, forecaster, horizon_ticks=horizon_ticks)
    if allocator is None:
        raise dependency_unavailable("AllocationEngine")
    recommendations = await _call(
        allocator.recommend,
        snapshot=snapshot,
        signals=signals,
        forecasts=computed.forecasts,
        budget_liters=budget_liters,
    )
    return snapshot, signals, computed, list(recommendations or [])


def _build_decision_record(rec: Any, *, tick: int, sim_time: str | None, policy: str | None) -> Any:
    """Build A3's `RecommendationRecord` (CONTRACT.md 6 does not pin its shape).

    A3 ships `RecommendationRecord.from_recommendation(rec, *, tick, sim_time,
    policy)` as the intended seam, so that is used when present. Otherwise the
    record is built by matching the peer class's own signature; if neither is
    possible the structured payload is passed through as a dict.
    """
    from app.api.schemas import _import_attr  # local: keeps this module's deps lazy

    cls = _import_attr("app.store.models", ("RecommendationRecord",))
    builder = getattr(cls, "from_recommendation", None) if cls is not None else None
    if callable(builder):
        try:
            signature = inspect.signature(builder)
        except (TypeError, ValueError):
            signature = None
        kwargs = {"tick": tick, "sim_time": sim_time, "policy": policy}
        if signature is not None:
            accepts_kwargs = any(
                p.kind is inspect.Parameter.VAR_KEYWORD
                for p in signature.parameters.values()
            )
            kwargs = {
                k: v
                for k, v in kwargs.items()
                if accepts_kwargs or k in signature.parameters
            }
        try:
            return builder(rec, **kwargs)
        except Exception:  # noqa: BLE001 - fall through to the generic builder
            pass

    payload = to_jsonable(rec) or {}
    payload = dict(payload) if isinstance(payload, dict) else {}
    payload.setdefault("tick", tick)
    if sim_time is not None:
        payload.setdefault("sim_time", sim_time)
        payload.setdefault("created_tick", tick)
    if policy is not None:
        payload.setdefault("policy", policy)
    return build_peer_model("app.store.models", ("RecommendationRecord",), payload)


async def _record_decision(
    repository: Any,
    metrics: Any,
    *,
    rec: Any,
    policy: str | None,
    snapshot: Any,
) -> int | None:
    """Audit write for the decision history (CONTRACT.md 6).

    A failure here is logged and reported as a null decision_id — it must never
    fail an operator's request, least of all one whose allocation already
    reached the simulator.
    """
    recorder = getattr(repository, "record_decision", None)
    try:
        if not callable(recorder):
            raise dependency_unavailable("Repository.record_decision")
        tick = _snapshot_tick(snapshot)
        sim_time = getattr(snapshot, "sim_time", None)
        record = _build_decision_record(
            rec,
            tick=tick if tick is not None else 0,
            sim_time=sim_time if isinstance(sim_time, str) else None,
            policy=policy,
        )
        result = recorder(record)
        result = await result if inspect.isawaitable(result) else result
        if result is None:
            # A3's `record_decision` returns the new row id; a None means the
            # write did not happen and the caller must not claim it did.
            record_failure(
                logger,
                metrics,
                event="decision_record_missing_id",
                code="repository_error",
                detail={"recommendation_id": str(getattr(rec, "id", ""))},
            )
            return None
        return int(result)
    except Exception as exc:  # noqa: BLE001 - audit failure must not break the request
        record_failure(
            logger,
            metrics,
            event="decision_record_failed",
            code="repository_error",
            detail={"exc": type(exc).__name__, "recommendation_id": str(getattr(rec, "id", ""))},
        )
        return None


async def _find_recommendation(
    rec_id: str,
    client: Any,
    repository: Any,
    forecaster: Any,
    detector: Any,
    allocator: Any,
    metrics: Any,
) -> tuple[Any, list[Any], RiskOfStockout, Any]:
    """Regenerate the current recommendation set and pick one by id.

    CONTRACT.md 7.3 makes `Recommendation.id` stable within a tick and the whole
    pipeline deterministic, which is what lets a stateless GET resolve an id
    without storing anything. An unknown id is a typed 404.
    """
    snapshot, signals, computed, recommendations = await _generate(
        client, repository, forecaster, detector, allocator, metrics
    )
    match = next((r for r in recommendations if str(getattr(r, "id", "")) == rec_id), None)
    if match is None:
        raise api_error(
            404,
            "recommendation_not_found",
            f"no current recommendation with id '{rec_id}'",
            details={"recommendation_id": rec_id, "considered": len(recommendations)},
        )
    return snapshot, signals, computed, match


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/v1/forecast", response_model=ForecastResponse, summary="Forecast + stockout risk")
async def forecast(
    station_id: str | None = Query(default=None, max_length=100),
    fuel_type: str | None = Query(default=None, max_length=20),
    horizon_ticks: int = Query(default=8, ge=1, le=72),
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    forecaster: Any = Depends(get_forecaster),
    metrics: Any = Depends(get_metrics),
) -> ForecastResponse:
    snapshot = await load_snapshot(client, metrics=metrics)
    history = await build_history(snapshot, client, repository, metrics=metrics)
    computed = await compute_forecasts(snapshot, history, forecaster, horizon_ticks=horizon_ticks)
    stations = _station_index(snapshot)
    requested_fuel = fuel_type.upper() if fuel_type else None

    rows: list[StationForecastOut] = []
    for key, result in sorted(computed.forecasts.items()):
        if station_id and key[0] != station_id:
            continue
        if requested_fuel and key[1] != requested_fuel:
            continue
        inventory = to_jsonable(getattr(stations.get(key[0]), "inventory", {})) or {}
        liters = inventory.get(key[1])
        rows.append(
            _to_station_forecast(
                key,
                result,
                computed.risks.get(key),
                float(liters) if isinstance(liters, (int, float)) else None,
            )
        )
    if station_id and not rows:
        raise api_error(
            404,
            "station_not_found",
            f"no station '{station_id}' with forecastable inventory",
            details={"station_id": station_id},
        )
    return ForecastResponse(
        horizon_ticks=horizon_ticks,
        generated_at_tick=_snapshot_tick(snapshot),
        count=len(rows),
        forecasts=rows,
        simulated=True,
    )


@router.get("/api/v1/risk", response_model=RiskResponse, summary="Current risk signals")
async def risk(
    severity: str | None = Query(default=None, max_length=20),
    kind: str | None = Query(default=None, max_length=50),
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    detector: Any = Depends(get_detector),
    metrics: Any = Depends(get_metrics),
) -> RiskResponse:
    snapshot = await load_snapshot(client, metrics=metrics)
    history = await build_history(snapshot, client, repository, metrics=metrics)
    signals = await detect_signals(snapshot, history, detector)
    payload = [to_jsonable(s) for s in signals]
    payload = [s for s in payload if isinstance(s, dict)]
    if severity:
        payload = [s for s in payload if str(s.get("severity", "")).lower() == severity.lower()]
    if kind:
        payload = [s for s in payload if str(s.get("kind", "")).lower() == kind.lower()]
    return RiskResponse(
        count=len(payload),
        generated_at_tick=_snapshot_tick(snapshot),
        signals=payload,
        simulated=True,
    )


@router.get(
    "/api/v1/recommendations",
    response_model=RecommendationsResponse,
    summary="Ranked, inspectable recommendations",
)
async def recommendations(
    budget_liters: float | None = Query(default=None, gt=0),
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    forecaster: Any = Depends(get_forecaster),
    detector: Any = Depends(get_detector),
    allocator: Any = Depends(get_allocator),
    metrics: Any = Depends(get_metrics),
) -> RecommendationsResponse:
    snapshot, _signals, _computed, recs = await _generate(
        client,
        repository,
        forecaster,
        detector,
        allocator,
        metrics,
        budget_liters=budget_liters,
    )
    return RecommendationsResponse(
        count=len(recs),
        policy=_engine_policy(allocator),
        generated_at_tick=_snapshot_tick(snapshot),
        budget_liters=budget_liters,
        recommendations=[_to_recommendation(r) for r in recs],
        simulated=True,
    )


@router.get(
    "/api/v1/recommendations/{rec_id}/explanation",
    response_model=ExplanationResponse,
    summary="Explanation of a recommendation (LLM or deterministic fallback)",
)
async def explain_recommendation(
    rec_id: str,
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    forecaster: Any = Depends(get_forecaster),
    detector: Any = Depends(get_detector),
    allocator: Any = Depends(get_allocator),
    explanation_service: Any = Depends(get_explanation_service),
    metrics: Any = Depends(get_metrics),
) -> ExplanationResponse:
    snapshot, signals, _computed, rec = await _find_recommendation(
        rec_id, client, repository, forecaster, detector, allocator, metrics
    )
    await _record_decision(
        repository,
        metrics,
        rec=rec,
        policy=_engine_policy(allocator),
        snapshot=snapshot,
    )
    try:
        explanation = await _call(explanation_service.explain_recommendation, rec, signals)
    except Exception as exc:  # noqa: BLE001 - A7 owns the fallback; surface it typed
        record_failure(
            logger,
            metrics,
            event="explanation_failed",
            code="explanation_unavailable",
            detail={"exc": type(exc).__name__},
        )
        raise dependency_unavailable("ExplanationService", exc) from exc
    payload = to_jsonable(explanation) or {}
    if not isinstance(payload, dict):
        payload = {"text": str(payload)}
    return ExplanationResponse(
        text=str(payload.get("text", "")),
        source=payload.get("source", "fallback"),
        model=payload.get("model"),
        degraded=bool(payload.get("degraded", True)),
        recommendation_id=rec_id,
        signals_considered=len(signals),
        simulated=True,
    )


@router.post(
    "/api/v1/recommendations/{rec_id}/submit",
    response_model=SubmitResponse,
    summary="Operator-initiated allocation submission",
)
async def submit_recommendation(
    rec_id: str,
    body: SubmitRequest | None = Body(default=None),
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    forecaster: Any = Depends(get_forecaster),
    detector: Any = Depends(get_detector),
    allocator: Any = Depends(get_allocator),
    metrics: Any = Depends(get_metrics),
) -> SubmitResponse:
    """The one write path. Requires an explicit `confirm: true`.

    Brief section 24: the backend recommends, it never auto-executes. A request
    without the operator's explicit confirmation is rejected with 400 *before*
    anything is read or written, so the guard cannot be bypassed by a payload
    that would otherwise have succeeded.
    """
    if body is None or body.confirm is not True:
        raise api_error(
            400,
            "confirmation_required",
            "submission requires an explicit {'confirm': true} field; "
            "the backend recommends and never auto-executes",
        )

    snapshot, _signals, _computed, rec = await _find_recommendation(
        rec_id, client, repository, forecaster, detector, allocator, metrics
    )
    payload = to_jsonable(rec) or {}
    payload = payload if isinstance(payload, dict) else {}

    tick = _snapshot_tick(snapshot)
    quantity = body.quantity_liters or payload.get("quantity_liters") or 0.0
    # A2's `AllocationRequest.idempotency_key` is required by the simulator, so
    # one is always sent. When the operator does not supply one it is derived
    # from the recommendation id and the tick: a network-level retry inside the
    # same tick therefore de-duplicates, while a later tick is a genuinely new
    # decision and legitimately produces a new allocation.
    idempotency_key = body.idempotency_key or f"{rec_id}-t{tick if tick is not None else 0}"
    request_payload = {
        "idempotency_key": idempotency_key,
        "source_depot_id": payload.get("depot_id"),
        "destination_station_id": payload.get("station_id"),
        "route_id": payload.get("route_id"),
        "fuel_type": payload.get("fuel_type"),
        "quantity": float(quantity),
    }
    missing = sorted(key for key, value in request_payload.items() if value in (None, ""))
    if missing:
        raise api_error(
            409,
            "recommendation_incomplete",
            "the recommendation does not carry everything the simulator needs to "
            "create an allocation",
            details={"recommendation_id": rec_id, "missing": missing},
        )
    allocation_request = build_peer_model(
        "app.sim.models", ("AllocationRequest",), request_payload
    )
    if isinstance(allocation_request, dict):
        raise api_error(
            503,
            "allocation_request_unavailable",
            "A2's AllocationRequest could not be constructed from the recommendation",
            details={"recommendation_id": rec_id},
        )

    allocation = await simulator_guard(
        "create_allocation",
        _call(client.create_allocation, allocation_request),
        metrics=metrics,
    )
    allocation_payload = to_jsonable(allocation) or {}
    if not isinstance(allocation_payload, dict):
        allocation_payload = {"value": allocation_payload}

    decision_id = await _record_decision(
        repository,
        metrics,
        rec=rec,
        policy=_engine_policy(allocator),
        snapshot=snapshot,
    )
    if decision_id is not None:
        await _record_outcome(
            repository,
            metrics,
            decision_id=decision_id,
            allocation_id=allocation_payload.get("id"),
            note=body.note,
        )
    return SubmitResponse(
        submitted=True,
        recommendation_id=rec_id,
        allocation=allocation_payload,
        allocation_id=allocation_payload.get("id"),
        decision_id=decision_id,
        note=body.note,
        simulated=True,
    )


async def _record_outcome(
    repository: Any,
    metrics: Any,
    *,
    decision_id: int,
    allocation_id: Any,
    note: str | None,
) -> None:
    """Record the human-review outcome against the decision row.

    A3's `record_decision_outcome` raises `LookupError` when the decision id has
    no row. That is a "this decision does not exist" condition, so it becomes the
    same typed 404 the rest of the API returns for an unknown id — never a 500.
    The allocation id travels in the error body so a response that arrives after
    the allocation was created still lets the operator reconcile.
    """
    recorder = getattr(repository, "record_decision_outcome", None)
    if not callable(recorder):
        return
    try:
        await _call(
            recorder,
            decision_id,
            submitted=True,
            allocation_id=allocation_id,
            note=note,
        )
    except LookupError as exc:
        record_failure(
            logger,
            metrics,
            event="decision_outcome_unknown_decision",
            code="decision_not_found",
            detail={"exc": type(exc).__name__, "decision_id": decision_id},
        )
        raise api_error(
            404,
            "decision_not_found",
            f"no decision with id {decision_id}; the outcome could not be recorded",
            details={"decision_id": decision_id, "allocation_id": allocation_id},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - the allocation already happened
        record_failure(
            logger,
            metrics,
            event="decision_outcome_record_failed",
            code="repository_error",
            detail={"exc": type(exc).__name__, "decision_id": decision_id},
        )
