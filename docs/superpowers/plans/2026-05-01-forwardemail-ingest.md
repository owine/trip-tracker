# ForwardEmail Ingest Adapter — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new HTTP endpoint `POST /api/ingest/forwardemail` that accepts ForwardEmail.net's webhook JSON payload and feeds the contained raw MIME into trip-tracker's existing email-ingest pipeline. This unlocks live email forwarding (Gmail filter → manual forward → FE alias → trip-tracker → `/inbox`) without touching the existing `/api/ingest/email` HMAC contract used for direct programmatic ingestion.

**Architecture:** ForwardEmail's webhook contract is JSON-shaped (`{ raw, headers, attachments[], session, ... }`) and uses `X-Webhook-Signature` only on paid plans. Trip-tracker's existing `/api/ingest/email` expects raw MIME bytes plus an HMAC + timestamp + nonce triple. Rather than degrade the existing endpoint, this plan adds a sibling adapter route that:

1. Authenticates via a `?token=<random>` query-string parameter (FE preserves the URL exactly from the DNS TXT record, so the secret rides through).
2. Parses FE's JSON, extracts `payload["raw"]` as MIME bytes.
3. Reuses a refactored `_persist_raw_email()` helper that both this route and `ingest_email` share — single dedup-on-message-id + RawEmail insert + `enqueue_parse` codepath.
4. Returns `202` quickly (FE's endpoint timeout is 5s; the LLM parse runs out-of-band on the worker).

The existing worker → forwarding-alias → segment-parse → `/inbox` chain is untouched. Resolution still happens via `to_address.split("@", 1)[0].lower()` matched against `forwarding_aliases.local_part` (`worker.py:103`), which works because FE's `payload.raw` preserves the original `To:` header set when you forwarded the message to your alias address.

**Tech Stack:** Python 3.14 (target=py313), FastAPI, SQLAlchemy 2.0 async, existing `parse_mime` + `enqueue_parse` helpers. No new dependencies. No new migrations.

**Branch:** `feat/forwardemail-ingest`. Cut from `main` AFTER Phase 8 (`v0.8.0`) ships and merges. Do not start this work on the `feat/phase-8-expenses` branch.

**Trust model decisions (locked):**

- **Auth = `?token=<random>` query param only.** Compared via `hmac.compare_digest`. Rationale: FE's URL is preserved verbatim from the DNS TXT record (which lives in your private DNS zone), so the token rides through every delivery. Leaked tokens are trivial to rotate (env var change + DNS update). Reverse-PTR / IP-allowlist would add latency and brittleness without meaningful security gain in a single-tenant deployment. Defense-in-depth via FE's published IPs can be added later if the threat model changes.
- **No HMAC.** FE's `X-Webhook-Signature` is paid-plan only and signs the JSON body, not the inner MIME. Free-plan operators get nothing. The `?token=` covers the same role.
- **No timestamp/nonce.** The replay-cache machinery in `/api/ingest/email` defends against attackers replaying captured webhook bodies; the trust boundary here is "this URL is private to my DNS." If the URL leaks, replays are the smallest of your problems.

**Out of scope (followups, separate phases):**

- **Attachments → documents store.** FE includes `attachments[]` with Buffer-shaped `content.data`. Phase 5's `LocalFsStorage` could absorb these, but wiring them through requires base64 decoding, magic-byte gating per Phase 5's `documents.py`, and is its own ~half-day task. **Initial scope strips attachments via FE's `?attachments=false` querystring on the webhook URL.**
- **FE paid-plan HMAC verification.** If you upgrade to a paid plan and want their signature as additional defense-in-depth, add a new setting + an optional verification block. Token-only is fine for v1.
- **Multi-tenant token-per-user.** Single global token is the right call for a personal deployment. If trip-tracker ever supports multiple owners with their own FE aliases, swap to a `forwarding_aliases.relay_token` column.
- **A `/admin/raw-emails` "received via FE" badge.** Cosmetic. Not blocking.

**Toolchain quirks worth re-stating:**

- `from __future__ import annotations` at top of every new module.
- ruff `target=py313` + mypy `python_version=3.14`. PEP 585 (`list[...]`, `str | None`).
- `Settings` lives in `src/trip_tracker/config.py` as a Pydantic v2 `BaseSettings`. New `SecretStr` fields default to required (no default value) unless they're truly optional.
- The existing `_settings_dep` in `webhook.py` is the FastAPI dependency that returns the global `Settings` singleton.
- `parse_mime(body: bytes) -> ParsedEmail` (`ingest/mime.py`) returns a dataclass with `to_address`, `from_address`, `subject`, `message_id`, `headers`. Re-use directly.
- `enqueue_parse(settings, raw_email_id)` lives in `ingest/webhook.py` (or wherever Phase 3 put it post-saq migration). Re-use directly.

---

## File Structure

```
src/trip_tracker/
├── config.py                    [MODIFY: add forwardemail_relay_token SecretStr]
├── ingest/
│   ├── webhook.py               [MODIFY: extract _persist_raw_email() helper; existing route calls it]
│   └── forwardemail.py          [CREATE: new adapter route]
├── app.py                       [MODIFY: include forwardemail router]
└── ...

tests/
├── test_ingest_forwardemail.py  [CREATE]
└── fixtures/
    └── forwardemail_payload.json [CREATE: realistic FE webhook body for tests]

docs/
└── forwardemail-setup.md        [CREATE: ops walkthrough]

.env.example                     [MODIFY: add FORWARDEMAIL_RELAY_TOKEN line]
README.md                        [MODIFY: short pointer to docs/forwardemail-setup.md]
```

---

## Task 1 — `forwardemail_relay_token` setting + env

**Model:** haiku.

### Files
- Modify: `src/trip_tracker/config.py`
- Modify: `.env.example`
- Modify: `tests/test_config.py` (or whatever file holds the existing settings tests)

### Step 1.1 — Add the setting

In `src/trip_tracker/config.py` add a new `SecretStr` field. Keep it required (no default) so misconfiguration fails fast at boot:

```python
forwardemail_relay_token: SecretStr = Field(
    ...,
    description="Shared secret for the ForwardEmail webhook adapter. "
                "Compared with the ?token= query param via hmac.compare_digest.",
)
```

Place it next to `webhook_secret` for organisation. If `webhook_secret` lives in a "ingest" section comment block, this belongs in the same block.

### Step 1.2 — `.env.example`

Add one line under whatever section `WEBHOOK_SECRET` lives in:

```
# Token gating the ForwardEmail webhook adapter. 32 bytes of randomness is plenty.
# Generate with: python -c "import secrets; print(secrets.token_urlsafe(32))"
FORWARDEMAIL_RELAY_TOKEN=replace-me
```

### Step 1.3 — Test

Extend the existing settings test to assert the field exists and is required (boot fails without it). If there's no such test file yet, the implementer should add a minimal one or skip this step and rely on integration coverage from T3.

### Step 1.4 — Commit

```bash
git commit -am "feat(ingest): add forwardemail_relay_token setting"
```

- [ ] **Step 1.1–1.3:** Add setting + env example + test.
- [ ] **Step 1.4:** Commit.

---

## Task 2 — Extract `_persist_raw_email()` helper

**Model:** haiku.

### Why this refactor

Both ingest routes (existing `/api/ingest/email` and new `/api/ingest/forwardemail`) need identical persistence semantics: dedup on `message_id` via `ON CONFLICT DO NOTHING`, return the inserted row's UUID (or None for duplicates), enqueue `parse_raw_email` if the row is new. Today this lives inline in `webhook.py:108–147`. Pulling it into a function lets the new route call it without copying logic.

### Files
- Modify: `src/trip_tracker/ingest/webhook.py`

### Step 2.1 — Extract the helper

Add at module scope, above `ingest_email`:

```python
async def _persist_raw_email(
    db: AsyncSession,
    body: bytes,
    parsed: ParsedEmail,
) -> uuid.UUID | None:
    """Insert a raw_emails row if Message-ID is new. Returns inserted ID, or None on duplicate.

    Caller is responsible for the surrounding transaction. The single-statement
    INSERT ... ON CONFLICT DO NOTHING RETURNING id pattern is concurrency-safe
    under READ COMMITTED.
    """
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
        .returning(RawEmail.id)
    )
    result: CursorResult[tuple[()]] = await db.execute(stmt)  # type: ignore[assignment]
    return result.scalar_one_or_none()
```

### Step 2.2 — Update `ingest_email` to call it

Replace the inline INSERT block (currently `webhook.py:114–132`) with:

```python
async with db.begin():
    recorded = await record_nonce(db, ts_seconds=ts, nonce=nonce)
    replay = not recorded
    new_raw_email_id = await _persist_raw_email(db, body, parsed)
    duplicate = new_raw_email_id is None and not replay
```

The structured-log line and `enqueue_parse` call below it stay unchanged.

### Step 2.3 — Tests

The existing `tests/test_ingest_webhook.py` tests should still pass without modification — the refactor is behaviour-preserving. Run them. If anything breaks, the refactor is wrong.

### Step 2.4 — Commit

```bash
git commit -am "refactor(ingest): extract _persist_raw_email() for adapter reuse"
```

- [ ] **Step 2.1:** Add helper.
- [ ] **Step 2.2:** Wire `ingest_email` to use it.
- [ ] **Step 2.3:** Run `uv run pytest tests/test_ingest_webhook.py tests/test_ingest_hmac.py -v` — should be green.
- [ ] **Step 2.4:** Commit.

---

## Task 3 — Adapter route + tests

**Model:** sonnet (route logic + 4 tests with realistic FE-shaped fixture).

### Files
- Create: `src/trip_tracker/ingest/forwardemail.py`
- Modify: `src/trip_tracker/app.py` (mount the new router)
- Create: `tests/fixtures/forwardemail_payload.json`
- Create: `tests/test_ingest_forwardemail.py`

### Step 3.1 — The route module

`src/trip_tracker/ingest/forwardemail.py`:

```python
"""POST /api/ingest/forwardemail — ForwardEmail.net webhook adapter.

ForwardEmail posts JSON like { raw, headers, attachments[], session, ... }.
We extract `raw` (the original RFC-822 MIME) and feed it through the same
persistence path as /api/ingest/email. Auth is a shared ?token= query param;
no HMAC because FE only signs payloads on paid plans, and the inner MIME is
not signed regardless.
"""

from __future__ import annotations

import hmac
import json
import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from structlog import get_logger

from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.ingest.mime import parse_mime
from trip_tracker.ingest.webhook import _persist_raw_email, _settings_dep, enqueue_parse

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = get_logger()


@router.post("/forwardemail", status_code=status.HTTP_202_ACCEPTED)
async def ingest_forwardemail(
    request: Request,
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(_settings_dep),  # noqa: B008
) -> Response:
    # Step 1: Token gate. Constant-time compare so timing leaks don't help an attacker.
    expected = settings.forwardemail_relay_token.get_secret_value()
    provided = request.query_params.get("token") or ""
    if not hmac.compare_digest(expected, provided):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # Step 2: Parse JSON. FE's max body is bounded by your reverse-proxy config;
    # we don't enforce a separate cap here.
    try:
        body_bytes = await request.body()
        payload = json.loads(body_bytes)
    except json.JSONDecodeError:
        return JSONResponse({"error": "bad_request", "detail": "invalid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad_request", "detail": "expected JSON object"}, status_code=400)

    raw_str = payload.get("raw")
    if not isinstance(raw_str, str) or not raw_str:
        return JSONResponse(
            {"error": "bad_request", "detail": "missing or empty 'raw' field"},
            status_code=400,
        )
    mime_body = raw_str.encode()  # FE delivers raw as a string with \r\n line endings

    # Step 3: Parse MIME headers, persist row, enqueue worker.
    parsed = parse_mime(mime_body)

    new_id: uuid.UUID | None
    async with db.begin():
        new_id = await _persist_raw_email(db, mime_body, parsed)

    log.info(
        "ingest_forwardemail",
        status=202,
        to_address=parsed.to_address,
        from_address=parsed.from_address,
        message_id=parsed.message_id[:64],
        body_bytes=len(mime_body),
        duplicate_message_id=new_id is None,
        fe_recipient=(payload.get("session") or {}).get("recipient"),
    )

    if new_id is not None:
        await enqueue_parse(settings, new_id)

    return Response(status_code=202)
```

Notes:
- The `_settings_dep` and `enqueue_parse` are already exported (or implicitly available) from `webhook.py`. If they're module-private, T2 should have added `__all__` or the implementer adjusts imports during T3.
- We log `fe_recipient` (FE's `session.recipient`) AND `parsed.to_address` (from the inner MIME). They should match, but if they don't, logs surface the mismatch for debugging.

### Step 3.2 — Mount the router

In `src/trip_tracker/app.py`, alongside the existing `app.include_router(ingest_router)` call:

```python
from trip_tracker.ingest.forwardemail import router as forwardemail_router
app.include_router(forwardemail_router)
```

(Or whichever import-style the file already uses for routers.)

### Step 3.3 — Test fixture

`tests/fixtures/forwardemail_payload.json` — a minimal FE-shaped payload with a real-enough `raw` MIME inside. Keep it small; this is for unit tests, not a parser smoke test:

```json
{
  "raw": "From: forwarder@example.com\r\nTo: me@trips.example.com\r\nSubject: Fwd: Test\r\nMessage-ID: <fe-test-001@example.com>\r\nContent-Type: text/plain\r\n\r\nbody\r\n",
  "headers": {},
  "headerLines": [],
  "html": "",
  "text": "body",
  "from": {"value": [{"address": "forwarder@example.com"}]},
  "messageId": "<fe-test-001@example.com>",
  "recipients": ["me@trips.example.com"],
  "session": {
    "recipient": "me@trips.example.com",
    "sender": "forwarder@example.com",
    "arrivalDate": "2026-05-01T12:00:00.000Z"
  }
}
```

### Step 3.4 — Tests

`tests/test_ingest_forwardemail.py` — 4 tests using the existing async test client + DB fixtures:

```python
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from trip_tracker.models.raw_email import RawEmail

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "forwardemail_payload.json").read_text())


@pytest.mark.asyncio
async def test_forwardemail_no_token_rejected(client):
    r = await client.post("/api/ingest/forwardemail", json=_FIXTURE)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_forwardemail_wrong_token_rejected(client):
    r = await client.post("/api/ingest/forwardemail?token=wrong", json=_FIXTURE)
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_forwardemail_missing_raw_rejected(client, fe_token):
    payload = {**_FIXTURE}
    del payload["raw"]
    r = await client.post(f"/api/ingest/forwardemail?token={fe_token}", json=payload)
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_forwardemail_happy_path_persists_and_enqueues(
    client, db_session, fe_token, mock_enqueue_parse
):
    r = await client.post(f"/api/ingest/forwardemail?token={fe_token}", json=_FIXTURE)
    assert r.status_code == 202
    rows = (await db_session.execute(select(RawEmail))).scalars().all()
    assert len(rows) == 1
    assert rows[0].to_address == "me@trips.example.com"
    assert rows[0].message_id == "<fe-test-001@example.com>"
    mock_enqueue_parse.assert_called_once()
```

Fixtures the implementer needs to wire up:

- `fe_token` — yields the value of `settings.forwardemail_relay_token` so tests don't hard-code it. Add to `conftest.py`.
- `mock_enqueue_parse` — patches `trip_tracker.ingest.forwardemail.enqueue_parse` (NOT the webhook one — Python imports rebind by name at module load, and the adapter module captures its own reference). Use `monkeypatch.setattr` or `unittest.mock.patch`.

If the existing `tests/conftest.py` already has a `client` fixture that auto-applies a Settings override with a known `webhook_secret`, extend it to also set a known `forwardemail_relay_token` (e.g., `"test-fe-token"`) and have `fe_token` yield that.

### Step 3.5 — Quality gates

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy src tests
uv run pytest tests/test_ingest_forwardemail.py -v
uv run pytest -x -q  # full suite, no regressions
```

### Step 3.6 — Commit

```bash
git commit -am "feat(ingest): ForwardEmail webhook adapter route"
```

- [ ] **Step 3.1–3.2:** Route module + app mount.
- [ ] **Step 3.3:** Fixture JSON.
- [ ] **Step 3.4:** 4 tests pass.
- [ ] **Step 3.5:** All gates green.
- [ ] **Step 3.6:** Commit.

---

## Task 4 — Operations doc

**Model:** inline (the user types this; or a haiku writer if delegated).

### Files
- Create: `docs/forwardemail-setup.md`
- Modify: `README.md` (one-line pointer)

### Content for `docs/forwardemail-setup.md`

Cover, concisely:

1. **Prerequisites** — domain pointed at FE MX, an alias configured in FE's dashboard (or DNS TXT for free plan), trip-tracker deployed at a public HTTPS URL.

2. **Generate the relay token.**
   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```
   Set this as `FORWARDEMAIL_RELAY_TOKEN` in your deployment env. Restart the app.

3. **Create the matching `forwarding_aliases` row** in trip-tracker. (Reference the `/admin/aliases` UI added in Phase 2.) The `local_part` must equal the local-part of the FE alias address (case-insensitive — worker lowercases before matching).

4. **Set the FE webhook URL** to:
   ```
   https://trips.example.com/api/ingest/forwardemail?token=<TOKEN>&attachments=false
   ```
   - `?token=` — the value from step 2.
   - `?attachments=false` — strips attachment buffers from FE's payload. Trip-tracker doesn't currently absorb FE attachments into `documents`; leaving `attachments=true` just makes the payload bigger and risks the 5s endpoint timeout.

5. **Smoke test.**
   - From your phone or desktop mail client, manually forward an existing flight/hotel confirmation to the FE alias.
   - Watch `docker compose logs -f web worker` (or the equivalent for your deployment).
   - Expect: `ingest_forwardemail status=202` log line within seconds, then `parse_raw_email` worker log a few seconds later, then a new row in `/inbox`.

6. **Troubleshooting.**
   - **401 from the adapter:** token mismatch. Check the URL in FE's dashboard exactly matches the env var.
   - **400 missing 'raw':** FE's `?raw=false` querystring filter is on. Remove it.
   - **202 but nothing in `/inbox`:** alias mismatch. The MIME's `To:` local-part doesn't match any `forwarding_aliases.local_part` row, so the worker marks the email `no_segments`. Check `/admin/raw-emails`.
   - **Duplicate Message-ID rejected silently:** that's the dedup path working as designed. If you forward the same email twice, only the first one creates a segment.

7. **Rotating the token.** Generate a new value, update the env var + redeploy, update the FE webhook URL. There's no key-rotation grace window — if you do it out of order, deliveries 401 until both sides match.

### `README.md` addition

Under whatever "Email ingestion" section already exists, one line:

> **ForwardEmail.net users:** see [`docs/forwardemail-setup.md`](docs/forwardemail-setup.md) for an alternative ingest path that accepts FE's webhook JSON directly.

### Commit

```bash
git commit -am "docs(ingest): ForwardEmail setup guide"
```

- [ ] **Step 4.1:** Write `docs/forwardemail-setup.md`.
- [ ] **Step 4.2:** Add README pointer.
- [ ] **Step 4.3:** Commit.

---

## Task 5 — Live smoke verification

**Model:** inline (operator runs this against a real deployment).

This is not a test that runs in CI. It's the actual "does it work?" check before merging.

### Pre-flight checks

- [ ] Branch `feat/forwardemail-ingest` is rebased on latest `main`.
- [ ] All four prior tasks committed; `git log --oneline main..HEAD` shows the expected 4 commits.
- [ ] Full test suite green locally: `uv run pytest -x -q`.
- [ ] The deployment target has `FORWARDEMAIL_RELAY_TOKEN` set.
- [ ] A `forwarding_aliases` row exists for the FE alias's local-part, attached to your user.

### The actual smoke

1. Push the branch and deploy (whatever your deploy flow is — direct-to-main per your conventions, or PR-then-merge if you prefer review).
2. Configure the FE webhook URL in their dashboard to your deployed endpoint with `?token=` and `?attachments=false`.
3. From your phone, find an old flight confirmation in your mail client and forward it to your FE alias.
4. Watch logs for the `ingest_forwardemail status=202` line and the subsequent `parse_raw_email` worker log.
5. Visit `/inbox` in the trip-tracker UI. The forwarded email should appear in the `review` bucket (or `no_segments` if the parsers can't extract anything — that's a parser problem, not an adapter problem).
6. Confirm or edit the segment.

### Acceptance

- [ ] At least one real forwarded email landed in `/inbox` end-to-end.
- [ ] Re-forwarding the same email a second time produces a duplicate-Message-ID log line, NOT a new `/inbox` row.
- [ ] An invalid `?token=` value produces a 401 in logs.

### Merge to main

If acceptance passes, fast-forward merge to main. No version tag is needed for an ingest adapter — the changelog entry is enough. Bump the next phase's `vN.N.0` tag with a note that FE ingest is supported.

---

## Followup tickets to file (do NOT do them in this branch)

1. **FE attachments → documents store.** Decode the base64 buffers from `payload.attachments[]`, magic-byte gate per Phase 5, write through `LocalFsStorage`, link to the resulting `RawEmail` row. ~half-day. Do AFTER the parser smoke confirms FE delivery is reliable end-to-end.
2. **FE paid-plan HMAC verification.** If you ever upgrade to a paid plan, add `forwardemail_signature_key: SecretStr | None` to settings and an `if expected_sig: verify(...)` block in the adapter. Keep token-only as the fallback when the key is unset.
3. **`/admin/raw-emails` "FE" badge.** Add a `parse_source_hint` column to `raw_emails` or sniff the `X-Forward-Email-*` headers FE injects, surface in the admin list. Cosmetic.
4. **Multi-tenant token-per-user.** When trip-tracker grows past single-owner, each `forwarding_aliases` row gets its own `relay_token` and the adapter looks up the token from the URL path or a header. Substantial work; revisit when there's a real use case.

---

## Verification gate (definition of done for this branch)

- [ ] All 4 tasks committed (or 5 if Task 1's standalone settings test is its own commit).
- [ ] Full test suite green; new tests in `tests/test_ingest_forwardemail.py` pass.
- [ ] Live smoke per Task 5 passed against a real FE delivery.
- [ ] `docs/forwardemail-setup.md` exists and reads cleanly to someone who isn't you.
- [ ] No regressions in existing `tests/test_ingest_webhook.py` (the T2 refactor must be behaviour-preserving).

When all five are checked, fast-forward merge `feat/forwardemail-ingest` into `main`. Done.
