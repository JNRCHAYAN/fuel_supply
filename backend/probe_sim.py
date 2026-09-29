"""Throwaway capacity probe for the simulator. Not part of the deliverable."""
import asyncio
import time

import httpx

BASE = "http://127.0.0.1:8001"
PATHS = [
    "/v1/instance", "/v1/depots", "/v1/stations", "/v1/routes",
    "/v1/regions", "/v1/supply-arrivals", "/v1/events", "/v1/metrics",
]


async def one(c, i):
    p = PATHS[i % len(PATHS)]
    t = time.perf_counter()
    try:
        r = await c.get(BASE + p, timeout=30)
        return (r.status_code, time.perf_counter() - t)
    except Exception as e:
        return (type(e).__name__, time.perf_counter() - t)


async def run(n):
    async with httpx.AsyncClient() as c:
        t = time.perf_counter()
        res = await asyncio.gather(*[one(c, i) for i in range(n)])
        el = time.perf_counter() - t
    ok = sum(1 for s, _ in res if s == 200)
    lat = sorted(d for _, d in res)
    print(f"n={n:4d} wall={el:6.2f}s ok={ok}/{n} "
          f"p50={lat[len(lat) // 2] * 1000:7.1f}ms max={lat[-1] * 1000:7.1f}ms")
    bad = {}
    for s, _ in res:
        if s != 200:
            bad[s] = bad.get(s, 0) + 1
    if bad:
        print("      failures:", bad)


async def main():
    for n in (1, 8, 25, 50):
        await run(n)
        await asyncio.sleep(2)


asyncio.run(main())
