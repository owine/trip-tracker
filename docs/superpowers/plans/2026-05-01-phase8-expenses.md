# Phase 8 — Expenses + Award Metadata Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-trip expense tracking with frozen-at-entry FX (Frankfurter / ECB rates), 8-value category enum, paid/pending status, hidden cancellation/deposit triple, plus inline award-redemption metadata on flight + lodging segments — covering airline miles AND CC-transferable points.

**Architecture:** A new `expenses/` subpackage holds the `Category` enum, `CURRENCY_MINOR` table, the Frankfurter-backed `fx.py` (httpx + Redis cache, 24h TTL, never-invert direction), and `freeze_fx` / `recompute_home_minor` helpers. A new `Expense` ORM + Alembic migration adds the `expenses` table (multi-FK cascade matching Phase 5 `Document`). A new `users.home_currency` column (default `USD`) is added in a separate migration. New `routes/expenses.py` provides create/edit/delete; trip detail (`routes/trips.py::trip_detail`) gains an inline expense section with rollups and an FxError-swallowing "saved by points" line. Award metadata writes to `Segment.details["award"]` (no schema change) via flight + lodging form extensions. Frankfurter is called synchronously inside the request — no saq task — because we need `fx_rate` available at INSERT time.

**Tech Stack:** Python 3.14 (target=py313), FastAPI, SQLAlchemy 2.0 async, Postgres 18, Redis 7, Alembic, httpx (already in deps), Pydantic v2, Jinja2 / Tailwind CSS, Frankfurter (free, keyless, ECB-backed). **No new dependencies.**

**Spec reference:** [`docs/superpowers/specs/2026-05-01-phase8-expenses-design.md`](../specs/2026-05-01-phase8-expenses-design.md). Section numbers below (e.g. §6.1) refer to this spec.

**Branch:** `feat/phase-8-expenses`. Cut from `main` HEAD when implementation starts (currently `3dbf401` after the spec landed).

**Out of scope (Phase 8.x):** auto-extract receipts from forwarded emails (8.1), CSV import from CC statements (8.2), hotel-loyalty award nights (8.3), per-segment cost rollup (8.4), expense splitting (8.5), multi-currency receipts (8.6), retroactive re-FX admin tool (8.7).

---

## Toolchain quirks worth re-stating per task

- `from __future__ import annotations` at top of every new module.
- ruff `target=py313` + mypy `python_version=3.14`. PEP 585 forms (`list[...]`, `dict[...]`, `str | None`).
- Money math uses `Decimal` (stdlib) throughout; only `int(round(...))` at the final integer cast. **Never** use `float` for currency.
- `numeric(20, 10)` for stored `fx_rate` (10 decimals, exact across reads).
- Timestamp convention: `created_at` / `updated_at` use `server_default=func.now()` and `onupdate=func.now()` matching `User` (NOT the lambda pattern from `Document`).
- ORM-level `index=True` is forbidden in this repo; Alembic owns all `ix_*` index creation. See `Segment` model comment.
- `parsers/enrich.py::Airport` already gives `iata, name, city, country, tz, lat, lon` — re-use directly. No new airports loader.
- `routes/trips.py::trip_detail` is the touch-point for the expense section AND the saved-by-points rollup; both must NOT 500 the page on FX errors.
- pre-commit djlint hook is `djlint-reformat`; template formatting must round-trip clean. Tailwind classes inline.
- Form-error display pattern: routes return `templates.TemplateResponse(request, "expenses/form.html", {..., "errors": {...field: msg}, "values": form_dict})` — same as `routes/trips.py`.
- Existing autouse fixtures in `tests/conftest.py` mock the saq queue (`_mock_meili_queue`, `_mock_documents_queue`); none of Phase 8's code paths enqueue saq jobs, so no new queue mock is needed. But Phase 8 introduces the **fake Redis** pattern for FX cache tests — see Task 4.
- Frankfurter base URL is `https://api.frankfurter.dev/v1/latest`. **Always** call without `symbols` param so the cache holds the full ~30-currency table per base.
- `Segment.details` is `Mapped[dict[str, Any]]` with `server_default="{}"` (already non-nullable). Award metadata is a sub-key: `details["award"] = {...}`. Writes must use `flag_modified(seg, "details")` from `sqlalchemy.orm.attributes` so SQLAlchemy detects the in-place mutation.

---

## File Structure

```
src/trip_tracker/
├── expenses/                                  [CREATE — new subpackage]
│   ├── __init__.py                            (marker only)
│   ├── categories.py                          Category enum (8 values)
│   ├── currencies.py                          CURRENCY_MINOR table + minor_digits
│   ├── fx.py                                  Frankfurter client + Redis cache + get_rate
│   ├── freeze.py                              freeze_fx + recompute_home_minor helpers
│   └── awards.py                              AwardDetails Pydantic model + k_format/program_short
├── models/
│   ├── expense.py                             [CREATE — Expense ORM]
│   └── user.py                                [MODIFY: add home_currency column]
├── routes/
│   ├── expenses.py                            [CREATE — POST/GET/POST/POST CRUD + autocomplete]
│   ├── trips.py                               [MODIFY: extend trip_detail handler with expenses + rollups]
│   ├── segments.py                            [MODIFY: parse award fields + clear-award short-circuit]
│   └── settings.py                            [MODIFY: home_currency dropdown + POST handler]
├── templates/
│   ├── expenses/
│   │   ├── form.html                          [CREATE — create/edit shared form]
│   │   └── _row.html                          [CREATE — single expense row partial]
│   ├── trips/
│   │   └── detail.html                        [MODIFY: insert expense section + saved-by-points line]
│   ├── segments/
│   │   ├── _award_section.html                [CREATE — collapsed award fields shared partial]
│   │   ├── _award_badge.html                  [CREATE — badge for segment row]
│   │   ├── _row.html                          [MODIFY: include _award_badge.html when details.award set]
│   │   ├── flight_form.html                   [MODIFY: include _award_section.html]
│   │   └── lodging_form.html                  [MODIFY: include _award_section.html]
│   └── settings/
│       └── page.html                          [MODIFY: add home_currency dropdown form]
├── app.py                                     [MODIFY: include expenses_router]
└── schemas/
    └── expense_forms.py                       [CREATE — ExpenseForm Pydantic v2 model]

migrations/versions/
├── 2026_05_<NN1>_<rev1>_phase8_expenses.py    [CREATE — expenses table]
└── 2026_05_<NN2>_<rev2>_phase8_home_currency.py  [CREATE — users.home_currency column]

tests/
├── test_expenses_categories.py                [CREATE]
├── test_expenses_currencies.py                [CREATE]
├── test_expenses_fx.py                        [CREATE]
├── test_expenses_freeze.py                    [CREATE]
├── test_expenses_awards.py                    [CREATE — AwardDetails + k_format + program_short]
├── test_models_expense.py                     [CREATE — cascade tests]
├── test_routes_expenses_crud.py               [CREATE]
├── test_routes_expenses_autocomplete.py       [CREATE]
├── test_routes_trips_expense_section.py       [CREATE — rollup + FxError swallow]
├── test_routes_segments_award.py              [CREATE — award POST + clear-award path]
├── test_routes_settings_home_currency.py      [CREATE]
└── test_award_badge_render.py                 [CREATE — k_format / program_short / template render]
```

---

## Task 1 — `Expense` ORM + Alembic migration + cascade tests

**Spec ref:** §4.1, §4.4 (cascade semantics).
**Model:** sonnet (multi-FK cascade subtleties).

**Files:**
- Create: `src/trip_tracker/models/expense.py`
- Create: `migrations/versions/2026_05_<NN>_<rev>_phase8_expenses.py`
- Create: `tests/test_models_expense.py`

### Step 1.1 — Write the failing cascade test

`tests/test_models_expense.py`:

```python
"""Expense ORM: multi-FK cascade and column constraints."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.expense import Expense
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


@pytest.mark.asyncio
async def test_expense_cascades_with_trip(db_session: AsyncSession) -> None:
    """Trip delete → expense rows gone (CASCADE)."""
    user = User(oidc_subject="e1", email="e1@x.com", display_name="E1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))

    exp = Expense(
        trip_id=trip.id, owner_user_id=user.id,
        amount_minor=3800, currency="EUR",
        fx_rate=Decimal("1.0700000000"), amount_home_minor=4066,
        home_currency="USD", category="food", incurred_on=date(2026, 6, 4),
        status="paid",
    )
    db_session.add(exp)
    await db_session.commit()

    await db_session.delete(trip)
    await db_session.commit()

    rows = (await db_session.execute(select(Expense).where(Expense.id == exp.id))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_expense_segment_set_null_on_segment_delete(db_session: AsyncSession) -> None:
    """Segment delete → expense.segment_id becomes NULL but row survives."""
    from datetime import datetime as _dt
    user = User(oidc_subject="e2", email="e2@x.com", display_name="E2")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    from trip_tracker.models.segment import Segment
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="lodging", status="confirmed",
        provider="Hotel X", start_at=_dt(2026, 6, 2, 15, 0, tzinfo=UTC), start_tz="UTC",
        end_at=_dt(2026, 6, 3, 11, 0, tzinfo=UTC), end_tz="UTC",
        details={}, parse_source="manual", parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.flush()
    exp = Expense(
        trip_id=trip.id, owner_user_id=user.id, segment_id=seg.id,
        amount_minor=20000, currency="USD",
        fx_rate=Decimal("1.0000000000"), amount_home_minor=20000,
        home_currency="USD", category="lodging", incurred_on=date(2026, 6, 2),
        status="paid",
    )
    db_session.add(exp)
    await db_session.commit()

    await db_session.delete(seg)
    await db_session.commit()
    await db_session.refresh(exp)
    assert exp.segment_id is None
    assert (await db_session.execute(select(Expense).where(Expense.id == exp.id))).scalar_one()


@pytest.mark.asyncio
async def test_expense_document_set_null_on_document_delete(db_session: AsyncSession) -> None:
    """Document delete → expense.document_id becomes NULL but row survives."""
    from trip_tracker.models.document import Document
    user = User(oidc_subject="e3", email="e3@x.com", display_name="E3")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    doc = Document(
        owner_user_id=user.id, filename="receipt.pdf", mime_type="application/pdf",
        size_bytes=1024, sha256="a" * 64, storage_key="docs/x.pdf",
    )
    db_session.add(doc)
    await db_session.flush()
    exp = Expense(
        trip_id=trip.id, owner_user_id=user.id, document_id=doc.id,
        amount_minor=3800, currency="EUR",
        fx_rate=Decimal("1.0700000000"), amount_home_minor=4066,
        home_currency="USD", category="food", incurred_on=date(2026, 6, 4), status="paid",
    )
    db_session.add(exp)
    await db_session.commit()

    await db_session.delete(doc)
    await db_session.commit()
    await db_session.refresh(exp)
    assert exp.document_id is None


@pytest.mark.asyncio
async def test_expense_owner_cascade_on_user_delete(db_session: AsyncSession) -> None:
    """User delete → expense rows gone (CASCADE)."""
    user = User(oidc_subject="e4", email="e4@x.com", display_name="E4")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    exp = Expense(
        trip_id=trip.id, owner_user_id=user.id,
        amount_minor=3800, currency="EUR",
        fx_rate=Decimal("1.0700000000"), amount_home_minor=4066,
        home_currency="USD", category="food", incurred_on=date(2026, 6, 4), status="paid",
    )
    db_session.add(exp)
    await db_session.commit()
    exp_id = exp.id

    await db_session.delete(user)
    await db_session.commit()
    rows = (await db_session.execute(select(Expense).where(Expense.id == exp_id))).scalars().all()
    assert rows == []
```

- [ ] **Step 1.1:** Write all four cascade tests (bodies above are complete; copy as-is).
- [ ] **Step 1.2:** Run `uv run pytest tests/test_models_expense.py -v` → expected: ImportError (Expense doesn't exist yet).
- [ ] **Step 1.3:** Implement `src/trip_tracker/models/expense.py`:

```python
"""Expense ORM. Spec §4.1."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, Date, DateTime, ForeignKey, Numeric, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trip_tracker.models.base import Base

if TYPE_CHECKING:
    from trip_tracker.models.document import Document
    from trip_tracker.models.segment import Segment
    from trip_tracker.models.trip import Trip
    from trip_tracker.models.user import User


class Expense(Base):
    """One trip expense with frozen-at-entry FX. Spec §4.1."""

    __tablename__ = "expenses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    trip_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("trips.id", ondelete="CASCADE"),
        nullable=False,
    )
    segment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("segments.id", ondelete="SET NULL"),
        nullable=True,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    fx_rate: Mapped[Decimal] = mapped_column(Numeric(20, 10), nullable=False)
    amount_home_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    home_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(String, nullable=True)
    incurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="paid")
    deposit_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cancellation_deadline: Mapped[date | None] = mapped_column(Date, nullable=True)
    cancellation_fee_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    # Phase 5 Document pattern: omit `back_populates` entirely when there's no inverse.
    trip: Mapped[Trip] = relationship(lazy="raise")
    segment: Mapped[Segment | None] = relationship(lazy="raise")
    document: Mapped[Document | None] = relationship(lazy="raise")
    owner: Mapped[User] = relationship(lazy="raise")
```

- [ ] **Step 1.4:** Run `uv run alembic heads` to confirm the current alembic head (should be the Phase 6 ICS-token migration, `2dead0c2dfd4`). Then `uv run alembic revision -m "phase8_expenses"` to scaffold. Edit the generated file: set `down_revision = "2dead0c2dfd4"` (or whatever `alembic heads` reported). The body:

```python
def upgrade() -> None:
    op.create_table(
        "expenses",
        sa.Column("id", postgresql.UUID(as_uuid=True),
                  server_default=sa.text("gen_random_uuid()"), primary_key=True),
        sa.Column("trip_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("segment_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("fx_rate", sa.Numeric(20, 10), nullable=False),
        sa.Column("amount_home_minor", sa.BigInteger(), nullable=False),
        sa.Column("home_currency", sa.String(length=3), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("incurred_on", sa.Date(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="paid"),
        sa.Column("deposit_minor", sa.BigInteger(), nullable=True),
        sa.Column("cancellation_deadline", sa.Date(), nullable=True),
        sa.Column("cancellation_fee_minor", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["trip_id"], ["trips.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["segment_id"], ["segments.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_expenses_trip", "expenses", ["trip_id"])
    op.create_index("ix_expenses_segment", "expenses", ["segment_id"])
    op.create_index("ix_expenses_owner", "expenses", ["owner_user_id"])
    op.create_index("ix_expenses_incurred", "expenses", ["incurred_on"])


def downgrade() -> None:
    op.drop_index("ix_expenses_incurred", table_name="expenses")
    op.drop_index("ix_expenses_owner", table_name="expenses")
    op.drop_index("ix_expenses_segment", table_name="expenses")
    op.drop_index("ix_expenses_trip", table_name="expenses")
    op.drop_table("expenses")
```

- [ ] **Step 1.5:** Run `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head` — round-trip clean.
- [ ] **Step 1.6:** Run `uv run pytest tests/test_models_expense.py -v` → all four tests PASS.
- [ ] **Step 1.7:** Commit:

```bash
git add src/trip_tracker/models/expense.py migrations/versions/*phase8_expenses* tests/test_models_expense.py
git commit -m "feat(expenses): Expense ORM + migration + cascade tests"
```

---

## Task 2 — `users.home_currency` column + migration

**Spec ref:** §4.2.
**Model:** haiku.

**Files:**
- Modify: `src/trip_tracker/models/user.py`
- Create: `migrations/versions/2026_05_<NN>_<rev>_phase8_home_currency.py`
- Modify: `tests/test_models_user.py` (or create `test_user_home_currency.py` if a focused file is preferred)

- [ ] **Step 2.1:** Write a failing test asserting a fresh `User` has `home_currency == "USD"`:

```python
@pytest.mark.asyncio
async def test_user_default_home_currency(db_session: AsyncSession) -> None:
    u = User(oidc_subject="hc1", email="hc1@x.com", display_name="HC1")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    assert u.home_currency == "USD"
```

- [ ] **Step 2.2:** Run → FAIL (`AttributeError: home_currency`).
- [ ] **Step 2.3:** Add to `User` model:

```python
home_currency: Mapped[str] = mapped_column(
    String(3), nullable=False, server_default="USD"
)
```

- [ ] **Step 2.4:** `uv run alembic revision -m "phase8_home_currency"`. Set `down_revision` to the revision id generated by Task 1 (run `uv run alembic heads` after Task 1's migration is in place — it should be Task 1's id). Body:

```python
def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("home_currency", sa.String(length=3),
                  nullable=False, server_default="USD"),
    )

def downgrade() -> None:
    op.drop_column("users", "home_currency")
```

- [ ] **Step 2.5:** `uv run alembic upgrade head && uv run alembic downgrade -1 && uv run alembic upgrade head`.
- [ ] **Step 2.6:** Run test → PASS.
- [ ] **Step 2.7:** Commit:

```bash
git commit -am "feat(expenses): add users.home_currency (default USD)"
```

---

## Task 3 — `Category` enum + `CURRENCY_MINOR` table

**Spec ref:** §4.3, §4.5.
**Model:** haiku (pure data).

**Files:**
- Create: `src/trip_tracker/expenses/__init__.py` (marker)
- Create: `src/trip_tracker/expenses/categories.py`
- Create: `src/trip_tracker/expenses/currencies.py`
- Create: `tests/test_expenses_categories.py`
- Create: `tests/test_expenses_currencies.py`

### Step 3.1 — Categories test

```python
# tests/test_expenses_categories.py
from trip_tracker.expenses.categories import Category, CATEGORY_LABELS

def test_category_values_are_lowercase_snake() -> None:
    expected = {"food", "transit", "lodging", "activities", "shopping",
                "gratuities", "connectivity", "other"}
    assert {c.value for c in Category} == expected

def test_category_labels_cover_all_values() -> None:
    assert set(CATEGORY_LABELS) == set(Category)

def test_category_label_examples() -> None:
    assert CATEGORY_LABELS[Category.FOOD] == "Food"
    assert CATEGORY_LABELS[Category.CONNECTIVITY] == "Connectivity"
```

### Step 3.2 — Currencies test

```python
# tests/test_expenses_currencies.py
from trip_tracker.expenses.currencies import CURRENCY_MINOR, minor_digits

def test_zero_decimal_currencies() -> None:
    for code in ("JPY", "KRW", "VND", "CLP", "ISK"):
        assert minor_digits(code) == 0

def test_three_decimal_currencies() -> None:
    for code in ("BHD", "JOD", "KWD", "OMR", "TND"):
        assert minor_digits(code) == 3

def test_default_two_decimals() -> None:
    assert minor_digits("USD") == 2
    assert minor_digits("EUR") == 2
    assert minor_digits("XYZ") == 2  # unknown defaults to 2
```

- [ ] **Step 3.1:** Write both tests. Run → ImportError.
- [ ] **Step 3.2:** Implement `categories.py`:

```python
"""Expense category enum + display labels. Spec §4.5."""

from __future__ import annotations

from enum import Enum


class Category(str, Enum):
    FOOD = "food"
    TRANSIT = "transit"
    LODGING = "lodging"
    ACTIVITIES = "activities"
    SHOPPING = "shopping"
    GRATUITIES = "gratuities"
    CONNECTIVITY = "connectivity"
    OTHER = "other"


CATEGORY_LABELS: dict[Category, str] = {
    Category.FOOD: "Food",
    Category.TRANSIT: "Transit",
    Category.LODGING: "Lodging",
    Category.ACTIVITIES: "Activities",
    Category.SHOPPING: "Shopping",
    Category.GRATUITIES: "Gratuities",
    Category.CONNECTIVITY: "Connectivity",
    Category.OTHER: "Other",
}
```

- [ ] **Step 3.3:** Implement `currencies.py`:

```python
"""ISO 4217 minor-unit digit lookup. Spec §4.3."""

from __future__ import annotations

CURRENCY_MINOR: dict[str, int] = {
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0,
    "BHD": 3, "JOD": 3, "KWD": 3, "OMR": 3, "TND": 3,
}


def minor_digits(code: str) -> int:
    """Return the number of fractional digits for an ISO 4217 currency.
    Defaults to 2 for any code not in the lookup table."""
    return CURRENCY_MINOR.get(code, 2)
```

- [ ] **Step 3.4:** Run tests → PASS.
- [ ] **Step 3.5:** Commit:

```bash
git add src/trip_tracker/expenses/__init__.py src/trip_tracker/expenses/categories.py src/trip_tracker/expenses/currencies.py tests/test_expenses_categories.py tests/test_expenses_currencies.py
git commit -m "feat(expenses): Category enum + CURRENCY_MINOR table"
```

---

## Task 4 — Frankfurter client + Redis cache + `get_rate`

**Spec ref:** §5.1, §5.2, §5.3.
**Model:** sonnet (cache shape, never-invert direction, Decimal precision are subtle).

**Files:**
- Create: `src/trip_tracker/expenses/fx.py`
- Create: `tests/test_expenses_fx.py`

Key contract:
- `fetch_rates(base) -> dict[str, Decimal]` — calls Frankfurter WITHOUT `symbols`, returns full table.
- Cache key `fx:<base>:<YYYY-MM-DD>`, 24h TTL, value stored as JSON map of strings (so Decimal precision survives Redis round-trip).
- `get_rate(base, target, redis)` — looks up under `base`, never inverts. `base == target` returns `Decimal(1)` with no I/O. Missing target raises `FxError`.

### Step 4.1 — Tests

```python
# tests/test_expenses_fx.py
from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest
from redis.asyncio import Redis

from trip_tracker.expenses.fx import (
    FxError,
    fetch_rates,
    get_cached_rates,
    get_rate,
    set_cached_rates,
)


class FakeRedis:
    """Minimal in-memory Redis stand-in covering get/set with ex=...

    Phase 7 used real fakeredis; Phase 8's surface area is small enough that
    a hand-rolled fake is simpler and avoids the dep.
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> None:
        self.store[key] = value.encode() if isinstance(value, str) else value


@pytest.mark.asyncio
async def test_get_rate_same_currency_returns_one() -> None:
    fake = FakeRedis()
    rate = await get_rate("USD", "USD", fake)  # type: ignore[arg-type]
    assert rate == Decimal(1)
    assert fake.store == {}  # no I/O


@pytest.mark.asyncio
async def test_set_get_cached_rates_roundtrip() -> None:
    fake = FakeRedis()
    rates = {"EUR": Decimal("0.93"), "JPY": Decimal("156.4")}
    await set_cached_rates("USD", rates, fake)  # type: ignore[arg-type]
    got = await get_cached_rates("USD", fake)  # type: ignore[arg-type]
    assert got == rates
    # Confirm decimals not floats
    assert isinstance(got["EUR"], Decimal)


@pytest.mark.asyncio
async def test_get_rate_cache_miss_calls_fetch() -> None:
    fake = FakeRedis()
    payload = {"EUR": Decimal("0.93"), "GBP": Decimal("0.79")}
    with patch("trip_tracker.expenses.fx.fetch_rates", AsyncMock(return_value=payload)) as m:
        rate = await get_rate("USD", "EUR", fake)  # type: ignore[arg-type]
    m.assert_awaited_once_with("USD")
    assert rate == Decimal("0.93")
    # Cache populated for next call
    assert "fx:USD:" in next(iter(fake.store))


@pytest.mark.asyncio
async def test_get_rate_cache_hit_no_fetch() -> None:
    fake = FakeRedis()
    await set_cached_rates("USD", {"EUR": Decimal("0.93")}, fake)  # type: ignore[arg-type]
    with patch("trip_tracker.expenses.fx.fetch_rates", AsyncMock()) as m:
        rate = await get_rate("USD", "EUR", fake)  # type: ignore[arg-type]
    m.assert_not_awaited()
    assert rate == Decimal("0.93")


@pytest.mark.asyncio
async def test_get_rate_missing_target_raises() -> None:
    fake = FakeRedis()
    with patch("trip_tracker.expenses.fx.fetch_rates",
               AsyncMock(return_value={"EUR": Decimal("0.93")})):
        with pytest.raises(FxError):
            await get_rate("USD", "ZZZ", fake)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_rates_parses_decimal_from_string() -> None:
    """Rates parsed via parse_float=Decimal so we never round-trip via float."""
    fake_response = AsyncMock()
    fake_response.status_code = 200
    fake_response.text = json.dumps({"base": "USD", "date": "2026-05-01",
                                     "rates": {"EUR": 0.9300000123, "JPY": 156.4}})
    fake_response.raise_for_status = lambda: None
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get = AsyncMock(return_value=fake_response)
    with patch("trip_tracker.expenses.fx.httpx.AsyncClient", return_value=fake_client):
        rates = await fetch_rates("USD")
    assert isinstance(rates["EUR"], Decimal)
    # Critical: precision preserved (no 0.93 == float(0.93) drift)
    assert str(rates["EUR"]) == "0.9300000123"


@pytest.mark.asyncio
async def test_fetch_rates_5xx_raises_fxerror() -> None:
    import httpx
    fake_response = AsyncMock()
    fake_response.raise_for_status = lambda: (_ for _ in ()).throw(
        httpx.HTTPStatusError("503", request=AsyncMock(), response=AsyncMock())
    )
    fake_client = AsyncMock()
    fake_client.__aenter__.return_value = fake_client
    fake_client.__aexit__.return_value = None
    fake_client.get = AsyncMock(return_value=fake_response)
    with patch("trip_tracker.expenses.fx.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(FxError):
            await fetch_rates("USD")
```

- [ ] **Step 4.1:** Write all tests. Run → ImportError.
- [ ] **Step 4.2:** Implement `src/trip_tracker/expenses/fx.py`:

```python
"""Frankfurter FX client + Redis cache. Spec §5.

Convention: always fetch under `base` without `symbols`, cache the full
~30-currency table per (base, date). Never invert — `get_rate(EUR, USD)`
fetches with base=EUR. Decimal precision is preserved end-to-end by
parsing the Frankfurter JSON with `parse_float=Decimal` and storing
cached values as strings in Redis.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from decimal import Decimal
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

_FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
_TTL_SEC = 86400  # 24h
_TIMEOUT = 10.0


class FxError(RuntimeError):
    """Raised when an FX rate is unavailable (HTTP failure with no cache,
    or a target currency not present in the response)."""


class _RedisLike(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> object: ...


def _today() -> str:
    return _dt.date.today().isoformat()


def _key(base: str) -> str:
    return f"fx:{base}:{_today()}"


async def fetch_rates(base: str) -> dict[str, Decimal]:
    """Single Frankfurter HTTP call. Returns ALL rates under `base` as Decimal.
    Raises FxError on 5xx / network failure / parse failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_FRANKFURTER_URL, params={"base": base})
            resp.raise_for_status()
            payload = json.loads(resp.text, parse_float=Decimal)
    except (httpx.HTTPError, ValueError) as exc:
        raise FxError(f"Frankfurter fetch failed: {exc}") from exc
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        raise FxError("Frankfurter response missing 'rates' object")
    # parse_float=Decimal already returns Decimals for floats; ints stay int — coerce.
    return {k: Decimal(str(v)) for k, v in rates.items()}


async def get_cached_rates(base: str, redis: _RedisLike) -> dict[str, Decimal] | None:
    raw = await redis.get(_key(base))
    if raw is None:
        return None
    decoded = raw.decode() if isinstance(raw, bytes) else raw
    parsed = json.loads(decoded)
    return {k: Decimal(v) for k, v in parsed.items()}


async def set_cached_rates(base: str, rates: dict[str, Decimal], redis: _RedisLike) -> None:
    serializable = {k: str(v) for k, v in rates.items()}
    await redis.set(_key(base), json.dumps(serializable), ex=_TTL_SEC)


async def get_rate(base: str, target: str, redis: _RedisLike) -> Decimal:
    """Return `1 base = X target` as Decimal. Cache hit → 0ms; cache miss →
    Frankfurter call + cache. Raises FxError if Frankfurter is unreachable
    AND no cache is available, or if `target` isn't supported."""
    if base == target:
        return Decimal(1)
    cached = await get_cached_rates(base, redis)
    if cached is None:
        cached = await fetch_rates(base)
        await set_cached_rates(base, cached, redis)
    if target not in cached:
        raise FxError(f"target currency {target!r} not available under base {base!r}")
    return cached[target]
```

- [ ] **Step 4.3:** Run `uv run pytest tests/test_expenses_fx.py -v` → all PASS.
- [ ] **Step 4.4:** Commit:

```bash
git add src/trip_tracker/expenses/fx.py tests/test_expenses_fx.py
git commit -m "feat(expenses): Frankfurter FX client + Redis cache + get_rate"
```

---

## Task 5 — `freeze_fx` + `recompute_home_minor` helpers

**Spec ref:** §4.4 (entry-time formula and edit-path recompute rule).
**Model:** haiku (pure function, table-driven tests).

**Files:**
- Create: `src/trip_tracker/expenses/freeze.py`
- Create: `tests/test_expenses_freeze.py`

### Step 5.1 — Tests (table-driven, covers JPY/USD/BHD/same-currency)

```python
# tests/test_expenses_freeze.py
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from trip_tracker.expenses.freeze import freeze_fx, recompute_home_minor


@pytest.mark.parametrize(
    "amount_minor,native,home,rate,expected_home_minor",
    [
        # USD→USD: same currency, rate=1, no FX call.
        (3800, "USD", "USD", Decimal(1), 3800),
        # EUR (2 decimals) → USD (2): 38.00 * 1.07 = 40.66 → 4066
        (3800, "EUR", "USD", Decimal("1.07"), 4066),
        # JPY (0 decimals) → USD (2): 5000 yen * 0.0064 = $32.00
        # 5000_minor (=5000 yen) * 0.0064 * 10**(2-0) = 5000 * 0.0064 * 100 = 3200
        (5000, "JPY", "USD", Decimal("0.0064"), 3200),
        # USD (2) → JPY (0): $38.00 * 156.4 = 5943.20 yen → 5943 (rounded)
        # 3800 * 156.4 * 10**(0-2) = 3800 * 156.4 / 100 = 5943.2 → 5943
        (3800, "USD", "JPY", Decimal("156.4"), 5943),
        # BHD (3) → USD (2): 1.234 BHD * 2.65 = 3.27 USD
        # 1234 * 2.65 * 10**(2-3) = 1234 * 2.65 / 10 = 326.99 → 327
        (1234, "BHD", "USD", Decimal("2.65"), 327),
        # ROUND_HALF_UP: 0.5 rounds up
        # amount=1, native=USD, home=USD, rate=Decimal("0.5") -> 1 * 0.5 = 0.5 -> 1
        (1, "USD", "USD", Decimal(1), 1),  # same-currency wins; rate is overridden
    ],
)
@pytest.mark.asyncio
async def test_freeze_fx_table(
    amount_minor: int, native: str, home: str, rate: "Decimal", expected_home_minor: int
) -> None:
    fake_redis = AsyncMock()
    with patch("trip_tracker.expenses.freeze.get_rate", AsyncMock(return_value=rate)):
        fx_rate, home_minor = await freeze_fx(amount_minor, native, home, fake_redis)
    if native == home:
        assert fx_rate == Decimal(1)
    else:
        assert fx_rate == rate
    assert home_minor == expected_home_minor


def test_recompute_home_minor_pure() -> None:
    """recompute_home_minor is sync and takes a known fx_rate (edit-path)."""
    # 38.00 EUR @ 1.07 → 40.66 USD
    assert recompute_home_minor(3800, "EUR", "USD", Decimal("1.07")) == 4066
    # 5000 JPY @ 0.0064 → 32.00 USD
    assert recompute_home_minor(5000, "JPY", "USD", Decimal("0.0064")) == 3200


def test_recompute_home_minor_round_half_up() -> None:
    # 100 minor * 0.005 = 0.5 → 1 with HALF_UP
    assert recompute_home_minor(100, "USD", "USD", Decimal("0.005")) == 1
```

- [ ] **Step 5.1:** Write tests. Run → ImportError.
- [ ] **Step 5.2:** Implement `src/trip_tracker/expenses/freeze.py`:

```python
"""freeze_fx + recompute_home_minor: pure money math. Spec §4.4."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from trip_tracker.expenses.currencies import minor_digits
from trip_tracker.expenses.fx import get_rate


class _RedisLike(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> object: ...


def recompute_home_minor(
    amount_minor: int, native: str, home: str, fx_rate: Decimal
) -> int:
    """Recompute the home-currency minor-unit equivalent for a known fx_rate.

    Used on the edit path when amount_minor changes but currency does not — we
    keep the original frozen fx_rate and only recompute the home equivalent.
    """
    home_d = minor_digits(home)
    native_d = minor_digits(native)
    factor = Decimal(10) ** (home_d - native_d)
    raw = Decimal(amount_minor) * fx_rate * factor
    return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def freeze_fx(
    amount_minor: int, native: str, home: str, redis: _RedisLike
) -> tuple[Decimal, int]:
    """Return (fx_rate, amount_home_minor) frozen at entry time. Spec §4.4.

    base == target → returns (Decimal(1), amount_minor) — no FX call.
    """
    fx_rate = await get_rate(native, home, redis)
    home_minor = recompute_home_minor(amount_minor, native, home, fx_rate)
    return fx_rate, home_minor
```

- [ ] **Step 5.3:** Run tests → PASS.
- [ ] **Step 5.4:** Commit:

```bash
git add src/trip_tracker/expenses/freeze.py tests/test_expenses_freeze.py
git commit -m "feat(expenses): freeze_fx + recompute_home_minor helpers"
```

---

## Task 6 — Expense CRUD routes (create / edit / delete)

**Spec ref:** §6.1 (with edit-path recompute rule from §4.4).
**Model:** sonnet (multi-file; edit recompute is the gotcha).

**Files:**
- Create: `src/trip_tracker/schemas/expense_forms.py`
- Create: `src/trip_tracker/routes/expenses.py`
- Modify: `src/trip_tracker/app.py` (include router)
- Create: `tests/test_routes_expenses_crud.py`

Key behaviors to test:
1. `POST /trips/{trip_id}/expenses` calls `freeze_fx` and persists with frozen `fx_rate` + `amount_home_minor`.
2. Auth: only owner OR trip-traveler can create/edit/delete; everyone else → 404.
3. **Edit recompute rule** (the trap):
   - currency unchanged + amount unchanged → no recompute.
   - currency unchanged + amount changed → keep `fx_rate`, recompute `amount_home_minor` via `recompute_home_minor`.
   - currency changed → re-fetch `fx_rate` AND recompute `amount_home_minor` (via `freeze_fx`).
4. Concurrent home-currency-change guard: form has hidden `home_currency_at_load` field; POST mismatch re-renders with flash.
5. Frankfurter unavailable → 503 + form re-render with flash; expense NOT persisted.
6. Cancellation/deposit triple is optional; nullable saved correctly.

### Step 6.1 — `expense_forms.py`

```python
"""ExpenseForm Pydantic v2 model — POST validation."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator

from trip_tracker.expenses.categories import Category


class ExpenseForm(BaseModel):
    amount_minor: int = Field(ge=0)
    currency: str = Field(min_length=3, max_length=3)
    category: Category
    notes: str | None = None
    incurred_on: date
    status: str = Field(default="paid", pattern="^(paid|pending)$")
    segment_id: str | None = None
    document_id: str | None = None
    deposit_minor: int | None = Field(default=None, ge=0)
    cancellation_deadline: date | None = None
    cancellation_fee_minor: int | None = Field(default=None, ge=0)
    home_currency_at_load: str = Field(min_length=3, max_length=3)

    @field_validator("currency", "home_currency_at_load")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.upper()
```

### Step 6.2 — Routes skeleton

```python
"""Expense CRUD routes. Spec §6.1."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import get_settings, require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.expenses.categories import CATEGORY_LABELS, Category
from trip_tracker.expenses.freeze import freeze_fx, recompute_home_minor
from trip_tracker.expenses.fx import FxError
from trip_tracker.models.expense import Expense
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.schemas.expense_forms import ExpenseForm

router = APIRouter(tags=["expenses"])
logger = logging.getLogger(__name__)


@router.get("/trips/{trip_id}/expenses/new", response_class=HTMLResponse)
async def new_expense_form(
    request: Request, trip_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    return _render_form(request, user, trip_id, values={}, errors={})

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


async def _redis(settings: Settings = Depends(get_settings)) -> AsyncRedis:  # noqa: B008
    return AsyncRedis.from_url(str(settings.redis_url))


async def _user_can_access_trip(db: AsyncSession, user: User, trip_id: uuid.UUID) -> bool:
    res = await db.execute(
        select(TripTraveler.user_id).where(
            TripTraveler.trip_id == trip_id, TripTraveler.user_id == user.id
        )
    )
    return res.scalar_one_or_none() is not None


@router.post("/trips/{trip_id}/expenses")
async def create_expense(
    request: Request,
    trip_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    redis: AsyncRedis = Depends(_redis),  # noqa: B008
) -> Response:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    form_data = dict(await request.form())
    try:
        form = ExpenseForm(**form_data)
    except ValidationError as exc:
        return _render_form(request, user, trip_id, form_data, errors=_pydantic_errors(exc))

    if form.home_currency_at_load != user.home_currency:
        return _render_form(
            request, user, trip_id, form_data,
            errors={"_form": "Your home currency changed in another tab — review and resubmit."},
        )

    try:
        fx_rate, home_minor = await freeze_fx(
            form.amount_minor, form.currency, user.home_currency, redis
        )
    except FxError as exc:
        logger.warning("FX unavailable on create: %s", exc)
        return _render_form(
            request, user, trip_id, form_data,
            errors={"_form": "Currency rates unavailable. Try again in a few minutes."},
        )

    exp = Expense(
        trip_id=trip_id,
        owner_user_id=user.id,
        amount_minor=form.amount_minor,
        currency=form.currency,
        fx_rate=fx_rate,
        amount_home_minor=home_minor,
        home_currency=user.home_currency,
        category=form.category.value,
        notes=form.notes,
        incurred_on=form.incurred_on,
        status=form.status,
        segment_id=uuid.UUID(form.segment_id) if form.segment_id else None,
        document_id=uuid.UUID(form.document_id) if form.document_id else None,
        deposit_minor=form.deposit_minor,
        cancellation_deadline=form.cancellation_deadline,
        cancellation_fee_minor=form.cancellation_fee_minor,
    )
    db.add(exp)
    await db.commit()
    if "session" in request.scope:
        request.session["flash"] = {"kind": "expense_saved",
                                    "amount_minor": form.amount_minor,
                                    "currency": form.currency,
                                    "home_minor": home_minor,
                                    "home_currency": user.home_currency}
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.get("/expenses/{expense_id}/edit", response_class=HTMLResponse)
async def edit_expense_form(
    request: Request,
    expense_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    exp = await db.get(Expense, expense_id)
    if exp is None or not (
        exp.owner_user_id == user.id
        or await _user_can_access_trip(db, user, exp.trip_id)
    ):
        raise HTTPException(404)
    return _render_form(
        request, user, exp.trip_id,
        values=_expense_to_form_values(exp),
        errors={},
        edit_id=expense_id,
    )


@router.post("/expenses/{expense_id}")
async def update_expense(
    request: Request,
    expense_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    redis: AsyncRedis = Depends(_redis),  # noqa: B008
) -> Response:
    exp = await db.get(Expense, expense_id)
    if exp is None or not (
        exp.owner_user_id == user.id
        or await _user_can_access_trip(db, user, exp.trip_id)
    ):
        raise HTTPException(404)

    form_data = dict(await request.form())
    try:
        form = ExpenseForm(**form_data)
    except ValidationError as exc:
        return _render_form(request, user, exp.trip_id, form_data,
                             errors=_pydantic_errors(exc), edit_id=expense_id)

    if form.home_currency_at_load != user.home_currency:
        return _render_form(
            request, user, exp.trip_id, form_data,
            errors={"_form": "Your home currency changed in another tab — review and resubmit."},
            edit_id=expense_id,
        )

    # === Edit-path recompute rule (Spec §4.4) ===
    currency_changed = form.currency != exp.currency
    amount_changed = form.amount_minor != exp.amount_minor

    if currency_changed:
        try:
            fx_rate, home_minor = await freeze_fx(
                form.amount_minor, form.currency, user.home_currency, redis
            )
        except FxError:
            return _render_form(
                request, user, exp.trip_id, form_data,
                errors={"_form": "Currency rates unavailable. Try again in a few minutes."},
                edit_id=expense_id,
            )
        exp.fx_rate = fx_rate
        exp.amount_home_minor = home_minor
        exp.home_currency = user.home_currency
    elif amount_changed:
        exp.amount_home_minor = recompute_home_minor(
            form.amount_minor, exp.currency, exp.home_currency, exp.fx_rate
        )
    # else: neither changed → leave fx_rate / amount_home_minor untouched

    exp.amount_minor = form.amount_minor
    exp.currency = form.currency
    exp.category = form.category.value
    exp.notes = form.notes
    exp.incurred_on = form.incurred_on
    exp.status = form.status
    exp.segment_id = uuid.UUID(form.segment_id) if form.segment_id else None
    exp.document_id = uuid.UUID(form.document_id) if form.document_id else None
    exp.deposit_minor = form.deposit_minor
    exp.cancellation_deadline = form.cancellation_deadline
    exp.cancellation_fee_minor = form.cancellation_fee_minor

    await db.commit()
    return RedirectResponse(f"/trips/{exp.trip_id}", status_code=303)


@router.post("/expenses/{expense_id}/delete")
async def delete_expense(
    expense_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    exp = await db.get(Expense, expense_id)
    if exp is None or not (
        exp.owner_user_id == user.id
        or await _user_can_access_trip(db, user, exp.trip_id)
    ):
        raise HTTPException(404)
    trip_id = exp.trip_id
    await db.delete(exp)
    await db.commit()
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


def _render_form(
    request: Request, user: User, trip_id: uuid.UUID,
    values: dict, *, errors: dict, edit_id: uuid.UUID | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "expenses/form.html",
        {"user": user, "trip_id": trip_id, "values": values, "errors": errors,
         "edit_id": edit_id, "category_labels": CATEGORY_LABELS,
         "home_currency": user.home_currency},
    )


def _pydantic_errors(exc: ValidationError) -> dict[str, str]:
    return {".".join(str(p) for p in e["loc"]): e["msg"] for e in exc.errors()}


def _expense_to_form_values(e: Expense) -> dict:
    return {
        "amount_minor": e.amount_minor, "currency": e.currency, "category": e.category,
        "notes": e.notes or "", "incurred_on": e.incurred_on.isoformat(), "status": e.status,
        "segment_id": str(e.segment_id) if e.segment_id else "",
        "document_id": str(e.document_id) if e.document_id else "",
        "deposit_minor": e.deposit_minor or "",
        "cancellation_deadline": e.cancellation_deadline.isoformat() if e.cancellation_deadline else "",
        "cancellation_fee_minor": e.cancellation_fee_minor or "",
    }
```

### Step 6.3 — Tests

Tests cover (file: `tests/test_routes_expenses_crud.py`):
1. `test_create_expense_freezes_fx` — POST EUR 38.00, verify `fx_rate` and `amount_home_minor` stored correctly (mock `get_rate`).
2. `test_create_expense_unauthorized_returns_404` — non-traveler.
3. `test_edit_amount_only_keeps_fx_rate` — currency unchanged, amount changes → fx_rate identical, home_minor recomputed.
4. `test_edit_currency_changed_refetches_fx` — currency changes → mock `get_rate` called once.
5. `test_edit_no_changes_no_recompute` — POST identical values → no `get_rate` call, fx_rate unchanged.
6. `test_create_with_fx_unavailable_renders_503_message` — patch `get_rate` to raise FxError; assert form re-rendered with `_form` error and Expense row count is 0.
7. `test_create_with_home_currency_mismatch_re_renders` — `home_currency_at_load=EUR` but user.home_currency=USD → flash and form re-render.
8. `test_delete_expense_removes_row`.
9. `test_cancellation_triple_persisted` — non-null `deposit_minor`, `cancellation_deadline`, `cancellation_fee_minor`.

- [ ] **Step 6.1:** Write `expense_forms.py`.
- [ ] **Step 6.2:** Write all 9 tests in `tests/test_routes_expenses_crud.py`. Run → fail/import errors.
- [ ] **Step 6.3:** Implement `routes/expenses.py` (template `expenses/form.html` is created in Task 7 — for now, tests can use a stub or assert via 303 redirects + DB state, NOT template render).
- [ ] **Step 6.4:** Add to `app.py`:

```python
from trip_tracker.routes import expenses as expenses_routes
...
app.include_router(expenses_routes.router)
```

- [ ] **Step 6.5:** Run tests. Some may need `expenses/form.html` to exist — create a minimal stub template for now (Task 7 fleshes it out):

```html
{# templates/expenses/form.html — stub (Task 7 expands) #}
<form method="post"><input name="amount_minor"></form>
```

- [ ] **Step 6.6:** All 9 tests PASS. mypy + ruff clean.
- [ ] **Step 6.7:** Commit:

```bash
git add src/trip_tracker/schemas/expense_forms.py src/trip_tracker/routes/expenses.py src/trip_tracker/app.py src/trip_tracker/templates/expenses/form.html tests/test_routes_expenses_crud.py
git commit -m "feat(expenses): CRUD routes with edit-path recompute rule"
```

---

## Task 7 — Expense form template + cancellation/deposit collapsible

**Spec ref:** §6.4.
**Model:** sonnet (djlint-heavy, accessibility, status quo styling).

**Files:**
- Modify: `src/trip_tracker/templates/expenses/form.html` (replace stub from Task 6)
- Create: `src/trip_tracker/templates/expenses/_row.html` (reused in Task 8 trip-detail rendering — created here for proximity)

### Step 7.1 — `expenses/form.html`

```html
{% extends "base.html" %}
{% block title %}{% if edit_id %}Edit expense{% else %}New expense{% endif %} · trip-tracker{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">
    {% if edit_id %}Edit expense{% else %}New expense{% endif %}
  </h1>
  {% if errors._form %}<p class="mt-2 text-red-600">{{ errors._form }}</p>{% endif %}
  <form method="post"
        action="{% if edit_id %}/expenses/{{ edit_id }}{% else %}/trips/{{ trip_id }}/expenses{% endif %}"
        class="mt-6 space-y-4">
    <input type="hidden" name="home_currency_at_load" value="{{ home_currency }}">
    <div class="grid grid-cols-2 gap-3">
      <label class="block text-sm">
        Amount (minor units)
        <input class="mt-1 w-full rounded border p-2"
               name="amount_minor" type="number" min="0" required
               value="{{ values.get('amount_minor', '') }}">
        {% if errors.amount_minor %}<span class="text-red-600">{{ errors.amount_minor }}</span>{% endif %}
      </label>
      <label class="block text-sm">
        Currency
        <input class="mt-1 w-full rounded border p-2"
               name="currency" maxlength="3" required
               value="{{ values.get('currency', '') }}"
               list="currency-codes">
        <datalist id="currency-codes">
          <option value="USD"><option value="EUR"><option value="GBP">
          <option value="JPY"><option value="CAD"><option value="AUD">
          <option value="CHF"><option value="CNY">
        </datalist>
      </label>
      <label class="block text-sm">
        Date
        <input class="mt-1 w-full rounded border p-2"
               name="incurred_on" type="date" required
               value="{{ values.get('incurred_on', '') }}">
      </label>
      <label class="block text-sm">
        Category
        <select class="mt-1 w-full rounded border p-2" name="category" required>
          {% for cat, label in category_labels.items() %}
            <option value="{{ cat.value }}"
                    {% if values.get('category') == cat.value %}selected{% endif %}>
              {{ label }}
            </option>
          {% endfor %}
        </select>
      </label>
      <label class="block text-sm">
        Status
        <select class="mt-1 w-full rounded border p-2" name="status">
          <option value="paid" {% if values.get('status', 'paid') == 'paid' %}selected{% endif %}>Paid</option>
          <option value="pending" {% if values.get('status') == 'pending' %}selected{% endif %}>Pending</option>
        </select>
      </label>
    </div>
    <label class="block text-sm">
      Notes
      <textarea class="mt-1 w-full rounded border p-2" name="notes" rows="2">{{ values.get('notes', '') }}</textarea>
    </label>
    <details class="rounded border p-3 text-sm">
      <summary class="cursor-pointer font-medium">Has cancellation policy / deposit?</summary>
      <div class="mt-2 grid grid-cols-3 gap-3">
        <label>Deposit (minor)
          <input class="mt-1 w-full rounded border p-2"
                 name="deposit_minor" type="number" min="0"
                 value="{{ values.get('deposit_minor', '') }}">
        </label>
        <label>Cancellation deadline
          <input class="mt-1 w-full rounded border p-2"
                 name="cancellation_deadline" type="date"
                 value="{{ values.get('cancellation_deadline', '') }}">
        </label>
        <label>Cancellation fee (minor)
          <input class="mt-1 w-full rounded border p-2"
                 name="cancellation_fee_minor" type="number" min="0"
                 value="{{ values.get('cancellation_fee_minor', '') }}">
        </label>
      </div>
    </details>
    <div class="flex gap-3">
      <button class="rounded bg-zinc-900 px-4 py-2 text-white dark:bg-zinc-100 dark:text-zinc-900">
        Save expense
      </button>
      <a href="/trips/{{ trip_id }}" class="rounded border px-4 py-2">Cancel</a>
    </div>
  </form>
{% endblock %}
```

### Step 7.2 — `expenses/_row.html` (used by trip detail in Task 8)

```html
{# Single expense row partial. Spec §6.3. #}
<li class="flex items-baseline gap-3 border-b py-2 text-sm">
  <span class="w-24 text-zinc-500">{{ e.incurred_on }}</span>
  <span class="w-28 rounded bg-zinc-100 px-2 dark:bg-zinc-800">
    {{ category_labels[Category(e.category)] }}
  </span>
  <span class="font-medium">
    {{ e.currency }} {{ "%.2f"|format(e.amount_minor / 10**minor_digits(e.currency)) }}
  </span>
  <span class="text-zinc-500">
    ({{ e.home_currency }} {{ "%.2f"|format(e.amount_home_minor / 10**minor_digits(e.home_currency)) }})
  </span>
  {% if e.status == 'paid' %}<span title="Paid">&#9989;</span>
  {% else %}<span title="Pending">&#8987;</span>{% endif %}
  {% if e.document_id %}
    <a href="/documents/{{ e.document_id }}" title="Receipt">&#128206;</a>
  {% endif %}
  {% if e.notes %}<span class="text-zinc-600">{{ e.notes }}</span>{% endif %}
  <span class="ml-auto flex gap-2">
    <a href="/expenses/{{ e.id }}/edit" class="text-sm text-blue-600">edit</a>
    <form method="post" action="/expenses/{{ e.id }}/delete" class="inline">
      <button class="text-sm text-red-600">delete</button>
    </form>
  </span>
</li>
{% if e.status == 'pending' and e.cancellation_deadline %}
  {% set days_left = (e.cancellation_deadline - today).days %}
  {% if days_left >= 0 and days_left <= 30 %}
    <li class="ml-24 text-xs text-amber-700">
      &#9888; Deposit forfeit after {{ e.cancellation_deadline }}
      ({{ days_left }} day{% if days_left != 1 %}s{% endif %})
    </li>
  {% endif %}
{% endif %}
```

- [ ] **Step 7.1:** Replace the stub `expenses/form.html` with the full markup above.
- [ ] **Step 7.2:** Create `expenses/_row.html`.
- [ ] **Step 7.3:** Run djlint: `uv run pre-commit run djlint-reformat --files src/trip_tracker/templates/expenses/form.html src/trip_tracker/templates/expenses/_row.html`.
- [ ] **Step 7.4:** Re-run Task 6 test suite — all still pass with the real template.
- [ ] **Step 7.5:** Commit:

```bash
git commit -am "feat(expenses): form.html + _row.html templates"
```

---

## Task 8 — Trip detail expense section + rollups + FxError swallow

**Spec ref:** §6.2, §6.3, §9 (FxError fallback).
**Model:** sonnet (touches existing route + template; FxError swallow is the gotcha).

**Files:**
- Modify: `src/trip_tracker/routes/trips.py::trip_detail`
- Modify: `src/trip_tracker/templates/trips/detail.html`
- Create: `tests/test_routes_trips_expense_section.py`

### Step 8.1 — Tests

```python
# tests/test_routes_trips_expense_section.py
@pytest.mark.asyncio
async def test_trip_detail_renders_paid_total_in_home_currency(...): ...

@pytest.mark.asyncio
async def test_trip_detail_renders_expected_total_paid_plus_pending(...): ...

@pytest.mark.asyncio
async def test_trip_detail_by_category_excludes_pending(...): ...

@pytest.mark.asyncio
async def test_trip_detail_saved_by_points_when_award_set(monkeypatch, ...):
    """Award segment with cash_equivalent=$1500, copay=$50 → saved $1450 (USD home)."""

@pytest.mark.asyncio
async def test_trip_detail_swallows_fxerror_in_saved_by_points(monkeypatch, ...):
    """FxError from get_rate during render → page still renders, saved-by-points line hidden."""
    from trip_tracker.expenses.fx import FxError
    async def _boom(*a, **kw): raise FxError("frankfurter down")
    monkeypatch.setattr("trip_tracker.routes.trips.get_rate", _boom)
    # ... build trip with award segment, hit /trips/<id>, expect 200 ...
    assert resp.status_code == 200
    assert b"Saved by points" not in resp.content

@pytest.mark.asyncio
async def test_trip_detail_cancellation_warning_renders_within_30_days(...): ...

@pytest.mark.asyncio
async def test_trip_detail_no_cancellation_warning_when_far_out(...): ...
```

### Step 8.2 — Handler change

In `routes/trips.py::trip_detail`, after segments are loaded, add:

```python
from collections import defaultdict
from datetime import date as _date_cls
from decimal import Decimal

from trip_tracker.expenses.currencies import minor_digits
from trip_tracker.expenses.categories import Category, CATEGORY_LABELS
from trip_tracker.expenses.fx import FxError, get_rate
from trip_tracker.expenses.freeze import recompute_home_minor
from trip_tracker.models.expense import Expense
from redis.asyncio import Redis as AsyncRedis

# ... inside trip_detail(...) handler signature, add:
#     redis: AsyncRedis = Depends(_redis),

expenses = (await db.execute(
    select(Expense).where(Expense.trip_id == trip.id)
    .order_by(Expense.incurred_on.desc(), Expense.created_at.desc())
)).scalars().all()

home_currency = user.home_currency
total_paid_home = sum(e.amount_home_minor for e in expenses if e.status == "paid")
total_expected_home = sum(e.amount_home_minor for e in expenses)
by_category: dict[str, int] = defaultdict(int)
for e in expenses:
    if e.status == "paid":
        by_category[e.category] += e.amount_home_minor

# Saved-by-points rollup with FxError swallow.
total_saved_home: int | None = 0
try:
    for s in segments:
        award = (s.details or {}).get("award")
        if not award or award.get("cash_equivalent_minor") is None:
            continue
        eq_rate = await get_rate(award["cash_equivalent_currency"], home_currency, redis)
        cp_rate = await get_rate(award["cash_copay_currency"], home_currency, redis)
        eq_home = recompute_home_minor(
            award["cash_equivalent_minor"], award["cash_equivalent_currency"],
            home_currency, eq_rate,
        )
        cp_home = recompute_home_minor(
            award["cash_copay_minor"], award["cash_copay_currency"],
            home_currency, cp_rate,
        )
        total_saved_home += eq_home - cp_home
except FxError:
    logger.info("FX unavailable; saved-by-points rollup hidden for trip %s", trip.id)
    total_saved_home = None

# Add to context dict:
context.update({
    "expenses": expenses,
    "total_paid_home": total_paid_home,
    "total_expected_home": total_expected_home,
    "by_category": by_category,
    "total_saved_home": total_saved_home,
    "home_currency": home_currency,
    "category_labels": CATEGORY_LABELS,
    "Category": Category,
    "minor_digits": minor_digits,
    "today": _date_cls.today(),
})
```

### Step 8.3 — Template insert (`trips/detail.html`)

Insert before the segments list (or wherever fits the existing layout). Match existing Tailwind:

```html
<section class="mt-8">
  <div class="flex items-baseline justify-between">
    <h2 class="text-2xl font-semibold">Expenses</h2>
    <a href="/trips/{{ trip.id }}/expenses/new"
       class="rounded bg-zinc-900 px-3 py-1 text-sm text-white dark:bg-zinc-100 dark:text-zinc-900">
      + Add expense
    </a>
  </div>
  <p class="mt-1 text-sm">
    Spent so far: <strong>{{ home_currency }}
      {{ "%.2f"|format(total_paid_home / 10**minor_digits(home_currency)) }}</strong>
    · Expected: {{ home_currency }}
      {{ "%.2f"|format(total_expected_home / 10**minor_digits(home_currency)) }}
    {% if total_saved_home is not none and total_saved_home > 0 %}
      · &#9992; Saved by points: ~{{ home_currency }}
        {{ "%.2f"|format(total_saved_home / 10**minor_digits(home_currency)) }}
    {% endif %}
  </p>
  {% if by_category %}
    <p class="mt-1 text-xs text-zinc-500">
      {% for cat, total in by_category.items() %}
        {{ category_labels[Category(cat)] }} {{ home_currency }}
          {{ "%.2f"|format(total / 10**minor_digits(home_currency)) }}{% if not loop.last %} · {% endif %}
      {% endfor %}
    </p>
  {% endif %}
  {% if expenses %}
    <ul class="mt-3">
      {% for e in expenses %}
        {% include "expenses/_row.html" %}
      {% endfor %}
    </ul>
  {% else %}
    <p class="mt-3 text-sm text-zinc-500">No expenses yet.</p>
  {% endif %}
</section>
```

(The `GET /trips/{trip_id}/expenses/new` handler that renders the empty form was created in Task 6 — the trip-detail "+ Add expense" link points to it.)

- [ ] **Step 8.1:** Add the 7 tests. Run → fail.
- [ ] **Step 8.2:** Implement the handler change in `trip_detail`.
- [ ] **Step 8.3:** Implement the template section + the `new_expense_form` route addition.
- [ ] **Step 8.4:** Run all tests → PASS.
- [ ] **Step 8.5:** Commit:

```bash
git commit -am "feat(expenses): trip-detail section + rollups + FxError swallow"
```

---

## Task 9 — `AwardDetails` Pydantic model + segment-route integration

**Spec ref:** §4.6.
**Model:** haiku (schema-only).

**Files:**
- Create: `src/trip_tracker/expenses/awards.py`
- Modify: `src/trip_tracker/routes/segments.py` (parse award fields + clear-award short-circuit)
- Create: `tests/test_expenses_awards.py`

### Step 9.1 — `awards.py` skeleton

```python
"""AwardDetails model + display helpers. Spec §4.6, §6.6."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator


class AwardDetails(BaseModel):
    program: str = Field(min_length=1, max_length=100)
    points_spent: int = Field(ge=1)
    cash_copay_minor: int = Field(ge=0)
    cash_copay_currency: str = Field(min_length=3, max_length=3)
    cash_equivalent_minor: int | None = Field(default=None, ge=0)
    cash_equivalent_currency: str | None = Field(default=None, min_length=3, max_length=3)

    @field_validator("cash_copay_currency", "cash_equivalent_currency")
    @classmethod
    def upper(cls, v: str | None) -> str | None:
        return v.upper() if v else v

    @model_validator(mode="after")
    def _equivalent_pair(self) -> "AwardDetails":
        """If cash_equivalent_minor is set, cash_equivalent_currency must be too.
        Saved-by-points rollup needs both — None currency would crash the rate lookup."""
        if self.cash_equivalent_minor is not None and not self.cash_equivalent_currency:
            raise ValueError("cash_equivalent_currency required when cash_equivalent_minor is set")
        return self


def k_format(points: int) -> str:
    """75000 → '75k', 1500 → '1.5k', 100 → '100'."""
    if points < 1000:
        return str(points)
    val = points / 1000.0
    if val == int(val):
        return f"{int(val)}k"
    return f"{val:.1f}k"


_PROGRAM_SHORT = {
    "Chase Ultimate Rewards": "Chase UR",
    "Amex Membership Rewards": "Amex MR",
    "Capital One Venture": "C1 Venture",
    "Citi ThankYou": "Citi TY",
    "Bilt Rewards": "Bilt",
    "United MileagePlus": "United",
    "Delta SkyMiles": "Delta",
    "American AAdvantage": "AAdvantage",
    "Alaska Mileage Plan": "Alaska",
    "Marriott Bonvoy": "Marriott",
    "Hyatt World of Hyatt": "Hyatt",
    "Hilton Honors": "Hilton",
    "IHG One Rewards": "IHG",
}


def program_short(program: str) -> str:
    """Map known program names to short forms; unknown programs pass through."""
    return _PROGRAM_SHORT.get(program, program)
```

### Step 9.2 — Tests

```python
# tests/test_expenses_awards.py
import pytest
from pydantic import ValidationError

from trip_tracker.expenses.awards import AwardDetails, k_format, program_short


@pytest.mark.parametrize("inp,exp", [(75000, "75k"), (1500, "1.5k"), (1000, "1k"), (100, "100"),
                                       (999, "999"), (12345, "12.3k")])
def test_k_format(inp: int, exp: str) -> None:
    assert k_format(inp) == exp


def test_program_short_known() -> None:
    assert program_short("Chase Ultimate Rewards") == "Chase UR"
    assert program_short("Amex Membership Rewards") == "Amex MR"


def test_program_short_unknown_passthrough() -> None:
    assert program_short("Random Loyalty Program") == "Random Loyalty Program"


def test_award_details_valid() -> None:
    a = AwardDetails(program="Chase Ultimate Rewards", points_spent=75000,
                     cash_copay_minor=560, cash_copay_currency="usd")
    assert a.cash_copay_currency == "USD"  # upper validator


def test_award_details_zero_points_rejected() -> None:
    with pytest.raises(ValidationError):
        AwardDetails(program="X", points_spent=0,
                     cash_copay_minor=0, cash_copay_currency="USD")


def test_award_details_negative_copay_rejected() -> None:
    with pytest.raises(ValidationError):
        AwardDetails(program="X", points_spent=1,
                     cash_copay_minor=-1, cash_copay_currency="USD")
```

### Step 9.3 — `routes/segments.py` integration

In the existing flight + lodging POST/edit handlers, after parsing common fields:

```python
from sqlalchemy.orm.attributes import flag_modified
from pydantic import ValidationError as _VE
from trip_tracker.expenses.awards import AwardDetails

def _apply_award_from_form(seg: Segment, form: dict[str, str]) -> dict | None:
    """Mutate seg.details based on award fields in form. Returns errors dict or None."""
    if form.get("clear_award") == "1" and (seg.details or {}).get("award"):
        seg.details = {k: v for k, v in (seg.details or {}).items() if k != "award"}
        flag_modified(seg, "details")
        return None
    if not form.get("award_points_spent"):
        return None  # award fields blank → no-op (don't touch existing if present)
    try:
        award = AwardDetails(
            program=form.get("award_program", ""),
            points_spent=int(form["award_points_spent"]),
            cash_copay_minor=int(form.get("award_cash_copay_minor") or 0),
            cash_copay_currency=form.get("award_cash_copay_currency", "USD"),
            cash_equivalent_minor=int(form["award_cash_equivalent_minor"])
                if form.get("award_cash_equivalent_minor") else None,
            cash_equivalent_currency=form.get("award_cash_equivalent_currency") or None,
        )
    except (_VE, ValueError) as exc:
        return {"award": str(exc)}
    details = dict(seg.details or {})
    details["award"] = award.model_dump(exclude_none=True)
    seg.details = details
    flag_modified(seg, "details")
    return None
```

Call `_apply_award_from_form(seg, form_data)` inside flight + lodging handlers AFTER constructing/loading the segment but BEFORE `db.commit()`. If it returns errors, re-render the form.

**Concrete wiring point** (`src/trip_tracker/routes/segments.py`):

```python
# Inside POST /segments and POST /segments/{id} handlers, after the segment
# row is built/loaded and just before the existing `await db.commit()`:
form_data = dict(await request.form())
award_errors = _apply_award_from_form(seg, form_data)
if award_errors:
    # Existing form-error path: build the same error context shape used for
    # validation failures elsewhere in this file (errors dict passed to
    # templates).
    return templates.TemplateResponse(
        request,
        f"segments/{seg.type}_form.html",
        {"user": user, "values": form_data,
         "errors": award_errors,
         "existing_award": (seg.details or {}).get("award")},
    )
# … existing db.commit() runs unchanged …
```

The `existing_award` context key is also passed to the GET-edit handlers (Task 10 Step 10.2) so the partial can pre-populate.

- [ ] **Step 9.1:** Write `awards.py`.
- [ ] **Step 9.2:** Write tests in `tests/test_expenses_awards.py`. Run → ImportError → PASS.
- [ ] **Step 9.3:** Wire `_apply_award_from_form` into flight + lodging routes. Tests for the route integration are added in Task 10.
- [ ] **Step 9.4:** Commit:

```bash
git commit -am "feat(expenses): AwardDetails model + k_format/program_short + segment route hook"
```

---

## Task 10 — Award fields on flight + lodging forms (with clear-award)

**Spec ref:** §6.5.
**Model:** sonnet (two forms, similar structure; clear-award path is the gotcha).

**Files:**
- Create: `src/trip_tracker/templates/segments/_award_section.html`
- Modify: `src/trip_tracker/templates/segments/flight_form.html`
- Modify: `src/trip_tracker/templates/segments/lodging_form.html`
- Create: `tests/test_routes_segments_award.py`

### Step 10.1 — `_award_section.html` partial

```html
{# Reusable award fields. Spec §6.5. Caller passes `existing_award` (dict|None). #}
<details class="rounded border p-3 text-sm" {% if existing_award %}open{% endif %}>
  <summary class="cursor-pointer font-medium">
    Booked with miles or points{% if existing_award %} (set){% endif %}
  </summary>
  <div class="mt-2 grid grid-cols-2 gap-3">
    <label class="block text-sm">
      Program
      <input class="mt-1 w-full rounded border p-2" name="award_program"
             list="award-programs" maxlength="100"
             value="{{ existing_award.program if existing_award else '' }}">
      <datalist id="award-programs">
        <option value="Chase Ultimate Rewards">
        <option value="Amex Membership Rewards">
        <option value="Capital One Venture">
        <option value="Citi ThankYou">
        <option value="Bilt Rewards">
        <option value="United MileagePlus">
        <option value="Delta SkyMiles">
        <option value="American AAdvantage">
        <option value="Alaska Mileage Plan">
        <option value="Marriott Bonvoy">
        <option value="Hyatt World of Hyatt">
        <option value="Hilton Honors">
        <option value="IHG One Rewards">
      </datalist>
    </label>
    <label class="block text-sm">
      Points spent
      <input class="mt-1 w-full rounded border p-2" name="award_points_spent"
             type="number" min="0"
             value="{{ existing_award.points_spent if existing_award else '' }}">
    </label>
    <label class="block text-sm">
      Cash co-pay (minor)
      <input class="mt-1 w-full rounded border p-2" name="award_cash_copay_minor"
             type="number" min="0"
             value="{{ existing_award.cash_copay_minor if existing_award else '' }}">
    </label>
    <label class="block text-sm">
      Co-pay currency
      <input class="mt-1 w-full rounded border p-2" name="award_cash_copay_currency"
             maxlength="3"
             value="{{ existing_award.cash_copay_currency if existing_award else 'USD' }}">
    </label>
    <label class="block text-sm">
      Cash equivalent (minor, optional)
      <input class="mt-1 w-full rounded border p-2" name="award_cash_equivalent_minor"
             type="number" min="0"
             value="{{ existing_award.cash_equivalent_minor if existing_award else '' }}">
      <span class="text-xs text-zinc-500">What this would have cost in cash. Used for "saved by points" totals.</span>
    </label>
    <label class="block text-sm">
      Equivalent currency
      <input class="mt-1 w-full rounded border p-2" name="award_cash_equivalent_currency"
             maxlength="3"
             value="{{ existing_award.cash_equivalent_currency if existing_award else 'USD' }}">
    </label>
    {% if existing_award %}
      <label class="col-span-2 mt-2 text-sm text-red-600">
        <input type="checkbox" name="clear_award" value="1">
        Clear award metadata (delete the existing award fields)
      </label>
    {% endif %}
  </div>
</details>
```

### Step 10.2 — Insert `{% include "segments/_award_section.html" %}` into both flight_form.html and lodging_form.html, before the closing button. Pass `existing_award` from each form's view (the existing form-render handlers must add it to context: `existing_award = (segment.details or {}).get("award") if segment else None`).

### Step 10.3 — Tests

```python
# tests/test_routes_segments_award.py
@pytest.mark.asyncio
async def test_create_flight_with_award_writes_details(...): ...

@pytest.mark.asyncio
async def test_edit_flight_clear_award_removes_key(...):
    """clear_award=1 → details.award gone, other details preserved."""

@pytest.mark.asyncio
async def test_edit_flight_clear_award_skips_validation(...):
    """clear_award=1 with blank fields → 303 redirect, no 400."""

@pytest.mark.asyncio
async def test_award_zero_points_rejected_with_form_error(...): ...

@pytest.mark.asyncio
async def test_lodging_award_writes_details(...): ...
```

- [ ] **Step 10.1:** Create `_award_section.html`.
- [ ] **Step 10.2:** Modify `flight_form.html` and `lodging_form.html` with includes.
- [ ] **Step 10.3:** Update segment route handlers to pass `existing_award` to the templates.
- [ ] **Step 10.4:** Verify `_apply_award_from_form` from Task 9 handles all 5 test cases. Tests PASS.
- [ ] **Step 10.5:** Run djlint on the modified templates.
- [ ] **Step 10.6:** Commit:

```bash
git commit -am "feat(expenses): award fields on flight + lodging forms + clear-award path"
```

---

## Task 11 — Award badge on segment row

**Spec ref:** §6.6.
**Model:** haiku.

**Files:**
- Create: `src/trip_tracker/templates/segments/_award_badge.html`
- Modify: `src/trip_tracker/templates/segments/_row.html` (include the badge)
- Create: `tests/test_award_badge_render.py`

### Step 11.1 — Badge template

```html
{# Award badge. Caller provides `award` (dict). Use k_format / program_short via Jinja filters. #}
{% if award %}
  <span class="ml-2 rounded bg-amber-100 px-2 py-0.5 text-xs text-amber-900">
    &#9992; {{ award.points_spent | k_format }} {{ award.program | program_short }}
    {% if award.cash_copay_minor and award.cash_copay_minor > 0 %}
      + {{ award.cash_copay_currency }}
        {{ "%.2f"|format(award.cash_copay_minor / 10**minor_digits(award.cash_copay_currency)) }}
    {% endif %}
    {% if award.cash_equivalent_minor %}
      — saved ~{{ award.cash_equivalent_currency }}
        {{ "%.2f"|format((award.cash_equivalent_minor - (award.cash_copay_minor or 0))
                         / 10**minor_digits(award.cash_equivalent_currency)) }}
    {% endif %}
  </span>
{% endif %}
```

### Step 11.2 — Register Jinja filters + globals on EVERY `Jinja2Templates` instance.

The repo has multiple `Jinja2Templates` singletons (one per route module — `trips.py`, `expenses.py`, `segments.py`, `settings.py` etc.). Each instance has its own filter/globals dict. Rather than registering in each, create a small helper and call it from every route module that imports `Jinja2Templates`.

Create `src/trip_tracker/templating.py`:

```python
"""Shared Jinja env extensions. Call register_globals(templates) from every
route module's templates instance so filters/globals are available everywhere."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from trip_tracker.expenses.awards import k_format, program_short
from trip_tracker.expenses.categories import CATEGORY_LABELS, Category
from trip_tracker.expenses.currencies import minor_digits


def register_globals(templates: Jinja2Templates) -> None:
    templates.env.filters["k_format"] = k_format
    templates.env.filters["program_short"] = program_short
    templates.env.globals["minor_digits"] = minor_digits
    templates.env.globals["Category"] = Category
    templates.env.globals["category_labels"] = CATEGORY_LABELS
```

Then in **every** route module that instantiates `templates = Jinja2Templates(...)`, add immediately after instantiation:

```python
from trip_tracker.templating import register_globals
register_globals(templates)
```

Run `grep -rn "Jinja2Templates(" src/trip_tracker/routes/` to find all sites — currently: `admin.py`, `documents.py`, `home.py`, `inbox.py`, `map.py`, `segments.py`, `settings.py`, `trips.py`, plus the new `expenses.py`. Register on **all** of them: any template that ever includes `segments/_row.html` (which now references `minor_digits` as a global) needs the registration, and `_row.html` is referenced from multiple parents (trip detail, inbox previews, map popups). It's cheaper to register globally than to track which partials propagate where.

This means `_row.html`'s `minor_digits(e.currency)` call resolves as a global (NOT a filter), and `_award_badge.html`'s `{{ award.points_spent | k_format }}` resolves as a filter, on every render path consistently.

### Step 11.3 — Modify `segments/_row.html`:

```html
{# After the existing main content of the row, add: #}
{% set award = (s.details or {}).get("award") %}
{% include "segments/_award_badge.html" %}
```

### Step 11.4 — Tests

```python
# tests/test_award_badge_render.py
def test_badge_renders_for_award_segment(client, ...):
    """Trip with one flight segment having details.award → page contains '75k Chase UR'."""

def test_badge_omits_saved_when_no_equivalent(client, ...):
    """Award with no cash_equivalent_minor → badge has no 'saved' suffix."""

def test_badge_omits_copay_when_zero(client, ...):
    """copay_minor=0 → no '+ $0.00' suffix."""
```

- [ ] **Step 11.1–4:** Create badge, register filters, modify row, add tests.
- [ ] **Step 11.5:** Run tests → PASS. djlint clean.
- [ ] **Step 11.6:** Commit:

```bash
git commit -am "feat(expenses): award badge on segment row"
```

---

## Task 12 — Award-program autocomplete endpoint

**Spec ref:** §6.1, §6.5.
**Model:** haiku.

**Files:**
- Modify: `src/trip_tracker/routes/segments.py` (add `GET /segments/award-programs.json`)
- Modify: `src/trip_tracker/templates/segments/_award_section.html` (use `<datalist>` populated from this endpoint via small inline JS)
- Create: `tests/test_routes_segments_award_autocomplete.py`

### Step 12.1 — Endpoint

```python
@router.get("/segments/award-programs.json")
async def award_programs_autocomplete(
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> list[str]:
    """Return up to 20 most recent distinct `program` values from this user's
    award segments, ordered by recency."""
    from sqlalchemy import func as _f
    # Use jsonb_extract_path_text for nested JSONB key lookup. SQLAlchemy 2.x
    # does NOT chain `details["award"]["program"]` cleanly to text — we must
    # call the Postgres function explicitly.
    program_expr = _f.jsonb_extract_path_text(Segment.details, "award", "program")
    res = await db.execute(
        select(program_expr)
        .where(Segment.owner_user_id == user.id)
        .where(program_expr.is_not(None))
        .order_by(Segment.created_at.desc())
        .limit(50)
    )
    seen: list[str] = []
    for row in res.scalars():
        if row and row not in seen:
            seen.append(row)
        if len(seen) >= 20:
            break
    return seen
```

### Step 12.2 — Template wiring (small JS in `_award_section.html`):

```html
{# After the static datalist, add a script that fetches user-recent programs and merges them #}
<script>
  (async () => {
    const dl = document.getElementById("award-programs");
    if (!dl) return;
    try {
      const resp = await fetch("/segments/award-programs.json");
      if (!resp.ok) return;
      const programs = await resp.json();
      for (const p of programs) {
        if (!Array.from(dl.options).some(o => o.value === p)) {
          const opt = document.createElement("option");
          opt.value = p;
          dl.appendChild(opt);
        }
      }
    } catch {}
  })();
</script>
```

### Step 12.3 — Tests

```python
@pytest.mark.asyncio
async def test_autocomplete_returns_distinct_recent_programs(...): ...

@pytest.mark.asyncio
async def test_autocomplete_only_returns_owned_segments(...):
    """Other user's segments are NOT exposed."""

@pytest.mark.asyncio
async def test_autocomplete_unauthenticated_redirects(...): ...
```

- [ ] **Step 12.1–3:** Implement, write tests. Tests PASS.
- [ ] **Step 12.4:** Commit:

```bash
git commit -am "feat(expenses): award-program autocomplete endpoint"
```

---

## Task 13 — Settings page `home_currency` dropdown + POST handler

**Spec ref:** §6.7.
**Model:** haiku (mirrors Phase 6 ICS regenerate POST shape).

**Files:**
- Modify: `src/trip_tracker/routes/settings.py`
- Modify: `src/trip_tracker/templates/settings/page.html`
- Create: `tests/test_routes_settings_home_currency.py`

### Step 13.1 — Handler

```python
@router.post("/settings/home_currency")
async def update_home_currency(
    request: Request,
    home_currency: str = Form(...),
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    code = home_currency.strip().upper()
    if len(code) != 3 or not code.isalpha():
        if "session" in request.scope:
            request.session["flash"] = {"kind": "home_currency_invalid"}
        return RedirectResponse("/settings", status_code=303)
    user.home_currency = code
    db.add(user)
    await db.commit()
    if "session" in request.scope:
        request.session["flash"] = {"kind": "home_currency_saved", "code": code}
    return RedirectResponse("/settings", status_code=303)
```

### Step 13.2 — Settings page extension

```html
<section class="mt-8 rounded border p-4">
  <h2 class="text-xl font-medium">Home currency</h2>
  <p class="mt-1 text-sm text-zinc-500">
    Used for trip-total rollups. Changing this only affects new expenses;
    existing rows keep their original frozen FX.
  </p>
  <form method="post" action="/settings/home_currency" class="mt-3 flex items-center gap-3">
    <select class="rounded border p-2" name="home_currency">
      {% for code in ["USD", "EUR", "GBP", "CAD", "AUD", "JPY", "CHF", "CNY",
                      "INR", "MXN", "BRL", "SGD", "HKD", "NOK", "SEK", "DKK"] %}
        <option value="{{ code }}" {% if user.home_currency == code %}selected{% endif %}>{{ code }}</option>
      {% endfor %}
    </select>
    <button class="rounded bg-zinc-900 px-3 py-1 text-white dark:bg-zinc-100 dark:text-zinc-900">Save</button>
  </form>
</section>
```

### Step 13.3 — Tests

```python
@pytest.mark.asyncio
async def test_post_home_currency_persists(...): ...

@pytest.mark.asyncio
async def test_post_home_currency_invalid_code_rejected(...): ...

@pytest.mark.asyncio
async def test_settings_page_renders_current_home_currency(...): ...
```

- [ ] **Step 13.1–3:** Implement + tests → PASS.
- [ ] **Step 13.2:** Commit:

```bash
git commit -am "feat(expenses): home_currency dropdown on settings page"
```

---

## Task 14 — README "Expenses (Phase 8)" section

**Spec ref:** §8 done-def, §11 deferred items.
**Model:** inline (docs only).

**Files:**
- Modify: `README.md`

Add a section like:

```markdown
## Expenses (Phase 8 — v0.8.0)

trip-tracker tracks per-trip expenses with frozen-at-entry FX so historical totals
never silently shift when ECB rates move.

- **Currencies:** ISO 4217 minor units (cents/sen/fils) stored as `bigint`;
  `Decimal` math throughout; `numeric(20,10)` `fx_rate`. JPY (0 decimals) and
  BHD (3 decimals) are handled via the `CURRENCY_MINOR` lookup.
- **FX:** Frankfurter (free, ECB-backed, no API key). Cached in Redis for 24h.
  If Frankfurter is unreachable AND nothing is cached, the expense save fails
  with a 503 — we never store a wrong rate.
- **Categories:** food, transit, lodging, activities, shopping, gratuities,
  connectivity, other (+ free-text notes).
- **Status:** paid (default) / pending. Pending expenses count toward the
  "Expected" total but not the "Spent so far" total.
- **Cancellation/deposit:** optional triple `deposit_minor` /
  `cancellation_deadline` / `cancellation_fee_minor`. Pending expenses with a
  deadline within 30 days surface a warning on the trip detail page.
- **Award redemptions:** flight + lodging segments accept inline award metadata
  (program, points, cash co-pay, optional cash equivalent). Covers airline
  miles AND CC-transferable points (Chase UR, Amex MR, Capital One, Citi TY,
  Bilt). Per-trip "saved by points" rollup uses live FX at render time;
  Frankfurter outages just hide the line, don't 500 the page.
- **Home currency:** per-user setting, default USD. Changing it only affects
  new expenses — existing rows keep their original frozen FX.

### Deferred to later v0.8.x phases

- v0.8.1 — Auto-extract expenses from forwarded receipt emails (vendor packs +
  Haiku LLM fallback).
- v0.8.2 — CSV import from credit-card statements.
- v0.8.3 — Hotel-loyalty award nights on lodging segments + nightly breakdown.
- v0.8.4 — Per-segment cost rollup.
- v0.8.5 — Expense splitting between travelers (master-spec non-goal; revisit if
  household travel ever becomes in scope).
- v0.8.6 — Multi-currency receipts (e.g., EUR folio + USD card surcharge).
- v0.8.7 — Re-FX historical expenses admin tool.
```

- [ ] **Step 14.1:** Add the section to README.
- [ ] **Step 14.2:** Commit:

```bash
git commit -am "docs(expenses): README Phase 8 section + deferred items"
```

---

## Task 15 — Verification gate + Playwright smoke

**Model:** inline.

- [ ] **Step 15.1:** Run the full pre-commit suite and the test suite:

```bash
uv run pre-commit run --all-files
uv run pytest -q --cov
```

Expected: zero pre-commit failures; coverage **≥ 85%** project-wide; all tests pass. If coverage dropped, identify the file(s) under 85% and either add tests or surface to the user before continuing.

- [ ] **Step 15.2:** Spin up the dev stack (`docker compose up -d`) and run `uv run alembic upgrade head` against a fresh DB. Confirm both Phase 8 migrations apply cleanly in order.

- [ ] **Step 15.3:** Manual smoke (Playwright MCP if available, else clicking through):

  1. Sign in via Authelia.
  2. Visit a trip with at least one flight segment.
  3. Add an expense: amount=3800, currency=EUR, category=food, status=paid. Confirm flash shows "Saved EUR 38.00 (USD 40.65)" (rate will vary; assert the home equivalent is reasonable).
  4. Edit the expense: change amount to 4000 (currency unchanged). Verify the home equivalent is recomputed and `fx_rate` in the DB is unchanged.
  5. Edit again: change currency to JPY. Verify a new `fx_rate` was fetched and home equivalent recomputed.
  6. Add a pending expense with cancellation_deadline today + 5 days. Verify the trip detail shows the warning row.
  7. Edit a flight segment: add award metadata (Chase UR, 75000 points, $5.60 copay, $1500 equivalent). Verify the badge "75k Chase UR + $5.60 — saved ~$1494.40" appears on the segment row and the trip summary line shows "Saved by points: ~$1,494.40".
  8. Visit `/settings`, change home currency to JPY. Verify the warning copy. Add a new expense in JPY currency; verify the trip rollup mixes correctly (old USD/EUR rows keep their frozen home-equiv; new JPY row uses JPY home).
  9. Induce an FxError during the saved-by-points render: temporarily edit `src/trip_tracker/expenses/fx.py::get_rate` to add `raise FxError("smoke test")` at the top of the function, restart the app, and reload a trip with at least one award segment. Verify the page renders 200 and the "Saved by points" line is hidden. (Killing Redis alone is NOT enough — that just falls through to a Frankfurter call which may succeed.) Revert the patch after smoke.
  10. Clear an award via the checkbox; verify the badge is gone and the "saved by points" total drops.

- [ ] **Step 15.4:** If any step fails, file the regression as a follow-up commit and re-run smoke. Don't tag until smoke is clean.

- [ ] **Step 15.5:** Commit any smoke-fix patches with messages like `fix(expenses): <issue> found in v0.8.0 smoke`.

---

## Task 16 — Tag v0.8.0 + push + schedule release-verification

**Model:** inline.

- [ ] **Step 16.1:** Bump version in `pyproject.toml`:

```toml
[project]
version = "0.8.0"
```

- [ ] **Step 16.2:** Commit the bump:

```bash
git commit -am "chore(release): bump version to 0.8.0"
```

- [ ] **Step 16.3:** Tag and push:

```bash
git tag -s v0.8.0 -m "Phase 8 — Expenses + award metadata"
git push origin feat/phase-8-expenses
git push origin v0.8.0
```

- [ ] **Step 16.4:** Open the PR with a summary of v0.8.0 scope (per spec §11), the 16-task task list status, and a smoke-test checklist matching Task 15.3.

- [ ] **Step 16.5:** After merge to main, schedule a release-verification agent to confirm CI green, smoke 1-week post-deploy:

```
/schedule release-verification --branch main --tag v0.8.0 --in 7d
```

---

## Done definition (mirrors Spec §8)

- [ ] `expenses` table + Alembic migration round-trip clean.
- [ ] `users.home_currency` column added; default `USD`.
- [ ] `Expense` ORM with cascade-on-trip-delete, SET-NULL on segment + document delete, owner CASCADE.
- [ ] `expenses/categories.py` exposes the 8-value `Category` enum + `CATEGORY_LABELS`.
- [ ] `expenses/currencies.py` exposes `CURRENCY_MINOR` + `minor_digits`.
- [ ] `expenses/fx.py`: `fetch_rates`, `get_cached_rates`, `set_cached_rates`, `get_rate(base, target, redis)`. Cold-cache `get_rate` performs a Frankfurter call within 1s.
- [ ] `freeze_fx` + `recompute_home_minor` Decimal-math helpers; tests cover JPY/USD/BHD/same-currency.
- [ ] Manual expense routes: create, edit (with §4.4 recompute rule), delete; auth + traveler scope.
- [ ] Trip detail page expense section with paid/expected totals, by-category breakdown, saved-by-points (FxError-swallowed), per-row cancellation warning.
- [ ] Award fields on flight + lodging forms; `clear_award=1` short-circuit; badge on segment row.
- [ ] Award-program autocomplete endpoint.
- [ ] Award badge `k_format` / `program_short` helpers with unit tests.
- [ ] Concurrent home-currency-change guard (hidden `home_currency_at_load` field; mismatch re-renders with flash).
- [ ] `📎` icon links to attached document.
- [ ] Settings page `home_currency` dropdown + POST handler with warning copy.
- [ ] Frankfurter cache miss + 5xx → form re-renders with retry message; expense NOT saved.
- [ ] README "Expenses (Phase 8)" section.
- [ ] Smoke test passes (Task 15.3).
- [ ] Project-wide coverage ≥ 85%; ruff + mypy + bandit + djlint + pre-commit clean.
- [ ] Signed `v0.8.0` tag pushed.
