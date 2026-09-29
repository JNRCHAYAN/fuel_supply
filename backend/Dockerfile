# syntax=docker/dockerfile:1
#
# Backend image — Fuel Supply Intelligence & Resilience Platform.
#
#   docker build -t fuel-supply-backend ./backend
#   docker run --rm -p 9000:9000 --env-file backend/.env fuel-supply-backend
#
# Design notes, because each of these is load-bearing:
#
#   * Multi-stage. The builder installs the dependency tree into a virtualenv;
#     the runtime stage copies only that virtualenv. No pip cache, no wheel
#     build artefacts and no compiler survive into the shipped image.
#   * requirements.txt is copied and installed BEFORE the source is copied.
#     Docker caches layer by layer, so editing application code rebuilds only
#     the last layer and not a numpy/scipy install.
#   * The runtime user is not root. The build user is not root's group either.
#   * No secret is baked in. The DeepSeek key is read from the environment at
#     runtime and .dockerignore keeps .env out of the build context entirely
#     (brief §18; CONTRACT §0.2, §0.5).
#
# The base is pinned to the same interpreter the contract pins (CONTRACT §2:
# Python 3.12), so the image and the test suite run the same language version.

ARG PYTHON_VERSION=3.12

# ---------------------------------------------------------------------------
# Stage 1 — builder. Resolve and install dependencies, nothing else.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

# A self-contained virtualenv is the unit copied into the runtime stage. It is
# simpler to reason about than a --prefix install and keeps the interpreter
# layout intact.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /build

# This layer is only invalidated when requirements.txt changes. Everything the
# application source does afterwards reuses it.
COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

# Fail the build here, not at first request, if a pinned dependency cannot
# import under this interpreter.
RUN python -c "import fastapi, httpx, pydantic, sqlalchemy, numpy, scipy, prometheus_client, structlog, tenacity"

# ---------------------------------------------------------------------------
# Stage 2 — runtime. Only the virtualenv and the application.
# ---------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

LABEL org.opencontainers.image.title="fuel-supply-backend" \
      org.opencontainers.image.description="Fuel Supply Intelligence & Resilience Platform — API gateway, intelligence and observability. Simulated data only." \
      org.opencontainers.image.source="https://github.com/JNRCHAYAN/fuel_supply" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PATH="/opt/venv/bin:${PATH}" \
    API_PORT=9000

# An unprivileged system account, created numerically so a Kubernetes
# runAsNonRoot / runAsUser policy has something to match. --no-create-home
# because the application never needs a writable home directory.
RUN groupadd --system --gid 1001 app \
 && useradd --system --uid 1001 --gid app --no-create-home --shell /usr/sbin/nologin app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Source last: changes here invalidate only this layer.
COPY --chown=app:app app ./app

# The default DATABASE_URL is sqlite+aiosqlite:///./data/fuel.db, so the
# directory must exist and be writable by the unprivileged user before the
# engine opens it. (CONTRACT §4)
RUN mkdir -p /app/data && chown -R app:app /app/data

USER app

EXPOSE 9000

# Liveness against the cheap, dependency-free endpoint the contract specifies
# (CONTRACT §9). Deliberately not /api/v1/status: that endpoint probes the
# simulator, the database and the LLM, so an orchestrator would restart a
# perfectly healthy API because the simulator blipped.
#
# curl is not in the slim image and is not worth adding for this; urllib is in
# the standard library. A non-200 response or any connection error raises, and
# an uncaught exception exits non-zero, which is exactly "unhealthy".
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import os, sys, urllib.request; port = os.environ.get('API_PORT', '9000'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + port + '/api/v1/health', timeout=4).getcode() == 200 else 1)"]

# Host 0.0.0.0 so the port is reachable from outside the container; proxy
# headers are trusted because the only thing in front of this is the compose
# network / the console's nginx.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "9000", "--proxy-headers", "--forwarded-allow-ips", "*"]
