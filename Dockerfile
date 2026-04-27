# syntax=docker/dockerfile:1.10

# ---- Builder stage ----
FROM python:3.13.13-slim@sha256:d2462a6bed37b4fc6cabecf5a2132ae70df772fe03c7393c4d98a0c2fb48aa2e AS builder

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install uv from its release image (also digest-pinnable; using stable tag here is acceptable
# as the binary is verified by uv's self-update mechanism).
COPY --from=ghcr.io/astral-sh/uv:0.5.4@sha256:5436c72d52c9c0d011010ce68f4c399702b3b0764adcf282fe0e546f20ebaef6 /uv /uvx /usr/local/bin/

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
COPY tailwind.config.js ./
COPY scripts/build-tailwind.sh ./scripts/build-tailwind.sh
RUN bash scripts/build-tailwind.sh

# Install the project itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---- Runtime stage ----
FROM python:3.13.13-slim@sha256:d2462a6bed37b4fc6cabecf5a2132ae70df772fe03c7393c4d98a0c2fb48aa2e AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Run as non-root.
RUN groupadd -r app && useradd -r -g app -d /app app
WORKDIR /app

# Pull in just the venv + source from builder.
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY pyproject.toml ./
COPY alembic.ini ./
COPY migrations ./migrations

USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx; r = httpx.get('http://127.0.0.1:8000/healthz', timeout=3); raise SystemExit(0 if r.status_code == 200 else 1)"

# Run migrations then serve. Wrapper script keeps Dockerfile readable.
CMD ["sh", "-c", "alembic upgrade head && python -m trip_tracker"]
