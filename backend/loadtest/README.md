# Load testing

Brief §17 requires a measured workload against at least one meaningful
application path. This directory holds that workload.

## What is exercised

| Task | Weight | Endpoint | Why |
| --- | --- | --- | --- |
| `health` | 1 | `GET /api/v1/health` | Baseline. Cheap and dependency-free, so it isolates API overhead from work. |
| `recommendations` | 5 | `GET /api/v1/recommendations` | The meaningful path (CONTRACT §11). This one call composes the whole intelligence chain — snapshot → forecast → detect → optimise. |
| `recommendation_explanation` | 2 | `GET /api/v1/recommendations/{id}/explanation` | The end-to-end operator journey. Also load-tests the LLM path, which without a key is the deterministic fallback — i.e. the degraded path an operator actually sees. |

Everything here is **read-only**. The workload deliberately never calls
`POST /api/v1/recommendations/{id}/submit`: a load generator must not drive
allocations in the simulated world (brief §24, CONTRACT §0.7). Human review is
preserved for consequential decisions, including under load.

## Running it

Against the compose stack (backend on `9000`):

```bash
python -m locust -f backend/loadtest/locustfile.py --headless \
    --users 50 --spawn-rate 5 --run-time 2m \
    --host http://localhost:9000 \
    --csv backend/loadtest/results
```

The `--csv` flag writes raw per-endpoint statistics next to the printed
summary; `results_stats.csv` is the one to keep as evidence.

Interactive mode, for exploring a running stack rather than measuring it:

```bash
python -m locust -f backend/loadtest/locustfile.py --host http://localhost:9000
# open http://localhost:8089
```

Locust is already in `backend/requirements.txt`, so it is installed in the
project virtualenv — no extra install step.

### Configuration

All optional, all with defaults, so the same file runs against compose, a local
uvicorn, or CI without editing:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOADTEST_HOST` | `http://localhost:9000` | Target. `--host` overrides it. |
| `LOADTEST_USERS` | `50` | Used for the concurrency line in the summary when the runner does not report it. Set `--users` too. |
| `LOADTEST_SPAWN_RATE` | `5` | Documentation only; pass `--spawn-rate`. |
| `LOADTEST_RUN_TIME` | `2m` | Printed in the summary so the figures carry their workload. Pass `--run-time` too. |
| `LOADTEST_ASSERT_SIMULATED` | `1` | Set to `0` to stop failing responses that omit `simulated: true`. |

## Output

Locust's own table already contains p50/p95/p99. On top of it, the `quitting`
listener prints a summary block with:

- **throughput** — requests/second
- **error rate** — percentage
- **concurrency** — simulated operators
- **latency** — average, p50, p95, p99, max, overall and per endpoint

The summary prints the workload alongside the numbers on purpose. A p95 without
its concurrency and duration is not a measurement, and the results section of
`docs/architecture.md` is written to keep the two together.

### The `simulated: true` check

By default a response is counted as a **failure** if it omits `simulated: true`
(brief §24, CONTRACT §0.6). That is intentional: the flag is mandatory on
anything carrying a recommendation or prediction, and a load test is the
cheapest place to catch a regression that drops it. Override with
`LOADTEST_ASSERT_SIMULATED=0` if you are load-testing a build that predates the
flag.

## Reading the numbers

Some things to look for rather than just recording them:

- **p50 vs p95 spread.** `/health` should be flat and fast. Recommendations hits
  the optimiser, so it costs real CPU. A wide gap between the two rows means the
  optimiser is queueing behind the event loop rather than the API being slow.
- **Error rate under a raised `--users`.** A read path that starts returning 5xx
  at concurrency N is usually a connection-pool or simulator-timeout limit, not
  CPU. The backend degrades to a stale cached snapshot when the simulator is
  slow, so a sustained error rate here means the *backend* is the bottleneck.
- **The explanation row when no key is configured.** This measures the
  deterministic fallback. If it is materially slower than the recommendations
  row, the fallback is doing more work than it should.

## Results

> **To be measured.** The figures produced by this workload are recorded in
> [`docs/architecture.md`](../docs/architecture.md), section
> **"Load-test results"**. That section is a labelled placeholder until the
> stack has been run end to end: no latency figure in this repository is
> invented, and none will be added without the command line that produced it.
>
> Run the command above against a running stack, then paste the summary block
> and the workload line into that section.
