# redis-py Bump: BLOCKED upstream

**Goal:** Track when `redis-py` 6.x or 7.x becomes reachable for this project. Renovate currently flags both as "available updates."

**Status as of 2026-04-30:** **BLOCKED.** This plan does not execute today. It exists so future-Claude knows why and what condition must change.

---

## Why this is blocked

Our `redis` Python pin (`>=5,<6`) is a transitive requirement of `arq` (the task queue library). arq's own pin caps `redis-py` at `<6`, even in the latest arq 0.28.0:

| arq version | redis-py constraint |
|---|---|
| 0.25.0 | `>=4.2.0` (no upper) |
| 0.26.0 | `>=4.2.0,<5` |
| 0.26.1+ | `>=4.2.0,<6` |
| 0.27.0 | `>=4.2.0,<6` |
| **0.28.0** | `>=4.2.0,<6` |

Bumping our `redis` pin to `>=6,<7` or `>=7,<8` would make `uv lock` fail — arq's constraint wins.

**The unblocking condition** is upstream arq raising its `<6` cap. Tracking issue: <https://github.com/python-arq/arq/issues/458> (open as of research; closed-as-duplicate but no code fix has shipped).

---

## What we're missing by staying on redis-py 5

Research dated 2026-04-30 surveyed redis-py 5 → 6 → 7 changes:

**redis-py 6.0** (May 2025):
- `ssl_check_hostname` default flipped to `True` — Sentinel + SSL setups need to compensate.
- `charset` and `errors` kwargs removed from `Redis.__init__`.
- Async `RedisCluster`: `connection_error_retry_attempts` removed, `cluster_error_retry_attempts` deprecated.
- `RedisGears` and `RedisGraph` module support removed.
- Search default dialect bumped to 2.

**redis-py 7.0** (Oct 2025):
- Sync context-manager removed from async `RedisCluster`.
- Internal `threading.Lock` → `RLock` (deadlock prevention).
- Type-hint cleanups in async `BlockingConnectionPool`.
- Python 3.9 support removed in 8.0 beta (7.x stable still supports 3.9+).
- **No protocol default change** (RESP2 → RESP3 default flip is in 8.0 beta, not 7.x stable).

**Our use:** We only call `arq`'s wrappers (`RedisSettings.from_dsn`, `create_pool`, `enqueue_job`, `aclose`) plus standalone async via `redis.asyncio.Redis`. Cluster features are unused. RedisGears/RedisGraph are unused. None of the breaking changes affect us in practice — we're not "missing" anything that costs us today.

---

## Trigger: when this plan executes

Re-evaluate on any of:

1. **arq publishes a release that lifts the `<6` cap.** Watch <https://github.com/python-arq/arq/releases> via Renovate.
2. **arq becomes unmaintained** and we migrate to a different task queue (e.g., dramatiq, celery, taskiq). Different plan; this one is moot.
3. **A redis-py 5.x security advisory is published.** Then we either pressure arq's maintainer or vendor a temporary fork.
4. **6 months pass with no arq cap-lift movement** (i.e., 2026-10-30). Re-survey: maybe arq has been replaced upstream by a fork that DOES support newer redis-py.

---

## Migration steps (FOR FUTURE — once unblocked)

When arq's `redis<6` cap is lifted to `<7` or `<8`:

- [ ] **Step 1 — Confirm new arq + redis combo**

  ```bash
  pip index versions arq  # check the latest
  pip show arq | grep Requires  # check its redis pin
  ```

- [ ] **Step 2 — Bump both in one PR**

  Bump `arq` AND `redis` pins simultaneously. They're coupled; bumping one without the other risks `uv lock` failure or a transient solver-resolved combo we didn't pick.

  ```bash
  uv add 'arq@^X.Y' 'redis@^N'
  uv lock --upgrade-package arq --upgrade-package redis
  ```

- [ ] **Step 3 — Verify worker still starts + tests pass**

  Same gate as the arq plan: `WorkerSettings` instantiates, worker tests green, full suite green, pre-commit clean.

- [ ] **Step 4 — Soak + tag**

  7-day soak on main. The combined arq + redis bump warrants the longer window even if individual changes are small.

---

## Rollback Plan (when this eventually executes)

Same shape as the arq plan: Redis volume is ephemeral, so any in-flight tasks are lost regardless. Rollback is a one-line `git revert` + `uv lock`.

The only state that lives in Redis is the ARQ task queue (no persistence configured; `# No volume — queue is ephemeral, parse_pending recovers backlog.`). The recovery path is `python -m trip_tracker parse_pending` to re-enqueue any RawEmails caught mid-flight.

---

## Action: monitor + re-evaluate

This plan does NOT execute today. Suggested follow-up: **schedule a quarterly remote agent** to check arq's GitHub for cap-lift movement. Sample prompt for the agent:

> Check <https://github.com/python-arq/arq/blob/main/pyproject.toml> for the redis-py pin. If it's been lifted to `<7` or higher, open a tracking issue in this repo (`owine/trip-tracker`) titled "arq lifted redis cap — reactivate redis-bump plan" with a link to docs/superpowers/plans/2026-04-30-redis-bump-blocked.md. If still `<6`, do nothing.
