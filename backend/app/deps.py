"""FastAPI dependency providers (CONTRACT.md section 3).

The application factory (:mod:`app.main`) constructs one instance of each
long-lived collaborator during its lifespan and publishes it on
``app.state``. The providers here hand those instances to route handlers, so
no route ever builds its own client, repository or metrics registry.

State names published by the lifespan (and read here):

======================  ==========================
``app.state.settings``  :class:`app.config.Settings`
``app.state.metrics``   :class:`app.observability.metrics.Metrics`
``app.state.repository``:class:`app.store.repository.Repository`
``app.state.simulator_client``  :class:`app.sim.client.SimulatorClient`
======================  ==========================

Every provider raises a typed :class:`RuntimeError` naming the missing
component when the lifespan did not run, rather than returning ``None`` and
turning a wiring mistake into an ``AttributeError`` deep inside a request.
"""

from __future__ import annotations

from typing import Any, TypeVar

from fastapi import Request

from app.config import Settings
from app.config import get_settings as _get_cached_settings
from app.observability.metrics import Metrics
from app.sim.client import SimulatorClient
from app.store.repository import Repository

__all__ = [
    "get_settings",
    "get_metrics",
    "get_repository",
    "get_simulator_client",
]

_T = TypeVar("_T")


def _from_state(request: Request, name: str, expected: type[_T]) -> _T:
    """Return ``app.state.<name>`` or fail loudly with the component's name."""
    value: Any = getattr(request.app.state, name, None)
    if isinstance(value, expected):
        return value
    raise RuntimeError(
        f"{name!r} is not initialised on the application state. "
        "The application lifespan did not run, or the factory was replaced "
        "by one that does not publish this component."
    )


def get_settings(request: Request) -> Settings:
    """Return the settings the running application was created with.

    Falls back to the process-wide cached settings for callers that are not
    attached to an app (for example a script or a bare ASGI shell).
    """
    settings: Any = getattr(request.app.state, "settings", None)
    if isinstance(settings, Settings):
        return settings
    return _get_cached_settings()


def get_metrics(request: Request) -> Metrics:
    """Return the process-wide Prometheus metrics registry wrapper."""
    return _from_state(request, "metrics", Metrics)


def get_repository(request: Request) -> Repository:
    """Return the snapshot/decision repository backed by ``DATABASE_URL``."""
    return _from_state(request, "repository", Repository)


def get_simulator_client(request: Request) -> SimulatorClient:
    """Return the shared simulator gateway client (breaker, cache, SSE)."""
    return _from_state(request, "simulator_client", SimulatorClient)
