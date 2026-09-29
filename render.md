# Deploying to Render

This guide walks you through deploying the Fuel Supply Intelligence &
Resilience Platform to [Render](https://render.com) using the `render.yaml`
Blueprint at the repository root. Nothing in the application is changed — the
existing Dockerfiles and configuration are reused as-is, so the deployed
platform is byte-for-byte equivalent to `docker compose up`.

> **TL;DR.** Push the repo to GitHub, then in Render create a New Blueprint
> pointing at it. Set `DEEPSEEK_API_KEY` (optional) on the backend service.
> Wait for the three services to deploy. Open the frontend's URL.

---

## 1. Prerequisites

- A Render account (free tier is enough for the simulator + frontend, the
  backend needs at least the Starter plan to attach a persistent disk for
  SQLite — see the in-memory workaround below if you want to stay on free).
- A GitHub/GitLab/Bitbucket repo containing this code. Render Blueprints read
  the `render.yaml` from the repository, so the file must be on the branch you
  connect.

## 2. One-time repo prep

No code changes are required. Render reads:

| File                         | What it does                                  |
| ---------------------------- | --------------------------------------------- |
| `render.yaml`                | Blueprint: declares the three services.       |
| `backend/Dockerfile`         | Backend image (unchanged).                    |
| `frontend/Dockerfile`        | Frontend image (unchanged).                   |
| `frontend/nginx/default.conf.template` | nginx config that env-substitutes upstreams. |
| `docker-compose.yml`         | Ignored by Render; kept for local dev.        |

The published BUP simulator image is referenced by tag in `render.yaml` and
is **never** rebuilt.

## 3. Create the Blueprint

1. In the Render Dashboard click **New +** → **Blueprint**.
2. Connect the GitHub/GitLab/Bitbucket repo that contains this code.
3. Render detects `render.yaml` and previews the three services it will
   create:
   - `fuel-supply-simulator` — Private Service, image only
   - `fuel-supply-backend`   — Web Service, Docker, port 9000
   - `fuel-supply-frontend`  — Web Service, Docker, port 80
4. Click **Apply**. Render creates all three services in parallel.

## 4. Set secrets

The only secret is `DEEPSEEK_API_KEY` on the `fuel-supply-backend` service.

1. Open the **fuel-supply-backend** service in the Dashboard.
2. Go to **Environment** → **Environment Variables**.
3. Add `DEEPSEEK_API_KEY` with your key. Leave it empty (delete the variable)
   to run with the deterministic templated explanation — that mode is fully
   supported and reports `source: "fallback"` so operators can see the
   difference.

If you skip this step the platform still deploys and works.

## 5. Wait for the deploy to finish

The frontend depends on the backend, the backend depends on the simulator.
Render handles that ordering automatically (Blueprint `dependsOn` is implicit
by name). The first build takes a few minutes because each service builds
its Docker image from scratch.

When everything is green:

| Service                       | URL (Render will show yours)         |
| ----------------------------- | ------------------------------------ |
| Operator console              | `https://fuel-supply-frontend.onrender.com` |
| Backend Swagger UI            | `https://fuel-supply-backend.onrender.com/docs` |
| Backend health                | `https://fuel-supply-backend.onrender.com/api/v1/health` |
| Simulator's own dashboard     | not exposed publicly (Private Service) |

Open the frontend URL. You should see the dashboard refresh every five
seconds with live data from the simulator.

---

## How the network is wired on Render

```
Browser ──▶ fuel-supply-frontend.onrender.com (nginx :443)
                │
                ├─ /api/*       ──▶ fuel-supply-backend.onrender.com ──▶ fuel-supply-simulator (private)
                │
                └─ /v1/*        ──▶ fuel-supply-simulator (private)
```

- The **frontend** exposes port 443 publicly (Render terminates TLS).
  Inside the container nginx listens on :80 and proxies to the two
  upstreams, whose URLs are env-substituted at boot.
- The **backend** exposes port 9000 publicly. Render routes 443 → 9000.
- The **simulator** is a Private Service — it has no public URL. Only the
  backend and frontend can reach it, over Render's internal DNS
  (`fuel-supply-simulator:8000`).

These URLs are set in `render.yaml`. They were chosen to match the
in-container service names the Dockerfiles and `docker-compose.yml` already
use, so nothing in the application had to change.

---

## Free-tier caveat — persistent disk

`render.yaml` declares a 1 GB persistent disk for the backend's SQLite
database. **Render's free Web Service plan does not support disks.** If you
choose the free plan the deploy will fail with an error like *"Disk is not
supported on free instances."*

Pick one of:

### Option A — In-memory database (free)

Edit the backend service in the Dashboard and replace the `DATABASE_URL`
value with:

```
sqlite+aiosqlite:///:memory:
```

Decision history and snapshots will not survive a redeploy. For a hackathon
demo that is fine — the simulator's own state is the source of truth and
is unaffected.

### Option B — External Postgres (recommended for production)

Render offers a managed PostgreSQL instance on the free plan. Create one
(Environment → New PostgreSQL), copy its **Internal Database URL**, and set:

```
DATABASE_URL=postgresql+asyncpg://<user>:<password>@<host>/<db>
```

The application already uses SQLAlchemy with an async driver, so the URL
swap is the only change needed.

### Option C — Upgrade the backend to Starter

The `render.yaml` already declares `plan: starter`. Pick that plan and the
disk mounts at `/app/data` and the SQLite file persists.

---

## Verifying the deployment

After the deploy finishes, smoke-test from a shell:

```bash
# Liveness — should always be 200.
curl -fsS https://fuel-supply-backend.onrender.com/api/v1/health

# Component status — may report degraded components on a cold start.
curl -fsS https://fuel-supply-backend.onrender.com/api/v1/status | jq

# Open the dashboard.
open https://fuel-supply-frontend.onrender.com
```

The dashboard polls `/api/v1/*` every five seconds and displays station
inventories, depot stocks, deliveries in flight, active disruptions and
component health.

---

## Updating the deployed stack

Push to the connected Git branch. Render rebuilds and redeploys only the
services whose files changed (Blueprint keeps the wiring). The simulator is
a pinned image, so it never rebuilds — to upgrade the simulator, edit
`render.yaml`'s `image:` tag and push.

## Tearing down

Render Dashboard → Blueprint → **Destroy**. All three services and the
backend's persistent disk are deleted. Decision history is gone.
