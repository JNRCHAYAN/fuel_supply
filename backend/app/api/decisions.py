"""Decision audit history.

CONTRACT.md section 9 rows: `GET /api/v1/decisions`, `GET /api/v1/decisions/{id}`.
CONTRACT.md section 6: the `decisions` table is the decision audit history
(brief section 20 recommended deliverable), so this is the operator's record of
what the system recommended and what was done about it.

A3's `RecommendationRecord` shape is not pinned by the contract, so the payloads
are projected through permissive models: an extra or renamed field in A3's model
degrades the display, it never becomes a 500.
"""

from __future__ import annotations

import inspect
from typing import Any

from fastapi import APIRouter, Depends, Query

from app.api.schemas import (
    DecisionDetailResponse,
    DecisionsResponse,
    api_error,
    dependency_unavailable,
    get_logger,
    get_metrics,
    get_repository,
    record_failure,
    to_jsonable,
)

logger = get_logger(__name__)

router = APIRouter(tags=["decisions"])

# Field names that make up the "outcome" half of a decision record. Whatever A3
# calls them, they are surfaced separately so the console can show
# recommendation and outcome as the brief's decision history does.
_OUTCOME_FIELDS = ("submitted", "allocation_id", "outcome_note", "note", "resolved_at")


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _split_outcome(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    decision = dict(payload)
    outcome = {}
    for field in _OUTCOME_FIELDS:
        if field in decision:
            outcome[field] = decision.pop(field)
    return decision, (outcome or None)


@router.get("/api/v1/decisions", response_model=DecisionsResponse, summary="Decision audit history")
async def list_decisions(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0, le=100000),
    repository: Any = Depends(get_repository),
    metrics: Any = Depends(get_metrics),
) -> DecisionsResponse:
    try:
        records = await _maybe_await(repository.list_decisions(limit=limit, offset=offset))
    except Exception as exc:  # noqa: BLE001 - typed error, never a stack trace
        record_failure(
            logger,
            metrics,
            event="list_decisions_failed",
            code="repository_error",
            detail={"exc": type(exc).__name__},
        )
        raise dependency_unavailable("Repository.list_decisions", exc) from exc
    rows = []
    for record in records or []:
        payload = to_jsonable(record)
        rows.append(payload if isinstance(payload, dict) else {"value": payload})
    return DecisionsResponse(
        count=len(rows), limit=limit, offset=offset, decisions=rows, simulated=True
    )


@router.get(
    "/api/v1/decisions/{decision_id}",
    response_model=DecisionDetailResponse,
    summary="One decision and its outcome",
)
async def get_decision(
    decision_id: int,
    repository: Any = Depends(get_repository),
    metrics: Any = Depends(get_metrics),
) -> DecisionDetailResponse:
    try:
        record = await _maybe_await(repository.get_decision(decision_id))
    except Exception as exc:  # noqa: BLE001
        record_failure(
            logger,
            metrics,
            event="get_decision_failed",
            code="repository_error",
            detail={"exc": type(exc).__name__, "decision_id": decision_id},
        )
        raise dependency_unavailable("Repository.get_decision", exc) from exc
    if record is None:
        raise api_error(
            404,
            "decision_not_found",
            f"no decision with id {decision_id}",
            details={"decision_id": decision_id},
        )
    payload = to_jsonable(record)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    decision, outcome = _split_outcome(payload)
    return DecisionDetailResponse(decision=decision, outcome=outcome, simulated=True)
