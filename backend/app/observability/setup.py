"""Wiring — ``install_observability(app)`` (CONTRACT.md section 10).

One call does four things, in this order, and is safe to make more than once:

1. configure structlog for JSON output at ``LOG_LEVEL``;
2. teach the redactor the application's secrets, so the DeepSeek key is scrubbed
   from every log line from the first request onward;
3. register the system (CPU/memory) collectors, guarded so a second call — or a
   second app in the same process, which is what a test suite does — cannot
   raise ``ValueError: Duplicated timeseries``;
4. add the request middleware and mount ``GET /metrics``.

Idempotency is not decoration. The failure it prevents is concrete: pytest builds
the app once per module, and a collector registered twice raises at construction
time, which reads as "the app is broken" rather than "it was built twice".
"""

from __future__ import annotations

import threading
from typing import Any, Final

from fastapi import FastAPI, Request, Response

from .logging import (
    clear_request_context,
    configure_logging,
    get_logger,
    register_settings_secrets,
)
from .metrics import METRICS, REGISTRY, Metrics, ensure_system_collectors, render_metrics
from .middleware import ObservabilityMiddleware

__all__ = ["install_observability", "metrics_endpoint"]

#: Marker attribute set on ``app.state`` so a second call on the same app is a
#: no-op rather than a second middleware that double-counts every request.
_INSTALLED_ATTR: Final[str] = "observability_installed"

_install_lock = threading.Lock()


async def metrics_endpoint(request: Request) -> Response:
    """``GET /metrics`` — the Prometheus text exposition (CONTRACT section 9).

    Deliberately outside the ``/api/v1`` prefix: it is for Prometheus, not for
    the console, and it is the one route that is not itself instrumented.

    Takes ``request`` because a route endpoint is always invoked with it
    (Starlette calls ``endpoint(request)``; FastAPI injects the same object).
    A zero-argument endpoint raises ``TypeError`` on every scrape and the
    endpoint then reports "did not produce a response" — which is exactly how
    the observability deliverable fails silently, so the parameter is
    load-bearing.
    """
    return Response(
        content=render_metrics(REGISTRY),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _resolve_settings(app: FastAPI, settings: Any | None) -> Any | None:
    """Find a settings object without making configuration a hard dependency.

    ``app.main`` publishes ``app.state.settings`` before calling us, so that is
    the first place to look. The ``app.config`` import is last and guarded: this
    module must still install on a bare FastAPI app (as in its own tests), where
    ``app.config`` may not exist or may need environment variables we do not
    have.
    """
    if settings is not None:
        return settings
    from_state = getattr(getattr(app, "state", None), "settings", None)
    if from_state is not None:
        return from_state
    try:
        from app.config import get_settings

        return get_settings()
    except Exception:  # noqa: BLE001 - settings are optional here, by design
        return None


def install_observability(app: FastAPI, settings: Any | None = None) -> None:
    """Install logging, metrics, request middleware and ``/metrics`` on ``app``.

    ``settings`` is optional and additive to the contract signature: pass one to
    pull ``LOG_LEVEL`` and register its secrets explicitly; omit it and the
    application state (then ``app.config``) is consulted.

    Calling this twice for the same app is a no-op. Calling it for two different
    apps in one process is also supported — the collectors are process-global,
    exactly as Prometheus requires, and ``Metrics`` is stateless, so the two apps
    feed the same series rather than fighting over the registry.
    """
    log = get_logger("app.observability")

    with _install_lock:
        if getattr(getattr(app, "state", None), _INSTALLED_ATTR, False):
            return

        resolved = _resolve_settings(app, settings)

        configure_logging(getattr(resolved, "log_level", "INFO"))

        registered = register_settings_secrets(resolved)
        fallback_active = ensure_system_collectors(REGISTRY)

        # A dependency provider looking for ``app.state.metrics`` — A1's
        # ``app.deps.get_metrics`` does exactly this — must find one even if the
        # lifespan has not published its own yet.
        if not isinstance(getattr(app.state, "metrics", None), Metrics):
            app.state.metrics = METRICS

        # Added before /metrics is mounted so the route exists when the first
        # scrape arrives. The middleware skips /metrics by path, so there is no
        # self-instrumentation.
        try:
            app.add_middleware(ObservabilityMiddleware)
        except RuntimeError as exc:
            # Starlette refuses middleware once the app has started. Report it
            # rather than pretending the request metrics exist.
            log.error(
                "observability.install_failed",
                error_type=type(exc).__name__,
                detail="middleware added after startup; request metrics disabled",
            )

        existing_paths = {
            getattr(route, "path", None) for route in getattr(app, "routes", [])
        }
        if "/metrics" not in existing_paths:
            app.add_route("/metrics", metrics_endpoint, include_in_schema=False)

        setattr(app.state, _INSTALLED_ATTR, True)

    log.info(
        "observability.installed",
        log_level=getattr(resolved, "log_level", "INFO"),
        # Not named "..._secrets_...": the redactor redacts any key whose name
        # looks credential-shaped, and it is right to. Field names cooperate.
        redaction_terms=registered,
        system_metrics_source="psutil" if fallback_active else "process_collector",
    )
    # The middleware binds and clears request context per request; make sure a
    # half-bound context from a previous (non-HTTP) install cannot leak in.
    clear_request_context()
