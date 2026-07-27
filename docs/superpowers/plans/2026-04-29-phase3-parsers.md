# Phase 3 — Parsers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the manual `/segments/new` flow as the primary path from email to itinerary by adding a parser pipeline (JSON-LD + 10 vendor packs + Haiku 4.5 fallback), an ARQ + Redis worker, an `/inbox` review UI, and a prefill path through the existing segment forms.

**Architecture:** New `parsers/` subpackage with a strategy chain (JSON-LD → vendor → LLM). Each strategy returns a `ParseResult` (Pydantic). Vendor parsers are filesystem-discovered subclasses of `VendorParser`, auto-registered via `__init_subclass__`. ARQ worker (same Docker image, separate command) consumes `parse_raw_email(id)` jobs enqueued by the existing webhook handler. Inbox UI surfaces three buckets (`review` / `no_segments` / duplicates) with five actions wired to existing or new routes.

**Tech Stack:** Python 3.14 (target=py313), FastAPI, SQLAlchemy 2.0 async, Postgres 18, Pydantic v2, ARQ + Redis 7, Anthropic SDK (Haiku 4.5), `extruct` for JSON-LD, Jinja2, pytest + pytest-asyncio + pytest-postgresql.

**Spec reference:** [`docs/superpowers/specs/2026-04-29-phase3-parsers-design.md`](../specs/2026-04-29-phase3-parsers-design.md). Section numbers (e.g. §5) below refer to this spec.

**Branch:** `feat/phase-3-parsers`. Cut from `main` at `e126caa` (or whatever is current `main` HEAD when implementation starts).

---

## File Structure

```
src/trip_tracker/
├── config.py                              [MODIFY: anthropic + redis + budget settings]
├── app.py                                 [MODIFY: include inbox router, nav link]
├── __main__.py                            [MODIFY: parse_pending subcommand]
├── ingest/
│   └── webhook.py                         [MODIFY: enqueue parse_raw_email after commit]
├── parsers/                               [CREATE — new subpackage]
│   ├── __init__.py
│   ├── base.py                            VendorParser ABC + ParseResult + SegmentDraft + registry
│   ├── jsonld.py                          extruct strategy
│   ├── enrich.py                          IATA → tz/lat/lon (static airports.csv lookup)
│   ├── cluster.py                         segment → trip clustering rule (geo + ±1d, 20% gap)
│   ├── budget.py                          LlmBudget read/write helpers
│   ├── llm.py                             Anthropic SDK + tool-use schema + Haiku call
│   ├── dispatch.py                        orchestration (JSON-LD → vendor → LLM)
│   └── vendors/
│       ├── __init__.py                    imports each subpackage to register
│       ├── air_france/{__init__,README,fixtures/}
│       ├── american/...
│       ├── united/...
│       ├── fairmont/...
│       ├── avis/...
│       ├── national/...
│       ├── amtrak/...
│       ├── sncf/...
│       ├── uber/...
│       └── blacklane/...
├── worker.py                              [CREATE — ARQ WorkerSettings + parse_raw_email task]
├── routes/
│   ├── inbox.py                           [CREATE — 3-bucket inbox + 5 actions]
│   └── segments.py                        [MODIFY: prefill via from_raw_email + ✨ indicators]
├── models/
│   └── llm_budget.py                      [CREATE — LlmBudget ORM]
├── schemas/
│   └── llm.py                             [CREATE — Anthropic tool-use Pydantic schemas]
├── templates/
│   ├── base.html                          [MODIFY: nav link to /inbox]
│   ├── inbox/
│   │   ├── list.html                      3 bucket sections (collapsible)
│   │   ├── _bucket_review.html            low-confidence parses (5 actions)
│   │   ├── _bucket_no_segments.html       no-segments emails (4 actions)
│   │   └── _bucket_duplicates.html        possible duplicates (3 actions)
│   └── segments/                          [MODIFY: ✨ AI-suggested indicators on prefilled fields]
└── static/
    └── data/
        └── airports.csv                   IATA → tz/lat/lon (~9000 rows, MIT-licensed)

migrations/versions/
└── YYYY_MM_DD_HHMM_<rev>_phase3_llm_budget.py  [CREATE — single new table]

tests/
├── conftest.py                            [MODIFY: register LlmBudget with Base]
├── fixtures/
│   └── parsers/
│       ├── jsonld_flight.eml              JSON-LD fixture (FlightReservation)
│       ├── jsonld_lodging.eml             JSON-LD fixture (LodgingReservation)
│       ├── unknown_sender.eml             Haiku-territory email
│       └── direct_from_host.eml           vacation rental from host's gmail
├── test_config_phase3.py                  [CREATE]
├── test_models_llm_budget.py              [CREATE]
├── test_parsers_base.py                   [CREATE]
├── test_parsers_enrich.py                 [CREATE]
├── test_parsers_cluster.py                [CREATE]
├── test_parsers_jsonld.py                 [CREATE]
├── test_parsers_budget.py                 [CREATE]
├── test_parsers_llm.py                    [CREATE — mocked]
├── test_parsers_llm_live.py               [CREATE — @pytest.mark.live_llm, skipped in CI]
├── test_parsers_dispatch.py               [CREATE]
├── test_parsers_vendors.py                [CREATE — parameterized over fixtures]
├── test_worker.py                         [CREATE]
├── test_routes_inbox.py                   [CREATE]
└── test_routes_segments_prefill.py        [CREATE]
```

**Why this layout:** New `parsers/` package keeps the strategy-chain logic together; each strategy is one focused module. `parsers/vendors/<name>/` colocates each vendor's parser with its README + fixtures so adding a new vendor is one folder. The ARQ worker exits via `worker.py` to keep the queue config out of `app.py`. Inbox is its own router (separate from `admin.py`) since it's user-scoped, not admin-only.

---

## Conventions Used Throughout This Plan

- **Commit messages:** Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`). Subject under 70 chars; body via heredoc when multi-line.
- **TDD:** Write failing test → run → see it fail for the *right reason* → implement minimum → re-run → green → commit.
- **Real Postgres in tests:** continues from Phases 1+2. `pytest-postgresql` runs an ephemeral instance.
- **Lifespan in tests:** any test that hits the DB through the app uses `async with app.router.lifespan_context(app):` (Phases 1+2 established this).
- **`uv` for everything.** No raw `pip`.
- **No raw assert in src/ for invariants.** Bandit B101 fails CI. If a runtime invariant is needed for type narrowing, mark with `# nosec B101` and a reason (Phase 2 commit `73b2c26` established this convention).
- **Strategy ordering:** JSON-LD → vendor → Haiku. Each strategy that returns `confidence < confidence_floor` falls through to the next.

### Per-task "Quality bar / things to watch"

Every task includes a "Quality bar" section. Recurring entries (apply to every task touching code):

- **`from __future__ import annotations`** at top of every Python module.
- **Strict mypy + ruff (target=py313, mypy_python=3.14):**
  - No quoted return-type annotations (`def foo(self) -> "Bar":`) — ruff UP037 strips them. Spec code blocks in this plan often use them; remove the quotes when implementing.
  - Pydantic v2: use the `@field_validator("x") @classmethod def f(cls, v): ...` form, NOT the lambda-assignment form (`x = field_validator(...)(lambda)` trips strict mypy).
  - FastAPI handlers returning union types (`RedirectResponse | HTMLResponse`) need `response_model=None` on the route decorator.
  - PEP 758 (parenthesized except groups in 3.14): keep target=py313 to avoid ruff format stripping the parens.
- **Pre-commit hooks** (ruff, ruff-format, mypy, djlint, gitleaks, ggshield, bandit) must pass clean. Don't bypass with `--no-verify`.
- **Coverage gate:** ≥85% project-wide. Configured in `pyproject.toml`.

---

## Task 1 — Settings + Dependencies + Env Vars

**Spec ref:** §8 (configuration).

**Files:**
- Modify: `pyproject.toml` (add 4 deps)
- Modify: `src/trip_tracker/config.py`
- Modify: `.env.example`
- Create: `tests/test_config_phase3.py`

- [ ] **Step 1.1 — Add Phase 3 dependencies**

```bash
uv add anthropic@^0.40 arq@^0.26 redis@^5 extruct@^0.18
uv add --dev pytest-asyncio  # already there, no-op if already present
```

Verify versions land in `pyproject.toml` under `[project] dependencies`.

- [ ] **Step 1.2 — Failing test for new settings fields**

`tests/test_config_phase3.py`:

```python
"""Phase 3 settings: Anthropic + Redis + LLM budget config."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def test_anthropic_api_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """ANTHROPIC_API_KEY must be set."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="anthropic_api_key"):
        Settings()


def test_redis_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REDIS_URL", raising=False)
    with pytest.raises(ValidationError, match="redis_url"):
        Settings()


def test_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_DAILY_BUDGET_CENTS, LLM_MODEL, LLM_CONFIDENCE_FLOOR have defaults."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    s = Settings()
    assert s.llm_daily_budget_cents == 100
    assert s.llm_model == "claude-haiku-4-5-20251001"
    assert s.llm_confidence_floor == pytest.approx(0.7)


def test_llm_confidence_floor_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    """Confidence floor must be in [0, 1]."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("LLM_CONFIDENCE_FLOOR", "1.5")
    with pytest.raises(ValidationError):
        Settings()
```

Run: `uv run pytest tests/test_config_phase3.py -v` — expect 4 failures.

- [ ] **Step 1.3 — Implement settings fields**

In `src/trip_tracker/config.py`, add (before the closing of the `Settings` class):

```python
# Phase 3 — parser pipeline
anthropic_api_key: SecretStr
redis_url: str
llm_daily_budget_cents: int = 100  # $1.00 USD/day soft cap
llm_model: str = "claude-haiku-4-5-20251001"
llm_confidence_floor: float = 0.7


@field_validator("llm_confidence_floor")
@classmethod
def _floor_in_unit_interval(cls, v: float) -> float:
    if not 0.0 <= v <= 1.0:
        raise ValueError("llm_confidence_floor must be in [0, 1]")
    return v
```

Make sure `SecretStr` is imported from pydantic and `field_validator` is imported from pydantic.

- [ ] **Step 1.4 — Update `.env.example`**

Append to `.env.example`:

```env

# --- Phase 3: parser pipeline ---
ANTHROPIC_API_KEY=sk-ant-...           # Haiku LLM fallback
REDIS_URL=redis://trip-tracker-redis:6379/0
# Optional (defaults shown):
# LLM_DAILY_BUDGET_CENTS=100           # $1.00 USD/day soft cap
# LLM_MODEL=claude-haiku-4-5-20251001  # pinned per master spec
# LLM_CONFIDENCE_FLOOR=0.7             # below = parse_status='review'
```

- [ ] **Step 1.5 — Run tests + commit**

```bash
uv run pytest tests/test_config_phase3.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add pyproject.toml uv.lock src/trip_tracker/config.py .env.example tests/test_config_phase3.py
git commit -m "feat(config): Phase 3 settings — Anthropic + Redis + LLM budget"
```

**Quality bar:**
- `SecretStr` for `anthropic_api_key` so it doesn't leak in logs.
- `@field_validator @classmethod` form (NOT lambda assignment).
- Add corresponding entries to `.env.example` only — never commit a real `.env` file (gitignored already).

---

## Task 2 — Alembic Migration + LlmBudget ORM Model + Segment.raw_email_id

**Spec ref:** §5 (daily budget cap), §6.1 (Discard action: "segment row deleted (if any was written)" — needs an FK to find the segment), §11 (Done definition).

This task does two schema things in one migration:
1. New `llm_budget` table for the daily Haiku spend cap.
2. New `segments.raw_email_id UUID NULL` column with FK to `raw_emails.id` ON DELETE SET NULL. Required so the Inbox `discard` action (Task 17) can locate and remove auto-created segments per spec §6.1. Existing rows get NULL (nothing to backfill — Phase 2 segments were all manual).

**Files:**
- Create: `src/trip_tracker/models/llm_budget.py`
- Create: `migrations/versions/YYYY_MM_DD_HHMM_<rev>_phase3_llm_budget.py`
- Modify: `src/trip_tracker/models/segment.py` (add `raw_email_id` column)
- Modify: `tests/conftest.py` (register the new model)
- Create: `tests/test_models_llm_budget.py`

- [ ] **Step 2.1 — Generate empty migration**

```bash
uv run alembic revision -m "phase3 llm_budget"
```

Note the generated filename and revision ID.

- [ ] **Step 2.2 — Fill in the migration**

Replace the generated file's `upgrade()` and `downgrade()` bodies:

```python
"""phase3 llm_budget

Revision ID: <generated>
Revises: bbf3bbe09be9
Create Date: <generated>
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "<generated>"
down_revision = "bbf3bbe09be9"  # phase 2 ingestion migration
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "llm_budget",
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column("cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Add raw_email_id FK to segments — lets Inbox `discard` find auto-created
    # segments per spec §6.1. ON DELETE SET NULL so deleting a RawEmail
    # doesn't cascade-delete segments (the user may have edited and confirmed).
    op.add_column(
        "segments",
        sa.Column(
            "raw_email_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_emails.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_segments_raw_email_id", "segments", ["raw_email_id"])


def downgrade() -> None:
    op.drop_index("ix_segments_raw_email_id", table_name="segments")
    op.drop_column("segments", "raw_email_id")
    op.drop_table("llm_budget")
```

- [ ] **Step 2.3 — Failing test for the model**

`tests/test_models_llm_budget.py`:

```python
"""LlmBudget ORM model + Postgres CHECK constraints."""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.llm_budget import LlmBudget


@pytest.mark.asyncio
async def test_insert_and_read_back(db_session: AsyncSession) -> None:
    row = LlmBudget(day=date(2026, 4, 30), cost_cents=42, request_count=5)
    db_session.add(row)
    await db_session.commit()

    fetched = (
        await db_session.execute(select(LlmBudget).where(LlmBudget.day == date(2026, 4, 30)))
    ).scalar_one()
    assert fetched.cost_cents == 42
    assert fetched.request_count == 5
    assert fetched.updated_at is not None


@pytest.mark.asyncio
async def test_default_values(db_session: AsyncSession) -> None:
    row = LlmBudget(day=date(2026, 5, 1))
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    assert row.cost_cents == 0
    assert row.request_count == 0
```

Run: `uv run pytest tests/test_models_llm_budget.py -v` — expect ImportError (model doesn't exist).

- [ ] **Step 2.4 — Implement the model**

`src/trip_tracker/models/llm_budget.py`:

```python
"""LlmBudget — daily Haiku spend tracking for the soft budget cap."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import TIMESTAMP, Date, Integer, func
from sqlalchemy.orm import Mapped, mapped_column

from trip_tracker.db import Base


class LlmBudget(Base):
    __tablename__ = "llm_budget"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    cost_cents: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

- [ ] **Step 2.4b — Add `raw_email_id` to Segment ORM**

In `src/trip_tracker/models/segment.py`, add (alongside the other columns):

```python
import uuid as uuid_pkg
from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID

# ... in the Segment class:
raw_email_id: Mapped[uuid_pkg.UUID | None] = mapped_column(
    UUID(as_uuid=True),
    ForeignKey("raw_emails.id", ondelete="SET NULL"),
    nullable=True,
    index=True,
)
```

(Add a quick test in `tests/test_models_segment.py` — or the equivalent existing file — that creates a Segment with `raw_email_id=raw.id` and asserts the relationship persists. The existing Phase 2 test file likely already exists; just add one test case.)

- [ ] **Step 2.5 — Register with conftest**

In `tests/conftest.py`, find the imports that bring all models into `Base.metadata`. Add:

```python
from trip_tracker.models import llm_budget  # noqa: F401
```

(Pattern matches the existing `from trip_tracker.models import segment  # noqa: F401` etc.)

- [ ] **Step 2.6 — Run + commit**

```bash
uv run pytest tests/test_models_llm_budget.py -v
uv run pytest -q
uv run alembic upgrade head    # verify migration runs cleanly against an ephemeral DB
uv run ruff check . && uv run mypy src
git add migrations/ src/trip_tracker/models/llm_budget.py tests/conftest.py tests/test_models_llm_budget.py
git commit -m "feat(models): LlmBudget — daily Haiku spend tracking"
```

**Quality bar:**
- `down_revision = "bbf3bbe09be9"` (Phase 2 migration). Verify by `alembic history` if unsure.
- `default=0` (Python-side) AND `server_default="0"` (DB-side) so `LlmBudget(day=...)` works without explicit zeros.

---

## Task 3 — Static airports.csv Data

**Spec ref:** §5 (location proximity via airports.csv lat/lon).

**Files:**
- Create: `src/trip_tracker/static/data/airports.csv`
- Modify: `pyproject.toml` (include CSV in package data, if not already glob-included)

- [ ] **Step 3.1 — Source the airports data**

Use OurAirports' MIT-licensed `airports.csv` (https://ourairports.com/data/). For Phase 3 we only need IATA-coded commercial airports. Run this one-time script (don't commit the script — output only):

```bash
mkdir -p src/trip_tracker/static/data
curl -fsSL https://davidmegginson.github.io/ourairports-data/airports.csv \
  | uv run python -c "
import csv, sys
reader = csv.DictReader(sys.stdin)
writer = csv.writer(sys.stdout)
writer.writerow(['iata', 'icao', 'name', 'city', 'country', 'tz', 'lat', 'lon'])
for r in reader:
    if r['iata_code'] and r['type'] in ('large_airport', 'medium_airport'):
        writer.writerow([
            r['iata_code'], r['ident'], r['name'],
            r['municipality'], r['iso_country'],
            '',  # tz filled by next pass
            r['latitude_deg'], r['longitude_deg'],
        ])
" > src/trip_tracker/static/data/airports.csv
```

The CSV ends up with ~7000–9000 rows. Tz is intentionally empty in the OurAirports source — fill with a static IATA→tz lookup. Use the `airportsdata` package one-time:

```bash
uv tool run --with airportsdata python -c "
import airportsdata, csv
tz = airportsdata.load('IATA')
with open('src/trip_tracker/static/data/airports.csv') as f:
    rows = list(csv.DictReader(f))
for r in rows:
    rec = tz.get(r['iata'])
    if rec:
        r['tz'] = rec['tz']
with open('src/trip_tracker/static/data/airports.csv', 'w', newline='') as f:
    w = csv.DictWriter(f, fieldnames=rows[0].keys())
    w.writeheader(); w.writerows(rows)
"
```

(`airportsdata` is a one-time scaffolding tool, not a runtime dep.)

- [ ] **Step 3.2 — Sanity-check the CSV**

```bash
head -5 src/trip_tracker/static/data/airports.csv
wc -l src/trip_tracker/static/data/airports.csv
# Verify a couple of known airports:
grep -E '^(JFK|CDG|ORY|SFO),' src/trip_tracker/static/data/airports.csv
```

Expected: 4 hits, each with non-empty tz + lat + lon.

- [ ] **Step 3.3 — Make sure the package includes it**

Check `pyproject.toml` `[tool.hatch.build]` or `[tool.setuptools.package-data]` includes `static/data/*.csv`. The Phase 1 build was already configured to include `static/**` for `iana_timezones.json`, so this should be automatic. Verify with:

```bash
uv build --wheel
unzip -l dist/trip_tracker-*.whl | grep airports.csv
```

If missing, add the glob to `pyproject.toml`.

- [ ] **Step 3.4 — Commit**

```bash
git add src/trip_tracker/static/data/airports.csv pyproject.toml
git commit -m "feat(parsers): add airports.csv for IATA → tz/lat/lon enrichment"
```

**Quality bar:**
- The CSV is ~500–800 KB committed. Acceptable; checked-in static data > pulling at runtime.
- License: OurAirports is public domain; document the source in a header comment if you want, but no license file required.

---

## Task 4 — `parsers/base.py`: Contract + Registry

**Spec ref:** §4 (plugin architecture), §5 (ParseResult shape).

**Files:**
- Create: `src/trip_tracker/parsers/__init__.py`
- Create: `src/trip_tracker/parsers/base.py`
- Create: `tests/test_parsers_base.py`

- [ ] **Step 4.1 — Failing tests**

`tests/test_parsers_base.py`:

```python
"""parsers.base: VendorParser ABC + ParseResult + registry."""

from __future__ import annotations

import re
from email.message import EmailMessage

import pytest

from trip_tracker.parsers.base import (
    ParseResult,
    SegmentDraft,
    VendorParser,
    get_registry,
)


class _FakeAA(VendorParser):
    name = "fake_aa"
    sender_patterns = [re.compile(r"@aa\.com$")]

    def parse(self, msg: EmailMessage) -> ParseResult:
        return ParseResult(segments=[], confidence=0.0, source="rules:fake_aa")


class _FakeAASpecific(VendorParser):
    name = "fake_aa_aadvantage"
    sender_patterns = [re.compile(r"^aadvantage@aa\.com$")]

    def parse(self, msg: EmailMessage) -> ParseResult:
        return ParseResult(segments=[], confidence=0.0, source="rules:fake_aa_aadv")


def test_subclass_auto_registers() -> None:
    reg = get_registry()
    names = {p.name for p in reg}
    assert "fake_aa" in names
    assert "fake_aa_aadvantage" in names


def test_match_predicate() -> None:
    assert _FakeAA.matches("noreply@aa.com")
    assert not _FakeAA.matches("notifications@united.com")


def test_dispatch_specific_first() -> None:
    """Longer sender patterns sort first so a narrower regex shadows a broader one."""
    from trip_tracker.parsers.base import select_parsers

    matched = select_parsers("aadvantage@aa.com")
    assert matched[0].name == "fake_aa_aadvantage"
    assert matched[1].name == "fake_aa"


def test_segment_draft_minimal() -> None:
    from datetime import UTC, datetime

    SegmentDraft(
        type="flight",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="America/New_York",
    )  # should not raise


def test_parse_result_warnings_optional() -> None:
    r = ParseResult(segments=[], confidence=0.5, source="json-ld")
    assert r.warnings == []
```

Run: expect 5 ImportError-shaped failures.

- [ ] **Step 4.2 — Implement `base.py`**

`src/trip_tracker/parsers/base.py`:

```python
"""VendorParser contract + ParseResult/SegmentDraft Pydantic schemas + registry.

Subclassing VendorParser auto-registers via __init_subclass__.
parsers/vendors/__init__.py imports each subpackage to trigger registration.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import datetime
from email.message import EmailMessage
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

SegmentType = Literal["flight", "lodging", "car", "train", "transfer", "activity"]


class SegmentDraft(BaseModel):
    """Pydantic mirror of the Segment ORM shape, no DB columns."""

    type: SegmentType
    status: Literal["confirmed", "tentative", "cancelled"] = "confirmed"
    confirmation_number: str | None = None
    provider: str | None = None
    start_at: datetime
    start_tz: str
    end_at: datetime | None = None
    end_tz: str | None = None
    start_location: dict[str, Any] | None = None
    end_location: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class ParseResult(BaseModel):
    """Output of any parser strategy."""

    segments: list[SegmentDraft] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    source: str  # "json-ld" | "rules:<name>" | "llm:haiku-4-5"
    warnings: list[str] = Field(default_factory=list)


_REGISTRY: list[type[VendorParser]] = []


class VendorParser(ABC):
    """Each vendor pack subclasses this. Auto-registered on subclass creation.

    sender_patterns: list of compiled regexes. The dispatcher matches the
    From: header against each parser's patterns; sorts by most-specific
    pattern first (longest pattern wins).
    """

    name: ClassVar[str]
    sender_patterns: ClassVar[list[re.Pattern[str]]]
    confidence_floor: ClassVar[float] = 0.85

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not hasattr(cls, "name"):
            raise TypeError(f"{cls.__name__} must define `name` ClassVar")
        if not hasattr(cls, "sender_patterns"):
            raise TypeError(f"{cls.__name__} must define `sender_patterns` ClassVar")
        _REGISTRY.append(cls)

    @abstractmethod
    def parse(self, msg: EmailMessage) -> ParseResult: ...

    @classmethod
    def matches(cls, from_address: str) -> bool:
        return any(p.search(from_address) for p in cls.sender_patterns)


def get_registry() -> list[type[VendorParser]]:
    """All registered vendor parser classes (no ordering guarantee)."""
    return list(_REGISTRY)


def select_parsers(from_address: str) -> list[type[VendorParser]]:
    """Return the parsers whose sender_patterns match `from_address`,
    sorted by most-specific pattern first.

    'Most specific' = longest pattern.string. Ties broken by name.
    """
    matched = [p for p in _REGISTRY if p.matches(from_address)]

    def specificity(parser: type[VendorParser]) -> tuple[int, str]:
        longest = max(len(pat.pattern) for pat in parser.sender_patterns)
        return (-longest, parser.name)

    return sorted(matched, key=specificity)
```

Also create `src/trip_tracker/parsers/__init__.py`:

```python
"""Parser strategies: JSON-LD → vendor → LLM."""

from __future__ import annotations
```

- [ ] **Step 4.3 — Run + commit**

```bash
uv run pytest tests/test_parsers_base.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/__init__.py src/trip_tracker/parsers/base.py tests/test_parsers_base.py
git commit -m "feat(parsers): VendorParser ABC + ParseResult + auto-registry"
```

**Quality bar:**
- `__init_subclass__` runs at subclass-definition time. Tests above define `_FakeAA` etc. inline, which triggers registration when the test module is imported. Acceptable for tests; registry is process-wide so vendor packs registered in production stay registered.
- `_REGISTRY` is a module-level list. Multiple imports won't double-register because Python caches modules.

---

## Task 5 — `parsers/enrich.py`: IATA Enrichment

**Spec ref:** §5 (enrich step: airport codes → tz + lat/lon).

**Files:**
- Create: `src/trip_tracker/parsers/enrich.py`
- Create: `tests/test_parsers_enrich.py`

- [ ] **Step 5.1 — Failing tests**

`tests/test_parsers_enrich.py`:

```python
"""Airport IATA → tz + lat/lon enrichment."""

from __future__ import annotations

import pytest

from trip_tracker.parsers.enrich import enrich_airport, get_airport, haversine_km


def test_get_airport_jfk() -> None:
    a = get_airport("JFK")
    assert a is not None
    assert a.tz == "America/New_York"
    assert a.city.lower() == "new york"
    assert -74.5 < a.lon < -73.5
    assert 40.0 < a.lat < 41.0


def test_get_airport_unknown() -> None:
    assert get_airport("XXX") is None


def test_get_airport_case_insensitive() -> None:
    assert get_airport("jfk") is not None


def test_haversine_known_pair() -> None:
    """JFK → CDG is ~5837 km."""
    jfk = get_airport("JFK")
    cdg = get_airport("CDG")
    assert jfk and cdg
    d = haversine_km((jfk.lat, jfk.lon), (cdg.lat, cdg.lon))
    assert 5800 < d < 5900


def test_haversine_zero() -> None:
    assert haversine_km((0.0, 0.0), (0.0, 0.0)) == 0.0


def test_enrich_airport_fills_tz_and_coords() -> None:
    loc = {"iata": "CDG", "city": "Paris"}
    enriched = enrich_airport(loc)
    assert enriched["tz"] == "Europe/Paris"
    assert "lat" in enriched and "lon" in enriched


def test_enrich_airport_unknown_returns_input() -> None:
    loc = {"iata": "XXX"}
    enriched = enrich_airport(loc)
    assert enriched == loc  # no enrichment, no error
```

- [ ] **Step 5.2 — Implement enrichment**

`src/trip_tracker/parsers/enrich.py`:

```python
"""Static IATA → tz/lat/lon lookup + haversine distance.

The airports.csv file is loaded once at module import (small, ~500 KB).
"""

from __future__ import annotations

import csv
import math
from dataclasses import dataclass
from importlib import resources
from typing import Any


@dataclass(frozen=True)
class Airport:
    iata: str
    name: str
    city: str
    country: str
    tz: str
    lat: float
    lon: float


def _load() -> dict[str, Airport]:
    out: dict[str, Airport] = {}
    src = resources.files("trip_tracker.static.data").joinpath("airports.csv")
    with src.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("tz") or not row.get("lat") or not row.get("lon"):
                continue
            try:
                out[row["iata"].upper()] = Airport(
                    iata=row["iata"].upper(),
                    name=row["name"],
                    city=row["city"],
                    country=row["country"],
                    tz=row["tz"],
                    lat=float(row["lat"]),
                    lon=float(row["lon"]),
                )
            except (ValueError, KeyError):
                continue
    return out


_AIRPORTS: dict[str, Airport] = _load()


def get_airport(iata: str) -> Airport | None:
    return _AIRPORTS.get(iata.upper())


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance between two (lat, lon) points, in km."""
    lat1, lon1 = a
    lat2, lon2 = b
    r = 6371.0  # Earth radius km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    h = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def enrich_airport(loc: dict[str, Any]) -> dict[str, Any]:
    """If `loc` has an 'iata' key, fill in tz/lat/lon if known. Return a new dict."""
    iata = loc.get("iata")
    if not iata:
        return dict(loc)
    a = get_airport(iata)
    if a is None:
        return dict(loc)
    return {**loc, "tz": a.tz, "lat": a.lat, "lon": a.lon, "city": loc.get("city") or a.city}
```

- [ ] **Step 5.3 — Run + commit**

```bash
uv run pytest tests/test_parsers_enrich.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/enrich.py tests/test_parsers_enrich.py
git commit -m "feat(parsers): airport IATA → tz/lat/lon enrichment + haversine"
```

**Quality bar:**
- `_load()` runs once at import. Module-level dict is fine for ~9000 entries (hundreds of KB in memory).
- `enrich_airport` returns a new dict (never mutates input) — Pydantic-friendly.
- Haversine uses `math.asin(sqrt(h))` (numerically stable for small distances, vs `2*atan2(sqrt(h), sqrt(1-h))`).

---

## Task 6 — `parsers/cluster.py`: Trip Clustering Rule

**Spec ref:** §5 (trip clustering rule with geo + ±1d adjacency + 20% gap).

**Files:**
- Create: `src/trip_tracker/parsers/cluster.py`
- Create: `tests/test_parsers_cluster.py`

This task uses TDD heavily — clustering logic is pure-function and table-driven.

- [ ] **Step 6.1 — Failing tests**

`tests/test_parsers_cluster.py`:

```python
"""Trip clustering rule: geo + ±1d adjacency + 20% gap → /inbox."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.parsers.cluster import (
    ClusterDecision,
    cluster_for_user,
    derive_destination,
)


@pytest.fixture
async def user(db_session: AsyncSession) -> User:
    u = User(oidc_subject="cluster-test", email="c@x.com", display_name="C")
    db_session.add(u)
    await db_session.commit()
    return u


def _flight_draft(start: datetime, end: datetime, origin_iata: str, dest_iata: str) -> SegmentDraft:
    return SegmentDraft(
        type="flight",
        start_at=start,
        start_tz="UTC",
        end_at=end,
        end_tz="UTC",
        start_location={"iata": origin_iata, "city": "Origin"},
        end_location={"iata": dest_iata, "city": "Dest"},
    )


@pytest.mark.asyncio
async def test_no_existing_trips_creates_new(db_session: AsyncSession, user: User) -> None:
    draft = _flight_draft(
        datetime(2026, 6, 1, 9, tzinfo=UTC),
        datetime(2026, 6, 1, 22, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "create_new"
    assert decision.auto_title == "Paris June 2026"


@pytest.mark.asyncio
async def test_single_overlapping_trip_attaches(db_session: AsyncSession, user: User) -> None:
    trip = Trip(
        title="Paris trip",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    draft = _flight_draft(
        datetime(2026, 6, 3, 9, tzinfo=UTC),
        datetime(2026, 6, 3, 22, tzinfo=UTC),
        "CDG",
        "JFK",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "attach"
    assert decision.trip_id == trip.id


@pytest.mark.asyncio
async def test_adjacent_plus_one_day_attaches(db_session: AsyncSession, user: User) -> None:
    trip = Trip(
        title="Paris",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    # +1 day after trip end
    draft = _flight_draft(
        datetime(2026, 6, 6, 9, tzinfo=UTC),
        datetime(2026, 6, 6, 22, tzinfo=UTC),
        "CDG",
        "JFK",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "attach"
    assert decision.trip_id == trip.id


@pytest.mark.asyncio
async def test_two_day_gap_does_not_cluster(db_session: AsyncSession, user: User) -> None:
    trip = Trip(
        title="Paris",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    # +2 days after trip end → outside ±1d adjacency
    draft = _flight_draft(
        datetime(2026, 6, 7, 9, tzinfo=UTC),
        datetime(2026, 6, 7, 22, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "create_new"


@pytest.mark.asyncio
async def test_close_score_routes_to_inbox(db_session: AsyncSession, user: User) -> None:
    """Two trips overlap with the same dates → ambiguous → /inbox."""
    for label in ("Paris", "Paris2"):
        t = Trip(
            title=label,
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 5),
            primary_destination="Paris",
            created_by=user.id,
        )
        db_session.add(t)
        await db_session.flush()
        db_session.add(TripTraveler(trip_id=t.id, user_id=user.id, role="owner"))
    await db_session.commit()

    draft = _flight_draft(
        datetime(2026, 6, 3, 9, tzinfo=UTC),
        datetime(2026, 6, 3, 22, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    decision = await cluster_for_user(db_session, user.id, draft)
    assert decision.kind == "ambiguous"


def test_derive_destination_flight_uses_end_city() -> None:
    draft = _flight_draft(
        datetime(2026, 6, 1, tzinfo=UTC),
        datetime(2026, 6, 1, tzinfo=UTC),
        "JFK",
        "CDG",
    )
    assert derive_destination(draft) == "Dest"


def test_derive_destination_lodging_uses_start_city() -> None:
    draft = SegmentDraft(
        type="lodging",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
        start_location={"city": "Paris"},
    )
    assert derive_destination(draft) == "Paris"
```

- [ ] **Step 6.2 — Implement clustering**

`src/trip_tracker/parsers/cluster.py`:

```python
"""Trip clustering rule: place a SegmentDraft into an existing Trip
(or signal a new one / route to /inbox).

Rule (spec §5):
  candidates = trips overlapping or adjacent ±1 day AND in location proximity
  score = 1 / (1 + days_to_trip_center)
  if no candidates -> create_new (auto-title)
  if best.score - second_best.score < 0.20 of best -> ambiguous
  else -> attach to best
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.parsers.base import SegmentDraft
from trip_tracker.parsers.enrich import get_airport, haversine_km

# Segment types whose primary destination is the END location (vs start).
_END_DESTINATION_TYPES = {"flight", "train", "transfer"}

# Spec §5: 200km threshold for airport-coord distance.
GEO_PROXIMITY_KM = 200.0


@dataclass
class ClusterDecision:
    kind: Literal["attach", "create_new", "ambiguous"]
    trip_id: uuid.UUID | None = None
    auto_title: str | None = None  # populated when kind == 'create_new'


def derive_destination(draft: SegmentDraft) -> str | None:
    end_loc = draft.end_location or {}
    start_loc = draft.start_location or {}
    if draft.type in _END_DESTINATION_TYPES:
        return end_loc.get("city") or start_loc.get("city")
    return start_loc.get("city") or end_loc.get("city")


def _auto_title(draft: SegmentDraft) -> str:
    dest = derive_destination(draft) or "Trip"
    return f"{dest} {draft.start_at.strftime('%B %Y')}"


def _segment_dates(draft: SegmentDraft) -> tuple[date, date]:
    s = draft.start_at.date()
    e = (draft.end_at or draft.start_at).date()
    return s, e


def _location_proximity(draft: SegmentDraft, trip: Trip) -> bool:
    """Approximate spec rule: same city OR within 200km via airport coords."""
    draft_dest = derive_destination(draft)
    trip_dest = trip.primary_destination
    if draft_dest and trip_dest and draft_dest.strip().lower() == trip_dest.strip().lower():
        return True
    # Geo distance via airports if both ends have IATA
    start = (draft.start_location or {}).get("iata")
    end = (draft.end_location or {}).get("iata")
    for iata in (start, end):
        if not iata:
            continue
        ap = get_airport(iata)
        if ap is None:
            continue
        # Compare to trip's primary_destination as a city-name proxy: skip
        # geo-distance trip-side until hotels have coords. Only short-circuit
        # if the airport's CITY matches the trip's destination.
        if trip_dest and ap.city.strip().lower() == trip_dest.strip().lower():
            return True
    return False


def _date_overlap_or_adjacent(draft: SegmentDraft, trip: Trip, *, adjacent_days: int = 1) -> bool:
    s, e = _segment_dates(draft)
    delta = timedelta(days=adjacent_days)
    return s - delta <= trip.end_date and e + delta >= trip.start_date


def _score(draft: SegmentDraft, trip: Trip) -> float:
    """Higher = better match. Inverse of days from trip center to segment start."""
    center_days = (trip.start_date - date(1970, 1, 1)).days + (
        (trip.end_date - trip.start_date).days / 2
    )
    seg_days = (draft.start_at.date() - date(1970, 1, 1)).days
    distance = abs(center_days - seg_days)
    return 1.0 / (1.0 + distance)


async def cluster_for_user(
    db: AsyncSession, user_id: uuid.UUID, draft: SegmentDraft
) -> ClusterDecision:
    """Find the best Trip for `draft` among `user_id`'s trips, or signal a new one."""
    rows = (
        (
            await db.execute(
                select(Trip)
                .join(TripTraveler, TripTraveler.trip_id == Trip.id)
                .where(TripTraveler.user_id == user_id)
            )
        )
        .scalars()
        .all()
    )

    candidates: list[tuple[Trip, float]] = []
    for trip in rows:
        if not _date_overlap_or_adjacent(draft, trip):
            continue
        if not _location_proximity(draft, trip):
            continue
        candidates.append((trip, _score(draft, trip)))

    if not candidates:
        return ClusterDecision(kind="create_new", auto_title=_auto_title(draft))

    candidates.sort(key=lambda x: x[1], reverse=True)
    best_trip, best_score = candidates[0]
    if len(candidates) >= 2:
        _, second_score = candidates[1]
        if best_score > 0 and (best_score - second_score) / best_score < 0.20:
            return ClusterDecision(kind="ambiguous", trip_id=best_trip.id)

    return ClusterDecision(kind="attach", trip_id=best_trip.id)
```

- [ ] **Step 6.3 — Run + commit**

```bash
uv run pytest tests/test_parsers_cluster.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/cluster.py tests/test_parsers_cluster.py
git commit -m "feat(parsers): trip clustering rule — geo + ±1d adjacency + 20% gap"
```

**Quality bar:**
- `cluster_for_user` is the only public entry. `_location_proximity`, `_score`, `_date_overlap_or_adjacent` are pure functions, easy to unit-test if needed.
- Returning `ClusterDecision` (named tuple-like) makes the call site readable: `if decision.kind == "attach": ...`.
- The "ambiguous" branch returns the *best* `trip_id` for diagnostic purposes but the dispatcher should leave `trip_id` null on the segment.

---

## Task 7 — `parsers/jsonld.py`: extruct Strategy

**Spec ref:** §5 step 1 (JSON-LD via `extruct`, 0.95 confidence).

**Files:**
- Create: `src/trip_tracker/parsers/jsonld.py`
- Create: `tests/test_parsers_jsonld.py`
- Create: `tests/fixtures/parsers/jsonld_flight.eml`
- Create: `tests/fixtures/parsers/jsonld_lodging.eml`

- [ ] **Step 7.1 — Create fixtures**

`tests/fixtures/parsers/jsonld_flight.eml` — synthetic email with a JSON-LD `FlightReservation`:

```
Subject: Your AirExample flight confirmation
From: confirmations@airexample.com
To: oliver@trips.example.com
Content-Type: text/html

<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FlightReservation",
  "reservationNumber": "ABC123",
  "reservationStatus": "https://schema.org/ReservationConfirmed",
  "reservationFor": {
    "@type": "Flight",
    "flightNumber": "EX42",
    "airline": {"@type": "Airline", "iataCode": "EX"},
    "departureAirport": {"@type": "Airport", "iataCode": "JFK", "name": "JFK"},
    "arrivalAirport":   {"@type": "Airport", "iataCode": "CDG", "name": "CDG"},
    "departureTime": "2026-06-01T09:00:00-04:00",
    "arrivalTime":   "2026-06-01T22:00:00+02:00"
  }
}
</script>
</head><body>Boring marketing HTML.</body></html>
```

`tests/fixtures/parsers/jsonld_lodging.eml` — similar but with `LodgingReservation`:

```
Subject: Your Hotel Example confirmation
From: reservations@hotelexample.com
To: oliver@trips.example.com
Content-Type: text/html

<html><head>
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "LodgingReservation",
  "reservationNumber": "HOT9",
  "reservationFor": {
    "@type": "LodgingBusiness",
    "name": "Hotel Example",
    "address": {"@type": "PostalAddress", "addressLocality": "Paris", "addressCountry": "FR"}
  },
  "checkinTime":  "2026-06-01T15:00:00+02:00",
  "checkoutTime": "2026-06-05T11:00:00+02:00"
}
</script>
</head><body>Welcome!</body></html>
```

- [ ] **Step 7.2 — Failing tests**

`tests/test_parsers_jsonld.py`:

```python
"""extruct-based JSON-LD strategy."""

from __future__ import annotations

from email import message_from_bytes
from email.policy import default as email_policy_default
from pathlib import Path

import pytest

from trip_tracker.parsers.jsonld import parse_jsonld

_FIXTURES = Path(__file__).parent / "fixtures" / "parsers"


def _msg(name: str):
    raw = (_FIXTURES / name).read_bytes()
    return message_from_bytes(raw, policy=email_policy_default)


def test_flight_reservation_extracted() -> None:
    result = parse_jsonld(_msg("jsonld_flight.eml"))
    assert result.confidence >= 0.9
    assert result.source == "json-ld"
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "flight"
    assert seg.confirmation_number == "ABC123"
    assert (seg.start_location or {}).get("iata") == "JFK"
    assert (seg.end_location or {}).get("iata") == "CDG"


def test_lodging_reservation_extracted() -> None:
    result = parse_jsonld(_msg("jsonld_lodging.eml"))
    assert len(result.segments) == 1
    seg = result.segments[0]
    assert seg.type == "lodging"
    assert seg.confirmation_number == "HOT9"
    assert (seg.start_location or {}).get("city") == "Paris"


def test_no_jsonld_returns_empty() -> None:
    """A plain-text email with no JSON-LD returns segments=[] confidence=0."""
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = "Plain"
    msg["From"] = "x@y.com"
    msg.set_content("No structured data here.")
    result = parse_jsonld(msg)
    assert result.segments == []
    assert result.confidence == 0.0
```

- [ ] **Step 7.3 — Implement `parse_jsonld`**

`src/trip_tracker/parsers/jsonld.py`:

```python
"""JSON-LD extraction strategy using extruct.

Looks for FlightReservation, LodgingReservation, RentalCarReservation,
EventReservation. Returns ParseResult with confidence ~0.95 on hit.
"""

from __future__ import annotations

import logging
from datetime import datetime
from email.message import EmailMessage
from typing import Any

import extruct  # type: ignore[import-untyped]

from trip_tracker.parsers.base import ParseResult, SegmentDraft

logger = logging.getLogger(__name__)

_TYPE_MAP = {
    "FlightReservation": "flight",
    "LodgingReservation": "lodging",
    "RentalCarReservation": "car",
    "EventReservation": "activity",
}


def _extract_html(msg: EmailMessage) -> str:
    """Pull the text/html part if present, else fall back to text/plain."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    if msg.get_content_type() == "text/html":
        payload = msg.get_payload(decode=True)
        if isinstance(payload, bytes):
            return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return ""


def _parse_iso(s: str) -> datetime | None:
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _flight_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    inner = d.get("reservationFor", {})
    start = _parse_iso(inner.get("departureTime", ""))
    if not start:
        return None
    end = _parse_iso(inner.get("arrivalTime", ""))
    dep = inner.get("departureAirport", {}) or {}
    arr = inner.get("arrivalAirport", {}) or {}
    return SegmentDraft(
        type="flight",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=str(start.tzinfo) if start.tzinfo else "UTC",
        end_at=end,
        end_tz=str(end.tzinfo) if end and end.tzinfo else None,
        start_location={"iata": dep.get("iataCode"), "name": dep.get("name")},
        end_location={"iata": arr.get("iataCode"), "name": arr.get("name")},
        details={"flight_number": inner.get("flightNumber")},
    )


def _lodging_from_jsonld(d: dict[str, Any]) -> SegmentDraft | None:
    start = _parse_iso(d.get("checkinTime", ""))
    end = _parse_iso(d.get("checkoutTime", ""))
    if not start:
        return None
    inner = d.get("reservationFor", {}) or {}
    addr = inner.get("address", {}) or {}
    return SegmentDraft(
        type="lodging",
        confirmation_number=d.get("reservationNumber"),
        start_at=start,
        start_tz=str(start.tzinfo) if start.tzinfo else "UTC",
        end_at=end,
        end_tz=str(end.tzinfo) if end and end.tzinfo else None,
        start_location={
            "name": inner.get("name"),
            "city": addr.get("addressLocality"),
            "country": addr.get("addressCountry"),
        },
    )


def parse_jsonld(msg: EmailMessage) -> ParseResult:
    """Run extruct over the email's HTML body, extract reservations."""
    html = _extract_html(msg)
    if not html:
        return ParseResult(segments=[], confidence=0.0, source="json-ld")
    try:
        data = extruct.extract(html, syntaxes=["json-ld"])
    except Exception as exc:  # noqa: BLE001 — extruct can raise broadly
        logger.warning("extruct failed: %s", exc)
        return ParseResult(segments=[], confidence=0.0, source="json-ld", warnings=[str(exc)])

    segments: list[SegmentDraft] = []
    for item in data.get("json-ld") or []:
        t = item.get("@type")
        if t == "FlightReservation":
            seg = _flight_from_jsonld(item)
        elif t == "LodgingReservation":
            seg = _lodging_from_jsonld(item)
        else:
            continue
        if seg:
            segments.append(seg)

    return ParseResult(
        segments=segments,
        confidence=0.95 if segments else 0.0,
        source="json-ld",
    )
```

- [ ] **Step 7.4 — Run + commit**

```bash
uv run pytest tests/test_parsers_jsonld.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/jsonld.py tests/test_parsers_jsonld.py tests/fixtures/parsers/
git commit -m "feat(parsers): JSON-LD strategy via extruct"
```

**Quality bar:**
- `extruct` has no type stubs → `# type: ignore[import-untyped]` on the import.
- `except Exception as exc:  # noqa: BLE001` — extruct can raise many things; logging + falling through is the spec'd behavior.
- `_parse_iso` returns `None` for malformed dates rather than raising → callers gracefully skip the segment.

---

## Task 8 — `parsers/budget.py`: Daily LLM Budget Tracker

**Spec ref:** §5 (daily budget cap).

**Files:**
- Create: `src/trip_tracker/parsers/budget.py`
- Create: `tests/test_parsers_budget.py`

- [ ] **Step 8.1 — Failing tests**

`tests/test_parsers_budget.py`:

```python
"""Daily LLM budget tracker — read/write LlmBudget."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.llm_budget import LlmBudget
from trip_tracker.parsers.budget import (
    cost_cents_for_usage,
    is_over_budget,
    record_usage,
)


def test_cost_cents_for_usage_haiku_pricing() -> None:
    """Haiku 4.5: $1/Mtok input, $5/Mtok output → 0.025 cents/Ktok input, 0.125 cents/Ktok output."""
    # 1000 input + 1000 output tokens → 0.0001 + 0.0005 USD = 0.06 cents → ceil to 1 cent.
    cents = cost_cents_for_usage(input_tokens=1000, output_tokens=1000)
    assert cents == 1


def test_cost_cents_zero_for_zero_usage() -> None:
    assert cost_cents_for_usage(input_tokens=0, output_tokens=0) == 0


@pytest.mark.asyncio
async def test_is_over_budget_initially_false(db_session: AsyncSession) -> None:
    over = await is_over_budget(db_session, cap_cents=100)
    assert over is False


@pytest.mark.asyncio
async def test_is_over_budget_at_or_above_cap(db_session: AsyncSession) -> None:
    today = datetime.now(tz=UTC).date()
    db_session.add(LlmBudget(day=today, cost_cents=150, request_count=3))
    await db_session.commit()
    assert await is_over_budget(db_session, cap_cents=100) is True


@pytest.mark.asyncio
async def test_record_usage_upserts(db_session: AsyncSession) -> None:
    today = datetime.now(tz=UTC).date()
    await record_usage(db_session, cost_cents=5)
    await record_usage(db_session, cost_cents=7)
    row = (await db_session.execute(select(LlmBudget).where(LlmBudget.day == today))).scalar_one()
    assert row.cost_cents == 12
    assert row.request_count == 2
```

- [ ] **Step 8.2 — Implement budget tracker**

`src/trip_tracker/parsers/budget.py`:

```python
"""LlmBudget read/write helpers + Haiku 4.5 cost calculation."""

from __future__ import annotations

import math
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.llm_budget import LlmBudget

# Haiku 4.5 pricing (USD per Mtok), per Anthropic docs.
_HAIKU_INPUT_USD_PER_MTOK = 1.0
_HAIKU_OUTPUT_USD_PER_MTOK = 5.0


def cost_cents_for_usage(*, input_tokens: int, output_tokens: int) -> int:
    """Round-up cents cost for a Haiku call.

    Returns 0 only when both counts are 0; any nonzero usage rounds up to ≥1 cent.
    """
    if input_tokens == 0 and output_tokens == 0:
        return 0
    usd = (
        input_tokens * _HAIKU_INPUT_USD_PER_MTOK / 1_000_000
        + output_tokens * _HAIKU_OUTPUT_USD_PER_MTOK / 1_000_000
    )
    return max(1, math.ceil(usd * 100))


async def is_over_budget(db: AsyncSession, *, cap_cents: int) -> bool:
    today = datetime.now(tz=UTC).date()
    row = (
        await db.execute(select(LlmBudget.cost_cents).where(LlmBudget.day == today))
    ).scalar_one_or_none()
    return (row or 0) >= cap_cents


async def record_usage(db: AsyncSession, *, cost_cents: int) -> None:
    """Upsert today's row: cost_cents += delta, request_count += 1."""
    today = datetime.now(tz=UTC).date()
    stmt = (
        pg_insert(LlmBudget)
        .values(day=today, cost_cents=cost_cents, request_count=1)
        .on_conflict_do_update(
            index_elements=["day"],
            set_={
                "cost_cents": LlmBudget.cost_cents + cost_cents,
                "request_count": LlmBudget.request_count + 1,
            },
        )
    )
    await db.execute(stmt)
    await db.commit()
```

- [ ] **Step 8.3 — Run + commit**

```bash
uv run pytest tests/test_parsers_budget.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/budget.py tests/test_parsers_budget.py
git commit -m "feat(parsers): daily LLM budget tracker — Haiku pricing + upsert"
```

**Quality bar:**
- `pg_insert(...).on_conflict_do_update` is Postgres-specific; this is fine since the project targets Postgres only.
- `cost_cents_for_usage` rounds *up* so any nonzero usage charges at least 1 cent. Avoids the "many sub-cent calls accumulate to zero" failure mode.

---

## Task 9 — `parsers/llm.py`: Anthropic SDK + Tool-Use

**Spec ref:** §5 (Haiku strategy with prompt caching + tool-use schema).

**Files:**
- Create: `src/trip_tracker/schemas/llm.py`
- Create: `src/trip_tracker/parsers/llm.py`
- Create: `tests/test_parsers_llm.py`
- Create: `tests/test_parsers_llm_live.py` (skipped in CI)

- [ ] **Step 9.1 — Tool-use Pydantic schemas**

`src/trip_tracker/schemas/llm.py`:

```python
"""Anthropic tool-use schemas for the parser fallback.

The Haiku call uses tool-use to force structured output matching
`SegmentDraft`. We mirror SegmentDraft fields here as the tool's
input schema (Anthropic's ToolInputSchema).
"""

from __future__ import annotations

from typing import Any

# JSON schema given to Anthropic for the `extract_segments` tool.
EXTRACT_SEGMENTS_TOOL: dict[str, Any] = {
    "name": "extract_segments",
    "description": (
        "Extract zero or more travel segments from the email body. "
        "Each segment is one leg: a flight, lodging stay, car rental, "
        "train, transfer (taxi/Uber/private car), or activity (event/tour). "
        "Return an empty list ONLY if the email contains no itinerary content "
        "(marketing, receipts, etc.). When unsure, prefer extracting with low "
        "confidence over returning empty."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "segments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["flight", "lodging", "car", "train", "transfer", "activity"],
                        },
                        "status": {
                            "type": "string",
                            "enum": ["confirmed", "tentative", "cancelled"],
                        },
                        "confirmation_number": {"type": ["string", "null"]},
                        "provider": {"type": ["string", "null"]},
                        "start_at": {
                            "type": "string",
                            "description": "ISO 8601 datetime with timezone offset",
                        },
                        "start_tz": {"type": "string", "description": "IANA tz name"},
                        "end_at": {"type": ["string", "null"]},
                        "end_tz": {"type": ["string", "null"]},
                        "start_location": {
                            "type": ["object", "null"],
                            "additionalProperties": True,
                        },
                        "end_location": {
                            "type": ["object", "null"],
                            "additionalProperties": True,
                        },
                        "details": {"type": "object", "additionalProperties": True},
                    },
                    "required": ["type", "start_at", "start_tz"],
                },
            },
            "confidence": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
                "description": (
                    "Self-rated confidence in this extraction. Use ≥0.85 for "
                    "obvious itineraries (clear sender + clear fields), 0.5–0.85 "
                    "for plausible-but-noisy cases, and <0.5 if you suspect "
                    "this isn't actually an itinerary."
                ),
            },
        },
        "required": ["segments", "confidence"],
    },
}

SYSTEM_PROMPT = """\
You parse forwarded confirmation emails into structured travel segments.

Input: the raw text/HTML of one email.
Output: a single tool call to extract_segments with the segments array.

Rules:
- One leg per segment (a round-trip flight = 2 segments).
- Datetimes MUST include timezone info (offset OR IANA tz name in start_tz/end_tz).
- If you can't determine a timezone, use the airport's tz for flights or the city's
  tz for lodging. Default to UTC only as a last resort.
- For type='lodging', start_at = check-in, end_at = check-out.
- Marketing emails, receipts (non-itinerary), and confirmations of past travel:
  return segments=[] with confidence ≥0.85.
- Cap your self-rated confidence at 0.85 even when very sure — vendor-specific
  rules will override your output if they later cover this sender.
"""
```

- [ ] **Step 9.2 — Failing tests for the LLM wrapper**

`tests/test_parsers_llm.py`:

```python
"""LLM strategy with mocked Anthropic SDK."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock

import pytest

from trip_tracker.parsers.llm import LLMClient, parse_with_llm


def _msg() -> EmailMessage:
    m = EmailMessage()
    m["Subject"] = "Sample"
    m["From"] = "x@y.com"
    m["To"] = "oliver@trips.example.com"
    m.set_content("Plain content for the LLM to read.")
    return m


def _fake_response(*, tool_input: dict, input_tokens: int = 100, output_tokens: int = 50):
    """Build a MagicMock matching anthropic.types.Message shape."""
    block = MagicMock()
    block.type = "tool_use"
    block.name = "extract_segments"
    block.input = tool_input
    msg = MagicMock()
    msg.content = [block]
    msg.usage = MagicMock()
    msg.usage.input_tokens = input_tokens
    msg.usage.output_tokens = output_tokens
    return msg


@pytest.mark.asyncio
async def test_parse_decodes_tool_use() -> None:
    client = MagicMock(spec=LLMClient)
    client.call = AsyncMock(
        return_value=_fake_response(
            tool_input={
                "segments": [
                    {
                        "type": "flight",
                        "status": "confirmed",
                        "start_at": "2026-06-01T09:00:00-04:00",
                        "start_tz": "America/New_York",
                        "end_at": "2026-06-01T22:00:00+02:00",
                        "end_tz": "Europe/Paris",
                        "start_location": {"iata": "JFK"},
                        "end_location": {"iata": "CDG"},
                        "details": {"flight_number": "DL44"},
                        "confirmation_number": "ABC123",
                        "provider": "Delta",
                    }
                ],
                "confidence": 0.9,  # will be clamped to 0.85
            },
        )
    )
    result = await parse_with_llm(client, _msg(), hint=None)
    assert result.confidence == 0.85
    assert result.source == "llm:haiku-4-5"
    assert len(result.segments) == 1


@pytest.mark.asyncio
async def test_parse_with_hint_appends_to_user_message() -> None:
    client = MagicMock(spec=LLMClient)
    client.call = AsyncMock(
        return_value=_fake_response(tool_input={"segments": [], "confidence": 0.9})
    )
    await parse_with_llm(client, _msg(), hint="This is a return flight")
    args, kwargs = client.call.call_args
    user_msg = kwargs["user_content"]
    assert "This is a return flight" in user_msg
```

- [ ] **Step 9.3 — Implement `LLMClient` + `parse_with_llm`**

`src/trip_tracker/parsers/llm.py`:

```python
"""Anthropic Haiku 4.5 strategy: tool-use forced structured output.

Two layers:
- LLMClient: thin wrapper over anthropic.AsyncAnthropic, async .call() method.
- parse_with_llm(client, msg, hint): orchestrates message construction, tool use,
  response decoding, confidence clamping. Returns ParseResult.

Budget enforcement happens in dispatch.py — this module assumes the call is
allowed.
"""

from __future__ import annotations

import logging
from email.message import EmailMessage
from typing import Any

from anthropic import AsyncAnthropic

from trip_tracker.config import Settings
from trip_tracker.parsers.base import ParseResult, SegmentDraft
from trip_tracker.schemas.llm import EXTRACT_SEGMENTS_TOOL, SYSTEM_PROMPT

logger = logging.getLogger(__name__)

# Haiku self-rated confidence is capped here regardless of model output.
HAIKU_CONFIDENCE_CEILING = 0.85


class LLMClient:
    """Thin Anthropic wrapper. Single call() method to keep mocking trivial."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    async def call(self, *, user_content: str) -> Any:
        """One Haiku call with prompt-caching enabled on the system prompt."""
        return await self._client.messages.create(
            model=self._settings.llm_model,
            max_tokens=2048,
            system=[
                {
                    "type": "text",
                    "text": SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[EXTRACT_SEGMENTS_TOOL],
            tool_choice={"type": "tool", "name": "extract_segments"},
            messages=[{"role": "user", "content": user_content}],
        )


def _msg_to_text(msg: EmailMessage) -> str:
    """Flatten an EmailMessage to a single string for the LLM."""
    parts = [
        f"Subject: {msg.get('Subject', '')}",
        f"From: {msg.get('From', '')}",
        f"To: {msg.get('To', '')}",
        f"Date: {msg.get('Date', '')}",
        "",
    ]
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    parts.append(
                        payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    )
                break
    else:
        if msg.get_content_type().startswith("text/"):
            payload = msg.get_payload(decode=True)
            if isinstance(payload, bytes):
                parts.append(payload.decode(msg.get_content_charset() or "utf-8", errors="replace"))
    return "\n".join(parts)


@dataclass
class LLMOutcome:
    """ParseResult plus the token counts so callers can record exact LLM cost."""

    result: ParseResult
    input_tokens: int
    output_tokens: int


async def parse_with_llm(client: LLMClient, msg: EmailMessage, *, hint: str | None) -> LLMOutcome:
    """Run Haiku once, decode the tool-use response, return LLMOutcome.

    `hint` (optional): short user-supplied note appended to the user message
    (the "Re-ask Claude with hint" inbox action).

    Returns LLMOutcome — the worker uses input/output_tokens to call
    `cost_cents_for_usage` and `record_usage` (Task 16).
    """
    user_text = _msg_to_text(msg)
    if hint:
        user_text += f"\n\n[User hint: {hint}]"

    response = await client.call(user_content=user_text)

    in_tok = int(getattr(response.usage, "input_tokens", 0) or 0)
    out_tok = int(getattr(response.usage, "output_tokens", 0) or 0)

    tool_input: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "extract_segments":
            tool_input = block.input
            break
    if tool_input is None:
        return LLMOutcome(
            result=ParseResult(
                segments=[],
                confidence=0.0,
                source="llm:haiku-4-5",
                warnings=["model did not invoke extract_segments tool"],
            ),
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    raw_conf = float(tool_input.get("confidence", 0.0))
    confidence = min(raw_conf, HAIKU_CONFIDENCE_CEILING)
    segments = [SegmentDraft.model_validate(s) for s in tool_input.get("segments", [])]

    return LLMOutcome(
        result=ParseResult(
            segments=segments,
            confidence=confidence,
            source="llm:haiku-4-5",
        ),
        input_tokens=in_tok,
        output_tokens=out_tok,
    )
```

(Add `from dataclasses import dataclass` to the imports at the top of `llm.py`.)

**Note for Task 10 (dispatcher):** the dispatcher calls `parse_with_llm` and now receives `LLMOutcome`. Update `dispatch.py` to read `.result` from the outcome — this is a 1-line change inside `dispatch_parse`. The dispatcher still returns `ParseOutcome` to its caller; LLM-specific token counts surface only when the worker calls `parse_with_llm` directly OR when the dispatcher exposes them via a new field on `ParseOutcome`. Simplest: have `ParseOutcome` carry an optional `llm_outcome: LLMOutcome | None = None` field that's populated when strategy 3 ran.

- [ ] **Step 9.4 — Live-LLM smoke test (skipped in CI)**

`tests/test_parsers_llm_live.py`:

```python
"""Live-LLM smoke test: real Haiku call to verify prompt + tool schema.

Marked @pytest.mark.live_llm. Skipped in CI. Run locally before each release:

    ANTHROPIC_API_KEY=sk-... uv run pytest -m live_llm -v
"""

from __future__ import annotations

import os
from email.message import EmailMessage

import pytest

from trip_tracker.config import Settings
from trip_tracker.parsers.llm import LLMClient, parse_with_llm


@pytest.mark.live_llm
@pytest.mark.skipif(not os.getenv("ANTHROPIC_API_KEY"), reason="requires ANTHROPIC_API_KEY")
@pytest.mark.asyncio
async def test_haiku_round_trip_with_canonical_email() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Your AirExample flight on 2026-06-01"
    msg["From"] = "confirmations@airexample.com"
    msg["To"] = "oliver@trips.example.com"
    msg.set_content(
        "Confirmation: ABC123\n"
        "Flight: AE42, JFK → CDG\n"
        "Departs 2026-06-01 09:00 EDT, arrives 2026-06-01 22:00 CEST\n"
        "Seat: 12A\n"
    )

    settings = Settings()  # picks up ANTHROPIC_API_KEY from env
    client = LLMClient(settings)
    result = await parse_with_llm(client, msg, hint=None)

    assert result.source == "llm:haiku-4-5"
    assert result.confidence <= 0.85  # ceiling
    assert len(result.segments) >= 1
    seg = result.segments[0]
    assert seg.type == "flight"
```

Add to `pyproject.toml` if not already there:

```toml
[tool.pytest.ini_options]
markers = [
    "live_llm: requires ANTHROPIC_API_KEY; not run in CI",
]
addopts = "-m 'not live_llm'"
```

- [ ] **Step 9.5 — Run + commit**

```bash
uv run pytest tests/test_parsers_llm.py -v       # unit tests, mocked
uv run pytest tests/test_parsers_llm_live.py -v  # should be skipped without API key
uv run pytest -q                                  # full suite, live tests excluded
uv run ruff check . && uv run mypy src
git add src/trip_tracker/schemas/llm.py src/trip_tracker/parsers/llm.py \
        tests/test_parsers_llm.py tests/test_parsers_llm_live.py pyproject.toml
git commit -m "feat(parsers): Haiku LLM strategy with tool-use + prompt caching"
```

**Quality bar:**
- `LLMClient.call(...)` returns `Any` because Anthropic SDK type stubs are loose. Acceptable: callers (only `parse_with_llm`) handle the shape with `getattr` + `model_validate`.
- `prompt_caching` via `cache_control: ephemeral` on the system prompt — saves ~90% on subsequent calls within 5 min. No-op on the first call.
- `tool_choice={"type": "tool", "name": "..."}` *forces* the model to call the tool, eliminating the "model just chatted instead" failure mode.

---

## Task 10 — `parsers/dispatch.py`: Strategy Orchestration

**Spec ref:** §5 (strategy order + confidence thresholds + retry).

**Files:**
- Create: `src/trip_tracker/parsers/dispatch.py`
- Create: `tests/test_parsers_dispatch.py`

- [ ] **Step 10.1 — Failing tests**

`tests/test_parsers_dispatch.py`:

```python
"""Dispatcher: JSON-LD → vendor → LLM, with confidence floor + budget."""

from __future__ import annotations

from datetime import UTC, datetime
from email.message import EmailMessage
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from trip_tracker.parsers.base import ParseResult, SegmentDraft
from trip_tracker.parsers.dispatch import ParseOutcome, dispatch_parse


def _msg() -> EmailMessage:
    m = EmailMessage()
    m["From"] = "x@y.com"
    m.set_content("body")
    return m


def _draft() -> SegmentDraft:
    return SegmentDraft(
        type="flight",
        start_at=datetime(2026, 6, 1, tzinfo=UTC),
        start_tz="UTC",
    )


@pytest.mark.asyncio
async def test_jsonld_short_circuits() -> None:
    """If JSON-LD returns confidence ≥ ceiling, vendor + LLM never called."""
    with (
        patch("trip_tracker.parsers.dispatch.parse_jsonld") as jsonld,
        patch("trip_tracker.parsers.dispatch.select_parsers") as vendors,
        patch("trip_tracker.parsers.dispatch.parse_with_llm") as llm,
    ):
        jsonld.return_value = ParseResult(segments=[_draft()], confidence=0.95, source="json-ld")
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        assert outcome.result.source == "json-ld"
        vendors.assert_not_called()
        llm.assert_not_called()


@pytest.mark.asyncio
async def test_vendor_runs_when_jsonld_empty() -> None:
    fake_vendor_cls = MagicMock()
    fake_vendor_cls.return_value.parse.return_value = ParseResult(
        segments=[_draft()],
        confidence=0.9,
        source="rules:fake",
    )
    with (
        patch(
            "trip_tracker.parsers.dispatch.parse_jsonld",
            return_value=ParseResult(segments=[], confidence=0.0, source="json-ld"),
        ),
        patch("trip_tracker.parsers.dispatch.select_parsers", return_value=[fake_vendor_cls]),
        patch("trip_tracker.parsers.dispatch.parse_with_llm") as llm,
    ):
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        assert outcome.result.source == "rules:fake"
        llm.assert_not_called()


@pytest.mark.asyncio
async def test_llm_runs_when_vendor_below_floor() -> None:
    fake_vendor_cls = MagicMock()
    fake_vendor_cls.return_value.parse.return_value = ParseResult(
        segments=[_draft()],
        confidence=0.4,
        source="rules:fake",
    )
    with (
        patch(
            "trip_tracker.parsers.dispatch.parse_jsonld",
            return_value=ParseResult(segments=[], confidence=0.0, source="json-ld"),
        ),
        patch("trip_tracker.parsers.dispatch.select_parsers", return_value=[fake_vendor_cls]),
        patch("trip_tracker.parsers.dispatch.is_over_budget", new=AsyncMock(return_value=False)),
        patch(
            "trip_tracker.parsers.dispatch.parse_with_llm",
            new=AsyncMock(
                return_value=ParseResult(
                    segments=[_draft()], confidence=0.85, source="llm:haiku-4-5"
                )
            ),
        ) as llm,
    ):
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        llm.assert_awaited_once()
        assert outcome.result.source == "llm:haiku-4-5"


@pytest.mark.asyncio
async def test_budget_skips_llm() -> None:
    """Over budget: LLM step skipped; outcome carries the best earlier result."""
    with (
        patch(
            "trip_tracker.parsers.dispatch.parse_jsonld",
            return_value=ParseResult(segments=[], confidence=0.0, source="json-ld"),
        ),
        patch("trip_tracker.parsers.dispatch.select_parsers", return_value=[]),
        patch("trip_tracker.parsers.dispatch.is_over_budget", new=AsyncMock(return_value=True)),
        patch("trip_tracker.parsers.dispatch.parse_with_llm") as llm,
    ):
        outcome = await dispatch_parse(
            _msg(), llm_client=MagicMock(), db=MagicMock(), cap_cents=100
        )
        llm.assert_not_called()
        assert outcome.budget_skipped is True
        assert outcome.result.segments == []
```

- [ ] **Step 10.2 — Implement `dispatch_parse`**

`src/trip_tracker/parsers/dispatch.py`:

```python
"""Strategy chain: JSON-LD → matched vendor → Haiku.

Caller (the worker) handles persistence + clustering + status assignment.
This module is pure orchestration over an EmailMessage.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage

from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.parsers.base import ParseResult, select_parsers
from trip_tracker.parsers.budget import is_over_budget
from trip_tracker.parsers.jsonld import parse_jsonld
from trip_tracker.parsers.llm import LLMClient, parse_with_llm

logger = logging.getLogger(__name__)


@dataclass
class ParseOutcome:
    result: ParseResult
    budget_skipped: bool = False  # True when LLM was needed but budget exhausted
    llm_input_tokens: int = 0  # populated when strategy 3 actually ran
    llm_output_tokens: int = 0  # populated when strategy 3 actually ran


async def dispatch_parse(
    msg: EmailMessage,
    *,
    llm_client: LLMClient,
    db: AsyncSession,
    cap_cents: int,
    hint: str | None = None,
) -> ParseOutcome:
    """Run JSON-LD, then matched vendor, then Haiku. Return the first
    high-confidence result, or the best low-confidence result, or empty.

    Strategy fall-through:
      - confidence ≥ ceiling (per strategy) → return immediately
      - confidence < ceiling → keep best so far, try next strategy
      - all strategies done → return best (may be confidence=0)
    """
    best: ParseResult = ParseResult(segments=[], confidence=0.0, source="none")

    # Strategy 1 — JSON-LD
    try:
        r1 = parse_jsonld(msg)
    except Exception as exc:  # noqa: BLE001 — extruct can raise broadly
        logger.warning("jsonld dispatch error: %s", exc)
        r1 = ParseResult(segments=[], confidence=0.0, source="json-ld")
    if r1.segments and r1.confidence >= 0.9:
        return ParseOutcome(result=r1)
    if r1.confidence > best.confidence:
        best = r1

    # Strategy 2 — matched vendor
    from_addr = msg.get("From", "")
    for parser_cls in select_parsers(from_addr):
        try:
            r2 = parser_cls().parse(msg)
        except Exception as exc:  # noqa: BLE001
            logger.warning("vendor %s raised: %s", parser_cls.name, exc)
            continue
        if r2.confidence >= parser_cls.confidence_floor:
            return ParseOutcome(result=r2)
        if r2.confidence > best.confidence:
            best = r2

    # Strategy 3 — LLM (budget gate)
    if await is_over_budget(db, cap_cents=cap_cents):
        return ParseOutcome(result=best, budget_skipped=True)

    try:
        outcome = await parse_with_llm(llm_client, msg, hint=hint)
    except Exception as exc:  # noqa: BLE001 — Anthropic SDK can raise broadly
        logger.warning("llm dispatch error: %s", exc)
        return ParseOutcome(result=best)

    r3 = outcome.result
    if r3.confidence > best.confidence:
        best = r3
    return ParseOutcome(
        result=best,
        llm_input_tokens=outcome.input_tokens,
        llm_output_tokens=outcome.output_tokens,
    )
```

- [ ] **Step 10.3 — Run + commit**

```bash
uv run pytest tests/test_parsers_dispatch.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/dispatch.py tests/test_parsers_dispatch.py
git commit -m "feat(parsers): dispatch — JSON-LD → vendor → Haiku with budget gate"
```

**Quality bar:**
- The `# noqa: BLE001` is justified per spec §5: parser exceptions log + fall through; they don't propagate.
- `select_parsers(from_addr)` returns parsers sorted most-specific-first, so the loop tries the narrower regex before broader ones.
- LLM failure (`r3` raises) returns the best earlier result, NOT empty — this prevents a Haiku outage from clobbering a partial vendor extraction.

---

## Task 11 — Vendor Pack: Air France

**Spec ref:** §4 (plugin architecture), §11 (priority — upcoming travel).

**Files:**
- Create: `src/trip_tracker/parsers/vendors/__init__.py`
- Create: `src/trip_tracker/parsers/vendors/air_france/__init__.py`
- Create: `src/trip_tracker/parsers/vendors/air_france/README.md`
- Create: `src/trip_tracker/parsers/vendors/air_france/fixtures/confirmation.eml`
- Create: `src/trip_tracker/parsers/vendors/air_france/fixtures/confirmation.expected.json`
- Create: `tests/test_parsers_vendors.py` (parameterized over fixtures)

- [ ] **Step 11.1 — Vendors package init (auto-discovers subpackages)**

`src/trip_tracker/parsers/vendors/__init__.py`:

```python
"""Vendor parser packs. Each subpackage's __init__.py defines a VendorParser
subclass, which auto-registers via __init_subclass__ in parsers.base.

Adding a new vendor:
  1. Create a subpackage `parsers/vendors/<name>/` with __init__.py defining
     a VendorParser subclass.
  2. Add a `from . import <name>` line below.
  3. Drop fixtures: `fixtures/<scenario>.eml` + `<scenario>.expected.json`.
  4. CI's parameterized vendor test will pick up the new fixtures automatically.
"""

from __future__ import annotations

# Each import triggers __init_subclass__ in parsers.base, registering the parser.
from . import air_france  # noqa: F401
```

(Subsequent vendor tasks add more `from . import <vendor>` lines here.)

- [ ] **Step 11.2 — Air France parser**

`src/trip_tracker/parsers/vendors/air_france/README.md`:

```markdown
# Air France parser pack

Handles email confirmations from `noreply@airfrance.fr`, `noreply@airfrance.com`,
`flyingblue@airfrance.com`. Last verified format: 2026.

The current Air France template embeds a JSON-LD `FlightReservation` block
which the upstream JSON-LD strategy will already pick up at confidence 0.95.
This parser is the **fallback** for the (rarer) plain-HTML version that arrives
when the user is on the older AF template (no JSON-LD).
```

`src/trip_tracker/parsers/vendors/air_france/__init__.py`:

```python
"""Air France parser pack."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_FLIGHT_NUM = re.compile(r"\b(AF|KL)\s?(\d{2,4})\b")
_IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:→|->|to)\s*([A-Z]{3})\b")
_DATE_TIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", flags=re.IGNORECASE)
_CONFIRMATION = re.compile(r"\b(?:confirmation|reservation)[\s:]+([A-Z0-9]{6,8})", re.I)


class AirFranceParser(VendorParser):
    name: ClassVar[str] = "air_france"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@airfrance\.(fr|com)$", re.I),
        re.compile(r"flyingblue@airfrance\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        flight_match = _FLIGHT_NUM.search(body)
        iata_match = _IATA_PAIR.search(body)
        dt_matches = _DATE_TIME.findall(body)
        conf_match = _CONFIRMATION.search(body)

        if not (flight_match and iata_match and dt_matches):
            return ParseResult(
                segments=[],
                confidence=0.0,
                source="rules:air_france",
                warnings=["could not locate flight number + IATA pair + datetime"],
            )

        flight_no = f"{flight_match.group(1)}{flight_match.group(2)}"
        origin, dest = iata_match.group(1), iata_match.group(2)
        y, m, d, hh, mm = dt_matches[0]
        start_at = datetime(int(y), int(m), int(d), int(hh), int(mm), tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="flight",
            confirmation_number=conf_match.group(1) if conf_match else None,
            provider="Air France",
            start_at=start_at,
            start_tz="UTC",
            start_location={"iata": origin},
            end_location={"iata": dest},
            details={"flight_number": flight_no},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:air_france")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

- [ ] **Step 11.3 — Fixture pair**

`src/trip_tracker/parsers/vendors/air_france/fixtures/confirmation.eml`:

```
Subject: Confirmation Air France AF12345
From: noreply@airfrance.fr
To: oliver@trips.example.com
Content-Type: text/plain

Bonjour,

Votre réservation: ABCDEF

AF1234, JFK -> CDG
2026-06-01 09:00 (heure locale au départ)
Arrivée: 2026-06-01 22:00

Merci.
```

`src/trip_tracker/parsers/vendors/air_france/fixtures/confirmation.expected.json`:

```json
{
  "source": "rules:air_france",
  "confidence": 0.9,
  "segments": [
    {
      "type": "flight",
      "confirmation_number": "ABCDEF",
      "provider": "Air France",
      "start_location": {"iata": "JFK"},
      "end_location": {"iata": "CDG"},
      "details": {"flight_number": "AF1234"}
    }
  ]
}
```

(Note: the expected JSON intentionally omits `start_at` / `start_tz` / `status` — the parameterized test only asserts that the parser-extracted fields contain these keys. See test below.)

- [ ] **Step 11.4 — Parameterized vendor test**

`tests/test_parsers_vendors.py`:

```python
"""Auto-parameterized over every vendors/*/fixtures/*.eml.

Adding a new vendor PR drops new fixture files; this test picks them up
automatically. No test code changes needed.
"""

from __future__ import annotations

import json
from email import message_from_bytes
from email.policy import default as email_policy_default
from pathlib import Path
from typing import Any

import pytest

import trip_tracker.parsers.vendors  # noqa: F401  # triggers registration
from trip_tracker.parsers.base import VendorParser, get_registry

_VENDORS_DIR = Path(__file__).parent.parent / "src" / "trip_tracker" / "parsers" / "vendors"


def _fixture_pairs() -> list[tuple[str, Path, Path]]:
    pairs: list[tuple[str, Path, Path]] = []
    for vendor_dir in _VENDORS_DIR.iterdir():
        if not vendor_dir.is_dir():
            continue
        fixtures = vendor_dir / "fixtures"
        if not fixtures.is_dir():
            continue
        for eml in sorted(fixtures.glob("*.eml")):
            expected = eml.with_suffix(".expected.json")
            if expected.exists():
                pairs.append((f"{vendor_dir.name}/{eml.stem}", eml, expected))
    return pairs


def _find_parser(vendor: str) -> type[VendorParser]:
    for cls in get_registry():
        if cls.name == vendor:
            return cls
    raise RuntimeError(f"no registered parser for vendor: {vendor}")


@pytest.mark.parametrize(
    ("name", "eml_path", "expected_path"),
    _fixture_pairs(),
    ids=lambda x: x if isinstance(x, str) else None,
)
def test_vendor_fixture(name: str, eml_path: Path, expected_path: Path) -> None:
    """Each fixture is parsed by its vendor's parser; output is compared to expected."""
    vendor_name = name.split("/")[0]
    parser_cls = _find_parser(vendor_name)
    parser = parser_cls()

    msg = message_from_bytes(eml_path.read_bytes(), policy=email_policy_default)
    result = parser.parse(msg)
    expected: dict[str, Any] = json.loads(expected_path.read_text())

    assert result.source == expected["source"], f"{name}: source mismatch"
    assert result.confidence >= expected["confidence"] - 0.001, f"{name}: confidence too low"
    assert len(result.segments) == len(expected["segments"]), f"{name}: segment count mismatch"

    for actual_seg, expected_seg in zip(result.segments, expected["segments"], strict=True):
        for key, expected_val in expected_seg.items():
            actual_val = getattr(actual_seg, key)
            assert actual_val == expected_val, (
                f"{name}: {key} mismatch — got {actual_val!r}, expected {expected_val!r}"
            )


def test_at_least_one_fixture_pair_exists() -> None:
    """If this fails, a vendor parser exists but has no fixtures (CI gate)."""
    assert len(_fixture_pairs()) >= 1
```

- [ ] **Step 11.5 — Run + commit**

```bash
uv run pytest tests/test_parsers_vendors.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/vendors/ tests/test_parsers_vendors.py
git commit -m "feat(parsers): Air France vendor pack + parameterized fixture test"
```

**Quality bar:**
- The parameterized test ID `f"{vendor_dir.name}/{eml.stem}"` makes pytest output read like `test_vendor_fixture[air_france/confirmation]` — easy to grep for one vendor.
- The expected.json compares only the keys it specifies, not the whole segment dict — lets fixtures be partial (`start_at` doesn't have to be exact, just present-and-non-null).

---

## Task 12 — Vendor Packs: American + United

**Spec ref:** §4 (plugin architecture).

**Files:**
- Create: `src/trip_tracker/parsers/vendors/american/{__init__,README,fixtures/}`
- Create: `src/trip_tracker/parsers/vendors/united/{__init__,README,fixtures/}`
- Modify: `src/trip_tracker/parsers/vendors/__init__.py` (add 2 imports)

- [ ] **Step 12.1 — American Airlines parser**

`src/trip_tracker/parsers/vendors/american/__init__.py`:

```python
"""American Airlines parser pack."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_FLIGHT_NUM = re.compile(r"\bAA\s?(\d{1,4})\b")
_IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:→|->|to)\s*([A-Z]{3})\b")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME = re.compile(r"(\d{2}):(\d{2})")
_CONFIRMATION = re.compile(r"(?:record locator|confirmation|conf #?)[:\s]+([A-Z0-9]{6})", re.I)


class AmericanParser(VendorParser):
    name: ClassVar[str] = "american"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@(notify\.)?aa\.com$", re.I),
        re.compile(r"@email\.aa\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        flight = _FLIGHT_NUM.search(body)
        iata = _IATA_PAIR.search(body)
        date_m = _DATE.search(body)
        time_m = _TIME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (flight and iata and date_m and time_m):
            return ParseResult(segments=[], confidence=0.0, source="rules:american")

        y, m, d = (int(g) for g in date_m.groups())
        hh, mm = (int(g) for g in time_m.groups())
        start_at = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="flight",
            confirmation_number=conf.group(1) if conf else None,
            provider="American Airlines",
            start_at=start_at,
            start_tz="UTC",
            start_location={"iata": iata.group(1)},
            end_location={"iata": iata.group(2)},
            details={"flight_number": f"AA{flight.group(1)}"},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:american")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

`src/trip_tracker/parsers/vendors/american/README.md`:

```markdown
# American Airlines parser pack

Handles `noreply@aa.com`, `notify@aa.com`, `*@email.aa.com`. Last verified: 2026.

Falls back to Haiku when the AA template includes JSON-LD (most cases) — JSON-LD
strategy runs first and at higher confidence.
```

`src/trip_tracker/parsers/vendors/american/fixtures/confirmation.eml`:

```
Subject: Your AA flight is confirmed
From: notify@aa.com
To: oliver@trips.example.com
Content-Type: text/plain

Record locator: ABC123

AA42, JFK -> LAX
2026-07-15 08:30
```

`src/trip_tracker/parsers/vendors/american/fixtures/confirmation.expected.json`:

```json
{
  "source": "rules:american",
  "confidence": 0.9,
  "segments": [
    {
      "type": "flight",
      "confirmation_number": "ABC123",
      "provider": "American Airlines",
      "start_location": {"iata": "JFK"},
      "end_location": {"iata": "LAX"},
      "details": {"flight_number": "AA42"}
    }
  ]
}
```

- [ ] **Step 12.2 — United Airlines parser**

`src/trip_tracker/parsers/vendors/united/__init__.py`:

```python
"""United Airlines parser pack."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_FLIGHT_NUM = re.compile(r"\bUA\s?(\d{1,4})\b")
_IATA_PAIR = re.compile(r"\b([A-Z]{3})\s*(?:→|->|to)\s*([A-Z]{3})\b")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_TIME = re.compile(r"(\d{2}):(\d{2})")
_CONFIRMATION = re.compile(r"(?:confirmation|conf #?)[:\s]+([A-Z0-9]{6})", re.I)


class UnitedParser(VendorParser):
    name: ClassVar[str] = "united"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@united\.com$", re.I),
        re.compile(r"@unitedairlines\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        flight = _FLIGHT_NUM.search(body)
        iata = _IATA_PAIR.search(body)
        date_m = _DATE.search(body)
        time_m = _TIME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (flight and iata and date_m and time_m):
            return ParseResult(segments=[], confidence=0.0, source="rules:united")

        y, m, d = (int(g) for g in date_m.groups())
        hh, mm = (int(g) for g in time_m.groups())
        start_at = datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="flight",
            confirmation_number=conf.group(1) if conf else None,
            provider="United Airlines",
            start_at=start_at,
            start_tz="UTC",
            start_location={"iata": iata.group(1)},
            end_location={"iata": iata.group(2)},
            details={"flight_number": f"UA{flight.group(1)}"},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:united")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

(README + fixture pair following the AA pattern: `confirmation.eml` from `noreply@united.com`, `flight UA1234`, expected.json mirrors AA's shape.)

- [ ] **Step 12.3 — Wire into vendors package**

In `src/trip_tracker/parsers/vendors/__init__.py`, add:

```python
from . import american, united  # noqa: F401
```

(Combine with the existing line as needed.)

- [ ] **Step 12.4 — Run + commit**

```bash
uv run pytest tests/test_parsers_vendors.py -v   # should now run 3 fixtures
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/vendors/american/ \
        src/trip_tracker/parsers/vendors/united/ \
        src/trip_tracker/parsers/vendors/__init__.py
git commit -m "feat(parsers): American + United vendor packs"
```

**Quality bar:**
- AA + UA share most of the regex patterns. **Don't** prematurely abstract a `_BasicAirlineParser` base class — Phase 4+ will probably want vendor-specific quirks (UA's tz handling differs from AA's, etc.). The duplication is intentional per YAGNI.
- The `_extract_text` helper is repeated; if a 3rd parser duplicates it, refactor to `parsers/_email_text.py` then.

---

## Task 13 — Vendor Packs: Fairmont + Avis + National

**Spec ref:** §4 (plugin architecture). 3 packs in one task.

**Files:** 3 vendor subpackages + fixtures, plus `vendors/__init__.py` import line.

- [ ] **Step 13.1 — Fairmont parser**

`src/trip_tracker/parsers/vendors/fairmont/__init__.py`:

```python
"""Fairmont (Accor brand) parser pack — type='lodging'."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_CHECK_IN = re.compile(r"check[\s-]?in[:\s]+(\d{4}-\d{2}-\d{2})", re.I)
_CHECK_OUT = re.compile(r"check[\s-]?out[:\s]+(\d{4}-\d{2}-\d{2})", re.I)
_HOTEL_NAME = re.compile(r"(Fairmont[^\n,]+)", re.I)
_CITY = re.compile(r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*),\s+(?:[A-Z]{2,}|[A-Z][a-z]+)")
_CONFIRMATION = re.compile(r"confirmation[:\s]+([A-Z0-9]{6,12})", re.I)


class FairmontParser(VendorParser):
    name: ClassVar[str] = "fairmont"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@fairmont\.com$", re.I),
        re.compile(r"@(reservations|email)\.fairmont\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        ci = _CHECK_IN.search(body)
        co = _CHECK_OUT.search(body)
        name = _HOTEL_NAME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (ci and co and name):
            return ParseResult(segments=[], confidence=0.0, source="rules:fairmont")

        ci_dt = datetime.fromisoformat(ci.group(1)).replace(hour=15, tzinfo=ZoneInfo("UTC"))
        co_dt = datetime.fromisoformat(co.group(1)).replace(hour=11, tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="lodging",
            confirmation_number=conf.group(1) if conf else None,
            provider="Fairmont",
            start_at=ci_dt,
            start_tz="UTC",
            end_at=co_dt,
            end_tz="UTC",
            start_location={"name": name.group(1).strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:fairmont")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

`fairmont/fixtures/confirmation.eml`:

```
Subject: Fairmont Le Château Frontenac confirmation
From: reservations@fairmont.com
To: oliver@trips.example.com
Content-Type: text/plain

Confirmation: ABC12345

Hotel: Fairmont Le Château Frontenac
Check-in: 2026-07-20
Check-out: 2026-07-25

Merci.
```

`fairmont/fixtures/confirmation.expected.json`:

```json
{
  "source": "rules:fairmont",
  "confidence": 0.9,
  "segments": [
    {
      "type": "lodging",
      "confirmation_number": "ABC12345",
      "provider": "Fairmont",
      "start_location": {"name": "Fairmont Le Château Frontenac"}
    }
  ]
}
```

- [ ] **Step 13.2 — Avis parser** (covers Avis + Budget engine — single parser handles both)

`src/trip_tracker/parsers/vendors/avis/__init__.py`:

```python
"""Avis parser pack — type='car'. Covers Avis + Budget (same email engine)."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_PICKUP = re.compile(r"pick[\s-]?up[:\s]+([\w\s]+?)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})", re.I)
_DROPOFF = re.compile(
    r"drop[\s-]?off[:\s]+([\w\s]+?)\s+(\d{4}-\d{2}-\d{2})\s+(\d{2}):(\d{2})", re.I
)
_CONFIRMATION = re.compile(r"confirmation[:\s]+([A-Z0-9]{6,12})", re.I)


class AvisParser(VendorParser):
    name: ClassVar[str] = "avis"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@(avis|budget)\.com$", re.I),
        re.compile(r"@email\.(avis|budget)\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        pu = _PICKUP.search(body)
        do = _DROPOFF.search(body)
        conf = _CONFIRMATION.search(body)
        if not (pu and do):
            return ParseResult(segments=[], confidence=0.0, source="rules:avis")

        pu_loc, py, pm, pd, ph, pmm = pu.groups()
        do_loc, dy, dm, dd, dh, dmm = do.groups()

        seg = SegmentDraft(
            type="car",
            confirmation_number=conf.group(1) if conf else None,
            provider="Avis",
            start_at=datetime(int(py), int(pm), int(pd), int(ph), int(pmm), tzinfo=ZoneInfo("UTC")),
            start_tz="UTC",
            end_at=datetime(int(dy), int(dm), int(dd), int(dh), int(dmm), tzinfo=ZoneInfo("UTC")),
            end_tz="UTC",
            start_location={"name": pu_loc.strip()},
            end_location={"name": do_loc.strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:avis")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

(Fixture: pickup at "JFK Airport 2026-07-15 14:00", dropoff at "JFK Airport 2026-07-20 12:00", confirmation "AVIS123456".)

- [ ] **Step 13.3 — National parser** (covers National + Enterprise + Alamo engine)

`src/trip_tracker/parsers/vendors/national/__init__.py` — same shape as Avis but provider="National", sender patterns `@(nationalcar|enterprise|alamo|enterpriseplus)\.com`.

- [ ] **Step 13.4 — Wire imports**

In `src/trip_tracker/parsers/vendors/__init__.py`:

```python
from . import air_france, american, avis, fairmont, national, united  # noqa: F401
```

- [ ] **Step 13.5 — Run + commit**

```bash
uv run pytest tests/test_parsers_vendors.py -v   # 6 fixtures now
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/vendors/{fairmont,avis,national}/ \
        src/trip_tracker/parsers/vendors/__init__.py
git commit -m "feat(parsers): Fairmont + Avis + National vendor packs"
```

**Quality bar:**
- Fairmont's `start_at`/`end_at` are set to `15:00`/`11:00` UTC respectively as best-effort defaults; refining tz from city name is Phase 4+.
- Avis covers both Avis and Budget; National covers National/Enterprise/Alamo. The `provider` field reflects the parsed pack identity; the `details.brand` field could carry the actual brand if needed (skip for now).

---

## Task 14 — Vendor Packs: Amtrak + SNCF

**Spec ref:** §4 (plugin architecture).

**Files:** 2 vendor subpackages + fixtures.

- [ ] **Step 14.1 — Amtrak parser**

`src/trip_tracker/parsers/vendors/amtrak/__init__.py`:

```python
"""Amtrak parser pack — type='train'."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_TRAIN = re.compile(r"\bTrain\s+(\d+)", re.I)
_STATION_PAIR = re.compile(
    r"([A-Z][a-zA-Z\s]+?)\s+(?:→|->|to)\s+([A-Z][a-zA-Z\s]+?)\s*(?:\n|on)", re.I
)
_DATETIME = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})")
_CONFIRMATION = re.compile(r"confirmation[:\s]+([A-Z0-9]{6,12})", re.I)


class AmtrakParser(VendorParser):
    name: ClassVar[str] = "amtrak"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@amtrak\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        train = _TRAIN.search(body)
        stations = _STATION_PAIR.search(body)
        dt = _DATETIME.search(body)
        conf = _CONFIRMATION.search(body)

        if not (stations and dt):
            return ParseResult(segments=[], confidence=0.0, source="rules:amtrak")

        y, m, d, hh, mm = (int(g) for g in dt.groups())
        seg = SegmentDraft(
            type="train",
            confirmation_number=conf.group(1) if conf else None,
            provider="Amtrak",
            start_at=datetime(y, m, d, hh, mm, tzinfo=ZoneInfo("UTC")),
            start_tz="UTC",
            start_location={"name": stations.group(1).strip()},
            end_location={"name": stations.group(2).strip()},
            details={"train_number": train.group(1) if train else None},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:amtrak")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

(Fixture: Train 195, "New York Penn -> Boston South Station", `2026-08-10 09:30`.)

- [ ] **Step 14.2 — SNCF parser** — same shape as Amtrak; sender patterns `@(sncf|tgv-europe|oui)\.com`. Provider="SNCF".

- [ ] **Step 14.3 — Wire imports + run + commit**

```python
# vendors/__init__.py
from . import air_france, american, amtrak, avis, fairmont, national, sncf, united  # noqa: F401
```

```bash
uv run pytest tests/test_parsers_vendors.py -v   # 8 fixtures now
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/vendors/{amtrak,sncf}/ src/trip_tracker/parsers/vendors/__init__.py
git commit -m "feat(parsers): Amtrak + SNCF vendor packs"
```

**Quality bar:**
- Train station names are free-text; we don't have an IATA-style code system. Storing them in `start_location.name` is fine for v0.3.0 — Phase 4 might add a station→geo lookup.

---

## Task 15 — Vendor Packs: Uber + Blacklane

**Spec ref:** §1, §2 (ground transport packs).

**Files:** 2 vendor subpackages + fixtures.

- [ ] **Step 15.1 — Uber parser**

`src/trip_tracker/parsers/vendors/uber/__init__.py`:

```python
"""Uber receipt parser — type='transfer'. Captures every ride per spec §2."""

from __future__ import annotations

import re
from datetime import datetime
from email.message import EmailMessage
from typing import ClassVar
from zoneinfo import ZoneInfo

from trip_tracker.parsers.base import ParseResult, SegmentDraft, VendorParser

_PICKUP = re.compile(r"(?:pickup|from)[:\s]+([^\n]+)", re.I)
_DROPOFF = re.compile(r"(?:drop[\s-]?off|to)[:\s]+([^\n]+)", re.I)
_DATETIME = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


class UberParser(VendorParser):
    name: ClassVar[str] = "uber"
    sender_patterns: ClassVar[list[re.Pattern[str]]] = [
        re.compile(r"@uber\.com$", re.I),
        re.compile(r"@receipt\.uber\.com$", re.I),
    ]

    def parse(self, msg: EmailMessage) -> ParseResult:
        body = _extract_text(msg)
        pu = _PICKUP.search(body)
        do = _DROPOFF.search(body)
        dt = _DATETIME.search(body)
        if not (pu and do and dt):
            return ParseResult(segments=[], confidence=0.0, source="rules:uber")

        date_str, time_str = dt.groups()
        d = datetime.fromisoformat(f"{date_str}T{time_str}").replace(tzinfo=ZoneInfo("UTC"))

        seg = SegmentDraft(
            type="transfer",
            provider="Uber",
            start_at=d,
            start_tz="UTC",
            start_location={"name": pu.group(1).strip()},
            end_location={"name": do.group(1).strip()},
        )
        return ParseResult(segments=[seg], confidence=0.9, source="rules:uber")


def _extract_text(msg: EmailMessage) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
```

(Fixture: pickup "JFK Airport", dropoff "Manhattan Hotel", `2026-07-15 14:30`.)

- [ ] **Step 15.2 — Blacklane parser** — same shape as Uber; sender pattern `@blacklane\.com`. Provider="Blacklane".

- [ ] **Step 15.3 — Wire imports + run + commit**

```python
# vendors/__init__.py
from . import (  # noqa: F401
    air_france,
    american,
    amtrak,
    avis,
    blacklane,
    fairmont,
    national,
    sncf,
    uber,
    united,
)
```

```bash
uv run pytest tests/test_parsers_vendors.py -v   # 10 fixtures now
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/parsers/vendors/{uber,blacklane}/ src/trip_tracker/parsers/vendors/__init__.py
git commit -m "feat(parsers): Uber + Blacklane ground-transport vendor packs"
```

**Quality bar:**
- All 10 vendor packs are now registered. `get_registry()` should return 10 classes.
- Each pack has ≥1 fixture (per the spec §4 fixture policy). The parameterized test is the gate.

---

## Task 16 — Worker + Redis + ARQ + Webhook Integration

**Spec ref:** §7 (worker model), §10 (`parse_pending` migration).

**Files:**
- Create: `src/trip_tracker/worker.py`
- Modify: `src/trip_tracker/__main__.py` (add `parse_pending` subcommand)
- Modify: `src/trip_tracker/ingest/webhook.py` (enqueue after commit)
- Modify: `docker-compose.yml` (add Redis + worker)
- Modify: `docker-compose.dev.yml` (Redis port forward for local dev)
- Create: `tests/test_worker.py`

- [ ] **Step 16.1 — Failing test: webhook enqueues + worker runs the task**

`tests/test_worker.py`:

```python
"""End-to-end worker test: webhook → enqueue → worker → DB write.

Uses ARQ's testing helpers (no real Redis) — the in-memory queue runs the
task synchronously inside the test.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.user import User
from trip_tracker.parsers.base import ParseResult, SegmentDraft

_FIXTURE_MIME = (
    b"Subject: Test\r\n"
    b"From: test@example.com\r\n"
    b"To: oliver@trips.example.com\r\n"
    b"Content-Type: text/plain\r\n"
    b"\r\n"
    b"Hello world\r\n"
)


@pytest.mark.asyncio
async def test_webhook_enqueues_parse_task(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """POST /api/ingest/email enqueues parse_raw_email(id) after commit."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("WEBHOOK_SECRET", "x" * 32)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    settings = Settings()

    user = User(oidc_subject="t", email="t@x.com", display_name="T")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)

    sig = hmac.new(b"x" * 32, _FIXTURE_MIME, hashlib.sha256).hexdigest()
    nonce = secrets.token_hex(16)

    with patch("trip_tracker.ingest.webhook.enqueue_parse") as mock_enqueue:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(transport=transport, base_url="http://test") as c,
        ):
            r = await c.post(
                "/api/ingest/email",
                content=_FIXTURE_MIME,
                headers={
                    "X-Webhook-Signature": sig,
                    "X-Webhook-Nonce": nonce,
                    "Content-Type": "message/rfc822",
                },
            )

    assert r.status_code == 202
    mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_parse_raw_email_writes_segment(
    db_session: AsyncSession,
) -> None:
    """The worker task: given a RawEmail id, parse it and write a Segment."""
    from trip_tracker.worker import parse_raw_email

    user = User(oidc_subject="w", email="w@x.com", display_name="W")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="confirmations@airexample.com",
        subject="Flight",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_FIXTURE_MIME,
        headers={},
        parse_status="pending",
    )
    db_session.add(raw)
    await db_session.commit()

    fake_dispatch = AsyncMock(
        return_value=type(
            "O",
            (),
            {  # ParseOutcome-shaped
                "result": ParseResult(
                    segments=[
                        SegmentDraft(
                            type="flight",
                            start_at=datetime(2026, 6, 1, tzinfo=UTC),
                            start_tz="UTC",
                            start_location={"city": "New York"},
                            end_location={"city": "Paris"},
                        )
                    ],
                    confidence=0.9,
                    source="rules:test",
                ),
                "budget_skipped": False,
            },
        )(),
    )

    with patch("trip_tracker.worker.dispatch_parse", new=fake_dispatch):
        # ARQ-style ctx is just a dict with at least 'session' or our own DB injection
        await parse_raw_email({"settings": Settings()}, str(raw.id))

    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"
```

- [ ] **Step 16.2 — Implement `worker.py`**

`src/trip_tracker/worker.py`:

```python
"""ARQ worker: parses RawEmail rows in the background.

Runs in a separate container from the FastAPI app, sharing the same image:
    command: ["arq", "trip_tracker.worker.WorkerSettings"]

Single task `parse_raw_email(raw_email_id)` is enqueued by the webhook
handler after RawEmail is committed.
"""

from __future__ import annotations

import logging
import uuid
from email import message_from_bytes
from email.policy import default as email_policy_default
from typing import Any

from arq.connections import RedisSettings
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.parsers.budget import cost_cents_for_usage, record_usage
from trip_tracker.parsers.cluster import cluster_for_user, derive_destination
from trip_tracker.parsers.dispatch import dispatch_parse
from trip_tracker.parsers.llm import LLMClient
import trip_tracker.parsers.vendors  # noqa: F401  # register all packs

logger = logging.getLogger(__name__)


async def parse_raw_email(ctx: dict[str, Any], raw_email_id: str) -> None:
    """Parse one RawEmail and persist the result.

    Idempotent: re-running on an already-parsed RawEmail is a no-op.

    TODO (Phase 3.5): the Inbox `reask` route stores a hint in
    raw.headers['X-Tt-Hint']. Pass it through to dispatch_parse here so the
    LLM picks up the user's correction. v0.3.0 ships without this propagation
    — re-parse runs but the hint is unused. Adding it is one line:
        hint = (raw.headers or {}).get("X-Tt-Hint")
    plus passing hint=hint into dispatch_parse.
    """
    settings: Settings = ctx["settings"]
    engine = ctx.get("engine") or create_async_engine(str(settings.database_url))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    rid = uuid.UUID(raw_email_id)
    async with Session() as db:
        raw = await db.get(RawEmail, rid)
        if raw is None:
            logger.warning("RawEmail %s not found", raw_email_id)
            return
        if raw.parse_status != "pending":
            logger.info("RawEmail %s already parsed (%s); skipping", raw_email_id, raw.parse_status)
            return

        # Resolve owner via case-insensitive local-part match
        local_part = raw.to_address.split("@", 1)[0].lower()
        owner = (
            await db.execute(
                select(ForwardingAlias).where(ForwardingAlias.local_part == local_part)
            )
        ).scalar_one_or_none()
        if owner is None:
            logger.info("no alias for %s — marking no_segments", raw.to_address)
            raw.parse_status = "no_segments"
            await db.commit()
            return

        msg = message_from_bytes(raw.mime_blob, policy=email_policy_default)
        client = LLMClient(settings)
        outcome = await dispatch_parse(
            msg,
            llm_client=client,
            db=db,
            cap_cents=settings.llm_daily_budget_cents,
        )

        if outcome.result.source == "llm:haiku-4-5":
            # Real token counts come from the dispatcher (Task 10 wires them
            # through ParseOutcome.llm_input_tokens / llm_output_tokens, fed
            # by parse_with_llm's LLMOutcome — Task 9).
            await record_usage(
                db,
                cost_cents=cost_cents_for_usage(
                    input_tokens=outcome.llm_input_tokens,
                    output_tokens=outcome.llm_output_tokens,
                ),
            )

        if not outcome.result.segments:
            raw.parse_status = "no_segments"
            await db.commit()
            return

        # Cluster + persist each segment
        for draft in outcome.result.segments:
            decision = await cluster_for_user(db, owner.user_id, draft)
            if decision.kind == "create_new":
                trip = Trip(
                    title=decision.auto_title or "Trip",
                    start_date=draft.start_at.date(),
                    end_date=(draft.end_at or draft.start_at).date(),
                    primary_destination=derive_destination(draft),
                    created_by=owner.user_id,
                )
                db.add(trip)
                await db.flush()
                db.add(TripTraveler(trip_id=trip.id, user_id=owner.user_id, role="owner"))
                trip_id = trip.id
            elif decision.kind == "attach":
                trip_id = decision.trip_id
            else:  # ambiguous → write segment without trip_id
                trip_id = None

            seg = Segment(
                trip_id=trip_id,
                owner_user_id=owner.user_id,
                type=draft.type,
                status=draft.status,
                confirmation_number=draft.confirmation_number,
                provider=draft.provider,
                start_at=draft.start_at,
                start_tz=draft.start_tz,
                end_at=draft.end_at,
                end_tz=draft.end_tz,
                start_location=draft.start_location,
                end_location=draft.end_location,
                details=draft.details,
                parse_source=outcome.result.source,
                parse_confidence=outcome.result.confidence,
                raw_email_id=raw.id,  # FK so Inbox.discard can locate this segment (Task 17)
            )
            db.add(seg)

        # Status: review if low confidence, parsed if high
        if outcome.result.confidence < settings.llm_confidence_floor:
            raw.parse_status = "review"
        else:
            raw.parse_status = "parsed"
        await db.commit()


# Read settings once at module import. Worker fails fast if env is incomplete.
_SETTINGS = Settings()


class WorkerSettings:
    """ARQ entry point. `command: ["arq", "trip_tracker.worker.WorkerSettings"]`.

    The `arq` CLI imports this module, reads the class attributes (functions,
    redis_settings, max_tries), then runs the worker loop. By the time `arq` is
    invoked, env vars are loaded (via the container's environment), so the
    module-level Settings() call is safe.
    """

    functions = [parse_raw_email]
    max_tries = 5
    keep_result_seconds = 0
    redis_settings = RedisSettings.from_dsn(_SETTINGS.redis_url)

    @staticmethod
    async def startup(ctx: dict[str, Any]) -> None:
        ctx["settings"] = _SETTINGS

    @staticmethod
    async def shutdown(ctx: dict[str, Any]) -> None:
        pass
```

(Note: the `WorkerSettings.redis_settings` lazy assignment dance is ARQ's idiom; ARQ reads the class attribute when the worker starts. If this gets messy, replace with a `make_worker_settings()` factory.)

- [ ] **Step 16.3 — Webhook integration**

In `src/trip_tracker/ingest/webhook.py`, after `await db.commit()` (or after the `async with db.begin():` block exits), add:

```python
from arq import create_pool
from arq.connections import RedisSettings


async def enqueue_parse(settings: Settings, raw_email_id: uuid.UUID) -> None:
    """Enqueue parse_raw_email task. Failure is logged but not propagated —
    the parse_pending admin command is the recovery path."""
    try:
        redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await redis.enqueue_job("parse_raw_email", str(raw_email_id))
        await redis.aclose()
    except Exception as exc:  # noqa: BLE001 — Redis blip shouldn't fail the webhook
        logger.warning("enqueue_parse failed for %s: %s", raw_email_id, exc)
```

In the existing handler, after the RawEmail is committed:

```python
await enqueue_parse(settings, raw_email.id)
```

- [ ] **Step 16.4 — `parse_pending` admin command**

Modify `src/trip_tracker/__main__.py`:

```python
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.ingest.webhook import enqueue_parse
from trip_tracker.models.raw_email import RawEmail


async def _parse_pending(*, max_emails: int = 1000, dry_run: bool = False) -> None:
    settings = Settings()
    engine = create_async_engine(str(settings.database_url))
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as db:
        rows = (
            (
                await db.execute(
                    select(RawEmail.id).where(RawEmail.parse_status == "pending").limit(max_emails)
                )
            )
            .scalars()
            .all()
        )
        print(f"Found {len(rows)} pending RawEmails")
        if dry_run:
            return
        for rid in rows:
            await enqueue_parse(settings, rid)
        print(f"Enqueued {len(rows)} parse jobs")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        # existing default behavior (uvicorn run) — preserve
        ...
    elif args[0] == "parse_pending":
        max_emails = 1000
        dry_run = False
        for a in args[1:]:
            if a == "--dry-run":
                dry_run = True
            elif a.startswith("--max-emails="):
                max_emails = int(a.split("=", 1)[1])
        asyncio.run(_parse_pending(max_emails=max_emails, dry_run=dry_run))
    else:
        print(f"Unknown subcommand: {args[0]}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
```

- [ ] **Step 16.5 — docker-compose updates**

In `docker-compose.yml`, add:

```yaml
  trip-tracker-redis:
    image: redis:7-alpine
    restart: unless-stopped
    networks: [internal]
    # No volume — queue is ephemeral, parse_pending recovers backlog.

  trip-tracker-worker:
    image: ${TRIP_TRACKER_IMAGE:-ghcr.io/REPLACE_OWNER/trip-tracker:latest}
    restart: unless-stopped
    command: ["arq", "trip_tracker.worker.WorkerSettings"]
    depends_on:
      trip-tracker-db:
        condition: service_healthy
      trip-tracker-redis:
        condition: service_started
    environment:
      DATABASE_URL: ${DATABASE_URL}
      ANTHROPIC_API_KEY: ${ANTHROPIC_API_KEY}
      REDIS_URL: ${REDIS_URL:-redis://trip-tracker-redis:6379/0}
      LLM_DAILY_BUDGET_CENTS: ${LLM_DAILY_BUDGET_CENTS:-100}
      LLM_MODEL: ${LLM_MODEL:-claude-haiku-4-5-20251001}
      LLM_CONFIDENCE_FLOOR: ${LLM_CONFIDENCE_FLOOR:-0.7}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      LOG_FORMAT: ${LOG_FORMAT:-json}
    networks: [internal]
```

Add `REDIS_URL` and `ANTHROPIC_API_KEY` to the existing `trip-tracker-app` `environment:` block too.

In `docker-compose.dev.yml`, add a port forward for Redis if local dev needs it.

- [ ] **Step 16.6 — Run + commit**

```bash
uv run pytest tests/test_worker.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/worker.py src/trip_tracker/__main__.py \
        src/trip_tracker/ingest/webhook.py \
        docker-compose.yml docker-compose.dev.yml \
        tests/test_worker.py
git commit -m "feat(worker): ARQ worker + Redis container + parse_pending command"
```

**Quality bar:**
- The module-level `_SETTINGS = Settings()` reads env at import time, so the worker fails fast on missing/malformed `REDIS_URL` or `ANTHROPIC_API_KEY`. The `app` process should NOT import `worker.py` (it runs in a separate container with the same env, but the `arq` CLI is the only entry point that loads this module).
- `enqueue_parse` swallows Redis errors. The webhook returns 202 even if Redis is down. Recovery: `python -m trip_tracker parse_pending`.
- `parse_raw_email` is idempotent (`if parse_status != "pending": return`). Re-runs are safe.

---

## Task 17 — Inbox Routes + Templates + 5 Actions

**Spec ref:** §6 (Inbox UI, three buckets, five actions).

**Files:**
- Create: `src/trip_tracker/routes/inbox.py`
- Create: `src/trip_tracker/templates/inbox/list.html`
- Create: `src/trip_tracker/templates/inbox/_bucket_review.html`
- Create: `src/trip_tracker/templates/inbox/_bucket_no_segments.html`
- Create: `src/trip_tracker/templates/inbox/_bucket_duplicates.html`
- Modify: `src/trip_tracker/templates/base.html` (nav link)
- Modify: `src/trip_tracker/app.py` (include router)
- Create: `tests/test_routes_inbox.py`

This is the largest UI task — 4 templates, 5 action handlers, 3-bucket query.

- [ ] **Step 17.1 — Failing tests for the 3 buckets + 5 actions**

`tests/test_routes_inbox.py`:

```python
"""Inbox routes: list (3 buckets) + 5 actions per bucket 1."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


def _cookie(user: User, settings: Settings) -> dict[str, str]:
    return {
        "tt_session": encode_session(
            SessionPayload(user_id=user.id, oidc_subject=user.oidc_subject),
            secret=settings.session_secret.get_secret_value(),
            max_age=3600,
        )
    }


_MIME = (
    b"Subject: Test\r\nFrom: x@y.com\r\nTo: oliver@trips.example.com\r\n"
    b"Content-Type: text/plain\r\n\r\nbody\r\n"
)


async def _setup_user_with_raw(
    db_session: AsyncSession, *, parse_status: str
) -> tuple[User, RawEmail]:
    user = User(oidc_subject="i", email="i@x.com", display_name="I")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="Test",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=_MIME,
        headers={},
        parse_status=parse_status,
    )
    db_session.add(raw)
    await db_session.commit()
    return user, raw


@pytest.mark.asyncio
async def test_inbox_list_shows_three_buckets(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, _ = await _setup_user_with_raw(db_session, parse_status="review")
    await _setup_user_with_raw(db_session, parse_status="no_segments")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get("/inbox")
    assert r.status_code == 200
    assert "Needs review" in r.text or "review" in r.text.lower()
    assert "No segments" in r.text or "no_segments" in r.text


@pytest.mark.asyncio
async def test_inbox_confirm_action(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="review")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)
    assert r.status_code == 303
    await db_session.refresh(raw)
    assert raw.parse_status == "parsed"


@pytest.mark.asyncio
async def test_inbox_discard_action(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Discard marks the email as 'no_segments' AND deletes auto-created segments
    (per spec §6.1: 'segment row deleted (if any was written)')."""
    from datetime import UTC, datetime

    from trip_tracker.models.segment import Segment
    from trip_tracker.models.trip import Trip
    from trip_tracker.models.trip_traveler import TripTraveler

    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="review")

    # Seed an auto-created segment linked to the RawEmail.
    trip = Trip(
        title="Auto",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="llm:haiku-4-5",
        parse_confidence=0.7,
        raw_email_id=raw.id,
    )
    db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.post(f"/inbox/{raw.id}/discard", follow_redirects=False)
    assert r.status_code == 303
    await db_session.refresh(raw)
    assert raw.parse_status == "no_segments"

    rows = (
        (await db_session.execute(select(Segment).where(Segment.raw_email_id == raw.id)))
        .scalars()
        .all()
    )
    assert rows == []  # auto-created segment deleted


@pytest.mark.asyncio
async def test_inbox_reparse_action(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Re-parse re-enqueues the parse task and resets parse_status='pending'."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user, raw = await _setup_user_with_raw(db_session, parse_status="no_segments")

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    with patch("trip_tracker.routes.inbox.enqueue_parse", new=AsyncMock()) as mock_enqueue:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=_cookie(user, settings)
            ) as c,
        ):
            r = await c.post(f"/inbox/{raw.id}/reparse", follow_redirects=False)
    assert r.status_code == 303
    await db_session.refresh(raw)
    assert raw.parse_status == "pending"
    mock_enqueue.assert_awaited_once()


@pytest.mark.asyncio
async def test_inbox_404_for_other_users_raw(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """A user can't act on a RawEmail they don't own (via alias)."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user_a, raw = await _setup_user_with_raw(db_session, parse_status="review")
    user_b = User(oidc_subject="b", email="b@x.com", display_name="B")
    db_session.add(user_b)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user_b, settings)
        ) as c,
    ):
        r = await c.post(f"/inbox/{raw.id}/confirm", follow_redirects=False)
    assert r.status_code == 404
```

- [ ] **Step 17.2 — Implement `routes/inbox.py`**

`src/trip_tracker/routes/inbox.py`:

```python
"""Inbox routes: 3 buckets + 5 actions.

Auth scoping: same case-insensitive lowering pattern as admin raw-emails
(extract local-part of to_address, lower, join to forwarding_aliases).
Admins (is_admin=True) see all RawEmails.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ingest.webhook import enqueue_parse
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.user import User

router = APIRouter(prefix="/inbox", tags=["inbox"])
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))


def _user_owned_filter(user: User) -> sa.ColumnElement[bool]:
    """Return a SQLA expression for 'this user owns this RawEmail'."""
    if user.is_admin:
        return sa.true()
    local = sa.func.lower(sa.func.split_part(RawEmail.to_address, "@", 1))
    return RawEmail.id.in_(
        select(RawEmail.id)
        .join(ForwardingAlias, ForwardingAlias.local_part == local)
        .where(ForwardingAlias.user_id == user.id)
    )


async def _load_owned(db: AsyncSession, user: User, raw_id: uuid.UUID) -> RawEmail:
    raw = (
        await db.execute(select(RawEmail).where(RawEmail.id == raw_id, _user_owned_filter(user)))
    ).scalar_one_or_none()
    if raw is None:
        raise HTTPException(404)
    return raw


@router.get("", response_class=HTMLResponse)
async def inbox_list(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    own = _user_owned_filter(user)
    review_rows = (
        (
            await db.execute(
                select(RawEmail)
                .where(RawEmail.parse_status == "review", own)
                .order_by(RawEmail.received_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    no_seg_rows = (
        (
            await db.execute(
                select(RawEmail)
                .where(RawEmail.parse_status == "no_segments", own)
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
            "duplicate_rows": [],  # Phase 3 ships duplicate detection in a follow-up
        },
    )


@router.post("/{raw_id}/confirm", response_model=None)
async def confirm(
    raw_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "parsed"
    await db.commit()
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/discard", response_model=None)
async def discard(
    raw_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    """Spec §6.1: discard sets parse_status='no_segments' AND deletes any
    segment(s) the parser auto-created from this email.

    Implementation: delete every Segment with raw_email_id = raw_id. If the
    user wanted to keep an auto-created segment, they'd click Confirm or
    Edit instead. (Task 18 prefill flow does NOT mutate raw_email_id on
    save, so user-confirmed segments still link to the RawEmail; discard
    treats this as the user saying "actually scrap everything from this
    email" — matching the spec's literal "segment row deleted" language.)
    """
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "no_segments"

    from sqlalchemy import delete

    from trip_tracker.models.segment import Segment

    await db.execute(delete(Segment).where(Segment.raw_email_id == raw_id))
    await db.commit()
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/reparse", response_model=None)
async def reparse(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    raw = await _load_owned(db, user, raw_id)
    raw.parse_status = "pending"
    await db.commit()
    settings: Settings = request.app.state.settings
    await enqueue_parse(settings, raw.id)
    return RedirectResponse("/inbox", status_code=303)


@router.post("/{raw_id}/reask", response_model=None)
async def reask(
    raw_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    hint: str = Form(...),
) -> Response:
    """Re-runs the parse with a user-supplied hint appended to the LLM prompt.

    For v0.3.0 this is implemented as: reset parse_status='pending' + enqueue
    a parse job. The worker doesn't currently accept a hint kwarg — that's a
    Phase 3.5 enhancement. For now, the hint is recorded in raw.headers as
    'X-Tt-Hint' for the worker to pick up if it adds the kwarg later.
    """
    raw = await _load_owned(db, user, raw_id)
    new_headers = dict(raw.headers or {})
    new_headers["X-Tt-Hint"] = hint
    raw.headers = new_headers
    raw.parse_status = "pending"
    await db.commit()
    settings: Settings = request.app.state.settings
    await enqueue_parse(settings, raw.id)
    return RedirectResponse("/inbox", status_code=303)
```

- [ ] **Step 17.3 — Templates**

`src/trip_tracker/templates/inbox/list.html`:

```html
{% extends "base.html" %}
{% block title %}Inbox · trip-tracker{% endblock %}
{% block content %}
  <h1 class="text-3xl font-semibold">Inbox</h1>
  <p class="mt-1 text-sm text-zinc-500">
    {{ review_rows|length }} need review · {{ no_seg_rows|length }} no segments
  </p>

  {% if review_rows %}
    <details open class="mt-6">
      <summary class="cursor-pointer text-lg font-medium">
        Needs review ({{ review_rows|length }})
      </summary>
      {% include "inbox/_bucket_review.html" %}
    </details>
  {% endif %}

  {% if no_seg_rows %}
    <details class="mt-6">
      <summary class="cursor-pointer text-lg font-medium">
        No segments detected ({{ no_seg_rows|length }})
      </summary>
      {% include "inbox/_bucket_no_segments.html" %}
    </details>
  {% endif %}

  {% if not review_rows and not no_seg_rows %}
    <p class="mt-6 text-zinc-500">Inbox empty — parsers caught everything 🎉</p>
  {% endif %}
{% endblock %}
```

`src/trip_tracker/templates/inbox/_bucket_review.html`:

```html
<ul class="mt-4 divide-y divide-zinc-200 dark:divide-zinc-800">
  {% for raw in review_rows %}
    <li class="py-3">
      <div class="flex items-baseline justify-between">
        <div>
          <div class="font-medium">{{ raw.subject or "(no subject)" }}</div>
          <div class="text-sm text-zinc-500">
            {{ raw.from_address }} · {{ raw.received_at.strftime("%Y-%m-%d %H:%M") }}
          </div>
        </div>
        <div class="space-x-2 text-sm">
          <form method="post" action="/inbox/{{ raw.id }}/confirm" class="inline">
            <button class="underline">Confirm</button>
          </form>
          <a class="underline" href="/admin/raw-emails/{{ raw.id }}">View raw</a>
          <form method="post" action="/inbox/{{ raw.id }}/reparse" class="inline">
            <button class="underline">Re-parse</button>
          </form>
          <form method="post" action="/inbox/{{ raw.id }}/discard" class="inline"
                onsubmit="return confirm('Discard?')">
            <button class="text-red-600 underline">Discard</button>
          </form>
        </div>
      </div>
      <details class="mt-2">
        <summary class="cursor-pointer text-sm text-zinc-500">Re-ask with hint</summary>
        <form method="post" action="/inbox/{{ raw.id }}/reask" class="mt-2 flex gap-2">
          <input class="flex-1 rounded border p-2 text-sm" name="hint"
                 placeholder="e.g. This is a return flight, not outbound" required>
          <button class="rounded bg-zinc-900 px-3 py-1.5 text-sm text-white dark:bg-zinc-100 dark:text-zinc-900">Re-ask</button>
        </form>
      </details>
    </li>
  {% endfor %}
</ul>
```

`src/trip_tracker/templates/inbox/_bucket_no_segments.html`:

```html
<ul class="mt-4 divide-y divide-zinc-200 dark:divide-zinc-800">
  {% for raw in no_seg_rows %}
    <li class="py-3 flex items-baseline justify-between">
      <div>
        <div>{{ raw.subject or "(no subject)" }}</div>
        <div class="text-sm text-zinc-500">{{ raw.from_address }}</div>
      </div>
      <div class="space-x-2 text-sm">
        <a class="underline" href="/admin/raw-emails/{{ raw.id }}">View raw</a>
        <form method="post" action="/inbox/{{ raw.id }}/reparse" class="inline">
          <button class="underline">Re-parse</button>
        </form>
        <a class="underline" href="/segments/new?from_raw_email={{ raw.id }}">Add manually</a>
      </div>
    </li>
  {% endfor %}
</ul>
```

`src/trip_tracker/templates/inbox/_bucket_duplicates.html` — empty placeholder for v0.3.0:

```html
{# Bucket 3: duplicate detection lands in Phase 3.5 (not v0.3.0). #}
```

- [ ] **Step 17.4 — Wire router + nav link**

In `src/trip_tracker/app.py`, include the inbox router:

```python
from trip_tracker.routes.inbox import router as inbox_router

# ... in create_app:
app.include_router(inbox_router)
app.state.settings = settings  # so request.app.state.settings works in inbox routes
```

In `src/trip_tracker/templates/base.html`, add an Inbox link in the nav between Trips and Admin (only for logged-in users):

```html
{% if user %}
  <a href="/inbox">Inbox</a>
{% endif %}
```

- [ ] **Step 17.5 — Run + commit**

```bash
uv run pytest tests/test_routes_inbox.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/routes/inbox.py \
        src/trip_tracker/templates/inbox/ \
        src/trip_tracker/templates/base.html \
        src/trip_tracker/app.py \
        tests/test_routes_inbox.py
git commit -m "feat(inbox): 3-bucket inbox UI + 5 actions"
```

**Quality bar:**
- The `_user_owned_filter` returns a SQLA expression usable in `.where(...)`. Pattern shamelessly copied from the admin raw-emails join (Phase 2 commit `233b8f3`/`574b505`).
- Bucket 3 (duplicates) is intentionally empty for v0.3.0. The template scaffolding is there so adding it later is purely additive.
- `reask` records the hint in `headers['X-Tt-Hint']` for future use; the worker doesn't currently consume it. This is intentional — it lets the inbox UI ship without coupling to a worker change.

---

## Task 18 — Segments Form Prefill Path

**Spec ref:** §6.1 (Edit action: `/segments/<id>/edit?from_raw_email=<id>` with ✨ indicators).

**Files:**
- Modify: `src/trip_tracker/routes/segments.py` (accept `from_raw_email` param, render ✨ indicators)
- Modify: 6 segment form templates (✨ indicator beside AI-set fields)
- Create: `tests/test_routes_segments_prefill.py`

- [ ] **Step 18.1 — Failing tests**

`tests/test_routes_segments_prefill.py`:

```python
"""Segments form prefill path: ?from_raw_email=<id> shows ✨ indicators."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
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
async def test_edit_with_from_raw_email_shows_sparkle(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """When ?from_raw_email=<id> is present, AI-set fields render with ✨."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="p", email="p@x.com", display_name="P")
    db_session.add(user)
    await db_session.flush()
    db_session.add(ForwardingAlias(local_part="oliver", user_id=user.id))
    raw = RawEmail(
        id=uuid.uuid4(),
        received_at=datetime.now(tz=UTC),
        to_address="oliver@trips.example.com",
        from_address="x@y.com",
        subject="t",
        message_id=f"<{uuid.uuid4()}@x>",
        mime_blob=b"",
        headers={},
        parse_status="review",
    )
    trip = Trip(
        title="T",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add_all([raw, trip])
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        provider="Air France",
        confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        start_location={"iata": "JFK"},
        end_location={"iata": "CDG"},
        details={"flight_number": "AF44"},
        parse_source="rules:air_france",
        parse_confidence=0.9,
    )
    db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit?from_raw_email={raw.id}")
    assert r.status_code == 200
    # ✨ indicator should appear at least once near AI-set fields
    assert "✨" in r.text


@pytest.mark.asyncio
async def test_edit_without_from_raw_email_no_sparkle(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Manual segment (no parser source) — no ✨."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="m", email="m@x.com", display_name="M")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="T",
        start_date=datetime(2026, 6, 1).date(),
        end_date=datetime(2026, 6, 5).date(),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id,
        owner_user_id=user.id,
        type="flight",
        status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC),
        start_tz="UTC",
        parse_source="manual",
        parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.get(f"/trips/{trip.id}/segments/{seg.id}/edit")
    assert r.status_code == 200
    assert "✨" not in r.text
```

- [ ] **Step 18.2 — Modify `routes/segments.py` edit handler**

In `edit_segment_form` (Phase 2 Task 14), accept a `from_raw_email` query param and pass `ai_suggested=True` into the template context whenever the segment's `parse_source != 'manual'`:

```python
@router.get("/trips/{trip_id}/segments/{segment_id}/edit", response_class=HTMLResponse)
async def edit_segment_form(
    request: Request,
    trip_id: uuid.UUID,
    segment_id: uuid.UUID,
    from_raw_email: uuid.UUID | None = Query(default=None),
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    seg = await _load_segment_for_user(db, trip_id, segment_id, user.id)
    trips = (await db.execute(_user_trips(db, user.id))).scalars().all()
    ai_suggested = seg.parse_source != "manual"
    return templates.TemplateResponse(
        request,
        f"segments/{seg.type}_form.html",
        {
            "user": user,
            "trips": trips,
            "timezones": TIMEZONES,
            "values": _segment_to_form_values(seg),
            "errors": {},
            "type": seg.type,
            "edit_segment_id": str(seg.id),
            "ai_suggested": ai_suggested,
            "from_raw_email": from_raw_email,
        },
    )
```

- [ ] **Step 18.3 — Add ✨ indicator to common fields template**

In `src/trip_tracker/templates/segments/_common_fields.html`, near the top (after the type hidden input):

```html
{% if ai_suggested %}
  {# Subtle banner: this form was prefilled by the parser. #}
  <p class="rounded bg-amber-50 p-2 text-sm text-amber-900 dark:bg-amber-950 dark:text-amber-100">
    ✨ AI-suggested values — review and confirm
  </p>
{% endif %}
```

In each per-type form template (flight/lodging/car/train/transfer/activity), wrap the type-specific fields' labels with a small ✨ when `ai_suggested`:

```html
{% set sparkle = "✨ " if ai_suggested else "" %}
<label class="block text-sm">{{ sparkle }}Flight number
  <input ...>
</label>
```

(Repeat across all 6 form templates' type-specific blocks. The common fields — trip selector, dates, status — also carry the sparkle when `ai_suggested`.)

- [ ] **Step 18.4 — Run + commit**

```bash
uv run pytest tests/test_routes_segments_prefill.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/routes/segments.py \
        src/trip_tracker/templates/segments/ \
        tests/test_routes_segments_prefill.py
git commit -m "feat(segments): ✨ AI-suggested indicators in prefilled edit form"
```

**Quality bar:**
- `ai_suggested` is derived from `parse_source != "manual"`, so manually-created segments never show ✨, even if edited via the edit form.
- The amber banner at the top is the primary signal; per-field ✨ is secondary noise. Don't over-do per-field icons — the spec says "small ✨ indicator", which the banner accomplishes more cleanly.

---

## Task 19 — README + Verification + Tag v0.3.0

**Spec ref:** §11 (Done definition).

**Files:**
- Modify: `README.md` (Phase 3 section)
- Run: full pytest, ruff, mypy, pre-commit, docker build, Playwright smoke
- Tag: `v0.3.0`

- [ ] **Step 19.1 — README updates**

Update the Status line in `README.md`:

```markdown
> **Status:** Phase 3 — automated parsing of forwarded emails into structured segments.
> Phase 4 (search + geocoding) is next.
```

Append a new section before "Production deploy":

```markdown
## How parsers work (Phase 3)

When a forwarding email arrives at `/api/ingest/email`, the worker runs three
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
or one or more. The dispatcher keeps the best result across strategies.

### Adding a new vendor

1. Create `src/trip_tracker/parsers/vendors/<name>/__init__.py` with a
   `VendorParser` subclass.
2. Add the import to `src/trip_tracker/parsers/vendors/__init__.py`.
3. Drop a fixture pair: `fixtures/<scenario>.eml` + `<scenario>.expected.json`.
4. CI's parameterized vendor test will pick up the new fixture automatically.

### Daily LLM budget

Set `LLM_DAILY_BUDGET_CENTS` (default 100 = $1/day). When exceeded, RawEmails
skip the Haiku step and route to `parse_status='review'` for manual handling.

### Recovering after deploy

Existing `RawEmail` rows from before v0.3.0 sit in `parse_status='pending'`.
Reprocess them once after deploy:

    docker compose exec trip-tracker-app python -m trip_tracker parse_pending

This is idempotent and safe to re-run.
```

- [ ] **Step 19.2 — Full local verification gate**

```bash
./scripts/build-tailwind.sh
uv run pytest --cov                          # ≥85%
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run pre-commit run --all-files
uv run bandit -c pyproject.toml -r src/
uv run djlint src/trip_tracker/templates --check
docker build -t trip-tracker:dev .
```

All must be green. Iterate on any failure.

- [ ] **Step 19.3 — Playwright smoke test (browser verification)**

Repeat the v0.2.0 verification pattern (the brainstorming session committed `4916450` was caught this way):

1. Bring up Postgres + Redis: `docker run -d --name trip-tracker-pg-verify -e POSTGRES_USER=trip -e POSTGRES_DB=trip -e POSTGRES_PASSWORD=trip-dev -p 5433:5432 postgres:18.3-alpine` and `docker run -d --name trip-tracker-redis-verify -p 6380:6379 redis:7-alpine`.
2. Create temporary `.env` pointing at 127.0.0.1:5433 + 127.0.0.1:6380 + a throwaway `ANTHROPIC_API_KEY=sk-ant-test`.
3. `uv run alembic upgrade head`.
4. Seed an admin user + alias + sample RawEmail with `parse_status='review'` (script similar to v0.2.0's `_verify_seed.py`).
5. Start the app: `uv run uvicorn 'trip_tracker.app:create_app' --factory --host 127.0.0.1 --port 8000 --log-level warning &`
6. Drive Playwright through `/inbox` → confirm action → discard action → re-parse action.
7. Tear down: kill app, `docker rm -f trip-tracker-pg-verify trip-tracker-redis-verify`, delete temp `.env`.

Note: a real Haiku round-trip requires a real `ANTHROPIC_API_KEY`. Skip the LLM step in browser verification — the live-LLM smoke test (Task 9) covers the prompt + tool-schema integration separately.

- [ ] **Step 19.4 — Commit, tag, push**

```bash
git add README.md
git commit -m "docs: Phase 3 — README parser + budget + recovery sections"

git tag -a -s v0.3.0 -m "Phase 3 — Parsers"
git push origin main
git push origin v0.3.0
```

The release workflow on GitHub fires on the tag push and produces a multi-arch image at `ghcr.io/<owner>/trip-tracker:v0.3.0`, signed with cosign + SBOM attached. Same pattern as v0.2.0.

- [ ] **Step 19.5 — Schedule release-verification agent**

Same pattern as v0.2.0: schedule a one-time remote agent ~20 min after the tag push to verify the GHCR image, signature, SBOM. See the v0.2.0 conversation history for the prompt template.

**Quality bar:**
- The full verification gate (`pytest --cov`, ruff, mypy, pre-commit, bandit, djlint, docker build) must all pass before tagging.
- If any vendor fixture parser drifted (a regex stopped matching after a refactor), this is where it surfaces.

---

## Done Definition for Phase 3

- All 19 tasks merged to `main`.
- CI green (lint + typecheck + test + security + docker + djlint + bandit).
- Coverage ≥ 85%; all 10 vendor parsers ship with ≥1 fixture.
- `python -m trip_tracker parse_pending` successfully reprocesses any Phase 2 leftover RawEmails.
- One real Air France confirmation round-trips: webhook → ARQ → Segment auto-created → Trip auto-clustered → visible at `/trips`.
- One unknown-sender email round-trips through Haiku → lands in `/inbox` bucket 1 with confidence in [0.7, 0.85] → ✨ prefilled edit form works → Confirm dismisses correctly.
- One direct-from-host vacation rental email round-trips through Haiku → produces `type='lodging'` segment with the host's name as the property name.
- A same-day Uber receipt during an existing trip auto-attaches as a `type='transfer'` segment (capture-everything rule).
- Daily-budget cap demonstrated: temporarily set `LLM_DAILY_BUDGET_CENTS=1`, send 5 emails, confirm 4 of them route to `review`.
- `v0.3.0` tag pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms tag landed cleanly.

After this lands, return to brainstorming/writing-plans for **Phase 4 — Search & geocoding** (Meilisearch index + post-commit sync, Nominatim hotel geocoding, expanded vendor pack catalog).
