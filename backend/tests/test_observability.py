"""Tests for the A9 observability workstream — CONTRACT.md section 10.

Covered here, in the order the brief lists the concerns:

* every metric the contract names actually exists and moves (the *intelligence*
  layer included — it is the layer teams skip);
* the ``path`` label collapses ids into one series, and unmatched URLs cannot
  grow the registry without bound;
* no metric anywhere in the registry carries a per-entity label, and no value
  survives that is not in a declared domain;
* the DeepSeek key does not appear in a rendered log line, in a metric label, or
  in the ``/metrics`` body, even when it is passed in as a value under an
  innocuous key;
* ``install_observability`` is idempotent — same app twice, and two apps in one
  process, neither of which may raise ``Duplicated timeseries``;
* ``/metrics`` renders with the contract's metric names in it.

These tests use an app of their own rather than ``app.main``: they are testing
the observability module, and a failure here should point at this module rather
than at the wiring in A1's factory.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:  # pragma: no cover - import bootstrap
    sys.path.insert(0, str(_BACKEND_ROOT))

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability import logging as obs_logging
from app.observability import middleware as obs_middleware
from app.observability.logging import (
    REDACTED,
    configure_logging,
    log_decision,
    log_fallback,
    log_integration_failure,
    log_recovery,
    register_secret,
    register_settings_secrets,
    registered_secret_count,
)
from app.observability.metrics import (
    BOUNDED_LABEL_DOMAINS,
    DECLARED_METRIC_NAMES,
    FORBIDDEN_LABEL_NAMES,
    METRICS,
    REGISTRY,
    Metrics,
    ensure_system_collectors,
    get_metrics,
    label_surface,
    render_metrics,
)
from app.observability.middleware import ObservabilityMiddleware, normalise_path
from app.observability.setup import install_observability

#: A value shaped like a real key, used to prove it never surfaces anywhere.
FAKE_API_KEY = "sk-test-DO-NOT-LOG-0123456789abcdef"

#: Credential-shaped words that must never appear as a metric label *name*.
_CREDENTIAL_LABEL_RE = re.compile(
    r"(api[_-]?key|secret|token|password|passwd|authorization|credential|cookie)",
    re.IGNORECASE,
)

#: Every metric CONTRACT.md section 10 requires, by exposition name, grouped by
#: the brief's four layers. The system layer is provided by ProcessCollector.
CONTRACT_METRICS = {
    "application": (
        "http_requests_total",
        "http_request_duration_seconds",
    ),
    "system": (
        "process_cpu_seconds_total",
        "process_resident_memory_bytes",
    ),
    "intelligence": (
        "forecast_confidence",
        "forecast_fallback_total",
        "risk_signals_total",
        "recommendations_generated_total",
        "recommendation_confidence",
        "llm_calls_total",
        "llm_latency_seconds",
        "llm_fallback_total",
        "circuit_breaker_state",
    ),
}


class _FakeSettings:
    """Duck-typed stand-in for ``app.config.Settings``.

    Deliberately not the real class: these tests must not depend on A1's file,
    nor on environment variables, and a redaction test needs a key that is
    *definitely* fake.
    """

    def __init__(self, **overrides: object) -> None:
        self.simulator_base_url = "http://simulator-api:8000"
        self.log_level = "INFO"
        self.llm_enabled = True
        self.deepseek_api_key = FAKE_API_KEY
        self.__dict__.update(overrides)

    def model_dump(self) -> dict[str, object]:
        return dict(self.__dict__)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _add_routes(app: FastAPI) -> None:
    @app.get("/api/v1/decisions/{decision_id}")
    async def get_decision(decision_id: int) -> dict[str, object]:
        return {"decision_id": decision_id, "simulated": True}

    @app.get("/api/v1/network/snapshot")
    async def snapshot() -> dict[str, object]:
        return {"simulated": True}

    @app.get("/api/v1/boom")
    async def boom() -> dict[str, object]:
        raise RuntimeError("simulated engine failure")


def _make_app(settings: object | None = None, *, log_stream: io.StringIO | None = None,
              install: bool = True) -> FastAPI:
    """A tiny app with this workstream installed. Mirrors A1's factory order."""
    app = FastAPI()
    if settings is not None:
        app.state.settings = settings
    if install:
        install_observability(app)
    if log_stream is not None:
        # install_observability() configures logging for stdout; re-point it at
        # the buffer *after* installing, exactly as a test would have to.
        configure_logging("INFO", stream=log_stream)
    _add_routes(app)
    return app


def _counter(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _histogram(name: str, **labels: str) -> tuple[float, float]:
    return (
        REGISTRY.get_sample_value(f"{name}_count", labels) or 0.0,
        REGISTRY.get_sample_value(f"{name}_sum", labels) or 0.0,
    )


def _exercise_every_metric() -> None:
    """Drive one sample through every metric the contract lists.

    Without this a labelled family has no children and does not appear in the
    exposition at all, so "the metric exists" could not be asserted for it.
    """
    METRICS.observe_request("GET", "/api/v1/exercise", 200, 0.012)
    METRICS.observe_request("GET", "/api/v1/exercise", 500, 0.9)
    METRICS.request_started()
    METRICS.request_finished()
    METRICS.set_component_up("simulator", True)
    METRICS.record_forecast(0.82, False)
    METRICS.record_forecast(0.31, True)
    METRICS.record_forecast_error(120.0)
    METRICS.record_signal("demand_anomaly", "critical")
    METRICS.record_recommendation(0.77)
    METRICS.record_recommendation(0.44, policy="heuristic")
    METRICS.record_llm_call("explanation", True, 0.42, None)
    METRICS.record_llm_call("summary", False, 30.0, "timeout")
    METRICS.set_breaker("simulator", "open")


@pytest.fixture
def log_stream() -> io.StringIO:
    """Capture structlog's rendered output, and restore a sane config after."""
    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)
    yield buffer
    configure_logging("INFO")


@pytest.fixture
def client() -> TestClient:
    return TestClient(_make_app(_FakeSettings()))


# --------------------------------------------------------------------------- #
# Application layer
# --------------------------------------------------------------------------- #

def test_http_counter_and_histogram_record_a_request(client: TestClient) -> None:
    labels = {"method": "GET", "path": "/api/v1/decisions/{decision_id}", "status": "200"}
    before_count = _counter("http_requests_total", **labels)
    before_hist = _histogram("http_request_duration_seconds",
                             method="GET", path="/api/v1/decisions/{decision_id}")

    response = client.get("/api/v1/decisions/17")
    assert response.status_code == 200

    after_count = _counter("http_requests_total", **labels)
    after_hist = _histogram("http_request_duration_seconds",
                            method="GET", path="/api/v1/decisions/{decision_id}")

    assert after_count - before_count == 1
    assert after_hist[0] - before_hist[0] == 1  # _count
    assert after_hist[1] - before_hist[1] > 0.0  # _sum, a real duration


def test_path_label_collapses_identifier_series(client: TestClient) -> None:
    """The whole point: /17 and /18 must be one series, not two."""
    for decision_id in (17, 18, 19):
        assert client.get(f"/api/v1/decisions/{decision_id}").status_code == 200

    template = _counter("http_requests_total",
                        method="GET", path="/api/v1/decisions/{decision_id}", status="200")
    assert template >= 3

    for decision_id in (17, 18, 19):
        assert _counter("http_requests_total", method="GET",
                        path=f"/api/v1/decisions/{decision_id}", status="200") == 0.0

    paths = {
        sample.labels.get("path")
        for family in REGISTRY.collect()
        for sample in family.samples
        if sample.name == "http_requests_total"
        and str(sample.labels.get("path", "")).startswith("/api/v1/decisions")
    }
    assert paths == {"/api/v1/decisions/{decision_id}"}


def test_request_id_header_and_in_flight_gauge_settle(client: TestClient) -> None:
    response = client.get("/api/v1/network/snapshot")
    assert response.headers.get("x-request-id")
    # Balanced inc/dec: no request is still "in flight" once it has returned.
    assert _counter("http_requests_in_flight") == 0.0


def test_failed_request_records_500_and_logs_it(log_stream: io.StringIO) -> None:
    app = _make_app(_FakeSettings(), log_stream=log_stream)
    labels = {"method": "GET", "path": "/api/v1/boom", "status": "500"}
    before = _counter("http_requests_total", **labels)

    with TestClient(app, raise_server_exceptions=False) as failing:
        assert failing.get("/api/v1/boom").status_code == 500

    assert _counter("http_requests_total", **labels) - before == 1

    records = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    failures = [r for r in records if r.get("event") == "http_request_failed"]
    assert failures, "an unhandled failure must produce a log line"
    assert failures[-1]["status"] == 500
    assert failures[-1]["error_type"] == "RuntimeError"
    # The traceback is rendered, and the exception's own text is scrubbed.
    assert "exception" in failures[-1]


def test_metrics_endpoint_is_not_self_instrumented(client: TestClient) -> None:
    assert client.get("/metrics").status_code == 200
    assert client.get("/metrics").status_code == 200

    scrapes = {
        sample.labels.get("path")
        for family in REGISTRY.collect()
        for sample in family.samples
        if sample.name == "http_requests_total" and sample.labels.get("path") == "/metrics"
    }
    assert not scrapes, "scrape traffic must not pollute request rate or error rate"
    assert _counter("http_requests_in_flight") == 0.0


# --------------------------------------------------------------------------- #
# Derived readouts for /api/v1/status
# --------------------------------------------------------------------------- #

def test_status_snapshot_reports_p95_error_rate_and_availability(client: TestClient) -> None:
    client.get("/api/v1/decisions/1")
    client.get("/api/v1/network/snapshot")

    snapshot = METRICS.status_snapshot()
    assert snapshot["requests_total"] > 0
    assert snapshot["p95_latency_ms"] is not None
    assert 0.0 <= snapshot["error_rate"] <= 1.0
    assert 0.0 <= snapshot["availability"] <= 1.0
    assert METRICS.p95_latency() is not None
    assert METRICS.p95_latency_ms() == pytest.approx(METRICS.p95_latency() * 1000.0, rel=1e-3)


def test_error_rate_counts_server_errors_by_default() -> None:
    path = "/api/v1/error-rate-probe"
    METRICS.observe_request("GET", path, 200, 0.01)
    METRICS.observe_request("GET", path, 404, 0.01)
    assert _counter("http_requests_total", method="GET", path=path, status="200") == 1.0
    assert _counter("http_requests_total", method="GET", path=path, status="404") == 1.0

    # 404 is the client being wrong; it must not count against availability.
    assert METRICS.error_rate(include_client_errors=True) >= METRICS.error_rate()
    assert METRICS.availability() == pytest.approx(1.0 - METRICS.error_rate())


# --------------------------------------------------------------------------- #
# Intelligence layer -- the layer teams skip
# --------------------------------------------------------------------------- #

def test_intelligence_layer_metrics_all_move() -> None:
    before_fallback = _counter("forecast_fallback_total")
    before_conf, _ = _histogram("forecast_confidence")
    before_error, _ = _histogram("forecast_error_liters")
    before_signals = _counter("risk_signals_total", kind="demand_anomaly", severity="critical")
    before_recs = _counter("recommendations_generated_total")
    before_rec_conf, _ = _histogram("recommendation_confidence")
    before_policy = _counter("allocation_policy_total", policy="heuristic")
    before_llm_ok = _counter("llm_calls_total", purpose="explanation", result="ok")
    before_llm_lat, _ = _histogram("llm_latency_seconds")

    _exercise_every_metric()

    assert _counter("forecast_fallback_total") - before_fallback >= 1
    assert _histogram("forecast_confidence")[0] - before_conf >= 2
    assert _histogram("forecast_error_liters")[0] - before_error >= 1
    assert _counter("risk_signals_total", kind="demand_anomaly",
                    severity="critical") - before_signals >= 1
    assert _counter("recommendations_generated_total") - before_recs >= 2
    assert _histogram("recommendation_confidence")[0] - before_rec_conf >= 2
    assert _counter("allocation_policy_total", policy="heuristic") - before_policy >= 1
    assert _counter("llm_calls_total", purpose="explanation", result="ok") - before_llm_ok >= 1
    assert _histogram("llm_latency_seconds")[0] - before_llm_lat >= 2


def test_llm_fallback_is_recorded_separately_from_success() -> None:
    ok_before = _counter("llm_calls_total", purpose="investigation", result="ok")
    fallback_before = _counter("llm_calls_total", purpose="investigation", result="fallback")
    timeout_before = _counter("llm_fallback_total", reason="timeout")
    no_key_before = _counter("llm_fallback_total", reason="no_key")

    METRICS.record_llm_call("investigation", True, 0.5, None)
    METRICS.record_llm_call("investigation", False, 30.0, "timeout")
    # A missing key never makes a call at all, but it is still a fallback event.
    METRICS.record_llm_fallback("no_key")

    assert _counter("llm_calls_total", purpose="investigation", result="ok") - ok_before == 1
    assert _counter("llm_calls_total",
                    purpose="investigation", result="fallback") - fallback_before == 1
    assert _counter("llm_fallback_total", reason="timeout") - timeout_before == 1
    assert _counter("llm_fallback_total", reason="no_key") - no_key_before == 1


def test_circuit_breaker_state_is_published(client: TestClient) -> None:
    METRICS.set_breaker("simulator", "closed")
    assert REGISTRY.get_sample_value(
        "circuit_breaker_state", {"component": "simulator"}) == 0.0
    METRICS.set_breaker("simulator", "half_open")
    assert REGISTRY.get_sample_value(
        "circuit_breaker_state", {"component": "simulator"}) == 1.0
    METRICS.set_breaker("simulator", "open")
    assert REGISTRY.get_sample_value(
        "circuit_breaker_state", {"component": "simulator"}) == 2.0

    # An unknown state is visibly wrong rather than silently 'closed'.
    METRICS.set_breaker("simulator", "confused")
    assert REGISTRY.get_sample_value(
        "circuit_breaker_state", {"component": "simulator"}) == -1.0
    METRICS.set_breaker("simulator", "closed")


# --------------------------------------------------------------------------- #
# Cardinality
# --------------------------------------------------------------------------- #

def test_no_per_entity_labels_anywhere() -> None:
    """CONTRACT section 10: never label by station_id or allocation_id.

    Two assertions, deliberately different in scope:

    * **globally** — no metric in the registry, ours or a third party's, carries
      a per-entity label name. This is the rule that protects the registry.
    * **for our own metrics** — every label name is one with a declared, closed
      value domain, which is what makes "bounded" checkable rather than
      aspirational. Third-party collectors (``python_info`` and friends) have
      their own labels and are exempt from this second check.
    """
    _exercise_every_metric()

    surface = label_surface()
    assert surface, "label_surface() must not be vacuous"
    assert DECLARED_METRIC_NAMES <= set(surface), (
        f"declared metrics missing from the registry: {DECLARED_METRIC_NAMES - set(surface)}"
    )

    offenders: dict[str, set[str]] = {}
    undeclared: dict[str, set[str]] = {}
    for metric_name, labels in surface.items():
        forbidden = set(labels) & FORBIDDEN_LABEL_NAMES
        if forbidden:
            offenders[metric_name] = forbidden
        if metric_name in DECLARED_METRIC_NAMES:
            unknown = set(labels) - set(BOUNDED_LABEL_DOMAINS)
            if unknown:
                undeclared[metric_name] = unknown

    assert not offenders, f"per-entity (unbounded) labels found: {offenders}"
    assert not undeclared, f"labels without a declared bounded domain: {undeclared}"


def test_unknown_label_values_collapse_to_other() -> None:
    """A peer inventing a new severity string must not create a new series."""
    METRICS.record_signal("kind_that_does_not_exist", "apocalyptic")
    assert _counter("risk_signals_total", kind="other", severity="other") >= 1

    METRICS.record_llm_fallback("a_reason_nobody_defined")
    assert _counter("llm_fallback_total", reason="other") >= 1

    for index in range(500):
        METRICS.record_signal(f"invented_kind_{index}", "critical")

    kinds = {
        sample.labels.get("kind")
        for family in REGISTRY.collect()
        for sample in family.samples
        if sample.name == "risk_signals_total"
    }
    assert kinds, "risk_signals_total should have samples after recording"
    assert kinds <= BOUNDED_LABEL_DOMAINS["kind"], (
        f"an unbounded kind leaked into a label: {kinds - BOUNDED_LABEL_DOMAINS['kind']}"
    )
    assert len(kinds) <= len(BOUNDED_LABEL_DOMAINS["kind"])


def test_unknown_http_method_and_status_collapse() -> None:
    METRICS.observe_request("TRACEISH", "/api/v1/method-probe", 799, 0.001)
    # Both land in a series with a collapsed label, never a per-value one.
    assert _counter("http_requests_total",
                    method="other", path="/api/v1/method-probe", status="other") >= 1


# --------------------------------------------------------------------------- #
# Path normalisation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("raw", "template", "expected"),
    [
        ("/api/v1/decisions/17", None, "/api/v1/decisions/{id}"),
        ("/api/v1/decisions/180000", None, "/api/v1/decisions/{id}"),
        ("/api/v1/decisions/17",
         "/api/v1/decisions/{decision_id}", "/api/v1/decisions/{decision_id}"),
        # v1 is a version, not an identifier: it must survive.
        ("/api/v1/network/snapshot", None, "/api/v1/network/snapshot"),
        ("/api/v1/allocations/3/cancel", None, "/api/v1/allocations/{id}/cancel"),
        ("/api/v1/x/123e4567-e89b-12d3-a456-426614174000", None, "/api/v1/x/{id}"),
        ("/", None, "/"),
    ],
)
def test_normalise_path(raw: str, template: str | None, expected: str) -> None:
    assert normalise_path(raw, template) == expected


def test_unmatched_path_series_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unmatched-URL flood must not grow the registry without bound.

    The paths are alphabetical on purpose: ``/nowhere/1`` would be collapsed to
    ``/nowhere/{id}`` and share a series for free, which is a different
    mechanism and would not exercise the cap.
    """
    monkeypatch.setattr(obs_middleware, "_unmatched_paths", set())
    monkeypatch.setattr(obs_middleware, "_UNMATCHED_SERIES_LIMIT", 3)

    produced = {normalise_path(f"/nowhere/alpha{index}") for index in range(20)}
    assert produced == {
        "/nowhere/alpha0",
        "/nowhere/alpha1",
        "/nowhere/alpha2",
        "__unmatched__",
    }


# --------------------------------------------------------------------------- #
# Redaction -- brief section 18, CONTRACT sections 0.2 and 10
# --------------------------------------------------------------------------- #

def test_secret_never_surfaces_in_a_log_line(log_stream: io.StringIO) -> None:
    settings = _FakeSettings()
    app = _make_app(settings, log_stream=log_stream)
    assert registered_secret_count() >= 1, "the settings key must be registered"

    with TestClient(app, raise_server_exceptions=False) as running:
        running.get("/api/v1/decisions/17")
        running.get("/api/v1/boom")

    # ...and through the peer-facing helpers, under keys that do not look secret,
    # as a nested value, and inside an exception message.
    log_decision(decision_id=7, station_id="ST-1", note=f"used {FAKE_API_KEY}")
    log_fallback(reason="timeout", credentials={"nested": FAKE_API_KEY})
    log_integration_failure(error=RuntimeError(f"401 from upstream with {FAKE_API_KEY}"))
    log_recovery(detail=f"recovered without {FAKE_API_KEY}")
    obs_logging.get_logger("app").info("raw", body=f"Authorization: Bearer {FAKE_API_KEY}")

    rendered = log_stream.getvalue()
    assert rendered.strip(), "nothing was logged; the test would pass vacuously"
    assert FAKE_API_KEY not in rendered
    assert REDACTED in rendered

    # Every line is JSON, and the credential-shaped key never carries a value.
    for line in rendered.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        assert "event" in record
        assert json.dumps(record).find(FAKE_API_KEY) == -1


def test_redaction_caps_payload_size(log_stream: io.StringIO) -> None:
    """A full response body must not be loggable by accident."""
    huge = "x" * 50_000
    obs_logging.get_logger("app").info("payload", body=huge, items=list(range(500)))
    record = json.loads(log_stream.getvalue().strip().splitlines()[-1])

    assert len(record["body"]) < len(huge)
    assert "truncated" in record["body"]
    assert len(record["items"]) <= obs_logging.MAX_SEQUENCE_ITEMS + 1


def test_secret_never_surfaces_in_a_metric_label_or_the_exposition() -> None:
    settings = _FakeSettings()
    app = _make_app(settings)
    with TestClient(app) as client:
        # A credential pasted into a URL becomes a label candidate. It must not
        # become a label value.
        response = client.get(f"/api/v1/leaky/{FAKE_API_KEY}")
        assert response.status_code == 404
        body = client.get("/metrics").text

    assert FAKE_API_KEY not in body
    assert REDACTED in body

    leaked = [
        sample.labels
        for family in REGISTRY.collect()
        for sample in family.samples
        for value in sample.labels.values()
        if FAKE_API_KEY in str(value)
    ]
    assert not leaked


def test_metric_label_key_names_are_never_credential_shaped() -> None:
    """A label *name* must not be a credential either."""
    for labels in label_surface().values():
        for label in labels:
            assert not _CREDENTIAL_LABEL_RE.search(label), label


# --------------------------------------------------------------------------- #
# Installation
# --------------------------------------------------------------------------- #

def test_install_is_idempotent_for_one_app() -> None:
    app = _make_app(_FakeSettings())
    install_observability(app)   # twice more: must not raise
    install_observability(app)

    middlewares = [
        entry for entry in app.user_middleware
        if getattr(entry, "cls", None) is ObservabilityMiddleware
    ]
    assert len(middlewares) == 1, "a second middleware would double-count every request"

    labels = {"method": "GET", "path": "/api/v1/decisions/{decision_id}", "status": "200"}
    before = _counter("http_requests_total", **labels)
    with TestClient(app) as client:
        assert client.get("/api/v1/decisions/4").status_code == 200
        assert client.get("/metrics").status_code == 200
    assert _counter("http_requests_total", **labels) - before == 1


def test_install_is_idempotent_across_apps() -> None:
    """A test suite builds the app repeatedly; collectors are process-global."""
    apps = [_make_app(_FakeSettings()) for _ in range(3)]
    for app in apps:
        assert any(
            getattr(route, "path", None) == "/metrics" for route in app.routes
        )
    with TestClient(apps[-1]) as client:
        assert client.get("/metrics").status_code == 200


def test_install_without_settings_still_builds() -> None:
    """No settings object, no app.config: must still install and scrape."""
    app = FastAPI()
    install_observability(app)
    _add_routes(app)
    with TestClient(app) as client:
        assert client.get("/metrics").status_code == 200


def test_metrics_endpoint_renders_every_contract_metric() -> None:
    _exercise_every_metric()
    app = _make_app(_FakeSettings())
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text

    # `# TYPE <name> ` is an exact check: it cannot be satisfied by a longer
    # metric name that merely starts with the one we are looking for.
    missing = {
        layer: [name for name in names if f"# TYPE {name} " not in body]
        for layer, names in CONTRACT_METRICS.items()
    }
    assert not any(missing.values()), f"contract metrics missing from /metrics: {missing}"

    # The system layer really is populated, not just declared.
    assert re.search(r"^process_cpu_seconds_total \S+$", body, re.MULTILINE)
    assert re.search(r"^process_resident_memory_bytes \S+$", body, re.MULTILINE)

    # And the exposition is parseable as Prometheus text.
    assert "# TYPE http_requests_total counter" in body
    assert "# TYPE http_request_duration_seconds histogram" in body


def test_system_layer_is_actually_populated() -> None:
    """Brief section 14 'System: CPU, memory'. ProcessCollector is registered,
    and where it cannot read /proc the psutil fallback fills the same names.

    The trap this guards: ``Metric.name`` strips a trailing ``_total``, so a
    naive ``"process_cpu_seconds_total" in {m.name for m in registry.collect()}``
    is always false and the system layer looks empty when it is not.
    """
    source_is_psutil = ensure_system_collectors(REGISTRY)
    assert source_is_psutil in (True, False)

    body = render_metrics(REGISTRY).decode()
    assert "# TYPE process_cpu_seconds_total counter" in body
    assert "# TYPE process_resident_memory_bytes gauge" in body
    assert re.search(r"^process_cpu_seconds_total \S+$", body, re.MULTILINE)
    assert re.search(r"^process_resident_memory_bytes \S+$", body, re.MULTILINE)

    # Idempotent: a second registration must not raise Duplicated timeseries.
    assert ensure_system_collectors(REGISTRY) in (True, False)


def test_redactor_does_not_eat_non_credential_fields(log_stream: io.StringIO) -> None:
    """Why the install log says ``redaction_terms``, not ``secrets_registered``.

    A field *named* like a credential is redacted even when its value is a bare
    count. That is the redactor behaving correctly, so the field name is what
    gives way.
    """
    obs_logging.get_logger("app").info(
        "observability.installed",
        redaction_terms=3,
        secrets_registered=3,  # credential-shaped name: correctly redacted
    )
    record = json.loads(log_stream.getvalue().strip().splitlines()[-1])

    assert record["redaction_terms"] == 3
    assert record["secrets_registered"] == REDACTED


def test_register_settings_secrets_counts_only_new_terms() -> None:
    secret = "sk-count-probe-0123456789abcdef"
    before = registered_secret_count()

    assert register_settings_secrets(_FakeSettings(deepseek_api_key=secret)) == 1
    assert registered_secret_count() == before + 1
    # Registering the same key again learns nothing new.
    assert register_settings_secrets(_FakeSettings(deepseek_api_key=secret)) == 0


def test_render_metrics_defaults_to_the_endpoint_registry() -> None:
    """``render_metrics()`` with no argument must be the endpoint's body.

    The *values* legitimately differ between two calls — the system layer reads
    live process counters — so the metric surface is what is compared.
    """
    def type_lines(payload: bytes) -> set[str]:
        return {
            line for line in payload.decode().splitlines() if line.startswith("# TYPE ")
        }

    default_render = render_metrics()
    explicit_render = render_metrics(REGISTRY)

    assert type_lines(default_render), "the exposition must not be empty"
    assert type_lines(default_render) == type_lines(explicit_render)
    assert "process_resident_memory_bytes" in default_render.decode()


# --------------------------------------------------------------------------- #
# Log helpers
# --------------------------------------------------------------------------- #

def test_log_helpers_emit_one_json_line_each(log_stream: io.StringIO) -> None:
    log_decision(decision_id=1, confidence=0.9)
    log_fallback(reason="circuit_open", component="simulator")
    log_recovery(component="simulator", downtime_ms=2100)
    log_integration_failure(error="connection reset", component="simulator")

    records = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    events = [record["event"] for record in records]
    assert events == ["decision", "fallback_activated", "recovery", "integration_failure"]
    assert all("timestamp" in record and "level" in record for record in records)
    assert records[1]["level"] == "warning"
    assert records[3]["level"] == "error"


def test_peer_logger_module_is_configured_for_json(log_stream: io.StringIO) -> None:
    """A peer's own structlog logger gets the pipeline, redaction included."""
    peer = obs_logging.get_logger("app.sim")
    peer.info("sim.fetch_failed", url="http://x/v1/depots", api_key=FAKE_API_KEY)
    record = json.loads(log_stream.getvalue().strip().splitlines()[-1])

    assert record["event"] == "sim.fetch_failed"
    assert record["api_key"] == REDACTED
    assert FAKE_API_KEY not in log_stream.getvalue()


def test_register_secret_ignores_short_and_non_string_values() -> None:
    before = registered_secret_count()
    register_secret("abc")          # too short to be a key
    register_secret("")
    register_secret(None)           # type: ignore[arg-type]
    register_secret(12345)          # type: ignore[arg-type]
    assert registered_secret_count() == before


# --------------------------------------------------------------------------- #
# Metrics facade shape -- CONTRACT section 10 signatures
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "method_name",
    [
        "observe_request",
        "record_forecast",
        "record_signal",
        "record_recommendation",
        "record_llm_call",
        "set_breaker",
    ],
)
def test_contract_methods_exist_and_accept_the_contract_arguments(method_name: str) -> None:
    metrics = Metrics()
    calls = {
        "observe_request": ("GET", "/api/v1/x", 200, 0.01),
        "record_forecast": (0.5, False),
        "record_signal": ("inventory_drop", "warning"),
        "record_recommendation": (0.6,),
        "record_llm_call": ("explanation", True, 0.2, None),
        "set_breaker": ("simulator", "closed"),
    }
    getattr(metrics, method_name)(*calls[method_name])  # must not raise


def test_metrics_instances_share_the_same_series() -> None:
    """Two constructions must not re-register, and must observe the same data."""
    assert get_metrics() is METRICS
    assert isinstance(Metrics(), Metrics)
    before = _counter("recommendations_generated_total")
    Metrics().record_recommendation(0.5)
    assert _counter("recommendations_generated_total") - before == 1
    assert METRICS.registry is REGISTRY
