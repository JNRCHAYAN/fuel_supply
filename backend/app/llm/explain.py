"""ExplanationService -- LLM-backed explanation with a mandatory fallback.

CONTRACT.md section 8.2. The brief (section 7) asks for incident explanation,
supply-chain summarisation, operator investigation and human-readable decision
explanations, and is explicit that the LLM supports the operational system
rather than being a chatbot. So this service **explains decisions the
deterministic engines already made**; it never makes one, never re-ranks, and
never invents a number.

The contract's interesting requirement is the fallback. Four triggers --
key absent, ``LLM_ENABLED=false``, timeout/transport failure, response failing
validation -- all produce a deterministic templated explanation built from the
*same structured facts*, marked ``source="fallback"``, ``degraded=True``. The
operator always gets an explanation, and always knows where it came from. This
is the brief's section 11 "ML model unavailable -> fallback" and it is exercised
by tests that run with no key configured, which is the default state.

Peer types (``Snapshot``, ``RiskSignal``, ``Recommendation``, ``Repository``)
are imported under ``TYPE_CHECKING`` only and read defensively via ``_get``, so
this module imports and runs whether or not those peers exist yet, and tolerates
a partially populated snapshot if one arrives.
"""

from __future__ import annotations

import inspect
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Literal, Mapping, Sequence

import structlog

from app.llm import prompts
from app.llm.deepseek import DeepSeekClient, LLMUnavailable

if TYPE_CHECKING:  # pragma: no cover - names for readers and type checkers only
    from app.intelligence.allocate import Recommendation
    from app.intelligence.detect import RiskSignal
    from app.observability.metrics import Metrics
    from app.sim.models import Snapshot
    from app.store.repository import Repository

logger = structlog.get_logger(__name__)

#: Severity ordering used to sort signals most severe first. Matches A5's
#: ``Severity`` values; an unknown value sorts last rather than crashing.
_SEVERITY_RANK = {"critical": 0, "serious": 1, "warning": 2, "info": 3}

#: Purpose labels. Bounded, used as a metric label.
PURPOSES = ("recommendation", "network", "investigation")


@dataclass(frozen=True)
class Explanation:
    """CONTRACT 8.2.

    ``reason`` is an additive field holding the bounded fallback reason (one of
    :data:`app.llm.prompts.FALLBACK_REASON_TEXT`) when ``source == "fallback"``,
    and ``None`` otherwise. It is what makes ``llm_fallback_total{reason}``
    possible without unbounded label cardinality.
    """

    text: str
    source: Literal["llm", "fallback"]
    model: str | None
    degraded: bool
    simulated: bool = True
    reason: str | None = None


# --------------------------------------------------------------------------
# Tolerant readers -- peers may be absent, partial, or plain dicts
# --------------------------------------------------------------------------


def _get(obj: Any, name: str, default: Any = None) -> Any:
    """Read ``name`` off an object or a mapping, with a default.

    Duck-typed on purpose: the API layer may hand us a domain object or a dict,
    and a peer module may not exist yet.
    """

    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _seq(value: Any) -> list[Any]:
    """Coerce a value to a list without raising."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    if isinstance(value, Mapping):
        return list(value.values())
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


def _num(value: Any) -> float | None:
    """Coerce to a finite float, or None. Never NaN, never raises."""

    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _enum_value(value: Any) -> Any:
    """Unwrap a str-enum to its value; pass anything else through."""

    return getattr(value, "value", value)


def _status_counts(items: Sequence[Any]) -> dict[str, int]:
    counter = Counter(_text(_enum_value(_get(item, "status", "UNKNOWN")), "UNKNOWN") for item in items)
    return dict(sorted(counter.items()))


# --------------------------------------------------------------------------
# Fact builders -- the only thing the model is ever shown
# --------------------------------------------------------------------------


def signal_facts(signal: Any) -> dict[str, Any]:
    """Structured facts for one A5 ``RiskSignal`` (CONTRACT 7.2)."""

    return {
        "kind": _text(_get(signal, "kind"), "unknown"),
        "severity": _text(_enum_value(_get(signal, "severity", "unknown")), "unknown"),
        "entity_type": _text(_get(signal, "entity_type"), "unknown"),
        "entity_id": _text(_get(signal, "entity_id"), "unknown"),
        "detected_at_tick": _num(_get(signal, "detected_at_tick")),
        "summary": _text(_get(signal, "summary")),
        "evidence": _get(signal, "evidence", {}) or {},
        "confidence": _num(_get(signal, "confidence")),
    }


def _alternative_facts(alternative: Any) -> dict[str, Any]:
    # `reason_not_chosen` is A6's field name (CONTRACT 7.3, app/intelligence/
    # allocate.py); the other spellings are accepted so the explanation layer
    # keeps working if the shape is widened.
    return {
        "station_id": _text(_get(alternative, "station_id"), "unknown"),
        "depot_id": _text(_get(alternative, "depot_id"), "unknown"),
        "route_id": _text(_get(alternative, "route_id"), "unknown"),
        "fuel_type": _text(_get(alternative, "fuel_type"), "unknown"),
        "quantity_liters": _num(_get(alternative, "quantity_liters")),
        "reason_not_chosen": _text(
            _get(alternative, "reason_not_chosen")
            or _get(alternative, "reason")
            or _get(alternative, "rejected_because")
            or ""
        ),
        "confidence": _num(_get(alternative, "confidence")),
    }


def _related(signal: Any, subject_ids: set[str]) -> bool:
    entity_id = _text(_get(signal, "entity_id"))
    return bool(entity_id) and entity_id in subject_ids


def recommendation_facts(
    rec: Any, signals: Sequence[Any] = (), *, policy: str | None = None
) -> dict[str, Any]:
    """Structured facts for one A6 ``Recommendation`` plus its signals.

    Every value comes from the engines. Nothing here is computed by a model, and
    nothing here is inferred beyond counting and sorting for presentation.
    """

    subject_ids = {
        _text(_get(rec, "station_id")),
        _text(_get(rec, "depot_id")),
        _text(_get(rec, "route_id")),
    } - {""}

    decorated: list[tuple[bool, dict[str, Any]]] = []
    for signal in _seq(signals):
        facts = signal_facts(signal)
        facts["related_to_recommendation"] = _related(signal, subject_ids)
        decorated.append((not facts["related_to_recommendation"], facts))
    # Related first, then most severe, then most confident, then kind+entity for
    # a total order -- so the fallback text is identical for identical input.
    decorated.sort(
        key=lambda pair: (
            pair[0],
            _SEVERITY_RANK.get(pair[1]["severity"], 99),
            -(pair[1]["confidence"] or 0.0),
            pair[1]["kind"],
            pair[1]["entity_id"],
        )
    )
    ordered_signals = [facts for _, facts in decorated]

    recommendation = {
        "id": _text(_get(rec, "id"), "unknown"),
        "station_id": _text(_get(rec, "station_id"), "unknown"),
        "depot_id": _text(_get(rec, "depot_id"), "unknown"),
        "route_id": _text(_get(rec, "route_id"), "unknown"),
        "fuel_type": _text(_get(rec, "fuel_type"), "unknown"),
        "quantity_liters": _num(_get(rec, "quantity_liters")),
        "rationale": _text(_get(rec, "rationale")),
        "constraints": [_text(item) for item in _seq(_get(rec, "constraints"))],
        "expected_impact": {
            _text(k): _num(v) if _num(v) is not None else _text(v)
            for k, v in (_get(rec, "expected_impact", {}) or {}).items()
        },
        "alternatives": [_alternative_facts(a) for a in _seq(_get(rec, "alternatives"))],
        "confidence": _num(_get(rec, "confidence")),
        "simulated": bool(_get(rec, "simulated", True)),
    }

    return {
        "purpose": "recommendation",
        "simulated": True,
        "recommendation": recommendation,
        "risk_signals": ordered_signals,
        "related_signal_count": sum(1 for s in ordered_signals if s["related_to_recommendation"]),
        "engine_confidence": recommendation["confidence"],
        "policy": policy or _text(_get(rec, "policy"), "") or "unspecified",
    }


def snapshot_facts(snapshot: Any, signals: Sequence[Any] = ()) -> dict[str, Any]:
    """Structured facts for an A2 ``Snapshot`` (CONTRACT 5.2) plus signals.

    Reads defensively: a missing attribute, an empty collection or a snapshot
    that only half-arrived yields a smaller fact dict, never an exception.
    """

    depots = _seq(_get(snapshot, "depots"))
    stations = _seq(_get(snapshot, "stations"))
    routes = _seq(_get(snapshot, "routes"))
    regions = _seq(_get(snapshot, "regions"))
    arrivals = _seq(_get(snapshot, "supply_arrivals"))
    events = _seq(_get(snapshot, "events"))
    metrics = _get(snapshot, "metrics")

    inventory_by_fuel: dict[str, float] = {}
    for station in stations:
        inventory = _get(station, "inventory", {}) or {}
        if not isinstance(inventory, Mapping):
            continue
        for fuel, litres in inventory.items():
            amount = _num(litres)
            if amount is None:
                continue
            fuel_key = _text(_enum_value(fuel))
            inventory_by_fuel[fuel_key] = inventory_by_fuel.get(fuel_key, 0.0) + amount

    depot_inventory_by_fuel: dict[str, float] = {}
    for depot in depots:
        inventory = _get(depot, "inventory", {}) or {}
        if not isinstance(inventory, Mapping):
            continue
        for fuel, litres in inventory.items():
            amount = _num(litres)
            if amount is None:
                continue
            fuel_key = _text(_enum_value(fuel))
            depot_inventory_by_fuel[fuel_key] = depot_inventory_by_fuel.get(fuel_key, 0.0) + amount

    active_events = [
        {
            "id": _get(event, "id"),
            "type": _text(_get(event, "type"), "unknown"),
            "status": _text(_enum_value(_get(event, "status", "UNKNOWN")), "UNKNOWN"),
            "start_tick": _num(_get(event, "start_tick")),
            "end_tick": _num(_get(event, "end_tick")),
            "parameters": _get(event, "parameters", {}) or {},
        }
        for event in events
        if _text(_enum_value(_get(event, "status", "")), "").upper() != "RESOLVED"
    ]

    signal_fact_list = [signal_facts(signal) for signal in _seq(signals)]
    signal_fact_list.sort(
        key=lambda item: (
            _SEVERITY_RANK.get(item["severity"], 99),
            -(item["confidence"] or 0.0),
            item["kind"],
            item["entity_id"],
        )
    )

    return {
        "purpose": "network",
        "simulated": True,
        "tick": _num(_get(snapshot, "tick")),
        "sim_time": _text(_get(snapshot, "sim_time"), "unknown"),
        "status": _text(_enum_value(_get(snapshot, "status", "unknown")), "unknown"),
        "stale": bool(_get(snapshot, "stale", False)),
        "age_seconds": _num(_get(snapshot, "age_seconds")) or 0.0,
        "counts": {
            "depots": len(depots),
            "stations": len(stations),
            "routes": len(routes),
            "regions": len(regions),
            "supply_arrivals": len(arrivals),
            "events": len(events),
        },
        "status_counts": {
            "depots": _status_counts(depots),
            "stations": _status_counts(stations),
            "routes": _status_counts(routes),
        },
        "station_inventory_by_fuel": dict(sorted(inventory_by_fuel.items())),
        "depot_inventory_by_fuel": dict(sorted(depot_inventory_by_fuel.items())),
        "supply_arrival_status": _status_counts(arrivals),
        "metrics": {
            "served_demand_liters": _num(_get(metrics, "served_demand_liters")),
            "unmet_demand_liters": _num(_get(metrics, "unmet_demand_liters")),
            "service_level": _num(_get(metrics, "service_level")),
            "allocation_liters": _num(_get(metrics, "allocation_liters")),
            "allocation_failures": _num(_get(metrics, "allocation_failures")),
        },
        "active_events": active_events[:10],
        "risk_signals": signal_fact_list,
    }


def investigation_facts(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Facts for an operator investigation.

    ``context`` is whatever the API layer assembled. It is sanitised by
    :func:`app.llm.prompts.sanitize_facts` before it can reach a prompt, so a
    context dict carrying credentials cannot leak into one.
    """

    facts: dict[str, Any] = {"purpose": "investigation", "simulated": True}
    if context:
        facts["context"] = context
    return facts


def _bounded_reason(reason: Any) -> str:
    """Clamp an arbitrary reason to the bounded label set (CONTRACT 10)."""

    text = _text(reason, "unknown") or "unknown"
    return text if text in prompts.FALLBACK_REASON_TEXT else "unknown"


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class ExplanationService:
    """Turns engine output into prose, with a guaranteed deterministic fallback.

    CONTRACT 8.2 signature. Never raises on an LLM problem: a missing key, a
    timeout, a rejected request or a response that fails validation all become a
    templated ``Explanation`` with ``source="fallback"``.
    """

    def __init__(self, client: DeepSeekClient, repo: "Repository", metrics: "Metrics") -> None:
        self._client = client
        self._repo = repo
        self._metrics = metrics

    # -- public API -------------------------------------------------------

    async def explain_recommendation(
        self,
        rec: "Recommendation",
        signals: Sequence["RiskSignal"],
        *,
        policy: str | None = None,
    ) -> Explanation:
        """Explain one decision-engine recommendation (brief section 7).

        ``policy`` is an additive, keyword-only extension: A6's ``Recommendation``
        carries no policy field, so the caller (A8) may pass
        ``AllocationEngine.policy`` to record which path -- optimiser or the
        section 11 priority heuristic -- actually produced this recommendation.
        Omitting it is fine; the explanation simply does not name a policy.
        """

        facts = recommendation_facts(rec, signals, policy=policy)
        system, user = prompts.recommendation_prompt(facts)
        return await self._run(
            purpose="recommendation",
            system=system,
            user=user,
            render=lambda reason: prompts.render_recommendation_fallback(facts, reason),
        )

    async def summarize_network(
        self, snapshot: "Snapshot", signals: Sequence["RiskSignal"]
    ) -> Explanation:
        """Summarise the simulated supply-chain state (brief section 7)."""

        facts = snapshot_facts(snapshot, signals)
        system, user = prompts.network_prompt(facts)
        return await self._run(
            purpose="network",
            system=system,
            user=user,
            render=lambda reason: prompts.render_network_fallback(facts, reason),
        )

    async def investigate(self, question: str, *, context: dict[str, Any]) -> Explanation:
        """Answer an operator investigation question (brief section 7)."""

        facts = investigation_facts(context)
        system, user = prompts.investigation_prompt(question, facts)
        return await self._run(
            purpose="investigation",
            system=system,
            user=user,
            render=lambda reason: prompts.render_investigation_fallback(question, facts, reason),
        )

    # -- orchestration ----------------------------------------------------

    async def _run(
        self,
        *,
        purpose: str,
        system: str,
        user: str,
        render: Callable[[str], str],
    ) -> Explanation:
        """Call the model, or fall back. Never raises."""

        started = time.perf_counter()
        model = getattr(self._client, "model", None)
        # Read defensively: this line is outside the guard below, and the
        # guarantee that this method never raises has to hold for the whole body.
        # Defaulting to None (rather than "unknown") means a client that simply
        # does not expose the attribute still gets its `complete` attempted; a
        # genuinely broken client fails inside the guard and degrades there.
        reason = getattr(self._client, "unavailable_reason", None)
        text: str | None = None

        if reason is None:
            try:
                text = await self._client.complete(system=system, user=user, purpose=purpose)
            except LLMUnavailable as exc:
                reason = _bounded_reason(getattr(exc, "reason", None))
                logger.warning(
                    "llm_fallback", purpose=purpose, reason=reason, degraded=True
                )
            except Exception as exc:  # noqa: BLE001 - degrade, never 500
                # CancelledError is a BaseException and is deliberately not
                # caught here, so cancellation still propagates.
                reason = "unknown"
                logger.error(
                    "llm_unexpected_failure", purpose=purpose, error_type=type(exc).__name__
                )

        latency = time.perf_counter() - started

        if text is None:
            explanation = self._fallback_explanation(purpose, reason, render)
        else:
            explanation = Explanation(
                text=text,
                source="llm",
                model=model,
                degraded=False,
            )

        await self._record_call(
            purpose=purpose,
            model=explanation.model or model or "unavailable",
            ok=explanation.source == "llm",
            latency_ms=latency * 1000.0,
            fallback_used=explanation.degraded,
        )
        self._record_metric(
            purpose=purpose,
            ok=explanation.source == "llm",
            latency=latency,
            fallback_reason=explanation.reason,
        )
        return explanation

    def _fallback_explanation(
        self, purpose: str, reason: str | None, render: Callable[[str], str]
    ) -> Explanation:
        """Build the deterministic explanation, with a last-resort guard.

        The template renderer is pure and is tested, so it should not raise -- but
        "the operator always gets an explanation" is the contract, and a crash
        here would be a 500. Hence the guard.
        """

        bounded = _bounded_reason(reason)
        try:
            text = render(bounded)
        except Exception as exc:  # noqa: BLE001 - the guard is the whole point
            logger.error(
                "llm_fallback_render_failed",
                purpose=purpose,
                reason=bounded,
                error_type=type(exc).__name__,
            )
            text = (
                f"SIMULATED {purpose.upper()} EXPLANATION\n"
                f"{prompts.FALLBACK_BANNER}\n"
                f"Reason the model was not used: {prompts.fallback_reason_text(bounded)}.\n"
                "The deterministic template could not be rendered for this request; "
                "the underlying facts remain available on the structured endpoints."
            )
        if not text or not text.strip():
            text = (
                f"SIMULATED {purpose.upper()} EXPLANATION\n"
                f"{prompts.FALLBACK_BANNER}\n"
                "No explanation text could be produced for this request. The underlying "
                "facts remain available on the structured endpoints."
            )
        return Explanation(
            text=text,
            source="fallback",
            model=None,
            degraded=True,
            reason=bounded,
        )

    # -- peer calls, both guarded -----------------------------------------

    async def _record_call(
        self,
        *,
        purpose: str,
        model: str,
        ok: bool,
        latency_ms: float,
        fallback_used: bool,
    ) -> None:
        """``Repository.record_llm_call`` (CONTRACT 6).

        Guarded and awaited-if-awaitable: a database problem must not cost the
        operator the explanation they already have.
        """

        record = getattr(self._repo, "record_llm_call", None)
        if record is None:
            return
        try:
            result = record(
                purpose=purpose,
                model=model,
                ok=ok,
                latency_ms=int(latency_ms),
                fallback_used=fallback_used,
            )
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - the explanation still stands
            logger.warning(
                "llm_repo_record_failed", purpose=purpose, error_type=type(exc).__name__
            )

    def _record_metric(
        self, *, purpose: str, ok: bool, latency: float, fallback_reason: str | None
    ) -> None:
        """``Metrics.record_llm_call`` (CONTRACT 10). Best effort."""

        record = getattr(self._metrics, "record_llm_call", None)
        if record is None:
            return
        try:
            record(purpose, ok, latency, fallback_reason)
        except Exception as exc:  # noqa: BLE001 - observability never breaks a call
            logger.warning(
                "llm_metric_failed", purpose=purpose, error_type=type(exc).__name__
            )


__all__ = [
    "Explanation",
    "ExplanationService",
    "LLMUnavailable",
    "PURPOSES",
    "investigation_facts",
    "recommendation_facts",
    "signal_facts",
    "snapshot_facts",
]
