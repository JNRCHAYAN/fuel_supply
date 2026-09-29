"""Ad-hoc live smoke check against the simulator at :8001 (read-only)."""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, r"C:\Users\Radhe\Pictures\fuel_supply\backend")
sys.path.insert(0, r"C:\Users\Radhe\Pictures\fuel_supply\backend\tests")

import app.api.network as net  # noqa: E402
from app.config import Settings  # noqa: E402
from app.sim.client import SimulatorClient  # noqa: E402
from test_api_support import build_app, client as tc, error_code  # noqa: E402

BASE = "http://127.0.0.1:8001"


async def direct() -> None:
    client = SimulatorClient(Settings(simulator_base_url=BASE))
    try:
        snap = await net.load_snapshot(client)
        print(
            "load_snapshot ->",
            "tick", snap.tick,
            "status", snap.status,
            "stale", snap.stale,
            "age", snap.age_seconds,
            "depots", len(snap.depots),
            "stations", len(snap.stations),
            "events", len(snap.events),
        )
        instance = await net.load_instance(client, snapshot=snap)
        print("load_instance ->", instance)
    finally:
        await client.aclose()


def through_the_api() -> None:
    app = build_app(client=SimulatorClient(Settings(simulator_base_url=BASE)))
    http = tc(app)
    response = http.get("/api/v1/network/snapshot")
    body = response.json()
    print(
        "GET /network/snapshot ->", response.status_code,
        "tick", body["tick"],
        "stale", body["stale"],
        "age", body["age_seconds"],
    )
    print("  instance ->", body["instance"])
    depot_id = body["depots"][0]["id"]
    station_id = body["stations"][0]["id"]
    print("  GET /depots/%s ->" % depot_id, http.get(f"/api/v1/depots/{depot_id}").status_code)
    print(
        "  GET /stations/%s ->" % station_id,
        http.get(f"/api/v1/stations/{station_id}").status_code,
    )
    unknown = http.get("/api/v1/depots/depot-nope")
    print("  GET /depots/depot-nope ->", unknown.status_code, error_code(unknown))
    unknown_station = http.get("/api/v1/stations/station-nope")
    print(
        "  GET /stations/station-nope ->",
        unknown_station.status_code,
        error_code(unknown_station),
    )


asyncio.run(direct())
through_the_api()
