"""Locust workload for the Fuel Supply Intelligence & Resilience Platform.

Brief §17 requires load-testing at least one meaningful application path and
reporting average / p50 / p95 / p99 latency, throughput, error rate and
concurrency. CONTRACT §11 names the path: the recommendations endpoint.

Why recommendations, and not /health
------------------------------------
`GET /api/v1/recommendations` is the most expensive and the most meaningful
request the system serves. It composes the whole intelligence chain — snapshot,
forecast, detection, the allocation optimiser — behind one call, so it is the
endpoint whose latency actually characterises the system. `/api/v1/health` is
included as a cheap baseline: the ratio between the two is what tells you
whether a slow p95 is the API's overhead or the intelligence layer's work.

Both paths are read-only. This workload deliberately does **not** touch
`POST /api/v1/recommendations/{id}/submit`: submitting allocations is an
operator-initiated action and a load generator must never drive the simulated
world (brief §24, CONTRACT §0.7).

Running it
----------
Headless, which is how the recorded figures were produced (see
`loadtest/README.md` and the results section of `docs/architecture.md`)::

    locust -f backend/loadtest/locustfile.py --headless \
        --users 50 --spawn-rate 5 --run-time 2m --host http://localhost:9000

Interactive, for exploring a running stack::

    locust -f backend/loadtest/locustfile.py --host http://localhost:9000
    # then open http://localhost:8089

At the end of a run this module prints a summary block with p50/p95/p99,
throughput and error rate per endpoint, formatted to be pasted into
docs/architecture.md.

Configuration is by environment variable, all with defaults, so the same file
runs against docker compose, a local uvicorn, or CI:

    LOADTEST_HOST              default http://localhost:9000
    LOADTEST_USERS             default 50
    LOADTEST_SPAWN_RATE        default 5
    LOADTEST_RUN_TIME          default 2m
    LOADTEST_ASSERT_SIMULATED  default 1 — see below

Ownership: A10. See CONTRACT.md §3.
"""

from __future__ import annotations

import os
from typing import Any

from locust import HttpUser, between, events, task

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_HOST = os.environ.get("LOADTEST_HOST", "http://localhost:9000")

#: When set to "0", a response is not failed for omitting `simulated: true`.
#: The check is on by default on purpose: brief §24 and CONTRACT §0.6 make the
#: flag mandatory on anything carrying a recommendation or prediction, and a
#: load test is the cheapest place to notice that it was dropped.
ASSERT_SIMULATED = os.environ.get("LOADTEST_ASSERT_SIMULATED", "1") != "0"

HEALTH_PATH = "/api/v1/health"
RECOMMENDATIONS_PATH = "/api/v1/recommendations"

# Request names. Locust groups statistics by name, so the chained task reports
# as its own row instead of being folded into the bare endpoint.
NAME_HEALTH = "GET /api/v1/health (baseline)"
NAME_RECOMMENDATIONS = "GET /api/v1/recommendations (intelligence chain)"
NAME_EXPLANATION = "GET /api/v1/recommendations/{id}/explanation (LLM path)"


def _fail_if_not_json(response: Any, expected_flag: bool) -> None:
    """Validate one response. Called inside a catch_response block."""
    if response.status_code != 200:
        response.failure(f"HTTP {response.status_code}: {response.text[:200]}")
        return
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 - any decode failure is the same failure
        response.failure("response body was not valid JSON")
        return

    if expected_flag and ASSERT_SIMULATED:
        # The flag may sit at the top level or on each item of a list payload;
        # either satisfies the requirement.
        if not _claims_simulated(body):
            response.failure("response omitted simulated=true (CONTRACT §0.6)")


def _claims_simulated(body: Any) -> bool:
    if isinstance(body, dict):
        if body.get("simulated") is True:
            return True
        for value in body.values():
            if isinstance(value, (dict, list)) and _claims_simulated(value):
                return True
        return False
    if isinstance(body, list):
        return any(_claims_simulated(item) for item in body)
    return False


class OperatorConsoleUser(HttpUser):
    """Reads the console's two most important endpoints, in that proportion."""

    host = DEFAULT_HOST
    wait_time = between(1, 3)

    # -- the baseline ------------------------------------------------------

    @task(1)
    def health(self) -> None:
        """Cheap liveness probe. The floor for any latency number below."""
        with self.client.get(
            HEALTH_PATH, name=NAME_HEALTH, catch_response=True
        ) as response:
            if response.status_code != 200:
                response.failure(f"HTTP {response.status_code}")
            else:
                response.success()

    # -- the meaningful path ----------------------------------------------

    @task(5)
    def recommendations(self) -> None:
        """The full intelligence chain: snapshot -> forecast -> detect -> optimise."""
        with self.client.get(
            RECOMMENDATIONS_PATH, name=NAME_RECOMMENDATIONS, catch_response=True
        ) as response:
            _fail_if_not_json(response, expected_flag=True)

    @task(2)
    def recommendation_explanation(self) -> None:
        """End-to-end: list recommendations, then explain the top one.

        Also exercises the LLM path — which, with no key configured, is the
        deterministic fallback (CONTRACT §8.2). That makes this task a load test
        of the *degraded* path, which is the one an operator experiences when
        the model is unavailable.
        """
        with self.client.get(
            RECOMMENDATIONS_PATH, name=NAME_RECOMMENDATIONS, catch_response=True
        ) as response:
            _fail_if_not_json(response, expected_flag=True)
            if response.status_code != 200:
                return
            try:
                body = response.json()
            except Exception:  # noqa: BLE001
                return

        recommendation_id = _first_id(body)
        if recommendation_id is None:
            # No recommendations on the board right now is a legitimate state,
            # not a failure: marking it failed would report an error rate that
            # is really just an idle network.
            return

        path = f"{RECOMMENDATIONS_PATH}/{recommendation_id}/explanation"
        with self.client.get(path, name=NAME_EXPLANATION,
                             catch_response=True) as response:
            _fail_if_not_json(response, expected_flag=True)


def _first_id(body: Any) -> str | None:
    """Pull a recommendation id out of whatever envelope the API returns.

    Tolerant on purpose — the API layer owns its response shape (CONTRACT §9),
    and this file must not break because a wrapper key was named differently.
    """
    items: Any = body
    if isinstance(body, dict):
        for key in ("items", "recommendations", "data", "results"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
    if isinstance(items, list) and items:
        first = items[0]
        if isinstance(first, dict):
            candidate = first.get("id")
            if isinstance(candidate, str):
                return candidate
            if candidate is not None:
                return str(candidate)
    return None


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def _pct(entry: Any, ratio: float) -> float | None:
    if not getattr(entry, "num_requests", 0):
        return None
    try:
        return float(entry.get_response_time_percentile(ratio))
    except Exception:  # noqa: BLE001 - never let reporting break a run
        return None


def _fmt(value: float | None, unit: str = "ms") -> str:
    return "n/a" if value is None else f"{value:,.0f}{unit}"


@events.quitting.add_listener
def report_summary(environment: Any, **_kwargs: Any) -> None:
    """Print the numbers brief §17 asks for, at the end of every run.

    Deliberately printed rather than written to a file: the figures are only
    meaningful next to the workload that produced them, so the run
    configuration is printed with them and both go into
    docs/architecture.md together.
    """
    stats = environment.stats
    total = stats.total

    width = 78
    line = "=" * width
    print()
    print(line)
    print("LOAD TEST SUMMARY - Fuel Supply Intelligence & Resilience Platform")
    print(line)
    print(f"host          : {getattr(environment, 'host', DEFAULT_HOST)}")
    print(f"concurrency   : {_concurrency(environment)} simulated operators")
    print(f"run time      : {_run_time(environment)}")
    print(f"requests      : {total.num_requests:,} "
          f"({total.num_failures:,} failed)")
    print(f"throughput    : {total.total_rps:,.2f} req/s")
    print(f"error rate    : {total.fail_ratio * 100:.2f}%")
    print(f"latency (all) : avg {_fmt(total.avg_response_time)} | "
          f"p50 {_fmt(_pct(total, 0.50))} | "
          f"p95 {_fmt(_pct(total, 0.95))} | "
          f"p99 {_fmt(_pct(total, 0.99))} | "
          f"max {_fmt(total.max_response_time)}")
    print()
    print("per endpoint (latency in ms)")
    print("-" * width)
    header = (f"{'endpoint':<54}{'reqs':>7}{'err%':>7}"
              f"{'p50':>7}{'p95':>7}{'p99':>7}")
    print(header)
    for name, entry in sorted(stats.entries.items()):
        if not entry.num_requests:
            continue
        # stats.entries is keyed by (name, method); report the name once.
        label = name[0] if isinstance(name, tuple) else str(name)
        print(f"{label[:52]:<54}{entry.num_requests:>7}"
              f"{entry.fail_ratio * 100:>6.1f}%"
              f"{_fmt(_pct(entry, 0.50), ''):>7}"
              f"{_fmt(_pct(entry, 0.95), ''):>7}"
              f"{_fmt(_pct(entry, 0.99), ''):>7}")
    print(line)
    print("Copy these figures into docs/architecture.md, section"
          " 'Load-test results',")
    print("together with the workload line above. Do not restate them without"
          " it.")
    print(line)
    print()


def _concurrency(environment: Any) -> Any:
    """The number of simulated operators the run was configured for.

    The live ``user_count`` is zero by the time this listener runs — the users
    have already been stopped — so a naive read reports "0 simulated operators",
    which is worse than reporting nothing. Prefer the configured target, and
    fall back to the environment variable.
    """
    runner = getattr(environment, "runner", None)
    for candidate in (
        getattr(runner, "user_count", None),
        getattr(runner, "target_user_count", None),
    ):
        if candidate:
            return candidate

    options = getattr(environment, "parsed_options", None)
    configured = getattr(options, "num_users", None)
    if configured:
        return configured

    return _env_int("LOADTEST_USERS", 0) or "unknown"


def _run_time(environment: Any) -> str:
    """The run duration, from the environment or from locust's own options."""
    raw = os.environ.get("LOADTEST_RUN_TIME")
    if raw:
        return raw
    options = getattr(environment, "parsed_options", None)
    parsed = getattr(options, "run_time", None)
    return str(parsed) if parsed else "n/a"
