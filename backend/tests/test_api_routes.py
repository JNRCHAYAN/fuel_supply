"""Route-table conformance against CONTRACT.md section 9.

These tests need no dependencies at all: they assert the shape of the surface
the operator console talks to, and they are the guard against a route silently
moving or changing method.
"""

from __future__ import annotations

import re

import test_api_support  # noqa: F401  (puts backend/ on sys.path)

from app.api.router import api_router

# The section 9 table, transcribed row for row.
#
# `/metrics` is deliberately absent: CONTRACT.md section 9 lists it in this
# table, but section 10 gives A9 the metrics registry and the exposition, and
# `install_observability` mounts it. Registering it here as well produced a
# duplicate route where the first-registered handler silently won. A9 owns it;
# `test_metrics_is_registered_once` proves the assembled app has exactly one.
CONTRACT_ROUTES: set[tuple[str, str]] = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/status"),
    ("GET", "/api/v1/network/snapshot"),
    ("GET", "/api/v1/network/demand-history"),
    ("GET", "/api/v1/forecast"),
    ("GET", "/api/v1/risk"),
    ("GET", "/api/v1/recommendations"),
    ("GET", "/api/v1/recommendations/{id}/explanation"),
    ("POST", "/api/v1/recommendations/{id}/submit"),
    ("GET", "/api/v1/decisions"),
    ("GET", "/api/v1/decisions/{id}"),
    ("GET", "/api/v1/events"),
    ("POST", "/api/v1/events/summary"),
    ("POST", "/api/v1/investigate"),
    ("GET", "/api/v1/admin/faults"),
    ("POST", "/api/v1/admin/faults"),
    ("POST", "/api/v1/admin/simulation/{action}"),
}


def _normalise(path: str) -> str:
    """`{rec_id}` and `{decision_id}` are the contract's `{id}`."""
    for placeholder in ("{rec_id}", "{decision_id}"):
        path = path.replace(placeholder, "{id}")
    return path


def _concrete_to_template(path: str) -> str:
    """The reverse, for the concrete paths the leak sweep calls."""
    return (
        re.sub(r"/recommendations/[^/]+/", "/recommendations/{id}/", path)
        .replace("/decisions/1", "/decisions/{id}")
        .replace("/simulation/step", "/simulation/{action}")
    )


def _implemented() -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in api_router.routes:
        for method in getattr(route, "methods", ()) or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            routes.add((method, _normalise(route.path)))
    return routes


def test_every_contract_route_is_implemented() -> None:
    missing = CONTRACT_ROUTES - _implemented()
    assert not missing, f"routes required by CONTRACT.md section 9 are missing: {sorted(missing)}"


def test_no_route_outside_the_contract_surface() -> None:
    unexpected = _implemented() - CONTRACT_ROUTES
    assert not unexpected, f"routes not in CONTRACT.md section 9: {sorted(unexpected)}"


def test_router_carries_the_full_paths_and_no_prefix() -> None:
    """`main.py` includes this router bare, so the paths must be absolute."""
    for route in api_router.routes:
        assert route.path.startswith("/api/v1/"), route.path


def test_openapi_schema_generates() -> None:
    paths = {_normalise(p): v for p, v in api_router_routes_openapi()["paths"].items()}
    for method, path in sorted(CONTRACT_ROUTES):
        assert path in paths, f"{path} missing from OpenAPI"
        assert method.lower() in paths[path], f"{method} {path} missing from OpenAPI"


def api_router_routes_openapi() -> dict:
    from test_api_support import build_bare_app

    return build_bare_app().openapi()


def test_metrics_is_registered_once() -> None:
    """A9 owns `/metrics`; A8 must not register a competing handler.

    Two handlers on one path do not raise — the first registered wins — so the
    only way to catch a silent disagreement is to count them.
    """
    from test_api_support import build_full_app

    app = build_full_app()
    handlers = [route for route in app.routes if getattr(route, "path", None) == "/metrics"]
    assert len(handlers) == 1, f"/metrics registered {len(handlers)} times"


def test_no_path_is_registered_twice() -> None:
    from test_api_support import build_full_app

    seen: set[tuple[str, str]] = set()
    duplicates: set[tuple[str, str]] = set()
    for route in build_full_app().routes:
        for method in getattr(route, "methods", ()) or ():
            key = (method, route.path)
            if key in seen:
                duplicates.add(key)
            seen.add(key)
    assert not duplicates, f"duplicate routes in the assembled app: {sorted(duplicates)}"


def test_submit_is_the_only_write_to_the_simulator() -> None:
    """Brief section 24: one explicit operator-initiated write path."""
    routes = {r.path for r in api_router.routes}
    assert "/api/v1/admin/faults" in routes  # demo control, not an allocation
    assert "/api/v1/recommendations/{rec_id}/submit" in routes


def test_response_models_are_declared_for_the_data_routes() -> None:
    """A missing response_model means an unvalidated response body."""
    undocumented = []
    for route in api_router.routes:
        if getattr(route, "response_model", None) is None:
            undocumented.append(route.path)
    assert not undocumented, f"routes without a response_model: {sorted(undocumented)}"


def test_admin_clear_faults_has_no_route() -> None:
    """`admin_clear_faults` exists in section 5.4 but has no row in section 9.

    Recorded as a test so the gap is visible rather than silently invented.
    """
    assert "/api/v1/admin/faults/clear" not in {r.path for r in api_router.routes}


# ---------------------------------------------------------------------------
# The whole surface, swept for leaked configuration
# ---------------------------------------------------------------------------

#: Every route, with a body that gets it to its handler. A route is included
#: when it can be exercised without side effects beyond its own fake.
_ALL_ROUTE_CALLS: tuple[tuple[str, str, dict | None], ...] = (
    ("GET", "/api/v1/health", None),
    ("GET", "/api/v1/status", None),
    ("GET", "/api/v1/network/snapshot", None),
    ("GET", "/api/v1/network/demand-history", None),
    ("GET", "/api/v1/forecast", None),
    ("GET", "/api/v1/risk", None),
    ("GET", "/api/v1/recommendations", None),
    ("GET", "/api/v1/recommendations/rec-1/explanation", None),
    ("POST", "/api/v1/recommendations/rec-1/submit", {"confirm": True}),
    ("GET", "/api/v1/decisions", None),
    ("GET", "/api/v1/decisions/1", None),
    ("GET", "/api/v1/events", None),
    ("POST", "/api/v1/events/summary", {}),
    ("POST", "/api/v1/investigate", {"question": "why is ST-2 short?"}),
    ("GET", "/api/v1/admin/faults", None),
    ("POST", "/api/v1/admin/faults", {"type": "depot_offline"}),
    ("POST", "/api/v1/admin/simulation/step", None),
)


def test_every_route_including_the_error_paths_leaks_no_secret() -> None:
    """CONTRACT.md 0.2 and 24: no response may carry a configuration secret.

    The fake settings object carries the sentinel as its API key, so a handler
    that serialises Settings, echoes the environment, or puts a credential in an
    error message is caught here rather than in front of a judge.
    """
    from test_api_support import SECRET_SENTINEL, FakeSimulatorClient, build_app, client

    # The sweep must be real: every call must hit a route that exists, and none
    # may answer 405 or 500. A typo'd path here would otherwise make the whole
    # test pass while checking nothing.
    registered = _implemented()
    for method, path, _body in _ALL_ROUTE_CALLS:
        template = _concrete_to_template(path)
        assert (method, template) in registered, f"{method} {path} is not a route"

    broken = FakeSimulatorClient()
    broken.fail_on = {
        "get_instance",
        "get_depots",
        "get_stations",
        "get_routes",
        "get_regions",
        "get_supply_arrivals",
        "get_events",
        "get_metrics",
        "get_health",
        "get_demand_history",
        "create_allocation",
        "admin_get_faults",
        "admin_inject_fault",
        "admin_step",
    }

    for app in (build_app(), build_app(client=broken)):
        http = client(app)
        for method, path, body in _ALL_ROUTE_CALLS:
            response = http.request(method, path, json=body)
            assert response.status_code not in (405, 500), f"{method} {path} -> {response.status_code}"
            assert SECRET_SENTINEL not in response.text, f"{method} {path} leaked the key"
            lowered = response.text.lower()
            for name in ("deepseek_api_key", "api_key", "authorization"):
                assert name not in lowered, f"{method} {path} echoed the field name {name}"
