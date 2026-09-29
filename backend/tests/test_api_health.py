"""`/api/v1/health`, `/api/v1/status` and `/metrics`.

CONTRACT.md section 9 and brief section 15. The load-bearing assertions here
are: health needs no dependencies at all, and a missing LLM key is reported as
`degraded`, never `down`.
"""

from __future__ import annotations

from types import SimpleNamespace

import test_api_support  # noqa: F401
from test_api_support import (
    SECRET_SENTINEL,
    FakeMetrics,
    FakeSimulatorClient,
    build_app,
    build_bare_app,
    client,
    fake_settings,
)

from app.api.health import _p95_from_buckets, prometheus_stats
from app.api.router import api_router

CONTRACT_COMPONENTS = {"simulator", "database", "llm", "decision_engine"}


def _ping_returning(value: bool):
    """A stand-in for ``Database.ping``: async, and reports by return value."""

    async def ping() -> bool:
        return value

    return ping


# ---------------------------------------------------------------------------
# /api/v1/health
# ---------------------------------------------------------------------------


def test_health_needs_no_dependencies() -> None:
    """Liveness: works on an app with zero overrides and no lifespan state."""
    response = client(build_bare_app()).get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["simulated"] is True


def test_health_is_unaffected_by_a_dead_simulator_and_database() -> None:
    """A liveness probe that fails when a dependency is down is a readiness
    probe, and would restart a perfectly healthy container."""
    broken = FakeSimulatorClient()
    broken.fail_on = {
        "get_health",
        "get_instance",
        "get_depots",
        "get_stations",
        "get_routes",
        "get_regions",
        "get_metrics",
        "get_events",
        "get_supply_arrivals",
        "get_demand_history",
    }
    app = build_app(client=broken)
    response = client(app).get("/api/v1/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# /api/v1/status
# ---------------------------------------------------------------------------


def test_status_reports_every_component_separately() -> None:
    body = client(build_app()).get("/api/v1/status").json()
    assert CONTRACT_COMPONENTS <= set(body["components"])
    assert body["components"]["api"]["status"] == "ok"
    assert body["components"]["simulator"]["status"] == "ok"
    assert body["components"]["database"]["status"] == "ok"
    assert body["components"]["decision_engine"]["status"] == "ok"


def test_status_carries_p95_latency_and_error_rate() -> None:
    body = client(build_app()).get("/api/v1/status").json()
    assert "p95_latency_ms" in body
    assert "error_rate" in body
    assert "requests_total" in body


def test_status_reports_llm_degaded_when_no_key_is_configured() -> None:
    """A missing key is a supported mode (CONTRACT.md 4 and 9), not a failure."""
    app = build_app(settings=fake_settings(llm_available=False))
    body = client(app).get("/api/v1/status").json()
    assert body["components"]["llm"]["status"] == "degraded"
    assert body["components"]["llm"]["status"] != "down"
    assert "fallback" in body["components"]["llm"]["detail"]


def test_status_reports_llm_ok_when_the_key_is_present() -> None:
    app = build_app(
        settings=fake_settings(llm_available=True),
        llm_client=SimpleNamespace(available=True),
    )
    body = client(app).get("/api/v1/status").json()
    assert body["components"]["llm"]["status"] == "ok"
    assert body["components"]["llm"]["model"] == "deepseek-chat"


def test_status_marks_the_simulator_down_when_it_fails() -> None:
    broken = FakeSimulatorClient()
    broken.fail_on = {"get_health"}
    body = client(build_app(client=broken)).get("/api/v1/status").json()
    assert body["components"]["simulator"]["status"] == "down"
    assert body["status"] == "down"


def test_status_mentions_an_open_circuit_breaker() -> None:
    broken = FakeSimulatorClient(breaker_state="open")
    broken.fail_on = {"get_health"}
    body = client(build_app(client=broken)).get("/api/v1/status").json()
    simulator = body["components"]["simulator"]
    assert simulator["status"] == "down"
    assert simulator["breaker_state"] == "open"
    assert "circuit breaker open" in simulator["detail"]


def test_status_marks_a_half_open_breaker_as_degraded() -> None:
    body = client(build_app(client=FakeSimulatorClient(breaker_state="half_open"))).get(
        "/api/v1/status"
    ).json()
    assert body["components"]["simulator"]["status"] == "degraded"


def test_status_marks_the_database_down_when_the_repository_raises() -> None:
    from test_api_support import FakeRepository

    repository = FakeRepository()
    repository.fail_on = {"latest_snapshot"}
    body = client(build_app(repository=repository)).get("/api/v1/status").json()
    assert body["components"]["database"]["status"] == "down"
    assert "Traceback" not in str(body)


def test_status_marks_the_database_down_when_ping_returns_false() -> None:
    """``Database.ping()`` reports failure by *returning* ``False``, not raising.

    Awaiting it and discarding the result made ``/api/v1/status`` answer
    ``"database": {"status": "ok"}`` for a database that could not be opened at
    all -- which is how a startup ``OperationalError`` stayed invisible while
    every write silently went nowhere.
    """
    from test_api_support import FakeRepository

    repository = FakeRepository()
    repository.ping = _ping_returning(False)
    body = client(build_app(repository=repository)).get("/api/v1/status").json()
    assert body["components"]["database"]["status"] == "down"


def test_status_reports_the_decision_engine_policy() -> None:
    body = client(build_app()).get("/api/v1/status").json()
    assert body["components"]["decision_engine"]["policy"] == "optimizer"


def test_status_uses_the_metrics_object_readout_when_available() -> None:
    class ReadoutMetrics(FakeMetrics):
        def status_snapshot(self) -> dict:
            return {
                "requests_total": 120,
                "p95_latency_ms": 164.0,
                "error_rate": 0.004,
                "availability": 0.996,
            }

    body = client(build_app(metrics=ReadoutMetrics())).get("/api/v1/status").json()
    assert body["p95_latency_ms"] == 164.0
    assert body["error_rate"] == 0.004
    assert body["requests_total"] == 120
    assert body["metrics_source"] == "Metrics.status_snapshot()"


def test_status_never_leaks_the_api_key_or_the_environment() -> None:
    response = client(build_app()).get("/api/v1/status")
    assert SECRET_SENTINEL not in response.text
    assert "api_key" not in response.text.lower()
    assert "sk-" not in response.text


def test_status_reports_a_degraded_decision_engine_without_scipy(monkeypatch) -> None:
    """The optimizer being unavailable is a supported fallback (CONTRACT.md 7.3)."""
    import app.api.health as health_module

    real_import = health_module.importlib.import_module

    def fake_import(name: str, *args, **kwargs):
        if name == "scipy.optimize":
            raise ImportError("scipy is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(health_module.importlib, "import_module", fake_import)
    body = client(build_app()).get("/api/v1/status").json()
    assert body["components"]["decision_engine"]["status"] == "degraded"
    assert "heuristic" in body["components"]["decision_engine"]["detail"]


# ---------------------------------------------------------------------------
# p95 computation (CONTRACT.md 10 pins no reader on Metrics)
# ---------------------------------------------------------------------------


def test_p95_interpolates_within_the_bucket() -> None:
    buckets = [
        (0.005, 0.0),
        (0.01, 0.0),
        (0.025, 0.0),
        (0.05, 100.0),
        (0.075, 100.0),
        (float("inf"), 100.0),
    ]
    value = _p95_from_buckets(buckets, 100.0)
    assert value is not None
    assert abs(value - 48.75) < 1e-6


def test_p95_is_none_without_observations() -> None:
    assert _p95_from_buckets([], 0.0) is None
    assert _p95_from_buckets([(0.1, 0.0)], 0.0) is None


def test_p95_falls_back_to_the_highest_finite_bucket() -> None:
    value = _p95_from_buckets([(0.01, 1.0), (float("inf"), 1.0)], 10.0)
    assert value == 10.0


def test_prometheus_stats_has_the_keys_the_status_page_needs() -> None:
    stats = prometheus_stats()
    for key in ("p95_latency_ms", "error_rate", "client_error_rate", "requests_total"):
        assert key in stats


# ---------------------------------------------------------------------------
# /metrics — owned by A9, mounted by install_observability
# ---------------------------------------------------------------------------


def test_metrics_endpoint_serves_the_prometheus_exposition() -> None:
    from test_api_support import build_full_app

    response = client(build_full_app()).get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]
    assert "python_gc_objects_collected_total" in response.text or "python_info" in response.text


def test_metrics_is_not_in_the_openapi_schema() -> None:
    from test_api_support import build_full_app

    assert "/metrics" not in build_full_app().openapi()["paths"]


def test_the_api_router_does_not_register_metrics() -> None:
    """A9 owns the path; a second handler would silently shadow it."""
    assert "/metrics" not in {route.path for route in api_router.routes}
