# trip-tracker

Self-hosted itinerary aggregator. Forwarded confirmation emails (flights, hotels,
rentals, trains, transfers, activities) → unified day-by-day timeline.

> **Status:** Phase 1 — skeleton + auth. Email ingestion lands in Phase 2.
> See [`docs/superpowers/specs/2026-04-26-trip-tracker-design.md`](docs/superpowers/specs/2026-04-26-trip-tracker-design.md) for the full spec.

## Quick start (local dev)

Requires Docker + `uv`.

```bash
git clone <repo> && cd trip-tracker
uv sync --all-groups
cp .env.example .env
# Fill in OIDC_* and SESSION_SECRET
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Visit http://localhost:8000.

## Development

```bash
uv run pytest            # tests (requires Postgres on PATH or use docker compose)
uv run ruff check .      # lint
uv run mypy              # types
uv run pre-commit run --all-files  # all hooks
```

## Production deploy

See `docker-compose.yml` — drop into your existing Traefik + Authelia Docker stack.
Configure forwardemail.net webhook → `/api/ingest/email` (Phase 2).
