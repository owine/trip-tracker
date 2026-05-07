# Phase 11: Single-User Collapse — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace OIDC-based multi-user authentication with single-user env-token cookie auth; drop multi-user tables (`trip_travelers`, `trip_merge_dismissals`); delete the merge/undo/dismiss/410 routes shipped in Phase 9; delete local trip CRUD routes (trip identity moves to TripIt in later phases).

**Architecture:** A single seeded `users` row (`id=00000000-0000-0000-0000-000000000001`, email from `OWNER_EMAIL`) represents the sole owner. The well-known UUID `uuid.UUID(int=1)` is exposed as the constant `OWNER_USER_ID` in `src/trip_tracker/auth/session.py` and is used consistently across the seeding migration, the bootstrap route, and `current_user` lookup. A new `GET /auth/bootstrap?token=<secret>` route validates `OWNER_SESSION_TOKEN` against env, sets a long-lived signed cookie (continues using `itsdangerous`, payload `{"user_id": "<uuid-str>"}`), and 302s to `/`. All other routes go through middleware that requires the cookie. OIDC code path is deleted entirely. ICS feed token auth (`User.ics_token_hash`) is preserved as a documented exception (calendar clients cannot carry session cookies). One Alembic migration drops all multi-user surface in a single transaction.

**Tech Stack:** FastAPI 0.122 · SQLAlchemy 2.0 async · Alembic · `itsdangerous` 2.2 (kept) · `pydantic-settings` 2.x.

---

## Pre-flight assumptions

This plan baselines on these assumptions; flag if any are wrong:

1. **The Phase 9 wrap (C6/C7/C8/W1/W2) is being skipped.** Those build merge UI, a sweeper cron, and Playwright smoke for features Phase 11 is about to delete. Investing in them is dead-weight.
2. **Tag the current main HEAD as `v0.9.0` before cutting `v2`** — last "TripIt clone" tag for forensic value.
3. **Phase 11 lives entirely on the `v2` branch.** Main does not get any of these changes until cutover (after Phase 15).
4. **Phase 12 (next plan) adds TripIt schema surface.** Phase 11 only removes; Phase 12 only adds.
5. **ICS feed auth survives** unchanged — the `ics_token_hash` field, the route that validates it, and the calendar URL pattern all remain. Documented as an exception in the plan.
6. **Consolidation logic from Phase 9 (B4/B5)** stays — `consolidation_candidates` carries forward to Phase 14a where it retargets at TripIt's trip list. Only the per-pair dismissal aspect goes (with the table).

---

## File map

### Files modified

- `src/trip_tracker/config.py` — Drop `oidc_*` settings; add `OWNER_EMAIL`, `OWNER_SESSION_TOKEN` (≥32 chars).
- `src/trip_tracker/auth/session.py` — Cookie helpers stay; payload simplified to `{"user_id": 1}`.
- `src/trip_tracker/auth/deps.py` — `current_user`/`require_user` simplified; `require_admin` deleted; `require_traveler`/`require_traveler_including_merged` simplified to "resolve trip by id, owner can access any trip".
- `src/trip_tracker/main.py` — Router wiring updated for new bootstrap route; OIDC routes removed.
- `src/trip_tracker/models/user.py` — Drop `oidc_subject`, `is_admin`. Keep `id`, `email`, `display_name`, `home_currency`, `ics_token_hash`, `created_at`, `updated_at`.
- `src/trip_tracker/models/trip.py` — Drop `created_by`, `merged_into_id`, `merged_at`, `merge_audit`, `dismissed_pairs_*` (if present).
- `src/trip_tracker/models/expense.py` — Drop `created_by_id`.
- `src/trip_tracker/routes/trips.py` — Delete merge/undo/dismiss/410-branch/CRUD routes; simplify remaining handlers (no `created_by` checks).
- `src/trip_tracker/trips/merge.py` — Delete entirely (the merge_trip_into / undo_merge_trip helpers).
- `src/trip_tracker/trips/consolidation.py` (or wherever `consolidation_candidates` lives) — Drop the dismissal join; everything else stays for Phase 14a.
- `src/trip_tracker/templates/trips/detail.html` — Drop merge banner + 410 page; keep skeleton for Phase 13 rewire.
- `src/trip_tracker/templates/inbox/_confirm_preview_banner.html` — Drop merge/dismiss buttons.
- `src/trip_tracker/templates/inbox/_bucket_review.html` — Drop the consolidation include guard for now (Phase 14a re-adds it).
- `pyproject.toml` — Remove `types-passlib` from dev deps.

### Files created

- `src/trip_tracker/auth/bootstrap.py` — New module: `GET /auth/bootstrap?token=<>` route.
- `migrations/versions/2026_05_06_NNNN_<id>_phase11_single_user_collapse.py` — One migration: drops multi-user columns and tables; seeds owner row.

### Files deleted

- `src/trip_tracker/auth/routes.py` — OIDC routes (login/callback/logout). Replaced by `bootstrap.py`.
- `src/trip_tracker/auth/oidc.py` — OIDC client wrapper. Authlib stays in deps for Phase 10 OAuth.
- `src/trip_tracker/models/trip_traveler.py` — Multi-user join.
- `src/trip_tracker/models/trip_merge_dismissal.py` — Phase 9 dismissal table.
- `src/trip_tracker/templates/trips/new.html`, `edit.html` (if present) — Trip CRUD forms.
- `tests/test_auth_routes.py`, `tests/test_oidc.py` — OIDC tests.
- `tests/test_auth_deps_admin.py` — `require_admin` tests.
- `tests/test_models_trip_traveler.py` — TripTraveler model.
- `tests/test_routes_trips_merge.py`, `test_routes_trips_undo_merge.py`, `test_routes_trips_dismiss.py` — Phase 9 merge/undo/dismiss tests.
- `tests/test_routes_trips_consolidation_banner.py` — Banner test (re-added under Phase 14a against TripIt candidates).

---

## Tasks

### Task 1: Tag v0.9.0 and cut the v2 branch

**Files:**
- Modify: branch + tag state only

- [ ] **Step 1: Confirm working tree clean and current main HEAD**

```bash
cd /Users/owine/Git/trip-tracker
git status
git log -1 --oneline
```

Expected: `On branch feat/phase-9-merge-and-consolidation`, working tree clean, HEAD is `9b70b04` (spec fold-in commit) or later.

- [ ] **Step 2: Verify on the right branch and decide the tag commit**

The `v0.9.0` tag should mark the **last "TripIt clone" era code state**. Per pre-flight assumption #1, we are skipping the C6-C8 wrap. Tag `3f82e7b` (the last C5-era commit on main, before brainstorming docs) — it represents the state where consolidation features are functionally complete.

```bash
git log --oneline main..HEAD  # see what's after main on this feature branch
git log -5 --oneline main      # see the last 5 main commits
```

- [ ] **Step 3: Merge brainstorm docs to main, then tag v0.9.0**

The two spec commits (`5d964db`, `9b70b04`) are docs-only and should land on main before tagging:

```bash
git checkout main
git merge --ff-only feat/phase-9-merge-and-consolidation
git tag -a v0.9.0 -m "v0.9.0: last release of TripIt-clone era (Phase 9 consolidation shipped through C5; C6-C8 deferred for the v1.0.0 pivot)"
git push origin main v0.9.0
```

Expected: tag pushed, GitHub Releases page shows v0.9.0.

- [ ] **Step 4: Cut v2 branch from v0.9.0**

```bash
git checkout -b v2 v0.9.0
git push -u origin v2
```

Expected: `v2` branch exists locally and on origin, tracking origin/v2.

- [ ] **Step 5: No commit yet**

Branch creation has no commit of its own. Move to Task 2.

---

### Task 2: Add OWNER_EMAIL and OWNER_SESSION_TOKEN to Settings; remove OIDC settings

**Files:**
- Modify: `src/trip_tracker/config.py:78-136`
- Test: `tests/test_config.py` (may exist; create if not)

- [ ] **Step 1: Read current config.py to confirm exact OIDC field locations**

```bash
sed -n '78,110p' src/trip_tracker/config.py
```

- [ ] **Step 2: Write failing test for new settings**

Append to (or create) `tests/test_config.py`:

```python
import pytest
from pydantic import ValidationError


def test_settings_requires_owner_email_and_session_token(monkeypatch):
    """OWNER_EMAIL and OWNER_SESSION_TOKEN are required (no defaults).
    OWNER_SESSION_TOKEN must be at least 32 chars."""
    from trip_tracker.config import Settings

    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    s = Settings(_env_file=None)
    assert s.owner_email == "owner@example.com"
    assert s.owner_session_token == "x" * 32


def test_settings_rejects_short_owner_session_token(monkeypatch):
    from trip_tracker.config import Settings

    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "tooshort")
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_settings_no_longer_has_oidc_fields(monkeypatch):
    from trip_tracker.config import Settings

    monkeypatch.setenv("OWNER_EMAIL", "owner@example.com")
    monkeypatch.setenv("OWNER_SESSION_TOKEN", "x" * 32)
    monkeypatch.setenv("SESSION_SECRET", "y" * 32)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://t:t@localhost/t")
    s = Settings(_env_file=None)
    assert not hasattr(s, "oidc_issuer")
    assert not hasattr(s, "oidc_client_id")
    assert not hasattr(s, "admin_group")
```

- [ ] **Step 3: Run test, expect failure**

```bash
uv run pytest tests/test_config.py -v
```

Expected: FAIL — `owner_email` does not exist on Settings.

- [ ] **Step 4: Implement: edit `src/trip_tracker/config.py`**

In the `Settings` class:
- DELETE the OIDC block (`oidc_issuer`, `oidc_client_id`, `oidc_client_secret`, `oidc_redirect_uri`, `admin_group`).
- ADD:

```python
owner_email: str = Field(
    ..., description="Email address of the single owner; seeded into users table on first boot."
)
owner_session_token: str = Field(
    ...,
    min_length=32,
    description="Shared secret presented at /auth/bootstrap?token=<>. ≥32 chars.",
)
```

- [ ] **Step 5: Run tests; expect pass**

```bash
uv run pytest tests/test_config.py -v
```

- [ ] **Step 6: Update `.env.example` and `docs/forwardemail-setup.md` if env vars are documented there**

```bash
grep -rn "OIDC\|oidc_issuer\|admin_group" .env.example docs/ 2>/dev/null || echo "none found"
```

Replace OIDC block in `.env.example` with:

```
OWNER_EMAIL=you@example.com
OWNER_SESSION_TOKEN=  # generate via: python -c 'import secrets; print(secrets.token_hex(32))'
```

- [ ] **Step 7: Commit**

```bash
git add src/trip_tracker/config.py tests/test_config.py .env.example
git commit -m "feat(phase11): add OWNER_EMAIL+OWNER_SESSION_TOKEN settings; drop oidc_* fields"
```

---

### Task 3: Build the bootstrap route and cookie set/read helpers

**Files:**
- Create: `src/trip_tracker/auth/bootstrap.py`
- Modify: `src/trip_tracker/auth/session.py:1-54` (simplify payload)
- Test: `tests/test_auth_bootstrap.py`

- [ ] **Step 1: Write failing tests for bootstrap behavior**

Create `tests/test_auth_bootstrap.py`:

```python
from httpx import AsyncClient
import pytest


@pytest.mark.asyncio
async def test_bootstrap_with_correct_token_sets_cookie_and_redirects(
    async_client: AsyncClient,
):
    response = await async_client.get(
        "/auth/bootstrap?token=" + "x" * 32,
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/"
    assert "tt_session" in response.cookies


@pytest.mark.asyncio
async def test_bootstrap_with_wrong_token_returns_401(
    async_client: AsyncClient,
):
    response = await async_client.get("/auth/bootstrap?token=wrong")
    assert response.status_code == 401
    assert "tt_session" not in response.cookies


@pytest.mark.asyncio
async def test_bootstrap_without_token_returns_400(
    async_client: AsyncClient,
):
    response = await async_client.get("/auth/bootstrap")
    assert response.status_code == 400
```

(Test fixture `async_client` should already exist in `tests/conftest.py` and inject `OWNER_SESSION_TOKEN="x"*32`. If not, this is the time to add it.)

- [ ] **Step 2: Run tests; expect failure**

```bash
uv run pytest tests/test_auth_bootstrap.py -v
```

Expected: FAIL — route does not exist.

- [ ] **Step 3: Implement `src/trip_tracker/auth/bootstrap.py`**

```python
"""Bootstrap route for single-user cookie auth.

GET /auth/bootstrap?token=<OWNER_SESSION_TOKEN> validates the env token,
sets a long-lived signed cookie identifying the seeded owner user, and
redirects to /.
"""
from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse

from trip_tracker.auth.session import OWNER_USER_ID, set_session_cookie
from trip_tracker.config import Settings, get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/bootstrap")
async def bootstrap(
    token: str | None = Query(default=None),
    settings: Settings = Depends(get_settings),
) -> Response:
    if token is None:
        raise HTTPException(status_code=400, detail="token query param required")
    if not secrets.compare_digest(token, settings.owner_session_token):
        raise HTTPException(status_code=401, detail="invalid token")
    response = RedirectResponse(url="/", status_code=302)
    set_session_cookie(response, user_id=OWNER_USER_ID)
    return response
```

- [ ] **Step 4: Modify `src/trip_tracker/auth/session.py` — add `OWNER_USER_ID` constant and update helpers**

The existing module signs a payload via `itsdangerous.URLSafeTimedSerializer`. Add the well-known UUID and adapt the helper functions to handle UUID payloads as strings (JSON cannot serialize UUID directly):

```python
import uuid
from fastapi import Response

# Well-known UUID for the single owner. Used by the seeding migration,
# the bootstrap route, and current_user. uuid.UUID(int=1) is the literal
# 00000000-0000-0000-0000-000000000001 — chosen for stability across env wipes.
OWNER_USER_ID: uuid.UUID = uuid.UUID(int=1)


def set_session_cookie(response: Response, user_id: uuid.UUID) -> None:
    settings = get_settings()
    serializer = _get_serializer(settings.session_secret)
    payload = serializer.dumps({"user_id": str(user_id)})
    response.set_cookie(
        key=settings.session_cookie_name,
        value=payload,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=True,
        samesite="lax",
    )


def decode_session_cookie(value: str) -> dict | None:
    """Returns the decoded payload dict or None on invalid/expired/tampered cookie.
    The 'user_id' key (if present) is a string-form UUID — callers parse it back."""
    settings = get_settings()
    serializer = _get_serializer(settings.session_secret)
    try:
        return serializer.loads(value, max_age=settings.session_max_age_seconds)
    except Exception:
        return None
```

- [ ] **Step 5: Wire the router into the app in `src/trip_tracker/main.py`**

Find where routers are included (look for `app.include_router(...)`); add:

```python
from trip_tracker.auth.bootstrap import router as bootstrap_router
app.include_router(bootstrap_router)
```

(In Task 6 we'll remove the old OIDC router.)

- [ ] **Step 6: Run tests; expect pass**

```bash
uv run pytest tests/test_auth_bootstrap.py -v
```

- [ ] **Step 7: Commit**

```bash
git add src/trip_tracker/auth/bootstrap.py src/trip_tracker/auth/session.py src/trip_tracker/main.py tests/test_auth_bootstrap.py
git commit -m "feat(phase11): add /auth/bootstrap?token= route with env-token cookie auth"
```

---

### Task 4: Simplify current_user, require_user; delete require_admin

**Files:**
- Modify: `src/trip_tracker/auth/deps.py:23-50`
- Test: `tests/test_auth_deps.py` (create or modify)

- [ ] **Step 1: Read existing deps.py to understand cookie-decode flow**

```bash
sed -n '1,50p' src/trip_tracker/auth/deps.py
```

- [ ] **Step 2: Write failing test**

Create `tests/test_auth_deps.py` (replacing any existing OIDC-flavored version):

```python
import pytest
from fastapi import HTTPException

from trip_tracker.auth.deps import current_user, require_user


@pytest.mark.asyncio
async def test_current_user_returns_owner_with_valid_cookie(
    db_session, owner_user, signed_session_cookie
):
    user = await current_user(
        session_cookie=signed_session_cookie, db=db_session
    )
    assert user is not None
    assert user.id == owner_user.id
    assert user.email == owner_user.email


@pytest.mark.asyncio
async def test_current_user_returns_none_with_no_cookie(db_session):
    user = await current_user(session_cookie=None, db=db_session)
    assert user is None


@pytest.mark.asyncio
async def test_current_user_returns_none_with_tampered_cookie(db_session):
    user = await current_user(session_cookie="invalid.payload", db=db_session)
    assert user is None


@pytest.mark.asyncio
async def test_require_user_raises_401_without_cookie(db_session):
    with pytest.raises(HTTPException) as exc:
        await require_user(session_cookie=None, db=db_session)
    assert exc.value.status_code == 401
```

(Fixtures `owner_user` and `signed_session_cookie` need to exist in `tests/conftest.py`; create them if absent — `owner_user` seeds the row, `signed_session_cookie` returns a valid signed-cookie string.)

- [ ] **Step 3: Run; expect failure**

```bash
uv run pytest tests/test_auth_deps.py -v
```

- [ ] **Step 4: Rewrite `src/trip_tracker/auth/deps.py`**

```python
"""Single-user auth dependencies.

`current_user` decodes the session cookie and loads the owner User row.
`require_user` raises 401 if no valid cookie. `require_admin` is removed
(single-user installs have no admin distinction). `require_traveler` and
`require_traveler_including_merged` collapse to "load trip by id" since
the owner can access any trip.
"""
from __future__ import annotations

import uuid

from fastapi import Cookie, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.session import decode_session_cookie
from trip_tracker.config import get_settings
from trip_tracker.db import get_session
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User


async def current_user(
    # Cookie name is hardcoded "tt_session" (matches Settings.session_cookie_name default).
    # FastAPI's Cookie() does not support resolving names from settings at runtime; if the
    # cookie name ever changes, update both Settings.session_cookie_name and this alias.
    tt_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_session),
) -> User | None:
    if tt_session is None:
        return None
    payload = decode_session_cookie(tt_session)
    if payload is None or "user_id" not in payload:
        return None
    try:
        user_uuid = uuid.UUID(payload["user_id"])
    except (ValueError, TypeError):
        return None
    result = await db.execute(select(User).where(User.id == user_uuid))
    return result.scalar_one_or_none()


async def require_user(
    tt_session: str | None = Cookie(default=None),
    db: AsyncSession = Depends(get_session),
) -> User:
    user = await current_user(tt_session=tt_session, db=db)
    if user is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


async def require_traveler(
    trip_id: uuid.UUID,
    user: User = Depends(require_user),
    db: AsyncSession = Depends(get_session),
) -> Trip:
    """Single-user mode: owner can access any trip. Returns Trip or 404."""
    result = await db.execute(select(Trip).where(Trip.id == trip_id))
    trip = result.scalar_one_or_none()
    if trip is None:
        raise HTTPException(status_code=404, detail="trip not found")
    return trip


# `require_traveler_including_merged` and `require_admin` deleted in this task.
```

The `Cookie(...)` alias resolves to `settings.session_cookie_name` at runtime — wire this via a small wrapper if FastAPI's Cookie cannot resolve dynamically; alternative is a hardcoded `"tt_session"` matching `Settings.session_cookie_name` default.

- [ ] **Step 5: Run tests; expect pass**

```bash
uv run pytest tests/test_auth_deps.py -v
```

- [ ] **Step 6: Run mypy on the file**

```bash
uv run mypy src/trip_tracker/auth/deps.py
```

Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/trip_tracker/auth/deps.py tests/test_auth_deps.py tests/conftest.py
git commit -m "feat(phase11): simplify current_user/require_user for single-user; drop require_admin + require_traveler_including_merged"
```

---

### Task 5: Delete OIDC routes and OIDC client module

**Files:**
- Delete: `src/trip_tracker/auth/routes.py`
- Delete: `src/trip_tracker/auth/oidc.py`
- Delete: `tests/test_auth_routes.py`, `tests/test_oidc.py`, `tests/test_auth_deps_admin.py`
- Modify: `src/trip_tracker/main.py` (remove the OIDC router include)

- [ ] **Step 1: Confirm no other modules import the OIDC client**

```bash
rg "from trip_tracker.auth.oidc" src/ tests/
rg "from trip_tracker.auth.routes import" src/ tests/
```

If anything outside the OIDC stack imports from these, address before deleting.

- [ ] **Step 2: Delete files**

```bash
git rm src/trip_tracker/auth/routes.py src/trip_tracker/auth/oidc.py
git rm tests/test_auth_routes.py tests/test_oidc.py tests/test_auth_deps_admin.py
```

- [ ] **Step 3: Remove the OIDC router include from `src/trip_tracker/main.py`**

```bash
grep -n "auth.routes\|oidc" src/trip_tracker/main.py
```

Delete those `include_router(...)` lines. The bootstrap router from Task 3 is the replacement.

- [ ] **Step 4: Run full test suite**

```bash
uv run pytest -x
```

Expected: all green. If any test fails, it's likely a stale fixture using OIDC; update the fixture or delete the test if it's now meaningless.

- [ ] **Step 5: Commit**

```bash
git add -u src/ tests/
git commit -m "feat(phase11): delete OIDC routes + client + tests; bootstrap is the only auth path"
```

---

### Task 6: Drop User.oidc_subject and User.is_admin from the model

**Files:**
- Modify: `src/trip_tracker/models/user.py:15-34`
- Test: `tests/test_models_user.py` (may exist; create if not)

(The Alembic migration that drops the underlying columns is Task 16. This task removes the ORM mapping so code doesn't break in the meantime; the migration applies in the same PR.)

- [ ] **Step 1: Confirm there are no remaining usages of `is_admin` or `oidc_subject`**

```bash
rg "is_admin|oidc_subject" src/ tests/
```

Expected: no hits in `src/` (require_admin is already gone). If any hit in `tests/`, fix before this step.

- [ ] **Step 2: Write failing test**

Add to `tests/test_models_user.py`:

```python
def test_user_model_has_no_oidc_subject_or_is_admin():
    from trip_tracker.models.user import User
    cols = {c.name for c in User.__table__.columns}
    assert "oidc_subject" not in cols
    assert "is_admin" not in cols
    assert "ics_token_hash" in cols  # ICS feed auth survives
    assert "email" in cols
```

- [ ] **Step 3: Run; expect failure**

```bash
uv run pytest tests/test_models_user.py::test_user_model_has_no_oidc_subject_or_is_admin -v
```

- [ ] **Step 4: Edit `src/trip_tracker/models/user.py`**

Remove the `oidc_subject` and `is_admin` Mapped fields. The class becomes:

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    home_currency: Mapped[str] = mapped_column(String(3), default="USD")
    ics_token_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now()
    )
```

- [ ] **Step 5: Run tests**

```bash
uv run pytest tests/test_models_user.py -v
```

- [ ] **Step 6: Commit**

```bash
git add src/trip_tracker/models/user.py tests/test_models_user.py
git commit -m "feat(phase11): drop User.oidc_subject + User.is_admin from ORM"
```

---

### Task 7: Simplify or delete TripMergeDismissal usage in consolidation_candidates

**Files:**
- Modify: the consolidation module (find with rg below)
- Test: `tests/test_trips_consolidation.py`

- [ ] **Step 1: Locate consolidation module + dismissal usage**

```bash
rg "TripMergeDismissal|trip_merge_dismissals|dismissed_pairs" src/ tests/
```

- [ ] **Step 2: Write failing test asserting dismissal join is gone**

Modify the relevant test (likely `tests/test_trips_consolidation.py`) to remove all "given a dismissal exists, candidate is filtered out" assertions and add:

```python
@pytest.mark.asyncio
async def test_consolidation_candidates_no_longer_filters_by_dismissal(
    db_session, owner_user, two_overlapping_trips
):
    """Phase 11: dismissals are gone; candidates are returned regardless."""
    from trip_tracker.trips.consolidation import consolidation_candidates
    from trip_tracker.trips.value_objects import ConsolidationTarget

    target = ConsolidationTarget.from_trip(two_overlapping_trips[0], segments=[])
    results = await consolidation_candidates(db_session, owner_user, target)
    # Should still surface the second trip — no filtering by dismissal
    assert any(c.trip.id == two_overlapping_trips[1].id for c in results)
```

- [ ] **Step 3: Run; expect failure**

```bash
uv run pytest tests/test_trips_consolidation.py -v
```

- [ ] **Step 4: Edit consolidation module**

Remove the `select(TripMergeDismissal)`/EXISTS subquery/JOIN that filters dismissed pairs. The function signature stays the same; only the query is leaner.

- [ ] **Step 5: Run; expect pass**

```bash
uv run pytest tests/test_trips_consolidation.py -v
```

- [ ] **Step 6: Commit**

```bash
git add -u
git commit -m "feat(phase11): drop dismissal filter from consolidation_candidates"
```

---

### Task 8: Delete the merge route + tests (Phase 9 C1)

**Files:**
- Modify: `src/trip_tracker/routes/trips.py:379-417`
- Delete: `tests/test_routes_trips_merge.py`
- Delete: `src/trip_tracker/trips/merge.py` (the helper file)

- [ ] **Step 1: Locate merge_trip_into call sites**

```bash
rg "merge_trip_into|undo_merge_trip" src/ tests/
```

- [ ] **Step 2: Delete the route**

In `src/trip_tracker/routes/trips.py`, remove the `@router.post("/{source_id}/merge-into/{target_id}")` decorator and its handler function (lines ~379-417 per the explorer; verify with `sed -n`).

- [ ] **Step 3: Delete the helpers file**

```bash
git rm src/trip_tracker/trips/merge.py tests/test_routes_trips_merge.py
```

- [ ] **Step 4: Run full suite**

```bash
uv run pytest -x
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(phase11): delete merge route + merge_trip_into helper + tests (Phase 9 C1)"
```

---

### Task 9: Delete the undo-merge route + tests (Phase 9 C2)

**Files:**
- Modify: `src/trip_tracker/routes/trips.py:337-376`
- Delete: `tests/test_routes_trips_undo_merge.py`

- [ ] **Step 1: Delete the route handler**

Remove the `@router.post("/{target_id}/undo-merge/{source_id}")` block.

- [ ] **Step 2: Delete tests**

```bash
git rm tests/test_routes_trips_undo_merge.py
```

- [ ] **Step 3: Run suite + commit**

```bash
uv run pytest -x
git add -u
git commit -m "feat(phase11): delete undo-merge route + tests (Phase 9 C2)"
```

---

### Task 10: Delete the dismiss-merge route + tests (Phase 9 C3)

**Files:**
- Modify: `src/trip_tracker/routes/trips.py:420-461`
- Delete: `tests/test_routes_trips_dismiss.py`

- [ ] **Step 1: Delete the route handler**

Remove the `@router.post("/{trip_id}/dismiss-merge/{other_id}")` block.

- [ ] **Step 2: Delete tests**

```bash
git rm tests/test_routes_trips_dismiss.py
```

- [ ] **Step 3: Run suite + commit**

```bash
uv run pytest -x
git add -u
git commit -m "feat(phase11): delete dismiss-merge route + tests (Phase 9 C3)"
```

---

### Task 11: Delete the 410-soft-delete branch in trip_detail (Phase 9 B7)

**Files:**
- Modify: `src/trip_tracker/routes/trips.py:138-144`
- Modify: `tests/test_routes_trip_detail.py` or wherever B7 tests live (`rg "410" tests/`)

- [ ] **Step 1: Find the 410 branch**

```bash
sed -n '130,160p' src/trip_tracker/routes/trips.py
```

- [ ] **Step 2: Remove the `if trip.merged_into_id is not None` block returning 410**

Trip detail handler now uses `require_traveler` (simplified per Task 4) — which already 404s if not found. There is no soft-delete state.

- [ ] **Step 3: Delete or update B7 tests**

```bash
rg "410|merged_into|test_trip_detail.*410" tests/
```

Delete the specific test functions asserting 410; leave others intact.

- [ ] **Step 4: Run suite + commit**

```bash
uv run pytest -x
git add -u
git commit -m "feat(phase11): remove 410-soft-delete branch in trip_detail (Phase 9 B7)"
```

---

### Task 12: Delete trip CRUD routes (new, create, edit, update, delete)

**Files:**
- Modify: `src/trip_tracker/routes/trips.py:58-105, 271-330`
- Delete: `src/trip_tracker/templates/trips/new.html`, `edit.html` (if present)
- Delete: associated tests

- [ ] **Step 1: Inventory the routes to delete**

```bash
sed -n '58,110p' src/trip_tracker/routes/trips.py
sed -n '271,335p' src/trip_tracker/routes/trips.py
```

- [ ] **Step 2: Delete the route handlers** for: `GET /new`, `POST /`, `GET /{trip_id}/edit`, `POST /{trip_id}` (update), `POST /{trip_id}/delete`.

- [ ] **Step 3: Delete the templates if present**

```bash
ls src/trip_tracker/templates/trips/
git rm src/trip_tracker/templates/trips/new.html src/trip_tracker/templates/trips/edit.html 2>/dev/null || true
```

- [ ] **Step 4: Delete or update tests for these routes**

```bash
rg "trips/new|trips/.*/edit|test_create_trip|test_update_trip|test_delete_trip" tests/
```

Delete entire test files if they're 100% about CRUD; otherwise excise the specific tests.

- [ ] **Step 5: Update `templates/trips/list.html`** to remove the "Create new trip" button/link

```bash
grep -n "Create\|/new" src/trip_tracker/templates/trips/list.html
```

Remove the link.

- [ ] **Step 6: Run suite + commit**

```bash
uv run pytest -x
git add -u
git commit -m "feat(phase11): delete local trip CRUD routes + templates (trip identity moves to TripIt in Phase 13)"
```

---

### Task 13: Drop TripTraveler model + delete tests

**Files:**
- Delete: `src/trip_tracker/models/trip_traveler.py`
- Delete: `tests/test_models_trip_traveler.py`
- Modify: any `src/trip_tracker/models/__init__.py` re-exports

- [ ] **Step 1: Confirm no remaining usages**

```bash
rg "TripTraveler|trip_traveler" src/ tests/
```

Expected: zero hits in `src/` after Task 4 (`require_traveler` was simplified to not query the table).

- [ ] **Step 2: Delete model + test**

```bash
git rm src/trip_tracker/models/trip_traveler.py tests/test_models_trip_traveler.py
```

- [ ] **Step 3: Remove re-export from models/__init__.py**

```bash
grep -n "TripTraveler" src/trip_tracker/models/__init__.py
```

Remove the import line.

- [ ] **Step 4: Run + commit**

```bash
uv run pytest -x
git add -u
git commit -m "feat(phase11): drop TripTraveler model + tests"
```

---

### Task 14: Drop TripMergeDismissal model + delete tests

**Files:**
- Delete: `src/trip_tracker/models/trip_merge_dismissal.py`
- Delete: `tests/test_models_trip_merge_dismissal.py` (if exists)
- Delete: `tests/test_routes_trips_consolidation_banner.py`, `tests/test_routes_inbox_consolidation_banner.py`
- Modify: `src/trip_tracker/models/__init__.py`

- [ ] **Step 1: Confirm no remaining usages**

```bash
rg "TripMergeDismissal|trip_merge_dismissals" src/ tests/
```

After Task 7, this should be zero hits in `src/`.

- [ ] **Step 2: Delete the model + tests**

```bash
git rm src/trip_tracker/models/trip_merge_dismissal.py
git rm -f tests/test_models_trip_merge_dismissal.py tests/test_routes_trips_consolidation_banner.py tests/test_routes_inbox_consolidation_banner.py
```

(Banner tests come back under Phase 14a against TripIt candidates.)

- [ ] **Step 3: Remove import from models/__init__.py**

- [ ] **Step 4: Run + commit**

```bash
uv run pytest -x
git add -u
git commit -m "feat(phase11): drop TripMergeDismissal model + Phase 9 banner tests (banner re-added in Phase 14a)"
```

---

### Task 15: Drop trip merge columns and expense.created_by_id from ORM

**Files:**
- Modify: `src/trip_tracker/models/trip.py:27-53`
- Modify: `src/trip_tracker/models/expense.py`

- [ ] **Step 1: Confirm no remaining code references**

```bash
rg "created_by|merged_into_id|merged_at|merge_audit|dismissed_pairs" src/ tests/
```

Expected: only model-definition lines hit. If any application code still references these, fix before this step.

- [ ] **Step 2: Edit `src/trip_tracker/models/trip.py`**

Remove the `Mapped` columns: `created_by`, `merged_into_id`, `merged_at`, `merge_audit`, `dismissed_pairs_*` (if present). Also remove any `relationship(...)` that pointed to TripMergeDismissal or TripTraveler.

- [ ] **Step 3: Edit `src/trip_tracker/models/expense.py`**

Remove `created_by_id` Mapped column.

- [ ] **Step 4: Run mypy + tests**

```bash
uv run mypy src/
uv run pytest -x
```

- [ ] **Step 5: Commit**

```bash
git add -u
git commit -m "feat(phase11): drop trip merge columns + expense.created_by_id from ORM"
```

---

### Task 16: Single Alembic migration: drop multi-user columns + tables; seed owner row

**Files:**
- Create: `migrations/versions/2026_05_06_NNNN_<id>_phase11_single_user_collapse.py`

- [ ] **Step 1: Inspect existing constraint names so the migration drops the right ones**

Constraint auto-naming differs by SQLAlchemy version + naming convention; the names in Step 2 are best-guess. Verify against the live DB before editing:

```bash
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d+ trips" | grep -i fkey
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d+ users" | grep -i "key\|fkey"
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d+ expenses" | grep -i fkey
```

Capture the actual constraint names in your notes; substitute them into Step 2 if they differ from the placeholders.

- [ ] **Step 2: Generate migration scaffold**

```bash
uv run alembic revision -m "phase11_single_user_collapse"
```

Note the generated filename + revision id.

- [ ] **Step 3: Edit the generated file**

Replace the auto-generated body with explicit ops:

```python
"""phase11_single_user_collapse

Revision ID: <auto>
Revises: 4cf28c429f18
Create Date: 2026-05-06 ...
"""
from __future__ import annotations

import os
import uuid

import sqlalchemy as sa
from alembic import op


revision: str = "<auto>"
down_revision: str | None = "4cf28c429f18"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop FK-bearing tables first
    op.drop_table("trip_merge_dismissals")
    op.drop_table("trip_travelers")

    # Drop columns from trips
    with op.batch_alter_table("trips") as batch:
        batch.drop_constraint("trips_created_by_fkey", type_="foreignkey")
        batch.drop_column("created_by")
        batch.drop_column("merged_into_id")
        batch.drop_column("merged_at")
        batch.drop_column("merge_audit")
        # If dismissed_pairs_* exist, drop here.

    # Drop expense.created_by_id
    with op.batch_alter_table("expenses") as batch:
        batch.drop_constraint("expenses_created_by_id_fkey", type_="foreignkey")
        batch.drop_column("created_by_id")

    # Drop user columns
    with op.batch_alter_table("users") as batch:
        batch.drop_constraint("users_oidc_subject_key", type_="unique")
        batch.drop_column("oidc_subject")
        batch.drop_column("is_admin")

    # Seed the owner row idempotently
    owner_email = os.environ.get("OWNER_EMAIL")
    if not owner_email:
        raise RuntimeError(
            "OWNER_EMAIL must be set when running phase11_single_user_collapse migration"
        )
    # OWNER_USER_ID = uuid.UUID(int=1) — must match auth/session.py constant exactly
    op.execute(
        sa.text(
            """
            INSERT INTO users (id, email, display_name, home_currency, created_at, updated_at)
            VALUES (:id, :email, 'Owner', 'USD', now(), now())
            ON CONFLICT (email) DO NOTHING
            """
        ).bindparams(id=uuid.UUID(int=1), email=owner_email)
    )


def downgrade() -> None:
    raise RuntimeError(
        "phase11_single_user_collapse is not reversible. To revert, restore from a v0.9.0 backup."
    )
```

(The exact constraint names may differ — verify by querying the DB or reading prior migrations.)

- [ ] **Step 4: Test migration locally against the dev DB**

```bash
# Backup first
docker compose exec postgres pg_dump -U trip_tracker trip_tracker > /tmp/pre-phase11.sql
# Apply
uv run alembic upgrade head
# Inspect
docker compose exec postgres psql -U trip_tracker -d trip_tracker -c "\d trips"
```

Expected: `created_by`, `merged_into_id`, `merged_at`, `merge_audit` all absent.

- [ ] **Step 5: Restore backup and re-test once more from clean**

```bash
# Reset
docker compose exec postgres psql -U trip_tracker -c "DROP DATABASE trip_tracker"
docker compose exec postgres psql -U trip_tracker -c "CREATE DATABASE trip_tracker"
docker compose exec -T postgres psql -U trip_tracker -d trip_tracker < /tmp/pre-phase11.sql
uv run alembic upgrade head
```

- [ ] **Step 6: Run full test suite**

```bash
uv run pytest
```

Expected: all green. (Tests that need an authenticated user use the `owner_user` fixture, which the migration now seeds.)

- [ ] **Step 7: Commit**

```bash
git add migrations/versions/
git commit -m "feat(phase11): alembic migration — drop multi-user columns/tables + seed owner row"
```

---

### Task 17: Final cleanup, smoke test, dep removal, commit

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/trip_tracker/templates/trips/detail.html`, `src/trip_tracker/templates/inbox/_confirm_preview_banner.html`, `src/trip_tracker/templates/inbox/_bucket_review.html`

- [ ] **Step 1: Strip merge banner + dismiss buttons from templates**

```bash
grep -rn "merge\|dismiss\|merged_into" src/trip_tracker/templates/
```

Remove blocks that reference these. The "Open in TripIt" deep link comes back in Phase 13; leave the spaces empty for now.

- [ ] **Step 2: Drop `types-passlib` from dev deps**

In `pyproject.toml`, remove the `types-passlib==X.Y.Z` line from `[dependency-groups].dev`. Run:

```bash
uv lock
```

- [ ] **Step 3: Run formatters and linters**

```bash
uv run ruff format src/ tests/ migrations/
uv run ruff check src/ tests/ migrations/
uv run mypy src/
```

Expected: all green.

- [ ] **Step 4: Smoke test the app boot path**

```bash
docker compose up -d
sleep 5
docker compose logs app | tail -50
curl -i http://localhost:8000/auth/bootstrap?token=$(grep OWNER_SESSION_TOKEN .env | cut -d= -f2)
```

Expected: 302 redirect to `/`, `Set-Cookie: tt_session=...`. Then:

```bash
COOKIE=$(curl -s -i http://localhost:8000/auth/bootstrap?token=... | grep -i set-cookie | head -1 | cut -d':' -f2 | tr -d ' \r')
curl -i -H "Cookie: $COOKIE" http://localhost:8000/trips
```

Expected: 200 OK, list page renders (empty since no trips yet — Phase 13 adds TripIt sync).

- [ ] **Step 5: Run full suite one more time**

```bash
uv run pytest -v
```

- [ ] **Step 6: Commit + push v2 branch**

```bash
git add -u
git commit -m "feat(phase11): cleanup — strip merge UI from templates, drop types-passlib, smoke verified"
git push origin v2
```

- [ ] **Step 7: Open a draft PR for tracking** (optional — main does not see v2 until Phase 15 cutover)

```bash
gh pr create --draft --base main --head v2 --title "v1.0.0: TripIt wrapper pivot (rolling)" --body "Tracking PR for the v2 branch. Will be force-merged at Phase 15 cutover. See docs/superpowers/specs/2026-05-06-tripit-wrapper-pivot-design.md."
```

---

## Phase 11 success criteria

- [ ] All tests green (`uv run pytest`)
- [ ] mypy clean (`uv run mypy src/`)
- [ ] App boots via `docker compose up`
- [ ] `GET /auth/bootstrap?token=$OWNER_SESSION_TOKEN` returns 302 + cookie
- [ ] `GET /trips` returns 200 with the cookie (empty trip list is fine)
- [ ] No references to `oidc_subject`, `is_admin`, `merged_into_id`, `merge_audit`, `TripTraveler`, `TripMergeDismissal`, `created_by` anywhere in `src/` or `tests/`
- [ ] `pyproject.toml` no longer contains `types-passlib`
- [ ] `v2` branch pushed to origin
- [ ] Draft PR open against main for tracking

---

## What's next: Phase 12 plan

Phase 12 adds the TripIt-side schema surface (does not require live TripIt access — it's a pure schema change against a known design):

- Add `tripit_trip_id`, `tripit_synced_at`, `tripit_etag`, `upstream_deleted_at` to `trips`.
- Add `tripit_segment_id`, `tripit_segment_type`, `tripit_synced_at` to `segments`.
- Create `raw_text`, `raw_document`, `tripit_oauth_credentials`, `tripit_sync_state`, `tripit_notification_log`, `attach_audit` tables.

Phase 12 plan to be written next; can begin immediately after Phase 11 lands on `v2`.
