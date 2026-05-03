# Phase 9 — Smarter Inbox + Trip Consolidation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship parse-time duplicate detection (v0.8.1) and home-anchored trip consolidation + soft-delete merge (v0.9.0), per the spec at `docs/superpowers/specs/2026-05-02-phase9-inbox-and-trip-consolidation-design.md` (commit `a8163d2`).

**Architecture:** Two sequential releases. Track A (v0.8.1) adds a parse-time dedup gate inside the existing saq parse worker before persistence — zero schema cost beyond a new `parse_status='duplicate'` value. Tracks B + C (v0.9.0) add `trips.merged_into_id` / `merged_at` / `merge_audit` columns plus a `trip_merge_dismissals` table; introduce two new pure modules under `src/trip_tracker/trips/` for home inference and consolidation candidates; add merge / undo / dismiss routes; and add a daily saq cron that hard-deletes soft-merged trips after 7 days. Every `select(Trip)` site in the codebase gains an explicit `WHERE merged_into_id IS NULL` filter.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 (async), PostgreSQL, Alembic migrations, saq workers, Pydantic v2, pytest with `pytest-asyncio`, ruff/mypy/djLint via pre-commit.

**Branch strategy:** Cut `feat/phase-9-dedup-and-merge` from current `main` (`b6dd211`). Two tags off this branch: `v0.8.1` (after Track A wraps) and `v0.9.0` (after Track B + C wrap). Single PR per release tag.

**Constraints (carry through every task):**
- Any CCR-delegated subtask MUST forbid edits to `tests/conftest.py`, `.pre-commit-config.yaml`, `Dockerfile*`, `.github/workflows/*`, `pyproject.toml` (per `feedback_remote-agent-shared-infra.md`).
- Never use `git add .` / `git add -A` (per `feedback_git-add-precision.md`). Stage explicit paths or use `git commit -am` for already-tracked files.
- ForwardEmail webhook returns 200 (per `feedback_forwardemail-requires-200.md`). Phase 9 does NOT touch the FE adapter.

---

## File Structure

### Track A — v0.8.1 (parse-time dedup)

```
migrations/versions/2026_05_03_<HHMM>_<rev>_phase9a_dedup_status.py    NEW
src/trip_tracker/parsers/dedup.py                                      NEW
src/trip_tracker/parsers/dispatch.py                                   MODIFY (insert dedup gate)
src/trip_tracker/worker.py                                             MODIFY (header rebind on dedup)
src/trip_tracker/routes/inbox.py                                       MODIFY (bucket + not-a-duplicate)
src/trip_tracker/templates/inbox/_bucket_duplicates.html               MODIFY (already exists as stub)
tests/test_parsers_dedup.py                                            NEW
tests/test_workers_parse.py                                            MODIFY/CREATE (dedup integration)
tests/test_routes_inbox.py                                             MODIFY (bucket + not-a-duplicate)
```

### Tracks B + C — v0.9.0 (consolidation + merge)

```
migrations/versions/2026_05_<DD>_<HHMM>_<rev>_phase9bc_merge_columns.py NEW
src/trip_tracker/models/trip.py                                        MODIFY (3 new columns)
src/trip_tracker/models/trip_merge_dismissal.py                        NEW
src/trip_tracker/models/__init__.py                                    MODIFY (export)
src/trip_tracker/trips/__init__.py                                     NEW
src/trip_tracker/trips/home.py                                         NEW
src/trip_tracker/trips/consolidation.py                                NEW
src/trip_tracker/trips/merge.py                                        NEW (single-txn reassignment helper)
src/trip_tracker/routes/trips.py                                       MODIFY (410, banner, merge actions, filter)
src/trip_tracker/routes/inbox.py                                       MODIFY (target_trip param, preview banner)
src/trip_tracker/routes/ics.py                                         MODIFY (410)
src/trip_tracker/routes/segments.py                                    MODIFY (filter)
src/trip_tracker/routes/documents.py                                   MODIFY (filter)
src/trip_tracker/routes/map.py                                         MODIFY (filter)
src/trip_tracker/auth/deps.py                                          MODIFY (filter, if relevant — verify)
src/trip_tracker/search/reindex.py                                     MODIFY (filter)
src/trip_tracker/parsers/cluster.py                                    MODIFY (filter — verify usage)
src/trip_tracker/worker.py                                             MODIFY (purge_merged_trips cron task)
src/trip_tracker/templates/trips/detail.html                           MODIFY (banner + dropdown + dialog)
src/trip_tracker/templates/trips/_merge_undo_flash.html                NEW
src/trip_tracker/templates/inbox/_confirm_preview_banner.html          NEW
tests/test_trips_home.py                                               NEW
tests/test_trips_consolidation.py                                      NEW
tests/test_routes_trips_merge.py                                       NEW
tests/test_routes_trips_undo_merge.py                                  NEW
tests/test_routes_trips_dismiss.py                                     NEW
tests/test_workers_cleanup.py                                          NEW
tests/test_routes_inbox_confirm_target_trip.py                         NEW
tests/test_routes_trip_filters.py                                      NEW (regression: soft-deleted excluded everywhere)
```

---

## Pre-flight

### Task P1: Cut branch + verify clean baseline

**Files:** none (branch operation)

- [ ] **Step 1: Cut branch from current main**

```bash
cd /Users/owine/Git/trip-tracker
git fetch origin
git checkout main
git pull --ff-only
git checkout -b feat/phase-9-dedup-and-merge
git rev-parse HEAD  # verify == b6dd211 or later
```

- [ ] **Step 2: Verify clean baseline**

Run: `uv run pytest -q`
Expected: 507 tests pass (state captured in `project_jsonld-and-auto-expense.md`).

Run: `uv run pre-commit run --all-files`
Expected: all hooks green.

- [ ] **Step 3: No commit yet — branch is just sitting at main HEAD**

---

# TRACK A — v0.8.1 (parse-time duplicate detection)

## Task A1: Alembic migration for `parse_status='duplicate'`

**Files:**
- Create: `migrations/versions/2026_05_03_<HHMM>_<rev>_phase9a_dedup_status.py`

The current code stores `parse_status` as plain text with no DB-level CHECK constraint (verified earlier). This migration is documentation-only DDL — no actual schema change. It exists as a marker so the parse-status vocabulary change is auditable in version history, and is the place to extend the constraint if a future migration adds one.

- [ ] **Step 1: Generate migration skeleton**

```bash
cd /Users/owine/Git/trip-tracker
uv run alembic revision -m "phase9a dedup status"
# note the generated filename
```

- [ ] **Step 2: Edit the migration to be doc-only with a check**

Replace the body so `upgrade()` and `downgrade()` are bodies that explicitly assert no CHECK constraint exists, and document the new `'duplicate'` value.

```python
"""phase9a dedup status

Documentation-only migration: extends the parse_status vocabulary
to include 'duplicate'. raw_emails.parse_status is currently a plain
text column with no CHECK constraint, so no DDL is required.

If a CHECK constraint on raw_emails.parse_status is ever added in
a future migration, that migration MUST include 'duplicate' in the
allowed value list.

Revision ID: <auto>
Revises: <auto>
Create Date: 2026-05-03 ...
"""
from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # No-op DDL. Documented vocabulary extension only.
    # Verify no CHECK constraint exists at upgrade time.
    bind = op.get_bind()
    result = bind.execute(sa.text(
        "SELECT 1 FROM information_schema.check_constraints "
        "WHERE constraint_name LIKE 'ck_raw_emails_parse_status%'"
    )).first()
    assert result is None, (
        "A CHECK constraint exists on raw_emails.parse_status. "
        "Update this migration to extend it to include 'duplicate'."
    )


def downgrade() -> None:
    pass
```

- [ ] **Step 3: Run migrations + verify**

Run: `uv run alembic upgrade head`
Expected: migration applies cleanly.

Run: `uv run alembic downgrade -1 && uv run alembic upgrade head`
Expected: round-trip succeeds.

- [ ] **Step 4: Commit**

```bash
git add migrations/versions/2026_05_03_*phase9a_dedup_status.py
git commit -m "feat(phase9a): document parse_status='duplicate' vocabulary"
```

---

## Task A2: `parsers/dedup.py` — strong match (conf# + provider)

**Files:**
- Create: `src/trip_tracker/parsers/dedup.py`
- Test: `tests/test_parsers_dedup.py`

- [ ] **Step 1: Write failing tests for strong match**

```python
# tests/test_parsers_dedup.py
"""Dedup gate: strong match on (provider_normalized, confirmation_number)."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.parsers.dedup import find_existing_segment
from trip_tracker.schemas.segments import SegmentDraft  # adjust import to actual location


async def _seed_segment(
    db: AsyncSession,
    *,
    user: User,
    trip: Trip,
    type_: str = "flight",
    provider: str | None = "Air France",
    confirmation: str | None = "XM8SK3",
    start_at: datetime | None = None,
    start_iata: str | None = "JFK",
    end_iata: str | None = "CDG",
    status: str = "confirmed",
) -> Segment:
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type=type_,
        status=status,
        confirmation_number=confirmation,
        provider=provider,
        start_at=start_at or datetime(2026, 6, 4, 16, 55, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": start_iata} if start_iata else None,
        end_location={"iata": end_iata} if end_iata else None,
        details={},
        parse_source="test",
        parse_confidence=0.9,
    )
    db.add(seg)
    await db.flush()
    return seg


@pytest.mark.asyncio
async def test_strong_match_same_conf_same_provider(
    db_session, user_with_trip,
):
    user, trip = user_with_trip
    seeded = await _seed_segment(db_session, user=user, trip=trip)
    draft = SegmentDraft(
        type="flight",
        confirmation_number="XM8SK3",
        provider="Air France",
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),  # different time, doesn't matter
        start_tz="UTC",
        start_location={"iata": "LAX"},  # different airports, doesn't matter
        end_location={"iata": "ORD"},
    )
    hit = await find_existing_segment(db_session, user.id, draft)
    assert hit is not None
    assert hit.id == seeded.id


@pytest.mark.asyncio
async def test_strong_match_case_insensitive_provider(
    db_session, user_with_trip,
):
    user, trip = user_with_trip
    seeded = await _seed_segment(db_session, user=user, trip=trip, provider="AIR FRANCE")
    draft = SegmentDraft(
        type="flight",
        confirmation_number="XM8SK3",
        provider="  air france  ",  # whitespace + case
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
    )
    hit = await find_existing_segment(db_session, user.id, draft)
    assert hit is not None
    assert hit.id == seeded.id


@pytest.mark.asyncio
async def test_strong_match_different_provider_returns_none(
    db_session, user_with_trip,
):
    user, trip = user_with_trip
    await _seed_segment(db_session, user=user, trip=trip, provider="Air France")
    draft = SegmentDraft(
        type="flight",
        confirmation_number="XM8SK3",
        provider="Delta",
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
    )
    assert await find_existing_segment(db_session, user.id, draft) is None


@pytest.mark.asyncio
async def test_strong_match_null_conf_returns_none(
    db_session, user_with_trip,
):
    user, trip = user_with_trip
    await _seed_segment(db_session, user=user, trip=trip, confirmation=None)
    draft = SegmentDraft(
        type="flight",
        confirmation_number=None,
        provider="Air France",
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
    )
    assert await find_existing_segment(db_session, user.id, draft) is None


@pytest.mark.asyncio
async def test_owner_scope_excludes_other_users(
    db_session, user_with_trip, other_user_with_trip,
):
    user, trip = user_with_trip
    other, _other_trip = other_user_with_trip
    await _seed_segment(db_session, user=other, trip=_other_trip)  # owned by other
    draft = SegmentDraft(
        type="flight",
        confirmation_number="XM8SK3",
        provider="Air France",
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
    )
    assert await find_existing_segment(db_session, user.id, draft) is None


@pytest.mark.asyncio
async def test_cancelled_segments_excluded(
    db_session, user_with_trip,
):
    user, trip = user_with_trip
    await _seed_segment(db_session, user=user, trip=trip, status="cancelled")
    draft = SegmentDraft(
        type="flight",
        confirmation_number="XM8SK3",
        provider="Air France",
        start_at=datetime(2026, 7, 1, 10, 0, tzinfo=UTC),
        start_tz="UTC",
    )
    assert await find_existing_segment(db_session, user.id, draft) is None
```

If the test file references fixtures (`user_with_trip`, `other_user_with_trip`) that don't exist, add them to `tests/conftest.py` ONLY if `feedback_remote-agent-shared-infra.md` permits — for the human author this is fine; for any CCR delegate, add them to a new `tests/fixtures/dedup.py` and import.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers_dedup.py -v`
Expected: ImportError on `trip_tracker.parsers.dedup` (module doesn't exist yet).

- [ ] **Step 3: Implement strong match**

```python
# src/trip_tracker/parsers/dedup.py
"""Dedup gate: find existing Segment matching a SegmentDraft.

Match rules in priority order:
  1. Strong:  (provider_normalized, confirmation_number) — both non-null.
  2. Medium (transit):  type + start_at±N min + IATA pair.
  3. Medium (lodging):  type='lodging' + date(start_at) + hotel name CI.
  4. No match below medium. Fuzzy provider matching is deliberately excluded.

Match candidates are scoped to owner_user_id and exclude cancelled segments.
"""
from __future__ import annotations

import uuid

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.schemas.segments import SegmentDraft  # adjust


def _normalize_provider(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


async def _strong_match(
    db: AsyncSession, owner_user_id: uuid.UUID, draft: SegmentDraft,
) -> Segment | None:
    if not draft.confirmation_number:
        return None
    provider_norm = _normalize_provider(draft.provider)
    if not provider_norm:
        return None
    stmt = (
        select(Segment)
        .where(
            Segment.owner_user_id == owner_user_id,
            Segment.status != "cancelled",
            Segment.confirmation_number == draft.confirmation_number,
            func.lower(func.trim(Segment.provider)) == provider_norm,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_existing_segment(
    db: AsyncSession, owner_user_id: uuid.UUID, draft: SegmentDraft,
) -> Segment | None:
    """Return the first existing Segment matching draft, or None."""
    if hit := await _strong_match(db, owner_user_id, draft):
        return hit
    # Medium match in next task.
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_parsers_dedup.py -v -k "strong or owner_scope or cancelled"`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/trip_tracker/parsers/dedup.py tests/test_parsers_dedup.py
git commit -m "feat(phase9a): dedup strong match on conf# + provider"
```

---

## Task A3: `parsers/dedup.py` — medium match (flights/trains/transfers + lodging)

**Files:**
- Modify: `src/trip_tracker/parsers/dedup.py`
- Modify: `tests/test_parsers_dedup.py`

- [ ] **Step 1: Add failing medium-match tests**

```python
# tests/test_parsers_dedup.py — additions

@pytest.mark.asyncio
async def test_medium_flight_within_30min_iata_match(db_session, user_with_trip):
    user, trip = user_with_trip
    seeded = await _seed_segment(
        db_session, user=user, trip=trip,
        confirmation=None, provider=None,  # no strong-match data
        start_at=datetime(2026, 6, 4, 16, 55, tzinfo=UTC),
        start_iata="JFK", end_iata="CDG",
    )
    draft = SegmentDraft(
        type="flight",
        confirmation_number=None,
        provider=None,
        start_at=datetime(2026, 6, 4, 17, 10, tzinfo=UTC),  # +15 min
        start_tz="UTC",
        start_location={"iata": "JFK"},
        end_location={"iata": "CDG"},
    )
    hit = await find_existing_segment(db_session, user.id, draft)
    assert hit and hit.id == seeded.id


@pytest.mark.asyncio
async def test_medium_flight_31min_apart_returns_none(db_session, user_with_trip):
    user, trip = user_with_trip
    await _seed_segment(
        db_session, user=user, trip=trip,
        confirmation=None, provider=None,
        start_at=datetime(2026, 6, 4, 16, 55, tzinfo=UTC),
        start_iata="JFK", end_iata="CDG",
    )
    draft = SegmentDraft(
        type="flight", confirmation_number=None, provider=None,
        start_at=datetime(2026, 6, 4, 17, 26, tzinfo=UTC),  # +31 min
        start_tz="UTC",
        start_location={"iata": "JFK"}, end_location={"iata": "CDG"},
    )
    assert await find_existing_segment(db_session, user.id, draft) is None


@pytest.mark.asyncio
async def test_medium_flight_different_iata_returns_none(db_session, user_with_trip):
    user, trip = user_with_trip
    await _seed_segment(
        db_session, user=user, trip=trip,
        confirmation=None, provider=None,
        start_iata="JFK", end_iata="CDG",
    )
    draft = SegmentDraft(
        type="flight", confirmation_number=None, provider=None,
        start_at=datetime(2026, 6, 4, 17, 0, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": "JFK"}, end_location={"iata": "ORY"},
    )
    assert await find_existing_segment(db_session, user.id, draft) is None


@pytest.mark.asyncio
async def test_medium_lodging_same_hotel_same_date(db_session, user_with_trip):
    user, trip = user_with_trip
    seeded = await _seed_segment(
        db_session, user=user, trip=trip, type_="lodging",
        confirmation=None, provider=None,
        start_at=datetime(2026, 6, 5, 15, 0, tzinfo=UTC),
        start_iata=None, end_iata=None,
    )
    seeded.start_location = {"name": "Hotel des Grands Boulevards"}
    await db_session.flush()
    draft = SegmentDraft(
        type="lodging", confirmation_number=None, provider=None,
        start_at=datetime(2026, 6, 5, 17, 30, tzinfo=UTC),  # same date
        start_tz="UTC",
        start_location={"name": "HOTEL DES GRANDS BOULEVARDS"},  # case differs
    )
    hit = await find_existing_segment(db_session, user.id, draft)
    assert hit and hit.id == seeded.id


@pytest.mark.asyncio
async def test_cross_type_conf_does_not_collide(db_session, user_with_trip):
    """Lodging conf# 'ABC123' must NOT match a flight with conf# 'ABC123'."""
    user, trip = user_with_trip
    await _seed_segment(
        db_session, user=user, trip=trip, type_="lodging",
        provider="Marriott", confirmation="ABC123",
    )
    draft = SegmentDraft(
        type="flight", confirmation_number="ABC123", provider="Marriott",
        start_at=datetime(2026, 6, 4, 17, 0, tzinfo=UTC), start_tz="UTC",
    )
    # Strong match still hits (conf+provider both equal). This is correct
    # per the spec — it's a TYPE-collision, not a key-collision. Verify:
    # spec §3.1 rule 1 doesn't include type. Add type to strong match if
    # the team decides cross-type strong matches are wrong.
    hit = await find_existing_segment(db_session, user.id, draft)
    assert hit is not None  # current behavior; flag for follow-up if wrong
```

Note on the last test: the spec's strong-match rule does NOT include `type`. If during implementation this is judged a real risk (forwarded receipts where two different services share a conf#), add `type` to the strong-match clause. Track as plan-time decision; default = match spec.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_parsers_dedup.py -v -k "medium or cross_type"`
Expected: medium tests fail (NotImplemented or wrong return).

- [ ] **Step 3: Implement medium match**

```python
# src/trip_tracker/parsers/dedup.py — additions

from datetime import timedelta

_MEDIUM_TIME_WINDOW_MIN = 30  # tunable; make configurable via Settings if churned

_TRANSIT_TYPES = frozenset({"flight", "train", "transfer"})


async def _medium_transit_match(
    db: AsyncSession, owner_user_id: uuid.UUID, draft: SegmentDraft,
) -> Segment | None:
    if draft.type not in _TRANSIT_TYPES:
        return None
    start_iata = (draft.start_location or {}).get("iata")
    end_iata = (draft.end_location or {}).get("iata")
    if not (start_iata and end_iata):
        return None
    window = timedelta(minutes=_MEDIUM_TIME_WINDOW_MIN)
    stmt = (
        select(Segment)
        .where(
            Segment.owner_user_id == owner_user_id,
            Segment.status != "cancelled",
            Segment.type == draft.type,
            Segment.start_at.between(draft.start_at - window, draft.start_at + window),
            Segment.start_location["iata"].astext == start_iata,
            Segment.end_location["iata"].astext == end_iata,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _medium_lodging_match(
    db: AsyncSession, owner_user_id: uuid.UUID, draft: SegmentDraft,
) -> Segment | None:
    if draft.type != "lodging":
        return None
    name = (draft.start_location or {}).get("name")
    if not name:
        return None
    stmt = (
        select(Segment)
        .where(
            Segment.owner_user_id == owner_user_id,
            Segment.status != "cancelled",
            Segment.type == "lodging",
            func.date(Segment.start_at) == draft.start_at.date(),
            func.lower(Segment.start_location["name"].astext) == name.strip().lower(),
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_existing_segment(
    db: AsyncSession, owner_user_id: uuid.UUID, draft: SegmentDraft,
) -> Segment | None:
    if hit := await _strong_match(db, owner_user_id, draft):
        return hit
    if hit := await _medium_transit_match(db, owner_user_id, draft):
        return hit
    if hit := await _medium_lodging_match(db, owner_user_id, draft):
        return hit
    return None
```

- [ ] **Step 4: Run all dedup tests**

Run: `uv run pytest tests/test_parsers_dedup.py -v`
Expected: all 11–12 tests pass.

- [ ] **Step 5: Commit**

```bash
git add src/trip_tracker/parsers/dedup.py tests/test_parsers_dedup.py
git commit -m "feat(phase9a): dedup medium match for transit + lodging"
```

---

## Task A4: Wire dedup into the parse worker

**Files:**
- Modify: `src/trip_tracker/parsers/dispatch.py` (verify exact insertion point) OR `src/trip_tracker/worker.py::parse_raw_email`
- Test: `tests/test_workers_parse.py` (extend or create)

The dedup gate sits between strategy execution and persistence. Read `parsers/dispatch.py` first to identify where drafts are returned vs persisted. The hook may live in `worker.py` if `dispatch_parse` returns drafts and the worker persists.

- [ ] **Step 1: Read existing flow**

```bash
grep -n "persist\|return.*outcome\|drafts\|SegmentDraft" \
  src/trip_tracker/parsers/dispatch.py src/trip_tracker/worker.py
```

Identify: where do `SegmentDraft`s become `Segment` rows? That's where the gate lands.

- [ ] **Step 2: Write the all-deduped integration test**

```python
# tests/test_workers_parse.py — additions
"""Integration: re-forwarding the same Trainline confirmation deduplicates."""

@pytest.mark.asyncio
async def test_reforward_same_email_lands_in_duplicate_bucket(
    db_session, user_with_alias, mock_strategies_returning_trainline_draft,
):
    """First forward → 1 segment + 1 expense. Second forward (different
    Message-ID, same conf#) → 0 new segments, raw_email.parse_status='duplicate'.
    """
    # First forward
    raw1 = await _ingest_via_webhook(...)
    await parse_raw_email({"db": db_session, ...}, raw_email_id=str(raw1.id))
    seg_count_1 = await _count_segments(db_session, raw1.owner)
    assert seg_count_1 == 1

    # Second forward, same conf# different Message-ID
    raw2 = await _ingest_via_webhook(..., message_id="<reforwarded@x.com>")
    await parse_raw_email({"db": db_session, ...}, raw_email_id=str(raw2.id))
    seg_count_2 = await _count_segments(db_session, raw2.owner)
    assert seg_count_2 == 1, "Second forward must NOT create a new segment"

    await db_session.refresh(raw2)
    assert raw2.parse_status == "duplicate"
    assert "X-Tt-Dedup-Against" in raw2.headers
```

Also add unit-flavored tests for the worker hook itself: all-deduped, mixed, all-fresh paths. Use `monkeypatch` to make `find_existing_segment` return controlled values rather than seeding real segments — keeps the worker test focused on the branch logic.

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_workers_parse.py -v -k "reforward or dedup"`
Expected: FAIL — second forward still creates a second segment.

- [ ] **Step 4: Insert the dedup gate**

In `worker.py::parse_raw_email` (or `dispatch.py`, wherever drafts are converted to ORM rows), after `outcome = await dispatch_parse(...)`:

```python
from trip_tracker.parsers.dedup import find_existing_segment

# ... existing code computes `drafts: list[SegmentDraft]` ...

matched: list[tuple[SegmentDraft, Segment]] = []
fresh: list[SegmentDraft] = []
for d in drafts:
    existing = await find_existing_segment(db, owner_user_id, d)
    if existing is not None:
        matched.append((d, existing))
    else:
        fresh.append(d)

if drafts and not fresh:
    raw.parse_status = "duplicate"
    raw.headers = {
        **(raw.headers or {}),
        "X-Tt-Dedup-Against": [str(s.id) for _, s in matched],
    }
    # Return early — NO segments persisted, NO auto-Expense (auto-Expense
    # fires from /inbox/<id>/confirm based on Segment.where(raw_email_id=raw.id),
    # which will return [] here).
    await db.commit()
    return

if matched:
    raw.headers = {
        **(raw.headers or {}),
        "X-Tt-Dedup-Partial": [
            {"draft_summary": str(d), "existing_id": str(s.id)}
            for d, s in matched
        ],
    }

# Persist only `fresh` drafts. (Existing persistence code, but iterate `fresh`
# instead of `drafts`.)
```

If `dispatch_parse` already persists internally, refactor it to return drafts only and let the worker own persistence. Add this refactor as a sub-step. Keep the change minimal — single function signature update + test fix.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_workers_parse.py -v`
Expected: all parse-worker tests including new ones pass.

- [ ] **Step 6: Run full suite to catch regressions**

Run: `uv run pytest -q`
Expected: 507 + new tests, all green.

- [ ] **Step 7: Commit**

```bash
git add src/trip_tracker/worker.py src/trip_tracker/parsers/dispatch.py tests/test_workers_parse.py
git commit -m "feat(phase9a): wire dedup gate into parse worker"
```

---

## Task A5: Inbox `duplicate_rows` bucket + template

**Files:**
- Modify: `src/trip_tracker/routes/inbox.py:78-118` (the `inbox_list` handler)
- Modify: `src/trip_tracker/templates/inbox/_bucket_duplicates.html`
- Modify: `src/trip_tracker/templates/inbox/list.html` (verify the slot includes the partial)
- Modify: `tests/test_routes_inbox.py`

- [ ] **Step 1: Failing test — duplicates bucket appears in inbox response**

```python
# tests/test_routes_inbox.py — additions

@pytest.mark.asyncio
async def test_inbox_surfaces_duplicate_rows(client, seeded_user, db_session):
    raw = RawEmail(
        to_address=f"{seeded_user.alias_local}@trips.example.com",
        from_address="forwards@example.com",
        message_id="<dup-1@example.com>",
        parse_status="duplicate",
        headers={"X-Tt-Dedup-Against": [str(uuid.uuid4())]},
        body=b"raw mime",
        ...
    )
    db_session.add(raw)
    await db_session.commit()

    r = await client.get("/inbox", headers=auth_headers(seeded_user))
    assert r.status_code == 200
    assert "<dup-1@example.com>" in r.text or "duplicate" in r.text.lower()
    # Tighter: parse the rendered HTML and assert the duplicates section
    # contains exactly one row.
```

- [ ] **Step 2: Run test to verify failure**

Run: `uv run pytest tests/test_routes_inbox.py::test_inbox_surfaces_duplicate_rows -v`
Expected: FAIL — `duplicate_rows` is hardcoded `[]` in `routes/inbox.py:117`.

- [ ] **Step 3: Populate `duplicate_rows`**

In `routes/inbox.py::inbox_list`, replace the hardcoded `[]`:

```python
dup_rows = (
    (
        await db.execute(
            select(RawEmail)
            .where(RawEmail.parse_status == "duplicate", own)
            .order_by(RawEmail.received_at.desc())
            .limit(50)
        )
    )
    .scalars()
    .all()
)
return templates.TemplateResponse(
    request,
    "inbox/list.html",
    {
        "user": user,
        "review_rows": review_rows,
        "no_seg_rows": no_seg_rows,
        "duplicate_rows": dup_rows,
    },
)
```

- [ ] **Step 4: Update `_bucket_duplicates.html`**

Render the rows. Each row links to the existing segment(s) referenced by `X-Tt-Dedup-Against`. The partial already exists from Phase 3.5; verify and update.

```jinja
{# templates/inbox/_bucket_duplicates.html #}
{% if duplicate_rows %}
<section class="inbox-bucket inbox-bucket-duplicate">
  <h2>Duplicates ({{ duplicate_rows|length }})</h2>
  <ul>
    {% for raw in duplicate_rows %}
      <li>
        <span class="from">{{ raw.from_address }}</span>
        <span class="subject">{{ raw.subject or '(no subject)' }}</span>
        <span class="received">{{ raw.received_at.strftime('%Y-%m-%d') }}</span>
        {% set against = (raw.headers or {}).get('X-Tt-Dedup-Against', []) %}
        {% if against %}
          <span class="matched">
            matches existing
            {% for sid in against %}
              <a href="/segments/{{ sid }}">{{ sid[:8] }}</a>{{ ", " if not loop.last }}
            {% endfor %}
          </span>
        {% endif %}
        <form method="post" action="/inbox/{{ raw.id }}/discard" class="inline">
          <button type="submit">Discard</button>
        </form>
        <form method="post" action="/inbox/{{ raw.id }}/not-a-duplicate" class="inline">
          <button type="submit">Not a duplicate</button>
        </form>
      </li>
    {% endfor %}
  </ul>
</section>
{% endif %}
```

- [ ] **Step 5: Run test to verify pass**

Run: `uv run pytest tests/test_routes_inbox.py -v`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add src/trip_tracker/routes/inbox.py \
        src/trip_tracker/templates/inbox/_bucket_duplicates.html \
        tests/test_routes_inbox.py
git commit -m "feat(phase9a): populate inbox duplicates bucket"
```

---

## Task A6: `POST /inbox/<raw_id>/not-a-duplicate` route

**Files:**
- Modify: `src/trip_tracker/routes/inbox.py` (add new handler near `reparse`)
- Modify: `tests/test_routes_inbox.py`

- [ ] **Step 1: Failing tests**

```python
# tests/test_routes_inbox.py — additions

@pytest.mark.asyncio
async def test_not_a_duplicate_resets_to_pending_and_clears_header(
    client, seeded_user, db_session, mock_enqueue_parse,
):
    raw = await _seed_raw_email(
        db_session, user=seeded_user,
        parse_status="duplicate",
        headers={"X-Tt-Dedup-Against": ["abc"]},
    )
    r = await client.post(
        f"/inbox/{raw.id}/not-a-duplicate",
        headers=auth_headers(seeded_user),
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/inbox"

    await db_session.refresh(raw)
    assert raw.parse_status == "pending"
    assert "X-Tt-Dedup-Against" not in raw.headers
    mock_enqueue_parse.assert_called_once()


@pytest.mark.asyncio
async def test_not_a_duplicate_other_user_returns_404(
    client, seeded_user, other_user, db_session,
):
    raw = await _seed_raw_email(db_session, user=other_user, parse_status="duplicate")
    r = await client.post(
        f"/inbox/{raw.id}/not-a-duplicate",
        headers=auth_headers(seeded_user),
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify failure (404 / 405)**

- [ ] **Step 3: Implement**

```python
# src/trip_tracker/routes/inbox.py — add near reparse

@router.post("/{raw_id}/not-a-duplicate", response_model=None)
async def not_a_duplicate(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """User overrides the dedup verdict. Clear the header, requeue parse."""
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "pending"
    new_headers = dict(raw.headers or {})
    new_headers.pop("X-Tt-Dedup-Against", None)
    raw.headers = new_headers
    await db.commit()
    settings: Settings = request.app.state.settings
    await enqueue_parse(settings, raw.id)
    return RedirectResponse("/inbox", status_code=303)
```

- [ ] **Step 4: Run + commit**

Run: `uv run pytest tests/test_routes_inbox.py -v`
Expected: all green.

```bash
git add src/trip_tracker/routes/inbox.py tests/test_routes_inbox.py
git commit -m "feat(phase9a): not-a-duplicate inbox action"
```

---

## Task A7: v0.8.1 manual smoke + tag

**Files:** none (release operation)

- [ ] **Step 1: Run full suite + pre-commit**

```bash
uv run pytest -q
uv run pre-commit run --all-files
```

Expected: 507 + ~19 new = ~526 tests passing; coverage ≥88%.

- [ ] **Step 2: Manual smoke** (per spec §6.4 v0.8.1)

Forward a Trainline confirmation to your test alias → confirm in inbox → verify auto-Expense fires. Forward the SAME email again (different Message-ID, e.g., re-forward via Apple Mail) → verify it lands in the duplicates bucket with a link to the existing segment → verify NO new Expense was created.

- [ ] **Step 3: Open PR for v0.8.1**

```bash
git push -u origin feat/phase-9-dedup-and-merge
gh pr create --title "feat: phase 9a — parse-time duplicate detection (v0.8.1)" \
  --body "$(cat <<'EOF'
## Summary
- Adds `parsers/dedup.py` with strong (conf# + provider) and medium (type + IATA + ±30min) match rules
- Wires the dedup gate into the parse worker between strategies and persistence
- Wires `duplicate_rows` bucket on /inbox + adds `not-a-duplicate` action
- Documentation-only Alembic migration documenting `parse_status='duplicate'`

Closes the gap noted in `project_duplicate-detection-gap.md`. Spec at `docs/superpowers/specs/2026-05-02-phase9-inbox-and-trip-consolidation-design.md` §3.1.

## Test plan
- [ ] uv run pytest -q (~526 tests)
- [ ] uv run pre-commit run --all-files
- [ ] Manual smoke: re-forward same Trainline confirmation, verify second lands in duplicates bucket and no new Expense
EOF
)"
```

- [ ] **Step 4: After CI green + ff-merge**

```bash
git checkout main
git pull --ff-only
git tag -s v0.8.1 -m "v0.8.1 — parse-time duplicate detection"
git push origin v0.8.1
```

- [ ] **Step 5: Update memory**

Edit `~/.claude/projects/-Users-owine-Git-trip-tracker/memory/project_duplicate-detection-gap.md` to mark shipped in v0.8.1; update MEMORY.md index entry. Schedule a release-verification routine to fire 7 days post-merge (same pattern as v0.8.0).

- [ ] **Step 6: Continue with v0.9.0 work on the same branch (or cut a fresh feature branch from new main)**

Recommendation: continue on `feat/phase-9-dedup-and-merge`. Track A is now in main; subsequent commits build on it.

```bash
git checkout feat/phase-9-dedup-and-merge
git rebase origin/main  # bring in any post-merge commits
```

---

# TRACK B + C — v0.9.0 (consolidation + merge)

## Task B1: Alembic migration — soft-delete columns + indexes + dismissals table

**Files:**
- Create: `migrations/versions/2026_05_<DD>_<HHMM>_<rev>_phase9bc_merge_columns.py`

- [ ] **Step 1: Generate skeleton + edit**

```bash
uv run alembic revision -m "phase9bc merge columns"
```

```python
"""phase9bc merge columns

Adds:
  - trips.merged_into_id, trips.merged_at, trips.merge_audit
  - ix_trips_owner_dates partial index (active trips only)
  - trip_merge_dismissals table

Per spec §4.2.

Revision ID: <auto>
Revises: <previous>
Create Date: 2026-05-<DD> ...
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


def upgrade() -> None:
    # 1. Trip soft-delete columns + audit
    op.add_column(
        "trips",
        sa.Column(
            "merged_into_id", postgresql.UUID(as_uuid=True), nullable=True,
        ),
    )
    op.add_column("trips", sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trips", sa.Column("merge_audit", postgresql.JSONB, nullable=True))
    op.create_foreign_key(
        "fk_trips_merged_into",
        "trips", "trips",
        ["merged_into_id"], ["id"],
        ondelete="SET NULL",
    )
    # SET NULL not CASCADE — if the target of a merge is later hard-deleted,
    # the source row should survive with a null pointer rather than vanish.

    # 2. Partial index for the owner+date range query (also covers the
    #    WHERE merged_into_id IS NULL filter clause everywhere).
    op.create_index(
        "ix_trips_owner_dates",
        "trips",
        ["created_by", "start_date", "end_date"],
        postgresql_where=sa.text("merged_into_id IS NULL"),
    )

    # 3. Per-pair dismissal table
    op.create_table(
        "trip_merge_dismissals",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_a_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trip_b_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "dismissed_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_a_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trip_b_id"], ["trips.id"], ondelete="CASCADE"),
        # Composite PK matches the ORM's primary_key=True declarations on
        # all three columns. Pair-uniqueness across LEAST/GREATEST is layered
        # on top via the UNIQUE INDEX below — the PK is plain (A,B) order,
        # the unique index canonicalizes pair order.
        sa.PrimaryKeyConstraint("user_id", "trip_a_id", "trip_b_id",
                                name="pk_trip_merge_dismissals"),
    )
    # Pair-uniqueness via expression UNIQUE INDEX. Postgres allows this
    # in a UNIQUE INDEX even though it can't be used as a PRIMARY KEY
    # expression. The LEAST/GREATEST canonicalization ensures (A,B) and
    # (B,A) are treated as the same pair regardless of insert order.
    op.execute(
        "CREATE UNIQUE INDEX uq_trip_merge_dismissals_pair "
        "ON trip_merge_dismissals ("
        "  user_id, "
        "  LEAST(trip_a_id, trip_b_id), "
        "  GREATEST(trip_a_id, trip_b_id)"
        ")"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_trip_merge_dismissals_pair")
    op.drop_table("trip_merge_dismissals")
    op.drop_index("ix_trips_owner_dates", table_name="trips")
    op.drop_constraint("fk_trips_merged_into", "trips", type_="foreignkey")
    op.drop_column("trips", "merge_audit")
    op.drop_column("trips", "merged_at")
    op.drop_column("trips", "merged_into_id")
```

- [ ] **Step 2: Run migration both directions**

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

Expected: both succeed. Verify columns + index in psql:

```bash
psql "$DATABASE_URL" -c "\d trips"
psql "$DATABASE_URL" -c "\d trip_merge_dismissals"
psql "$DATABASE_URL" -c "\di trips"
```

- [ ] **Step 3: Commit**

```bash
git add migrations/versions/2026_05_*phase9bc_merge_columns.py
git commit -m "feat(phase9bc): trip soft-delete columns + dismissals table"
```

---

## Task B2: Update `Trip` model + add `TripMergeDismissal` model

**Files:**
- Modify: `src/trip_tracker/models/trip.py`
- Create: `src/trip_tracker/models/trip_merge_dismissal.py`
- Modify: `src/trip_tracker/models/__init__.py` (export)

- [ ] **Step 1: Add columns to `Trip`**

```python
# src/trip_tracker/models/trip.py — additions

from typing import Any
from sqlalchemy.dialects.postgresql import JSONB

class Trip(Base):
    # ... existing columns ...
    merged_into_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="SET NULL"),
        nullable=True,
    )
    merged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    merge_audit: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True,
    )
```

- [ ] **Step 2: Create `TripMergeDismissal` model**

```python
# src/trip_tracker/models/trip_merge_dismissal.py
"""Dismissed trip-merge suggestions, per-user, per-pair."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.models.base import Base


class TripMergeDismissal(Base):
    __tablename__ = "trip_merge_dismissals"

    # No real PK — pair-uniqueness lives in the LEAST/GREATEST UNIQUE INDEX
    # created in the migration. We declare a composite primary_key here for
    # SQLAlchemy's sake but it does NOT match the unique index logic.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trip_a_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    trip_b_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        primary_key=True,
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
```

- [ ] **Step 3: Export from `models/__init__.py`**

Add `from trip_tracker.models.trip_merge_dismissal import TripMergeDismissal`.

- [ ] **Step 4: Verify with a smoke import**

Run: `uv run python -c "from trip_tracker.models import TripMergeDismissal, Trip; print('OK')"`

- [ ] **Step 5: Commit**

```bash
git add src/trip_tracker/models/trip.py \
        src/trip_tracker/models/trip_merge_dismissal.py \
        src/trip_tracker/models/__init__.py
git commit -m "feat(phase9bc): Trip soft-delete columns + TripMergeDismissal model"
```

---

## Task B3: `trips/home.py` — auto-infer home

**Files:**
- Create: `src/trip_tracker/trips/__init__.py` (empty if needed)
- Create: `src/trip_tracker/trips/home.py`
- Test: `tests/test_trips_home.py`

- [ ] **Step 1: Failing tests** (per spec §6.2)

```python
# tests/test_trips_home.py
"""infer_home: dominant endpoint city across last 20 confirmed segments."""
import pytest
from datetime import UTC, datetime, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from trip_tracker.trips.home import infer_home


async def _seed_segments(
    db: AsyncSession, user, *, endpoints: list[tuple[str, str]],  # (start, end)
    status: str = "confirmed",
    start_at_base: datetime | None = None,
) -> None:
    base = start_at_base or datetime(2026, 1, 1, tzinfo=UTC)
    for i, (s, e) in enumerate(endpoints):
        seg = Segment(
            trip_id=...,  # use a single shared trip for the user
            owner_user_id=user.id,
            type="flight",
            status=status,
            start_at=base + timedelta(days=i),
            start_tz="UTC",
            start_location={"city": s},
            end_location={"city": e},
            details={},
            parse_source="test",
            parse_confidence=0.9,
        )
        db.add(seg)
    await db.flush()


@pytest.mark.asyncio
async def test_top_endpoint_at_30pct_returns_city(db_session, user):
    # 6 of 20 endpoints (15 segments × 2 endpoints = 30) are NYC.
    # Wait: 15 segments contribute 30 endpoint observations.
    # Let's seed 10 segments → 20 endpoints, 6 of which are 'NYC'.
    pairs = [("NYC", "X")] * 3 + [("Y", "NYC")] * 3 + [("A", "B")] * 7
    await _seed_segments(db_session, user, endpoints=pairs)
    home = await infer_home(db_session, user.id)
    assert home == "NYC"


@pytest.mark.asyncio
async def test_below_30pct_returns_none(db_session, user):
    # 4 of 20 endpoints = 20% < 30%. Returns None.
    pairs = [("NYC", "X")] * 2 + [("Y", "NYC")] * 2 + [("A", "B")] * 8
    await _seed_segments(db_session, user, endpoints=pairs)
    home = await infer_home(db_session, user.id)
    assert home is None


@pytest.mark.asyncio
async def test_last_20_window_respected(db_session, user):
    # 21 NYC segments + 5 other-city segments more recent.
    # Only the 20 most recent count → other city dominates.
    pass


@pytest.mark.asyncio
async def test_cancelled_excluded(db_session, user):
    pairs_cancelled = [("NYC", "NYC")] * 10
    pairs_confirmed = [("PARIS", "PARIS")] * 10
    await _seed_segments(db_session, user, endpoints=pairs_cancelled, status="cancelled")
    await _seed_segments(db_session, user, endpoints=pairs_confirmed)
    home = await infer_home(db_session, user.id)
    assert home == "PARIS"


@pytest.mark.asyncio
async def test_empty_history_returns_none(db_session, user):
    home = await infer_home(db_session, user.id)
    assert home is None


@pytest.mark.asyncio
async def test_both_endpoints_contribute(db_session, user):
    # 5 segments where city X is start, 5 where city X is end → 10/20 = 50%.
    pairs = [("X", "Y")] * 5 + [("Z", "X")] * 5
    await _seed_segments(db_session, user, endpoints=pairs)
    home = await infer_home(db_session, user.id)
    assert home == "X"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run pytest tests/test_trips_home.py -v`
Expected: ImportError on `trip_tracker.trips.home`.

- [ ] **Step 3: Implement**

```python
# src/trip_tracker/trips/home.py
"""Auto-infer the user's 'home' from segment endpoint frequency.

Used by the consolidation candidate scorer to decide if a trip is 'open'
(no return-to-home segment yet) and whether a new segment is a closing leg.

No persisted column — recomputed per query. Cheap given the partial index.
"""
from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment

_LAST_N = 20
_DOMINANCE_FLOOR = 0.30  # 30% — see spec §3.2


async def infer_home(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Top endpoint city across the user's last N confirmed segments,
    if its share ≥ 30% of total endpoint observations. Else None.
    """
    stmt = (
        select(Segment.start_location, Segment.end_location)
        .where(
            Segment.owner_user_id == user_id,
            Segment.status == "confirmed",
        )
        .order_by(Segment.start_at.desc())
        .limit(_LAST_N)
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None

    counter: Counter[str] = Counter()
    total = 0
    for start_loc, end_loc in rows:
        for loc in (start_loc, end_loc):
            city = (loc or {}).get("city")
            if city:
                counter[city] += 1
                total += 1

    if total == 0:
        return None
    top_city, top_count = counter.most_common(1)[0]
    if (top_count / total) >= _DOMINANCE_FLOOR:
        return top_city
    return None
```

- [ ] **Step 4: Run tests + commit**

Run: `uv run pytest tests/test_trips_home.py -v`
Expected: 6 tests pass.

```bash
git add src/trip_tracker/trips/__init__.py \
        src/trip_tracker/trips/home.py \
        tests/test_trips_home.py
git commit -m "feat(phase9bc): auto-infer user home from segment endpoints"
```

---

## Task B4: `ConsolidationTarget` value object

**Files:**
- Create: `src/trip_tracker/trips/consolidation.py` (skeleton)
- Test: `tests/test_trips_consolidation.py` (skeleton)

- [ ] **Step 1: Failing test for `ConsolidationTarget.from_drafts` + `from_trip`**

```python
# tests/test_trips_consolidation.py — initial
import pytest
from trip_tracker.trips.consolidation import ConsolidationTarget


def test_from_drafts_extracts_endpoints():
    drafts = [
        SegmentDraft(type="flight", start_at=..., end_at=...,
                     start_location={"city": "JFK", "iata": "JFK"},
                     end_location={"city": "Paris", "iata": "CDG"}),
        SegmentDraft(type="lodging", start_at=..., end_at=...,
                     start_location={"name": "Hotel des Grands Boulevards", "city": "Paris"}),
    ]
    target = ConsolidationTarget.from_drafts(drafts)
    assert target.start_city == "JFK"
    assert target.end_city == "Paris"
    assert "CDG" in target.endpoint_iatas
```

- [ ] **Step 2: Implement skeleton**

```python
# src/trip_tracker/trips/consolidation.py
"""Trip consolidation suggestions — home-anchored with geometric fallback."""
from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import IntEnum

from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.schemas.segments import SegmentDraft  # adjust


class _Weight(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3


@dataclass(frozen=True)
class ConsolidationTarget:
    start_date: date
    end_date: date
    start_city: str | None
    end_city: str | None
    endpoint_iatas: frozenset[str]
    trip_id: uuid.UUID | None  # None for drafts

    @classmethod
    def from_trip(
        cls, trip: Trip, segments: Sequence[Segment],
    ) -> "ConsolidationTarget":
        ordered = sorted(segments, key=lambda s: s.start_at)
        start_city = (ordered[0].start_location or {}).get("city") if ordered else None
        end_city = (ordered[-1].end_location or {}).get("city") if ordered else None
        iatas: set[str] = set()
        for s in ordered:
            for loc in (s.start_location, s.end_location):
                iata = (loc or {}).get("iata")
                if iata:
                    iatas.add(iata)
        return cls(
            start_date=trip.start_date,
            end_date=trip.end_date,
            start_city=start_city,
            end_city=end_city,
            endpoint_iatas=frozenset(iatas),
            trip_id=trip.id,
        )

    @classmethod
    def from_drafts(cls, drafts: Sequence[SegmentDraft]) -> "ConsolidationTarget":
        ordered = sorted(drafts, key=lambda d: d.start_at)
        start_city = (ordered[0].start_location or {}).get("city") if ordered else None
        end_city = (ordered[-1].end_location or {}).get("city") if ordered else None
        iatas: set[str] = set()
        for d in ordered:
            for loc in (d.start_location, d.end_location):
                iata = (loc or {}).get("iata") if loc else None
                if iata:
                    iatas.add(iata)
        start_date = ordered[0].start_at.date() if ordered else date.today()
        end_date = (ordered[-1].end_at or ordered[-1].start_at).date() if ordered else start_date
        return cls(
            start_date=start_date,
            end_date=end_date,
            start_city=start_city,
            end_city=end_city,
            endpoint_iatas=frozenset(iatas),
            trip_id=None,
        )
```

- [ ] **Step 3: Run + commit**

```bash
git add src/trip_tracker/trips/consolidation.py tests/test_trips_consolidation.py
git commit -m "feat(phase9bc): ConsolidationTarget value object"
```

---

## Task B5: `consolidation_candidates` — home-anchored + geometric fallback

**Files:**
- Modify: `src/trip_tracker/trips/consolidation.py`
- Modify: `tests/test_trips_consolidation.py`

- [ ] **Step 1: Failing tests** (per spec §6.2 — 10 tests)

Cover all 10 test cases listed in the spec. Use a `geo` helper for distance — confirm `src/trip_tracker/geo/` exists and has a haversine; if not, add a minimal one.

- [ ] **Step 2: Run tests to verify failure**

- [ ] **Step 3: Implement**

```python
# src/trip_tracker/trips/consolidation.py — additions

from datetime import timedelta
from sqlalchemy import select
from trip_tracker.geo.distance import haversine_km  # confirm path
from trip_tracker.models.trip_merge_dismissal import TripMergeDismissal
from trip_tracker.trips.home import infer_home


@dataclass(frozen=True)
class ConsolidationCandidate:
    trip: Trip
    weight: _Weight


_GAP_DAYS_FALLBACK = 3  # geometric fallback only; tunable via Settings later
_DISTANCE_KM_LOW = 500


async def _user_trips_within_window(
    db: AsyncSession, user: User, target: ConsolidationTarget,
) -> list[Trip]:
    window = timedelta(days=_GAP_DAYS_FALLBACK)
    clauses = [
        Trip.created_by == user.id,
        Trip.merged_into_id.is_(None),
        Trip.start_date - window <= target.end_date,
        Trip.end_date + window >= target.start_date,
    ]
    # Conditional: only exclude the target trip when calling from trip-detail
    # (where we have a Trip already). For the inbox-confirm preview path,
    # target.trip_id is None and there's nothing to exclude.
    if target.trip_id is not None:
        clauses.append(Trip.id != target.trip_id)
    stmt = (
        select(Trip)
        .where(*clauses)
        .order_by(Trip.start_date.desc())
        .limit(50)
    )
    return list((await db.execute(stmt)).scalars().all())


async def _dismissed_pair_ids(
    db: AsyncSession, user: User, target_trip_id: uuid.UUID | None,
) -> frozenset[uuid.UUID]:
    if target_trip_id is None:
        return frozenset()
    stmt = select(
        TripMergeDismissal.trip_a_id, TripMergeDismissal.trip_b_id,
    ).where(
        TripMergeDismissal.user_id == user.id,
        (TripMergeDismissal.trip_a_id == target_trip_id) |
        (TripMergeDismissal.trip_b_id == target_trip_id),
    )
    rows = (await db.execute(stmt)).all()
    out: set[uuid.UUID] = set()
    for a, b in rows:
        out.add(b if a == target_trip_id else a)
    return frozenset(out)


async def consolidation_candidates(
    db: AsyncSession, user: User, target: ConsolidationTarget,
) -> list[ConsolidationCandidate]:
    home = await infer_home(db, user.id)
    dismissed = await _dismissed_pair_ids(db, user, target.trip_id)
    candidates: list[ConsolidationCandidate] = []

    for trip in await _user_trips_within_window(db, user, target):
        if trip.id in dismissed:
            continue
        # Build a transient ConsolidationTarget for the existing trip so we
        # can reuse endpoint logic.
        trip_segments = await _load_trip_segments(db, trip.id)  # helper
        trip_view = ConsolidationTarget.from_trip(trip, trip_segments)

        # Home-anchored:
        if home is not None and _trip_is_open(trip_view, home):
            if target.start_city == trip_view.end_city:
                candidates.append(ConsolidationCandidate(trip, _Weight.HIGH))
                continue
            if target.end_city == home and _trip_has_outbound_from_home(trip_view, home):
                candidates.append(ConsolidationCandidate(trip, _Weight.HIGH))
                continue

        # Geometric fallback:
        if _shared_endpoint_city(trip_view, target):
            candidates.append(ConsolidationCandidate(trip, _Weight.MEDIUM))
        elif _min_endpoint_distance_km(trip_view, target) <= _DISTANCE_KM_LOW:
            candidates.append(ConsolidationCandidate(trip, _Weight.LOW))

    candidates.sort(key=lambda c: (-c.weight, -c.trip.start_date.toordinal()))
    return candidates[:3]


# ... helper functions: _trip_is_open, _trip_has_outbound_from_home,
#     _shared_endpoint_city, _min_endpoint_distance_km, _load_trip_segments ...
```

- [ ] **Step 4: Run tests + commit**

```bash
git add src/trip_tracker/trips/consolidation.py tests/test_trips_consolidation.py
git commit -m "feat(phase9bc): consolidation_candidates with home-anchored + geometric fallback"
```

---

## Task B6: Filter clause audit — every `select(Trip)` site

**Files (each needs `WHERE merged_into_id IS NULL` added to its Trip queries):**
- Modify: `src/trip_tracker/routes/trips.py`
- Modify: `src/trip_tracker/routes/segments.py`
- Modify: `src/trip_tracker/routes/documents.py`
- Modify: `src/trip_tracker/routes/map.py`
- Modify: `src/trip_tracker/auth/deps.py` (verify usage — may not need changes)
- Modify: `src/trip_tracker/search/reindex.py`
- Modify: `src/trip_tracker/parsers/cluster.py` (verify usage)
- Test: `tests/test_routes_trip_filters.py` (NEW — regression coverage)

This is a single bulk task because each site is a one-line addition. Do them in one PR-sized commit so it's atomic.

- [ ] **Step 1: Write the regression test FIRST**

```python
# tests/test_routes_trip_filters.py
"""Soft-deleted trips must NOT appear in any user-facing listing."""

@pytest.mark.parametrize("path", [
    "/trips",
    "/segments",
    "/documents",
    "/map",
    # Add other listings that surface trips by id
])
@pytest.mark.asyncio
async def test_soft_deleted_trip_excluded(client, seeded_user, db_session, path):
    trip_active = await _seed_trip(db_session, seeded_user, title="Active Trip")
    trip_merged = await _seed_trip(
        db_session, seeded_user, title="Merged Trip",
        merged_into_id=trip_active.id, merged_at=datetime.now(UTC),
    )
    r = await client.get(path, headers=auth_headers(seeded_user))
    assert "Active Trip" in r.text
    assert "Merged Trip" not in r.text
```

- [ ] **Step 2: Run + verify the test fails on at least one path**

- [ ] **Step 3: Add the filter clause to each file**

For each `select(Trip)` site, add `.where(Trip.merged_into_id.is_(None))`. Use grep to verify no site is missed:

```bash
grep -rn "select(Trip)" src/trip_tracker --include="*.py"
```

Each result should now have `.where(Trip.merged_into_id.is_(None))` either inline or one line below.

- [ ] **Step 4: Run all tests**

Run: `uv run pytest -q`
Expected: full suite still passes; new regression test green on every path.

- [ ] **Step 5: Commit**

```bash
git add src/trip_tracker/routes/trips.py \
        src/trip_tracker/routes/segments.py \
        src/trip_tracker/routes/documents.py \
        src/trip_tracker/routes/map.py \
        src/trip_tracker/auth/deps.py \
        src/trip_tracker/search/reindex.py \
        src/trip_tracker/parsers/cluster.py \
        tests/test_routes_trip_filters.py
git commit -m "feat(phase9bc): filter soft-deleted trips from every Trip query"
```

---

## Task B7: `GET /trips/<id>` returns 410 for soft-deleted

**Files:**
- Modify: `src/trip_tracker/routes/trips.py`
- Modify: `src/trip_tracker/routes/ics.py`
- Test: `tests/test_routes_trips_merge.py` (partial — full merge tests in Task C1)

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_soft_deleted_trip_returns_410(client, seeded_user, db_session):
    target = await _seed_trip(db_session, seeded_user, title="Target")
    source = await _seed_trip(
        db_session, seeded_user, title="Merged",
        merged_into_id=target.id, merged_at=datetime.now(UTC),
    )
    r = await client.get(f"/trips/{source.id}", headers=auth_headers(seeded_user))
    assert r.status_code == 410
    assert str(target.id) in r.text or "Target" in r.text


@pytest.mark.asyncio
async def test_soft_deleted_ics_returns_410(client, seeded_user, db_session):
    target = await _seed_trip(db_session, seeded_user, title="Target")
    source = await _seed_trip(
        db_session, seeded_user, title="Merged",
        merged_into_id=target.id, merged_at=datetime.now(UTC),
    )
    r = await client.get(f"/ics/{source.id}.ics")  # ICS may be unauth
    assert r.status_code == 410
    assert r.content == b""
```

- [ ] **Step 2: Implement in `routes/trips.py::trip_detail`**

```python
trip = ...  # existing fetch (without merged_into_id filter for this route)
if trip.merged_into_id is not None:
    return Response(
        status_code=410,
        content=f"This trip was merged into {trip.merged_into_id}. "
                f"Visit /trips/{trip.merged_into_id} instead.",
        media_type="text/plain",
    )
```

Same shape in `routes/ics.py`.

- [ ] **Step 3: Run + commit**

```bash
git add src/trip_tracker/routes/trips.py src/trip_tracker/routes/ics.py tests/test_routes_trips_merge.py
git commit -m "feat(phase9bc): 410 Gone on soft-deleted trip GET + ICS"
```

---

## Task C1: `POST /trips/<source>/merge-into/<target>` route

**Files:**
- Create: `src/trip_tracker/trips/merge.py` (single-transaction helper)
- Modify: `src/trip_tracker/routes/trips.py` (route handler)
- Test: `tests/test_routes_trips_merge.py`

- [ ] **Step 1: Failing tests** (10 cases per spec §6.2)

Cover: happy path, 403 on non-owner source, 403 on non-owner target, 400 self-merge, 400 source already merged, 400 target already merged, 404 nonexistent ids, source GET returns 410 post-merge, source ICS returns 410 post-merge, listings exclude soft-deleted.

- [ ] **Step 2: Implement merge helper**

```python
# src/trip_tracker/trips/merge.py
"""Single-transaction merge of source trip into target trip.

Builds the merge_audit JSONB so undo can be lossless. See spec §3.3.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.document import Document
from trip_tracker.models.expense import Expense
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler


async def merge_trip_into(
    db: AsyncSession, source: Trip, target: Trip,
) -> dict:
    """Reassign FKs from source → target, populate merge_audit, soft-delete source.
    Returns the audit payload for the caller to flash.
    """
    # 1. Capture moved IDs for the audit
    moved_segment_ids = (await db.execute(
        select(Segment.id).where(Segment.trip_id == source.id)
    )).scalars().all()
    moved_expense_ids = (await db.execute(
        select(Expense.id).where(Expense.trip_id == source.id)
    )).scalars().all()
    moved_document_ids = (await db.execute(
        select(Document.id).where(Document.trip_id == source.id)
    )).scalars().all()

    # 2. Compute trip_traveler diff: which user_ids are on source but NOT on target
    source_users = (await db.execute(
        select(TripTraveler.user_id).where(TripTraveler.trip_id == source.id)
    )).scalars().all()
    target_users = (await db.execute(
        select(TripTraveler.user_id).where(TripTraveler.trip_id == target.id)
    )).scalars().all()
    added_traveler_user_ids = list(set(source_users) - set(target_users))

    # 3. Reassign FKs
    await db.execute(update(Segment).where(Segment.trip_id == source.id).values(trip_id=target.id))
    await db.execute(update(Expense).where(Expense.trip_id == source.id).values(trip_id=target.id))
    await db.execute(update(Document).where(Document.trip_id == source.id).values(trip_id=target.id))

    # 4. Trip_travelers: insert added rows, then delete source rows
    if added_traveler_user_ids:
        # Re-fetch the source rows to get role + other columns
        added_rows = (await db.execute(
            select(TripTraveler).where(
                TripTraveler.trip_id == source.id,
                TripTraveler.user_id.in_(added_traveler_user_ids),
            )
        )).scalars().all()
        for r in added_rows:
            db.add(TripTraveler(
                trip_id=target.id,
                user_id=r.user_id,
                role=r.role,  # adjust to actual schema
            ))
    await db.execute(delete(TripTraveler).where(TripTraveler.trip_id == source.id))

    # 5. Recompute target dates
    await db.execute(
        update(Trip)
        .where(Trip.id == target.id)
        .values(
            start_date=min(target.start_date, source.start_date),
            end_date=max(target.end_date, source.end_date),
            updated_at=datetime.now(UTC),
        )
    )

    # 6. Soft-delete source + populate audit
    audit = {
        "source_segment_ids": [str(i) for i in moved_segment_ids],
        "source_expense_ids": [str(i) for i in moved_expense_ids],
        "source_document_ids": [str(i) for i in moved_document_ids],
        "added_traveler_user_ids": [str(u) for u in added_traveler_user_ids],
        "source_start_date": source.start_date.isoformat(),
        "source_end_date": source.end_date.isoformat(),
        "schema_version": 1,
    }
    await db.execute(
        update(Trip).where(Trip.id == source.id).values(
            merged_into_id=target.id,
            merged_at=datetime.now(UTC),
            merge_audit=audit,
        )
    )
    return audit
```

- [ ] **Step 3: Implement route**

```python
# src/trip_tracker/routes/trips.py — additions

@router.post("/{source_id}/merge-into/{target_id}", response_model=None)
async def merge_into(
    source_id: uuid.UUID,
    target_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    # Existence first (so non-existent IDs don't leak ownership)
    source = (await db.execute(select(Trip).where(Trip.id == source_id))).scalar_one_or_none()
    target = (await db.execute(select(Trip).where(Trip.id == target_id))).scalar_one_or_none()
    if source is None or target is None:
        raise HTTPException(404)
    # Ownership
    if source.created_by != user.id or target.created_by != user.id:
        raise HTTPException(403)
    # Self-merge
    if source.id == target.id:
        raise HTTPException(400, detail="Cannot merge a trip into itself")
    # Either already merged
    if source.merged_into_id is not None or target.merged_into_id is not None:
        raise HTTPException(400, detail="One of the trips has already been merged")

    # Transaction handling: check the existing route pattern in this file.
    # FastAPI's `get_session` dependency in this codebase typically yields a
    # session whose transaction is implicit (autocommit-style with explicit
    # `await db.commit()` at the end). If `async with db.begin()` raises
    # "transaction already begun", drop the explicit `begin()` block and
    # call `await db.commit()` at the end instead — match the pattern used
    # by `routes/inbox.py::confirm` (auto-Expense flow).
    await merge_trip_into(db, source, target)
    await db.commit()

    return RedirectResponse(f"/trips/{target.id}?merged_from={source.id}", status_code=303)
```

- [ ] **Step 4: Run + commit**

```bash
git add src/trip_tracker/trips/merge.py \
        src/trip_tracker/routes/trips.py \
        tests/test_routes_trips_merge.py
git commit -m "feat(phase9bc): merge-into route with single-txn reassignment + audit"
```

---

## Task C2: `POST /trips/<target>/undo-merge/<source>`

**Files:**
- Modify: `src/trip_tracker/trips/merge.py` (add `undo_merge`)
- Modify: `src/trip_tracker/routes/trips.py`
- Test: `tests/test_routes_trips_undo_merge.py`

- [ ] **Step 1: Failing tests** (7 cases per spec §6.2 + revised totals)

Cover: within-window restore; after-window 410; 409 on chain (target re-merged); source 410 lifted post-undo; ICS live again post-undo; trip_travelers undo restores source-only rows without removing target's pre-existing collaborators (audit-driven); target trip_travelers added by merge are removed on undo (audit-driven).

- [ ] **Step 2: Implement undo helper**

Read `merge_audit` off the source row, reverse each FK, restore source travelers, delete travelers added to target, recompute target dates excluding source span, null `merged_into_id` / `merged_at` / `merge_audit` on source.

- [ ] **Step 3: Implement route + window check**

```python
_UNDO_WINDOW = timedelta(days=7)

@router.post("/{target_id}/undo-merge/{source_id}", response_model=None)
async def undo_merge(...):
    # Auth + source.merged_into_id == target.id check
    # Window: now - source.merged_at <= 7 days
    # Chain check: target.merged_into_id IS NULL (else 409)
    # Apply undo via helper
```

- [ ] **Step 4: Run + commit**

```bash
git add src/trip_tracker/trips/merge.py \
        src/trip_tracker/routes/trips.py \
        tests/test_routes_trips_undo_merge.py
git commit -m "feat(phase9bc): undo-merge with audit-driven traveler restore"
```

---

## Task C3: `POST /trips/<id>/dismiss-merge/<other_id>`

**Files:**
- Modify: `src/trip_tracker/routes/trips.py`
- Test: `tests/test_routes_trips_dismiss.py` (NEW)

- [ ] **Step 1: Failing tests** (idempotent insert via UNIQUE INDEX; consolidation_candidates excludes dismissed pair)

- [ ] **Step 2: Implement** (insert with `ON CONFLICT DO NOTHING` semantics via try/except `IntegrityError`)

- [ ] **Step 3: Run + commit**

```bash
git add src/trip_tracker/routes/trips.py tests/test_routes_trips_dismiss.py
git commit -m "feat(phase9bc): dismiss-merge route"
```

---

## Task C4: Inbox confirm with `?target_trip=<id>`

**Files:**
- Modify: `src/trip_tracker/routes/inbox.py::confirm`
- Test: `tests/test_routes_inbox_confirm_target_trip.py`

- [ ] **Step 1: Failing tests** (4 cases per spec §6.2: valid + owned inherits; 403 non-owner; 400 soft-deleted; omitted = existing behavior)

- [ ] **Step 2: Implement**

In `confirm`, accept `target_trip: uuid.UUID | None = None` query param. If set, validate ownership + not soft-deleted, then assign segments to that trip_id and skip the new-Trip path.

- [ ] **Step 3: Run + commit**

```bash
git add src/trip_tracker/routes/inbox.py tests/test_routes_inbox_confirm_target_trip.py
git commit -m "feat(phase9bc): inbox confirm honors ?target_trip="
```

---

## Task C5: Consolidation banners — trip detail + inbox preview

**Files:**
- Modify: `src/trip_tracker/routes/trips.py::trip_detail` (compute candidates, pass to template)
- Modify: `src/trip_tracker/templates/trips/detail.html` (banner)
- Modify: `src/trip_tracker/routes/inbox.py` (preview render — find the GET that renders the about-to-confirm preview)
- Create: `src/trip_tracker/templates/inbox/_confirm_preview_banner.html`
- Test: extend existing route tests with banner-presence assertions

- [ ] **Step 1: Failing tests** (banner appears when candidates non-empty; banner suppressed when dismissed; banner suppressed when zero candidates)

- [ ] **Step 2: Implement**

```python
# routes/trips.py::trip_detail
target = ConsolidationTarget.from_trip(trip, segments)
candidates = await consolidation_candidates(db, user, target)
return templates.TemplateResponse(
    request, "trips/detail.html",
    {"trip": trip, ..., "consolidation_candidates": candidates},
)
```

- [ ] **Step 3: Run + commit**

```bash
git add src/trip_tracker/routes/trips.py \
        src/trip_tracker/routes/inbox.py \
        src/trip_tracker/templates/trips/detail.html \
        src/trip_tracker/templates/inbox/_confirm_preview_banner.html
git commit -m "feat(phase9bc): consolidation banners on trip detail + confirm preview"
```

---

## Task C6: Merge UI — dropdown + confirm dialog + undo flash

**Files:**
- Modify: `src/trip_tracker/templates/trips/detail.html`
- Create: `src/trip_tracker/templates/trips/_merge_undo_flash.html`

- [ ] **Step 1: Implement dropdown listing user's other active trips**

Sort by date proximity to the current trip. Use plain HTML `<select>` + form, not HTMX. POST submits to `/trips/<source>/merge-into/<target>`.

- [ ] **Step 2: 5-second confirm dialog**

```html
<dialog id="merge-confirm-dialog">
  <p>Move <strong>{{ source_segment_count }} segments</strong>,
     <strong>{{ source_expense_count }} expenses</strong>,
     <strong>{{ source_document_count }} documents</strong>
     from <em>{{ source_title }}</em> into <em>{{ target_title }}</em>?</p>
  <p>The source trip will be hidden for 7 days, then permanently deleted.
     ICS subscribers will get 410 Gone immediately.</p>
  <form method="dialog">
    <button type="button" onclick="this.closest('dialog').close()">Cancel</button>
    <button id="merge-go" disabled>Merge in <span id="merge-countdown">5</span>s…</button>
  </form>
  <script>
    /* Re-enable after 5s */
    let n = 5;
    const id = setInterval(() => {
      n -= 1;
      document.getElementById('merge-countdown').textContent = n;
      if (n <= 0) {
        clearInterval(id);
        const btn = document.getElementById('merge-go');
        btn.disabled = false;
        btn.textContent = 'Merge';
      }
    }, 1000);
  </script>
</dialog>
```

- [ ] **Step 3: Undo flash partial**

When `?merged_from=<id>` is in the query string on `/trips/<target>`, render the flash banner with countdown of days remaining + Undo button POSTing to `/trips/<target>/undo-merge/<source>`.

- [ ] **Step 4: Manual smoke**

Open trip-detail, select another trip from dropdown, click merge → wait 5s → confirm. Verify redirect + flash. Click Undo. Verify restore.

- [ ] **Step 5: Commit**

```bash
git add src/trip_tracker/templates/trips/detail.html \
        src/trip_tracker/templates/trips/_merge_undo_flash.html
git commit -m "feat(phase9bc): merge dropdown + 5s confirm dialog + undo flash"
```

---

## Task C7: Hard-delete sweeper saq cron

**Files:**
- Modify: `src/trip_tracker/worker.py` (add `purge_merged_trips` task + register in cron)
- Test: `tests/test_workers_cleanup.py` (NEW, 3 tests)

- [ ] **Step 1: Failing tests**

```python
@pytest.mark.asyncio
async def test_sweeper_deletes_past_window(db_session, seeded_user):
    # Trip merged 8 days ago
    expired = await _seed_trip(
        db_session, seeded_user,
        merged_into_id=..., merged_at=datetime.now(UTC) - timedelta(days=8),
    )
    await purge_merged_trips({"db_factory": db_session_factory})
    res = (await db_session.execute(select(Trip).where(Trip.id == expired.id))).scalar_one_or_none()
    assert res is None


@pytest.mark.asyncio
async def test_sweeper_preserves_within_window(db_session, seeded_user):
    fresh = await _seed_trip(
        db_session, seeded_user,
        merged_into_id=..., merged_at=datetime.now(UTC) - timedelta(days=3),
    )
    await purge_merged_trips({...})
    res = (await db_session.execute(select(Trip).where(Trip.id == fresh.id))).scalar_one_or_none()
    assert res is not None


@pytest.mark.asyncio
async def test_sweeper_cascades_dismissals(...):
    # Hard-delete cascades trip_merge_dismissals via FK
    pass
```

- [ ] **Step 2: Implement task**

Mirror the session-acquire pattern used by the existing saq tasks
(`parse_raw_email`, `sync_meili`, `refresh_weather`) in `worker.py` — read
those first to find how `ctx` exposes the AsyncSession factory, then copy
that idiom. The shape is roughly:

```python
# src/trip_tracker/worker.py — add (adjust to match the existing
# session-acquire idiom in this file; do NOT invent a new pattern)

async def purge_merged_trips(ctx: dict[str, Any]) -> None:
    """Hard-delete trips that have been soft-merged for >= 7 days.
    Daily cron at 04:00 UTC. Cascades on FKs clean up segments / expenses /
    documents / trip_travelers / trip_merge_dismissals (all should already
    be empty / pointed at the target).
    """
    cutoff = datetime.now(UTC) - timedelta(days=7)
    # Use the same async-session pattern as parse_raw_email above.
    async with _session_for_task(ctx) as db:  # placeholder: match existing
        result = await db.execute(
            delete(Trip).where(
                Trip.merged_into_id.is_not(None),
                Trip.merged_at < cutoff,
            )
        )
        await db.commit()
        log.info("purged_merged_trips", count=result.rowcount)
```

- [ ] **Step 3: Register in saq settings (cron schedule)**

In the same file's `settings` dict at the bottom:

```python
from saq import CronJob

settings = {
    "queue": ...,
    "functions": [parse_raw_email, sync_meili, refresh_weather, purge_merged_trips],
    "cron_jobs": [
        CronJob(purge_merged_trips, cron="0 4 * * *"),  # 04:00 UTC daily
    ],
    ...
}
```

Confirm the saq settings shape matches the existing repo's pattern (search for existing `cron_jobs` if any; if first cron task in repo, the import is new).

- [ ] **Step 4: Run + commit**

```bash
git add src/trip_tracker/worker.py tests/test_workers_cleanup.py
git commit -m "feat(phase9bc): purge_merged_trips daily cron sweeper"
```

---

## Task C8: v0.9.0 manual smoke + tag

- [ ] **Step 1: Run full suite**

Run: `uv run pytest -q`
Expected: ~526 + ~40 = ~566 tests; coverage ≥88%.

Run: `uv run pre-commit run --all-files`
Expected: all hooks green.

- [ ] **Step 2: Manual smoke** (per spec §6.4 v0.9.0)

Forward outbound JFK→CDG → confirm → Trip 1 (Paris). Forward CDG hotel for the same week → preview shows banner → click "Add to Trip 1" → verify single trip with 2 segments. Manually create a second adjacent trip → use dropdown to merge into Trip 1 → wait 5s → confirm → verify redirect with flash → click Undo → verify restore.

- [ ] **Step 3: Open PR + ff-merge + tag**

```bash
git push origin feat/phase-9-dedup-and-merge
gh pr create --title "feat: phase 9bc — trip consolidation + merge (v0.9.0)" --body "$(cat <<'EOF'
## Summary
- Auto-inferred user home (last 20 confirmed segments, 30% dominance floor)
- Consolidation candidate suggestions on trip detail + inbox confirm preview
- Soft-delete merge with merge_audit JSONB for lossless undo
- 7-day undo window; daily 04:00 UTC sweeper hard-deletes after
- ICS feeds for merged source trips return 410 Gone immediately
- Filter clause WHERE merged_into_id IS NULL on every Trip select

Spec at `docs/superpowers/specs/2026-05-02-phase9-inbox-and-trip-consolidation-design.md` §3.2-§3.3.

## Test plan
- [ ] uv run pytest -q (~566 tests)
- [ ] uv run pre-commit run --all-files
- [ ] Manual smoke: consolidation banner + merge + undo
EOF
)"
```

After CI green + ff-merge:

```bash
git checkout main
git pull --ff-only
git tag -s v0.9.0 -m "v0.9.0 — trip consolidation + merge"
git push origin v0.9.0
```

---

## Wrap

### Task W1: Update memory

- [ ] Update `MEMORY.md` index entries (replace placeholder HEADs with v0.8.1 + v0.9.0 commits and tags).
- [ ] Mark `project_duplicate-detection-gap.md` as shipped in v0.8.1.
- [ ] Create `project_phase9-status.md` capturing v0.9.0 outcomes (HEAD, tag, test count, coverage, surprises).
- [ ] If any new feedback emerged during implementation (e.g., subtle dedup edge cases, sweeper races), capture as `feedback_*.md`.

### Task W2: Schedule release verifications

- [ ] Schedule a remote agent for v0.8.1 verification 7 days post-merge (model: claude-sonnet-4-6, environment: Default). Use the same prompt template from v0.8.0 — `cd ... && git pull && verify tag && uv run pytest -q && uv run pre-commit run --all-files && summarize regressions/bugs filed since tag`.
- [ ] Same for v0.9.0 7 days after its merge.

---

## Plan execution notes

**Branching strategy** (per spec §9):
- One branch (`feat/phase-9-dedup-and-merge`), two tags (v0.8.1 then v0.9.0), two PRs.
- Track A's PR ff-merges before Track B+C work begins (or rebase Track B+C on the post-merge main if work was started in parallel).
- Don't squash either PR — preserve the per-task commit history for future bisection.

**Pre-commit hook hygiene** (per `feedback_pre-commit-version-pin.md`):
- If a CI failure appears after pre-commit passed locally, check the hook revs in `.pre-commit-config.yaml` against runtime pins in `pyproject.toml` — version skew is the usual culprit.
- DO NOT skip hooks (`--no-verify`) to push past failures.

**Subagent delegation** (per `feedback_remote-agent-shared-infra.md`):
- If any task is delegated to a CCR remote agent, the prompt MUST forbid edits to: `tests/conftest.py`, `.pre-commit-config.yaml`, `Dockerfile*`, `.github/workflows/*`, `pyproject.toml`.
- Prefer adding new fixtures to `tests/fixtures/<topic>.py` rather than `tests/conftest.py` to keep the conftest clean.
- Patch-pin any new dep added (none expected for Phase 9 — we use only stdlib + already-installed libs).

**Test counts to verify at each release:**
- v0.8.1: ~526 total (507 baseline + ~19 new). Coverage ≥88%.
- v0.9.0: ~566 total (~526 + ~40 new). Coverage ≥88%.
