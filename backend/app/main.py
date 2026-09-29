"""Application factory, lifespan and router mount (CONTRACT.md sections 1, 3, 9).

The backend is the **single gateway**: the console talks only to this service,
and this service talks to the simulator. That is what makes the platform one
origin and removes every CORS question — the simulator sends no CORS headers and
405s the preflight.

What this module owns
---------------------
* ``create_app()`` — assembles FastAPI, observability, CORS and the API router.
* The **lifespan** — builds and publishes the shared collaborators, then tears
  them down. Order matters: state is published *before* a start attempt, so a
  degraded simulator or database still leaves a servable application.

Degraded boot is deliberate, not accidental
-------------------------------------------
Brief section 11 requires the platform to keep working when a dependency is
unavailable. A failed ``Repository.init()`` or ``SimulatorClient.start()`` is
therefore logged and tolerated: the process still boots, ``/api/v1/health``
still answers, and ``/api/v1/status`` reports the component as degraded. Only a
*missing module* is fatal, and that is a build error rather than a runtime one.

Integration seams
-----------------
CONTRACT.md pins the method surface of every peer but leaves constructor shapes
unstated. Both call sites are confined to ``_build_repository`` and
``_mount_api``:

* ``Repository(database_url)`` — matches A3's ``app/store/repository.py``, whose
  first positional parameter is the URL (it also accepts a ``Database``).
* ``Metrics()`` — A9's ``app/observability/metrics.py`` is not yet present in the
  tree; if its constructor needs arguments, ``_startup`` is the one place to
  change.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.config import Settings, get_settings
from app.ingest import SnapshotIngestor
from app.observability.metrics import Metrics
from app.observability.setup import install_observability
from app.sim.client import SimulatorClient
from app.store.repository import Repository

__all__ = ["create_app", "app"]

log = structlog.get_logger(__name__)


# --------------------------------------------------------------------------
# Integration seams — the only places that assume a peer's unpinned shape.
# --------------------------------------------------------------------------
def _build_repository(settings: Settings) -> Repository:
    """Construct the storage repository.

    CONTRACT.md section 6 pins every ``Repository`` *method* but not its
    constructor. A3's implementation takes the database URL as its first
    positional parameter (``Repository(database_url)``) and resolves the rest
    via ``app.store.db.Database``, so the settings URL is handed over explicitly
    rather than re-read from the environment behind our back.
    """
    return Repository(settings.database_url)


def _mount_api(app: FastAPI) -> None:
    """Mount ``api_router`` with no additional prefix.

    A8's ``app/api/router.py`` declares the full section-9 paths on the
    sub-routers themselves (``@router.get("/api/v1/health")``, ``/metrics``) and
    its ``api_router`` carries no prefix, so the router must be included bare.
    Adding ``/api/v1`` here would produce ``/api/v1/api/v1/health``;
    ``test_no_double_api_prefix`` guards exactly that mistake.
    """
    app.include_router(api_router)


# --------------------------------------------------------------------------
# Lifespan
# --------------------------------------------------------------------------
async def _startup(app: FastAPI, settings: Settings) -> None:
    log.info(
        "startup.begin",
        simulator_base_url=settings.simulator_base_url,
        llm_available=settings.llm_available,
        database_url_scheme=settings.database_url.split("://", 1)[0],
    )

    metrics = Metrics()
    app.state.metrics = metrics

    repository = _build_repository(settings)
    app.state.repository = repository
    try:
        await repository.init()
    except Exception as exc:  # noqa: BLE001 - boot must survive a dead database
        # Brief section 11: "database unavailable -> serve from memory and mark
        # degraded". Only the exception *type* is logged; a driver message can
        # echo the URL, and a URL can carry a password (brief section 18).
        log.warning("startup.database_degraded", error_type=type(exc).__name__)

    simulator = SimulatorClient(settings, metrics)
    app.state.simulator_client = simulator
    try:
        await simulator.start()
    except Exception as exc:  # noqa: BLE001 - boot must survive a dead simulator
        log.warning("startup.simulator_degraded", error_type=type(exc).__name__)

    # The ingest task is what actually writes to the database: without it the
    # repository is inert, every table stays empty and the forecast layer reads
    # an empty series.  A failure to start it degrades ingestion, never boot.
    ingestor = SnapshotIngestor(simulator, repository, metrics=metrics)
    app.state.ingestor = ingestor
    try:
        await ingestor.start()
    except Exception as exc:  # noqa: BLE001 - boot must survive a dead ingest loop
        log.warning("startup.ingest_degraded", error_type=type(exc).__name__)

    log.info("startup.complete")


async def _shutdown(app: FastAPI) -> None:
    log.info("shutdown.begin")

    # Stop ingesting before its collaborators are torn down, so a poll that is
    # in flight cannot write through a closing repository or simulator.
    ingestor = getattr(app.state, "ingestor", None)
    if ingestor is not None:
        try:
            await ingestor.aclose()
        except Exception as exc:  # noqa: BLE001 - never block shutdown
            log.warning("shutdown.ingest_close_failed", error_type=type(exc).__name__)

    simulator = getattr(app.state, "simulator_client", None)
    if simulator is not None:
        try:
            await simulator.aclose()
        except Exception as exc:  # noqa: BLE001 - never block shutdown
            log.warning("shutdown.simulator_close_failed", error_type=type(exc).__name__)

    repository = getattr(app.state, "repository", None)
    if repository is not None:
        # CONTRACT.md section 6 pins no repository shutdown hook, so accept
        # either spelling rather than leaking the engine on exit.
        closer = getattr(repository, "aclose", None) or getattr(repository, "close", None)
        if closer is not None:
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 - never block shutdown
                log.warning("shutdown.repository_close_failed", error_type=type(exc).__name__)

    log.info("shutdown.complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the ASGI application.

    ``settings`` is injectable so tests (and the load test) can boot against a
    throwaway database and an unreachable simulator without touching the real
    environment.
    """
    resolved = settings if settings is not None else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await _startup(app, resolved)
        try:
            yield
        finally:
            await _shutdown(app)

    application = FastAPI(
        title="Fuel Supply Intelligence & Resilience Platform",
        version="0.1.0",
        description=(
            "Decision support for a **simulated** fuel supply network. "
            "Recommendations are advisory: the platform never executes an "
            "allocation automatically, and every predictive response is "
            "labelled `simulated: true`."
        ),
        lifespan=lifespan,
    )

    # Settings first: dependencies resolve from app.state, and a caller that
    # inspects the app before the lifespan runs still gets real configuration.
    application.state.settings = resolved

    # Logging, metrics, request middleware and /metrics (A9).
    install_observability(application)

    if resolved.cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=resolved.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    _mount_api(application)
    return application


#: Module-level ASGI target for ``uvicorn app.main:app``.
app = create_app()
