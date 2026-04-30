# Migrate arq → saq

> **For agentic workers:** Single-task migration plan. Dispatch as one sonnet implementer (touches webhook + worker + compose; non-trivial integration). After this lands, the redis-py 6/7 bump becomes reachable.

**Goal:** Replace `arq` with `saq` as the async task queue. Redis stays. The single task — `parse_raw_email(raw_email_id: str)` — keeps its semantics. Operationally identical from the user's perspective.

**Why now:** arq entered maintenance-only mode 2026-04-16 per upstream issue #510 (statement from Samuel Colvin, the maintainer + Pydantic). No new features, security-only fixes, no commitment to ongoing maintenance. arq's redis-py `<6` cap is now permanent — blocking our redis-py 6/7 bump and quietly accumulating a vulnerability surface area as redis-py advances. saq is the closest async-Redis analog with active maintenance and modern redis-py support.

**Risk level:** MEDIUM. The task function body is unchanged. Surface area: imports, decorators, enqueue calls, worker entrypoint, docker-compose service command, env var name. Test coverage protects most of this — the `WorkerSettings` startup/shutdown tests need rewriting against saq's `Worker` constructor.

---

## Migration target: saq

- **Library:** `saq[hiredis]` (required: the plain `saq` extra still pins `redis<7`, but `saq[hiredis]` has no cap)
- **Latest:** 0.26.3 (2026-03-04), actively maintained
- **Async story:** native async, Redis-only — the spiritual successor to arq
- **Bonus:** built-in web UI for job inspection
- **Stars/maintainership signal:** ~845 stars, regular commits, used in production by the sqlglot/sqlmesh team

---

## Files Touched

| File | Change |
|---|---|
| `pyproject.toml` | Replace `arq>=0.28,<0.29` with `saq[hiredis]>=0.26,<0.27`. Bump `redis` from `>=5,<6` to `>=7,<8` (now reachable). |
| `src/trip_tracker/worker.py` | Replace `WorkerSettings` class with saq's `Queue` + `Worker` setup. `parse_raw_email` body unchanged. |
| `src/trip_tracker/ingest/webhook.py` | Replace `enqueue_parse` (uses `arq.create_pool`) with saq queue enqueue. |
| `src/trip_tracker/__main__.py` | Update `parse_pending` to use saq's enqueue API. |
| `docker-compose.yml` | Worker `command:` changes from `["arq", "trip_tracker.worker.WorkerSettings"]` to `["saq", "trip_tracker.worker.settings"]`. |
| `tests/test_worker.py` | Rewrite the WorkerSettings.startup/shutdown tests against saq's `Worker` lifecycle. The `parse_raw_email` task tests are unchanged (still call the function directly). |
| `README.md` | Update the "How parsers work" section's reference to ARQ. |

---

## Pre-flight Reading

- saq docs: <https://saq.readthedocs.io/>
- The settings-dict pattern: <https://saq.readthedocs.io/en/latest/usage.html#settings>
- Retry / backoff: <https://saq.readthedocs.io/en/latest/usage.html#tasks> (the `retries=N` kwarg + `Job.error` exposure)
- Queue serialization: arq queue keys live under `arq:`, saq under `saq:` — no overlap, so any in-flight arq jobs are dropped silently at cutover. **This is fine** because the docker-compose Redis service has no volume; queue is ephemeral by design.

---

## Migration Steps

### Step 1 — Swap dependencies

```bash
uv remove arq
uv add 'saq[hiredis]>=0.26,<0.27'
uv add 'redis>=7,<8'  # now reachable since saq doesn't cap
uv lock --upgrade
```

Verify `pyproject.toml`:
- `arq` removed
- `saq[hiredis]>=0.26,<0.27` present
- `redis>=7,<8` present

### Step 2 — Rewrite `src/trip_tracker/worker.py`

Replace the whole `WorkerSettings` class with the saq-native shape. The task body itself is unchanged:

```python
"""saq worker: parses RawEmail rows in the background.

Runs in a separate container from the FastAPI app, sharing the same image:
    command: ["saq", "trip_tracker.worker.settings"]

Single task `parse_raw_email(raw_email_id)` is enqueued by the webhook
handler after RawEmail is committed.
"""

from __future__ import annotations

import logging
import uuid
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as email_policy_default
from typing import Any

from saq import Queue
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import trip_tracker.parsers.vendors  # noqa: F401  # register all packs
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

logger = logging.getLogger(__name__)


async def parse_raw_email(ctx: dict[str, Any], *, raw_email_id: str) -> None:
    """Parse one RawEmail and persist the result.

    Idempotent: re-running on an already-parsed RawEmail is a no-op.
    saq passes kwargs through `ctx` for the function's keyword args (note
    the kw-only signature). Engine and settings live in the worker context.
    """
    settings: Settings = ctx["settings"]
    engine = ctx["engine"]
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    rid = uuid.UUID(raw_email_id)
    async with SessionMaker() as db:
        # ... (body unchanged from the arq version — same DB queries,
        # same dispatch_parse + cluster_for_user + segment writes)
        ...


async def startup(ctx: dict[str, Any]) -> None:
    """Build worker-process singletons. saq calls this once when the
    worker boots."""
    s = Settings()
    ctx["settings"] = s
    ctx["engine"] = create_async_engine(str(s.database_url))


async def shutdown(ctx: dict[str, Any]) -> None:
    """Dispose the engine on graceful shutdown."""
    engine = ctx.get("engine")
    if engine is not None:
        await engine.dispose()


# saq picks up `settings` (a dict) when invoked via `saq trip_tracker.worker.settings`.
_SETTINGS = Settings()
queue = Queue.from_url(_SETTINGS.redis_url)

settings = {
    "queue": queue,
    "functions": [parse_raw_email],
    "startup": startup,
    "shutdown": shutdown,
    "concurrency": 1,  # one task at a time per worker; matches arq's effective default
    # Retries: per-task `retries=5` set at enqueue time, OR via @saq.utils.backoff decorator
    # We choose the enqueue-time approach — see webhook.py.
}
```

The actual function body (the ~80 lines from the existing worker.py — `db.get(RawEmail, rid)`, the alias lookup, `dispatch_parse`, `record_usage`, the cluster + segment write loop, the `parse_status` flip) is **unchanged**. Just paste it inside the new function signature.

**Key API differences:**
- `ctx` is a dict (same as arq).
- Task signature: kw-only args after `ctx` (saq passes enqueue kwargs as kwargs, not positional).
- `Queue.from_url(redis_url)` replaces `RedisSettings.from_dsn(...)`.
- No `WorkerSettings` class — saq uses a `settings` dict that the CLI loads.

### Step 3 — Rewrite `src/trip_tracker/ingest/webhook.py`

Replace `enqueue_parse`:

```python
from saq import Queue

from trip_tracker.config import Settings


async def enqueue_parse(settings: Settings, raw_email_id: uuid.UUID) -> None:
    """Enqueue parse_raw_email task. Failure is logged but not propagated —
    the parse_pending admin command is the recovery path."""
    try:
        q = Queue.from_url(settings.redis_url)
        await q.enqueue(
            "parse_raw_email",
            raw_email_id=str(raw_email_id),
            retries=5,  # max attempts, including initial — saq does exponential backoff between
        )
        await q.disconnect()
    except Exception as exc:
        logger.warning("enqueue_parse failed for %s: %s", raw_email_id, exc)
```

**Key differences:**
- `q.enqueue(name, **kwargs, retries=N)` replaces `q.enqueue_job(name, *args)`. Tasks take kwargs in saq.
- `q.disconnect()` (or `q.aclose()` depending on saq version) replaces `q.aclose()`.
- The retry count is set at enqueue time (per-job) rather than worker-globally as in arq.

### Step 4 — Update `src/trip_tracker/__main__.py`

`parse_pending` re-enqueues all `parse_status='pending'` RawEmails. Same shape, but use the new `enqueue_parse`:

```python
# unchanged: imports of enqueue_parse from webhook
# unchanged: query for pending rows
# unchanged: loop calling `await enqueue_parse(settings, rid)` per row
```

The function body stays — only `enqueue_parse`'s implementation is different (Step 3).

### Step 5 — docker-compose

In `docker-compose.yml`, the worker service's `command:`:

```yaml
trip-tracker-worker:
  ...
  command: ["saq", "trip_tracker.worker.settings"]
```

(was `["arq", "trip_tracker.worker.WorkerSettings"]`)

The Redis service is unchanged. The env vars (`REDIS_URL`, etc.) are unchanged. The dev compose's port forward unchanged.

### Step 6 — Rewrite the WorkerSettings tests

`tests/test_worker.py` has two tests specifically targeting arq's `WorkerSettings.startup`/`shutdown`:
- `test_worker_settings_startup_creates_engine`
- `test_worker_settings_shutdown_no_engine_is_safe`

Replace with tests against saq's `startup` / `shutdown` functions directly:

```python
@pytest.mark.asyncio
async def test_worker_startup_creates_engine(
    db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """startup() populates ctx['engine'] and ctx['settings']."""
    monkeypatch.setenv("DATABASE_URL", db_url)
    from trip_tracker.worker import shutdown, startup

    ctx: dict = {}
    await startup(ctx)
    assert "engine" in ctx
    assert "settings" in ctx
    await shutdown(ctx)


@pytest.mark.asyncio
async def test_worker_shutdown_no_engine_is_safe() -> None:
    """shutdown() doesn't raise when no engine was created."""
    from trip_tracker.worker import shutdown

    await shutdown({})  # empty ctx
```

Other worker tests (`test_parse_raw_email_*` — 7 of them) are **unchanged**. They invoke `parse_raw_email` directly with a constructed `ctx` dict, which works identically under saq.

The webhook test `test_webhook_enqueues_parse_task` patches `trip_tracker.ingest.webhook.enqueue_parse` — that patch target is unchanged.

### Step 7 — Run + verify

```bash
uv run pytest tests/test_worker.py -v       # 10 tests, expect 10 green
uv run pytest -q                              # full suite, expect 204+ passing
uv run ruff check . && uv run mypy src
uv run pre-commit run --all-files
```

Note the pre-commit mypy hook may need `saq` added to `additional_dependencies` (we just hit this with anthropic in commit `b9ef779`).

### Step 8 — Update README

Replace ARQ references in the "How parsers work" section. Specifically the line:
> "When a forwarding email arrives at /api/ingest/email, an ARQ worker runs three strategies in priority order:"

becomes:
> "When a forwarding email arrives at /api/ingest/email, a saq worker runs three strategies in priority order:"

### Step 9 — Commit + push

Single commit, infra-class change (per workflow memory: "infra direct to main"):

```bash
git add pyproject.toml uv.lock src/trip_tracker/worker.py \
        src/trip_tracker/ingest/webhook.py src/trip_tracker/__main__.py \
        docker-compose.yml tests/test_worker.py README.md \
        .pre-commit-config.yaml  # if mypy hook deps updated
git commit -m "refactor(worker): migrate arq → saq; bump redis to 7

arq entered maintenance-only mode 2026-04-16 (issue #510, statement
from upstream maintainer). The redis-py <6 cap is now permanent.
saq is the closest async-Redis analog with active maintenance and
modern redis-py support.

Migration is API-shape only — task body unchanged. saq's settings
dict replaces WorkerSettings class; Queue.from_url replaces
RedisSettings.from_dsn; q.enqueue(name, **kwargs, retries=N)
replaces q.enqueue_job(name, *args). Worker container command
flips from 'arq ...' to 'saq ...'.

Bumps redis to >=7,<8 — now reachable since saq[hiredis] doesn't
cap. Closes the gap that's been blocked since v0.3.0."
git push origin main
```

### Step 10 — Production verification

After merge + container rebuild, smoke-check:

```bash
docker compose ps  # worker container running, Redis healthy
docker compose logs trip-tracker-worker | tail -20  # saq's "Worker starting" message
docker compose exec trip-tracker-app python -m trip_tracker parse_pending --dry-run
```

Send one test email through the webhook; check `/admin/raw-emails` and `/inbox` for the result.

---

## What Could Go Wrong

| Failure mode | Detection | Mitigation |
|---|---|---|
| saq's `Queue.from_url` doesn't accept our redis://...:6379/0 format | Worker container fails to start | Inspect saq docs; URL parse may need `redis_url` env var name vs constructor arg |
| Task signature mismatch (saq passes kwargs, not args) | Task fails on first call with TypeError | Already handled in Step 2 — `parse_raw_email(ctx, *, raw_email_id)` is kw-only |
| Retry semantics diverge (arq's `max_tries=5` worker-wide vs saq's per-job `retries=5`) | Some emails retry more/fewer times than expected | Per-job retries=5 at enqueue is the explicit choice; document |
| saq web UI exposes job data | Possible PII concern in self-hosted single-user setup | Don't expose its port externally; default config doesn't auto-expose |
| Mypy hook can't resolve saq stubs | Pre-commit fails | Add `saq[hiredis]>=0.26` to mypy hook's `additional_dependencies` (same fix as anthropic) |

---

## Rollback Plan

The Redis container has **no volume**, so any in-flight queue state is ephemeral. Rolling back to arq is safe.

**If discovered before tag:**

```bash
git revert <migration-commit-sha>
uv lock
git push origin main
```

**If discovered after tag pushed:**

1. Revert the migration commit on main.
2. Next tag (v0.3.x or v0.4.0) reverts to arq pinned at 0.28.
3. After rollback deploy: `python -m trip_tracker parse_pending` re-enqueues any RawEmails left mid-flight.

**State that needs cleanup on rollback:**
- Any saq-prefixed Redis keys persist until Redis is restarted/flushed. Since the queue is ephemeral by design, this doesn't matter — they're orphaned but harmless.
- The Postgres `llm_budget` table is library-agnostic; unaffected.
- The `parse_status` enum values are unchanged.

**Revert blast radius:** trip-tracker only.

---

## Done Definition

- All tests pass (≥85% coverage).
- mypy + ruff + bandit + djlint + pre-commit clean.
- Docker build succeeds.
- Worker container starts cleanly under saq.
- Webhook → saq enqueue → worker → DB write end-to-end works against a real Redis 7 container.
- README updated.
- Renovate dashboard no longer flags arq (because it's gone) and redis (because we're now on 7).

---

## When to execute

**Recommended slot: bundle into Phase 4 ("Search & geocoding").**

Reasoning:
- Phase 4 is feature work that justifies a v0.4.0 tag, naturally bundling the migration.
- Migration is a multi-file refactor with cross-component coordination — feels heavier than the in-flight phase 3 deferred-bumps batch.
- Phase 4's brainstorm hasn't started yet; the migration plan can land as the first task of Phase 4 (zero risk to v0.3.0's soak window).
- Doing the migration AS Phase 4 starts lets the worker-startup smoke test be part of the v0.4.0 verification gate — same Playwright + docker-compose dance as v0.2.0/v0.3.0.

**Alternative slot: standalone v0.3.1 patch tag.**
- If Phase 4 is more than a week away, justifying a standalone v0.3.1 for the migration alone is reasonable.
- Decision criterion: are you actively using arq's queue right now? If yes, the redis-py 5 → 7 vulnerability surface is real and v0.3.1 makes sense. If you're not actively forwarding emails yet, bundle into Phase 4.

**Not recommended:** rushed bump on top of the v0.3.0 soak window. Two PRs landing on top of each other (arq bump + arq → saq migration in the same week) muddies the soak signal and makes rollback attribution harder.
