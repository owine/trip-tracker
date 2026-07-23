# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Self-hosted itinerary aggregator (TripIt replacement). Forwarded confirmation
emails → parsed segments → day-by-day trip timeline, with search, documents,
ICS feed, maps/weather, and expenses. Python 3.14, FastAPI, server-rendered
Jinja + Tailwind (no JS framework — Leaflet on map pages is the only exception).

`README.md` is the operator-facing manual (env var reference, Authelia OIDC
setup, ForwardEmail wiring, per-phase feature docs). Read it before changing
anything deploy-adjacent.

Versioning is not what `git tag` suggests: the package version is `0.8.1` and
Phase 9 shipped on `main` without a version bump. The `v0.9.0` tag is **not** an
ancestor of `main` — it marks an abandoned line of work, and Phase 9 was
re-landed on `main` separately. `pyproject.toml`'s `version` and `main`'s HEAD
are authoritative; don't reason from tags.

## Commands

```bash
uv sync --all-groups                  # install (dev group included)
uv run pytest                         # full suite
uv run pytest tests/test_worker.py    # one file
uv run pytest tests/test_worker.py::test_name -x       # one test
uv run pytest -k "dedup and not llm"  # by expression
uv run pytest --cov --cov-report=term # with coverage (fail_under = 85)
uv run ruff check . && uv run ruff format --check .
uv run mypy                           # strict; `files = ["src"]` — tests aren't typechecked
uv run djlint src/trip_tracker/templates --check
uv run pre-commit run --all-files
bash scripts/build-tailwind.sh        # regenerate static/tailwind.css (committed artifact)
```

Local dev stack (app + postgres + redis + meilisearch, port 8000):

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

Admin CLI subcommands (run inside the app container in prod):

```bash
python -m trip_tracker parse_pending [--max-emails=N] [--dry-run]  # re-enqueue stuck RawEmails
python -m trip_tracker reindex [--batch-size=N] [--dry-run]        # rebuild all 3 Meili indexes
```

Migrations: `uv run alembic revision --autogenerate -m "..."` then review. The
container runs `alembic upgrade head` before serving (see Dockerfile `CMD`).

### Running tests requires a real Postgres *server* on PATH

`tests/conftest.py` uses `pytest-postgresql`, which spawns its own ephemeral
cluster via `initdb`/`pg_ctl`. Client tools alone aren't enough — CI installs
`postgresql-18` (server) from PGDG for exactly this reason. Tests marked
`live_llm` are excluded by default via `addopts`.

## Architecture

### Two processes, one image

`docker-compose.yml` runs the same image twice:

- **app** — `python -m trip_tracker` → uvicorn → `create_app()` in `app.py`
- **worker** — `saq trip_tracker.worker.settings` → the task functions in `worker.py`

Config mirrors this split (`config.py`): `WorkerSettings` holds DB/Redis/LLM/
Meili/docs/logging; `Settings(WorkerSettings)` adds session/OIDC/webhook/upload
fields. The worker container is deliberately *not* given OIDC or session
secrets. Anything typed `WorkerSettings` accepts a `Settings`, never the
reverse — keep worker-side code typed against `WorkerSettings`.

`worker.py` instantiates `WorkerSettings()` at **module import time**, so a
missing worker env var is a boot failure, not a first-job failure.

### Storage roles

- **Postgres** — source of truth. Async SQLAlchemy 2.0 (`asyncpg`), declarative
  models in `models/`, Alembic migrations in `migrations/versions/`.
- **Meilisearch** — *derived* index only (`trips`, `segments`, `documents`).
  Never read from it for correctness; `reindex` rebuilds it from Postgres.
- **Redis** — saq job queue plus caches: weather forecasts (1h), FX rates (24h).

### The ingest → segment pipeline

1. `POST /api/ingest/email` (HMAC, `ingest/webhook.py`) or
   `POST /api/ingest/forwardemail` (shared-secret token, `ingest/forwardemail.py`)
   persists a `RawEmail` (dedup on `Message-ID` via `INSERT … ON CONFLICT`) and
   enqueues `parse_raw_email`.
2. `worker.parse_raw_email` resolves the recipient local-part → `ForwardingAlias`
   → owner user, persists PDF attachments, then calls `parsers.dispatch.dispatch_parse`.
3. `dispatch_parse` runs three strategies in order, keeping the best result:
   **JSON-LD** (`extruct`, ~0.95) → **vendor rule packs** (matched on the
   `From:` header, ~0.9) → **Anthropic Haiku tool-use** (clamped to 0.85, gated
   by a daily cent budget in `parsers/budget.py`).
4. `parsers/dedup.py` partitions drafts into already-seen vs fresh; all-duplicate
   → `parse_status='duplicate'`, partial → `'review'`.
5. `parsers/cluster.py` attaches each fresh draft to an existing trip
   (geo-distance via bundled `airports.csv`, ±1 day adjacency) or creates one.
6. `parse_status` lands as `parsed` / `review` / `duplicate` / `no_segments`.
   Anything below `LLM_CONFIDENCE_FLOOR` routes to `/inbox` for human confirmation.
7. Meili sync + document text extraction are enqueued as separate saq tasks
   **after** commit, so the picking worker can see the rows.

`effective_from()` (`parsers/forwarded.py`) unwraps user-forwarded mail so the
outer `From:` (the user) doesn't defeat vendor matching.

### Adding a vendor parser

Subclass `VendorParser` in `parsers/vendors/<name>/__init__.py` (registration is
automatic via `__init_subclass__`; you must define `name` and `sender_patterns`),
add the import to `parsers/vendors/__init__.py`, then drop a
`fixtures/<scenario>.eml` + `<scenario>.expected.json` pair.
`tests/test_parsers_vendors.py` discovers fixture pairs by globbing — **no test
code changes needed**.

### Web layer

Routers in `routes/` are plain FastAPI returning `HTMLResponse` from Jinja
templates. Each route module builds its own `Jinja2Templates` and must call
`templating.register_globals(templates)` so shared filters/globals (`money`,
`k_format`, `category_labels`, `app_version`) exist. Auth is cookie-session
(`itsdangerous`) established via Authelia OIDC; the guards live in
`auth/deps.py` — `require_user`, `require_admin`, `require_traveler`
(404s on soft-merged trips) and `require_traveler_including_merged` (read-only
handlers that must distinguish 410 from 404 — never use it on mutations).

## Conventions and landmines

- **Alembic owns all indexes.** Models deliberately omit `index=True`; adding it
  produces duplicate `ix_*` creation. Constraint names come from the naming
  convention in `models/base.py` so autogenerate diffs stay stable.
- **JSONB columns must be rebound, not mutated.** `raw.headers = {**raw.headers,
  ...}` — in-place mutation won't mark the column dirty.
- **`/api/ingest/forwardemail` must return 200.** ForwardEmail treats any other
  status (including 202) as failure and bounces the email back to the sender.
- **Money is integer minor units** (`bigint`) with `Decimal` math and a
  `numeric(20,10)` frozen `fx_rate`. Use `expenses/currencies.py::minor_digits`
  — JPY has 0 decimals, BHD has 3. FX is frozen at entry; if Frankfurter is
  unreachable with a cold cache the save 503s rather than storing a wrong rate.
- **`requires-python` is an exact pin** (`==3.14.6`) and must track the
  Dockerfile's `python:3.14.6-slim` base. Drift makes uv build the venv against
  a uv-managed interpreter under `/root`, unreachable by the non-root runtime
  user — a past production outage. `UV_PYTHON_PREFERENCE=only-system` in the
  builder stage now fails the build loudly instead.
- **Dependency pinning:** every runtime and dev dep is patch-pinned in
  `pyproject.toml`; GitHub Actions and Docker bases are SHA-pinned. Renovate
  (via the shared `owine/renovate-config` presets) does the bumping. Don't
  loosen a pin to fix a conflict.
- **pre-commit runs ruff/mypy/djlint through `uv run`** (`repo: local` hooks), so
  the hook version is always the `pyproject.toml` pin. Don't reintroduce mirror
  repos with their own `rev:` — that skew used to break CI after clean hook runs.
  Requires `uv sync` first.
- **Ruff targets `py313`, mypy targets `3.14`.** Ruff doesn't yet know py314;
  py313 is the safe subset. mypy is `strict = true` with `warn_unreachable`.
- **`filterwarnings = ["error"]`** — a new library DeprecationWarning fails the
  suite until explicitly ignored.
- **`tests/conftest.py` is shared infrastructure.** Autouse fixtures set every
  required env var and mock the saq queues, Meili client, and Redis so no test
  touches a real service. Editing it to make one test pass has broken hundreds
  of others; fix the test instead. Same for CI workflows, the Dockerfile, and
  the pre-commit config — treat them as out of scope unless that's the task.

## CI

`.github/workflows/ci.yml`: `lint`, `typecheck`, `test`, `security-python`
(bandit + pip-audit), `security-secrets` (gitleaks), `security-semgrep` all
gate `docker-build`. PRs build amd64 only with `push=false` and skip entirely
for doc-only changes (paths-filter); pushes to main build both arches, merge a
multi-arch manifest, cosign-sign it, and Trivy-scan the published digest.
GHCR package is **private** — pulls need auth.

Two semgrep `p/python` supply-chain rules are `--exclude-rule`'d because the
protections they check for live in the Renovate preset, not this repo. Don't
"fix" them by adding uv `exclude-newer` or a local `minimumReleaseAge`.

## Docs

Design specs and implementation plans live in
`docs/superpowers/specs/YYYY-MM-DD-<phase>-design.md` and
`docs/superpowers/plans/YYYY-MM-DD-<phase>.md`. The master spec is
`docs/superpowers/specs/2026-04-26-trip-tracker-design.md`; route and model
docstrings reference it by section (e.g. "Spec §6.4"). Operator setup for the
free-plan ForwardEmail path is `docs/forwardemail-setup.md`.
