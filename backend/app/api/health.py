"""Liveness, component status and the Prometheus scrape endpoint.

CONTRACT.md section 9 rows: `GET /api/v1/health`, `GET /api/v1/status`,
`GET /metrics`. Brief section 15 is the shape of `/status`: each component
reported separately, followed by p95 latency and error rate.
"""

from __future__ import annotations

import importlib
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from app.api.schemas import (
    ComponentStatus,
    HealthResponse,
    StatusResponse,
    get_allocator,
    get_deepseek_client,
    get_metrics,
    get_repository,
    get_settings,
    get_simulator_client,
    to_jsonable,
)

router = APIRouter(tags=["health"])

_SEVERITY = {"ok": 0, "degraded": 1, "down": 2}


# ---------------------------------------------------------------------------
# Optional variants
# ---------------------------------------------------------------------------
# /status is the endpoint an operator opens *when something is broken*, so it
# must not itself fail because one component is missing. These wrappers turn a
# provider failure into `None` and let the component be reported as down. Tests
# override these (they delegate to the real providers above).


def _optional(provider: Any, request: Request) -> Any | None:
    try:
        return provider(request)
    except Exception:
        return None


def get_simulator_client_or_none(request: Request) -> Any | None:
    return _optional(get_simulator_client, request)


def get_repository_or_none(request: Request) -> Any | None:
    return _optional(get_repository, request)


def get_metrics_or_none(request: Request) -> Any | None:
    return _optional(get_metrics, request)


def get_allocator_or_none(request: Request) -> Any | None:
    return _optional(get_allocator, request)


def get_deepseek_or_none(request: Request) -> Any | None:
    return _optional(get_deepseek_client, request)


def get_settings_or_none() -> Any | None:
    try:
        return get_settings()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Metric reading
# ---------------------------------------------------------------------------
# CONTRACT.md section 10's Metrics object exposes `observe_request` but no
# reader, so p95 is computed from the Prometheus histogram the middleware
# fills. See the workstream report: this is a contract gap.


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _registries() -> list[Any]:
    registries: list[Any] = []
    try:
        from prometheus_client import REGISTRY as default_registry

        registries.append(default_registry)
    except Exception:
        pass
    module = None
    for name in ("app.observability.metrics", "app.observability"):
        try:
            module = importlib.import_module(name)
            break
        except Exception:
            module = None
    if module is not None:
        candidates = []
        for name in ("REGISTRY", "registry", "METRICS_REGISTRY"):
            candidates.append(getattr(module, name, None))
        getter = getattr(module, "get_registry", None)
        if callable(getter):
            try:
                candidates.append(getter())
            except Exception:
                pass
        for candidate in candidates:
            if candidate is not None and hasattr(candidate, "collect"):
                if all(candidate is not r for r in registries):
                    registries.append(candidate)
    return registries


def _p95_from_buckets(buckets: list[tuple[float, float]], count: float) -> float | None:
    """Linear-interpolated p95 in milliseconds from cumulative buckets."""
    if count <= 0 or not buckets:
        return None
    target = 0.95 * count
    buckets = sorted(buckets, key=lambda b: b[0])
    prev_le = 0.0
    prev_cum = 0.0
    for le, cumulative in buckets:
        if cumulative >= target:
            if le == float("inf"):
                return prev_le * 1000.0
            span = le - prev_le
            if cumulative > prev_cum:
                fraction = (target - prev_cum) / (cumulative - prev_cum)
            else:
                fraction = 1.0
            return (prev_le + span * max(0.0, min(1.0, fraction))) * 1000.0
        prev_le, prev_cum = le, cumulative
    # Fewer observations than the target: the highest finite bucket is the
    # best available answer, and it is a conservative one.
    finite = [b for b in buckets if b[0] != float("inf")]
    return finite[-1][0] * 1000.0 if finite else None


def prometheus_stats() -> dict[str, Any]:
    """Aggregate p95 latency and error rate across the registries we can see.

    Buckets are summed across label sets, so this is an aggregate p95 across
    routes, not a per-route percentile — the right number for a status page.
    """
    total = 0.0
    errors = 0.0
    client_errors = 0.0
    count = 0.0
    buckets: list[tuple[float, float]] = []
    saw_any = False
    for registry in _registries():
        try:
            families = list(registry.collect())
        except Exception:
            continue
        for family in families:
            name = getattr(family, "name", "") or ""
            samples = getattr(family, "samples", ()) or ()
            if name.startswith("http_request_duration_seconds"):
                saw_any = True
                for sample in samples:
                    if sample.name.endswith("_bucket"):
                        le = _as_float(sample.labels.get("le"))
                        if le is None:
                            continue
                        buckets.append((le, sample.value))
                    elif sample.name.endswith("_count"):
                        count += sample.value
            elif name in ("http_requests_total", "http_requests"):
                saw_any = True
                for sample in samples:
                    status = str(sample.labels.get("status", ""))
                    total += sample.value
                    if status.startswith("5"):
                        errors += sample.value
                    elif status.startswith("4"):
                        client_errors += sample.value
    stats: dict[str, Any] = {
        "p95_latency_ms": _p95_from_buckets(buckets, count) if buckets else None,
        "error_rate": (errors / total) if total > 0 else None,
        "client_error_rate": (client_errors / total) if total > 0 else None,
        "requests_total": int(total) if total else None,
        "source": "prometheus" if saw_any else None,
    }
    return stats


def _stats_from_metrics_object(metrics: Any) -> dict[str, Any]:
    """Use A9's own readouts where they exist.

    CONTRACT.md section 10 pins only the *writers* on `Metrics`
    (`observe_request`, `record_forecast`, ...). A9 also ships
    `status_snapshot()` / `p95_latency_ms()` / `error_rate()`, which are
    preferred here; the Prometheus registry below is the fallback for any other
    `Metrics` implementation.
    """
    stats: dict[str, Any] = {}
    for name in (
        "status_snapshot",
        "snapshot",
        "stats",
        "latency_stats",
        "metrics_snapshot",
    ):
        reader = getattr(metrics, name, None)
        if callable(reader):
            try:
                value = reader()
            except Exception:
                continue
            if isinstance(value, dict):
                stats.update(value)
                stats["source"] = f"Metrics.{name}()"
                break
    direct = getattr(metrics, "p95_latency_ms", None)
    if callable(direct):
        try:
            stats["p95_latency_ms"] = direct()
        except Exception:
            pass
    elif isinstance(direct, (int, float)):
        stats["p95_latency_ms"] = direct
    rate = getattr(metrics, "error_rate", None)
    if callable(rate):
        try:
            stats["error_rate"] = rate()
        except Exception:
            pass
    elif isinstance(rate, (int, float)):
        stats["error_rate"] = rate
    return stats


def _normalise_keys(stats: dict[str, Any]) -> dict[str, Any]:
    """Accept the handful of spellings a peer might use for the same number."""
    aliases = {
        "p95_ms": "p95_latency_ms",
        "p95": "p95_latency_ms",
        "latency_p95_ms": "p95_latency_ms",
        "errors_rate": "error_rate",
        "error_ratio": "error_rate",
        "total_requests": "requests_total",
    }
    out = dict(stats)
    for alias, canonical in aliases.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
    return out


def _collect_stats(metrics: Any | None) -> dict[str, Any]:
    stats: dict[str, Any] = {}
    if metrics is not None:
        stats.update(_normalise_keys(_stats_from_metrics_object(metrics)))
    fallback = prometheus_stats()
    for key, value in fallback.items():
        if stats.get(key) is None and value is not None:
            stats[key] = value
    stats.setdefault("p95_latency_ms", None)
    stats.setdefault("error_rate", None)
    stats.setdefault("requests_total", None)
    # A9 reports the request count as a float histogram count; the status
    # response is an integer count.
    total = stats.get("requests_total")
    if isinstance(total, (int, float)):
        stats["requests_total"] = int(total)
    for key in ("p95_latency_ms", "error_rate", "client_error_rate"):
        value = stats.get(key)
        if isinstance(value, (int, float)):
            stats[key] = round(float(value), 6)
    return stats


# ---------------------------------------------------------------------------
# Component probes
# ---------------------------------------------------------------------------


async def _probe_simulator(client: Any) -> ComponentStatus:
    if client is None:
        return ComponentStatus(status="down", detail="simulator client is not configured")
    breaker = getattr(client, "breaker_state", None)
    if callable(breaker):
        try:
            breaker = breaker()
        except Exception:
            breaker = None
    extra: dict[str, Any] = {"breaker_state": breaker}
    try:
        health = await client.get_health()
    except Exception as exc:
        detail = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
        if breaker == "open":
            return ComponentStatus(
                status="down",
                detail=f"circuit breaker open; last error: {detail}",
                **extra,
            )
        return ComponentStatus(status="down", detail=str(detail), **extra)

    payload = to_jsonable(health)
    if not isinstance(payload, dict):
        payload = {"value": payload}
    raw_status = payload.pop("status", None)  # `status` is the field we set below
    extra.update({k: v for k, v in payload.items() if k not in extra})
    raw = str(raw_status or "").lower()
    if raw in ("ok", "healthy", "up", "running"):
        status = "ok"
    elif raw in ("degraded", "warning", "paused"):
        status = "degraded"
    else:
        status = "down"
    if breaker == "half_open" and status == "ok":
        status = "degraded"
    return ComponentStatus(status=status, detail=raw_status, **extra)


async def _probe_database(repository: Any) -> ComponentStatus:
    if repository is None:
        return ComponentStatus(status="down", detail="repository is not configured")
    # Prefer an explicit ping; fall back to the cheapest contract read.
    ping = getattr(repository, "ping", None)
    try:
        if callable(ping):
            result = ping()
            if hasattr(result, "__await__"):
                result = await result
            # `Database.ping()` reports failure by returning False, not raising.
            # Reading only for the await and ignoring the value reported a
            # database that could not be opened as `ok`.
            if result is False:
                return ComponentStatus(
                    status="down",
                    detail="ping failed; the database is not reachable",
                )
            return ComponentStatus(status="ok", detail="ping ok")
        latest = repository.latest_snapshot()
        if hasattr(latest, "__await__"):
            latest = await latest
        detail = "ok" if latest is None else "ok (snapshot stored)"
        return ComponentStatus(status="ok", detail=detail)
    except Exception as exc:
        return ComponentStatus(status="down", detail=f"{type(exc).__name__}: {exc}")


def _probe_llm(settings: Any, client: Any) -> ComponentStatus:
    """`degraded`, never `down`, when no key is configured — CONTRACT.md 9.

    A missing key is a supported operating mode: the deterministic explanation
    path serves the operator (CONTRACT.md section 8.2).
    """
    model = getattr(settings, "deepseek_model", None)
    configured = bool(getattr(settings, "llm_available", False))
    if client is not None:
        available = getattr(client, "available", None)
        if callable(available):
            try:
                available = available()
            except Exception:
                available = None
        if available is not None:
            configured = bool(configured and available)
    if settings is None and client is None:
        return ComponentStatus(
            status="degraded",
            detail="llm not configured; deterministic fallback explanations in use",
        )
    if configured:
        return ComponentStatus(status="ok", detail="deepseek client ready", model=model)
    return ComponentStatus(
        status="degraded",
        detail="no API key configured; deterministic fallback explanations in use",
        model=model,
    )


def _probe_decision_engine(allocator: Any) -> ComponentStatus:
    if allocator is None:
        return ComponentStatus(status="down", detail="allocation engine is not available")
    policy = getattr(allocator, "policy", None)
    if callable(policy):  # pragma: no cover - defensive
        try:
            policy = policy()
        except Exception:
            policy = None
    optimizer = None
    try:
        optimizer = importlib.import_module("scipy.optimize") is not None
    except Exception:
        optimizer = False
    if optimizer:
        return ComponentStatus(status="ok", detail=f"policy={policy or 'optimizer'}", policy=policy)
    return ComponentStatus(
        status="degraded",
        detail=f"scipy unavailable; priority heuristic in use (policy={policy or 'heuristic'})",
        policy=policy,
    )


def _worst(components: dict[str, ComponentStatus]) -> str:
    worst = "ok"
    for component in components.values():
        if _SEVERITY.get(component.status, 0) > _SEVERITY[worst]:
            worst = component.status
    return worst


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/api/v1/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    """Deliberately dependency-free and constant-time.

    CONTRACT.md section 9: this is the container HEALTHCHECK and the liveness
    probe, so it must not touch the simulator, the database, the LLM or the
    metrics registry. Anything that can fail belongs on /status.
    """
    return HealthResponse()


@router.get("/api/v1/status", response_model=StatusResponse, summary="Component status")
async def status(
    request: Request,
    settings: Any | None = Depends(get_settings_or_none),
    simulator: Any | None = Depends(get_simulator_client_or_none),
    repository: Any | None = Depends(get_repository_or_none),
    metrics: Any | None = Depends(get_metrics_or_none),
    allocator: Any | None = Depends(get_allocator_or_none),
    llm_client: Any | None = Depends(get_deepseek_or_none),
) -> StatusResponse:
    components = {
        # Brief section 15 lists the backend API itself first.
        "api": ComponentStatus(status="ok", detail="serving requests"),
        "simulator": await _probe_simulator(simulator),
        "database": await _probe_database(repository),
        "llm": _probe_llm(settings, llm_client),
        "decision_engine": _probe_decision_engine(allocator),
    }
    stats = _collect_stats(metrics)
    return StatusResponse(
        status=_worst(components),  # type: ignore[arg-type]
        components=components,
        p95_latency_ms=stats.get("p95_latency_ms"),
        error_rate=stats.get("error_rate"),
        requests_total=stats.get("requests_total"),
        checked_at=round(time.time(), 3),
        metrics_source=stats.get("source"),
        client_error_rate=stats.get("client_error_rate"),
    )


# `/metrics` is owned by A9 (`app/observability/setup.py`), which owns the
# registry and the exposition. CONTRACT.md section 9 lists it in this table, but
# registering it a second time here would be a duplicate route: the first
# registered handler wins, and two definitions can silently disagree. Left out
# deliberately — see `tests/test_api_routes.py::test_metrics_is_registered_once`.
