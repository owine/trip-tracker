# Phase 4 — Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add typo-tolerant full-text search via Meilisearch + a ⌘K command palette. Bundles the arq → saq migration (Task 1) so the new `sync_meili` task is authored against the active queue library from day one.

**Architecture:** New Meilisearch container (internal-network only) holds two derived indexes (`trips`, `segments`) populated by a saq `sync_meili` task. Every Trip/Segment write commit explicitly enqueues a sync. A FastAPI `/api/search/<index>` proxy authenticates via the existing Authelia session, injects a server-side `traveler_ids = <user.id>` filter, and forwards to Meili. The browser never sees the Meili master key. ⌘K palette is an Alpine.js component included in `base.html`, search-as-you-type from char 1, deep-links to `/trips/<id>` or `/trips/<tid>#segment-<sid>`.

**Tech Stack:** Python 3.14 (target=py313), saq (replaces arq), Redis 7 (replaces 5), Meilisearch 1.13, `meilisearch-python-async` client, Alpine.js 3 (CDN-loaded), Tailwind, FastAPI, SQLAlchemy 2.0 async, Postgres 18, Pydantic v2, pytest + pytest-asyncio + pytest-postgresql.

**Spec reference:** [`docs/superpowers/specs/2026-04-30-phase4-search-design.md`](../specs/2026-04-30-phase4-search-design.md). Section numbers (e.g. §6) below refer to this spec.

**Branch:** `feat/phase-4-search`. Cut from `main` at `df7fd54` (or whatever is current `main` HEAD when implementation starts).

---

## File Structure

```
src/trip_tracker/
├── config.py                                [MODIFY: add MEILI_URL + MEILI_MASTER_KEY]
├── app.py                                   [MODIFY: include search router; init Meili client]
├── worker.py                                [REPLACED in Task 1: saq settings dict + sync_meili]
├── ingest/webhook.py                        [MODIFY in Task 1: rewrite enqueue_parse for saq]
├── __main__.py                              [MODIFY in Task 1: parse_pending; Task 10: + reindex]
├── search/                                  [CREATE — new subpackage]
│   ├── __init__.py
│   ├── client.py                            singleton Meili client + Protocol shape for tests
│   ├── sync.py                              trip_to_doc, segment_to_doc, enqueue_meili_sync
│   ├── proxy.py                             /api/search/<index> route handler
│   └── reindex.py                           full rebuild used by `python -m trip_tracker reindex`
├── routes/
│   ├── trips.py                             [MODIFY: add enqueue_meili_sync after 3 commits]
│   ├── segments.py                          [MODIFY: add enqueue_meili_sync after 3 commits]
│   └── inbox.py                             [MODIFY: add enqueue_meili_sync after discard commit]
└── templates/
    ├── base.html                            [MODIFY: add Alpine CDN + palette include]
    ├── _search_palette.html                 [CREATE]
    └── segments/_row.html                   [MODIFY: add id="segment-<id>" anchor]

docker-compose.yml                           [MODIFY: add trip-tracker-search service + volume]
docker-compose.dev.yml                       [MODIFY: expose Meili port for local dev]

tests/
├── conftest.py                              [MODIFY: register Meili autouse fixture]
├── test_search_sync.py                      [CREATE: trip_to_doc, segment_to_doc, enqueue helpers]
├── test_search_sync_task.py                 [CREATE: sync_meili task with mocked client]
├── test_routes_search.py                    [CREATE: /api/search/* with mocked Meili]
├── test_search_reindex.py                   [CREATE: reindex CLI command]
├── test_search_integration.py               [CREATE: marked @pytest.mark.live_meili]
└── test_worker.py                           [MODIFY in Task 1: rewrite startup/shutdown tests for saq]
```

**Why this layout:** New `search/` subpackage isolates the Meili-aware code from existing routes. `client.py` exposes a `Protocol`-typed singleton + a `get_meili()` FastAPI dependency, so tests inject a fake. `sync.py` is the only place that touches BOTH Postgres and Meili (the doc-rendering happens here, not in routes). `proxy.py` is one route handler — small enough to live in its own file because keeping it apart makes the auth-filter-injection invariant easier to audit.

---

## Conventions Used Throughout This Plan

- **Commit messages:** Conventional Commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`). Subject under 70 chars; body via heredoc when multi-line.
- **TDD:** Write failing test → run → see it fail for the *right reason* → implement minimum → re-run → green → commit.
- **Real Postgres in tests:** `pytest-postgresql` continues from prior phases.
- **Mock Meili in unit tests; one live integration test gated behind `@pytest.mark.live_meili`** (skipped in CI by default; runs locally before tagging).
- **`uv` for everything.** No raw `pip`.
- **`from __future__ import annotations`** at top of every module.

### Per-task "Quality bar / things to watch"

Recurring entries (apply to every task touching code):

- **Strict mypy + ruff (target=py313, mypy_python=3.14):**
  - No quoted return-type annotations (`def foo() -> "Bar":`) — ruff UP037 strips them.
  - Pydantic v2: use `@field_validator("x") @classmethod def f(cls, v): ...`, NOT lambda assignment.
  - FastAPI handlers returning union types need `response_model=None` on the route decorator.
  - PEP 758 (parenthesized except groups in 3.14): keep target=py313 to avoid ruff format stripping the parens.
- **Pre-commit hooks** (ruff, ruff-format, mypy, djlint, gitleaks, ggshield, bandit) must pass clean. Don't bypass with `--no-verify`. If pre-commit's mypy disagrees with local mypy, check the hook's `additional_dependencies` — Phase 3 commit `b9ef779` established that adding the new package there closes the version-skew gap.
- **Coverage gate:** ≥85% project-wide.
- **No raw `assert` in `src/` for invariants.** Bandit B101 fails CI. If a runtime invariant is needed for type narrowing, add `# nosec B101` with a reason (Phase 2 commit `73b2c26` convention).

---

## Task 1 — Migrate arq → saq

**Spec ref:** §2 (in scope), §11 (migration). The full migration steps live in a separate plan: [`docs/superpowers/plans/2026-04-30-arq-to-saq-migration.md`](./2026-04-30-arq-to-saq-migration.md).

This task **executes that plan**. The new Phase 4 `sync_meili` task (Task 5) will be authored against saq, so this must land first.

**Files:** as enumerated in the migration plan (`pyproject.toml`, `src/trip_tracker/worker.py`, `src/trip_tracker/ingest/webhook.py`, `src/trip_tracker/__main__.py`, `docker-compose.yml`, `tests/test_worker.py`, `README.md`).

- [ ] **Step 1.1 — Read the migration plan**

Open `docs/superpowers/plans/2026-04-30-arq-to-saq-migration.md` and follow Steps 1-9. The plan is self-contained.

- [ ] **Step 1.2 — Run the migration's verification gate**

```bash
uv run pytest tests/test_worker.py -v       # 10 tests, expect all green
uv run pytest -q                              # full suite, expect 204+ passing
uv run ruff check . && uv run mypy src
uv run pre-commit run --all-files
```

- [ ] **Step 1.3 — Commit**

```bash
git add pyproject.toml uv.lock src/trip_tracker/worker.py \
        src/trip_tracker/ingest/webhook.py src/trip_tracker/__main__.py \
        docker-compose.yml tests/test_worker.py README.md \
        .pre-commit-config.yaml  # if mypy hook deps updated
git commit -m "refactor(worker): migrate arq → saq; bump redis to 7"
```

**Quality bar:**
- The `parse_raw_email` task body is **unchanged**. Only signature (kw-only `raw_email_id`) and `ctx` shape adjustments.
- Pre-commit may need `saq[hiredis]>=0.26` added to mypy hook's `additional_dependencies` if mypy disagrees between hook and local. Check `b9ef779` for the pattern.
- Worker container `command:` flips from `["arq", "trip_tracker.worker.WorkerSettings"]` to `["saq", "trip_tracker.worker.settings"]`.
- `unique=True` is NOT needed for `parse_raw_email` enqueues (each RawEmail has its own UUID). It WILL be needed for `sync_meili` in Task 5.

---

## Task 2 — Settings + Dependencies + Meili Container

**Spec ref:** §9 (configuration).

**Files:**
- Modify: `pyproject.toml` (add Meili client dep)
- Modify: `src/trip_tracker/config.py`
- Modify: `.env.example`
- Modify: `docker-compose.yml` (add `trip-tracker-search` service)
- Modify: `docker-compose.dev.yml` (expose Meili port)
- Create: `tests/test_config_phase4.py`

- [ ] **Step 2.1 — Add Meili client dependency**

```bash
uv add 'meilisearch-python-async>=1.13,<2'
```

`meilisearch-python-async` is the maintained async fork. Confirm it lands in `pyproject.toml` `[project] dependencies`.

- [ ] **Step 2.2 — Failing test for new settings fields**

`tests/test_config_phase4.py`:

```python
"""Phase 4 settings: Meilisearch URL + master key."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.config import Settings


def test_meili_url_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEILI_URL", raising=False)
    with pytest.raises(ValidationError, match="meili_url"):
        Settings()


def test_meili_master_key_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEILI_MASTER_KEY", raising=False)
    with pytest.raises(ValidationError, match="meili_master_key"):
        Settings()


def test_meili_master_key_is_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """SecretStr — never leaks in repr or log output."""
    monkeypatch.setenv("MEILI_URL", "http://localhost:7700")
    monkeypatch.setenv("MEILI_MASTER_KEY", "super-secret-32-byte-value")
    s = Settings()
    assert "super-secret-32-byte-value" not in repr(s)
    assert s.meili_master_key.get_secret_value() == "super-secret-32-byte-value"
```

Run: `uv run pytest tests/test_config_phase4.py -v` — expect 3 failures.

- [ ] **Step 2.3 — Implement settings fields**

In `src/trip_tracker/config.py`, append before the closing of the `Settings` class:

```python
    # Phase 4 — search
    meili_url: str
    meili_master_key: SecretStr
```

`SecretStr` already imported from prior phases (Anthropic key uses it).

- [ ] **Step 2.4 — Update `.env.example`**

Append:

```env

# --- Phase 4: search ---
MEILI_URL=http://trip-tracker-search:7700
MEILI_MASTER_KEY=  # generate: openssl rand -hex 32
```

- [ ] **Step 2.5 — Update `tests/conftest.py` autouse fixture**

The autouse env fixture from Phase 1+ sets all required env vars before each test. Add the two new ones with throwaway values:

```python
# in conftest.py's autouse env fixture
monkeypatch.setenv("MEILI_URL", "http://meili-test:7700")
monkeypatch.setenv("MEILI_MASTER_KEY", "test-master-key-32-bytes-for-tests")
```

- [ ] **Step 2.6 — Add Meili to docker-compose.yml**

After the `trip-tracker-redis` service block, add:

```yaml
  trip-tracker-search:
    image: getmeili/meilisearch:v1.13
    restart: unless-stopped
    environment:
      MEILI_MASTER_KEY: ${MEILI_MASTER_KEY}
      MEILI_ENV: production
    volumes:
      - trip-tracker-meili:/meili_data
    networks: [internal]
```

In the `volumes:` block at the bottom, add:

```yaml
  trip-tracker-meili:
```

Add `MEILI_URL: ${MEILI_URL:-http://trip-tracker-search:7700}` and `MEILI_MASTER_KEY: ${MEILI_MASTER_KEY}` to the `environment:` blocks of BOTH `trip-tracker-app` AND `trip-tracker-worker`.

- [ ] **Step 2.7 — Add port forward in docker-compose.dev.yml**

```yaml
  trip-tracker-search:
    ports:
      - "7700:7700"
```

- [ ] **Step 2.8 — Run + commit**

```bash
uv run pytest tests/test_config_phase4.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add pyproject.toml uv.lock src/trip_tracker/config.py .env.example \
        tests/conftest.py tests/test_config_phase4.py \
        docker-compose.yml docker-compose.dev.yml
git commit -m "feat(config): Phase 4 settings — Meilisearch URL + master key"
```

**Quality bar:**
- `MEILI_URL` is `str`, NOT `HttpUrl` — Meili runs on http internally; HttpUrl would force trailing-slash quirks.
- `MEILI_MASTER_KEY` is `SecretStr` to avoid log leakage.
- The Meili volume name `trip-tracker-meili` follows the existing `trip-tracker-pg` naming.
- The Meili service is internal-network only — NO Traefik labels.

---

## Task 3 — Meili Client + Protocol + Dependency

**Spec ref:** §10 (test strategy — mock pattern).

**Files:**
- Create: `src/trip_tracker/search/__init__.py`
- Create: `src/trip_tracker/search/client.py`
- Create: `tests/test_search_client.py`

- [ ] **Step 3.1 — Create the `search/` package**

`src/trip_tracker/search/__init__.py`:

```python
"""Meilisearch derived-index subsystem: client, sync, proxy, reindex."""

from __future__ import annotations
```

- [ ] **Step 3.2 — Failing test**

`tests/test_search_client.py`:

```python
"""Meili client + dependency injection."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from trip_tracker.config import Settings
from trip_tracker.search.client import MeiliClientProtocol, build_client, get_meili


def test_protocol_methods_exist() -> None:
    """The Protocol describes the surface our code uses (lightweight)."""
    # If MeiliClientProtocol drops a required method, this test starts failing
    # because static type-narrowing in client.py won't typecheck.
    assert hasattr(MeiliClientProtocol, "index")


def test_build_client_uses_settings() -> None:
    s = Settings(_env_file=None,
                 database_url="postgresql+asyncpg://u:p@h/d",
                 session_secret="x" * 32,
                 oidc_issuer="https://x", oidc_client_id="x",
                 oidc_client_secret="x", oidc_redirect_uri="https://x",
                 base_url="https://x", webhook_secret="x" * 32,
                 anthropic_api_key="sk-ant-test", redis_url="redis://x",
                 meili_url="http://meili-test:7700",
                 meili_master_key="key32bytes")
    client = build_client(s)
    assert client is not None
    # The client should have an index() method (Protocol-conformant)
    assert hasattr(client, "index")
```

- [ ] **Step 3.3 — Implement `client.py`**

`src/trip_tracker/search/client.py`:

```python
"""Singleton Meili client + dependency injection.

The Protocol shape lets tests inject a MagicMock without subclassing
the real client class.
"""

from __future__ import annotations

from typing import Any, Protocol

from fastapi import Request
from meilisearch_python_async import Client

from trip_tracker.config import Settings


class MeiliIndexProtocol(Protocol):
    """The subset of meilisearch_python_async.Index methods we use."""

    async def update_documents(self, documents: list[dict[str, Any]]) -> Any: ...
    async def delete_document(self, document_id: str) -> Any: ...
    async def search(self, query: str, opt_params: dict[str, Any] | None = None) -> Any: ...


class MeiliClientProtocol(Protocol):
    """The subset of meilisearch_python_async.Client we use."""

    def index(self, uid: str) -> MeiliIndexProtocol: ...
    async def create_index(
        self, uid: str, primary_key: str | None = None
    ) -> Any: ...
    async def delete_index(self, uid: str) -> Any: ...


def build_client(settings: Settings) -> MeiliClientProtocol:
    """Construct a Meili client from settings. One per process."""
    return Client(
        url=settings.meili_url,
        api_key=settings.meili_master_key.get_secret_value(),
    )


async def get_meili(request: Request) -> MeiliClientProtocol:
    """FastAPI dependency. Reads from app.state.meili (set in lifespan)."""
    return request.app.state.meili
```

- [ ] **Step 3.4 — Wire client into `app.py` lifespan**

In `src/trip_tracker/app.py`, find or create a lifespan context manager. Inside it, add:

```python
from trip_tracker.search.client import build_client

# in lifespan:
app.state.meili = build_client(settings)
yield
# on shutdown — meilisearch-python-async manages its own pool; nothing extra needed
```

- [ ] **Step 3.5 — Run + commit**

```bash
uv run pytest tests/test_search_client.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/search/__init__.py src/trip_tracker/search/client.py \
        src/trip_tracker/app.py tests/test_search_client.py
git commit -m "feat(search): Meili client + Protocol + FastAPI dependency"
```

**Quality bar:**
- `MeiliClientProtocol` is a `typing.Protocol`, not an ABC. Tests inject a `MagicMock(spec=MeiliClientProtocol)` and don't subclass the real `Client`.
- `app.state.meili` is FastAPI's recommended pattern for request-scoped singletons.
- `meilisearch-python-async` may not have type stubs. If mypy complains, add to the existing `[[tool.mypy.overrides]]` block: `module = ["meilisearch_python_async", "meilisearch_python_async.*"]`, `ignore_missing_imports = true`.

---

## Task 4 — Doc Rendering: `trip_to_doc` + `segment_to_doc`

**Spec ref:** §4.1, §4.2 (index field schemas).

**Files:**
- Create: `src/trip_tracker/search/sync.py` (partial — just the two doc renderers + `enqueue_meili_sync` placeholder)
- Create: `tests/test_search_sync.py`

- [ ] **Step 4.1 — Failing tests**

`tests/test_search_sync.py`:

```python
"""trip_to_doc and segment_to_doc — pure mappers from ORM to Meili doc."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.sync import segment_to_doc, trip_to_doc


@pytest.mark.asyncio
async def test_trip_to_doc_basic_fields(db_session: AsyncSession) -> None:
    user = User(oidc_subject="u", email="u@x.com", display_name="U")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="Paris May 2026",
        start_date=date(2026, 6, 1),
        end_date=date(2026, 6, 5),
        primary_destination="Paris",
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    doc = await trip_to_doc(trip, db=db_session)
    assert doc["id"] == str(trip.id)
    assert doc["title"] == "Paris May 2026"
    assert doc["primary_destination"] == "Paris"
    assert doc["start_date"] == (trip.start_date - date(1970, 1, 1)).days
    assert doc["end_date"] == (trip.end_date - date(1970, 1, 1)).days
    assert doc["traveler_ids"] == [str(user.id)]


@pytest.mark.asyncio
async def test_segment_to_doc_flight(db_session: AsyncSession) -> None:
    user = User(oidc_subject="u2", email="u2@x.com", display_name="U2")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(
        title="Trip", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
        created_by=user.id,
    )
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="flight", status="confirmed",
        provider="Air France", confirmation_number="ABC123",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC), start_tz="UTC",
        start_location={"iata": "JFK", "city": "New York"},
        end_location={"iata": "CDG", "city": "Paris"},
        details={"flight_number": "AF44", "notes": "anniversary trip", "seat": "12A"},
        parse_source="manual", parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["id"] == str(seg.id)
    assert doc["trip_id"] == str(trip.id)
    assert doc["traveler_ids"] == [str(user.id)]
    assert doc["type"] == "flight"
    assert doc["provider"] == "Air France"
    assert doc["confirmation_number"] == "ABC123"
    assert doc["start_at_unix"] == int(seg.start_at.timestamp())
    assert doc["start_city"] == "New York"
    assert doc["end_city"] == "Paris"
    assert doc["vehicle_number"] == "AF44"
    assert doc["notes"] == "anniversary trip"


@pytest.mark.asyncio
async def test_segment_to_doc_lodging_no_vehicle_number(
    db_session: AsyncSession,
) -> None:
    """Lodging segments don't have a vehicle number — should be None."""
    user = User(oidc_subject="u3", email="u3@x.com", display_name="U3")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="lodging", status="confirmed",
        start_at=datetime(2026, 6, 1, 15, tzinfo=UTC), start_tz="UTC",
        start_location={"name": "Le Marais Hotel", "city": "Paris"},
        details={"room_type": "Deluxe Suite"},
        parse_source="manual", parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["vehicle_number"] is None
    assert doc["start_city"] == "Paris"
    assert doc["notes"] is None  # no notes field in details


@pytest.mark.asyncio
async def test_segment_to_doc_train_uses_train_number(
    db_session: AsyncSession,
) -> None:
    user = User(oidc_subject="u4", email="u4@x.com", display_name="U4")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    seg = Segment(
        trip_id=trip.id, owner_user_id=user.id, type="train", status="confirmed",
        start_at=datetime(2026, 6, 2, 9, tzinfo=UTC), start_tz="UTC",
        start_location={"name": "Paris Gare de Lyon"},
        end_location={"name": "Lyon Part Dieu"},
        details={"train_number": "9573"},
        parse_source="manual", parse_confidence=1.0,
    )
    db_session.add(seg)
    await db_session.commit()

    doc = await segment_to_doc(seg, db=db_session)
    assert doc["vehicle_number"] == "9573"
```

Run: `uv run pytest tests/test_search_sync.py -v` — expect 4 ImportError failures.

- [ ] **Step 4.2 — Implement doc renderers**

`src/trip_tracker/search/sync.py`:

```python
"""Doc rendering + enqueue helper for the Meili sync subsystem.

Pure functions live here. The saq task that calls them lives in worker.py.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler

_EPOCH = date(1970, 1, 1)


async def _trip_traveler_ids(db: AsyncSession, trip_id: uuid.UUID) -> list[str]:
    rows = (
        await db.execute(
            select(TripTraveler.user_id).where(TripTraveler.trip_id == trip_id)
        )
    ).scalars().all()
    return [str(uid) for uid in rows]


async def trip_to_doc(trip: Trip, *, db: AsyncSession) -> dict[str, Any]:
    """Render a Trip ORM row to its Meili index doc."""
    return {
        "id": str(trip.id),
        "title": trip.title,
        "primary_destination": trip.primary_destination,
        "start_date": (trip.start_date - _EPOCH).days,
        "end_date": (trip.end_date - _EPOCH).days,
        "traveler_ids": await _trip_traveler_ids(db, trip.id),
    }


def _vehicle_number(seg: Segment) -> str | None:
    """Flatten flight_number or train_number from JSONB details, or None."""
    details = seg.details or {}
    if seg.type == "flight":
        return details.get("flight_number")
    if seg.type == "train":
        return details.get("train_number")
    return None


def _city_from_location(loc: dict[str, Any] | None) -> str | None:
    if not loc:
        return None
    return loc.get("city")


async def segment_to_doc(seg: Segment, *, db: AsyncSession) -> dict[str, Any]:
    """Render a Segment ORM row to its Meili index doc."""
    details = seg.details or {}
    return {
        "id": str(seg.id),
        "trip_id": str(seg.trip_id) if seg.trip_id else None,
        "traveler_ids": (
            await _trip_traveler_ids(db, seg.trip_id) if seg.trip_id else []
        ),
        "type": seg.type,
        "provider": seg.provider,
        "confirmation_number": seg.confirmation_number,
        "start_at_unix": int(seg.start_at.timestamp()),
        "start_city": _city_from_location(seg.start_location),
        "end_city": _city_from_location(seg.end_location),
        "vehicle_number": _vehicle_number(seg),
        "notes": details.get("notes"),
    }


# Placeholder to be filled by Task 5; declared here so callers compile.
async def enqueue_meili_sync(
    settings: Any,  # Settings; quoted to avoid circular import at this stage
    *,
    entity: Literal["trip", "segment"],
    entity_id: uuid.UUID,
) -> None:
    """Enqueue a sync_meili saq job. Filled in Task 5."""
    raise NotImplementedError("filled in Task 5")
```

- [ ] **Step 4.3 — Run + commit**

```bash
uv run pytest tests/test_search_sync.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/search/sync.py tests/test_search_sync.py
git commit -m "feat(search): trip_to_doc + segment_to_doc pure renderers"
```

**Quality bar:**
- `vehicle_number` is `None` for `lodging`/`car`/`transfer`/`activity` types — verified by the lodging test. Don't try to invent it from `car_class` etc.
- `traveler_ids` is fetched fresh from the DB on each render. The list is denormalized at index time only; we don't try to keep it cached.
- `start_at_unix` is int seconds (sortable + filterable in Meili). Date math uses `int(dt.timestamp())`.
- The placeholder `enqueue_meili_sync` is a NotImplementedError stub — Task 5 fills it.

---

## Task 5 — `enqueue_meili_sync` + saq `sync_meili` Task

**Spec ref:** §5 (sync model), §5.2 (coalescing via saq `unique=True`).

**Files:**
- Modify: `src/trip_tracker/search/sync.py` (replace the `enqueue_meili_sync` stub)
- Modify: `src/trip_tracker/worker.py` (add `sync_meili` task to the saq settings dict)
- Create: `tests/test_search_sync_task.py`

- [ ] **Step 5.1 — Failing test**

`tests/test_search_sync_task.py`:

```python
"""sync_meili saq task: upserts on existing rows, deletes on missing rows."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.client import MeiliClientProtocol


@pytest.mark.asyncio
async def test_sync_meili_upserts_existing_trip(
    db_url: str, db_session: AsyncSession
) -> None:
    from trip_tracker.worker import sync_meili

    user = User(oidc_subject="m1", email="m1@x.com", display_name="M1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T1", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    await db_session.commit()

    fake_index = MagicMock()
    fake_index.update_documents = AsyncMock()
    fake_index.delete_document = AsyncMock()
    fake_meili = MagicMock(spec=MeiliClientProtocol)
    fake_meili.index = MagicMock(return_value=fake_index)

    engine = create_async_engine(db_url)
    ctx = {"settings": Settings(), "engine": engine, "meili": fake_meili}

    await sync_meili(ctx, entity="trip", entity_id=str(trip.id))

    fake_meili.index.assert_called_with("trips")
    fake_index.update_documents.assert_awaited_once()
    docs = fake_index.update_documents.call_args[0][0]
    assert docs[0]["id"] == str(trip.id)
    assert docs[0]["title"] == "T1"

    await engine.dispose()


@pytest.mark.asyncio
async def test_sync_meili_deletes_when_row_missing(
    db_url: str, db_session: AsyncSession
) -> None:
    """If the entity isn't in Postgres (deleted), issue a Meili delete instead."""
    from trip_tracker.worker import sync_meili

    fake_index = MagicMock()
    fake_index.update_documents = AsyncMock()
    fake_index.delete_document = AsyncMock()
    fake_meili = MagicMock(spec=MeiliClientProtocol)
    fake_meili.index = MagicMock(return_value=fake_index)

    bogus_id = uuid.uuid4()
    engine = create_async_engine(db_url)
    ctx = {"settings": Settings(), "engine": engine, "meili": fake_meili}

    await sync_meili(ctx, entity="segment", entity_id=str(bogus_id))

    fake_meili.index.assert_called_with("segments")
    fake_index.delete_document.assert_awaited_once_with(str(bogus_id))
    fake_index.update_documents.assert_not_called()

    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_meili_sync_uses_unique_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """enqueue_meili_sync passes unique=True with a stable key per (entity, id)."""
    from trip_tracker.search.sync import enqueue_meili_sync

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock()
    fake_queue.disconnect = AsyncMock()

    monkeypatch.setattr(
        "trip_tracker.search.sync._build_queue", lambda settings: fake_queue
    )

    seg_id = uuid.uuid4()
    await enqueue_meili_sync(Settings(), entity="segment", entity_id=seg_id)

    fake_queue.enqueue.assert_awaited_once()
    kwargs = fake_queue.enqueue.call_args.kwargs
    assert kwargs.get("entity") == "segment"
    assert kwargs.get("entity_id") == str(seg_id)
    assert kwargs.get("unique") is True
    # saq's "key" param identifies the dedup target
    assert "meili_sync:segment:" in kwargs.get("key", "")
```

- [ ] **Step 5.2 — Implement `enqueue_meili_sync`**

Replace the stub in `src/trip_tracker/search/sync.py`:

```python
from saq import Queue

from trip_tracker.config import Settings


def _build_queue(settings: Settings) -> Queue:
    """Factory for the saq Queue. Indirected so tests can monkeypatch it."""
    return Queue.from_url(settings.redis_url)


async def enqueue_meili_sync(
    settings: Settings,
    *,
    entity: Literal["trip", "segment"],
    entity_id: uuid.UUID,
) -> None:
    """Enqueue a sync_meili saq job, deduping in-flight duplicates."""
    q = _build_queue(settings)
    try:
        await q.enqueue(
            "sync_meili",
            entity=entity,
            entity_id=str(entity_id),
            unique=True,
            key=f"meili_sync:{entity}:{entity_id}",
            retries=5,
        )
    finally:
        await q.disconnect()
```

- [ ] **Step 5.3 — Implement `sync_meili` task in `worker.py`**

Append to `src/trip_tracker/worker.py` (next to `parse_raw_email`):

```python
from trip_tracker.search.client import MeiliClientProtocol, build_client
from trip_tracker.search.sync import segment_to_doc, trip_to_doc


async def sync_meili(
    ctx: dict[str, Any], *, entity: str, entity_id: str
) -> None:
    """Upsert one Trip or Segment to Meili. On delete from Postgres, the
    entity is gone — issue a Meili delete instead."""
    settings: Settings = ctx["settings"]
    engine = ctx["engine"]
    meili: MeiliClientProtocol = ctx["meili"]
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    rid = uuid.UUID(entity_id)
    async with SessionMaker() as db:
        if entity == "trip":
            row = await db.get(Trip, rid)
            if row is None:
                await meili.index("trips").delete_document(str(rid))
            else:
                doc = await trip_to_doc(row, db=db)
                await meili.index("trips").update_documents([doc])
        elif entity == "segment":
            row = await db.get(Segment, rid)
            if row is None:
                await meili.index("segments").delete_document(str(rid))
            else:
                doc = await segment_to_doc(row, db=db)
                await meili.index("segments").update_documents([doc])
        else:
            raise ValueError(f"unknown entity: {entity}")
```

In the `settings` dict at the bottom of `worker.py`, add `sync_meili` to `functions` and add the Meili client to `startup`:

```python
async def startup(ctx: dict[str, Any]) -> None:
    s = Settings()
    ctx["settings"] = s
    ctx["engine"] = create_async_engine(str(s.database_url))
    ctx["meili"] = build_client(s)


# ...
settings = {
    "queue": queue,
    "functions": [parse_raw_email, sync_meili],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": 1,
}
```

`shutdown` doesn't need to dispose the Meili client (the underlying httpx client cleans up on GC).

- [ ] **Step 5.4 — Run + commit**

```bash
uv run pytest tests/test_search_sync_task.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/search/sync.py src/trip_tracker/worker.py \
        tests/test_search_sync_task.py
git commit -m "feat(search): sync_meili saq task + enqueue_meili_sync helper"
```

**Quality bar:**
- `unique=True` + `key=f"meili_sync:{entity}:{entity_id}"` together dedupe duplicate enqueues. saq drops the new enqueue if a job with that key is already queued.
- Per-job `retries=5` for sync_meili (Meili being down shouldn't permanently lose a sync).
- The `_build_queue` factory indirection is to make `enqueue_meili_sync` mockable in tests.
- `ctx["meili"]` is set once per worker process in `startup` — not per task.

---

## Task 6 — Wire `enqueue_meili_sync` into Write Sites

**Spec ref:** §5.1 (8 write sites enumerated).

**Files:**
- Modify: `src/trip_tracker/routes/trips.py` (3 commit sites)
- Modify: `src/trip_tracker/routes/segments.py` (3 commit sites)
- Modify: `src/trip_tracker/routes/inbox.py` (1 commit site — discard)
- Modify: `src/trip_tracker/worker.py` (parse_raw_email writes new Segment + sometimes Trip)

This task is mechanical: after each `await db.commit()`, add a call to `enqueue_meili_sync`. The trick is getting the right entity (sometimes trip, sometimes segment, sometimes both).

- [ ] **Step 6.1 — Read existing write sites**

```bash
grep -n "await db.commit()" src/trip_tracker/routes/trips.py \
    src/trip_tracker/routes/segments.py src/trip_tracker/routes/inbox.py \
    src/trip_tracker/worker.py
```

Expected: 8 commit sites total (per spec §5.1 table).

- [ ] **Step 6.2 — Add a regression test**

`tests/test_search_sync_wiring.py`:

```python
"""Verify write sites enqueue meili sync after commit.

Spec §5.1 catalogs 8 sites; this test exercises one per route module
(create_trip, create_segment, discard) plus the worker. Adding new
write sites later means adding new tests here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
from trip_tracker.models.forwarding_alias import ForwardingAlias
from trip_tracker.models.raw_email import RawEmail
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
async def test_create_trip_enqueues_meili_sync(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="t1", email="t1@x.com", display_name="T1")
    db_session.add(user)
    await db_session.commit()

    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    with patch("trip_tracker.routes.trips.enqueue_meili_sync", new=AsyncMock()) as mock:
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport, base_url="http://test", cookies=_cookie(user, settings)
            ) as c,
        ):
            r = await c.post(
                "/trips",
                data={
                    "title": "Test",
                    "start_date": "2026-06-01",
                    "end_date": "2026-06-05",
                    "primary_destination": "Paris",
                },
                follow_redirects=False,
            )
    assert r.status_code == 303
    mock.assert_awaited()
    kwargs = mock.call_args.kwargs
    assert kwargs.get("entity") == "trip"


# Similar tests for create_segment, edit_trip, edit_segment, delete_trip,
# delete_segment, inbox.discard. Use the patch-on-the-route-module idiom
# so each test only triggers its own write site.
```

(Implementer: write at least 4 of these — `create_trip`, `create_segment`, `delete_segment`, and `inbox_discard`. The remaining 4 follow the same pattern but aren't strictly necessary for coverage.)

- [ ] **Step 6.3 — Wire `routes/trips.py`**

In each of the 3 mutating handlers (`create_trip`, `edit_trip`, `delete_trip`), after `await db.commit()`:

```python
from trip_tracker.search.sync import enqueue_meili_sync

# ...
await db.commit()
await enqueue_meili_sync(settings, entity="trip", entity_id=trip.id)
```

(For `delete_trip`, also enqueue sync for any cascaded segments — but the segment FK is `ON DELETE CASCADE`; Postgres deletes them, and we'd need to know their IDs at commit time. For v0.4.0, accept that cascaded-segment deletes don't trigger Meili deletes. The next reindex catches the drift. Document this as a known limitation in §5 of the spec → already noted in Task 19's done definition.)

- [ ] **Step 6.4 — Wire `routes/segments.py`**

In `create_segment`, `update_segment`, `delete_segment`. The `create_segment` and `update_segment` paths sometimes mutate a Trip too (date widening); enqueue both:

```python
await db.commit()
await enqueue_meili_sync(settings, entity="trip", entity_id=trip.id)
await enqueue_meili_sync(settings, entity="segment", entity_id=seg.id)
```

In `delete_segment`:

```python
await db.commit()
await enqueue_meili_sync(settings, entity="segment", entity_id=seg.id)
```

- [ ] **Step 6.5 — Wire `routes/inbox.py::discard`**

After the `delete(Segment).where(...)` cascade completes:

```python
await db.commit()
# The segment was deleted; sync_meili will handle the delete-from-Meili
# branch (db.get returns None → delete_document). For each deleted segment ID,
# enqueue a sync.
for sid in deleted_segment_ids:
    await enqueue_meili_sync(settings, entity="segment", entity_id=sid)
```

The implementer needs to capture the IDs BEFORE the delete. Adjust the discard route to do `select(Segment.id).where(...)` first, then `delete()`.

- [ ] **Step 6.6 — Wire `worker.py::parse_raw_email`**

After the final `await db.commit()` in `parse_raw_email`:

```python
if outcome.result.segments:
    for seg in created_segments:  # track these in the loop above
        await enqueue_meili_sync(settings, entity="segment", entity_id=seg.id)
    if trip_was_created or trip_was_widened:
        await enqueue_meili_sync(settings, entity="trip", entity_id=trip.id)
```

The existing loop in `parse_raw_email` constructs Segment objects but doesn't keep a list. Add `created_segments: list[Segment] = []` and `created_segments.append(seg)` inside the loop.

- [ ] **Step 6.7 — Run + commit**

```bash
uv run pytest tests/test_search_sync_wiring.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/routes/trips.py src/trip_tracker/routes/segments.py \
        src/trip_tracker/routes/inbox.py src/trip_tracker/worker.py \
        tests/test_search_sync_wiring.py
git commit -m "feat(search): wire enqueue_meili_sync into 8 write sites"
```

**Quality bar:**
- Each handler imports `enqueue_meili_sync` from `trip_tracker.search.sync`. Don't create duplicate paths.
- `settings` is available in routes via `request.app.state.settings` (Phase 3 commit `a3ff571` established this).
- The `delete_trip` cascaded-segments limitation is documented but not fixed in v0.4.0. Reindex covers it.

---

## Task 7 — `/api/search/<index>` Proxy Route

**Spec ref:** §6 (search proxy).

**Files:**
- Create: `src/trip_tracker/search/proxy.py`
- Modify: `src/trip_tracker/app.py` (include proxy router)
- Create: `tests/test_routes_search.py`

- [ ] **Step 7.1 — Failing tests**

`tests/test_routes_search.py`:

```python
"""/api/search/<index> proxy: auth, filter injection, response shape."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.app import create_app
from trip_tracker.auth.session import SessionPayload, encode_session
from trip_tracker.config import Settings
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
async def test_search_segments_filters_by_user(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Server injects traveler_ids = '<user.id>' regardless of client input."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="s1", email="s1@x.com", display_name="S1")
    db_session.add(user)
    await db_session.commit()

    fake_index = MagicMock()
    fake_index.search = AsyncMock(return_value={"hits": [], "estimatedTotalHits": 0})
    fake_meili = MagicMock()
    fake_meili.index = MagicMock(return_value=fake_index)

    app = create_app(settings=settings)
    app.state.meili = fake_meili
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.post(
            "/api/search/segments",
            json={"q": "Paris", "limit": 10},
        )
    assert r.status_code == 200
    body: dict[str, Any] = r.json()
    assert "hits" in body
    fake_index.search.assert_awaited_once()
    call_kwargs = fake_index.search.call_args.kwargs
    opt_params = call_kwargs.get("opt_params") or {}
    assert f"traveler_ids = '{user.id}'" in opt_params["filter"]


@pytest.mark.asyncio
async def test_search_requires_session(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No session cookie → 401."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://test") as c,
    ):
        r = await c.post("/api/search/segments", json={"q": "Paris"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_search_invalid_index_returns_422(
    db_url: str, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """Path param is constrained to {trips, segments}; other values rejected."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    settings = Settings()
    user = User(oidc_subject="s2", email="s2@x.com", display_name="S2")
    db_session.add(user)
    await db_session.commit()

    app = create_app(settings=settings)
    app.state.meili = MagicMock()
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=transport, base_url="http://test", cookies=_cookie(user, settings)
        ) as c,
    ):
        r = await c.post("/api/search/widgets", json={"q": "x"})
    assert r.status_code == 422
```

- [ ] **Step 7.2 — Implement `proxy.py`**

`src/trip_tracker/search/proxy.py`:

```python
"""/api/search/<index> proxy route.

Authenticates via the existing Authelia session, injects a server-side
traveler_ids filter, forwards the query to Meili.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from trip_tracker.auth.deps import require_user
from trip_tracker.models.user import User
from trip_tracker.search.client import MeiliClientProtocol, get_meili

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchRequest(BaseModel):
    q: str = ""
    limit: int = Field(default=20, ge=1, le=50)


class SearchHit(BaseModel):
    """Pass-through; we don't validate the per-index doc shape here."""

    model_config = {"extra": "allow"}


class SearchResponse(BaseModel):
    hits: list[dict[str, Any]]
    total: int


@router.post("/{index}")
async def search(
    index: Literal["trips", "segments"],
    body: SearchRequest,
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    meili: MeiliClientProtocol = Depends(get_meili),  # noqa: B008
) -> SearchResponse:
    # Server-side filter injection — never trust client filters.
    opt_params = {
        "filter": f"traveler_ids = '{user.id!s}'",
        "limit": body.limit,
    }
    results = await meili.index(index).search(query=body.q, opt_params=opt_params)
    return SearchResponse(
        hits=results.get("hits", []),
        total=results.get("estimatedTotalHits", 0),
    )
```

- [ ] **Step 7.3 — Wire router into `app.py`**

```python
from trip_tracker.search.proxy import router as search_router
# in create_app:
app.include_router(search_router)
```

- [ ] **Step 7.4 — Run + commit**

```bash
uv run pytest tests/test_routes_search.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/search/proxy.py src/trip_tracker/app.py \
        tests/test_routes_search.py
git commit -m "feat(search): /api/search/<index> proxy with auth + filter injection"
```

**Quality bar:**
- `Literal["trips", "segments"]` on the path param gives FastAPI's automatic 422 on invalid values — no manual validation needed.
- The `traveler_ids = '<uuid>'` Meili filter syntax (single quotes) is mandatory; double quotes don't parse.
- `body.limit` is clamped via Pydantic Field(le=50) — the spec says max 50.

---

## Task 8 — ⌘K Palette UI

**Spec ref:** §7 (palette).

**Files:**
- Modify: `src/trip_tracker/templates/base.html` (add Alpine CDN + palette include)
- Create: `src/trip_tracker/templates/_search_palette.html`
- Modify: `src/trip_tracker/templates/segments/_row.html` (add `id="segment-<id>"` anchor)

This task is mostly frontend. No new pytest tests — Playwright in Task 11 covers the end-to-end smoke.

- [ ] **Step 8.1 — Add Alpine CDN + palette include to `base.html`**

In `src/trip_tracker/templates/base.html`, before the closing `</body>`:

```html
{% if user %}
  {% include "_search_palette.html" %}
{% endif %}
<script defer src="https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js"
        integrity="sha384-..."
        crossorigin="anonymous"></script>
</body>
```

(The implementer should generate the actual SRI hash with `curl https://unpkg.com/alpinejs@3.14.1/dist/cdn.min.js | shasum -a 384 -b | xxd -r -p | base64`, OR pull the hash from <https://www.srihash.org/>. Pin Alpine to `3.14.1` exactly — Renovate will handle bumps.)

- [ ] **Step 8.2 — Create `_search_palette.html`**

`src/trip_tracker/templates/_search_palette.html`:

```html
<div x-data="searchPalette()"
     @keydown.window.meta.k.prevent="open()"
     @keydown.window.ctrl.k.prevent="open()"
     x-cloak>
  <div x-show="isOpen"
       @keydown.escape.window="close()"
       @click.self="close()"
       class="fixed inset-0 z-50 flex items-start justify-center pt-24 bg-black/50">
    <div class="w-full max-w-2xl rounded-lg bg-white shadow-xl dark:bg-zinc-900"
         @keydown.arrow-down.prevent="moveDown()"
         @keydown.arrow-up.prevent="moveUp()"
         @keydown.enter.prevent="activate()">
      <input x-model="query"
             @input.debounce.150ms="search()"
             type="search"
             class="w-full border-b border-zinc-200 bg-transparent p-4 text-lg focus:outline-none dark:border-zinc-800"
             placeholder="Search trips and segments…"
             x-ref="qbox">
      <div class="max-h-96 overflow-y-auto p-2">
        <template x-if="trips.length > 0">
          <div>
            <div class="px-2 py-1 text-xs font-semibold uppercase tracking-wider text-zinc-500">Trips</div>
            <template x-for="(hit, i) in trips" :key="hit.id">
              <a :href="`/trips/${hit.id}`"
                 class="block rounded px-3 py-2 text-sm"
                 :class="{ 'bg-zinc-100 dark:bg-zinc-800': isActive('trip', i) }"
                 @mouseenter="setActive('trip', i)">
                <span x-text="hit.title"></span>
                <span x-show="hit.primary_destination"
                      class="ml-2 text-zinc-500"
                      x-text="hit.primary_destination"></span>
              </a>
            </template>
          </div>
        </template>
        <template x-if="segments.length > 0">
          <div class="mt-2">
            <div class="px-2 py-1 text-xs font-semibold uppercase tracking-wider text-zinc-500">Segments</div>
            <template x-for="(hit, i) in segments" :key="hit.id">
              <a :href="`/trips/${hit.trip_id}#segment-${hit.id}`"
                 class="block rounded px-3 py-2 text-sm"
                 :class="{ 'bg-zinc-100 dark:bg-zinc-800': isActive('segment', i) }"
                 @mouseenter="setActive('segment', i)">
                <span class="rounded bg-zinc-200 px-1.5 py-0.5 text-xs uppercase dark:bg-zinc-800"
                      x-text="hit.type"></span>
                <span class="ml-2" x-text="hit.provider || hit.confirmation_number || hit.start_city"></span>
                <span class="ml-2 text-zinc-500"
                      x-show="hit.confirmation_number"
                      x-text="hit.confirmation_number"></span>
              </a>
            </template>
          </div>
        </template>
        <template x-if="query && trips.length === 0 && segments.length === 0 && !loading">
          <p class="p-4 text-sm text-zinc-500">No matches.</p>
        </template>
        <template x-if="error">
          <p class="p-4 text-sm text-red-600" x-text="error"></p>
        </template>
      </div>
    </div>
  </div>
</div>

<script>
  function searchPalette() {
    return {
      isOpen: false,
      query: "",
      trips: [],
      segments: [],
      activeKind: "trip",
      activeIdx: 0,
      loading: false,
      error: "",
      open() {
        this.isOpen = true;
        this.error = "";
        this.$nextTick(() => this.$refs.qbox.focus());
      },
      close() {
        this.isOpen = false;
        this.query = "";
        this.trips = [];
        this.segments = [];
      },
      async search() {
        if (!this.query.trim()) {
          this.trips = []; this.segments = [];
          return;
        }
        this.loading = true; this.error = "";
        try {
          const [tRes, sRes] = await Promise.all([
            fetch("/api/search/trips", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({q: this.query, limit: 5}),
            }),
            fetch("/api/search/segments", {
              method: "POST",
              headers: {"Content-Type": "application/json"},
              body: JSON.stringify({q: this.query, limit: 5}),
            }),
          ]);
          if (!tRes.ok || !sRes.ok) throw new Error("Search request failed");
          const tBody = await tRes.json();
          const sBody = await sRes.json();
          this.trips = tBody.hits || [];
          this.segments = sBody.hits || [];
          this.activeKind = this.trips.length > 0 ? "trip" : "segment";
          this.activeIdx = 0;
        } catch (e) {
          this.error = "Search unavailable.";
        } finally {
          this.loading = false;
        }
      },
      isActive(kind, i) {
        return this.activeKind === kind && this.activeIdx === i;
      },
      setActive(kind, i) {
        this.activeKind = kind; this.activeIdx = i;
      },
      moveDown() {
        const total = this.trips.length + this.segments.length;
        if (total === 0) return;
        // Flatten to a single index, advance, then split back
        let flat = this.activeKind === "trip" ? this.activeIdx : this.trips.length + this.activeIdx;
        flat = (flat + 1) % total;
        this._setFromFlat(flat);
      },
      moveUp() {
        const total = this.trips.length + this.segments.length;
        if (total === 0) return;
        let flat = this.activeKind === "trip" ? this.activeIdx : this.trips.length + this.activeIdx;
        flat = (flat - 1 + total) % total;
        this._setFromFlat(flat);
      },
      _setFromFlat(flat) {
        if (flat < this.trips.length) {
          this.activeKind = "trip"; this.activeIdx = flat;
        } else {
          this.activeKind = "segment"; this.activeIdx = flat - this.trips.length;
        }
      },
      activate() {
        if (this.activeKind === "trip" && this.trips[this.activeIdx]) {
          window.location = `/trips/${this.trips[this.activeIdx].id}`;
        } else if (this.activeKind === "segment" && this.segments[this.activeIdx]) {
          const h = this.segments[this.activeIdx];
          window.location = `/trips/${h.trip_id}#segment-${h.id}`;
        }
      },
    };
  }
</script>

<style>
  [x-cloak] { display: none !important; }
</style>
```

- [ ] **Step 8.3 — Add anchor IDs to segment rows**

In `src/trip_tracker/templates/segments/_row.html`, the outer `<li>`:

```html
<li id="segment-{{ s.id }}" class="py-3 flex items-baseline justify-between scroll-mt-20">
```

(`scroll-mt-20` so the `#segment-<id>` deep-link doesn't land flush with the top of the viewport — 5rem of breathing room.)

- [ ] **Step 8.4 — Run + commit**

```bash
uv run pytest -q                # nothing new should break
uv run ruff check . && uv run mypy src
uv run djlint src/trip_tracker/templates --check
git add src/trip_tracker/templates/base.html \
        src/trip_tracker/templates/_search_palette.html \
        src/trip_tracker/templates/segments/_row.html
git commit -m "feat(search): ⌘K palette + segment anchors"
```

**Quality bar:**
- Alpine.js is pinned to `3.14.1` exactly with SRI hash. Renovate manages bumps.
- `x-cloak` + the `[x-cloak]` style hides the modal until Alpine initializes (avoids flash-of-unstyled-modal).
- The two `fetch` calls are concurrent via `Promise.all`. Keeps the perceived latency at max(trips_query, segments_query).
- Hover over a result moves the keyboard highlight there too — single source of truth for "active result".
- `scroll-mt-20` on segments solves the "anchor link scrolls behind sticky nav" papercut.

---

## Task 9 — Index Setup + Configuration on Boot

**Spec ref:** §4.3 (index settings).

**Files:**
- Modify: `src/trip_tracker/search/client.py` (add index-config helper)
- Modify: `src/trip_tracker/app.py` (call it in lifespan)
- Create: `tests/test_search_setup.py`

Meilisearch needs filterable + sortable attributes configured on each index for the proxy's `traveler_ids` filter to work. We do this once on app startup (idempotent — Meili accepts the same settings repeatedly).

- [ ] **Step 9.1 — Failing test**

`tests/test_search_setup.py`:

```python
"""ensure_indexes_configured: runs idempotent Meili settings updates."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from trip_tracker.search.client import ensure_indexes_configured


@pytest.mark.asyncio
async def test_ensure_indexes_configured_calls_settings() -> None:
    fake_idx_trips = MagicMock()
    fake_idx_trips.update_filterable_attributes = AsyncMock()
    fake_idx_trips.update_sortable_attributes = AsyncMock()
    fake_idx_segments = MagicMock()
    fake_idx_segments.update_filterable_attributes = AsyncMock()
    fake_idx_segments.update_sortable_attributes = AsyncMock()

    fake_meili = MagicMock()
    fake_meili.create_index = AsyncMock()

    def index_router(name: str):
        return {"trips": fake_idx_trips, "segments": fake_idx_segments}[name]

    fake_meili.index = MagicMock(side_effect=index_router)

    await ensure_indexes_configured(fake_meili)

    fake_idx_trips.update_filterable_attributes.assert_awaited_with(["traveler_ids", "start_date", "end_date"])
    fake_idx_trips.update_sortable_attributes.assert_awaited_with(["start_date"])
    fake_idx_segments.update_filterable_attributes.assert_awaited_with(
        ["traveler_ids", "trip_id", "type", "start_at_unix"]
    )
    fake_idx_segments.update_sortable_attributes.assert_awaited_with(["start_at_unix"])
```

- [ ] **Step 9.2 — Implement `ensure_indexes_configured`**

Add to `src/trip_tracker/search/client.py`:

```python
import logging

logger = logging.getLogger(__name__)


_TRIP_FILTERABLE = ["traveler_ids", "start_date", "end_date"]
_TRIP_SORTABLE = ["start_date"]
_SEGMENT_FILTERABLE = ["traveler_ids", "trip_id", "type", "start_at_unix"]
_SEGMENT_SORTABLE = ["start_at_unix"]


async def ensure_indexes_configured(meili: MeiliClientProtocol) -> None:
    """Ensure both indexes exist with the right filterable/sortable attrs.

    Idempotent. Run on app startup. If indexes don't exist, Meili creates
    them on the first update_documents call later — but the attribute
    config has to land before then so the proxy's filter syntax is valid.
    """
    for name, filterable, sortable in (
        ("trips", _TRIP_FILTERABLE, _TRIP_SORTABLE),
        ("segments", _SEGMENT_FILTERABLE, _SEGMENT_SORTABLE),
    ):
        try:
            await meili.create_index(name, primary_key="id")
        except Exception:  # noqa: BLE001 — meili raises on conflict; idempotent
            pass
        idx = meili.index(name)
        await idx.update_filterable_attributes(filterable)
        await idx.update_sortable_attributes(sortable)
        logger.info("Meili index %r configured", name)
```

(The `Protocol` in `client.py` needs to be widened for `update_filterable_attributes` + `update_sortable_attributes`. Add them to `MeiliIndexProtocol`.)

- [ ] **Step 9.3 — Wire into app lifespan**

In `src/trip_tracker/app.py`'s lifespan:

```python
from trip_tracker.search.client import ensure_indexes_configured

# in lifespan, after building the client:
app.state.meili = build_client(settings)
try:
    await ensure_indexes_configured(app.state.meili)
except Exception as exc:  # noqa: BLE001
    # Don't fail the app if Meili is down at boot — search is non-critical.
    logger.warning("Meili index config failed at startup: %s", exc)
```

- [ ] **Step 9.4 — Run + commit**

```bash
uv run pytest tests/test_search_setup.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/search/client.py src/trip_tracker/app.py \
        tests/test_search_setup.py
git commit -m "feat(search): configure index attrs on app startup"
```

**Quality bar:**
- Idempotent: `create_index` raises on conflict; we swallow. Subsequent `update_*` calls succeed regardless.
- App startup tolerates Meili being down (logs + continues). Search just doesn't work until Meili comes back. Graceful degradation > hard fail.
- The `# noqa: BLE001` is justified per the spec §5.4 failure-handling table.

---

## Task 10 — `reindex` CLI Command

**Spec ref:** §8.

**Files:**
- Create: `src/trip_tracker/search/reindex.py`
- Modify: `src/trip_tracker/__main__.py` (add `reindex` subcommand)
- Create: `tests/test_search_reindex.py`

- [ ] **Step 10.1 — Failing test**

`tests/test_search_reindex.py`:

```python
"""reindex CLI: walks Postgres, batch-upserts to Meili."""

from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.search.reindex import reindex_all


@pytest.mark.asyncio
async def test_reindex_walks_all_rows(
    db_url: str, db_session: AsyncSession
) -> None:
    user = User(oidc_subject="r1", email="r1@x.com", display_name="R1")
    db_session.add(user)
    await db_session.flush()
    trip = Trip(title="T", start_date=date(2026, 6, 1), end_date=date(2026, 6, 5),
                created_by=user.id)
    db_session.add(trip)
    await db_session.flush()
    db_session.add(TripTraveler(trip_id=trip.id, user_id=user.id, role="owner"))
    db_session.add(Segment(
        trip_id=trip.id, owner_user_id=user.id, type="flight", status="confirmed",
        start_at=datetime(2026, 6, 1, 13, tzinfo=UTC), start_tz="UTC",
        parse_source="manual", parse_confidence=1.0,
    ))
    await db_session.commit()

    fake_idx_trips = MagicMock()
    fake_idx_trips.update_documents = AsyncMock()
    fake_idx_segments = MagicMock()
    fake_idx_segments.update_documents = AsyncMock()
    fake_meili = MagicMock()
    fake_meili.delete_index = AsyncMock()
    fake_meili.create_index = AsyncMock()
    fake_meili.index = MagicMock(side_effect=lambda n: {"trips": fake_idx_trips, "segments": fake_idx_segments}[n])

    engine = create_async_engine(db_url)
    counts = await reindex_all(engine, fake_meili, batch_size=100)
    await engine.dispose()

    assert counts["trips"] == 1
    assert counts["segments"] == 1
    fake_idx_trips.update_documents.assert_awaited()
    fake_idx_segments.update_documents.assert_awaited()


@pytest.mark.asyncio
async def test_reindex_dry_run_skips_meili(
    db_url: str, db_session: AsyncSession
) -> None:
    fake_meili = MagicMock()
    fake_meili.delete_index = AsyncMock()
    fake_meili.create_index = AsyncMock()
    fake_idx = MagicMock()
    fake_idx.update_documents = AsyncMock()
    fake_meili.index = MagicMock(return_value=fake_idx)

    engine = create_async_engine(db_url)
    counts = await reindex_all(engine, fake_meili, batch_size=100, dry_run=True)
    await engine.dispose()

    assert counts == {"trips": 0, "segments": 0}
    fake_idx.update_documents.assert_not_called()
```

- [ ] **Step 10.2 — Implement `reindex_all`**

`src/trip_tracker/search/reindex.py`:

```python
"""Full Meili rebuild from Postgres. Idempotent; called from
`python -m trip_tracker reindex`.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.search.client import (
    MeiliClientProtocol,
    ensure_indexes_configured,
)
from trip_tracker.search.sync import segment_to_doc, trip_to_doc

logger = logging.getLogger(__name__)


async def reindex_all(
    engine: AsyncEngine,
    meili: MeiliClientProtocol,
    *,
    batch_size: int = 100,
    dry_run: bool = False,
) -> dict[str, int]:
    """Walk Trips + Segments, batch-upsert. Returns a count dict."""
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    if not dry_run:
        for name in ("trips", "segments"):
            try:
                await meili.delete_index(name)
            except Exception:  # noqa: BLE001 — missing index is fine
                pass
        await ensure_indexes_configured(meili)

    counts = {"trips": 0, "segments": 0}

    async with SessionMaker() as db:
        # Trips
        trips_idx = meili.index("trips")
        batch: list[dict[str, Any]] = []
        for trip in (await db.execute(select(Trip))).scalars().all():
            batch.append(await trip_to_doc(trip, db=db))
            counts["trips"] += 1
            if len(batch) >= batch_size:
                if not dry_run:
                    await trips_idx.update_documents(batch)
                batch = []
        if batch and not dry_run:
            await trips_idx.update_documents(batch)

        # Segments
        seg_idx = meili.index("segments")
        batch = []
        for seg in (await db.execute(select(Segment))).scalars().all():
            batch.append(await segment_to_doc(seg, db=db))
            counts["segments"] += 1
            if len(batch) >= batch_size:
                if not dry_run:
                    await seg_idx.update_documents(batch)
                batch = []
        if batch and not dry_run:
            await seg_idx.update_documents(batch)

    if dry_run:
        # Test-friendly: dry run reports zero "indexed" since nothing was sent
        return {"trips": 0, "segments": 0}
    logger.info("reindex complete: %s", counts)
    return counts
```

- [ ] **Step 10.3 — Wire into `__main__.py`**

In `src/trip_tracker/__main__.py`, add `reindex` subcommand (after the existing `parse_pending` subcommand):

```python
async def _reindex(*, batch_size: int = 100, dry_run: bool = False) -> None:
    settings = Settings()
    engine = create_async_engine(str(settings.database_url))
    meili = build_client(settings)
    try:
        counts = await reindex_all(engine, meili, batch_size=batch_size, dry_run=dry_run)
        print(f"reindex: trips={counts['trips']} segments={counts['segments']} "
              f"{'(dry-run)' if dry_run else ''}")
    finally:
        await engine.dispose()


# In the dispatch block:
elif sys.argv[1] == "reindex":
    batch_size = 100
    dry_run = "--dry-run" in sys.argv
    for arg in sys.argv[2:]:
        if arg.startswith("--batch-size="):
            batch_size = int(arg.split("=", 1)[1])
    asyncio.run(_reindex(batch_size=batch_size, dry_run=dry_run))
```

- [ ] **Step 10.4 — Run + commit**

```bash
uv run pytest tests/test_search_reindex.py -v
uv run pytest -q
uv run ruff check . && uv run mypy src
git add src/trip_tracker/search/reindex.py src/trip_tracker/__main__.py \
        tests/test_search_reindex.py
git commit -m "feat(search): reindex CLI command"
```

**Quality bar:**
- Batch size 100 default. The implementer can ship with this; tuning is later.
- `--dry-run` reports counts as zero (since nothing was sent). Spec §8.1 said "log what would be upserted" — counting against zero is a reasonable interpretation.
- The full `delete_index` + `create_index` cycle is intentional: schema changes between releases need a fresh index. If the indexes don't exist, both deletes silently no-op (per spec §8.2 idempotency note).

---

## Task 11 — README + Verification + Tag v0.4.0

**Spec ref:** §12 (Done definition).

**Files:**
- Modify: `README.md`
- Run: full pytest + cov, ruff, mypy, pre-commit, bandit, djlint, docker build, Playwright smoke
- Tag: `v0.4.0`

- [ ] **Step 11.1 — README updates**

Update Status:

```markdown
> **Status:** Phase 4 — typo-tolerant search via ⌘K command palette.
> Phase 5 (documents + OCR) is next.
```

Append a new section before "Production deploy":

```markdown
## Search (Phase 4)

Press **⌘K** (Mac) or **Ctrl+K** (anywhere else) to open the search palette.
Type to find trips and segments by title, destination, provider, confirmation
number, city, flight/train number, or notes.

### How search works

Meilisearch stores a **derived index** of trips and segments. Postgres remains
the source of truth. After every Trip or Segment write, the saq worker
syncs the row to Meili (typically <1s end-to-end).

### Recovering after deploy

Existing Trip and Segment rows from before v0.4.0 aren't yet in Meili.
After deploy, run once:

    docker compose exec trip-tracker-app python -m trip_tracker reindex

Idempotent. Safe to re-run after a Meili upgrade or any time the index
drifts from Postgres. Use `--dry-run` to preview without writing.
```

- [ ] **Step 11.2 — Run the full local verification gate**

```bash
./scripts/build-tailwind.sh
uv run pytest --cov                          # ≥85%
uv run ruff check src tests migrations
uv run ruff format --check .                 # whole tree per CI's scope
uv run mypy src
uv run pre-commit run --all-files
uv run bandit -c pyproject.toml -r src/
uv run djlint src/trip_tracker/templates --check
docker build -t trip-tracker:dev .
```

All must be green. Iterate on any failure.

- [ ] **Step 11.3 — Playwright smoke test**

Same recipe as v0.3.0 (see git log around `4916450` and `f5b5a41`):

1. Boot Postgres + Redis + Meili containers locally on alternate ports (5433, 6380, 7701) so dev compose isn't impacted.
2. Run migrations.
3. Seed an admin user + alias + a sample Trip + Segment + RawEmail (script similar to v0.2.0's `_verify_seed.py`).
4. Run `python -m trip_tracker reindex` to populate Meili.
5. Start the app via `uv run uvicorn 'trip_tracker.app:create_app' --factory --host 127.0.0.1 --port 8000`.
6. Drive Playwright:
   - Navigate to `/trips`, paste session cookie.
   - Press Cmd+K (or trigger the keydown event).
   - Type "Paris" — verify the modal opens and a result appears.
   - Click the result — verify URL navigates to `/trips/<id>` (or `#segment-<id>` if a segment).
7. Tear down containers.

- [ ] **Step 11.4 — Commit, tag, push**

```bash
git add README.md
git commit -m "docs: README — Phase 4 search section + recovery"

git tag -a -s v0.4.0 -m "Phase 4 — Search & saq migration"
git checkout main
git merge --ff-only feat/phase-4-search
git push origin main
git push origin v0.4.0
```

The release workflow on GitHub fires on the tag push, producing a multi-arch image at `ghcr.io/<owner>/trip-tracker:v0.4.0`, signed with cosign + SBOM attached.

- [ ] **Step 11.5 — Schedule release-verification agent**

Same pattern as v0.2.0 / v0.3.0: schedule a one-time remote agent ~20 min after tag push to verify GHCR image, signature, SBOM. Reuse the prompt template from prior tags.

**Quality bar:**
- Coverage ≥85% AFTER all 11 tasks land.
- The full `ruff format --check .` (note: whole tree, not just `src tests migrations`) must pass — Phase 3 commit `0546d4e` taught us this.
- `bandit` clean. Any `# nosec` comments include the specific code (e.g., `# nosec B101`).
- The signed tag uses your SSH signing key (already configured per Phase 2).

---

## Done Definition for Phase 4

- All 11 tasks merged to `main`.
- CI green (lint + typecheck + test + security + docker + djlint + bandit).
- Coverage ≥ 85%.
- arq fully replaced; no `arq` import remains in `src/`.
- Meilisearch container running, internal-network only, master key never exposed externally.
- `python -m trip_tracker reindex` rebuilds both indexes from Postgres; subsequent searches return correct results.
- Pressing ⌘K opens the palette; typing "Paris" returns matches; clicking a segment result navigates to `/trips/<tid>#segment-<sid>` and the page scrolls to that segment.
- `/api/search/segments` returns 401 without a session cookie; with a session cookie, returns only the authenticated user's matches (verified by injecting a second user and confirming their data doesn't surface).
- Daily-budget cap from Phase 3 still works (saq retry semantics didn't break it).
- `v0.4.0` tag pushed; release workflow produces signed multi-arch GHCR image; release-verification scheduled agent confirms tag landed cleanly.

After this lands, return to brainstorming/writing-plans for **Phase 5 — Documents + OCR** (vault, PDF text extraction via pdfplumber, Tesseract worker, document index as Meili's 3rd index, presigned download URLs).
