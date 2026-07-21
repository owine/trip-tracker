# syntax=docker/dockerfile:1.25.0@sha256:0adf442eae370b6087e08edc7c50b552d80ddf261576f4ebd6421006b2461f12

# ---- Builder stage ----
FROM python:3.14.6-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # Build the venv against the base image's system Python, never a uv-managed
    # standalone interpreter under /root (unreachable by the non-root runtime
    # user). If `requires-python` ever drifts from the python base image tag,
    # this fails the build loudly instead of silently shipping a broken image.
    UV_PYTHON_PREFERENCE=only-system \
    UV_PYTHON_DOWNLOADS=never

# Install uv from its release image (also digest-pinnable; using stable tag here is acceptable
# as the binary is verified by uv's self-update mechanism).
COPY --from=ghcr.io/astral-sh/uv:0.11.30@sha256:93b61e21202b1dab861092748e46bbd6e0e41dd84f59b9174efd2353186e1b47 /uv /uvx /usr/local/bin/

WORKDIR /app

# Install OS deps needed by asyncpg / cryptography (already wheel-bundled, but keep build-essential
# available in case a transitive dep needs to compile). These are removed in the runtime stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Cache deps via mount.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy source + build Tailwind.
COPY src ./src
COPY README.md ./
COPY scripts/build-tailwind.sh ./scripts/build-tailwind.sh
RUN bash scripts/build-tailwind.sh

# Install the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Runtime stage ----
FROM python:3.14.6-slim@sha256:cea0e6040540fb2b965b6e7fb5ffa00871e632eef63719f0ea54bca189ce14a6 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Run as non-root.
RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app

# Pre-create the documents storage dir with `app` ownership. When a named
# volume mounts at /data, Docker initializes it from this directory's
# contents + perms — so the mounted volume is owned by `app` and writable.
# Without this, the worker (running as `app`) can't `mkdir /data/documents`
# at startup because root owns the volume's mount point by default.
RUN mkdir -p /data/documents && chown -R app:app /data

# Pull in just the venv + source from builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./
COPY alembic.ini ./
COPY migrations ./migrations

USER app

EXPOSE 8000

# Build-time identification. Placed AFTER all heavy COPY layers so a changing
# GIT_SHA / VERSION only invalidates this tiny ENV layer, not the venv or
# source layers above. Defaults let local builds work without needing args.
# VERSION is `github.ref_name` from CI: a semver tag like "v0.8.1" on tagged
# releases, or the branch name (e.g. "main") on branch pushes. Empty on local
# dev builds → Python falls back to trip_tracker.__version__ from pyproject.
ARG GIT_SHA=unknown
ARG VERSION=
ENV TRIP_TRACKER_GIT_SHA=$GIT_SHA
ENV TRIP_TRACKER_VERSION=$VERSION

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx; r = httpx.get('http://127.0.0.1:8000/healthz', timeout=3); raise SystemExit(0 if r.status_code == 200 else 1)"

# Run migrations then serve. Wrapper script keeps Dockerfile readable.
CMD ["sh", "-c", "alembic upgrade head && python -m trip_tracker"]
