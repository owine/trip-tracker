# trip-tracker

Self-hosted itinerary aggregator. Forwarded confirmation emails (flights, hotels,
rentals, trains, transfers, activities) → unified day-by-day timeline.

> **Status:** Phase 2 — email ingestion + manual itinerary entry.
> Phase 3 (per-vendor parsers) is next.
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

## Email forwarding setup (Phase 2)

1. Generate a webhook secret: `python -c 'import secrets; print(secrets.token_hex(32))'` and put it in `.env` as `WEBHOOK_SECRET=…`.
2. As admin, log in and go to `/admin/aliases` → "+ New". Create `<your-local-part>` mapped to your user (e.g. `oliver`).
3. In forwardemail.net's dashboard for `trips.<your-domain>`:
   - Add forwarding rule `oliver@trips.<your-domain>` → webhook URL `https://trips.<your-domain>/api/ingest/email`.
   - Configure HMAC secret to match `WEBHOOK_SECRET`.
   - Confirm the signature header name; if it's not `X-Webhook-Signature`, set `WEBHOOK_SIGNATURE_HEADER=` accordingly.
4. Test: forward yourself a confirmation email to `oliver@trips.<your-domain>`. Within seconds it appears at `/admin/raw-emails`.
5. Manually create a segment for it via `/segments/new` (parsers arrive in Phase 3).
