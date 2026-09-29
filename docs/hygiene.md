# Engineering hygiene record

Evidence for the participant brief **§18 (Security and Engineering Hygiene)** and
**§19 (Required Deliverables)**. This records what was removed, why, and what the
secrets and configuration audit found.

## 1. Throwaway artifacts removed

| Artifact | Verification before removal | Action |
| --- | --- | --- |
| `backend/probe_sim.py` | Docstring states "Throwaway capacity probe for the simulator. Not part of the deliverable." `grep -rn probe_sim` matched only the unrelated `_probe_simulator()` function in `app/api/health.py`; nothing imported or referenced the file. `backend/Dockerfile` `COPY`s only `app/`, so it was never in the image either. | `git rm` |
| `New Text Document.txt` (repo root) | Tracked stray file with a default Windows name. Its content was a 9-line draft of the `simulator-api` Compose service (image tag `asifmahmoud414/bup-fuel-supply-simulator:1.0.0`; `SIMULATION_SPEED`/`TICK_MINUTES`/`SIMULATOR_START_MODE`; `ports: "8001:8000"`). It is fully superseded by `docker-compose.yml`, which carries the same service with a variable host port (`${SIMULATOR_HOST_PORT:-8000}:8000`), a healthcheck and a `running` start mode. Nothing unique was lost. | `git rm` |

### Stale git worktree registration

`git worktree list` reported `.claude/worktrees/feat+frontend-ui-ux` as `locked`,
but the directory did not exist on disk. `git worktree list --porcelain` showed
the lock reason `claude session feat/frontend-ui-ux (pid 1864)`; that pid was no
longer running, and the recorded `gitdir` pointed at a non-existent location.

Repair, in order:

```
git worktree unlock .claude/worktrees/feat+frontend-ui-ux
git worktree prune -v
# -> Removing worktrees/feat+frontend-ui-ux: gitdir file points to non-existent location
```

`git worktree list` now reports only the main worktree.

**The branch was deliberately not deleted.** `worktree-feat+frontend-ui-ux`
holds 20 commits not reachable from `main` (frontend work with a diverged
history), so it is not "fully merged"; deleting it would discard unique work.
It is left in place for its owner to decide.

## 2. Secrets audit

**`.env` handling — correct.** `backend/.env` (which holds the real
`DEEPSEEK_API_KEY`) is **not tracked** by git, and **is ignored**:
`git check-ignore -v backend/.env` resolves to `.gitignore:54` (`backend/.env`),
with the general `.env` rule at `.gitignore:13` as a second line of defence. The
root `.env` is ignored by the same general rule.

**Tracked tree — no live credential.** A sweep of the tracked tree for `sk-`
keys, private keys, AWS key ids, GitHub tokens, and `password`/`secret`/`token`
literals found **no live credential**. Every hit is a synthetic fixture or a
placeholder:

| File:line | Value | Nature |
| --- | --- | --- |
| `README.md:291` | `sk-your-key-here` | Documentation placeholder |
| `backend/tests/test_api_support.py:41` | `sk-DO-NOT-LEAK-…` | Redaction-test fixture |
| `backend/tests/test_llm.py:65` | `sk-live-9f3c…` | Redaction-test fixture (name is misleading; 16 hex chars, not a real key format) |
| `backend/tests/test_llm.py:651,654,1112,1113,1119` | `sk-abcdef1234567890` | Redaction-test fixture |
| `backend/tests/test_observability.py:68` | `sk-test-DO-NOT-LOG-…` | Redaction-test fixture |
| `backend/tests/test_observability.py:677` | `sk-count-probe-…` | Metric-label-leak test fixture |

### Incident found and fixed during this pass

The **real** `DEEPSEEK_API_KEY` (redacted prefix `sk-40f4…`) had been written
into the committed template `backend/.env.example` (line 84). It matched the
value in `backend/.env` exactly. The line has been reset to `DEEPSEEK_API_KEY=`
(empty), which is what the template must carry.

Verified that the value never reached git: `git log --all -S<key>` returns no
commits, and `git grep <key> HEAD` returns nothing. The exposure was confined to
the working tree and was removed before any commit that contained it. If that
file was ever shared outside the working tree, rotate the key as a precaution.

## 3. Configuration documentation gap closed

`backend/app/config.py` was cross-checked against `backend/.env.example`. Every
setting the code reads was documented **except one**:

- **`SIMULATOR_CACHE_TTL_SECONDS`** (default `1.0`) — the read-cache lifetime and
  the window in which concurrent callers share one in-flight simulator fetch.
  Added to `backend/.env.example` with a safe placeholder and a comment
  explaining it is a load guard against the simulator's connection pool, and
  added the matching row to the README's direct-run configuration table.

No other setting was missing; the example carries no real values.

Out of scope but noted: `backend/loadtest/locustfile.py` reads `LOADTEST_HOST`,
`LOADTEST_ASSERT_SIMULATED` and `LOADTEST_RUN_TIME` from the environment. These
are load-test harness knobs (documented in `backend/loadtest/README.md`), not
service configuration, so they were not added to the service `.env.example`.

## 4. Deployment / run documentation corrected

The README and the Compose file disagreed about the simulator's host port:

- `docker-compose.yml` maps `"${SIMULATOR_HOST_PORT:-8000}:8000"`, and this
  checkout's root `.env` sets `SIMULATOR_HOST_PORT=8001`.
- The README's Docker quick-start table hard-coded the simulator as published on
  `8000`, and its direct-run instructions gave `8001` only as a hypothetical
  ("for example, if you started the stack with `SIMULATOR_HOST_PORT=8001`").

Fixed in `README.md`:

- The quick-start table now shows `${SIMULATOR_HOST_PORT}` (default `8000`), and
  the simulator's own dashboard/Swagger URLs use that variable, with a note that
  this deployment publishes `8001`.
- The direct-run section now states plainly that the backend must be pointed at
  host port **`8001`** on this deployment (`SIMULATOR_BASE_URL=http://localhost:8001`),
  with a pointer to `SIMULATOR_HOST_PORT` for other machines.

Not changed (outside this task's file ownership): the root `.env.example` still
ships `SIMULATOR_HOST_PORT=8000`, so a fresh clone defaults to `8000` unless its
`.env` sets `8001`. Flagged here for the owner.

## 5. Ignore-rule hardening

`.gitignore` and `backend/.dockerignore` gained narrow rules for throwaway
scratch scripts (`probe_*.py`, `bench_*.py`, `scratch/`), directly motivated by
the removed `backend/probe_sim.py`, so the next scratch file cannot be committed
or reach a build context by accident.

## Deliberately not changed

- `backend/.live_smoke_network.py` (a live-smoke script added by concurrent work)
  lives under `backend/`, outside this task's scope; it currently shows as
  deleted in the working tree and was left for its owner.
- The synthetic `sk-…` fixtures under `backend/tests/**` are legitimate
  redaction-test data, not credentials, and were left in place.
