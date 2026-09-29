"""Request/response models and shared API plumbing (CONTRACT.md section 9).

Two things live here:

1.  Pydantic models for every request body and response the API returns, so
    that CONTRACT.md section 9's "every route validates input via schemas.py"
    and section 0.3 ("validate external input") hold for the whole surface.
2.  A small amount of shared plumbing the route modules all need: the typed
    error envelope, JSON coercion for the peer dataclasses, and the FastAPI
    dependency providers.

Why the dependency providers are here rather than in the route modules: every
route module imports this one, and this module imports nothing of ours, so it is
the only place a shared provider can live without creating an import cycle
between `router.py` and the route modules. See the "Deviations" note in the
workstream report — `app/deps.py` is owned by another workstream (A1) and its
provider names are not pinned by the contract, so the providers below resolve
A1's providers by name at call time and fall back to `app.state`.
"""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import logging
from enum import Enum
from typing import Any, Callable, Iterator, Literal, Mapping

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Logging — structlog when A9's dependency is installed, stdlib otherwise.
# CONTRACT.md section 0.4: every failure path yields a typed error, a log line
# and a metric.
# ---------------------------------------------------------------------------


class _StdlibStyleLogger:
    """Minimal adapter so `logger.error("event", key=value)` works either way."""

    def __init__(self, name: str) -> None:
        self._log = logging.getLogger(name)

    def _emit(self, level: int, event: str, **kw: Any) -> None:
        extra = " ".join(f"{k}={v!r}" for k, v in sorted(kw.items()))
        self._log.log(level, "%s %s", event, extra)

    def debug(self, event: str, **kw: Any) -> None:
        self._emit(logging.DEBUG, event, **kw)

    def info(self, event: str, **kw: Any) -> None:
        self._emit(logging.INFO, event, **kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._emit(logging.WARNING, event, **kw)

    def error(self, event: str, **kw: Any) -> None:
        self._emit(logging.ERROR, event, **kw)


def get_logger(name: str) -> Any:
    try:  # structlog is A9's concern; the API must not hard-depend on it.
        import structlog

        return structlog.get_logger(name)
    except Exception:  # pragma: no cover - only when structlog is unavailable
        return _StdlibStyleLogger(name)


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------
#
# CONTRACT.md section 5.3 establishes `{"detail": {"code", "message"}}` as the
# house envelope for simulator errors. The API reuses that shape so an operator
# (and the console) sees one error format everywhere, with `simulated` added per
# section 0.6. FastAPI's default HTTPException handler renders `detail` verbatim,
# so raising HTTPException(detail={...}) produces exactly this body.

SIMULATED = True


class ErrorDetail(BaseModel):
    """The typed error body carried inside `detail`."""

    code: str
    message: str
    simulated: bool = True
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    """`{"detail": {"code": ..., "message": ..., "simulated": true}}`."""

    detail: ErrorDetail


def api_error(
    status_code: int,
    code: str,
    message: str,
    *,
    details: Mapping[str, Any] | None = None,
) -> HTTPException:
    """Build a typed HTTPException. Never include a secret in `message`."""
    detail: dict[str, Any] = {"code": code, "message": message, "simulated": True}
    if details:
        detail["details"] = {str(k): v for k, v in details.items()}
    return HTTPException(status_code=status_code, detail=detail)


def dependency_unavailable(name: str, exc: BaseException | None = None) -> HTTPException:
    """503 when a peer module or a configured dependency is not present.

    This is the honest failure for a component the API composes but does not
    own: the operator gets a typed error naming the missing piece, never a 500
    and never a stack trace.
    """
    message = f"dependency '{name}' is unavailable"
    if exc is not None:
        message = f"{message} ({type(exc).__name__})"
    return api_error(503, "dependency_unavailable", message, details={"dependency": name})


# ---------------------------------------------------------------------------
# JSON coercion
# ---------------------------------------------------------------------------


def to_jsonable(value: Any) -> Any:
    """Convert peer dataclasses / enums / models into JSON-safe structures.

    The API composes objects owned by A2 (simulator models), A3 (records), A4-A6
    (engine results) and A7 (explanations). Serialising them generically keeps
    the API decoupled from their exact declaration style.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: to_jsonable(getattr(value, f.name, None))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_jsonable(v) for v in value]
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return to_jsonable(dump())
        except Exception:
            pass
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {k: to_jsonable(v) for k, v in attrs.items() if not k.startswith("_")}
    return str(value)


def _as_dict_list(items: Any) -> list[dict[str, Any]]:
    if not items:
        return []
    return [to_jsonable(i) for i in items]


# ---------------------------------------------------------------------------
# Failure classification for simulator calls
# ---------------------------------------------------------------------------
#
# A2 owns SimulatorError. Importing it eagerly would make this module (and so
# every route) unimportable until A2 lands, so the class is matched by name and
# by shape -- `status_code`, `code`, `message`, `kind` are read defensively with
# getattr, and none of them is imported. That is deliberate: see the workstream
# report.

#: A failure whose *only* honest reading is "the simulator is not answering".
#: These names are A2's local breaker/unavailable errors; they carry no status
#: code because no response ever arrived.
_SIMULATOR_UNAVAILABLE_NAMES = frozenset({"CircuitOpenError", "SimulatorUnavailable"})

#: `SimulatorError.kind` values that mean we never got a usable response
#: (app/sim/errors.py): connection refused/DNS/timeout, an undecodable 2xx body,
#: and a tripped breaker with no last-good value. Retryable by construction.
_UNAVAILABLE_KINDS = frozenset({"transport", "decode", "circuit_open"})


def is_simulator_error(exc: BaseException) -> bool:
    if type(exc).__name__ in {"SimulatorError", "CircuitOpenError", "SimulatorUnavailable"}:
        return True
    return exc.__class__.__module__.startswith("app.sim")


def _is_simulator_unavailable(exc: BaseException) -> bool:
    """True when the simulator (not our request) is at fault.

    A 5xx is the simulator failing to serve us; a transport/decode failure, a
    tripped breaker or an explicit breaker/unavailable class means we never got
    a usable answer at all. All of those are the back-off-and-retry case.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status >= 500:
        return True
    if type(exc).__name__ in _SIMULATOR_UNAVAILABLE_NAMES:
        return True
    return getattr(exc, "kind", None) in _UNAVAILABLE_KINDS


def simulator_error_to_http(exc: BaseException) -> HTTPException:
    """Map a simulator failure onto the HTTP status the *caller* must branch on.

    Guide section 9 is a per-code table, not one status: a rejected allocation is
    its own 404/409/422 and only ``503 FAULT_INJECTED`` (plus a transport fault,
    which is the same condition seen from this side) is the back-off case.
    Section 10 bullet 11 pins ``POST /v1/allocations`` to ``201/200/404/409/503``.

    This used to collapse *every* simulator error to 502, which destroyed that
    distinction: a 409 ``INSUFFICIENT_INVENTORY`` or ``IDEMPOTENCY_KEY_MISMATCH``
    reached the operator as "upstream is broken, try again", actively inviting a
    re-POST of the very allocation the simulator had just refused. So:

    * a 4xx the simulator chose (``SimulatorError.status_code``) is preserved --
      our request was wrong and the ``code`` in the body says which way;
    * a 5xx, a transport fault or a tripped breaker is 503, the
      back-off-and-retry case;
    * anything we cannot classify at all stays 502.

    The machine-readable ``code`` is carried in the body in every case: it, not
    the status, is what callers branch on.
    """
    code = getattr(exc, "code", None) or "simulator_error"
    message = getattr(exc, "message", None) or str(exc) or type(exc).__name__
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int) and 400 <= status_code < 500:
        status = status_code
    elif _is_simulator_unavailable(exc):
        status = 503
    else:
        status = 502
    return api_error(status, str(code), str(message), details={"source": "simulator"})


# ---------------------------------------------------------------------------
# Honest record of a failure: log line + metric (CONTRACT.md section 0.4)
# ---------------------------------------------------------------------------


def record_failure(
    logger: Any,
    metrics: Any | None,
    *,
    event: str,
    code: str,
    detail: Mapping[str, Any] | None = None,
) -> None:
    try:
        logger.error(event, code=code, **(dict(detail) if detail else {}))
    except Exception:  # pragma: no cover - logging must never break a request
        pass
    recorder = getattr(metrics, "record_api_error", None)
    if callable(recorder):
        try:
            recorder(code)
        except Exception:  # pragma: no cover
            pass


_log = get_logger("app.api")


async def simulator_guard(
    operation: str,
    awaitable: Any,
    *,
    metrics: Any | None = None,
) -> Any:
    """Await a simulator call, converting any failure into a typed status.

    The status is the simulator's own when it rejected the request (4xx) and a
    503 when the simulator is unavailable; see `simulator_error_to_http`.
    CONTRACT.md section 0.4 and the "a simulator failure is a typed error, not a
    stack trace" requirement. Cancellation is deliberately *not* swallowed:
    `asyncio.CancelledError` derives from BaseException.
    """
    try:
        return await awaitable
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - every simulator failure is typed
        code = getattr(exc, "code", None) or "simulator_error"
        record_failure(
            _log,
            metrics,
            event="simulator_call_failed",
            code=str(code),
            detail={"operation": operation, "exc": type(exc).__name__},
        )
        raise simulator_error_to_http(exc) from exc


# ---------------------------------------------------------------------------
# Peer request-object construction (A2's AllocationRequest / EventRequest /
# FaultRequest are not pinned by name in the contract beyond the type names)
# ---------------------------------------------------------------------------


def _import_attr(module_name: str, names: tuple[str, ...]) -> Any | None:
    try:
        module = importlib.import_module(module_name)
    except Exception:
        return None
    for name in names:
        obj = getattr(module, name, None)
        if obj is not None:
            return obj
    return None


def build_peer_model(module_name: str, names: tuple[str, ...], data: Mapping[str, Any]) -> Any:
    """Construct a peer request object, or fall back to a plain dict.

    Dataclasses and ordinary constructors are supported. If the peer class is
    not importable yet, the validated payload is passed through as a dict so the
    endpoint stays usable; the simulator client normalises whatever it receives.
    """
    cls = _import_attr(module_name, names)
    if cls is None:
        return dict(data)
    try:
        signature = inspect.signature(cls)
    except (TypeError, ValueError):
        return dict(data)
    params = signature.parameters
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
    kwargs = {
        k: v
        for k, v in data.items()
        if k in params or (accepts_kwargs and v is not None)
    }
    missing = [
        name
        for name, p in params.items()
        if p.default is inspect.Parameter.empty
        and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and name not in kwargs
    ]
    if missing:
        return dict(data)
    try:
        return cls(**kwargs)
    except Exception:
        return dict(data)


# ---------------------------------------------------------------------------
# Dependency providers
# ---------------------------------------------------------------------------
#
# A1 owns `app/deps.py` and `app/config.py`. The contract names the objects
# (`Settings`, `SimulatorClient`, `Repository`, `Metrics`) but not the provider
# function names, so each provider below:
#   1. asks `app.deps` for any of the plausible names, calling it correctly,
#   2. falls back to the attribute A1 would set on `app.state`,
#   3. falls back to constructing the engine from its contract signature,
#   4. raises a typed 503 naming the missing dependency.
#
# Every one of these is a module-level function, so tests can replace it with
# `app.dependency_overrides[get_repository] = lambda: FakeRepository()`.

_DEPS_MODULE = "app.deps"


def _invoke_provider(fn: Callable[..., Any], request: Request | None) -> Any:
    provider = fn
    # Unwrap functools.partial / bound wrappers that hide the signature.
    target = getattr(provider, "__wrapped__", provider)
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return provider()
    required = [
        p
        for p in signature.parameters.values()
        if p.kind
        in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        and p.default is inspect.Parameter.empty
    ]
    if not required:
        return provider()
    if request is None:
        return provider()
    return provider(request)


def _from_deps(names: tuple[str, ...], request: Request | None) -> tuple[bool, Any]:
    try:
        module = importlib.import_module(_DEPS_MODULE)
    except Exception:
        return False, None
    for name in names:
        fn = getattr(module, name, None)
        if callable(fn):
            try:
                return True, _invoke_provider(fn, request)
            except HTTPException:
                raise
            except Exception:
                continue
    return False, None


def _from_state(request: Request | None, names: tuple[str, ...]) -> tuple[bool, Any]:
    if request is None:
        return False, None
    state = getattr(request.app, "state", None)
    if state is None:
        return False, None
    for name in names:
        value = getattr(state, name, None)
        if value is not None:
            return True, value
    return False, None


def _cached(request: Request | None, key: str, factory: Callable[[], Any]) -> Any:
    """Build once per app (engines are cheap; an LLM client is not)."""
    if request is not None:
        state = getattr(request.app, "state", None)
        if state is not None:
            existing = getattr(state, key, None)
            if existing is not None:
                return existing
    value = factory()
    if request is not None:
        try:
            setattr(request.app.state, key, value)
        except Exception:  # pragma: no cover - State is permissive in practice
            pass
    return value


# --- settings --------------------------------------------------------------


def get_settings() -> Any:
    """Settings from A1's `app.config.get_settings` (CONTRACT.md section 4)."""
    for module_name in ("app.config", "app.settings"):
        getter = _import_attr(module_name, ("get_settings", "settings"))
        if getter is None:
            continue
        try:
            value = getter() if callable(getter) else getter
        except Exception as exc:
            raise dependency_unavailable("settings", exc) from exc
        if value is not None:
            return value
    raise dependency_unavailable("settings")


# --- simulator -------------------------------------------------------------


def get_simulator_client(request: Request) -> Any:
    """A2's `SimulatorClient` (CONTRACT.md section 5.4)."""
    found, value = _from_deps(
        ("get_simulator_client", "get_simulator", "simulator_client", "get_client"),
        request,
    )
    if found:
        return value
    found, value = _from_state(request, ("simulator", "simulator_client"))
    if found:
        return value
    raise dependency_unavailable("simulator_client")


# --- repository ------------------------------------------------------------


def get_repository(request: Request) -> Any:
    """A3's `Repository` (CONTRACT.md section 6)."""
    found, value = _from_deps(("get_repository", "get_repo", "repository"), request)
    if found:
        return value
    found, value = _from_state(request, ("repository", "repo"))
    if found:
        return value
    raise dependency_unavailable("repository")


# --- metrics ---------------------------------------------------------------


def get_metrics(request: Request) -> Any:
    """A9's `Metrics` (CONTRACT.md section 10)."""
    found, value = _from_deps(("get_metrics", "metrics", "get_metrics_client"), request)
    if found:
        return value
    found, value = _from_state(request, ("metrics",))
    if found:
        return value
    raise dependency_unavailable("metrics")


# --- intelligence engines (A4 / A5 / A6) -----------------------------------


def get_forecaster(request: Request) -> Any:
    """A4's `DemandForecaster` (CONTRACT.md section 7.1)."""
    found, value = _from_deps(("get_forecaster", "get_demand_forecaster"), request)
    if found:
        return value
    found, value = _from_state(request, ("forecaster", "demand_forecaster"))
    if found:
        return value

    def _build() -> Any:
        cls = _import_attr("app.intelligence.forecast", ("DemandForecaster",))
        if cls is None:
            raise dependency_unavailable("DemandForecaster")
        return cls()

    return _cached(request, "_api_forecaster", _build)


def get_detector(request: Request) -> Any:
    """A5's `AnomalyDetector` (CONTRACT.md section 7.2)."""
    found, value = _from_deps(("get_detector", "get_anomaly_detector"), request)
    if found:
        return value
    found, value = _from_state(request, ("detector", "anomaly_detector"))
    if found:
        return value

    def _build() -> Any:
        cls = _import_attr("app.intelligence.detect", ("AnomalyDetector",))
        if cls is None:
            raise dependency_unavailable("AnomalyDetector")
        return cls()

    return _cached(request, "_api_detector", _build)


def get_allocator(request: Request) -> Any:
    """A6's `AllocationEngine` (CONTRACT.md section 7.3)."""
    found, value = _from_deps(("get_allocator", "get_allocation_engine", "get_decision_engine"), request)
    if found:
        return value
    found, value = _from_state(request, ("allocator", "allocation_engine", "decision_engine"))
    if found:
        return value

    def _build() -> Any:
        cls = _import_attr("app.intelligence.allocate", ("AllocationEngine",))
        if cls is None:
            raise dependency_unavailable("AllocationEngine")
        settings = None
        try:
            settings = get_settings()
        except HTTPException:
            settings = None
        policy = getattr(settings, "allocation_policy", None)
        try:
            return cls(policy=policy) if policy else cls()
        except TypeError:
            return cls()

    return _cached(request, "_api_allocator", _build)


# --- explanation service (A7) ----------------------------------------------


def get_explanation_service(request: Request) -> Any:
    """A7's `ExplanationService` (CONTRACT.md section 8.2)."""
    found, value = _from_deps(("get_explanation_service", "get_explainer", "get_explanation"), request)
    if found:
        return value
    found, value = _from_state(request, ("explanation_service", "explainer"))
    if found:
        return value

    def _build() -> Any:
        service_cls = _import_attr("app.llm.explain", ("ExplanationService",))
        client_cls = _import_attr("app.llm.deepseek", ("DeepSeekClient",))
        if service_cls is None or client_cls is None:
            raise dependency_unavailable("ExplanationService")
        client = client_cls(get_settings(), get_metrics(request))
        repository = get_repository(request)
        return service_cls(client, repository, get_metrics(request))

    return _cached(request, "_api_explanation_service", _build)


def get_deepseek_client(request: Request) -> Any:
    """A7's `DeepSeekClient` (CONTRACT.md section 8.1), for /status reporting."""
    found, value = _from_deps(("get_deepseek_client", "get_llm_client"), request)
    if found:
        return value
    found, value = _from_state(request, ("deepseek", "llm_client"))
    if found:
        return value

    def _build() -> Any:
        cls = _import_attr("app.llm.deepseek", ("DeepSeekClient",))
        if cls is None:
            raise dependency_unavailable("DeepSeekClient")
        return cls(get_settings(), get_metrics(request))

    return _cached(request, "_api_deepseek_client", _build)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

_ALLOW_EXTRA = ConfigDict(extra="allow")


class HealthResponse(BaseModel):
    """Liveness probe body — constant, dependency-free (CONTRACT.md 9)."""

    status: Literal["ok"] = "ok"
    service: str = "fuel-supply-intelligence"
    version: str = "1.0.0"
    simulated: bool = True


class ComponentStatus(BaseModel):
    status: Literal["ok", "degraded", "down"]
    detail: str | None = None
    model_config = _ALLOW_EXTRA


class StatusResponse(BaseModel):
    """Brief section 15's status page, component by component."""

    status: Literal["ok", "degraded", "down"]
    components: dict[str, ComponentStatus]
    p95_latency_ms: float | None = None
    error_rate: float | None = None
    requests_total: int | None = None
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class NetworkSnapshotResponse(BaseModel):
    tick: int | None = None
    sim_time: str | None = None
    status: str | None = None
    stale: bool = False
    age_seconds: float = 0.0
    instance: dict[str, Any] | None = None
    depots: list[dict[str, Any]] = Field(default_factory=list)
    stations: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    regions: list[dict[str, Any]] = Field(default_factory=list)
    supply_arrivals: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class DemandObservationOut(BaseModel):
    station_id: str | None = None
    fuel_type: str | None = None
    tick: int | None = None
    liters: float | None = None
    model_config = _ALLOW_EXTRA


class DemandHistoryResponse(BaseModel):
    station_id: str | None = None
    fuel_type: str | None = None
    count: int = 0
    observations: list[DemandObservationOut] = Field(default_factory=list)
    simulated: bool = True


class ForecastPointOut(BaseModel):
    tick: int
    liters: float
    lower: float
    upper: float
    model_config = _ALLOW_EXTRA


class StockoutRiskOut(BaseModel):
    probability: float
    ticks_to_stockout: float | None = None
    confidence: float
    basis: str
    model_config = _ALLOW_EXTRA


class StationForecastOut(BaseModel):
    station_id: str
    fuel_type: str
    method: str
    confidence: float
    fallback_used: bool
    points: list[ForecastPointOut] = Field(default_factory=list)
    stockout_risk: StockoutRiskOut | None = None
    inventory_liters: float | None = None
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class ForecastResponse(BaseModel):
    horizon_ticks: int
    generated_at_tick: int | None = None
    count: int = 0
    forecasts: list[StationForecastOut] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class RiskSignalOut(BaseModel):
    """A5's RiskSignal. Fields carry defaults so a partial peer payload degrades
    the display rather than failing the request (CONTRACT.md 5.2: tolerate a
    surprise value, never crash a request)."""

    kind: str = "unknown"
    severity: str = "info"
    entity_type: str = "unknown"
    entity_id: str = "unknown"
    detected_at_tick: int | None = None
    summary: str = ""
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class RiskResponse(BaseModel):
    count: int = 0
    generated_at_tick: int | None = None
    signals: list[RiskSignalOut] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class RecommendationOut(BaseModel):
    """A6's Recommendation (CONTRACT.md 7.3) — inspectable by construction."""

    id: str = ""
    station_id: str = ""
    depot_id: str = ""
    route_id: str = ""
    fuel_type: str = ""
    quantity_liters: float = 0.0
    rationale: str = ""
    constraints: list[str] = Field(default_factory=list)
    expected_impact: dict[str, float] = Field(default_factory=dict)
    alternatives: list[dict[str, Any]] = Field(default_factory=list)
    confidence: float = 0.0
    decision_id: int | None = None
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class RecommendationsResponse(BaseModel):
    count: int = 0
    policy: str | None = None
    generated_at_tick: int | None = None
    budget_liters: float | None = None
    recommendations: list[RecommendationOut] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class ExplanationResponse(BaseModel):
    """A7's Explanation (CONTRACT.md 8.2), flattened for the console."""

    text: str
    source: Literal["llm", "fallback"]
    model: str | None = None
    degraded: bool = False
    simulated: bool = True
    recommendation_id: str | None = None
    question: str | None = None
    signals_considered: int = 0
    model_config = _ALLOW_EXTRA


class SubmitRequest(BaseModel):
    """`confirm` is required and must be literally true (CONTRACT.md 9).

    `strict=True` matters: without it Pydantic v2 would coerce the string
    `"yes"` or the integer `1` into `True`, and the operator's explicit
    acknowledgement would stop being explicit.
    """

    confirm: bool = Field(
        default=False,
        strict=True,
        description="Explicit operator acknowledgement. Must be the boolean true; anything else is a 400.",
    )
    #: Guide section 5.1: `idempotency_key` is 1-150 characters on the wire.
    #: A longer key is a 422 from the simulator, so it is rejected here instead.
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=150)
    #: `allow_inf_nan=False` because `gt=0` alone accepts `inf`: a payload of
    #: `{"quantity_liters": 1e999}` passes the bound, is serialised as the
    #: non-standard JSON literal `Infinity`, and reaches the simulator as an
    #: unparseable body. Better to refuse it at the boundary, where the operator
    #: can see which field was wrong.
    quantity_liters: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    note: str | None = Field(default=None, max_length=1000)
    model_config = _ALLOW_EXTRA


class SubmitResponse(BaseModel):
    submitted: bool
    recommendation_id: str
    allocation: dict[str, Any] = Field(default_factory=dict)
    allocation_id: int | None = None
    decision_id: int | None = None
    note: str | None = None
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class DecisionOut(BaseModel):
    """A3's RecommendationRecord (shape not pinned by the contract):

    permissive on purpose so an extra field in A3's model cannot turn into a
    500 on the audit-history endpoint.
    """

    id: int | None = None
    model_config = _ALLOW_EXTRA


class DecisionsResponse(BaseModel):
    count: int = 0
    limit: int = 50
    offset: int = 0
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class DecisionDetailResponse(BaseModel):
    decision: dict[str, Any] = Field(default_factory=dict)
    outcome: dict[str, Any] | None = None
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class EventOut(BaseModel):
    id: int | None = None
    type: str | None = None
    start_tick: int | None = None
    end_tick: int | None = None
    status: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    model_config = _ALLOW_EXTRA


class EventsResponse(BaseModel):
    count: int = 0
    events: list[EventOut] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class SummaryRequest(BaseModel):
    """Body for POST /api/v1/events/summary. Whole body is optional."""

    focus: str | None = Field(default=None, max_length=300)
    model_config = _ALLOW_EXTRA


class InvestigateRequest(BaseModel):
    question: str = Field(..., min_length=3, max_length=1000)
    context: dict[str, Any] = Field(default_factory=dict)
    model_config = _ALLOW_EXTRA


class FaultIn(BaseModel):
    """Validated fault injection payload (demo of the resilience path)."""

    type: str = Field(..., min_length=1, max_length=100)
    model_config = _ALLOW_EXTRA


class FaultOut(BaseModel):
    model_config = _ALLOW_EXTRA


class AdminFaultsResponse(BaseModel):
    count: int = 0
    faults: list[dict[str, Any]] = Field(default_factory=list)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class AdminFaultCreatedResponse(BaseModel):
    fault: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


class SimulationActionResponse(BaseModel):
    action: Literal["run", "pause", "step", "reset"]
    result: dict[str, Any] = Field(default_factory=dict)
    simulated: bool = True
    model_config = _ALLOW_EXTRA


# ---------------------------------------------------------------------------
# Query parameter enums
# ---------------------------------------------------------------------------

FUEL_TYPES: tuple[str, ...] = ("DIESEL", "PETROL", "OCTANE")


def iter_public_settings(settings: Any) -> Iterator[tuple[str, Any]]:
    """Whitelist of settings that may appear in an API response.

    CONTRACT.md section 0.2 forbids returning a secret, and section 24 forbids
    exposing anything not deliberately public. Only these keys are ever read
    out of Settings by the API layer.
    """
    for key in ("deepseek_model", "llm_enabled", "simulator_base_url"):
        value = getattr(settings, key, None)
        if value is not None:
            yield key, value
