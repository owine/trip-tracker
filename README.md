# trip-tracker

Self-hosted itinerary aggregator. Forwarded confirmation emails (flights, hotels,
rentals, trains, transfers, activities) → unified day-by-day timeline.

> **Status:** Phase 3 — automated parsing of forwarded emails into structured segments.
> Phase 4 (search + geocoding) is next.
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
5. The parser pipeline (Phase 3) auto-extracts a segment from the email and either auto-attaches it to an existing trip or creates a new one. Low-confidence extractions land in `/inbox` for manual confirmation.

## How parsers work (Phase 3)

When a forwarding email arrives at `/api/ingest/email`, a saq worker runs three
strategies in priority order:

1. **JSON-LD via extruct** — for emails that embed schema.org `FlightReservation`,
   `LodgingReservation`, `RentalCarReservation`, or `EventReservation`. Highest
   confidence (~0.95).
2. **Vendor rule packs** — Air France, American, United, Fairmont, Avis, National,
   Amtrak, SNCF, Uber, Blacklane. Each pack matches by `From:` header. Confidence
   ~0.9 on a successful match.
3. **Anthropic Haiku 4.5** — fallback for unknown senders. Tool-use schema mirrors
   the `SegmentDraft` shape. Self-rated confidence clamped at 0.85 so vendor packs
   added later naturally override the cached Haiku result.

Each strategy can return zero segments (with high confidence "no itinerary here")
or one or more. The dispatcher keeps the best result across strategies. Trip
clustering then attaches the segment(s) to an existing trip — geo-distance via
`airports.csv` (200km threshold) when both endpoints have coords, normalized
city-name match otherwise; ±1 day adjacency window — or creates a new trip with
auto-title `"{primary_destination} {month year}"`.

### Adding a new vendor

1. Create `src/trip_tracker/parsers/vendors/<name>/__init__.py` with a
   `VendorParser` subclass.
2. Add the import to `src/trip_tracker/parsers/vendors/__init__.py`.
3. Drop a fixture pair: `fixtures/<scenario>.eml` + `<scenario>.expected.json`.
4. CI's parameterized vendor test will pick up the new fixture automatically —
   no test code changes required.

### Daily LLM budget

Set `LLM_DAILY_BUDGET_CENTS` (default 100 = $1/day). When exceeded, RawEmails
skip the Haiku step and route to `parse_status='review'` for manual handling.

### Recovering after deploy

Existing `RawEmail` rows from before v0.3.0 sit in `parse_status='pending'`.
Reprocess them once after deploy:

```bash
docker compose exec trip-tracker-app python -m trip_tracker parse_pending
```

Idempotent — safe to re-run. Use `--max-emails=N` to cap the batch and
`--dry-run` to preview without enqueueing.
