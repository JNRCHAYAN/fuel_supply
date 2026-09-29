"""`api_router` — the single object `app.main` mounts.

CONTRACT.md section 3 requires this module to expose `api_router: APIRouter`.
The full paths from the section 9 table are declared on the routers themselves,
so `app.include_router(api_router)` must be called **without** a prefix.

Route table implemented here (CONTRACT.md section 9):

    GET  /api/v1/health                                  health.py
    GET  /api/v1/status                                  health.py
    GET  /api/v1/network/snapshot                        network.py
    GET  /api/v1/network/demand-history                  network.py
    GET  /api/v1/depots/{entity_id}                      network.py
    GET  /api/v1/stations/{entity_id}                    network.py
    GET  /api/v1/forecast                                intelligence.py
    GET  /api/v1/risk                                    intelligence.py
    GET  /api/v1/recommendations                         intelligence.py
    GET  /api/v1/recommendations/{id}/explanation        intelligence.py
    POST /api/v1/recommendations/{id}/submit             intelligence.py
    GET  /api/v1/allocations                             allocations.py
    POST /api/v1/allocations/{id}/cancel                 allocations.py
    GET  /api/v1/decisions                               decisions.py
    GET  /api/v1/decisions/{id}                          decisions.py
    GET  /api/v1/events                                  events.py
    POST /api/v1/events/summary                          events.py
    POST /api/v1/investigate                             events.py
    GET  /api/v1/admin/faults                            admin.py
    POST /api/v1/admin/faults                            admin.py
    POST /api/v1/admin/faults/clear                       admin.py
    GET  /api/v1/admin/audit                              admin.py
    GET  /api/v1/admin/events                             admin.py
    POST /api/v1/admin/events                             admin.py
    POST /api/v1/admin/toggle                             admin.py
    POST /api/v1/admin/simulation/{action}                admin.py
    GET  /metrics                                        health.py
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api import admin, allocations, decisions, events, health, intelligence, network

api_router: APIRouter = APIRouter()

api_router.include_router(health.router)
api_router.include_router(network.router)
api_router.include_router(intelligence.router)
api_router.include_router(allocations.router)
api_router.include_router(decisions.router)
api_router.include_router(events.router)
api_router.include_router(admin.router)

# The sub-routers are re-exported for convenience (`from app.api.router import
# health`); A1's main.py only needs `api_router`. A9's `install_observability`
# is invoked by A1, not here: the API does not own the middleware stack.
__all__ = [
    "api_router",
    "admin",
    "allocations",
    "decisions",
    "events",
    "health",
    "intelligence",
    "network",
]
