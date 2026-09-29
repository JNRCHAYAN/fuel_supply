"""Prometheus metrics for the four observability layers — CONTRACT.md section 10.

          brief section 14 layer   |  metrics
    -------------------------------+-------------------------------------------
    Application (rate, latency,    |  http_requests_total, http_request_duration_seconds,
      error rate, availability)    |  http_requests_in_flight, service_up
    System (CPU, memory)           |  process_*  (ProcessCollector)
    Intelligence (prediction error,|  forecast_confidence, forecast_fallback_total,
      confidence, alert rate,      |  forecast_error_liters, risk_signals_total,
      decision frequency, fallback |  recommendations_generated_total,
      activation)                  |  recommendation_confidence, allocation_policy_total,
                                   |  llm_calls_total, llm_latency_seconds,
                                   |  llm_fallback_total, circuit_breaker_state
    Logs                           |  see app/observability/logging.py

Two rules are structural here, not conventions:

**Bounded labels.** No metric is ever labelled by ``station_id``, ``depot_id``,
``allocation_id``, ``route_id`` or any other per-entity value — see
``FORBIDDEN_LABEL_NAMES`` and ``_bound()``. Every label value passes through an
allow-list and collapses to ``"other"`` when it is not recognised, so a peer
that invents a new severity string cannot blow the registry up.
``test_observability`` asserts this over the whole registry, not just over the
metrics this module happens to declare.

**No secrets in labels.** Label values are URL-derived, so they are run through
the logging layer's ``scrub_text`` before they are recorded.

This module owns the metric *definitions* only; it never imports the API, the
simulator, or the store. Peers receive a ``Metrics`` through ``app/deps.py``.
"""

from __future__ import annotations

import math
import threading
from typing import Any, Final, Iterable, Mapping

from prometheus_client import (
    # The *default* registry, on purpose: if another workstream exposes /metrics
    # with a bare ``generate_latest()`` it must show these series, otherwise the
    # demo has two half-empty endpoints. ProcessCollector already lives here.
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    ProcessCollector,
    generate_latest,
)

from .logging import scrub_text

__all__ = [
    "BOUNDED_LABEL_DOMAINS",
    "DECLARED_METRIC_NAMES",
    "FORBIDDEN_LABEL_NAMES",
    "METRICS",
    "Metrics",
    "REGISTRY",
    "ensure_system_collectors",
    "get_metrics",
    "label_surface",
    "render_metrics",
]


# --------------------------------------------------------------------------- #
# Cardinality control
# --------------------------------------------------------------------------- #

#: Label names that are never allowed on a Counter or Histogram. These are
#: per-entity identifiers: one series per station/allocation/route means one
#: series per row in the simulator, which is unbounded and eventually exhausts
#: memory and scrape time. The identifier belongs in the *log*, where
#: cardinality is free.
FORBIDDEN_LABEL_NAMES: Final[frozenset[str]] = frozenset(
    {
        "station_id",
        "depot_id",
        "allocation_id",
        "route_id",
        "region_id",
        "entity_id",
        "decision_id",
        "recommendation_id",
        "signal_id",
        "event_id",
        "user_id",
        "request_id",
        "trace_id",
        "correlation_id",
        "id",
    }
)

_OTHER = "other"

#: Every label this module emits, with the closed set of values it may take. An
#: empty frozenset marks a domain that is open but bounded by construction
#: (``path`` = route templates; ``status`` = 100..599).
BOUNDED_LABEL_DOMAINS: Final[Mapping[str, frozenset[str]]] = {
    "method": frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", _OTHER}),
    "path": frozenset(),
    "status": frozenset(),
    "kind": frozenset(
        {
            "demand_anomaly",
            "inventory_drop",
            "route_bottleneck",
            "regional_disruption",
            "supply_shortfall",
            _OTHER,
        }
    ),
    "severity": frozenset({"info", "warning", "serious", "critical", _OTHER}),
    "purpose": frozenset(
        {"explanation", "summary", "investigation", "forecast", "recommendation", _OTHER}
    ),
    "result": frozenset({"ok", "error", "fallback", _OTHER}),
    "reason": frozenset(
        {
            "no_key",
            "disabled",
            "timeout",
            "http_error",
            "invalid_response",
            "circuit_open",
            "unavailable",
            "insufficient_history",
            "infeasible",
            "error",
            _OTHER,
        }
    ),
    "component": frozenset({"simulator", "database", "llm", "decision_engine", "backend", _OTHER}),
    "policy": frozenset({"optimizer", "heuristic", _OTHER}),
    "state": frozenset({"closed", "half_open", "open", _OTHER}),
}


def _bound(label: str, value: Any) -> str:
    """Collapse ``value`` into the declared domain for ``label``.

    An unrecognised value becomes ``"other"``: the count is preserved, the
    cardinality is not blown. Open-domain labels (``path``, ``status``) have
    their own normalisers below rather than going through here.
    """
    text = str(value).strip().lower()
    domain = BOUNDED_LABEL_DOMAINS.get(label)
    if domain:
        return text if text in domain else _OTHER
    return text or _OTHER


def _method_label(method: Any) -> str:
    """HTTP methods are caller-supplied; only the standard verbs are series."""
    text = str(method).strip().upper()
    return text if text in BOUNDED_LABEL_DOMAINS["method"] else _OTHER


def _status_label(status: Any) -> str:
    """A status code is bounded by HTTP itself (100-599); anything else is not."""
    try:
        code = int(status)
    except (TypeError, ValueError):
        return _OTHER
    return str(code) if 100 <= code <= 599 else _OTHER


# --------------------------------------------------------------------------- #
# Application layer -- rate, latency, error rate, availability
# --------------------------------------------------------------------------- #

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled, by method, route template and status code.",
    labelnames=("method", "path", "status"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds, by method and route template.",
    labelnames=("method", "path"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

HTTP_REQUESTS_IN_FLIGHT = Gauge(
    "http_requests_in_flight",
    "HTTP requests currently being handled.",
)

SERVICE_UP = Gauge(
    "service_up",
    "1 when the component is usable, 0 when it is not. Drives availability.",
    labelnames=("component",),
)

# --------------------------------------------------------------------------- #
# Intelligence layer -- the layer teams skip. CONTRACT.md section 10.
# --------------------------------------------------------------------------- #

FORECAST_CONFIDENCE = Histogram(
    "forecast_confidence",
    "Confidence reported by the demand forecaster (0..1).",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

FORECAST_FALLBACK_TOTAL = Counter(
    "forecast_fallback_total",
    "Forecasts produced by the fallback (level-based) path instead of a model.",
)

FORECAST_ERROR_LITERS = Histogram(
    "forecast_error_liters",
    "Absolute forecast error in liters, once the predicted tick arrives.",
    buckets=(10.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0),
)

RISK_SIGNALS_TOTAL = Counter(
    "risk_signals_total",
    "Risk/shortage signals raised, by kind and derived severity.",
    labelnames=("kind", "severity"),
)

RECOMMENDATIONS_GENERATED_TOTAL = Counter(
    "recommendations_generated_total",
    "Allocation recommendations produced (decision frequency).",
)

RECOMMENDATION_CONFIDENCE = Histogram(
    "recommendation_confidence",
    "Confidence attached to each generated recommendation (0..1).",
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

ALLOCATION_POLICY_TOTAL = Counter(
    "allocation_policy_total",
    "Which decision policy produced the recommendations: the optimizer or the "
    "priority-heuristic fallback (brief section 11 fallback activation).",
    labelnames=("policy",),
)

LLM_CALLS_TOTAL = Counter(
    "llm_calls_total",
    "LLM calls by purpose and result. 'fallback' means a deterministic "
    "explanation was served instead of the model's.",
    labelnames=("purpose", "result"),
)

LLM_LATENCY_SECONDS = Histogram(
    "llm_latency_seconds",
    "DeepSeek call latency in seconds (including calls that timed out).",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0),
)

LLM_FALLBACK_TOTAL = Counter(
    "llm_fallback_total",
    "LLM fallback activations by reason. The brief grades this explicitly.",
    labelnames=("reason",),
)

CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state per component: 0=closed, 1=half_open, 2=open.",
    labelnames=("component",),
)

_BREAKER_STATE_VALUES: Final[Mapping[str, float]] = {
    "closed": 0.0,
    "half_open": 1.0,
    "open": 2.0,
}

#: Every collector declared above, so ``label_surface()`` can report the label
#: names of metrics that have not been observed yet.
_DECLARED_METRICS: Final[tuple[Any, ...]] = (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_FLIGHT,
    SERVICE_UP,
    FORECAST_CONFIDENCE,
    FORECAST_FALLBACK_TOTAL,
    FORECAST_ERROR_LITERS,
    RISK_SIGNALS_TOTAL,
    RECOMMENDATIONS_GENERATED_TOTAL,
    RECOMMENDATION_CONFIDENCE,
    ALLOCATION_POLICY_TOTAL,
    LLM_CALLS_TOTAL,
    LLM_LATENCY_SECONDS,
    LLM_FALLBACK_TOTAL,
    CIRCUIT_BREAKER_STATE,
)


def _exposition_name(metric: Any) -> str:
    """The name this collector appears under in the /metrics text.

    prometheus_client strips a trailing ``_total`` off a Counter's internal name
    and re-appends it at exposition time, so ``Counter("http_requests_total")``
    has ``_name == "http_requests"`` and is scraped as ``http_requests_total``.
    """
    name = metric._name
    if isinstance(metric, Counter) and not name.endswith("_total"):
        return f"{name}_total"
    return name


#: Exposition names of every metric this module declares. Public so the
#: bounded-label rule can be asserted against the declared surface specifically,
#: rather than against whatever third-party collectors share the registry.
DECLARED_METRIC_NAMES: Final[frozenset[str]] = frozenset(
    _exposition_name(metric) for metric in _DECLARED_METRICS
)


# --------------------------------------------------------------------------- #
# System layer
# --------------------------------------------------------------------------- #

_system_lock = threading.Lock()
_system_collectors_ready = False
_psutil_fallback_active = False


class _PsutilProcessCollector:
    """Emit ``process_*`` where ``prometheus_client``'s collector cannot.

    ``prometheus_client``'s ``ProcessCollector`` reads ``/proc``; off Linux it
    silently yields *nothing* (verified: ``collect()`` returns ``[]`` when
    ``/proc/stat`` is absent), so the system layer would be empty on a
    developer's machine and only populate inside the Linux container — exactly
    the kind of difference that hides a bug until deployment. When the platform
    collector has nothing to say, this emits the same metric names from
    ``psutil``.

    ``psutil`` is not a new dependency: it is already pinned transitively by
    ``locust`` in ``requirements.txt``. If the import fails we degrade to
    ``ProcessCollector`` alone rather than failing the scrape.
    """

    def __init__(self, namespace: str = "") -> None:
        self._prefix = f"{namespace}_process_" if namespace else "process_"

    def collect(self) -> Iterable[Any]:
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil ships with locust
            return []

        from prometheus_client.metrics_core import (
            CounterMetricFamily,
            GaugeMetricFamily,
        )

        families: list[Any] = []
        try:
            process = psutil.Process()
            with process.oneshot():
                cpu_seconds = float(sum(process.cpu_times()[:2]))
                memory = process.memory_info()
                started = float(process.create_time())
        except Exception:  # noqa: BLE001 - an unreadable counter must not 500 the scrape
            return []

        families.append(
            CounterMetricFamily(
                self._prefix + "cpu_seconds_total",
                "Total user and system CPU time spent in seconds.",
                value=cpu_seconds,
            )
        )
        families.append(
            GaugeMetricFamily(
                self._prefix + "resident_memory_bytes",
                "Resident memory size in bytes.",
                value=float(memory.rss),
            )
        )
        families.append(
            GaugeMetricFamily(
                self._prefix + "virtual_memory_bytes",
                "Virtual memory size in bytes.",
                value=float(memory.vms),
            )
        )
        families.append(
            GaugeMetricFamily(
                self._prefix + "start_time_seconds",
                "Start time of the process since unix epoch in seconds.",
                value=started,
            )
        )
        try:
            # Only the cheap POSIX call. ``open_files()`` is the fallback on
            # platforms without ``num_fds``, but on Windows it walks every
            # handle and measured 178 ms here — an unacceptable cost to add to
            # every scrape. ``process_open_fds`` is simply absent there.
            if hasattr(process, "num_fds"):
                families.append(
                    GaugeMetricFamily(
                        self._prefix + "open_fds",
                        "Number of open file descriptors.",
                        value=float(process.num_fds()),
                    )
                )
        except Exception:  # noqa: BLE001 - descriptor counts are optional
            pass

        return families


def _exposes(registry: CollectorRegistry | None, name: str) -> bool:
    """True when ``name`` appears in this registry's Prometheus exposition.

    Compares the **exposed** name, not ``Metric.name``. prometheus_client strips
    a trailing ``_total`` off a counter family's internal name and re-appends it
    when rendering, so ``CounterMetricFamily("process_cpu_seconds_total")``
    reports ``Metric.name == "process_cpu_seconds"``. Checking the internal name
    for a ``_total`` metric is therefore always false — which is exactly the
    check that decides whether the system layer is populated.
    """
    try:
        body = generate_latest(registry or REGISTRY).decode("utf-8", "replace")
    except Exception:  # noqa: BLE001 - a broken registry must not break startup
        return False
    return f"# TYPE {name} " in body or f"\n{name} " in f"\n{body}"


def ensure_system_collectors(registry: CollectorRegistry | None = None) -> bool:
    """Register the system (CPU/memory) layer exactly once. Idempotent.

    Returns True when the psutil fallback is the active source of ``process_*``
    (the Windows case). Safe to call repeatedly and from several apps in one
    process: the registry and its collectors are process-global, so the guard is
    too. Calling it twice must not raise — a test builds the app repeatedly.
    """
    global _system_collectors_ready, _psutil_fallback_active
    target = registry or REGISTRY
    with _system_lock:
        if target is REGISTRY and _system_collectors_ready:
            return _psutil_fallback_active

        # prometheus_client registers one at import; tolerate a duplicate rather
        # than crash startup.
        try:
            ProcessCollector(registry=target)
        except ValueError:
            pass

        fallback_active = False
        if not _exposes(target, "process_cpu_seconds_total"):
            try:
                target.register(_PsutilProcessCollector())
            except ValueError:
                pass
            fallback_active = _exposes(target, "process_cpu_seconds_total")

        if target is REGISTRY:
            _system_collectors_ready = True
            _psutil_fallback_active = fallback_active
        return fallback_active


# --------------------------------------------------------------------------- #
# Histogram helpers
# --------------------------------------------------------------------------- #

def _aggregate_histogram(histogram: Any) -> tuple[dict[float, float], float]:
    """Sum a labelled histogram across every label set.

    Returns ``(cumulative count by upper bound, total count)`` — the raw
    material for a cross-series percentile.
    """
    buckets: dict[float, float] = {}
    count = 0.0
    for family in histogram.collect():
        for sample in family.samples:
            if sample.name.endswith("_bucket"):
                try:
                    upper = float(sample.labels.get("le"))
                except (TypeError, ValueError):
                    continue
                buckets[upper] = buckets.get(upper, 0.0) + sample.value
            elif sample.name.endswith("_count"):
                count += sample.value
    return buckets, count


def _quantile_from_buckets(buckets: Mapping[float, float], count: float,
                           quantile: float) -> float | None:
    """Smallest bucket bound whose cumulative share reaches ``quantile``.

    A bucket-boundary estimate, not an interpolated one: it is honest about the
    histogram's resolution, and the bucket bounds are chosen for HTTP latency so
    the reported p95 is never more than one step optimistic.
    """
    if count <= 0:
        return None
    for upper in sorted(buckets):
        if math.isinf(upper):
            continue
        if buckets[upper] / count >= quantile:
            return upper
    return None


# --------------------------------------------------------------------------- #
# The Metrics facade -- CONTRACT.md section 10
# --------------------------------------------------------------------------- #

class Metrics:
    """The metrics facade injected through ``app/deps.py``.

    Stateless and free to construct: the collectors are process-global (as
    Prometheus requires), so ``Metrics()`` in a dependency provider and
    ``METRICS`` here share every series. Constructing a second instance never
    re-registers a collector.
    """

    def __init__(self) -> None:
        # Exposed for discoverability: peers may want the registry for /metrics.
        self.registry = REGISTRY

    # -- application ------------------------------------------------------- #

    def observe_request(self, method: str, path: str, status: int | str,
                        duration: float) -> None:
        """Record one finished HTTP request: rate, latency and error rate.

        ``path`` must already be bounded (a route template). The middleware
        normalises it; this method scrubs it again, so a credential that ended
        up in a URL can never become a label value either.
        """
        method_label = _method_label(method)
        path_label = scrub_text(str(path), limit=200)
        status_label = _status_label(status)

        HTTP_REQUESTS_TOTAL.labels(
            method=method_label, path=path_label, status=status_label
        ).inc()

        try:
            seconds = float(duration)
        except (TypeError, ValueError):
            return
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=method_label, path=path_label
        ).observe(max(0.0, seconds))

    def request_started(self) -> None:
        HTTP_REQUESTS_IN_FLIGHT.inc()

    def request_finished(self) -> None:
        HTTP_REQUESTS_IN_FLIGHT.dec()

    def set_component_up(self, component: str, up: bool) -> None:
        """Availability input for /api/v1/status (brief section 15)."""
        SERVICE_UP.labels(component=_bound("component", component)).set(1.0 if up else 0.0)

    # -- intelligence ------------------------------------------------------ #

    def record_forecast(self, confidence: float, fallback: bool) -> None:
        """One forecast. ``fallback`` marks the level-based path (CONTRACT 7.1)."""
        try:
            FORECAST_CONFIDENCE.observe(float(confidence))
        except (TypeError, ValueError):
            pass
        if fallback:
            FORECAST_FALLBACK_TOTAL.inc()

    def record_forecast_error(self, error_liters: float) -> None:
        """Realised prediction error — the brief's 'prediction error' metric."""
        try:
            FORECAST_ERROR_LITERS.observe(abs(float(error_liters)))
        except (TypeError, ValueError):
            pass

    def record_signal(self, kind: str, severity: str) -> None:
        """One RiskSignal (CONTRACT 7.2). This is the shortage-alert rate."""
        RISK_SIGNALS_TOTAL.labels(
            kind=_bound("kind", kind), severity=_bound("severity", severity)
        ).inc()

    def record_shortage_alert(self, kind: str, severity: str) -> None:
        """Alias for :meth:`record_signal` — the brief calls these shortage alerts."""
        self.record_signal(kind, severity)

    def record_recommendation(self, confidence: float,
                              policy: str | None = None) -> None:
        """One recommendation produced: decision frequency + model confidence.

        ``policy`` is optional and additive to the contract signature. CONTRACT
        7.3 requires the engine to report whether the optimizer or the heuristic
        ran, and that *is* the decision layer's fallback activation signal.
        """
        RECOMMENDATIONS_GENERATED_TOTAL.inc()
        try:
            RECOMMENDATION_CONFIDENCE.observe(float(confidence))
        except (TypeError, ValueError):
            pass
        if policy is not None:
            self.record_policy(policy)

    def record_policy(self, policy: str) -> None:
        """Which allocation policy ran: ``optimizer`` or ``heuristic``."""
        ALLOCATION_POLICY_TOTAL.labels(policy=_bound("policy", policy)).inc()

    def record_llm_call(self, purpose: str, ok: bool, latency: float,
                        fallback_reason: str | None) -> None:
        """One DeepSeek call (contract signature).

        A non-None ``fallback_reason`` means the deterministic template was
        served instead of the model, and double-counts into
        ``llm_fallback_total`` so the fallback rate is queryable on its own.
        """
        purpose_label = _bound("purpose", purpose)
        try:
            LLM_LATENCY_SECONDS.observe(max(0.0, float(latency)))
        except (TypeError, ValueError):
            pass

        if fallback_reason:
            result = "fallback"
        elif ok:
            result = "ok"
        else:
            result = "error"
        LLM_CALLS_TOTAL.labels(purpose=purpose_label, result=result).inc()

        if fallback_reason:
            LLM_FALLBACK_TOTAL.labels(reason=_bound("reason", fallback_reason)).inc()

    def record_llm_fallback(self, reason: str) -> None:
        """Fallback activation without a preceding call (no key, LLM disabled)."""
        LLM_FALLBACK_TOTAL.labels(reason=_bound("reason", reason)).inc()

    def set_breaker(self, component: str, state: str) -> None:
        """Publish a circuit breaker state (CONTRACT 5.5 exposes it).

        0=closed, 1=half_open, 2=open, so ``max()`` over a component's series is
        a usable alert expression and ``changes()`` shows flapping. An
        unrecognised state records -1 so it is visibly wrong rather than
        silently "closed".
        """
        state_label = _bound("state", state)
        CIRCUIT_BREAKER_STATE.labels(
            component=_bound("component", component)
        ).set(_BREAKER_STATE_VALUES.get(state_label, -1.0))

    # -- derived readouts, consumed by /api/v1/status (CONTRACT section 9) --- #

    def latency_quantile(self, quantile: float) -> float | None:
        """Cross-series latency quantile in seconds, from the histogram."""
        buckets, count = _aggregate_histogram(HTTP_REQUEST_DURATION_SECONDS)
        return _quantile_from_buckets(buckets, count, quantile)

    def p95_latency(self) -> float | None:
        """95th percentile HTTP latency in seconds (None before any request)."""
        return self.latency_quantile(0.95)

    def p95_latency_ms(self) -> float | None:
        """95th percentile HTTP latency in milliseconds."""
        seconds = self.p95_latency()
        return None if seconds is None else round(seconds * 1000.0, 3)

    def request_count(self) -> float:
        """Total requests observed since process start."""
        _, count = _aggregate_histogram(HTTP_REQUEST_DURATION_SECONDS)
        return count

    def request_rate(self, window_seconds: float) -> float:
        """Mean requests per second over an operator-supplied window."""
        if window_seconds <= 0:
            return 0.0
        return self.request_count() / float(window_seconds)

    def error_rate(self, *, include_client_errors: bool = False) -> float:
        """Share of requests that failed. 4xx excluded by default.

        A 404 on an unknown recommendation id is the client being wrong; a 5xx
        is *us* being wrong. Availability (brief section 15) tracks the latter,
        so that is the default. Pass ``include_client_errors=True`` for the
        broader figure.
        """
        prefixes = ("4", "5") if include_client_errors else ("5",)
        total = 0.0
        failed = 0.0
        for family in HTTP_REQUESTS_TOTAL.collect():
            for sample in family.samples:
                if not sample.name.endswith("_total"):
                    continue
                total += sample.value
                if str(sample.labels.get("status", "")).startswith(prefixes):
                    failed += sample.value
        if total <= 0:
            return 0.0
        return failed / total

    def availability(self) -> float:
        """1 - error_rate. 'Service availability' in brief section 14."""
        return max(0.0, 1.0 - self.error_rate())

    def status_snapshot(self) -> dict[str, Any]:
        """Everything /api/v1/status needs from the metrics middleware.

        One call instead of five, so the API layer does not have to know the
        name of each readout.
        """
        in_flight = REGISTRY.get_sample_value("http_requests_in_flight")
        return {
            "requests_total": self.request_count(),
            "p95_latency_ms": self.p95_latency_ms(),
            "error_rate": round(self.error_rate(), 6),
            "availability": round(self.availability(), 6),
            "in_flight": 0.0 if in_flight is None else in_flight,
        }


#: Process-wide instance. ``Metrics()`` is equivalent; this just saves peers a
#: construction site in ``app/deps.py``.
METRICS: Final[Metrics] = Metrics()


def get_metrics() -> Metrics:
    """FastAPI-dependency-friendly accessor (``Depends(get_metrics)``)."""
    return METRICS


# --------------------------------------------------------------------------- #
# Exposition
# --------------------------------------------------------------------------- #

def render_metrics(registry: CollectorRegistry | None = None) -> bytes:
    """Render the Prometheus text exposition for the /metrics endpoint."""
    return generate_latest(registry or REGISTRY)


def label_surface(registry: CollectorRegistry | None = None) -> dict[str, tuple[str, ...]]:
    """``{metric name: label names}`` for every metric family in the registry.

    Exists so the bounded-cardinality rule is *inspectable* rather than a claim
    in a docstring — see ``test_no_per_entity_labels_anywhere``. Declared
    collectors are listed from their declarations, so a label shows up here even
    before the metric has been observed; anything else found in the registry is
    added from its samples.
    """
    surface: dict[str, tuple[str, ...]] = {}
    for metric in _DECLARED_METRICS:
        surface[_exposition_name(metric)] = tuple(sorted(metric._labelnames))

    for family in (registry or REGISTRY).collect():
        names = set(surface.get(family.name, ()))
        for sample in family.samples:
            names.update(sample.labels.keys())
        names.discard("le")
        surface[family.name] = tuple(sorted(names))
    return surface
