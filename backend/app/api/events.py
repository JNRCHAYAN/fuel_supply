"""Crisis events, network summary and operator investigation.

CONTRACT.md section 9 rows: `GET /api/v1/events`, `POST /api/v1/events/summary`,
`POST /api/v1/investigate`.

Both POSTs return an A7 `Explanation`, which always carries `source` and
`degraded` so the operator can see whether an LLM answered or the deterministic
fallback did (CONTRACT.md 8.2, brief section 11 "ML model unavailable ->
fallback"). Neither route can 500 because the LLM is missing.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Body, Depends, Query

from app.api.intelligence import build_history, detect_signals
from app.api.network import load_snapshot
from app.api.schemas import (
    EventsResponse,
    ExplanationResponse,
    InvestigateRequest,
    SummaryRequest,
    dependency_unavailable,
    get_detector,
    get_explanation_service,
    get_logger,
    get_metrics,
    get_repository,
    get_simulator_client,
    record_failure,
    simulator_guard,
    to_jsonable,
)

logger = get_logger(__name__)

router = APIRouter(tags=["events"])


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _explain(service: Any, method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Call A7 and normalise its Explanation into a flat, JSON-safe payload."""
    caller = getattr(service, method, None)
    if not callable(caller):
        raise dependency_unavailable(f"ExplanationService.{method}")
    result = await _maybe_await(caller(*args, **kwargs))
    payload = to_jsonable(result)
    if not isinstance(payload, dict):
        payload = {"text": str(payload)}
    return payload


@router.get("/api/v1/events", response_model=EventsResponse, summary="Crisis events")
async def list_events(
    limit: int = Query(default=50, ge=1, le=500),
    status: str | None = Query(default=None, max_length=20),
    type: str | None = Query(default=None, max_length=50),  # noqa: A002 - matches the domain field
    client: Any = Depends(get_simulator_client),
    metrics: Any = Depends(get_metrics),
) -> EventsResponse:
    events = await simulator_guard(
        "get_events", _maybe_await(client.get_events()), metrics=metrics
    )
    rows = to_jsonable(events) or []
    if not isinstance(rows, list):
        rows = [rows]
    rows = [row for row in rows if isinstance(row, dict)]
    if status:
        rows = [r for r in rows if str(r.get("status", "")).lower() == status.lower()]
    if type:
        rows = [r for r in rows if str(r.get("type", "")).lower() == type.lower()]
    return EventsResponse(count=len(rows[:limit]), events=rows[:limit], simulated=True)


@router.post(
    "/api/v1/events/summary",
    response_model=ExplanationResponse,
    summary="Network state summary (LLM or deterministic fallback)",
)
async def events_summary(
    body: SummaryRequest | None = Body(default=None),
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    detector: Any = Depends(get_detector),
    explanation_service: Any = Depends(get_explanation_service),
    metrics: Any = Depends(get_metrics),
) -> ExplanationResponse:
    snapshot = await load_snapshot(client, metrics=metrics)
    history = await build_history(snapshot, client, repository, metrics=metrics)
    signals = await detect_signals(snapshot, history, detector)
    focus = body.focus if body else None
    # `summarize_network(snapshot, signals)` is the contract signature, so the
    # optional operator focus is echoed back rather than folded into A7's prompt
    # from here (prompts live in A7's prompts.py, CONTRACT.md 8.2).
    try:
        payload = await _explain(explanation_service, "summarize_network", snapshot, signals)
    except Exception as exc:  # noqa: BLE001 - typed failure, never a stack trace
        record_failure(
            logger,
            metrics,
            event="summary_failed",
            code="explanation_unavailable",
            detail={"exc": type(exc).__name__},
        )
        raise dependency_unavailable("ExplanationService", exc) from exc
    return ExplanationResponse(
        text=str(payload.get("text", "")),
        source=payload.get("source", "fallback"),
        model=payload.get("model"),
        degraded=bool(payload.get("degraded", True)),
        signals_considered=len(signals),
        focus=focus,
        simulated=True,
    )


@router.post(
    "/api/v1/investigate",
    response_model=ExplanationResponse,
    summary="Operator investigation (LLM or deterministic fallback)",
)
async def investigate(
    body: InvestigateRequest,
    client: Any = Depends(get_simulator_client),
    repository: Any = Depends(get_repository),
    detector: Any = Depends(get_detector),
    explanation_service: Any = Depends(get_explanation_service),
    metrics: Any = Depends(get_metrics),
) -> ExplanationResponse:
    snapshot = await load_snapshot(client, metrics=metrics)
    history = await build_history(snapshot, client, repository, metrics=metrics)
    signals = await detect_signals(snapshot, history, detector)

    # Structured facts only: the engines' output plus the operator's own
    # context. No credentials, no raw environment, no simulator payload dumps.
    context: dict[str, Any] = {
        "tick": getattr(snapshot, "tick", None),
        "sim_time": getattr(snapshot, "sim_time", None),
        "instance_status": getattr(snapshot, "status", None),
        "stale": bool(getattr(snapshot, "stale", False)),
        "depot_count": len(getattr(snapshot, "depots", ()) or ()),
        "station_count": len(getattr(snapshot, "stations", ()) or ()),
        "signals": [to_jsonable(s) for s in signals[:10]],
        "operator_context": body.context or {},
    }
    try:
        payload = await _explain(
            explanation_service, "investigate", body.question, context=context
        )
    except Exception as exc:  # noqa: BLE001
        record_failure(
            logger,
            metrics,
            event="investigation_failed",
            code="explanation_unavailable",
            detail={"exc": type(exc).__name__},
        )
        raise dependency_unavailable("ExplanationService", exc) from exc
    return ExplanationResponse(
        text=str(payload.get("text", "")),
        source=payload.get("source", "fallback"),
        model=payload.get("model"),
        degraded=bool(payload.get("degraded", True)),
        question=body.question,
        signals_considered=len(signals),
        simulated=True,
    )
