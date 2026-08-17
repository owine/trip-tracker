# syntax=docker/dockerfile:1.26.0@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# ---- Builder stage ----
FROM python:3.14.7-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS builder

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
COPY --from=ghcr.io/astral-sh/uv:0.12.3@sha256:2d890623d310b57771ce840f0da5eed5fc6d657da05ffaa45d82797b53fa3abc /uv /uvx /usr/local/bin/

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
FROM python:3.14.7-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4 AS runtime

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

# Drop the base image's system pip. Nothing at runtime needs it — the app runs
# entirely from /app/.venv (built by uv in the builder stage), and alembic/saq/
# uvicorn are venv console scripts. Removing it also removes pip's vendored
# tree and, with it, `pip/_vendor/bom.cdx.json`: pip 26.2.1 (new in
# python:3.14.7-slim) added that CycloneDX manifest declaring vendored
# msgpack 1.1.2 + setuptools 70.3.0, which syft records into the `sbom: true`
# build attestation and Trivy then reports as 2 HIGH findings — failing
# trivy-scan on every main push. The vendored code shipped in 3.14.6-slim too,
# just undeclared, so this is a reporting fix, not a new exposure. Deleting the
# package beats a .trivyignore (advisory IDs go stale when pip re-vendors) or a
# skip-files path (hardcodes python3.14, breaks silently at 3.15). The
# interpreter itself is untouched; use `python -m ensurepip` or `uv pip` if you
# ever need pip inside a running container. Placed before the COPY layers so it
# stays cached when only source changes. purelib is resolved at build time
# rather than hardcoded so this survives future minor-version bumps.
RUN SITE="$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')" \
    && rm -rf "$SITE"/pip "$SITE"/pip-*.dist-info \
       /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.* \
    && if python -c "import pip" 2>/dev/null; then echo "pip still importable" >&2; exit 1; fi

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
