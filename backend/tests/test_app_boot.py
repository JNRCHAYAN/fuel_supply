"""Boot-path tests for ``app/main.py`` (owner: A1).

These tests exercise the real application factory — no stubs, no monkeypatched
peer modules. ``app/main.py`` imports five peer modules at module level
(CONTRACT.md section 3), so on a partially-built repository this file has to
report *which* peer is missing rather than quietly pass. A red test here is a
true signal about the integration state of the tree; a green one is a true
signal that the backend actually boots.

Nothing here depends on the live simulator. The boot settings point at a
discard port and a throwaway SQLite file, so a passing run proves the
application boots and serves *while the simulator is unreachable* — which is the
degraded-mode behaviour brief section 11 asks for.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings

#: Modules ``app/main.py`` imports at module level, mapped to the workstream
#: that owns them, so a failure can be reported by name and by owner.
PEER_IMPORTS: dict[str, str] = {
    "app.api.router": "A8 - api_router",
    "app.observability.metrics": "A9 - Metrics",
    "app.observability.setup": "A9 - install_observability",
    "app.sim.client": "A2 - SimulatorClient",
    "app.store.repository": "A3 - Repository",
}


def _attempt_import() -> tuple[Any, str | None, str | None]:
    """Import ``app.main`` once, capturing any failure with its module name."""
    try:
        return importlib.import_module("app.main"), None, None
    except Exception as exc:  # noqa: BLE001 - captured and reported, never swallowed
        missing = getattr(exc, "name", None)
        return None, f"{type(exc).__name__}: {exc}", missing


APP_MAIN, IMPORT_ERROR, MISSING_MODULE = _attempt_import()

#: Set when the *only* reason the import failed is an absent peer module.
MISSING_PEER: str | None = None
if MISSING_MODULE:
    for candidate in PEER_IMPORTS:
        if MISSING_MODULE == candidate or candidate.startswith(f"{MISSING_MODULE}."):
            MISSING_PEER = candidate
            break


def _failure_report() -> str:
    """A message that names the blocker instead of hiding behind a traceback."""
    lines = [
        "",
        "=" * 72,
        "app.main could not be imported, so the boot path cannot be exercised.",
        "=" * 72,
        f"  {IMPORT_ERROR}",
        "",
    ]
    if MISSING_PEER:
        lines += [
            f"  BLOCKER: peer module '{MISSING_PEER}' does not exist yet",
            f"           owner: {PEER_IMPORTS[MISSING_PEER]}",
            "",
        ]
    elif MISSING_MODULE:
        lines += [
            f"  Missing module reported by Python: '{MISSING_MODULE}'",
            "  This is not one of the five peer modules app/main.py imports, so it",
            "  is a missing dependency or a typo rather than a pending workstream.",
            "",
        ]
    lines.append("  app/main.py imports these peers at module level:")
    for module, owner in PEER_IMPORTS.items():
        lines.append(f"    - {module:<28} [{owner}]")
    lines += [
        "",
        "  This test is deliberately not skipped: while a peer is absent the",
        "  backend genuinely does not boot, and the suite must say so.",
        "=" * 72,
    ]
    return "\n".join(lines)


def _require_app_module() -> Any:
    """Return ``app.main``, or fail this test by naming the blocking peer.

    Deliberately a helper rather than a fixture: called inside the test body it
    reports as a FAILED test with this message, where a fixture would report a
    setup ERROR and bury the explanation. The test is never skipped — while a
    peer module is absent, the backend genuinely does not boot.
    """
    if APP_MAIN is None:
        pytest.fail(_failure_report(), pytrace=False)
    return APP_MAIN


def _boot_settings(tmp_path: Path) -> Settings:
    """Settings that boot the app against nothing real.

    Port 9 (discard) refuses immediately, so ``SimulatorClient.start()`` cannot
    hang the suite, and the database is a throwaway file under ``tmp_path`` so
    the operator's ``./data/fuel.db`` is never touched.
    """
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        simulator_base_url="http://127.0.0.1:9",
        simulator_timeout_seconds=0.25,
        simulator_max_retries=1,
        circuit_reset_seconds=1,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'boot.db').as_posix()}",
        deepseek_api_key="",
        llm_enabled=False,
        log_level="WARNING",
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------
def test_app_main_imports() -> None:
    """The factory module, its peers and the module-level ASGI app all import."""
    if APP_MAIN is None:
        pytest.fail(_failure_report(), pytrace=False)
    assert APP_MAIN.app is not None


# ---------------------------------------------------------------------------
# Factory wiring (no lifespan, no network)
# ---------------------------------------------------------------------------
def test_create_app_returns_a_fastapi_app(tmp_path: Path) -> None:

    app_module = _require_app_module()
    app = app_module.create_app(_boot_settings(tmp_path))
    assert app.title
    assert app.router is not None


def test_api_router_is_mounted_under_api_v1(tmp_path: Path) -> None:

    """CONTRACT section 9: every API route lives under ``/api/v1``."""
    app_module = _require_app_module()
    app = app_module.create_app(_boot_settings(tmp_path))
    paths = {getattr(route, "path", "") for route in app.routes}
    v1_paths = {path for path in paths if path.startswith("/api/v1")}
    assert v1_paths, f"no route is mounted under /api/v1; routes are {sorted(paths)}"


def test_no_double_api_prefix(tmp_path: Path) -> None:

    """Guards the failure mode of applying the prefix twice."""
    app_module = _require_app_module()
    app = app_module.create_app(_boot_settings(tmp_path))
    offenders = [
        getattr(route, "path", "")
        for route in app.routes
        if getattr(route, "path", "").startswith("/api/v1/api/v1")
    ]
    assert not offenders, f"prefix applied twice: {offenders}"


def test_health_route_is_registered(tmp_path: Path) -> None:

    """``GET /api/v1/health`` is liveness and must always exist."""
    app_module = _require_app_module()
    app = app_module.create_app(_boot_settings(tmp_path))
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/v1/health" in paths, f"routes are {sorted(paths)}"


def test_settings_are_published_before_the_lifespan_runs(tmp_path: Path) -> None:
    """Dependencies resolve from app.state, so it must be populated eagerly."""
    app_module = _require_app_module()
    settings = _boot_settings(tmp_path)
    app = app_module.create_app(settings)
    assert app.state.settings is settings


# ---------------------------------------------------------------------------
# Lifespan and serving
# ---------------------------------------------------------------------------
def test_lifespan_publishes_components_and_shuts_down_cleanly(tmp_path: Path) -> None:
    """Startup builds the shared collaborators; shutdown releases them."""
    app_module = _require_app_module()
    settings = _boot_settings(tmp_path)
    app = app_module.create_app(settings)

    with TestClient(app):
        state = app.state
        assert state.settings is settings
        assert state.metrics is not None
        assert state.repository is not None
        assert state.simulator_client is not None

    # Leaving the context runs the shutdown half of the lifespan. If it raised,
    # TestClient would have propagated, so reaching here is the assertion.


def test_health_answers_without_a_live_simulator(tmp_path: Path) -> None:
    """The platform stays servable when the simulator is unreachable."""
    app_module = _require_app_module()
    app = app_module.create_app(_boot_settings(tmp_path))
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 200, response.text
        assert isinstance(response.json(), dict)


def _diagnose_metrics_failure(exc: BaseException) -> str:
    """Explain a ``/metrics`` failure well enough to route it to its owner."""
    lines = [
        "",
        "=" * 72,
        "GET /metrics did not produce a response.",
        "=" * 72,
        f"  {type(exc).__name__}: {exc}",
        "",
    ]
    if isinstance(exc, TypeError) and "metrics_endpoint" in str(exc):
        lines += [
            "  OWNER: A9 - backend/app/observability/setup.py",
            "",
            "  `metrics_endpoint()` is defined with no parameters and registered as",
            "  `app.add_route('/metrics', metrics_endpoint)`. Starlette invokes a route",
            "  endpoint as `endpoint(request)`, so the handler must accept the request:",
            "",
            "      def metrics_endpoint(request: Request) -> Response:",
            "",
            "  Not an A1 defect: `app/main.py` only calls `install_observability(app)`.",
            "  A8 also registers `/metrics` (api/health.py) and would serve it; A9's",
            "  route is installed first, so it wins the match and raises.",
        ]
    lines.append("=" * 72)
    return "\n".join(lines)


def test_prometheus_endpoint_is_served_without_the_api_prefix(tmp_path: Path) -> None:
    """CONTRACT section 9 lists ``/metrics`` as the one unprefixed route.

    Currently red because of A9's handler signature — a true statement about the
    tree, reported by name rather than skipped.
    """
    app_module = _require_app_module()
    app = app_module.create_app(_boot_settings(tmp_path))
    with TestClient(app) as client:
        try:
            response = client.get("/metrics")
        except Exception as exc:  # noqa: BLE001 - reported with a diagnosis
            pytest.fail(_diagnose_metrics_failure(exc), pytrace=False)
        assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Dependency providers (app/deps.py)
# ---------------------------------------------------------------------------
def _deps_module() -> Any:
    """Import ``app.deps`` lazily so a peer import error names itself."""
    return importlib.import_module("app.deps")


def test_dependencies_resolve_from_the_running_application(tmp_path: Path) -> None:
    """All four providers hand back the components the lifespan published.

    A probe route is added to a real app and fetched, so this exercises the
    actual dependency-injection path — annotations, state lookup and all —
    rather than calling the providers as plain functions.
    """
    app_module = _require_app_module()
    deps = _deps_module()
    from fastapi import APIRouter, Depends

    app = app_module.create_app(_boot_settings(tmp_path))

    probe = APIRouter()

    @probe.get("/__probe__/deps")
    def _probe(  # type: ignore[no-untyped-def]
        settings=Depends(deps.get_settings),
        metrics=Depends(deps.get_metrics),
        repository=Depends(deps.get_repository),
        simulator_client=Depends(deps.get_simulator_client),
    ) -> dict[str, str]:
        return {
            "settings": type(settings).__name__,
            "metrics": type(metrics).__name__,
            "repository": type(repository).__name__,
            "simulator_client": type(simulator_client).__name__,
        }

    app.include_router(probe)

    with TestClient(app) as client:
        response = client.get("/__probe__/deps")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "settings": "Settings",
            "metrics": "Metrics",
            "repository": "Repository",
            "simulator_client": "SimulatorClient",
        }


def test_component_providers_fail_loudly_when_the_lifespan_did_not_run() -> None:
    """CONTRACT section 12.3: the failure path is tested, not just the happy one.

    A provider that returned ``None`` would turn a wiring mistake into an
    ``AttributeError`` deep inside a request; it must name the component instead.
    """
    deps = _deps_module()
    import types

    request = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace())
    )

    for provider, name in (
        (deps.get_metrics, "metrics"),
        (deps.get_repository, "repository"),
        (deps.get_simulator_client, "simulator_client"),
    ):
        with pytest.raises(RuntimeError, match=name):
            provider(request)


def test_settings_provider_falls_back_to_the_cached_settings() -> None:
    """Without a published app state, settings still resolve (never None)."""
    deps = _deps_module()
    import types

    request = types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace())
    )
    settings = deps.get_settings(request)
    assert isinstance(settings, Settings)
    # Cached, so repeated resolution is cheap and stable.
    assert deps.get_settings(request) is settings
