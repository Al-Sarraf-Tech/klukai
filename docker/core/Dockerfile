# syntax=docker/dockerfile:1.7
#
# Multi-stage build for klukai's companion-core.
#
# Stage 1: builder — installs build-time deps + creates a virtualenv with
#   all Python packages. Slow path; cached aggressively.
# Stage 2: runtime — copies just the venv + app source into a fresh
#   python:3.13-slim. No build tools, no apt cache, no pip cache.
#
# Image hardening notes (Phase 2 S+ uplift):
# - Base image SHA-pinned (immutable across rebuilds)
# - Non-root `appuser`
# - HEALTHCHECK matches docker-compose.yml so `docker run` works standalone
# - Cache mount on pip layer keeps rebuild fast without polluting image
# - Reproducible: SOURCE_DATE_EPOCH from build ARG (set by CI)
#
# Phase 4 stretch: switch runtime stage to `gcr.io/distroless/python3-debian12`.

# python:3.14-slim @ 2026-05-29 — aligned to the Python that requirements.txt is
# pip-compiled for (and that the full test suite runs on). Previously 3.13, which
# mismatched the 3.14 hash file: on 3.13 libcst pulls a transitive pyyaml-ft that
# --require-hashes then rejects. SHA-pinned (manifest-list digest) for reproducibility.
ARG PYTHON_IMAGE=python:3.14-slim@sha256:c845af9399020c7e562969a13689e929074a10fd057acd1b1fad06a2fb068e97

# ───────────────────────────────────────────────────────────────────
# Stage 1: builder
# ───────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Build-time deps only — present in builder, NOT in runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Create an isolated venv so we can COPY it cleanly to the runtime stage
RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# ───────────────────────────────────────────────────────────────────
# Stage 2: runtime
# ───────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Runtime needs curl for HEALTHCHECK; nothing else
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/* /var/cache/apt/* /var/log/apt/*

# Copy the prebuilt venv from builder
COPY --from=builder /opt/venv /opt/venv

# Non-root runtime user
RUN useradd --create-home --shell /bin/bash --uid 1000 appuser && \
    mkdir -p /images && chown appuser:appuser /images

# App source LAST so code changes only invalidate the small final layer
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser migrations/ ./migrations/
COPY --chown=appuser:appuser seed_memories.py .

USER appuser

EXPOSE 8300

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:8300/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8300", "--log-level", "info"]
