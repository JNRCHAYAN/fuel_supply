"""`/api/v1/status` must not report a cached simulator reading as healthy.

Brief section 15 ("the application should expose the health of important
components where meaningful") and guide 7.10 (the simulator's `/v1/health`
bypasses fault injection, so it answers `ok` mid-outage).

The defect these tests pin down
-------------------------------
A2's client knows when a read came from its cache -- `_read` returns
`(value, stale, age_seconds)` and `_serve_last_good` is the only producer of
`stale=True` -- but `get_health()` drops that flag and returns the bare
`Health` (`app/sim/client.py`). So during a real outage, with the circuit
breaker OPEN, `/api/v1/status` was served the last-good `Health(status="ok")`
and printed **"Fuel Simulator: Healthy"** from cache. The client's own
docstring names this exact failure, and an operator reading the status page
could not tell a live simulator from a cached one.

Every test here is written against fakes shaped like A2's client, and the ones
marked "regression" fail against the code as it was.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import test_api_support  # noqa: F401
from test_api_support import FakeSimulatorClient, build_app, client, fake_settings

from app.api.health import _breaker_state, _read_is_stale

STATUS = "/api/v1/status"
HEALTH = "/api/v1/health"


# ---------------------------------------------------------------------------
# Fakes shaped like A2's resilience surface
# ---------------------------------------------------------------------------


class CachedHealthSimulator(FakeSimulatorClient):
    """A2's client mid-outage: the breaker is OPEN and health comes from cache.

    ``get_health()`` returns the cached ``Health`` exactly as the real client
    does, with the ``stale`` flag ``_serve_last_good`` computed dropped on the
    way out. This is the defect, reproduced faithfully rather than invented.
    """

    def __init__(self, *, breaker_state: str = "open", age_seconds: float = 47.5) -> None:
        super().__init__(breaker_state=breaker_state)
        self._age_seconds = age_seconds

    def last_good_age(self, key: str) -> float | None:
        return self._age_seconds if key == "health" else None

    @property
    def memo_seconds(self) -> float:
        # `simulator_cache_ttl_seconds` defaults to 1.0 (app/config.py).
        return 1.0


class BareSimulatorClient:
    """A client with a health endpoint and nothing else.

    No breaker, no cache, no staleness surface. The route must still answer,
    and must take the client's word rather than fail.
    """

    async def get_health(self) -> dict[str, Any]:
        return {"status": "ok", "database": "ok"}


class RaisingBreakerClient(FakeSimulatorClient):
    """A client whose `breaker_state` property throws.

    A half-torn-down client is exactly when an operator opens the status page;
    reading the breaker must not be able to take the page down with it.
    """

    @property
    def breaker_state(self) -> str:  # type: ignore[override]
        raise RuntimeError("breaker state is unavailable")


class MetricStaleClient(FakeSimulatorClient):
    """A client that reports staleness directly, with a CLOSED breaker."""

    def __init__(self, *, stale: bool) -> None:
        super().__init__(breaker_state="closed")
        self._stale = stale

    def read_meta(self, key: str) -> dict[str, Any]:
        return {"stale": self._stale, "age_seconds": 12.0}


class CircuitOpenError(RuntimeError):
    """A `SimulatorError.circuit_open` by shape: it carries `kind`."""

    kind = "circuit_open"


class RefusingSimulatorClient(FakeSimulatorClient):
    """Breaker OPEN with an empty cache: the read raises instead of fabricating."""

    def __init__(self) -> None:
        super().__init__(breaker_state="open")

    async def get_health(self) -> Any:
        raise CircuitOpenError(
            "simulator circuit breaker is open and there is no cached value for 'health'"
        )


def _simulator(body: dict[str, Any]) -> dict[str, Any]:
    return body["components"]["simulator"]


# ---------------------------------------------------------------------------
# The regression: an OPEN breaker must not yield "healthy"
# ---------------------------------------------------------------------------


def test_open_breaker_with_a_cached_healthy_reading_is_not_reported_healthy() -> None:
    """Regression: the cached `Health(status="ok")` must not become `ok` here."""
    body = client(build_app(client=CachedHealthSimulator())).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "degraded"
    assert simulator["status"] != "ok"
    assert simulator["stale"] is True
    assert simulator["breaker_state"] == "open"
    assert body["status"] == "degraded"


def test_an_open_breaker_alone_withholds_a_healthy_verdict() -> None:
    """Regression: no cache surface needed -- `breaker_state` is enough.

    The old code consulted the breaker only when `get_health()` *raised*, and
    downgraded an otherwise-ok result only for `half_open`.
    """
    fake = FakeSimulatorClient(breaker_state="open")
    body = client(build_app(client=fake)).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "degraded"
    assert simulator["stale"] is True


def test_a_half_open_breaker_withholds_a_healthy_verdict() -> None:
    fake = FakeSimulatorClient(breaker_state="half_open")
    body = client(build_app(client=fake)).get(STATUS).json()
    assert _simulator(body)["status"] == "degraded"


def test_a_cached_read_without_a_breaker_still_reads_as_stale() -> None:
    """Regression: the cache's own age is evidence, breaker or no breaker.

    A successful fetch stamps the cache with `now`, so an age past the memo
    window is a value this call re-served instead of fetching.
    """

    class AgedCacheClient(FakeSimulatorClient):
        def __init__(self) -> None:
            super().__init__(breaker_state="closed")

        def last_good_age(self, key: str) -> float | None:
            return 47.5 if key == "health" else None

        @property
        def memo_seconds(self) -> float:
            return 1.0

    body = client(build_app(client=AgedCacheClient())).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "degraded"
    assert simulator["stale"] is True
    assert simulator["breaker_state"] == "closed"


def test_a_memo_hit_within_the_window_is_not_stale() -> None:
    """The memo is *aged*, not *degraded*: the simulator did answer, recently."""

    class MemoClient(FakeSimulatorClient):
        def __init__(self) -> None:
            super().__init__(breaker_state="closed")

        def last_good_age(self, key: str) -> float | None:
            return 0.4 if key == "health" else None

        @property
        def memo_seconds(self) -> float:
            return 1.0

    body = client(build_app(client=MemoClient())).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "ok"
    assert simulator["stale"] is False


# ---------------------------------------------------------------------------
# The truth still has to read "healthy" when it is
# ---------------------------------------------------------------------------


def test_closed_breaker_with_a_fresh_reading_still_reports_healthy() -> None:
    app = build_app(
        client=FakeSimulatorClient(breaker_state="closed"),
        # The default fixture runs without an LLM key, and a missing key is a
        # `degraded` component by contract -- configure one so that the
        # top-level verdict is decided by the simulator under test.
        settings=fake_settings(llm_available=True),
        llm_client=SimpleNamespace(available=True),
    )
    body = client(app).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "ok"
    assert simulator["stale"] is False
    assert simulator["breaker_state"] == "closed"
    assert body["status"] == "ok"


def test_an_explicitly_fresh_read_is_believed_over_the_age_heuristic() -> None:
    """A client that says `stale=False` is taken at its word (evidence 1 wins)."""
    body = client(build_app(client=MetricStaleClient(stale=False))).get(STATUS).json()
    assert _simulator(body)["status"] == "ok"


def test_an_explicitly_stale_read_is_reported_degraded() -> None:
    body = client(build_app(client=MetricStaleClient(stale=True))).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "degraded"
    assert simulator["stale"] is True


def test_a_stale_reading_is_degraded_and_never_down() -> None:
    """Brief section 11: serve from cache and say so; do not claim an outage."""
    body = client(build_app(client=CachedHealthSimulator())).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "degraded"
    assert "cache" in simulator["detail"]
    assert "did not answer a live request" in simulator["detail"]


def test_the_three_states_are_distinguishable_in_the_payload() -> None:
    """`ok` / `stale` / `unavailable` must be told apart without reading logs."""
    ok = _simulator(client(build_app(client=FakeSimulatorClient())).get(STATUS).json())
    stale = _simulator(client(build_app(client=CachedHealthSimulator())).get(STATUS).json())
    unavailable = _simulator(
        client(build_app(client=RefusingSimulatorClient())).get(STATUS).json()
    )
    assert ok["status"] == "ok" and ok["stale"] is False
    assert stale["status"] == "degraded" and stale["stale"] is True
    assert unavailable["status"] == "down" and "circuit breaker open" in unavailable["detail"]


def test_an_open_breaker_with_nothing_cached_is_down() -> None:
    """`_serve_last_good` raises rather than fabricating a value."""
    body = client(build_app(client=RefusingSimulatorClient())).get(STATUS).json()
    simulator = _simulator(body)
    assert simulator["status"] == "down"
    assert simulator["breaker_state"] == "open"
    assert body["status"] == "down"


# ---------------------------------------------------------------------------
# A client that cannot answer the question must not break the route
# ---------------------------------------------------------------------------


def test_a_client_with_no_breaker_or_cache_surface_does_not_break_the_route() -> None:
    response = client(build_app(client=BareSimulatorClient())).get(STATUS)
    assert response.status_code == 200
    simulator = _simulator(response.json())
    assert simulator["status"] == "ok"
    assert simulator["stale"] is False
    assert simulator["breaker_state"] is None


def test_a_client_whose_breaker_property_raises_does_not_break_the_route() -> None:
    response = client(build_app(client=RaisingBreakerClient())).get(STATUS)
    assert response.status_code == 200
    simulator = _simulator(response.json())
    assert simulator["status"] == "ok"
    assert simulator["breaker_state"] is None


def test_the_status_page_never_reports_a_component_it_cannot_observe() -> None:
    """An engine the process never built is absent, not `down`."""
    components = client(build_app()).get(STATUS).json()["components"]
    assert "forecasting" not in components
    assert "anomaly_detection" not in components


def test_an_engine_published_on_the_app_state_is_reported() -> None:
    app = build_app()
    app.state.forecaster = SimpleNamespace()
    app.state._api_detector = SimpleNamespace()
    components = client(app).get(STATUS).json()["components"]
    assert components["forecasting"]["status"] == "ok"
    assert components["anomaly_detection"]["status"] == "ok"


# ---------------------------------------------------------------------------
# Liveness is not readiness (the two probes stay separate)
# ---------------------------------------------------------------------------


def test_health_stays_live_while_the_status_page_reports_degraded() -> None:
    """A degraded dependency must not fail the liveness probe.

    `/health` is the container HEALTHCHECK: failing it here would restart a
    container that is running and serving requests perfectly well.
    """
    app = build_app(client=CachedHealthSimulator())
    assert client(app).get(HEALTH).status_code == 200
    assert client(app).get(HEALTH).json()["status"] == "ok"
    assert client(app).get(STATUS).json()["status"] == "degraded"


def test_health_answers_while_the_status_page_is_down() -> None:
    app = build_app(client=RefusingSimulatorClient())
    assert client(app).get(HEALTH).status_code == 200
    assert client(app).get(STATUS).json()["status"] == "down"


# ---------------------------------------------------------------------------
# The status page keeps its metrics figures
# ---------------------------------------------------------------------------


def test_the_latency_and_error_figures_survive_a_degraded_simulator() -> None:
    body = client(build_app(client=CachedHealthSimulator())).get(STATUS).json()
    for key in ("p95_latency_ms", "error_rate", "requests_total"):
        assert key in body


# ---------------------------------------------------------------------------
# The evidence helpers in isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("open", "open"),
        ("CLOSED", "closed"),
        (SimpleNamespace(value="half_open"), "half_open"),
        ("", None),
        (None, None),
    ],
)
def test_breaker_state_normalises_what_a_client_may_return(value: Any, expected: str | None) -> None:
    assert _breaker_state(SimpleNamespace(breaker_state=value)) == expected


def test_breaker_state_returns_none_when_the_client_has_none() -> None:
    assert _breaker_state(SimpleNamespace()) is None
    assert _breaker_state(None) is None


def test_read_is_stale_prefers_the_client_s_own_answer() -> None:
    client_ = SimpleNamespace(breaker_state="closed", read_meta=lambda key: {"stale": True})
    assert _read_is_stale(client_, "health", "closed") == (True, None)


def test_read_is_stale_falls_back_to_none_evidence() -> None:
    assert _read_is_stale(SimpleNamespace(), "health", None) == (False, None)
