# Phase 6 — ICS Subscribable Feed Design

**Status:** Approved (brainstorm 2026-04-30, owine + Claude).
**Target tag:** `v0.6.0`.
**Predecessors:** Phase 1 (auth), Phase 2 (raw-email webhook), Phase 3 (parsers + saq worker), Phase 4 (Meilisearch + ⌘K palette), Phase 5 (documents).
**Successor (sketched):** Phase 6.x — per-type SUMMARY polish; per-device multi-token model; trip-level feed; ETag/If-Modified-Since.

---

## 1. Goal

Expose the user's segment timeline as a subscribable iCalendar feed. A token-signed URL — `/ics/<token>.ics`, Authelia-exempt — returns RFC 5545 `text/calendar` content that any standard calendar client (Apple Calendar, Google Calendar, Thunderbird) can subscribe to and refetch periodically. Read-only; no write surface.

**Out of scope for v0.6.0:** multiple feeds per user, per-segment-type SUMMARY polish, trip-level events, ETag/304-on-no-change, push notifications, separate "disable" mechanism (regenerate-only revocation), audit trail for fetches.

---

## 2. Scope decisions (locked during brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Event grain | Segments only — one VEVENT per Segment (no trip-level events) |
| 2 | Time scope | All segments ever — no past/future filter |
| 3 | VEVENT shape | Rich uniform (URL deep-link + 3hr flight VALARM); per-type polish deferred |
| 4 | Token storage at rest | Hashed (sha256) on `users.ics_token_hash`; UNIQUE index |

---

## 3. Architecture overview

```
┌──────────────────────┐      GET /ics/<token>.ics      ┌──────────────────┐
│ Calendar Client      │ ─────────────────────────────► │  Traefik         │
│ (Apple/Google/Thunder)│   (Authelia-exempt path)       │  /ics/ → app     │
└──────────────────────┘ ◄───── text/calendar ────────  └────────┬─────────┘
                                                                  │
                                                                  ▼
                                              ┌─────────────────────────────────┐
                                              │  routes/ics.py                  │
                                              │  resolve_token(token, db)       │
                                              │  → User | None  (404 on miss)   │
                                              └────────────┬────────────────────┘
                                                           ▼
                                  SELECT segments JOIN trip_travelers
                                  WHERE tt.user_id = :uid ORDER BY start_at
                                                           ▼
                                              ┌─────────────────────────────────┐
                                              │  ics/render.py                  │
                                              │  render_calendar(user, segs)    │
                                              │  → RFC 5545 text                │
                                              └─────────────────────────────────┘
```

**Public surface:** one auth-less GET route. **Settings:** generate / regenerate token; show plaintext URL exactly once at generation. **Storage:** one new `text` column on `users`.

---

## 4. Data model

### 4.1 New column

```sql
ALTER TABLE users
  ADD COLUMN ics_token_hash text,
  ADD CONSTRAINT uq_users_ics_token_hash UNIQUE (ics_token_hash);

CREATE INDEX ix_users_ics_token_hash ON users(ics_token_hash);
```

- **Nullable.** A user has no feed URL until they generate one (opt-in).
- **UNIQUE.** Defends against (vanishingly unlikely) hash collisions on the 256-bit space; also gives the index for O(log n) lookup.
- **Hashed via `hashlib.sha256(token_bytes).hexdigest()`** — 64 hex chars. No salt: the input is a 256-bit cryptographic random, immune to dictionary/rainbow attacks. SHA-256 is fast; lookup-by-hash stays O(log n) via the index.

### 4.2 Why hashed, not bcrypt/argon2

The token is **256 bits of cryptographic randomness**, not a user-chosen password. There's no offline-brute-force advantage to slow hashing. SHA-256 is sufficient and keeps the lookup path cheap. This matches the project's existing convention (Phase 2 webhook secret is also unsalted-sha256 in spirit; the master spec §Threat-Model risk #6 says "Share tokens hashed at rest").

---

## 5. Token lifecycle

### 5.1 Helpers — `src/trip_tracker/ics/tokens.py`

```python
import hashlib
import secrets


def generate_token() -> tuple[str, str]:
    """Returns (plaintext, hash). Caller stores ONLY the hash."""
    plaintext = secrets.token_urlsafe(32)   # ~43 URL-safe chars
    hash_ = hashlib.sha256(plaintext.encode()).hexdigest()
    return plaintext, hash_


def hash_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode()).hexdigest()


async def resolve_token(plaintext: str, db: AsyncSession) -> User | None:
    h = hash_token(plaintext)
    return (
        await db.execute(select(User).where(User.ics_token_hash == h))
    ).scalar_one_or_none()
```

### 5.2 Operations

| Operation | Effect |
|---|---|
| **Generate** (first time)        | `plaintext, hash_ = generate_token(); user.ics_token_hash = hash_`; flash plaintext URL **ONCE**; commit. |
| **Regenerate**                  | Same as generate; old hash overwritten in place; old URL returns 404 on next fetch. |
| **Resolve** (every feed fetch)  | `select(User).where(User.ics_token_hash == sha256(token))`; 404 on miss. |
| **Revocation**                  | v0.6.0 ships regenerate only. To revoke without replacing: SQL `UPDATE users SET ics_token_hash = NULL`; deferred as a Settings "Disable feed" button if a real need surfaces. |
| **Expiry**                      | None. Calendar feeds are long-lived in client config; silent expiry breaks subscriptions. Regeneration is the explicit revoke path. |

### 5.3 The plaintext is never re-displayed

After the one-time toast at generation, `users.ics_token_hash` carries no recoverable form of the secret. Settings shows a hash-suffix-only presence indicator (e.g., `Calendar feed: enabled · ●●●●●...a3f7c`) so the user knows a token exists, but the URL itself is gone unless they saved it. Forgetting the URL = regenerate.

---

## 6. ICS serialization (`src/trip_tracker/ics/render.py`)

### 6.1 Calendar wrapper

```ics
BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//trip-tracker//EN
CALSCALE:GREGORIAN
METHOD:PUBLISH
NAME:Trip-Tracker · <user.display_name>
X-WR-CALNAME:Trip-Tracker · <user.display_name>
…<one VEVENT per segment, ordered by start_at>…
END:VCALENDAR
```

`X-WR-CALNAME` is non-standard but every major client (Apple, Google, Thunderbird) honors it for the subscription label. `NAME` is the RFC-7986 standard equivalent.

### 6.2 Per-segment VEVENT

```ics
BEGIN:VEVENT
UID:<segment_id>@<host>
DTSTAMP:<now in UTC, basic format>
DTSTART:<segment.start_at in UTC, basic format>
DTEND:<segment.end_at in UTC, OR start_at + 1h if NULL>
SUMMARY:<icon> <type-aware title>
LOCATION:<best-effort>
DESCRIPTION:<conf# + provider + parse_source if AI-suggested>
URL:<BASE_URL>/trips/<trip_id>#segment-<segment_id>
[BEGIN:VALARM ... END:VALARM if flight]
END:VEVENT
```

**`<host>`** comes from `urllib.parse.urlparse(settings.base_url).netloc`. Stable across redeploys as long as `BASE_URL` doesn't change.

### 6.3 Per-type SUMMARY/LOCATION dispatch

Small lookup table in `_render_segment_vevent`:

| `segment.type` | Icon | SUMMARY format | LOCATION |
|---|---|---|---|
| `flight`       | ✈ | `✈ {flight_number} {start_iata} → {end_iata}` | `end_location.city` |
| `lodging`      | 🏨 | `🏨 {provider}` | `end_location.address` or `.city` |
| `car`          | 🚗 | `🚗 {provider} {start_location.city}` | `start_location.city` |
| `train`        | 🚆 | `🚆 {train_number} {start_iata?} → {end_iata?}` | `end_location.city` |
| `transfer`     | 🚐 | `🚐 {provider}` | `end_location.city` |
| `activity`     | 🎟 | `🎟 {provider}` | `start_location.city` |

**Fallback** when per-type fields are missing: `📍 {type} · {provider or ""}`. The dispatcher gracefully degrades for partially-parsed segments.

### 6.4 VALARM on flights only

```ics
BEGIN:VALARM
ACTION:DISPLAY
DESCRIPTION:✈ Leave for the airport — {flight_number}
TRIGGER:-PT3H
END:VALARM
```

Three-hour ahead notification. Other segment types (hotels, cars, etc.) don't emit a VALARM — calendar client defaults handle "1 day before" reminders if the user wants them.

### 6.5 Times — always UTC basic format

DTSTART/DTEND/DTSTAMP all emit `YYYYMMDDTHHMMSSZ` (RFC 5545 §3.3.5 "form #2"). Calendar clients localize to the device timezone on display. **Don't emit `TZID=...` with embedded VTIMEZONE blocks** — that path is fraught with DST/zone-rename edge cases. UTC-only sidesteps the entire issue.

### 6.6 RFC 5545 line folding

Lines >75 octets wrap with `\r\n ` (CRLF + single leading space). The `_fold(line: str) -> str` helper applies this universally. **Octets, not characters** — UTF-8 emoji and accented chars consume 2-4 bytes each and must be counted by their encoded length, not `len()`.

### 6.7 Text escaping

RFC 5545 §3.3.11 requires escaping in TEXT-typed fields (SUMMARY, DESCRIPTION, LOCATION):

| Raw | Escaped |
|---|---|
| `,` | `\,` |
| `;` | `\;` |
| `\` | `\\` |
| `\n` (newline in body) | `\n` (literal backslash-n) |

Six-character helper `_escape(s: str) -> str`. URL field and DTSTART/DTEND are NOT TEXT — no escaping there.

### 6.8 Filter

```python
SELECT s.*
FROM segments s
JOIN trip_travelers tt ON tt.trip_id = s.trip_id
WHERE tt.user_id = :user_id
ORDER BY s.start_at
```

Mirrors search-proxy semantics: returns segments on any trip the user is a traveler on, including shared trips.

---

## 7. Routes

### 7.1 Public ICS feed — `GET /ics/<token>.ics`

New module `src/trip_tracker/routes/ics.py`:

```python
@router.get("/ics/{token}.ics", response_class=Response)
async def ics_feed(
    token: str,
    db: AsyncSession = Depends(get_session),  # noqa: B008
    settings: Settings = Depends(get_settings),  # noqa: B008
) -> Response:
    user = await resolve_token(token, db)
    if user is None:
        raise HTTPException(status_code=404, detail="Not found")

    segments = (await db.execute(
        select(Segment)
        .join(TripTraveler, TripTraveler.trip_id == Segment.trip_id)
        .where(TripTraveler.user_id == user.id)
        .order_by(Segment.start_at)
    )).scalars().all()

    body = render_calendar(user=user, segments=segments,
                           base_url=str(settings.base_url))
    return Response(
        content=body,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="trip-tracker.ics"',
            "Cache-Control": "private, max-age=300",
        },
    )
```

The router is included in `app.py` **without** the `require_user` dependency. `<token>` matches FastAPI's default path-converter (any non-`/` chars), and `secrets.token_urlsafe()` only produces `[A-Za-z0-9_-]` + `=` — no path traversal possible. The `.ics` suffix is just URL aesthetic; it doesn't affect routing.

**404 path** is the same shape (status, body, headers minus Content-Disposition) for invalid token AND `ics_token_hash IS NULL` user, so timing/response-shape doesn't reveal "this token *almost* matches a real user."

**Cache-Control: private, max-age=300:** lets a single client's HTTP cache buffer for 5 min. Calendar clients refetch on their own polling schedule (Apple Calendar ~15min, Google Calendar ~1hr) regardless of `Cache-Control`. The `private` keeps shared caches out.

**Logging:** every fetch logs at INFO level: `token_prefix=<first 6 chars of hash> user_id=<id> n_segments=<N>`. The token plaintext is never logged.

### 7.2 Settings page — generate + regenerate

Modify `src/trip_tracker/routes/admin.py` (or whichever module hosts settings; if Settings doesn't exist as a route yet, create `src/trip_tracker/routes/settings.py` as a sibling).

| Method + Path | Purpose |
|---|---|
| `GET /settings`               | Render Settings page including ICS section |
| `POST /settings/ics/regenerate` | Generate a new token; flash plaintext URL ONCE; show `<form>`-redirect-to-`/settings` |

**UX states:**

- **No token (`ics_token_hash IS NULL`):**
  - Section title: "Calendar feed (subscribable iCalendar)"
  - Body text: "Generate a calendar feed URL to subscribe your phone or computer's calendar app to your trips."
  - Button: `[ Generate calendar feed URL ]`

- **Token exists, just generated (one-time flash after POST redirect):**
  - Banner: "🔗 Your new calendar feed URL — save it now, it won't be shown again:"
  - URL text-area + copy-to-clipboard button: `https://trips.example.com/ics/<plaintext-token>.ics`
  - Note: "Subscribe in Apple Calendar: File → New Calendar Subscription → paste this URL."

- **Token exists, normal page state:**
  - "Calendar feed: enabled · `●●●●●●...a3f7c`" (last 5 chars of the hash; non-recoverable)
  - Button: `[ Regenerate ]` (red-tinted; warning text "Your old URL will stop working immediately.")

The plaintext-URL flash uses Phase 1's session flash mechanism (`request.session["flash"]` → consumed on next render). The plaintext URL is in the FLASH only (a session cookie), not persisted to the DB beyond the hash.

### 7.3 Authelia exemption

The `/ics/` prefix MUST be added to Traefik's exempt-path list. The existing exemption (Phase 1/2) covers `/api/ingest/email` and `/healthz`; the README and `docker-compose.yml` snippet add `/ics/`:

```yaml
- "traefik.http.routers.app-public.rule=Host(`${TRIP_HOST}`) && (PathPrefix(`/api/ingest/email`) || PathPrefix(`/healthz`) || PathPrefix(`/ics/`))"
```

Self-hosters who don't run Authelia (dev mode, alternative reverse proxy) get the public route working without any extra config — the route itself has no `require_user`.

---

## 8. Threat model

| Risk | Mitigation |
|---|---|
| Stolen URL exposes segment metadata to an attacker | Regenerate from Settings (one click, instant invalidation). The exposure is limited to: dates, cities, flight/train numbers, confirmation numbers in DESCRIPTION. No write surface; no documents; no payment data. |
| Timing attack to enumerate near-valid tokens | The 404 response shape is identical for invalid-token and valid-token-but-no-user. The hash lookup is constant-time enough on a short hash column to make practical timing oracles uneconomical. |
| Brute-force tokens via `/ics/<random>.ics` | 256-bit token space; even at 1M req/s a brute-force attacker needs ~10^65 years. Authelia exemption doesn't add risk because the auth is the token, not the session. |
| Backup leaks the user's URL | Only the SHA-256 hash is in the DB. The plaintext exists exactly once during the regenerate flash and on the user's calendar client. |
| Calendar client misbehavior (Outlook with `webcal://`) | README documents the workaround: use the `https://...` URL directly in Outlook. |
| Authelia bypass surface adds risk | The `/ics/` prefix is read-only and tightly scoped. Rate-limiting at Traefik (1000 req/h per IP — well above any legit client's polling rate) is the README-documented hardening. |

---

## 9. Done definition

- [ ] `users.ics_token_hash` column added; Alembic migration round-trips clean.
- [ ] `generate_token()` + `hash_token()` + `resolve_token()` helpers tested with the round-trip property: `resolve(plaintext) → user`.
- [ ] `render_calendar(user, segments, base_url)` produces RFC 5545 output: validates against `icalendar.Calendar.from_ical()` in tests; SUMMARY/LOCATION/DESCRIPTION escape-correctness covered with comma + semicolon + newline + emoji fixtures.
- [ ] Per-segment-type SUMMARY/LOCATION dispatch covered for all 6 types + fallback.
- [ ] Flight VEVENT carries `-PT3H` VALARM; non-flight types do not.
- [ ] Times emitted in UTC basic format; line folding at 75 octets respects multi-byte chars.
- [ ] `GET /ics/<token>.ics` returns `text/calendar; charset=utf-8` for valid token; 404 for invalid; 404 for valid-token-of-disabled-user.
- [ ] UID stability: re-fetch produces identical UIDs per segment.
- [ ] Settings page: "Generate" button when no token; one-time plaintext URL flash; "Regenerate" button when token exists.
- [ ] Old URL returns 404 immediately after regeneration.
- [ ] README "ICS feed (Phase 6)" section with subscription instructions for Apple Calendar / Google Calendar / Thunderbird + Traefik exempt-path snippet + Outlook `webcal://` warning.
- [ ] Playwright/curl smoke: subscribe a real `.ics` URL, parse via `icalendar` lib, assert ≥1 known segment's SUMMARY, assert UID is stable across two fetches.
- [ ] 85% coverage holds. ruff + mypy + bandit + djlint + pre-commit all clean.
- [ ] Signed `v0.6.0` tag pushed; release-verification scheduled agent confirms.

---

## 10. Risks & mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | RFC 5545 line folding mishandles multi-byte chars | `_fold()` counts UTF-8 octets, not codepoints. Test with emoji-laden segment titles. |
| 2 | Text escaping misses an edge case | Test with `,` `;` `\` `\n` in real-shape fixtures (e.g., hotel name "Hôtel Le Bristol, Paris"). |
| 3 | UID collision across re-fetches | UID = `<segment_id>@<host>`; segment_id is a UUID; collision probability is zero. Test by hashing two fetches' VEVENTs and asserting UIDs match. |
| 4 | Per-type dispatcher chokes on partially-parsed segment | Fallback `📍 {type} · {provider or ""}` always renders. Test against fixtures with empty `start_location` / NULL `end_at`. |
| 5 | Authelia bypass adds attack surface | `/ics/` is read-only, token-gated. Document `1000 req/h per IP` rate-limit in README. |
| 6 | Calendar client polls too aggressively | `Cache-Control: private, max-age=300` defends single-client; nothing we can do about per-client poll rate. Most clients are well-behaved. |
| 7 | Settings page doesn't yet exist as a separate route | Create `src/trip_tracker/routes/settings.py` if needed, or attach the ICS section to an existing admin/profile page. Phase 1's user model has the hooks; just need the route. |

---

## 11. Sequencing (rough — full plan from `writing-plans`)

| # | Task | Model | Notes |
|---|---|---|---|
| 1 | Schema + migration + token helpers (`generate_token`, `hash_token`, `resolve_token`) | haiku | Pure utility |
| 2 | ICS serializer (`render_calendar`, `_render_segment_vevent`, `_fold`, `_escape`, per-type dispatch) | sonnet | RFC 5545 correctness — many edge cases |
| 3 | Public `/ics/<token>.ics` route + Authelia-exempt path documented + integration tests | haiku | Mostly mechanical once Tasks 1+2 land |
| 4 | Settings page UX (generate / show-once / regenerate); flash + copy-to-clipboard | sonnet | May need to create a settings route module |
| 5 | docker-compose Traefik exempt-path doc + README ICS section | inline | No code, just config + docs |
| 6 | Verification gate + Playwright smoke (subscribe real `.ics`, parse via `icalendar`, verify segments + UID stability) + tag v0.6.0 | inline | Same shape as v0.5.0 ship |

~6 tasks total — smaller phase than 4/5 because the surface area is just one read-only route + a serializer + a settings UI affordance.

---

## 12. Future phases (Phase 6.x)

- **Phase 6.1 — Per-segment-type SUMMARY polish.** Iterate the dispatcher as real subscribed-feed usage reveals what the user actually wants in their calendar (e.g., hotel events showing nights instead of overnight ranges).
- **Phase 6.2 — Per-device tokens.** `ics_tokens` table allowing multiple tokens per user; revoke one device without breaking another's subscription. Useful only if multi-device key management becomes a real need.
- **Phase 6.3 — Trip-level feed variant.** Optional second feed `/ics/<token>/trips.ics` with one all-day VEVENT per trip; users who want week-view glanceability without per-segment noise.
- **Phase 6.4 — ETag / If-Modified-Since.** Return 304 when the user's last-segment-update timestamp is older than the client's `If-Modified-Since`. Bandwidth-saving optimization for high-poll clients.
- **Phase 6.5 — Disable-without-regenerate.** Settings "Disable feed" button that NULLs the hash without producing a new token (for users who want to stop sharing without immediately re-enabling).
