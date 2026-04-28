# Phase 2 — Ingestion v0 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up the email ingestion webhook (HMAC-verified raw MIME storage with replay protection) and the manual itinerary entry UI (per-type segment forms with implicit trip creation, plus admin alias and raw-email management) on top of the Phase 1 foundation.

**Architecture:** All in the existing FastAPI process — no new containers, no Redis, no ARQ. Single Alembic migration adds six tables. Webhook is synchronous; replay-cache and Message-ID dedupe via Postgres `INSERT … ON CONFLICT DO NOTHING`. Manual entry uses six per-type Jinja forms backed by per-type Pydantic models, with all writes (trip + traveler + segment) committed in a single transaction.

**Tech Stack:** Python 3.13, FastAPI, SQLAlchemy 2.0 async, Postgres 18, Pydantic v2 + pydantic-settings, Jinja2, structlog (already wired Phase 1), httpx (test client), pytest + pytest-asyncio + pytest-postgresql, Alembic.

**Spec reference:** [`docs/superpowers/specs/2026-04-27-phase2-ingestion-v0-design.md`](../specs/2026-04-27-phase2-ingestion-v0-design.md). Section numbers (e.g. §5 step 3) below refer to this spec.

**Branch:** `feat/phase-2-ingestion`. Cut from `main` at `b56d584` (or whatever is current `main` HEAD when implementation starts).

---

## File Structure

```
src/trip_tracker/
├── config.py                              [MODIFY: add webhook env + validators]
├── auth/
│   └── deps.py                            [MODIFY: add require_admin, require_traveler]
├── app.py                                 [MODIFY: include new routers, nav links]
├── ingest/                                [CREATE]
│   ├── __init__.py
│   ├── webhook.py                         APIRouter for POST /api/ingest/email
│   ├── hmac_verify.py                     verify_signature, record_nonce, prune_replay_cache
│   └── mime.py                            parse_mime(body) -> ParsedEmail
├── models/                                [CREATE 6 files]
│   ├── forwarding_alias.py
│   ├── trip.py
│   ├── trip_traveler.py
│   ├── segment.py
│   ├── raw_email.py
│   └── webhook_replay.py
├── routes/
│   ├── trips.py                           [CREATE]
│   ├── segments.py                        [CREATE]
│   └── admin.py                           [CREATE]
├── schemas/                               [CREATE]
│   ├── __init__.py
│   ├── segment_forms.py                   FlightSegmentForm, LodgingSegmentForm, ...
│   └── trip_forms.py                      TripForm, NewTripFromSegment
├── static/
│   └── iana_timezones.json                [CREATE: pre-rendered tz list]
└── templates/                             [CREATE many — see Task 12+]
    ├── base.html                          [MODIFY: nav]
    ├── trips/{list,detail,edit,_row}.html
    ├── segments/{type_picker,_common_fields,
    │             flight_form,lodging_form,car_form,
    │             train_form,transfer_form,activity_form,_row}.html
    └── admin/{alias_list,alias_form,
                raw_email_list,raw_email_detail}.html

migrations/versions/
└── YYYY_MM_DD_HHMM_<rev>_phase2_ingestion.py    [CREATE: one migration, six tables]

tests/
├── conftest.py                            [MODIFY: register new models with Base]
├── fixtures/
│   └── webhooks/
│       └── sample.eml                     [CREATE: real forwardemail-shaped MIME fixture]
├── test_config_webhook_validators.py      [CREATE]
├── test_models_forwarding_alias.py        [CREATE]
├── test_models_raw_email.py               [CREATE]
├── test_models_webhook_replay.py          [CREATE]
├── test_models_trip.py                    [CREATE]
├── test_models_trip_traveler.py           [CREATE]
├── test_models_segment.py                 [CREATE]
├── test_ingest_hmac.py                    [CREATE]
├── test_ingest_mime.py                    [CREATE]
├── test_ingest_webhook.py                 [CREATE]
├── test_auth_deps_admin.py                [CREATE]
├── test_routes_trips.py                   [CREATE]
├── test_routes_segments.py                [CREATE]
└── test_routes_admin.py                   [CREATE]
```

**Why this layout:** New subpackages `ingest/` and `schemas/` keep the webhook logic and form-validation logic isolated from the existing `auth/` and `routes/` namespaces. Each model file is one ORM class — small files Claude can hold in context. One Alembic migration since the FK graph is interlinked (segments→trips→users etc.) and creating piecemeal would require either a topological sort or temporary nullable FKs.

---

## Conventions Used Throughout This Plan

- **Commit messages:** Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`). Subject under 70 chars; body via heredoc when multi-line.
- **TDD:** Write failing test → run → see it fail for the *right reason* → implement minimum → re-run → green → commit.
- **Real Postgres in tests:** continues from Phase 1. `pytest-postgresql` runs an ephemeral instance.
- **Lifespan in tests:** any test that hits the DB through the app uses `async with app.router.lifespan_context(app):` (Phase 1 established this).
- **`uv` for everything.** No raw `pip`.
- **No new dependencies needed** — Phase 1's deps (`fastapi`, `sqlalchemy`, `pydantic`, `httpx`, `email` stdlib, `zoneinfo` stdlib, `hmac` stdlib) cover Phase 2.

---

## Task 1 — Settings + Webhook Env Vars + Static IANA Timezone Fixture

**Spec ref:** §5 (env table + validators), §6 (datetime UX).

**Files:**
- Modify: `src/trip_tracker/config.py`
- Modify: `tests/test_config.py`
- Create: `tests/test_config_webhook_validators.py`
- Create: `src/trip_tracker/static/iana_timezones.json`
- Modify: `.env.example`

- [ ] **Step 1.1 — Add the new fields + validators to `Settings`**

In `src/trip_tracker/config.py`, after the existing fields, add:

```python
import re

from pydantic import Field, SecretStr, field_validator


class Settings(BaseSettings):
    # ... existing fields ...

    webhook_secret: SecretStr = Field(...)
    webhook_signature_header: str = Field(default="X-Webhook-Signature")
    webhook_timestamp_tolerance_seconds: int = Field(default=300)
    webhook_max_body_bytes: int = Field(default=26_214_400)  # 25 MiB

    _RESERVED_HEADER_RE = re.compile(
        r"^(authorization|cookie|host|content-length|content-type|x-forwarded-.*)$",
        re.IGNORECASE,
    )

    @field_validator("webhook_signature_header")
    @classmethod
    def _validate_signature_header(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be empty")
        if cls._RESERVED_HEADER_RE.match(v):
            raise ValueError(f"{v!r} collides with a reserved/proxy header")
        return v

    @field_validator("webhook_timestamp_tolerance_seconds")
    @classmethod
    def _validate_tolerance(cls, v: int) -> int:
        if not 0 < v <= 3600:
            raise ValueError("must be in (0, 3600]")
        return v

    @field_validator("webhook_max_body_bytes")
    @classmethod
    def _validate_max_body(cls, v: int) -> int:
        if not 0 < v <= 100 * 1024 * 1024:
            raise ValueError("must be in (0, 100 MiB]")
        return v
```

Update `Settings.model_config` to load `WEBHOOK_*` env vars (it already reads from environment via `BaseSettings`).

- [ ] **Step 1.2 — Update `tests/conftest.py` autouse env so existing tests keep passing**

In `tests/conftest.py`, in `_set_required_env`, add:

```python
monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)
```

(Other webhook envs have defaults.)

- [ ] **Step 1.3 — Write failing test for the new validators**

Create `tests/test_config_webhook_validators.py`:

```python
"""Settings validators for webhook env vars."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-set required Phase 1 envs (autouse fixture cleared via per-test monkeypatch."""
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("SESSION_SECRET", "x" * 32)
    monkeypatch.setenv("OIDC_ISSUER", "https://auth.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "trip-tracker")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://trips.example.com/auth/callback")
    monkeypatch.setenv("BASE_URL", "https://trips.example.com")
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)


def test_signature_header_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    s = Settings()
    assert s.webhook_signature_header == "X-Webhook-Signature"


def test_signature_header_empty_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SIGNATURE_HEADER", "   ")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize(
    "header",
    ["Authorization", "cookie", "Host", "Content-Length", "X-Forwarded-For"],
)
def test_signature_header_reserved_rejected(
    monkeypatch: pytest.MonkeyPatch, header: str
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_SIGNATURE_HEADER", header)
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("seconds", [0, -1, 3601, 100_000])
def test_tolerance_out_of_range_rejected(
    monkeypatch: pytest.MonkeyPatch, seconds: int
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS", str(seconds))
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("size", [0, -1, 200 * 1024 * 1024])
def test_max_body_out_of_range_rejected(
    monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", str(size))
    with pytest.raises(ValidationError):
        Settings()
```

- [ ] **Step 1.4 — Run the new test, see it fail (validators not implemented)**

Run: `uv run pytest tests/test_config_webhook_validators.py -v`. Expect failures.

- [ ] **Step 1.5 — The validators from Step 1.1 should now make all tests pass**

Run: `uv run pytest tests/test_config_webhook_validators.py tests/test_config.py -v`. Expect all green.

- [ ] **Step 1.6 — Generate the IANA timezone fixture**

Create `src/trip_tracker/static/iana_timezones.json` with the filtered list. Generate via:

```bash
uv run python -c '
import json, zoneinfo
zones = sorted(
    z for z in zoneinfo.available_timezones()
    if "/" in z
    and not any(z.startswith(p) for p in ("Etc/", "posix/", "right/", "SystemV/"))
)
print(json.dumps(zones, indent=2))
' > src/trip_tracker/static/iana_timezones.json
```

Verify: `head src/trip_tracker/static/iana_timezones.json` shows entries like `"Africa/Abidjan"`. File should be ~400 lines.

- [ ] **Step 1.7 — Update `.env.example`**

Append to `.env.example`:

```bash

# --- Webhook (forwardemail.net) ---
WEBHOOK_SECRET=  # generate: python -c 'import secrets; print(secrets.token_hex(32))'
# Header name forwardemail.net signs with — confirm at config time, override only if needed:
# WEBHOOK_SIGNATURE_HEADER=X-Webhook-Signature
# WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS=300
# WEBHOOK_MAX_BODY_BYTES=26214400
```

- [ ] **Step 1.8 — Verify and commit**

```bash
uv run ruff check src tests
uv run mypy src
uv run pytest -q
git add src/trip_tracker/config.py tests/test_config_webhook_validators.py \
        tests/conftest.py src/trip_tracker/static/iana_timezones.json .env.example
git commit -m "feat(config): add webhook env vars + validators; ship IANA tz fixture"
```

---

## Task 2 — Single Alembic Migration: All Six Phase-2 Tables

**Spec ref:** §4 (full schema).

**Files:**
- Create: `migrations/versions/YYYY_MM_DD_HHMM_<rev>_phase2_ingestion.py`

This migration is hand-written (not autogenerated). Reasoning: the generated `tsvector` column on `segments` and the composite-PK on `webhook_replay_cache` need precise SQL, and we want every constraint named per `models/base.py`'s convention.

- [ ] **Step 2.1 — Scaffold the migration**

```bash
uv run alembic revision -m "phase2 ingestion"
```

Note the generated filename. Open the new file under `migrations/versions/`.

- [ ] **Step 2.2 — Replace migration body**

Replace the body with the following (keep the `revision`, `down_revision`, etc. metadata that alembic generated):

```python
"""phase2 ingestion

Revision ID: <generated>
Revises: 8e8121194c7d
Create Date: <generated>

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "<generated>"
down_revision: str | Sequence[str] | None = "8e8121194c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # forwarding_aliases
    op.create_table(
        "forwarding_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("local_part", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_forwarding_aliases")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_forwarding_aliases_user_id_users"),
                                ondelete="CASCADE"),
        sa.UniqueConstraint("local_part", name=op.f("uq_forwarding_aliases_local_part")),
    )

    # trips
    op.create_table(
        "trips",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("primary_destination", sa.String(255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cover_color", sa.String(16), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trips")),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"],
                                name=op.f("fk_trips_created_by_users"),
                                ondelete="RESTRICT"),
        sa.CheckConstraint("end_date >= start_date", name=op.f("ck_trips_date_range")),
    )

    # trip_travelers
    op.create_table(
        "trip_travelers",
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("trip_id", "user_id", name=op.f("pk_trip_travelers")),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"],
                                name=op.f("fk_trip_travelers_trip_id_trips"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                name=op.f("fk_trip_travelers_user_id_users"),
                                ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('owner', 'companion')",
                           name=op.f("ck_trip_travelers_role")),
    )

    # raw_emails (created BEFORE segments since segments.raw_email_id FKs into it)
    op.create_table(
        "raw_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("received_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("to_address", sa.String(320), nullable=False),
        sa.Column("from_address", sa.String(320), nullable=False),
        sa.Column("subject", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(998), nullable=False),
        sa.Column("mime_blob", sa.LargeBinary(), nullable=False),
        sa.Column("headers", postgresql.JSONB(), nullable=False),
        sa.Column("parse_status", sa.String(16),
                  server_default=sa.text("'pending'"), nullable=False),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_raw_emails")),
        sa.UniqueConstraint("message_id", name=op.f("uq_raw_emails_message_id")),
        sa.CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'failed', 'no_segments', 'review')",
            name=op.f("ck_raw_emails_parse_status"),
        ),
    )
    op.create_index("ix_raw_emails_received_at", "raw_emails",
                    [sa.text("received_at DESC")])
    op.create_index("ix_raw_emails_parse_status", "raw_emails", ["parse_status"])
    op.create_index("ix_raw_emails_to_address", "raw_emails", ["to_address"])

    # segments
    op.create_table(
        "segments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16),
                  server_default=sa.text("'confirmed'"), nullable=False),
        sa.Column("confirmation_number", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(128), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_tz", sa.String(64), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_tz", sa.String(64), nullable=True),
        sa.Column("start_location", postgresql.JSONB(), nullable=True),
        sa.Column("end_location", postgresql.JSONB(), nullable=True),
        sa.Column("details", postgresql.JSONB(),
                  server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("parse_source", sa.String(64), nullable=False),
        sa.Column("parse_confidence", sa.Float(precision=53), nullable=False),
        sa.Column("search_text", postgresql.TSVECTOR(),
                  sa.Computed(
                      "to_tsvector('simple'::regconfig, "
                      "coalesce(provider, '') || ' ' || "
                      "coalesce(confirmation_number, '') || ' ' || "
                      "coalesce(start_location ->> 'name', '') || ' ' || "
                      "coalesce(end_location   ->> 'name', '') || ' ' || "
                      "coalesce(start_location ->> 'city', '') || ' ' || "
                      "coalesce(end_location   ->> 'city', '')",
                      persisted=True,
                  ),
                  nullable=True),
        sa.Column("raw_email_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("superseded_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_segments")),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"],
                                name=op.f("fk_segments_trip_id_trips"),
                                ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"],
                                name=op.f("fk_segments_owner_user_id_users"),
                                ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["raw_email_id"], ["raw_emails.id"],
                                name=op.f("fk_segments_raw_email_id_raw_emails"),
                                ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["superseded_by"], ["segments.id"],
                                name=op.f("fk_segments_superseded_by_segments"),
                                ondelete="SET NULL"),
        sa.CheckConstraint(
            "type IN ('flight', 'lodging', 'car', 'train', 'transfer', 'activity')",
            name=op.f("ck_segments_type"),
        ),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'tentative')",
            name=op.f("ck_segments_status"),
        ),
        sa.CheckConstraint(
            "parse_confidence >= 0 AND parse_confidence <= 1",
            name=op.f("ck_segments_confidence_range"),
        ),
    )
    op.create_index("ix_segments_trip_id", "segments", ["trip_id"])
    op.create_index("ix_segments_owner_user_id_start_at", "segments",
                    ["owner_user_id", "start_at"])
    op.create_index("ix_segments_start_at", "segments", ["start_at"])
    op.create_index("ix_segments_search_text", "segments", ["search_text"],
                    postgresql_using="gin")

    # webhook_replay_cache
    op.create_table(
        "webhook_replay_cache",
        sa.Column("ts_seconds", sa.BigInteger(), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("ts_seconds", "nonce",
                                name=op.f("pk_webhook_replay_cache")),
    )
    op.create_index("ix_webhook_replay_cache_expires_at",
                    "webhook_replay_cache", ["expires_at"])


def downgrade() -> None:
    op.drop_table("webhook_replay_cache")
    op.drop_table("segments")
    op.drop_table("raw_emails")
    op.drop_table("trip_travelers")
    op.drop_table("trips")
    op.drop_table("forwarding_aliases")
```

Replace `<generated>` placeholders with the actual values alembic emitted (don't touch them).

- [ ] **Step 2.3 — Apply migration locally to verify**

Apply via the test fixture path (the test conftest will spin up a fresh PG and run `metadata.create_all` based on the SQLAlchemy models — but the *Alembic* path needs verification too):

```bash
docker run --rm -d --name tt-pg-tmp -e POSTGRES_PASSWORD=p -e POSTGRES_DB=tt -p 5433:5432 postgres:18-alpine
sleep 3
DATABASE_URL=postgresql+asyncpg://postgres:p@localhost:5433/tt \
SESSION_SECRET=$(python -c 'import secrets;print(secrets.token_hex(32))') \
WEBHOOK_SECRET=$(python -c 'import secrets;print(secrets.token_hex(32))') \
OIDC_ISSUER=https://x OIDC_CLIENT_ID=x OIDC_CLIENT_SECRET=x \
OIDC_REDIRECT_URI=https://x/cb BASE_URL=https://x \
uv run alembic upgrade head

# Verify tables exist
docker exec tt-pg-tmp psql -U postgres -d tt -c "\dt"
# Should list: forwarding_aliases, raw_emails, segments, trip_travelers, trips,
#              users, webhook_replay_cache, alembic_version

# Verify generated column works
docker exec tt-pg-tmp psql -U postgres -d tt -c "
INSERT INTO segments (id, trip_id, owner_user_id, type, status, provider,
    start_at, start_tz, details, parse_source, parse_confidence)
VALUES ('00000000-0000-0000-0000-000000000001'::uuid,
        '00000000-0000-0000-0000-000000000002'::uuid,
        '00000000-0000-0000-0000-000000000003'::uuid,
        'flight', 'confirmed', 'Delta', now(), 'America/New_York',
        '{}'::jsonb, 'manual', 1.0)
ON CONFLICT DO NOTHING;
" 2>&1 | head
# We expect the FK violation to fire (no users/trips rows) — that proves the
# constraints are wired. The point is the GENERATED column expression must
# parse without error during INSERT.

docker stop tt-pg-tmp
```

- [ ] **Step 2.4 — Commit**

```bash
git add migrations/versions/*phase2_ingestion.py
git commit -m "feat(db): Phase 2 migration — six tables (aliases, trips, segments, raw_emails, replay)"
```

---

## Task 3 — ORM Model: `ForwardingAlias`

**Spec ref:** §4 `forwarding_aliases`.

**Files:**
- Create: `src/trip_tracker/models/forwarding_alias.py`
- Modify: `src/trip_tracker/models/__init__.py`
- Modify: `tests/conftest.py` (register the model so `metadata.create_all` picks it up)
- Create: `tests/test_models_forwarding_alias.py`

- [ ] **Step 3.1 — Write the failing test**

Create `tests/test_models_forwarding_alias.py`:

```python
"""ForwardingAlias model: uniqueness, FK cascade."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.user import User


async def _user(db: AsyncSession, *, email: str = "u@example.com") -> User:
    u = User(oidc_subject=f"sub-{email}", email=email, display_name="U")
    db.add(u)
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_create_alias(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    alias = ForwardingAlias(user_id=user.id, local_part="oliver")
    db_session.add(alias)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(ForwardingAlias).where(ForwardingAlias.local_part == "oliver")
        )
    ).scalar_one()
    assert fetched.user_id == user.id


@pytest.mark.asyncio
async def test_local_part_unique(db_session: AsyncSession) -> None:
    u1 = await _user(db_session, email="a@example.com")
    u2 = await _user(db_session, email="b@example.com")
    db_session.add(ForwardingAlias(user_id=u1.id, local_part="dup"))
    await db_session.commit()
    db_session.add(ForwardingAlias(user_id=u2.id, local_part="dup"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_cascade_on_user_delete(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    db_session.add(ForwardingAlias(user_id=user.id, local_part="ada"))
    await db_session.commit()

    await db_session.delete(user)
    await db_session.commit()

    rows = (await db_session.execute(select(ForwardingAlias))).scalars().all()
    assert rows == []
```

- [ ] **Step 3.2 — Run, see ImportError**

Run: `uv run pytest tests/test_models_forwarding_alias.py -v`. Expect ImportError on `forwarding_alias`.

- [ ] **Step 3.3 — Implement the model**

Create `src/trip_tracker/models/forwarding_alias.py`:

```python
"""Forwarding alias: maps `<local_part>@trips.<domain>` → owner user."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class ForwardingAlias(Base):
    __tablename__ = "forwarding_aliases"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    local_part: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ForwardingAlias local_part={self.local_part!r} user_id={self.user_id}>"
```

- [ ] **Step 3.4 — Register in models package**

Edit `src/trip_tracker/models/__init__.py`:

```python
"""SQLAlchemy ORM models."""

from trip_tracker.models.forwarding_alias import ForwardingAlias  # noqa: F401
from trip_tracker.models.user import User  # noqa: F401
```

- [ ] **Step 3.5 — Update conftest so the model is registered before `metadata.create_all`**

In `tests/conftest.py`, the existing `db_session` fixture already imports `trip_tracker.models.user`. Replace that block with:

```python
import trip_tracker.models  # noqa: F401  -- registers all mappers via package __init__
```

(Future tasks add more models; the package import covers them all.)

- [ ] **Step 3.6 — Run tests, see green**

Run: `uv run pytest tests/test_models_forwarding_alias.py -v`. Expect 3 passing.

Run: `uv run pytest -q`. All prior tests still pass.

- [ ] **Step 3.7 — Commit**

```bash
git add src/trip_tracker/models/forwarding_alias.py \
        src/trip_tracker/models/__init__.py \
        tests/conftest.py \
        tests/test_models_forwarding_alias.py
git commit -m "feat(models): add ForwardingAlias"
```

---

## Task 4 — ORM Models: `RawEmail` + `WebhookReplay`

**Spec ref:** §4 `raw_emails`, `webhook_replay_cache`.

**Files:**
- Create: `src/trip_tracker/models/raw_email.py`
- Create: `src/trip_tracker/models/webhook_replay.py`
- Modify: `src/trip_tracker/models/__init__.py`
- Create: `tests/test_models_raw_email.py`
- Create: `tests/test_models_webhook_replay.py`

- [ ] **Step 4.1 — Write the failing tests**

Create `tests/test_models_raw_email.py`:

```python
"""RawEmail model: Message-ID uniqueness, parse_status check, jsonb headers."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.raw_email import RawEmail


@pytest.mark.asyncio
async def test_create_raw_email(db_session: AsyncSession) -> None:
    e = RawEmail(
        to_address="oliver@trips.example.com",
        from_address="confirmations@delta.com",
        subject="Your trip confirmation",
        message_id="<abc123@delta.com>",
        mime_blob=b"From: confirmations@delta.com\r\n\r\nbody",
        headers={"Subject": "Your trip confirmation"},
    )
    db_session.add(e)
    await db_session.commit()

    fetched = (
        await db_session.execute(
            select(RawEmail).where(RawEmail.message_id == "<abc123@delta.com>")
        )
    ).scalar_one()
    assert fetched.parse_status == "pending"
    assert fetched.headers["Subject"] == "Your trip confirmation"


@pytest.mark.asyncio
async def test_message_id_unique(db_session: AsyncSession) -> None:
    base = dict(
        to_address="oliver@trips.example.com",
        from_address="x@example.com",
        message_id="<dup@example.com>",
        mime_blob=b"",
        headers={},
    )
    db_session.add(RawEmail(**base))
    await db_session.commit()
    db_session.add(RawEmail(**base))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_parse_status_check(db_session: AsyncSession) -> None:
    e = RawEmail(
        to_address="o@x.com", from_address="f@x.com",
        message_id="<x@x.com>", mime_blob=b"", headers={},
        parse_status="bogus",
    )
    db_session.add(e)
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

Create `tests/test_models_webhook_replay.py`:

```python
"""WebhookReplay: composite PK enforcement."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.webhook_replay import WebhookReplay


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    db_session.add(
        WebhookReplay(
            ts_seconds=1_777_300_000,
            nonce="abc",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_composite_pk_conflict(db_session: AsyncSession) -> None:
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    db_session.add(WebhookReplay(ts_seconds=1, nonce="n", expires_at=expires))
    await db_session.commit()
    db_session.add(WebhookReplay(ts_seconds=1, nonce="n", expires_at=expires))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_same_ts_different_nonce_ok(db_session: AsyncSession) -> None:
    expires = datetime.now(timezone.utc) + timedelta(hours=24)
    db_session.add(WebhookReplay(ts_seconds=2, nonce="a", expires_at=expires))
    db_session.add(WebhookReplay(ts_seconds=2, nonce="b", expires_at=expires))
    await db_session.commit()
```

- [ ] **Step 4.2 — Run, see ImportError**

- [ ] **Step 4.3 — Implement `raw_email.py`**

Create `src/trip_tracker/models/raw_email.py`:

```python
"""RawEmail: stored MIME bytes for an incoming forwarded confirmation."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, LargeBinary, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class RawEmail(Base):
    __tablename__ = "raw_emails"
    __table_args__ = (
        CheckConstraint(
            "parse_status IN ('pending', 'parsed', 'failed', 'no_segments', 'review')",
            name="ck_raw_emails_parse_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # Note: NO `index=True` on these columns — the Alembic migration creates
    # the indexes with explicit names. Setting `index=True` here would cause
    # SQLAlchemy's metadata naming convention (from models/base.py) to also
    # auto-create an index, colliding with the migration on the same column.
    # `unique=True` on message_id is fine because Alembic creates the unique
    # *constraint* (named `uq_raw_emails_message_id`) which Postgres backs
    # with a btree — different name space from `ix_*` indexes.
    to_address: Mapped[str] = mapped_column(String(320), nullable=False)
    from_address: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id: Mapped[str] = mapped_column(String(998), unique=True, nullable=False)
    mime_blob: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    parse_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    parse_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 4.4 — Implement `webhook_replay.py`**

Create `src/trip_tracker/models/webhook_replay.py`:

```python
"""WebhookReplay: 24h replay-protection cache for ingest webhook."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String
from sqlalchemy.orm import Mapped, mapped_column


from trip_tracker.models.base import Base


class WebhookReplay(Base):
    __tablename__ = "webhook_replay_cache"

    ts_seconds: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    nonce: Mapped[str] = mapped_column(String(64), primary_key=True)
    # No `index=True` — same rule as RawEmail/Segment: the Alembic migration
    # owns `ix_*` index creation. The composite PK already covers (ts_seconds,
    # nonce) lookups; the explicit `ix_webhook_replay_cache_expires_at` exists
    # only to make the prune query fast.
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 4.5 — Register in models package**

Edit `src/trip_tracker/models/__init__.py`, append:

```python
from trip_tracker.models.raw_email import RawEmail  # noqa: F401
from trip_tracker.models.webhook_replay import WebhookReplay  # noqa: F401
```

- [ ] **Step 4.6 — Run tests, see green; commit**

```bash
uv run pytest tests/test_models_raw_email.py tests/test_models_webhook_replay.py -v
git add src/trip_tracker/models/raw_email.py src/trip_tracker/models/webhook_replay.py \
        src/trip_tracker/models/__init__.py \
        tests/test_models_raw_email.py tests/test_models_webhook_replay.py
git commit -m "feat(models): add RawEmail and WebhookReplay"
```

---

## Task 5 — ORM Models: `Trip` + `TripTraveler`

**Spec ref:** §4 `trips`, `trip_travelers`.

**Files:**
- Create: `src/trip_tracker/models/trip.py`
- Create: `src/trip_tracker/models/trip_traveler.py`
- Modify: `src/trip_tracker/models/__init__.py`
- Create: `tests/test_models_trip.py`
- Create: `tests/test_models_trip_traveler.py`

- [ ] **Step 5.1 — Failing tests**

`tests/test_models_trip.py`:

```python
"""Trip model: CRUD + date range CHECK + FK to creator."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


async def _user(db: AsyncSession) -> User:
    u = User(oidc_subject="creator", email="c@example.com", display_name="C")
    db.add(u)
    await db.commit()
    return u


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    t = Trip(
        title="Paris May 2026",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 8),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(t)
    await db_session.commit()
    assert t.id is not None


@pytest.mark.asyncio
async def test_date_range_check(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    t = Trip(
        title="Bad", start_date=date(2026, 5, 8), end_date=date(2026, 5, 1),
        created_by=user.id,
    )
    db_session.add(t)
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

`tests/test_models_trip_traveler.py`:

```python
"""TripTraveler: composite PK + role check + cascade."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _trip_with_user(db: AsyncSession) -> tuple[Trip, User]:
    u = User(oidc_subject="x", email="x@example.com", display_name="X")
    db.add(u)
    await db.flush()
    t = Trip(title="T", start_date=date(2026, 1, 1),
             end_date=date(2026, 1, 2), created_by=u.id)
    db.add(t)
    await db.commit()
    return t, u


@pytest.mark.asyncio
async def test_create(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_role_check(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="bogus"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_cascade_on_trip_delete(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()
    await db_session.delete(trip)
    await db_session.commit()
    rows = (await db_session.execute(select(TripTraveler))).scalars().all()
    assert rows == []
```

- [ ] **Step 5.2 — Implement `trip.py`**

Create `src/trip_tracker/models/trip.py`:

```python
"""Trip: a derived/explicit grouping of segments by date + destination."""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class Trip(Base):
    __tablename__ = "trips"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_trips_date_range"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    primary_destination: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    cover_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 5.3 — Implement `trip_traveler.py`**

Create `src/trip_tracker/models/trip_traveler.py`:

```python
"""TripTraveler: composite-PK join from trips × users with role."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class TripTraveler(Base):
    __tablename__ = "trip_travelers"
    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'companion')", name="ck_trip_travelers_role"
        ),
    )

    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

- [ ] **Step 5.4 — Register + green + commit**

Edit `src/trip_tracker/models/__init__.py`:

```python
from trip_tracker.models.trip import Trip  # noqa: F401
from trip_tracker.models.trip_traveler import TripTraveler  # noqa: F401
```

```bash
uv run pytest tests/test_models_trip.py tests/test_models_trip_traveler.py -v
git add src/trip_tracker/models/trip.py src/trip_tracker/models/trip_traveler.py \
        src/trip_tracker/models/__init__.py \
        tests/test_models_trip.py tests/test_models_trip_traveler.py
git commit -m "feat(models): add Trip and TripTraveler"
```

---

## Task 6 — ORM Model: `Segment` (with generated tsvector)

**Spec ref:** §4 `segments` (the table with the generated column).

**Files:**
- Create: `src/trip_tracker/models/segment.py`
- Modify: `src/trip_tracker/models/__init__.py`
- Create: `tests/test_models_segment.py`

- [ ] **Step 6.1 — Failing test**

`tests/test_models_segment.py`:

```python
"""Segment: per-type CRUD, jsonb roundtrip, generated search_text, FK + check constraints."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


async def _trip_with_user(db: AsyncSession) -> tuple[Trip, User]:
    u = User(oidc_subject="s", email="s@example.com", display_name="S")
    db.add(u)
    await db.flush()
    t = Trip(title="T", start_date=date(2026, 6, 1),
             end_date=date(2026, 6, 5), created_by=u.id)
    db.add(t)
    await db.commit()
    return t, u


@pytest.mark.asyncio
async def test_create_flight(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        provider="Delta",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        start_tz="America/New_York",
        end_at=datetime(2026, 6, 1, 21, 0, tzinfo=timezone.utc),
        end_tz="Europe/Paris",
        start_location={"iata": "JFK", "name": "JFK Airport", "city": "New York"},
        end_location={"iata": "CDG", "name": "Charles de Gaulle", "city": "Paris"},
        details={"flight_number": "DL44", "seat": "12A"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()
    await db_session.refresh(seg)

    assert seg.id is not None
    assert seg.start_location["iata"] == "JFK"
    assert seg.details["flight_number"] == "DL44"


@pytest.mark.asyncio
async def test_search_text_generated(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="flight", status="confirmed",
        provider="Delta", confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        start_tz="UTC",
        start_location={"name": "JFK", "city": "New York"},
        end_location={"name": "CDG", "city": "Paris"},
        parse_source="manual", parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    # Read raw tsvector via Postgres
    row = await db_session.execute(
        text("SELECT search_text::text FROM segments WHERE id = :id"),
        {"id": seg.id},
    )
    sv = row.scalar_one()
    # tsvector text format includes the lexemes; e.g. "'abc123':2 'cdg':4 ..."
    assert "delta" in sv.lower()
    assert "abc123" in sv.lower()
    assert "paris" in sv.lower()


@pytest.mark.asyncio
async def test_type_check(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="bogus",
        status="confirmed",
        start_at=datetime(2026, 6, 1, tzinfo=timezone.utc), start_tz="UTC",
        parse_source="manual", parse_confidence=1.0,
    )
    db_session.add(seg)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_confidence_range(db_session: AsyncSession) -> None:
    trip, user = await _trip_with_user(db_session)
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="flight", status="confirmed",
        start_at=datetime(2026, 6, 1, tzinfo=timezone.utc), start_tz="UTC",
        parse_source="manual", parse_confidence=1.5,
    )
    db_session.add(seg)
    with pytest.raises(IntegrityError):
        await db_session.commit()
```

- [ ] **Step 6.2 — Implement `segment.py`**

Create `src/trip_tracker/models/segment.py`:

```python
"""Segment: a single trip leg (flight, lodging, car, etc.). Spec §3."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


_SEARCH_TEXT_EXPR = (
    "to_tsvector('simple'::regconfig, "
    "coalesce(provider, '') || ' ' || "
    "coalesce(confirmation_number, '') || ' ' || "
    "coalesce(start_location ->> 'name', '') || ' ' || "
    "coalesce(end_location   ->> 'name', '') || ' ' || "
    "coalesce(start_location ->> 'city', '') || ' ' || "
    "coalesce(end_location   ->> 'city', ''))"
)


class Segment(Base):
    __tablename__ = "segments"
    __table_args__ = (
        CheckConstraint(
            "type IN ('flight', 'lodging', 'car', 'train', 'transfer', 'activity')",
            name="ck_segments_type",
        ),
        CheckConstraint(
            "status IN ('confirmed', 'cancelled', 'tentative')",
            name="ck_segments_status",
        ),
        CheckConstraint(
            "parse_confidence >= 0 AND parse_confidence <= 1",
            name="ck_segments_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # NO `index=True` on these columns — see RawEmail comment. Alembic owns
    # all `ix_*` index creation; ORM-level index=True would collide.
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="confirmed"
    )
    confirmation_number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(128), nullable=True)
    start_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    start_tz: Mapped[str] = mapped_column(String(64), nullable=False)
    end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    end_tz: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    end_location: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    parse_source: Mapped[str] = mapped_column(String(64), nullable=False)
    parse_confidence: Mapped[float] = mapped_column(Float(precision=53), nullable=False)
    # `Mapped[str | None]`: SQLAlchemy's TSVECTOR adapter round-trips as text.
    # Nullable in the ORM even though Postgres always populates it, because at
    # INSERT-time the Python side has no value to send and the generated value
    # is only visible after a refresh. Tests use raw SQL to read this column.
    search_text: Mapped[str | None] = mapped_column(
        TSVECTOR, Computed(_SEARCH_TEXT_EXPR, persisted=True), nullable=True
    )
    raw_email_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("raw_emails.id", ondelete="SET NULL"),
        nullable=True,
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("segments.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 6.3 — Register + green + commit**

```python
# src/trip_tracker/models/__init__.py — append
from trip_tracker.models.segment import Segment  # noqa: F401
```

```bash
uv run pytest tests/test_models_segment.py -v
git add src/trip_tracker/models/segment.py src/trip_tracker/models/__init__.py \
        tests/test_models_segment.py
git commit -m "feat(models): add Segment with generated search_text tsvector"
```

---

## Task 7 — `ingest/hmac_verify.py`: HMAC + Replay Cache Helpers

**Spec ref:** §5 step 2 (HMAC), step 4 (prune-gate), step 5 (record_nonce).

**Files:**
- Create: `src/trip_tracker/ingest/__init__.py` (empty `"""Ingest pipeline."""`)
- Create: `src/trip_tracker/ingest/hmac_verify.py`
- Create: `tests/test_ingest_hmac.py`

- [ ] **Step 7.1 — Failing tests**

`tests/test_ingest_hmac.py`:

```python
"""HMAC + replay cache primitives."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.ingest.hmac_verify import (
    PruneGate,
    prune_replay_cache,
    record_nonce,
    verify_signature,
)
from trip_tracker.models.webhook_replay import WebhookReplay


SECRET = b"x" * 32


def test_verify_signature_match() -> None:
    body = b"hello"
    sig = "sha256=" + hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, sig, SECRET) is True


def test_verify_signature_mismatch() -> None:
    body = b"hello"
    sig = "sha256=" + ("0" * 64)
    assert verify_signature(body, sig, SECRET) is False


def test_verify_signature_missing_prefix() -> None:
    body = b"hello"
    bare = hmac.new(SECRET, body, hashlib.sha256).hexdigest()
    assert verify_signature(body, bare, SECRET) is False


def test_verify_signature_empty_header() -> None:
    assert verify_signature(b"hello", "", SECRET) is False


@pytest.mark.asyncio
async def test_record_nonce_first_succeeds(db_session: AsyncSession) -> None:
    ok = await record_nonce(db_session, ts_seconds=1, nonce="a")
    await db_session.commit()
    assert ok is True


@pytest.mark.asyncio
async def test_record_nonce_conflict_returns_false(db_session: AsyncSession) -> None:
    await record_nonce(db_session, ts_seconds=1, nonce="b")
    await db_session.commit()
    ok = await record_nonce(db_session, ts_seconds=1, nonce="b")
    await db_session.commit()
    assert ok is False


@pytest.mark.asyncio
async def test_prune_replay_cache_removes_expired(db_session: AsyncSession) -> None:
    past = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.add(WebhookReplay(ts_seconds=10, nonce="old", expires_at=past))
    db_session.add(
        WebhookReplay(
            ts_seconds=11, nonce="fresh",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        )
    )
    await db_session.commit()

    deleted = await prune_replay_cache(db_session)
    await db_session.commit()
    assert deleted == 1

    rows = (await db_session.execute(select(WebhookReplay))).scalars().all()
    assert {r.nonce for r in rows} == {"fresh"}


def test_prune_gate_skip_within_window() -> None:
    gate = PruneGate(interval_seconds=60.0)
    t0 = time.monotonic()
    assert gate.should_prune(now=t0) is True
    assert gate.should_prune(now=t0 + 30) is False
    assert gate.should_prune(now=t0 + 61) is True
```

- [ ] **Step 7.2 — Run, see ImportError**

- [ ] **Step 7.3 — Implement `hmac_verify.py`**

Create `src/trip_tracker/ingest/__init__.py`:

```python
"""Ingest pipeline."""
```

Create `src/trip_tracker/ingest/hmac_verify.py`:

```python
"""HMAC verification + replay-cache primitives. Spec §5 steps 2, 4, 5."""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass, field

from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.webhook_replay import WebhookReplay


def verify_signature(body: bytes, header_value: str, secret: bytes) -> bool:
    """Constant-time HMAC-SHA256 verification.

    Header MUST be of form ``sha256=<64 hex chars>``. Returns False for any
    deviation (missing/empty header, missing prefix, mismatched digest).
    """
    if not header_value or not header_value.startswith("sha256="):
        return False
    provided_hex = header_value.removeprefix("sha256=")
    expected_hex = hmac.new(secret, body, hashlib.sha256).hexdigest()
    # compare_digest requires equal-length strings; both are 64 hex chars
    if len(provided_hex) != len(expected_hex):
        return False
    return hmac.compare_digest(provided_hex, expected_hex)


async def record_nonce(
    session: AsyncSession, *, ts_seconds: int, nonce: str
) -> bool:
    """Insert ``(ts_seconds, nonce)`` into webhook_replay_cache.

    Returns True if newly recorded, False if PK conflict (replay seen before).
    Caller is responsible for the surrounding transaction; this function does
    NOT commit. ``expires_at`` is set to now + 24h via SQL ``now()``.
    """
    stmt = (
        pg_insert(WebhookReplay)
        .values(
            ts_seconds=ts_seconds,
            nonce=nonce,
            expires_at=text("now() + interval '24 hours'"),
        )
        .on_conflict_do_nothing(index_elements=["ts_seconds", "nonce"])
    )
    result = await session.execute(stmt)
    return result.rowcount == 1


async def prune_replay_cache(session: AsyncSession) -> int:
    """Delete rows past ``expires_at``. Returns rows deleted."""
    result = await session.execute(
        delete(WebhookReplay).where(WebhookReplay.expires_at < text("now()"))
    )
    return result.rowcount or 0


@dataclass
class PruneGate:
    """Process-local 60s gate for opportunistic replay-cache pruning.

    Multi-worker uvicorn deploys get 1 prune per worker per minute, which is
    fine — pruning is hygiene, not correctness.
    """

    interval_seconds: float = 60.0
    _last: float = field(default=float("-inf"))

    def should_prune(self, *, now: float | None = None) -> bool:
        t = now if now is not None else time.monotonic()
        if t - self._last >= self.interval_seconds:
            self._last = t
            return True
        return False
```

- [ ] **Step 7.4 — Green + commit**

```bash
uv run pytest tests/test_ingest_hmac.py -v
uv run ruff check src tests && uv run mypy src
git add src/trip_tracker/ingest/__init__.py src/trip_tracker/ingest/hmac_verify.py \
        tests/test_ingest_hmac.py
git commit -m "feat(ingest): HMAC verification + replay cache primitives"
```

---

## Task 8 — `ingest/mime.py`: Parse Raw MIME

**Spec ref:** §5 step 6 (MIME parse + synthetic Message-ID).

**Files:**
- Create: `src/trip_tracker/ingest/mime.py`
- Create: `tests/fixtures/webhooks/sample.eml`
- Create: `tests/test_ingest_mime.py`

- [ ] **Step 8.1 — Create the fixture**

Create `tests/fixtures/webhooks/sample.eml` (raw forwardemail-shaped MIME):

```
From: Delta Air Lines <confirmations@delta.com>
To: oliver@trips.example.com
Subject: Your Trip Confirmation - DL44
Date: Mon, 27 Apr 2026 14:30:00 -0400
Message-ID: <abc123-confirm@delta.com>
MIME-Version: 1.0
Content-Type: multipart/alternative; boundary="boundary42"

--boundary42
Content-Type: text/plain; charset="utf-8"

Your flight DL44 from JFK to CDG is confirmed.

--boundary42
Content-Type: text/html; charset="utf-8"

<html><body><p>Your flight <b>DL44</b> from JFK to CDG is confirmed.</p></body></html>

--boundary42--
```

(Actual file uses CRLF line endings to match real MIME — see Step 8.5.)

- [ ] **Step 8.2 — Failing test**

`tests/test_ingest_mime.py`:

```python
"""MIME parsing helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from trip_tracker.ingest.mime import ParsedEmail, parse_mime

FIXTURE = Path(__file__).parent / "fixtures" / "webhooks" / "sample.eml"


def _body() -> bytes:
    return FIXTURE.read_bytes()


def test_parse_basic() -> None:
    parsed = parse_mime(_body())
    assert isinstance(parsed, ParsedEmail)
    assert parsed.message_id == "<abc123-confirm@delta.com>"
    assert parsed.to_address == "oliver@trips.example.com"
    assert parsed.from_address.endswith("confirmations@delta.com>") or \
           parsed.from_address == "confirmations@delta.com"
    assert parsed.subject == "Your Trip Confirmation - DL44"
    assert "Delta" in parsed.headers["From"]


def test_synthetic_message_id_when_missing() -> None:
    body = _body().replace(b"Message-ID: <abc123-confirm@delta.com>\r\n", b"")
    parsed = parse_mime(body)
    expected_hex = hashlib.sha256(body).hexdigest()
    assert parsed.message_id == f"<sha256:{expected_hex}@trip-tracker.local>"


def test_long_subject_handled() -> None:
    body = _body()
    parsed = parse_mime(body)
    assert parsed.subject is not None  # already short here
    # Real test: very long subject doesn't crash
    long = b"Subject: " + (b"x" * 2000) + b"\r\n"
    body2 = body.replace(b"Subject: Your Trip Confirmation - DL44\r\n", long)
    p2 = parse_mime(body2)
    assert p2.subject is not None and len(p2.subject) >= 500
```

- [ ] **Step 8.3 — Implement `mime.py`**

Create `src/trip_tracker/ingest/mime.py`:

```python
"""MIME parsing for ingested emails. Spec §5 step 6."""

from __future__ import annotations

import email
import email.policy
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    message_id: str
    to_address: str
    from_address: str
    subject: str | None
    headers: dict[str, str]
    body: bytes


def _str(v: object) -> str:
    """Coerce header value to a plain string with whitespace stripped."""
    return str(v).strip()


def parse_mime(body: bytes) -> ParsedEmail:
    """Parse raw MIME bytes; synthesize a Message-ID if missing.

    The synthetic Message-ID format is ``<sha256:<64 hex>@trip-tracker.local>``
    and is included verbatim (with angle brackets) in the returned struct so
    the caller can store it as-is.
    """
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(body)

    raw_msg_id = msg.get("Message-ID") or msg.get("Message-Id")
    if raw_msg_id:
        message_id = _str(raw_msg_id)
    else:
        digest = hashlib.sha256(body).hexdigest()
        message_id = f"<sha256:{digest}@trip-tracker.local>"

    headers: dict[str, str] = {}
    for key, value in msg.items():
        # Last-write-wins for duplicated headers; spec allows either.
        headers[key] = _str(value)

    return ParsedEmail(
        message_id=message_id,
        to_address=_str(msg.get("To") or ""),
        from_address=_str(msg.get("From") or ""),
        subject=_str(msg.get("Subject")) if msg.get("Subject") else None,
        headers=headers,
        body=body,
    )
```

- [ ] **Step 8.4 — Ensure CRLF line endings on the fixture**

```bash
# git config disables CRLF mangling on this path:
echo "tests/fixtures/webhooks/*.eml binary" >> .gitattributes
# Convert fixture to CRLF:
uv run python -c '
from pathlib import Path
p = Path("tests/fixtures/webhooks/sample.eml")
data = p.read_text().replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8")
p.write_bytes(data)
'
```

- [ ] **Step 8.5 — Green + commit**

```bash
uv run pytest tests/test_ingest_mime.py -v
git add src/trip_tracker/ingest/mime.py tests/test_ingest_mime.py \
        tests/fixtures/webhooks/sample.eml .gitattributes
git commit -m "feat(ingest): MIME parser with synthetic Message-ID fallback"
```

---

## Task 9 — `ingest/webhook.py`: The `POST /api/ingest/email` Endpoint

**Spec ref:** §5 (entire algorithm).

**Files:**
- Create: `src/trip_tracker/ingest/webhook.py`
- Modify: `src/trip_tracker/app.py` (include the router)
- Create: `tests/test_ingest_webhook.py`

- [ ] **Step 9.1 — Failing test**

`tests/test_ingest_webhook.py`:

```python
"""Webhook ingest end-to-end. Spec §5."""

from __future__ import annotations

import hashlib
import hmac
import time
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.config import Settings
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.webhook_replay import WebhookReplay


FIXTURE = Path(__file__).parent / "fixtures" / "webhooks" / "sample.eml"
SECRET = "x" * 32


def _sig(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _headers(body: bytes, *, ts: int | None = None, nonce: str = "n1") -> dict[str, str]:
    return {
        "Content-Type": "message/rfc822",
        "X-Webhook-Signature": _sig(body),
        "X-Webhook-Timestamp": str(ts if ts is not None else int(time.time())),
        "X-Webhook-Nonce": nonce,
    }


async def _post(
    app, body: bytes, headers: dict[str, str], db_url: str
) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            return await c.post("/api/ingest/email", content=body, headers=headers)


@pytest.mark.asyncio
async def test_happy_path_persists_raw_email(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    r = await _post(app, body, _headers(body, nonce="happy"), db_url)
    assert r.status_code == 202

    rows = (await db_session.execute(select(RawEmail))).scalars().all()
    assert len(rows) == 1
    assert rows[0].message_id == "<abc123-confirm@delta.com>"
    assert rows[0].mime_blob == body


@pytest.mark.asyncio
async def test_hmac_missing_returns_401(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h.pop("X-Webhook-Signature")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_hmac_no_prefix_returns_401(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h["X-Webhook-Signature"] = h["X-Webhook-Signature"].removeprefix("sha256=")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_body_too_big_returns_413(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("WEBHOOK_MAX_BODY_BYTES", "1024")  # tiny limit
    app = create_app()
    body = b"x" * 2048
    r = await _post(app, body, _headers(body, nonce="big"), db_url)
    assert r.status_code == 413


@pytest.mark.asyncio
async def test_timestamp_skew_returns_400(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, ts=int(time.time()) - 10_000, nonce="old")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_replay_returns_202_silently(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="rep")
    r1 = await _post(app, body, h, db_url)
    assert r1.status_code == 202
    r2 = await _post(app, body, h, db_url)
    assert r2.status_code == 202  # silent — not 200, not 409
    # Still only one row
    n = await db_session.execute(select(func.count()).select_from(RawEmail))
    assert n.scalar_one() == 1


@pytest.mark.asyncio
async def test_duplicate_message_id_returns_202_silently(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    r1 = await _post(app, body, _headers(body, nonce="dup-a"), db_url)
    assert r1.status_code == 202
    r2 = await _post(app, body, _headers(body, nonce="dup-b"), db_url)
    assert r2.status_code == 202
    n = await db_session.execute(select(func.count()).select_from(RawEmail))
    assert n.scalar_one() == 1


@pytest.mark.asyncio
async def test_missing_nonce_returns_400(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h.pop("X-Webhook-Nonce")
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_oversized_nonce_returns_400(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x" * 65)  # 65 > max 64
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_non_integer_timestamp_returns_400(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()
    h = _headers(body, nonce="x")
    h["X-Webhook-Timestamp"] = "not-a-number"
    r = await _post(app, body, h, db_url)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_unknown_alias_still_persists(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Spec §5: unknown alias = persist anyway, parse_status='pending'.
    Owner is derived lazily via JOIN at /admin/raw-emails query time.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes()  # to: oliver@trips.example.com — no alias for "oliver" yet
    r = await _post(app, body, _headers(body, nonce="orphan"), db_url)
    assert r.status_code == 202

    re = (await db_session.execute(select(RawEmail))).scalar_one()
    assert re.to_address == "oliver@trips.example.com"
    assert re.parse_status == "pending"


@pytest.mark.asyncio
async def test_missing_message_id_synthesizes(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Spec §5 step 6: missing Message-ID → synthesize <sha256:...@trip-tracker.local>."""
    import hashlib as _hashlib

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = FIXTURE.read_bytes().replace(
        b"Message-ID: <abc123-confirm@delta.com>\r\n", b""
    )
    expected_hex = _hashlib.sha256(body).hexdigest()
    r = await _post(app, body, _headers(body, nonce="synth"), db_url)
    assert r.status_code == 202

    re = (await db_session.execute(select(RawEmail))).scalar_one()
    assert re.message_id == f"<sha256:{expected_hex}@trip-tracker.local>"


@pytest.mark.asyncio
async def test_crlf_bom_body_round_trips_hmac(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BOM-prefixed CRLF body must HMAC-verify against the exact bytes sent.

    Validates that no middleware (uvicorn/ASGITransport) silently rewrites the
    request body. If this test fails, our HMAC math is computed over different
    bytes than the server sees.
    """
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", SECRET)
    app = create_app()
    body = b"\xef\xbb\xbf" + FIXTURE.read_bytes()  # UTF-8 BOM prefix
    r = await _post(app, body, _headers(body, nonce="bom"), db_url)
    assert r.status_code == 202


@pytest.fixture(autouse=True)
def _reset_prune_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """The webhook module owns a process-wide PruneGate singleton; reset it
    per-test so prune-frequency is deterministic and one test's prune doesn't
    silence the next test's expected prune.
    """
    from trip_tracker.ingest import webhook as wh

    monkeypatch.setattr(wh, "_PRUNE_GATE", wh.PruneGate(interval_seconds=60.0))
```

- [ ] **Step 9.2 — Implement `webhook.py`**

Create `src/trip_tracker/ingest/webhook.py`:

```python
"""POST /api/ingest/email — verify HMAC, dedupe, persist raw_email. Spec §5."""

from __future__ import annotations

import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ingest.hmac_verify import (
    PruneGate,
    prune_replay_cache,
    record_nonce,
    verify_signature,
)
from trip_tracker.ingest.mime import parse_mime
from trip_tracker.models.raw_email import RawEmail

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = structlog.get_logger(__name__)
_PRUNE_GATE = PruneGate(interval_seconds=60.0)


def _settings_dep() -> Settings:
    return Settings()


@router.post("/email", status_code=status.HTTP_202_ACCEPTED)
async def ingest_email(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(_settings_dep),
) -> Response:
    # Step 1: Streaming size cap.
    max_bytes = settings.webhook_max_body_bytes
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > max_bytes:
            return JSONResponse(
                {"error": "payload_too_large", "max_bytes": max_bytes},
                status_code=413,
            )
        chunks.append(chunk)
    body = b"".join(chunks)

    # Step 2: Verify HMAC.
    sig = request.headers.get(settings.webhook_signature_header) or ""
    secret_bytes = settings.webhook_secret.get_secret_value().encode()
    if not verify_signature(body, sig, secret_bytes):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Step 3: Verify timestamp + nonce.
    ts_raw = request.headers.get("X-Webhook-Timestamp") or ""
    nonce = (request.headers.get("X-Webhook-Nonce") or "").strip()
    try:
        ts = int(ts_raw)
    except ValueError:
        return JSONResponse({"error": "bad_request", "detail": "bad timestamp"}, 400)
    if not (1 <= len(nonce) <= 64):
        return JSONResponse({"error": "bad_request", "detail": "bad nonce"}, 400)
    skew = abs(int(time.time()) - ts)
    if skew > settings.webhook_timestamp_tolerance_seconds:
        return JSONResponse(
            {"error": "bad_request", "detail": "timestamp skew"}, 400
        )

    # Step 4: Periodic prune (best-effort). Run BEFORE opening the main txn so
    # this is a separate, committed unit of work — pruning is hygiene, not
    # correctness, and we don't want it tangled with the nonce-insert txn.
    # Note: bind the context manager to a name first because Python's `async
    # with X if cond else Y:` is a SyntaxError (the `with` statement does not
    # accept inline conditional expressions for the cm).
    if _PRUNE_GATE.should_prune():
        prune_cm = db.begin_nested() if db.in_transaction() else db.begin()
        async with prune_cm:
            await prune_replay_cache(db)

    # Parse MIME *before* opening the main txn so the structured logging line
    # at the bottom can reference `parsed` even if the txn body raises before
    # the INSERT — the parse itself is pure CPU and always safe.
    parsed = parse_mime(body)

    # Step 5–6: Single transaction for nonce-insert + raw_emails-insert.
    async with db.begin():
        recorded = await record_nonce(db, ts_seconds=ts, nonce=nonce)
        replay = not recorded

        stmt = (
            pg_insert(RawEmail)
            .values(
                to_address=parsed.to_address,
                from_address=parsed.from_address,
                subject=parsed.subject,
                message_id=parsed.message_id,
                mime_blob=body,
                headers=parsed.headers,
                parse_status="pending",
            )
            .on_conflict_do_nothing(index_elements=["message_id"])
        )
        result = await db.execute(stmt)
        duplicate = result.rowcount == 0 and not replay

    log.info(
        "ingest_webhook",
        status=202,
        to_address=parsed.to_address,
        from_address=parsed.from_address,
        message_id=parsed.message_id[:64],
        body_bytes=len(body),
        replay=replay,
        duplicate_message_id=duplicate,
    )

    # Step 7: Always 202 once HMAC + timestamp pass.
    return Response(status_code=202)
```

- [ ] **Step 9.3 — Wire router into `app.py`**

Edit `src/trip_tracker/app.py`. Add import:

```python
from trip_tracker.ingest.webhook import router as ingest_router
```

Inside `create_app`, after `app.include_router(auth_router)`:

```python
    app.include_router(ingest_router)
```

- [ ] **Step 9.4 — Update Phase 1 app-factory test**

`tests/test_app_factory.py` currently asserts the four auth routes. Add `/api/ingest/email`:

```python
    assert "/api/ingest/email" in routes
```

- [ ] **Step 9.5 — Green + commit**

```bash
uv run pytest tests/test_ingest_webhook.py tests/test_app_factory.py -v
uv run pytest -q
git add src/trip_tracker/ingest/webhook.py src/trip_tracker/app.py \
        tests/test_ingest_webhook.py tests/test_app_factory.py
git commit -m "feat(ingest): /api/ingest/email webhook handler with HMAC + dedupe"
```

---

## Task 10 — Auth Deps: `require_admin` + `require_traveler`

**Spec ref:** §6 routes table (auth column).

**Files:**
- Modify: `src/trip_tracker/auth/deps.py`
- Create: `tests/test_auth_deps_admin.py`

- [ ] **Step 10.1 — Failing test**

`tests/test_auth_deps_admin.py`:

```python
"""require_admin and require_traveler dependencies."""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_admin, require_traveler
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _user(*, admin: bool = False) -> User:
    return User(
        id=uuid.uuid4(), oidc_subject="s", email="x@y.com",
        display_name="X", is_admin=admin,
    )


@pytest.mark.asyncio
async def test_require_admin_allows_admin() -> None:
    user = _user(admin=True)
    out = await require_admin(user=user)
    assert out is user


@pytest.mark.asyncio
async def test_require_admin_blocks_non_admin() -> None:
    user = _user(admin=False)
    with pytest.raises(HTTPException) as ei:
        await require_admin(user=user)
    assert ei.value.status_code == 403


@pytest.mark.asyncio
async def test_require_traveler_allows_member(db_session: AsyncSession) -> None:
    creator = User(oidc_subject="c", email="c@x.com", display_name="C")
    db_session.add(creator)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2), created_by=creator.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=creator.id, role="owner"))
    await db_session.commit()

    out = await require_traveler(trip_id=trip.id, user=creator, db=db_session)
    assert out.id == trip.id


@pytest.mark.asyncio
async def test_require_traveler_404_for_non_member(db_session: AsyncSession) -> None:
    creator = User(oidc_subject="c2", email="c2@x.com", display_name="C")
    other = User(oidc_subject="o", email="o@x.com", display_name="O")
    db_session.add_all([creator, other])
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 1, 1),
                end_date=date(2026, 1, 2), created_by=creator.id)
    db_session.add(trip)
    await db_session.commit()

    with pytest.raises(HTTPException) as ei:
        await require_traveler(trip_id=trip.id, user=other, db=db_session)
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_require_traveler_404_for_unknown_trip(db_session: AsyncSession) -> None:
    user = _user()
    with pytest.raises(HTTPException) as ei:
        await require_traveler(trip_id=uuid.uuid4(), user=user, db=db_session)
    assert ei.value.status_code == 404
```

- [ ] **Step 10.2 — Implement deps**

Append to `src/trip_tracker/auth/deps.py`:

```python
import uuid as _uuid

from fastapi import Path
from sqlalchemy import select

from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler


async def require_admin(user: User = Depends(require_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return user


async def require_traveler(
    trip_id: _uuid.UUID = Path(...),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Trip:
    """Return the Trip if the current user is one of its travelers; else 404."""
    stmt = (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(Trip.id == trip_id, TripTraveler.user_id == user.id)
    )
    trip = (await db.execute(stmt)).scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return trip
```

- [ ] **Step 10.3 — Green + commit**

```bash
uv run pytest tests/test_auth_deps_admin.py -v
git add src/trip_tracker/auth/deps.py tests/test_auth_deps_admin.py
git commit -m "feat(auth): require_admin and require_traveler dependencies"
```

---

## Task 11 — Pydantic Form Schemas (Trip + Six Segment Types)

**Spec ref:** §6 Per-type segment form fields, §6 Datetime + tz UX.

**Files:**
- Create: `src/trip_tracker/schemas/__init__.py`
- Create: `src/trip_tracker/schemas/trip_forms.py`
- Create: `src/trip_tracker/schemas/segment_forms.py`
- Create: `tests/test_schemas_segment_forms.py`

- [ ] **Step 11.1 — Implement schemas**

Create `src/trip_tracker/schemas/__init__.py`: `"""Pydantic form schemas."""`

Create `src/trip_tracker/schemas/trip_forms.py`:

```python
"""Pydantic forms for trip CRUD."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, model_validator


class TripForm(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    start_date: date
    end_date: date
    primary_destination: str | None = Field(default=None, max_length=255)
    notes: str | None = None
    cover_color: str | None = Field(default=None, max_length=16)

    @model_validator(mode="after")
    def _date_range(self) -> "TripForm":
        if self.end_date < self.start_date:
            raise ValueError("end_date must be on or after start_date")
        return self
```

Create `src/trip_tracker/schemas/segment_forms.py`:

```python
"""Pydantic forms for per-type segment creation/edit. Spec §6."""

from __future__ import annotations

import uuid
import zoneinfo
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

SegmentType = Literal["flight", "lodging", "car", "train", "transfer", "activity"]
SegmentStatus = Literal["confirmed", "cancelled", "tentative"]


def _validate_iana(tz: str) -> str:
    if tz not in zoneinfo.available_timezones():
        raise ValueError(f"unknown IANA timezone: {tz!r}")
    return tz


# Trip selection: either an existing trip ID OR a new-trip title.
class TripSelector(BaseModel):
    existing_trip_id: uuid.UUID | None = None
    new_trip_title: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def _exactly_one(self) -> "TripSelector":
        has_existing = self.existing_trip_id is not None
        has_new = bool(self.new_trip_title and self.new_trip_title.strip())
        if has_existing == has_new:
            raise ValueError("provide exactly one of existing_trip_id or new_trip_title")
        return self


class _SegmentBase(BaseModel):
    """Common fields for all segment types."""
    trip_selector: TripSelector
    status: SegmentStatus = "confirmed"
    confirmation_number: str | None = Field(default=None, max_length=64)
    provider: str | None = Field(default=None, max_length=128)
    start_local: datetime
    start_tz: str
    end_local: datetime | None = None
    end_tz: str | None = None
    notes: str | None = None

    _validate_start_tz = field_validator("start_tz")(lambda v: _validate_iana(v))

    @field_validator("end_tz")
    @classmethod
    def _validate_end_tz(cls, v: str | None) -> str | None:
        return None if v is None else _validate_iana(v)


class FlightSegmentForm(_SegmentBase):
    type: Literal["flight"] = "flight"
    flight_number: str | None = Field(default=None, max_length=16)
    origin_iata: str | None = Field(default=None, min_length=3, max_length=4)
    origin_city: str | None = Field(default=None, max_length=128)
    destination_iata: str | None = Field(default=None, min_length=3, max_length=4)
    destination_city: str | None = Field(default=None, max_length=128)
    seat: str | None = Field(default=None, max_length=16)


class LodgingSegmentForm(_SegmentBase):
    type: Literal["lodging"] = "lodging"
    hotel_name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=128)
    country: str | None = Field(default=None, max_length=128)
    room_type: str | None = Field(default=None, max_length=64)


class CarSegmentForm(_SegmentBase):
    type: Literal["car"] = "car"
    pickup_location: str = Field(min_length=1, max_length=255)
    pickup_city: str | None = Field(default=None, max_length=128)
    dropoff_location: str = Field(min_length=1, max_length=255)
    dropoff_city: str | None = Field(default=None, max_length=128)
    car_class: str | None = Field(default=None, max_length=64)


class TrainSegmentForm(_SegmentBase):
    type: Literal["train"] = "train"
    train_number: str | None = Field(default=None, max_length=32)
    origin_station: str = Field(min_length=1, max_length=255)
    destination_station: str = Field(min_length=1, max_length=255)
    seat: str | None = Field(default=None, max_length=32)


class TransferSegmentForm(_SegmentBase):
    type: Literal["transfer"] = "transfer"
    pickup_location: str = Field(min_length=1, max_length=255)
    dropoff_location: str = Field(min_length=1, max_length=255)


class ActivitySegmentForm(_SegmentBase):
    type: Literal["activity"] = "activity"
    venue_name: str = Field(min_length=1, max_length=255)
    address: str | None = Field(default=None, max_length=512)
    city: str | None = Field(default=None, max_length=128)


SegmentForm = Annotated[
    FlightSegmentForm | LodgingSegmentForm | CarSegmentForm
    | TrainSegmentForm | TransferSegmentForm | ActivitySegmentForm,
    Field(discriminator="type"),
]
```

- [ ] **Step 11.2 — Tests**

`tests/test_schemas_segment_forms.py`:

```python
"""Pydantic segment form schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from pydantic import ValidationError

from trip_tracker.schemas.segment_forms import (
    FlightSegmentForm,
    LodgingSegmentForm,
    TripSelector,
)


def test_flight_form_minimal() -> None:
    f = FlightSegmentForm(
        trip_selector=TripSelector(new_trip_title="Trip"),
        start_local=datetime(2026, 6, 1, 9, 0),
        start_tz="America/New_York",
    )
    assert f.type == "flight"


def test_unknown_tz_rejected() -> None:
    with pytest.raises(ValidationError):
        FlightSegmentForm(
            trip_selector=TripSelector(new_trip_title="T"),
            start_local=datetime(2026, 6, 1),
            start_tz="Mars/Olympus",
        )


def test_trip_selector_requires_one() -> None:
    with pytest.raises(ValidationError):
        TripSelector()  # neither
    with pytest.raises(ValidationError):
        TripSelector(existing_trip_id=uuid.uuid4(), new_trip_title="X")  # both


def test_lodging_requires_hotel_name() -> None:
    with pytest.raises(ValidationError):
        LodgingSegmentForm(
            trip_selector=TripSelector(new_trip_title="T"),
            start_local=datetime(2026, 6, 1),
            start_tz="UTC",
            hotel_name="",
        )
```

- [ ] **Step 11.3 — Green + commit**

```bash
uv run pytest tests/test_schemas_segment_forms.py -v
git add src/trip_tracker/schemas/ tests/test_schemas_segment_forms.py
git commit -m "feat(schemas): per-type segment form validation"
```

---

## Task 12 — Trips Routes + Templates

**Spec ref:** §6 routes table, trip-list ordering subsection.

**Files:**
- Create: `src/trip_tracker/routes/trips.py`
- Create: `src/trip_tracker/templates/trips/list.html`
- Create: `src/trip_tracker/templates/trips/detail.html`
- Create: `src/trip_tracker/templates/trips/edit.html`
- Create: `src/trip_tracker/templates/trips/_row.html`
- Modify: `src/trip_tracker/templates/base.html` (nav link)
- Modify: `src/trip_tracker/app.py` (include router)
- Create: `tests/test_routes_trips.py`

- [ ] **Step 12.1 — Failing test**

`tests/test_routes_trips.py`:

```python
"""Trips routes: list/detail/edit/delete with traveler scoping."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _user(db: AsyncSession, *, email: str = "u@example.com") -> User:
    u = User(oidc_subject=f"sub-{email}", email=email, display_name="U")
    db.add(u)
    await db.commit()
    return u


async def _trip(db: AsyncSession, owner: User, **overrides) -> Trip:
    fields = dict(
        title="Default", start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5), created_by=owner.id,
    )
    fields.update(overrides)
    t = Trip(**fields)
    db.add(t)
    await db.flush()
    db.add(TripTraveler(trip_id=t.id, user_id=owner.id, role="owner"))
    await db.commit()
    return t


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_list_only_shows_user_trips(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    me = await _user(db_session, email="me@x.com")
    other = await _user(db_session, email="other@x.com")
    mine = await _trip(db_session, me, title="My Trip")
    await _trip(db_session, other, title="Other Trip")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(me, settings)
        ) as c:
            r = await c.get("/trips")
    assert r.status_code == 200
    assert "My Trip" in r.text
    assert "Other Trip" not in r.text


@pytest.mark.asyncio
async def test_detail_404_for_non_traveler(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    creator = await _user(db_session, email="c@x.com")
    other = await _user(db_session, email="o@x.com")
    trip = await _trip(db_session, creator)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(other, settings)
        ) as c:
            r = await c.get(f"/trips/{trip.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_edit_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    me = await _user(db_session)
    trip = await _trip(db_session, me)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(me, settings)
        ) as c:
            r = await c.post(
                f"/trips/{trip.id}",
                data={
                    "title": "Updated",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-10",
                    "primary_destination": "Paris",
                },
                follow_redirects=False,
            )
    assert r.status_code == 303
    await db_session.refresh(trip)
    assert trip.title == "Updated"
```

- [ ] **Step 12.2 — Implement `routes/trips.py`**

Create `src/trip_tracker/routes/trips.py`:

```python
"""Trips routes: list, detail, edit, delete. Spec §6."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_traveler, require_user
from trip_tracker.db import get_session
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.schemas.trip_forms import TripForm

router = APIRouter(prefix="/trips", tags=["trips"])

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("", response_class=HTMLResponse)
async def list_trips(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    today = date.today()
    is_past = case((Trip.end_date < today, 1), else_=0)
    stmt = (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(TripTraveler.user_id == user.id)
        .order_by(
            is_past.asc(),
            case((Trip.end_date >= today, Trip.start_date), else_=None).asc(),
            case((Trip.end_date < today, Trip.start_date), else_=None).desc(),
        )
    )
    trips = (await db.execute(stmt)).scalars().all()
    return templates.TemplateResponse(
        request, "trips/list.html", {"trips": trips, "user": user}
    )


@router.get("/{trip_id}", response_class=HTMLResponse)
async def trip_detail(
    request: Request,
    trip: Trip = Depends(require_traveler),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    from trip_tracker.models.segment import Segment

    segments = (
        await db.execute(
            select(Segment).where(Segment.trip_id == trip.id).order_by(Segment.start_at)
        )
    ).scalars().all()
    return templates.TemplateResponse(
        request, "trips/detail.html",
        {"trip": trip, "segments": segments, "user": user},
    )


@router.get("/{trip_id}/edit", response_class=HTMLResponse)
async def edit_trip_form(
    request: Request,
    trip: Trip = Depends(require_traveler),
    user: User = Depends(require_user),
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "trips/edit.html", {"trip": trip, "user": user, "errors": {}}
    )


@router.post("/{trip_id}")
async def update_trip(
    request: Request,
    trip: Trip = Depends(require_traveler),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
    title: str = Form(...),
    start_date: date = Form(...),
    end_date: date = Form(...),
    primary_destination: str | None = Form(None),
    notes: str | None = Form(None),
    cover_color: str | None = Form(None),
):
    try:
        form = TripForm(
            title=title, start_date=start_date, end_date=end_date,
            primary_destination=primary_destination, notes=notes,
            cover_color=cover_color,
        )
    except Exception as e:
        return templates.TemplateResponse(
            request, "trips/edit.html",
            {"trip": trip, "user": user, "errors": {"_form": str(e)}},
        )
    trip.title = form.title
    trip.start_date = form.start_date
    trip.end_date = form.end_date
    trip.primary_destination = form.primary_destination
    trip.notes = form.notes
    trip.cover_color = form.cover_color
    await db.commit()
    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


@router.post("/{trip_id}/delete")
async def delete_trip(
    trip: Trip = Depends(require_traveler),
    db: AsyncSession = Depends(get_session),
):
    await db.delete(trip)
    await db.commit()
    return RedirectResponse("/trips", status_code=303)
```

- [ ] **Step 12.3 — Templates**

Create `src/trip_tracker/templates/trips/list.html`:

```html
{% extends "base.html" %}
{% block title %}Trips · trip-tracker{% endblock %}
{% block content %}
  <div class="flex items-baseline justify-between">
    <h1 class="text-3xl font-semibold">Trips</h1>
    <a class="rounded bg-zinc-900 px-3 py-1.5 text-sm text-white dark:bg-zinc-100 dark:text-zinc-900"
       href="/segments/new">+ Add segment</a>
  </div>
  <ul class="mt-6 divide-y divide-zinc-200 dark:divide-zinc-800">
    {% for t in trips %}
      {% include "trips/_row.html" %}
    {% else %}
      <li class="py-6 text-zinc-500">No trips yet. <a class="underline" href="/segments/new">Add your first segment.</a></li>
    {% endfor %}
  </ul>
{% endblock %}
```

Create `src/trip_tracker/templates/trips/_row.html`:

```html
<li class="py-3">
  <a href="/trips/{{ t.id }}" class="block hover:underline">
    <div class="flex items-baseline justify-between">
      <span class="text-lg font-medium">{{ t.title }}</span>
      <span class="text-sm text-zinc-500">{{ t.start_date }} – {{ t.end_date }}</span>
    </div>
    {% if t.primary_destination %}
      <div class="text-sm text-zinc-500">{{ t.primary_destination }}</div>
    {% endif %}
  </a>
</li>
```

Create `src/trip_tracker/templates/trips/detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ trip.title }} · trip-tracker{% endblock %}
{% block content %}
  <div class="flex items-baseline justify-between">
    <div>
      <h1 class="text-3xl font-semibold">{{ trip.title }}</h1>
      <p class="text-sm text-zinc-500">
        {{ trip.start_date }} – {{ trip.end_date }}
        {% if trip.primary_destination %} · {{ trip.primary_destination }}{% endif %}
      </p>
    </div>
    <div class="space-x-2 text-sm">
      <a class="underline" href="/trips/{{ trip.id }}/edit">Edit</a>
      <form method="post" action="/trips/{{ trip.id }}/delete" class="inline"
            onsubmit="return confirm('Delete this trip?')">
        <button class="text-red-600 underline">Delete</button>
      </form>
    </div>
  </div>
  <ul class="mt-6 divide-y divide-zinc-200 dark:divide-zinc-800">
    {% for s in segments %}
      {% include "segments/_row.html" %}
    {% else %}
      <li class="py-6 text-zinc-500">No segments yet.
        <a class="underline" href="/segments/new">Add one.</a></li>
    {% endfor %}
  </ul>
{% endblock %}
```

Create `src/trip_tracker/templates/trips/edit.html`:

```html
{% extends "base.html" %}
{% block title %}Edit · {{ trip.title }}{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">Edit trip</h1>
  {% if errors %}<p class="text-red-600">{{ errors._form }}</p>{% endif %}
  <form method="post" action="/trips/{{ trip.id }}" class="mt-6 space-y-4">
    <label class="block">Title
      <input class="mt-1 w-full rounded border p-2" name="title"
             value="{{ trip.title }}" required>
    </label>
    <div class="grid grid-cols-2 gap-4">
      <label class="block">Start date
        <input class="mt-1 w-full rounded border p-2" type="date" name="start_date"
               value="{{ trip.start_date }}" required>
      </label>
      <label class="block">End date
        <input class="mt-1 w-full rounded border p-2" type="date" name="end_date"
               value="{{ trip.end_date }}" required>
      </label>
    </div>
    <label class="block">Primary destination
      <input class="mt-1 w-full rounded border p-2" name="primary_destination"
             value="{{ trip.primary_destination or '' }}">
    </label>
    <label class="block">Notes
      <textarea class="mt-1 w-full rounded border p-2" name="notes" rows="3">{{ trip.notes or '' }}</textarea>
    </label>
    <button class="rounded bg-zinc-900 px-4 py-2 text-white dark:bg-zinc-100 dark:text-zinc-900">Save</button>
  </form>
{% endblock %}
```

- [ ] **Step 12.4 — Update base nav**

Edit `src/trip_tracker/templates/base.html`. Inside `<body>`, before `<main>`:

```html
{% if user %}
<nav class="border-b border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900">
  <div class="mx-auto flex max-w-2xl items-center justify-between px-4 py-3">
    <a class="font-semibold" href="/">trip-tracker</a>
    <div class="space-x-4 text-sm">
      <a href="/trips" class="hover:underline">Trips</a>
      {% if user.is_admin %}<a href="/admin/aliases" class="hover:underline">Admin</a>{% endif %}
      <a href="/auth/logout" class="hover:underline">Sign out</a>
    </div>
  </div>
</nav>
{% endif %}
```

The home route's context needs `user`. Update `src/trip_tracker/routes/home.py` to include `user` in the context dict (it already does — verify).

- [ ] **Step 12.5 — Wire router into `app.py`**

Add:

```python
from trip_tracker.routes.trips import router as trips_router
# inside create_app, after auth:
app.include_router(trips_router)
```

- [ ] **Step 12.6 — Green + commit**

```bash
uv run pytest tests/test_routes_trips.py -v
uv run pytest -q
git add src/trip_tracker/routes/trips.py src/trip_tracker/templates/trips/ \
        src/trip_tracker/templates/base.html src/trip_tracker/app.py \
        tests/test_routes_trips.py
git commit -m "feat(routes): trips list/detail/edit/delete with traveler scoping"
```

---

## Task 13 — Segments Routes: Type Picker + Per-Type Forms + Create

**Spec ref:** §6 (entire UI section), submission flow + derivation rule.

This is the largest task — six per-type form templates, the picker, and the POST handler.

**Files:**
- Create: `src/trip_tracker/routes/segments.py`
- Create: `src/trip_tracker/templates/segments/type_picker.html`
- Create: `src/trip_tracker/templates/segments/_common_fields.html`
- Create: `src/trip_tracker/templates/segments/{flight,lodging,car,train,transfer,activity}_form.html`
- Create: `src/trip_tracker/templates/segments/_row.html`
- Modify: `src/trip_tracker/app.py` (include router)
- Create: `tests/test_routes_segments.py`

- [ ] **Step 13.1 — Failing tests**

`tests/test_routes_segments.py`:

```python
"""Segments routes: type picker, per-type forms, create with implicit trip."""

from __future__ import annotations

import uuid
from datetime import date

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _user(db: AsyncSession) -> User:
    u = User(oidc_subject="s", email="u@x.com", display_name="U")
    db.add(u)
    await db.commit()
    return u


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_type_picker(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.get("/segments/new")
    assert r.status_code == 200
    for t in ["Flight", "Lodging", "Car", "Train", "Transfer", "Activity"]:
        assert t in r.text


@pytest.mark.asyncio
async def test_create_flight_with_new_trip(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.post(
                "/segments",
                data={
                    "type": "flight",
                    "trip_selector_new_trip_title": "Paris May 2026",
                    "status": "confirmed",
                    "provider": "Delta",
                    "confirmation_number": "ABC123",
                    "flight_number": "DL44",
                    "origin_iata": "JFK",
                    "origin_city": "New York",
                    "destination_iata": "CDG",
                    "destination_city": "Paris",
                    "start_local": "2026-06-01T09:00",
                    "start_tz": "America/New_York",
                    "end_local": "2026-06-01T22:00",
                    "end_tz": "Europe/Paris",
                    "seat": "12A",
                },
                follow_redirects=False,
            )
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/trips/")

    trip = (await db_session.execute(select(Trip))).scalar_one()
    assert trip.title == "Paris May 2026"
    assert trip.primary_destination == "Paris"  # end_location.city for flights

    seg = (await db_session.execute(select(Segment))).scalar_one()
    assert seg.type == "flight"
    assert seg.start_location["iata"] == "JFK"
    assert seg.details["flight_number"] == "DL44"
    assert seg.parse_source == "manual"
    assert seg.parse_confidence == 1.0


@pytest.mark.asyncio
async def test_create_lodging_destination_from_start(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.post(
                "/segments",
                data={
                    "type": "lodging",
                    "trip_selector_new_trip_title": "Hotel Trip",
                    "status": "confirmed",
                    "hotel_name": "Le Marais Hotel",
                    "city": "Paris",
                    "country": "France",
                    "start_local": "2026-06-01T15:00",
                    "start_tz": "Europe/Paris",
                    "end_local": "2026-06-05T11:00",
                    "end_tz": "Europe/Paris",
                },
                follow_redirects=False,
            )
    assert r.status_code == 303
    trip = (await db_session.execute(select(Trip))).scalar_one()
    assert trip.primary_destination == "Paris"  # start_location.city for lodging


@pytest.mark.asyncio
async def test_create_segment_existing_trip_widens_dates(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(title="T", start_date=date(2026, 6, 5),
                end_date=date(2026, 6, 7), created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.post(
                "/segments",
                data={
                    "type": "flight",
                    "trip_selector_existing_trip_id": str(trip.id),
                    "status": "confirmed",
                    "start_local": "2026-06-01T09:00",  # before existing trip start
                    "start_tz": "UTC",
                },
                follow_redirects=False,
            )
    assert r.status_code == 303
    await db_session.refresh(trip)
    assert trip.start_date == date(2026, 6, 1)  # widened
    assert trip.end_date == date(2026, 6, 7)    # unchanged
```

- [ ] **Step 13.2 — Implement `routes/segments.py`**

Create `src/trip_tracker/routes/segments.py`:

```python
"""Segments routes: per-type creation/edit/delete. Spec §6."""

from __future__ import annotations

import json
import uuid
import zoneinfo
from datetime import date, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user
from trip_tracker.db import get_session
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.schemas.segment_forms import (
    ActivitySegmentForm,
    CarSegmentForm,
    FlightSegmentForm,
    LodgingSegmentForm,
    TrainSegmentForm,
    TransferSegmentForm,
    TripSelector,
)

router = APIRouter(tags=["segments"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

# Pre-load IANA tz list once.
_TZ_FIXTURE = (
    Path(__file__).parent.parent / "static" / "iana_timezones.json"
)
TIMEZONES = json.loads(_TZ_FIXTURE.read_text())

FORM_BY_TYPE = {
    "flight": FlightSegmentForm,
    "lodging": LodgingSegmentForm,
    "car": CarSegmentForm,
    "train": TrainSegmentForm,
    "transfer": TransferSegmentForm,
    "activity": ActivitySegmentForm,
}

DESTINATION_FROM_END = {"flight", "train", "transfer"}


def _to_utc(local: datetime, tz: str) -> datetime:
    return local.replace(tzinfo=zoneinfo.ZoneInfo(tz)).astimezone(
        zoneinfo.ZoneInfo("UTC")
    )


def _user_trips(db: AsyncSession, user_id: uuid.UUID):
    return (
        select(Trip)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(TripTraveler.user_id == user_id)
        .order_by(Trip.start_date.desc())
    )


@router.get("/segments/new", response_class=HTMLResponse)
async def new_segment(
    request: Request,
    type: str | None = Query(default=None),
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    if type is None:
        return templates.TemplateResponse(
            request, "segments/type_picker.html", {"user": user}
        )
    if type not in FORM_BY_TYPE:
        raise HTTPException(400, detail="unknown segment type")
    trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
    return templates.TemplateResponse(
        request, f"segments/{type}_form.html",
        {
            "user": user, "trips": trips, "timezones": TIMEZONES,
            "values": {}, "errors": {}, "type": type,
        },
    )


@router.post("/segments")
async def create_segment(
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    form_data = await request.form()
    seg_type = form_data.get("type")
    if seg_type not in FORM_BY_TYPE:
        raise HTTPException(400, detail="unknown segment type")
    form_cls = FORM_BY_TYPE[seg_type]

    raw: dict[str, Any] = dict(form_data)
    raw["trip_selector"] = TripSelector(
        existing_trip_id=raw.pop("trip_selector_existing_trip_id", None) or None,
        new_trip_title=raw.pop("trip_selector_new_trip_title", None) or None,
    ).model_dump()

    try:
        form = form_cls.model_validate(raw)
    except ValidationError as e:
        trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
        return templates.TemplateResponse(
            request, f"segments/{seg_type}_form.html",
            {
                "user": user, "trips": trips, "timezones": TIMEZONES,
                "values": raw, "errors": {"_form": str(e)}, "type": seg_type,
            },
            status_code=200,
        )

    # Convert datetimes to UTC.
    start_at = _to_utc(form.start_local, form.start_tz)
    end_at = _to_utc(form.end_local, form.end_tz) if form.end_local and form.end_tz else None

    # Build location jsonb per type.
    start_loc, end_loc, details = _shape_payload(form)

    async with db.begin():
        if form.trip_selector.existing_trip_id is not None:
            trip = (
                await db.execute(
                    select(Trip)
                    .join(TripTraveler, TripTraveler.trip_id == Trip.id)
                    .where(
                        Trip.id == form.trip_selector.existing_trip_id,
                        TripTraveler.user_id == user.id,
                    )
                )
            ).scalar_one_or_none()
            if trip is None:
                raise HTTPException(404)
            seg_start_date = start_at.date()
            seg_end_date = (end_at or start_at).date()
            new_start = min(trip.start_date, seg_start_date)
            new_end = max(trip.end_date, seg_end_date)
            if (new_start, new_end) != (trip.start_date, trip.end_date):
                trip.start_date = new_start
                trip.end_date = new_end
        else:
            assert form.trip_selector.new_trip_title  # validated by Pydantic
            seg_end_date = (end_at or start_at).date()
            primary = _derive_destination(seg_type, start_loc, end_loc)
            trip = Trip(
                title=form.trip_selector.new_trip_title.strip(),
                start_date=start_at.date(),
                end_date=seg_end_date,
                primary_destination=primary,
                created_by=user.id,
            )
            db.add(trip)
            await db.flush()
            db.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

        seg = Segment(
            trip_id=trip.id,
            owner_user_id=user.id,
            type=seg_type,
            status=form.status,
            confirmation_number=form.confirmation_number,
            provider=form.provider,
            start_at=start_at,
            start_tz=form.start_tz,
            end_at=end_at,
            end_tz=form.end_tz,
            start_location=start_loc,
            end_location=end_loc,
            details=details,
            parse_source="manual",
            parse_confidence=1.0,
        )
        db.add(seg)

    return RedirectResponse(f"/trips/{trip.id}", status_code=303)


def _shape_payload(
    form: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Return (start_location, end_location, details) jsonb dicts per type."""
    details: dict[str, Any] = {}
    if form.notes:
        details["notes"] = form.notes
    t = form.type

    if t == "flight":
        start = _drop_none(name=form.origin_iata, iata=form.origin_iata,
                           city=form.origin_city)
        end = _drop_none(name=form.destination_iata, iata=form.destination_iata,
                         city=form.destination_city)
        if form.flight_number:
            details["flight_number"] = form.flight_number
        if form.seat:
            details["seat"] = form.seat
        return start or None, end or None, details

    if t == "lodging":
        loc = _drop_none(name=form.hotel_name, address=form.address,
                        city=form.city, country=form.country)
        if form.room_type:
            details["room_type"] = form.room_type
        return loc or None, loc or None, details

    if t == "car":
        start = _drop_none(name=form.pickup_location, city=form.pickup_city)
        end = _drop_none(name=form.dropoff_location, city=form.dropoff_city)
        if form.car_class:
            details["car_class"] = form.car_class
        return start or None, end or None, details

    if t == "train":
        start = _drop_none(name=form.origin_station)
        end = _drop_none(name=form.destination_station)
        if form.train_number:
            details["train_number"] = form.train_number
        if form.seat:
            details["seat"] = form.seat
        return start or None, end or None, details

    if t == "transfer":
        return ({"name": form.pickup_location},
                {"name": form.dropoff_location}, details)

    if t == "activity":
        loc = _drop_none(name=form.venue_name, address=form.address, city=form.city)
        return loc or None, None, details

    raise AssertionError(f"unhandled type: {t}")


def _drop_none(**kw: Any) -> dict[str, Any]:
    return {k: v for k, v in kw.items() if v}


def _derive_destination(
    seg_type: str,
    start_loc: dict[str, Any] | None,
    end_loc: dict[str, Any] | None,
) -> str | None:
    primary_side = end_loc if seg_type in DESTINATION_FROM_END else start_loc
    fallback_side = start_loc if seg_type in DESTINATION_FROM_END else end_loc
    return (
        (primary_side or {}).get("city")
        or (fallback_side or {}).get("city")
        or None
    )
```

- [ ] **Step 13.3 — Templates**

Create `src/trip_tracker/templates/segments/type_picker.html`:

```html
{% extends "base.html" %}
{% block title %}New segment · trip-tracker{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">What kind of segment?</h1>
  <div class="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-3">
    {% for t in ["flight", "lodging", "car", "train", "transfer", "activity"] %}
      <a class="rounded border border-zinc-200 p-4 text-center hover:bg-zinc-50 dark:border-zinc-800 dark:hover:bg-zinc-900"
         href="/segments/new?type={{ t }}">{{ t.capitalize() }}</a>
    {% endfor %}
  </div>
{% endblock %}
```

Create `src/trip_tracker/templates/segments/_common_fields.html`:

```html
{# Reused inside every per-type form. Renders trip selector + datetime + tz + status. #}
<input type="hidden" name="type" value="{{ type }}">

<fieldset class="rounded border border-zinc-200 p-3 dark:border-zinc-800">
  <legend class="px-1 text-sm">Trip</legend>
  <label class="block text-sm">
    <input type="radio" name="_trip_mode" value="existing"
           onclick="document.getElementById('new-trip-row').hidden=true; document.getElementById('existing-trip-row').hidden=false"
           {% if values.get("trip_selector_existing_trip_id") or trips %}checked{% endif %}>
    Existing trip
  </label>
  <div id="existing-trip-row" {% if not (values.get("trip_selector_existing_trip_id") or trips) %}hidden{% endif %}>
    <select name="trip_selector_existing_trip_id" class="mt-1 w-full rounded border p-2">
      <option value="">— pick a trip —</option>
      {% for t in trips %}
        <option value="{{ t.id }}"
          {% if values.get("trip_selector_existing_trip_id") == t.id|string %}selected{% endif %}>
          {{ t.title }} ({{ t.start_date }})
        </option>
      {% endfor %}
    </select>
  </div>
  <label class="mt-2 block text-sm">
    <input type="radio" name="_trip_mode" value="new"
           onclick="document.getElementById('new-trip-row').hidden=false; document.getElementById('existing-trip-row').hidden=true"
           {% if values.get("trip_selector_new_trip_title") %}checked{% endif %}>
    New trip
  </label>
  <div id="new-trip-row" {% if not values.get("trip_selector_new_trip_title") %}hidden{% endif %}>
    <input class="mt-1 w-full rounded border p-2" name="trip_selector_new_trip_title"
           placeholder="Trip title (e.g. Paris May 2026)"
           value="{{ values.get('trip_selector_new_trip_title', '') }}">
  </div>
</fieldset>

<div class="grid grid-cols-2 gap-3">
  <label class="block text-sm">Start (local)
    <input class="mt-1 w-full rounded border p-2" type="datetime-local"
           name="start_local" value="{{ values.get('start_local', '') }}" required>
  </label>
  <label class="block text-sm">Start tz
    <select class="mt-1 w-full rounded border p-2" name="start_tz" required id="start-tz-select">
      {% for tz in timezones %}<option value="{{ tz }}"
        {% if values.get('start_tz') == tz %}selected{% endif %}>{{ tz }}</option>{% endfor %}
    </select>
  </label>
  <label class="block text-sm">End (local)
    <input class="mt-1 w-full rounded border p-2" type="datetime-local"
           name="end_local" value="{{ values.get('end_local', '') }}">
  </label>
  <label class="block text-sm">End tz
    <select class="mt-1 w-full rounded border p-2" name="end_tz" id="end-tz-select">
      <option value="">—</option>
      {% for tz in timezones %}<option value="{{ tz }}"
        {% if values.get('end_tz') == tz %}selected{% endif %}>{{ tz }}</option>{% endfor %}
    </select>
  </label>
</div>

<label class="block text-sm">Status
  <select class="mt-1 w-full rounded border p-2" name="status">
    {% for s in ["confirmed", "tentative", "cancelled"] %}
      <option value="{{ s }}"
        {% if values.get('status', 'confirmed') == s %}selected{% endif %}>{{ s }}</option>
    {% endfor %}
  </select>
</label>

<label class="block text-sm">Confirmation #
  <input class="mt-1 w-full rounded border p-2" name="confirmation_number"
         value="{{ values.get('confirmation_number', '') }}">
</label>

<label class="block text-sm">Provider
  <input class="mt-1 w-full rounded border p-2" name="provider"
         value="{{ values.get('provider', '') }}">
</label>

<label class="block text-sm">Notes
  <textarea class="mt-1 w-full rounded border p-2" name="notes" rows="2">{{ values.get('notes', '') }}</textarea>
</label>

<script>
  // Pre-select browser tz on first render only (when no existing value).
  (function() {
    const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    for (const id of ['start-tz-select', 'end-tz-select']) {
      const sel = document.getElementById(id);
      if (sel && !sel.value) sel.value = browserTz;
    }
  })();
</script>
```

Create `src/trip_tracker/templates/segments/flight_form.html`:

```html
{% extends "base.html" %}
{% block title %}New flight · trip-tracker{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">New flight</h1>
  {% if errors %}<p class="mt-2 text-red-600">{{ errors._form }}</p>{% endif %}
  <form method="post" action="/segments" class="mt-6 space-y-4">
    {% include "segments/_common_fields.html" %}
    <div class="grid grid-cols-2 gap-3">
      <label class="block text-sm">Flight number
        <input class="mt-1 w-full rounded border p-2" name="flight_number" value="{{ values.get('flight_number', '') }}"></label>
      <label class="block text-sm">Seat
        <input class="mt-1 w-full rounded border p-2" name="seat" value="{{ values.get('seat', '') }}"></label>
      <label class="block text-sm">Origin (IATA)
        <input class="mt-1 w-full rounded border p-2" name="origin_iata" maxlength="4" value="{{ values.get('origin_iata', '') }}"></label>
      <label class="block text-sm">Origin city
        <input class="mt-1 w-full rounded border p-2" name="origin_city" value="{{ values.get('origin_city', '') }}"></label>
      <label class="block text-sm">Destination (IATA)
        <input class="mt-1 w-full rounded border p-2" name="destination_iata" maxlength="4" value="{{ values.get('destination_iata', '') }}"></label>
      <label class="block text-sm">Destination city
        <input class="mt-1 w-full rounded border p-2" name="destination_city" value="{{ values.get('destination_city', '') }}"></label>
    </div>
    <button class="rounded bg-zinc-900 px-4 py-2 text-white dark:bg-zinc-100 dark:text-zinc-900">Create</button>
  </form>
{% endblock %}
```

Create the remaining five form templates following the same pattern. Each: extends `base.html`, includes `segments/_common_fields.html`, hidden `<input name="type" value="<type>">` is already inside `_common_fields.html`, then renders the type-specific fields. Use `value="{{ values.get('<field>', '') }}"` on every input so a server-side validation error round-trip preserves what the user typed.

Full second example — `src/trip_tracker/templates/segments/lodging_form.html`:

```html
{% extends "base.html" %}
{% block title %}New lodging · trip-tracker{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">New lodging</h1>
  {% if errors %}<p class="mt-2 text-red-600">{{ errors._form }}</p>{% endif %}
  <form method="post" action="/segments" class="mt-6 space-y-4">
    {% include "segments/_common_fields.html" %}
    <label class="block text-sm">Hotel name
      <input class="mt-1 w-full rounded border p-2" name="hotel_name" required
             value="{{ values.get('hotel_name', '') }}">
    </label>
    <label class="block text-sm">Address
      <input class="mt-1 w-full rounded border p-2" name="address"
             value="{{ values.get('address', '') }}">
    </label>
    <div class="grid grid-cols-2 gap-3">
      <label class="block text-sm">City
        <input class="mt-1 w-full rounded border p-2" name="city"
               value="{{ values.get('city', '') }}">
      </label>
      <label class="block text-sm">Country
        <input class="mt-1 w-full rounded border p-2" name="country"
               value="{{ values.get('country', '') }}">
      </label>
    </div>
    <label class="block text-sm">Room type
      <input class="mt-1 w-full rounded border p-2" name="room_type"
             value="{{ values.get('room_type', '') }}">
    </label>
    <button class="rounded bg-zinc-900 px-4 py-2 text-white dark:bg-zinc-100 dark:text-zinc-900">Create</button>
  </form>
{% endblock %}
```

Note on the flight form already shown: it has **no** `required` type-specific fields (origin/destination IATA + city are all optional in `FlightSegmentForm`), which is why none of its inputs carry `required`. The `*` convention below is the documented requirement marker for the remaining four templates.

For the remaining four (`car_form.html`, `train_form.html`, `transfer_form.html`, `activity_form.html`), follow this exact structure but swap the type-specific fields per these tables. **All inputs use `name="<field>"` and `value="{{ values.get('<field>', '') }}"`. Fields marked `*` are `required`; others are optional.**

`car_form.html`:
- `pickup_location` * (text)
- `pickup_city` (text)
- `dropoff_location` * (text)
- `dropoff_city` (text)
- `car_class` (text)

`train_form.html`:
- `train_number` (text)
- `origin_station` * (text)
- `destination_station` * (text)
- `seat` (text)

`transfer_form.html`:
- `pickup_location` * (text)
- `dropoff_location` * (text)

`activity_form.html`:
- `venue_name` * (text)
- `address` (text)
- `city` (text)

H1 text per type: "New car rental", "New train", "New transfer", "New activity". `<title>` block follows the same pattern as `lodging_form.html`.

Create `src/trip_tracker/templates/segments/_row.html`:

```html
<li class="py-3 flex items-baseline justify-between">
  <div>
    <span class="rounded bg-zinc-200 px-1.5 py-0.5 text-xs uppercase dark:bg-zinc-800">{{ s.type }}</span>
    <span class="ml-2 font-medium">{{ s.provider or "—" }}</span>
    {% if s.confirmation_number %}<span class="ml-2 text-zinc-500">{{ s.confirmation_number }}</span>{% endif %}
    <div class="text-sm text-zinc-500">
      {{ s.start_at.strftime("%Y-%m-%d %H:%M") }} {{ s.start_tz }}
      {% if s.start_location %} · {{ s.start_location.get("name") or s.start_location.get("city") }}{% endif %}
      {% if s.end_location %} → {{ s.end_location.get("name") or s.end_location.get("city") }}{% endif %}
    </div>
  </div>
  <div class="space-x-2 text-sm">
    <a class="underline" href="/trips/{{ s.trip_id }}/segments/{{ s.id }}/edit">Edit</a>
    <form method="post" action="/trips/{{ s.trip_id }}/segments/{{ s.id }}/delete" class="inline"
          onsubmit="return confirm('Delete?')">
      <button class="text-red-600 underline">Delete</button>
    </form>
  </div>
</li>
```

- [ ] **Step 13.4 — Wire router into `app.py`**

```python
from trip_tracker.routes.segments import router as segments_router
# inside create_app:
app.include_router(segments_router)
```

- [ ] **Step 13.5 — Green + commit**

```bash
uv run pytest tests/test_routes_segments.py -v
uv run pytest -q
git add src/trip_tracker/routes/segments.py src/trip_tracker/templates/segments/ \
        src/trip_tracker/app.py tests/test_routes_segments.py
git commit -m "feat(routes): segments type picker + per-type forms + create"
```

---

## Task 14 — Segment Edit + Delete Routes

**Spec ref:** §6 routes table.

**Files:**
- Modify: `src/trip_tracker/routes/segments.py` (add edit + update + delete)
- Append to: `tests/test_routes_segments.py`

- [ ] **Step 14.1 — Failing tests (full bodies)**

Append to `tests/test_routes_segments.py`. Each test creates state via direct ORM inserts (faster than going through the `POST /segments` form for setup) and only exercises the new edit/delete routes via HTTP:

```python
async def _seed_flight(
    db: AsyncSession, owner: User, trip: Trip
) -> Segment:
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=owner.id,
        type="flight",
        status="confirmed",
        provider="Delta",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 13, 0, tzinfo=timezone.utc),  # 09:00 EDT
        start_tz="America/New_York",
        end_at=datetime(2026, 6, 2, 2, 0, tzinfo=timezone.utc),     # 22:00 CEST
        end_tz="Europe/Paris",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={"flight_number": "DL44", "seat": "12A"},
        parse_source="manual",
        parse_confidence=1.0,
    )
    db.add(seg)
    await db.commit()
    return seg


@pytest.mark.asyncio
async def test_edit_segment_renders_prefilled_form(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(title="T", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5), created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = await _seed_flight(db_session, user, trip)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit")

    assert r.status_code == 200
    # Prefilled values from the segment:
    assert "Delta" in r.text
    assert "ABC123" in r.text
    assert "DL44" in r.text
    assert "JFK" in r.text
    assert "CDG" in r.text
    # The local datetime display (note: 13:00 UTC → 09:00 in America/New_York):
    assert "2026-06-01T09:00" in r.text


@pytest.mark.asyncio
async def test_edit_segment_round_trip_updates_db(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(title="T", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5), created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = await _seed_flight(db_session, user, trip)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.post(
                f"/trips/{trip.id}/segments/{seg.id}",
                data={
                    "type": "flight",
                    "trip_selector_existing_trip_id": str(trip.id),
                    "status": "confirmed",
                    "provider": "Delta",
                    "confirmation_number": "ABC123",
                    "flight_number": "DL44",
                    "origin_iata": "JFK",
                    "origin_city": "New York",
                    "destination_iata": "ORY",  # changed
                    "destination_city": "Paris",
                    "start_local": "2026-06-01T09:00",
                    "start_tz": "America/New_York",
                    "end_local": "2026-06-01T22:00",
                    "end_tz": "Europe/Paris",
                    "seat": "1A",  # changed
                },
                follow_redirects=False,
            )
    assert r.status_code == 303

    await db_session.refresh(seg)
    assert seg.end_location["iata"] == "ORY"
    assert seg.details["seat"] == "1A"
    # confirmation/provider/flight_number unchanged:
    assert seg.confirmation_number == "ABC123"


@pytest.mark.asyncio
async def test_delete_segment_removes_row(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = await _user(db_session)
    trip = Trip(title="T", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5), created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = await _seed_flight(db_session, user, trip)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(user, settings),
        ) as c:
            r = await c.post(
                f"/trips/{trip.id}/segments/{seg.id}/delete",
                follow_redirects=False,
            )
    assert r.status_code == 303
    assert r.headers["location"] == f"/trips/{trip.id}"

    rows = (await db_session.execute(select(Segment))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_edit_segment_404_for_non_traveler(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    creator = await _user(db_session)
    other = User(oidc_subject="other", email="other@x.com", display_name="O")
    db_session.add(other)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 5), created_by=creator.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=creator.id, role="owner"))
    seg = await _seed_flight(db_session, creator, trip)

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test",
            cookies=_cookie(other, settings),
        ) as c:
            r_edit = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit")
            r_post = await c.post(
                f"/trips/{trip.id}/segments/{seg.id}",
                data={"type": "flight"}, follow_redirects=False,
            )
            r_del = await c.post(
                f"/trips/{trip.id}/segments/{seg.id}/delete",
                follow_redirects=False,
            )

    # All three must 404 — non-member can't see the trip exists.
    assert r_edit.status_code == 404
    assert r_post.status_code == 404
    assert r_del.status_code == 404
```

- [ ] **Step 14.2 — Implement edit/update/delete handlers**

Append to `src/trip_tracker/routes/segments.py`:

```python
@router.get("/trips/{trip_id}/segments/{segment_id}/edit", response_class=HTMLResponse)
async def edit_segment_form(
    request: Request,
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
    return templates.TemplateResponse(
        request, f"segments/{seg.type}_form.html",
        {
            "user": user, "trips": trips, "timezones": TIMEZONES,
            "values": _segment_to_form_values(seg), "errors": {},
            "type": seg.type, "edit_segment_id": str(seg.id),
        },
    )


@router.post("/trips/{trip_id}/segments/{segment_id}")
async def update_segment(
    request: Request,
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    """Update an existing segment in place.

    Phase 2 scope: type and trip CANNOT change. The form re-uses the per-type
    template, so the `type` field is hidden and immutable; the `trip_selector`
    is forced to the current trip (we ignore any new-trip submission). This
    keeps the diff small — moving a segment between trips can land later.

    Auto-widening of trip dates DOES re-run on update (the new datetime may
    extend or shrink the range).
    """
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    # Re-load the trip so we can widen its dates after recomputing.
    trip = (
        await db.execute(select(Trip).where(Trip.id == trip_id))
    ).scalar_one()

    form_data = await request.form()
    seg_type = form_data.get("type")
    if seg_type != seg.type:
        # Defensive: form posts the hidden `type` field; mismatch is tampering.
        raise HTTPException(400, detail="segment type immutable")
    form_cls = FORM_BY_TYPE[seg_type]

    raw: dict[str, Any] = dict(form_data)
    # Force the trip selector to the current trip — edits never re-route trips.
    raw["trip_selector"] = TripSelector(
        existing_trip_id=trip.id, new_trip_title=None
    ).model_dump()
    raw.pop("trip_selector_existing_trip_id", None)
    raw.pop("trip_selector_new_trip_title", None)

    try:
        form = form_cls.model_validate(raw)
    except ValidationError as e:
        return templates.TemplateResponse(
            request, f"segments/{seg_type}_form.html",
            {
                "user": user, "trips": [trip], "timezones": TIMEZONES,
                "values": raw, "errors": {"_form": str(e)}, "type": seg_type,
                "edit_segment_id": str(seg.id),
            },
            status_code=200,
        )

    start_at = _to_utc(form.start_local, form.start_tz)
    end_at = (
        _to_utc(form.end_local, form.end_tz)
        if form.end_local and form.end_tz else None
    )
    start_loc, end_loc, details = _shape_payload(form)

    async with db.begin():
        seg.status = form.status
        seg.confirmation_number = form.confirmation_number
        seg.provider = form.provider
        seg.start_at = start_at
        seg.start_tz = form.start_tz
        seg.end_at = end_at
        seg.end_tz = form.end_tz
        seg.start_location = start_loc
        seg.end_location = end_loc
        seg.details = details

        # Re-widen trip dates against the new segment timing.
        seg_start_date = start_at.date()
        seg_end_date = (end_at or start_at).date()
        new_start = min(trip.start_date, seg_start_date)
        new_end = max(trip.end_date, seg_end_date)
        if (new_start, new_end) != (trip.start_date, trip.end_date):
            trip.start_date = new_start
            trip.end_date = new_end

    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.post("/trips/{trip_id}/segments/{segment_id}/delete")
async def delete_segment(
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
):
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    await db.delete(seg)
    await db.commit()
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


async def _load_segment_for_user(
    db: AsyncSession, trip_id: uuid.UUID, segment_id: uuid.UUID, user_id: uuid.UUID
) -> Segment:
    stmt = (
        select(Segment)
        .join(Trip, Trip.id == Segment.trip_id)
        .join(TripTraveler, TripTraveler.trip_id == Trip.id)
        .where(
            Trip.id == trip_id,
            Segment.id == segment_id,
            TripTraveler.user_id == user_id,
        )
    )
    seg = (await db.execute(stmt)).scalar_one_or_none()
    if seg is None:
        raise HTTPException(404)
    return seg


def _segment_to_form_values(seg: Segment) -> dict[str, Any]:
    """Flatten a Segment row into the dict shape templates expect."""
    sl = seg.start_location or {}
    el = seg.end_location or {}
    d = seg.details or {}
    base = {
        "trip_selector_existing_trip_id": str(seg.trip_id),
        "status": seg.status,
        "provider": seg.provider or "",
        "confirmation_number": seg.confirmation_number or "",
        "start_local": seg.start_at.astimezone(zoneinfo.ZoneInfo(seg.start_tz))
                                  .strftime("%Y-%m-%dT%H:%M"),
        "start_tz": seg.start_tz,
        "end_local": (
            seg.end_at.astimezone(zoneinfo.ZoneInfo(seg.end_tz)).strftime("%Y-%m-%dT%H:%M")
            if seg.end_at and seg.end_tz else ""
        ),
        "end_tz": seg.end_tz or "",
        "notes": d.get("notes", ""),
    }
    if seg.type == "flight":
        base.update(
            flight_number=d.get("flight_number", ""), seat=d.get("seat", ""),
            origin_iata=sl.get("iata", ""), origin_city=sl.get("city", ""),
            destination_iata=el.get("iata", ""), destination_city=el.get("city", ""),
        )
    elif seg.type == "lodging":
        base.update(
            hotel_name=sl.get("name", ""), address=sl.get("address", ""),
            city=sl.get("city", ""), country=sl.get("country", ""),
            room_type=d.get("room_type", ""),
        )
    elif seg.type == "car":
        base.update(
            pickup_location=sl.get("name", ""), pickup_city=sl.get("city", ""),
            dropoff_location=el.get("name", ""), dropoff_city=el.get("city", ""),
            car_class=d.get("car_class", ""),
        )
    elif seg.type == "train":
        base.update(
            origin_station=sl.get("name", ""),
            destination_station=el.get("name", ""),
            train_number=d.get("train_number", ""),
            seat=d.get("seat", ""),
        )
    elif seg.type == "transfer":
        base.update(
            pickup_location=sl.get("name", ""),
            dropoff_location=el.get("name", ""),
        )
    elif seg.type == "activity":
        base.update(
            venue_name=sl.get("name", ""), address=sl.get("address", ""),
            city=sl.get("city", ""),
        )
    return base
```

- [ ] **Step 14.3 — Green + commit**

```bash
uv run pytest tests/test_routes_segments.py -v
git add src/trip_tracker/routes/segments.py tests/test_routes_segments.py
git commit -m "feat(routes): segment edit + delete with traveler scoping"
```

---

## Task 15 — Admin: Aliases CRUD

**Spec ref:** §6 admin routes.

**Files:**
- Create: `src/trip_tracker/routes/admin.py`
- Create: `src/trip_tracker/templates/admin/alias_list.html`
- Create: `src/trip_tracker/templates/admin/alias_form.html`
- Modify: `src/trip_tracker/app.py` (include router)
- Create: `tests/test_routes_admin.py`

- [ ] **Step 15.1 — Failing tests**

`tests/test_routes_admin.py`:

```python
"""Admin routes: aliases + raw-emails."""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


@pytest.mark.asyncio
async def test_non_admin_blocked(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="x", email="x@x.com", display_name="X", is_admin=False)
    db_session.add(user); await db_session.commit()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings),
        ) as c:
            r = await c.get("/admin/aliases")
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_alias_crud(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    admin = User(oidc_subject="a", email="a@x.com", display_name="A", is_admin=True)
    db_session.add(admin); await db_session.commit()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(admin, settings),
        ) as c:
            r = await c.post("/admin/aliases",
                             data={"local_part": "oliver", "user_id": str(admin.id)},
                             follow_redirects=False)
            assert r.status_code == 303

            r = await c.get("/admin/aliases")
            assert "oliver" in r.text

            alias = (await db_session.execute(
                select(ForwardingAlias).where(ForwardingAlias.local_part == "oliver")
            )).scalar_one()

            r = await c.post(f"/admin/aliases/{alias.id}/delete",
                             follow_redirects=False)
            assert r.status_code == 303
            rows = (await db_session.execute(select(ForwardingAlias))).scalars().all()
            assert rows == []
```

- [ ] **Step 15.2 — Implement `routes/admin.py`**

Create `src/trip_tracker/routes/admin.py`:

```python
"""Admin routes: forwarding-alias CRUD + raw-email list/detail."""

from __future__ import annotations

import re
import uuid
from email import message_from_bytes
from email.policy import default as email_policy_default
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_admin
from trip_tracker.db import get_session
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User

router = APIRouter(prefix="/admin", tags=["admin"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


@router.get("/aliases", response_class=HTMLResponse)
async def alias_list(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    rows = (
        await db.execute(
            select(ForwardingAlias, User)
            .join(User, User.id == ForwardingAlias.user_id)
            .order_by(ForwardingAlias.local_part)
        )
    ).all()
    return templates.TemplateResponse(
        request, "admin/alias_list.html",
        {"user": user, "rows": rows},
    )


@router.get("/aliases/new", response_class=HTMLResponse)
async def alias_new_form(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    users = (await db.execute(select(User).order_by(User.email))).scalars().all()
    return templates.TemplateResponse(
        request, "admin/alias_form.html",
        {"user": user, "users": users, "alias": None, "errors": {}},
    )


# RFC-5321 valid local-part chars (lowercase only — we normalize on input).
# Spec §4: forwarding_aliases.local_part is "lowercase, RFC-5321 valid local-part chars only".
_LOCAL_PART_RE = re.compile(r"^[a-z0-9._%+\-]+$")


@router.post("/aliases")
async def alias_create(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    local_part: str = Form(...),
    user_id: uuid.UUID = Form(...),
):
    normalized = local_part.lower().strip()
    if not _LOCAL_PART_RE.match(normalized) or len(normalized) > 64:
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        return templates.TemplateResponse(
            request, "admin/alias_form.html",
            {
                "user": user, "users": users, "alias": None,
                "errors": {"_form": "invalid local part"},
            },
            status_code=200,
        )
    try:
        db.add(ForwardingAlias(local_part=normalized, user_id=user_id))
        await db.commit()
    except IntegrityError:
        await db.rollback()
        users = (await db.execute(select(User).order_by(User.email))).scalars().all()
        return templates.TemplateResponse(
            request, "admin/alias_form.html",
            {
                "user": user, "users": users, "alias": None,
                "errors": {"_form": f"alias {normalized!r} already exists"},
            },
            status_code=200,
        )
    return RedirectResponse("/admin/aliases", status_code=303)


@router.post("/aliases/{alias_id}/delete")
async def alias_delete(
    alias_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    alias = await db.get(ForwardingAlias, alias_id)
    if alias is None:
        raise HTTPException(404)
    await db.delete(alias)
    await db.commit()
    return RedirectResponse("/admin/aliases", status_code=303)
```

- [ ] **Step 15.3 — Templates**

Create `src/trip_tracker/templates/admin/alias_list.html`:

```html
{% extends "base.html" %}
{% block title %}Aliases · Admin{% endblock %}
{% block content %}
  <div class="flex items-baseline justify-between">
    <h1 class="text-3xl font-semibold">Forwarding aliases</h1>
    <a href="/admin/aliases/new" class="rounded bg-zinc-900 px-3 py-1.5 text-sm text-white dark:bg-zinc-100 dark:text-zinc-900">+ New</a>
  </div>
  <table class="mt-6 w-full text-sm">
    <thead class="text-left text-zinc-500">
      <tr><th class="py-2">Local part</th><th>Owner</th><th>Created</th><th></th></tr>
    </thead>
    <tbody class="divide-y divide-zinc-200 dark:divide-zinc-800">
      {% for alias, owner in rows %}
        <tr>
          <td class="py-2 font-mono">{{ alias.local_part }}</td>
          <td>{{ owner.email }}</td>
          <td class="text-zinc-500">{{ alias.created_at.date() }}</td>
          <td>
            <form method="post" action="/admin/aliases/{{ alias.id }}/delete"
                  onsubmit="return confirm('Delete?')" class="inline">
              <button class="text-red-600 underline">Delete</button>
            </form>
          </td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
{% endblock %}
```

Create `src/trip_tracker/templates/admin/alias_form.html`:

```html
{% extends "base.html" %}
{% block title %}New alias{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">New forwarding alias</h1>
  <form method="post" action="/admin/aliases" class="mt-6 space-y-4">
    <label class="block text-sm">Local part
      <input class="mt-1 w-full rounded border p-2" name="local_part"
             pattern="[a-z0-9._%+-]+" required>
    </label>
    <label class="block text-sm">Owner
      <select class="mt-1 w-full rounded border p-2" name="user_id" required>
        {% for u in users %}<option value="{{ u.id }}">{{ u.email }}</option>{% endfor %}
      </select>
    </label>
    <button class="rounded bg-zinc-900 px-4 py-2 text-white dark:bg-zinc-100 dark:text-zinc-900">Create</button>
  </form>
{% endblock %}
```

- [ ] **Step 15.4 — Wire + green + commit**

```python
# app.py
from trip_tracker.routes.admin import router as admin_router
app.include_router(admin_router)
```

```bash
uv run pytest tests/test_routes_admin.py -v
git add src/trip_tracker/routes/admin.py src/trip_tracker/templates/admin/ \
        src/trip_tracker/app.py tests/test_routes_admin.py
git commit -m "feat(admin): forwarding-alias CRUD"
```

---

## Task 16 — Admin: Raw-Emails List + Detail

**Spec ref:** §6 admin routes (raw-emails).

**Files:**
- Modify: `src/trip_tracker/routes/admin.py` (append handlers)
- Create: `src/trip_tracker/templates/admin/raw_email_list.html`
- Create: `src/trip_tracker/templates/admin/raw_email_detail.html`
- Append to: `tests/test_routes_admin.py`

- [ ] **Step 16.1 — Append handlers**

Append to `src/trip_tracker/routes/admin.py`:

```python
@router.get("/raw-emails", response_class=HTMLResponse)
async def raw_email_list(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
    page: int = 1,
) -> HTMLResponse:
    page_size = 50
    offset = max(0, (page - 1) * page_size)
    # LEFT JOIN forwarding_aliases on the local-part of to_address.
    stmt = (
        select(RawEmail, User)
        .outerjoin(
            ForwardingAlias,
            ForwardingAlias.local_part == sa.func.split_part(RawEmail.to_address, "@", 1),
        )
        .outerjoin(User, User.id == ForwardingAlias.user_id)
        .order_by(RawEmail.received_at.desc())
        .limit(page_size)
        .offset(offset)
    )
    # Need an `import sqlalchemy as sa` at top for sa.func.
    rows = (await db.execute(stmt)).all()
    return templates.TemplateResponse(
        request, "admin/raw_email_list.html",
        {"user": user, "rows": rows, "page": page},
    )


@router.get("/raw-emails/{raw_email_id}", response_class=HTMLResponse)
async def raw_email_detail(
    request: Request,
    raw_email_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    re = await db.get(RawEmail, raw_email_id)
    if re is None:
        raise HTTPException(404)
    msg = message_from_bytes(re.mime_blob, policy=email_policy_default)
    text_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                text_body = part.get_content()
                break
    else:
        text_body = msg.get_content() if msg.get_content_type() == "text/plain" else ""
    return templates.TemplateResponse(
        request, "admin/raw_email_detail.html",
        {"user": user, "re": re, "text_body": text_body},
    )


@router.get("/raw-emails/{raw_email_id}/eml")
async def raw_email_download(
    raw_email_id: uuid.UUID,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_session),
):
    re = await db.get(RawEmail, raw_email_id)
    if re is None:
        raise HTTPException(404)
    return Response(
        content=re.mime_blob,
        media_type="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{re.id}.eml"'},
    )
```

(`import sqlalchemy as sa` is already at the top of `admin.py` from Task 15's imports.)

- [ ] **Step 16.2 — Templates**

Create `src/trip_tracker/templates/admin/raw_email_list.html`:

```html
{% extends "base.html" %}
{% block title %}Raw emails · Admin{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">Raw emails</h1>
  <table class="mt-6 w-full text-sm">
    <thead class="text-left text-zinc-500">
      <tr><th class="py-2">Received</th><th>To</th><th>From</th><th>Subject</th><th>Owner</th><th>Status</th></tr>
    </thead>
    <tbody class="divide-y divide-zinc-200 dark:divide-zinc-800">
      {% for re, owner in rows %}
        <tr>
          <td class="py-2">{{ re.received_at.strftime("%Y-%m-%d %H:%M") }}</td>
          <td>{{ re.to_address }}</td>
          <td>{{ re.from_address }}</td>
          <td><a class="underline" href="/admin/raw-emails/{{ re.id }}">{{ re.subject or "—" }}</a></td>
          <td>{{ owner.email if owner else "—" }}</td>
          <td>{{ re.parse_status }}</td>
        </tr>
      {% endfor %}
    </tbody>
  </table>
  <div class="mt-4 text-sm">
    {% if page > 1 %}<a class="underline" href="?page={{ page - 1 }}">← prev</a>{% endif %}
    <a class="underline" href="?page={{ page + 1 }}">next →</a>
  </div>
{% endblock %}
```

Create `src/trip_tracker/templates/admin/raw_email_detail.html`:

```html
{% extends "base.html" %}
{% block title %}{{ re.subject or "Email" }} · Admin{% endblock %}
{% block content %}
  <h1 class="text-2xl font-semibold">{{ re.subject or "(no subject)" }}</h1>
  <table class="mt-4 text-sm">
    <tr><th class="pr-3 text-left text-zinc-500">From</th><td>{{ re.from_address }}</td></tr>
    <tr><th class="pr-3 text-left text-zinc-500">To</th><td>{{ re.to_address }}</td></tr>
    <tr><th class="pr-3 text-left text-zinc-500">Received</th><td>{{ re.received_at }}</td></tr>
    <tr><th class="pr-3 text-left text-zinc-500">Message-ID</th><td class="font-mono">{{ re.message_id }}</td></tr>
    <tr><th class="pr-3 text-left text-zinc-500">Status</th><td>{{ re.parse_status }}</td></tr>
  </table>
  <a class="mt-4 inline-block underline" href="/admin/raw-emails/{{ re.id }}/eml">Download .eml</a>
  <pre class="mt-4 whitespace-pre-wrap rounded bg-zinc-100 p-3 text-sm dark:bg-zinc-900">{{ text_body }}</pre>
{% endblock %}
```

- [ ] **Step 16.3 — Append tests + green + commit**

Add tests for: list shows orphan email with owner `—`; detail renders text/plain body; non-admin → 403.

```bash
uv run pytest tests/test_routes_admin.py -v
git add src/trip_tracker/routes/admin.py src/trip_tracker/templates/admin/ \
        tests/test_routes_admin.py
git commit -m "feat(admin): raw-emails list + detail + .eml download"
```

---

## Task 17 — README + End-to-End Verification + Tag v0.2.0

**Files:**
- Modify: `README.md` (add forwarding setup section)
- Run: full pytest, ruff, mypy, pre-commit, docker build, compose smoke
- Tag: `v0.2.0`

- [ ] **Step 17.1 — Append to `README.md`**

```markdown

## Email forwarding setup (Phase 2)

1. Generate a webhook secret: `python -c 'import secrets; print(secrets.token_hex(32))'` and put it in `.env` as `WEBHOOK_SECRET=…`.
2. As admin, log in and go to `/admin/aliases` → "+ New". Create `<your-local-part>` mapped to your user (e.g. `oliver`).
3. In forwardemail.net's dashboard for `trips.<your-domain>`:
   - Add forwarding rule `oliver@trips.<your-domain>` → webhook URL `https://trips.<your-domain>/api/ingest/email`.
   - Configure HMAC secret to match `WEBHOOK_SECRET`.
   - Confirm the signature header name; if it's not `X-Webhook-Signature`, set `WEBHOOK_SIGNATURE_HEADER=` accordingly.
4. Test: forward yourself a confirmation email to `oliver@trips.<your-domain>`. Within seconds it appears at `/admin/raw-emails`.
5. Manually create a segment for it via `/segments/new` (parsers arrive in Phase 3).
```

- [ ] **Step 17.2 — Tailwind rebuild + base nav verification**

```bash
./scripts/build-tailwind.sh
# Manually browse — `uv run python -m trip_tracker` then visit /trips, /admin/aliases, etc.
```

- [ ] **Step 17.3 — Full local verification gate**

```bash
uv run pytest --cov           # ≥ 85% (Phase 1 baseline)
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run pre-commit run --all-files
docker build -t trip-tracker:dev .
```

All must be green. Iterate on any failure.

- [ ] **Step 17.4 — Commit + tag + push**

```bash
git add README.md
git commit -m "docs: README — Phase 2 email forwarding setup"

git tag -a -s v0.2.0 -m "Phase 2 — Ingestion v0"
git push origin main
git push origin v0.2.0
```

The release workflow on GitHub fires on the tag push and produces a multi-arch image at `ghcr.io/<owner>/trip-tracker:v0.2.0`, signed with cosign + SBOM attached.

---

## Done Definition for Phase 2

- All 17 tasks merged to `main`.
- CI green (lint + typecheck + test + security + docker).
- Coverage ≥ 85 %.
- `v0.2.0` tag pushed; release workflow succeeded; signed multi-arch image at GHCR.
- Forwardemail.net forwarding configured against the production deployment; one real confirmation email round-trips end-to-end (forward → `/admin/raw-emails` shows it → admin can manually add a segment via `/segments/new` and see it on `/trips/:id`).

After this lands, return to brainstorming/writing-plans for **Phase 3 — Parsers** (json-ld, provider rules, Haiku fallback, ARQ + Redis).
