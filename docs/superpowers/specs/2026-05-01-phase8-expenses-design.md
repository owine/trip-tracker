# Phase 8 — Expenses + Award Metadata Design

**Status:** Approved (brainstorm 2026-05-01, owine + Claude).
**Target tag:** `v0.8.0`.
**Predecessors:** Phase 1–7 (auth, ingestion, parsers, search, documents, ICS, map+weather).
**Successor (sketched):** Phase 8.1 — auto-extract expenses from forwarded receipt emails.

---

## 1. Goal

Add per-trip expense tracking with frozen-at-entry FX (Frankfurter / ECB rates), payment-status awareness (paid vs pending), cancellation-deposit deadlines, and inline award-redemption metadata on flight + lodging segments. After this phase, trip-tracker covers the original v1 feature set: forwarded emails → segments + documents + map + ICS + expenses + points-redemption value.

**Out of scope for v0.8.0 (deferred):** auto-extract from receipt emails (Phase 8.1), CSV import from CC statements (Phase 8.2), expense splitting between travelers (master spec explicit non-goal), hotel-loyalty award nights, per-segment cost rollup, multi-currency receipts, retroactive FX updates.

---

## 2. Scope decisions (locked during brainstorm)

| # | Decision | Choice |
|---|---|---|
| 1 | Phase 8 scope | Expenses + award/points support (vs OCR / S3 / Pacific-arc fix) |
| 2 | Award redemption tracking | Inline on `Segment.details["award"]` (no new table) |
| 3 | Base currency | Per-user `home_currency` setting, default `USD`, FX frozen at entry |
| 4 | Award redemption applies to | Flights + lodging segments |
| 5 | Categories | Small enum (8 values) + free-text `notes` |
| 6 | Entry surface | Manual only in v0.8.0; auto-extract deferred to v0.8.1 |
| 7 | Payment status | `paid` / `pending` enum, default `paid` |
| 8 | Cancellation/deposit | 3 nullable columns, hidden behind a UI checkbox |
| 9 | Phase 8 split | v0.8.0 = core expenses + award; v0.8.1 = auto-extract |

---

## 3. Architecture overview

```
                            ┌────────────────────────────────────┐
                            │  Trip detail page                  │
                            │  (templates/trips/detail.html)     │
                            └────────────┬───────────────────────┘
                                         │ loads via trip_detail handler
                                         ▼
                  ┌──────────────────────────────────────────────┐
                  │  Expenses section (inline)                   │
                  │  - paid total · expected total               │
                  │  - by-category breakdown                     │
                  │  - per-row: native + home equiv + status     │
                  │  - cancellation deadline warnings            │
                  └──────────────────┬───────────────────────────┘
                                     │
        manual entry: POST           │
        /trips/<id>/expenses         │
                                     ▼
                ┌─────────────────────────────────────────┐
                │  routes/expenses.py                     │
                │  - require_user + traveler scope        │
                │  - call freeze_fx() before INSERT       │
                └────────┬────────────────────────────────┘
                         ▼
            ┌─────────────────────────────────────────┐
            │  expenses/fx.py                         │
            │  get_rate(base, target, redis)          │
            │  └─► Redis cache (24h TTL)              │
            │  └─► Frankfurter on cache miss          │
            └─────────────────────────────────────────┘
                         │
                         ▼
                  expenses table
                  ────────────
                  trip_id, segment_id, document_id, owner_user_id
                  amount_minor, currency, fx_rate, amount_home_minor, home_currency
                  category, notes, incurred_on, status
                  deposit_minor, cancellation_deadline, cancellation_fee_minor

  ╔═════════════════════════════════════════════════════════════════════╗
  ║  AWARD METADATA — separate from expenses; lives on Segment.details ║
  ║                                                                     ║
  ║  flight + lodging form: optional "Booked with miles or points"     ║
  ║  collapsed section. POST writes Segment.details["award"] = {...}.  ║
  ║                                                                     ║
  ║  segment row: ✈ AF007 JFK → CDG · 75k Chase UR + $5.60 — saved ~$1120│
  ╚═════════════════════════════════════════════════════════════════════╝
```

**Public surface:** 3 expense routes (create/edit/delete) under existing trip auth. **Storage:** new `expenses` table; one new column on `users`. **External APIs:** Frankfurter (free, keyless, ECB-backed daily rates) — synchronously called on cache miss; 24h Redis cache. **No new saq tasks** — FX must be available inline before expense save.

---

## 4. Data model

### 4.1 New `expenses` table

```sql
CREATE TABLE expenses (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  trip_id         uuid NOT NULL REFERENCES trips(id)     ON DELETE CASCADE,
  segment_id      uuid     REFERENCES segments(id)       ON DELETE SET NULL,
  document_id     uuid     REFERENCES documents(id)      ON DELETE SET NULL,
  owner_user_id   uuid NOT NULL REFERENCES users(id)     ON DELETE CASCADE,

  -- Money (minor units; integer arithmetic, no float drift)
  amount_minor          bigint NOT NULL,
  currency              text NOT NULL,           -- ISO 4217: "EUR", "JPY", "USD"
  fx_rate               numeric(20, 10) NOT NULL, -- frozen: 1 currency = X home_currency
  amount_home_minor     bigint NOT NULL,         -- precomputed home-currency equivalent
  home_currency         text NOT NULL,           -- snapshot of user.home_currency at entry

  -- Classification
  category              text NOT NULL,           -- enum value (lower_snake)
  notes                 text,

  -- Dates + status
  incurred_on           date NOT NULL,
  status                text NOT NULL DEFAULT 'paid', -- 'paid' | 'pending'

  -- Cancellation / deposit (collapsed in UI; nullable; "hybrid" structure per Q8)
  deposit_minor          bigint,
  cancellation_deadline  date,
  cancellation_fee_minor bigint,

  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_expenses_trip       ON expenses(trip_id);
CREATE INDEX ix_expenses_segment    ON expenses(segment_id);
CREATE INDEX ix_expenses_owner      ON expenses(owner_user_id);
CREATE INDEX ix_expenses_incurred   ON expenses(incurred_on);
```

**Cascade semantics** (Phase 5 documents pattern):
- **Trip delete → CASCADE.** Trip gone, expenses gone.
- **Segment delete → SET NULL.** Expense survives ("the cancellation fee" is independent of the deleted hotel segment).
- **Document delete → SET NULL.** Receipt gone, expense row stays.
- **User delete → CASCADE.** Same as Phase 5.

**Convention:** `created_at` / `updated_at` use `server_default=func.now()` + `onupdate=func.now()`, matching Phase 1 User. The migration uses Postgres `now()` for both; ORM uses `onupdate=func.now()`.

**Status validation:** No DB-level CHECK constraint or Postgres enum — app-side Pydantic validation only (parity with Phase 5 Document `extract_status`).

### 4.2 `users.home_currency` column

```sql
ALTER TABLE users ADD COLUMN home_currency text NOT NULL DEFAULT 'USD';
```

Default `USD`. Settable in the existing Settings page (Phase 6). **Changing it does NOT retroactively re-FX existing expenses** — `amount_home_minor` was frozen at entry. Future entries use the new home currency. Settings page warns: *"Changing your home currency only affects new expenses; existing trip totals stay in the currency they were entered in."*

### 4.3 Money precision: minor units, not floats

`amount_minor` is `bigint` storing the smallest unit (cents for USD/EUR, sen for JPY, fils for BHD/JOD). Every currency has a `minor_digits` value:

```python
# src/trip_tracker/expenses/currencies.py
CURRENCY_MINOR: dict[str, int] = {
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0,   # zero-decimal
    "BHD": 3, "JOD": 3, "KWD": 3, "OMR": 3, "TND": 3,   # three-decimal
    # everything else defaults to 2
}

def minor_digits(code: str) -> int:
    return CURRENCY_MINOR.get(code, 2)
```

Display divides by `10 ** minor_digits(code)`. Math always on integers; no float drift.

For `fx_rate`: `numeric(20, 10)` gives 10 decimal places — exact across reads, enough precision for any cross-rate. Not Python `float`.

### 4.4 The "freeze FX" formula

At entry time:

```python
fx_rate: Decimal = await get_rate(
    base=form.currency,
    target=user.home_currency,
    redis=redis,
)
home_digits = minor_digits(user.home_currency)
native_digits = minor_digits(form.currency)
factor = Decimal(10) ** (home_digits - native_digits)
amount_home_minor = int(
    (Decimal(form.amount_minor) * fx_rate * factor).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
)
```

`Decimal` (not float) for the multiplication; `int()` only at the end. Stored, never recomputed — that's "frozen FX."

**Edit-path recompute rule.** When the user edits an existing expense, the route MUST:
- If `currency` changed: re-fetch `fx_rate` AND recompute `amount_home_minor`.
- If `currency` unchanged BUT `amount_minor` changed: keep the original `fx_rate`; recompute `amount_home_minor` using the formula above.
- If neither changed: no recompute.

Skipping the second case is the silent-corruption trap — a row whose native amount no longer matches its home equivalent.

### 4.5 Categories — closed enum

`Category(str, Enum)` in `expenses/categories.py`:

```python
class Category(str, Enum):
    FOOD = "food"
    TRANSIT = "transit"
    LODGING = "lodging"
    ACTIVITIES = "activities"
    SHOPPING = "shopping"
    GRATUITIES = "gratuities"
    CONNECTIVITY = "connectivity"
    OTHER = "other"
```

DB stores the lowercase `str` value. Form dropdown shows readable labels.

### 4.6 Award metadata on `Segment.details`

No schema change — `Segment.details` is `Mapped[dict[str, Any] | None]` (JSONB). Award metadata occupies a known sub-key:

```python
segment.details["award"] = {
    "program": "Chase Ultimate Rewards",   # free-text; autocomplete from prior entries
    "points_spent": 75000,
    "cash_copay_minor": 560,               # 5.60 in cash_copay_currency
    "cash_copay_currency": "USD",
    "cash_equivalent_minor": 112500,       # optional self-reported "would have cost cash"
    "cash_equivalent_currency": "USD",
}
```

`program` is free-text covering airline programs (United MileagePlus, Delta SkyMiles, AS Mileage Plan) AND CC-transferable-points programs (Chase Ultimate Rewards, Amex Membership Rewards, Capital One Venture, Citi ThankYou, Bilt). Autocomplete via a JS-side `<datalist>` populated from prior entries on this user's segments.

`cash_copay_minor` is required when `points_spent > 0` (taxes/fees on a Chase Travel booking, or partner-airline tax on a transfer redemption). Can be 0 (some Hyatt awards have no co-pay).

`cash_equivalent_minor` is optional self-reported "what this would have cost in cash." Used purely for the per-trip "saved by points" rollup. We don't compute it; the user types it.

Pydantic v2 model `AwardDetails(BaseModel)` validates the shape on form POST:

```python
class AwardDetails(BaseModel):
    program: str = Field(min_length=1, max_length=100)
    points_spent: int = Field(ge=1)
    cash_copay_minor: int = Field(ge=0)
    cash_copay_currency: str = Field(min_length=3, max_length=3)
    cash_equivalent_minor: int | None = Field(default=None, ge=0)
    cash_equivalent_currency: str | None = Field(default=None, min_length=3, max_length=3)
```

Empty-form behavior: if `points_spent` is blank, no `details["award"]` key is written. Partial submissions (program but no points_spent) → 400 with field-level errors via FastAPI's existing form-error display pattern.

**Clearing an existing award.** The form gains a "Clear award metadata" checkbox visible only when `details.award` is already set. On POST, the route checks the `clear_award` flag **before** attempting `AwardDetails` parse: if set, skip validation entirely, delete the `"award"` key from `details` (preserving any other JSONB siblings), and write the segment back. Without this short-circuit, blank fields + clear-checked would 400 on the `points_spent` validator. Without an explicit "clear" action, blanking fields and re-saving has no effect — Pydantic rejects the empty submission with field errors. Avoids accidental clears.

---

## 5. FX subsystem (`src/trip_tracker/expenses/fx.py`)

### 5.1 Frankfurter HTTP client

```
GET https://api.frankfurter.dev/v1/latest?base=USD
```

We always call **without** a `symbols` parameter so Frankfurter returns ALL ~30 supported currencies under that base in one shot. This makes the per-base cache entry maximally useful: a cache hit covers any target currency.

Response (~150ms typical):

```json
{
  "amount": 1.0,
  "base": "USD",
  "date": "2026-05-01",
  "rates": {"EUR": 0.93, "JPY": 156.4, "GBP": 0.79, ...}
}
```

ECB publishes daily ~16:00 CET; weekends carry forward. Frankfurter handles that.

```python
async def fetch_rates(base: str) -> dict[str, Decimal]:
    """Single Frankfurter HTTP call. Returns ALL rates under `base` as Decimal.
    Raises FxError on 5xx/network failure. 10s timeout via httpx.
    """
```

Rates are parsed from the JSON response by reading them as strings first (`json.loads(..., parse_float=Decimal)` or equivalent), then converting — this avoids a float→Decimal round-trip that would import precision loss.

### 5.2 Redis cache

Key: `fx:<base>:<YYYY-MM-DD>` (e.g., `fx:USD:2026-05-01`). TTL: 24h. Value: JSON map of `{currency: rate_str}`.

```python
async def get_cached_rates(base: str, redis: Redis) -> dict[str, Decimal] | None:
    """Returns parsed rates if cached, else None. Today's date is the cache key
    component — auto-rolls when the calendar day changes."""

async def set_cached_rates(base: str, rates: dict[str, Decimal], redis: Redis) -> None:
    """Caches under fx:<base>:<today_iso> with 24h TTL. Stores rate values as
    strings to preserve Decimal precision through Redis."""

async def get_rate(base: str, target: str, redis: Redis) -> Decimal:
    """Convert between any two currencies. Returns the rate `1 base = X target`.

    Implementation: ALWAYS look up under cache key `fx:<base>:<today>`. On cache
    miss, call fetch_rates(base) and store the full table. Then return rates[target].

    Special cases:
    - base == target → return Decimal(1) with no I/O.
    - target not in rates → FxError (Frankfurter doesn't support that currency).

    We do NOT invert (i.e., never call fetch_rates(target) and divide). Each
    distinct `base` gets its own cache entry; for a 2-currency user (home + one
    foreign), expect 2 cache entries.
    """
```

The cache stores ALL ~30 symbols Frankfurter returns under one base, not per (base, target) pair — one HTTP call yields the full table, all cacheable together.

### 5.3 Failure mode

| Scenario | Behavior |
|---|---|
| Cache hit | Return immediately, ~0ms |
| Cache miss + Frankfurter 200 | Cache + return, ~150ms |
| Cache miss + Frankfurter 5xx/timeout | `raise FxError`; route returns 503 with retry message; expense NOT saved |

We never store an FX rate the user didn't see. Better to fail loudly than to silently corrupt a trip total with a wrong rate.

### 5.4 Why no saq task

FX must be available **synchronously** before saving an expense (we need `fx_rate` and `amount_home_minor` in the same INSERT). Background-fetching would mean the expense lands with a placeholder — both worse than blocking the request for ~150ms (cache miss) or 0ms (cache hit). Cache TTL is 24h, so at most one Frankfurter call per (base, day).

---

## 6. Routes + UI

### 6.1 Expense routes (`src/trip_tracker/routes/expenses.py`)

| Method + Path | Purpose |
|---|---|
| `POST /trips/{trip_id}/expenses`   | Create. Calls `freeze_fx()` before INSERT. Redirect 303 to trip detail. |
| `GET /expenses/{id}/edit`          | Render the edit form pre-populated (auth: owner OR trip-traveler). |
| `POST /expenses/{id}`              | Update. Recompute per §4.4 edit-path rule (re-fetch `fx_rate` only if `currency` changed; recompute `amount_home_minor` if `amount_minor` OR `currency` changed). |
| `GET /segments/award-programs.json`| Autocomplete endpoint. Returns recent distinct `program` strings from this user's `details.award` entries. `require_user`-gated. |
| `POST /expenses/{id}/delete`       | Delete. HTMX `hx-delete` form-method-override. |

All routes `require_user`-gated. Ownership: owner OR trip-traveler can read/edit/delete. Mirrors Phase 5 documents auth.

The list view lives **inline on trip detail** (no separate `/trips/<id>/expenses` page). Phase 5 documents went the separate-page route; expenses are denser per trip and the detail page is the natural surface, so embedding the table inline keeps the user in one place.

### 6.2 Trip detail page extensions

`src/trip_tracker/routes/trips.py::trip_detail` gains:

```python
expenses = (await db.execute(
    select(Expense)
    .where(Expense.trip_id == trip.id)
    .order_by(Expense.incurred_on.desc(), Expense.created_at.desc())
)).scalars().all()

home_currency = user.home_currency

# Two rollups
total_paid_home = sum(e.amount_home_minor for e in expenses if e.status == "paid")
total_expected_home = sum(e.amount_home_minor for e in expenses)  # paid + pending

# Categories (paid only — pending entries don't yet count)
by_category: dict[str, int] = defaultdict(int)
for e in expenses:
    if e.status == "paid":
        by_category[e.category] += e.amount_home_minor

# Award "saved by points" rollup across flight + lodging segments.
# Live FX is OK here (soft estimate, not stored). FxError is SWALLOWED so a
# Frankfurter outage doesn't 500 the whole trip detail page.
total_saved_home: int | None = 0
try:
    for s in segments:
        award = (s.details or {}).get("award")
        if not award or award.get("cash_equivalent_minor") is None:
            continue
        eq_home_rate = await get_rate(award["cash_equivalent_currency"], home_currency, redis)
        cp_home_rate = await get_rate(award["cash_copay_currency"], home_currency, redis)
        # convert minor-units across digit differences then sum (eq - copay)
        total_saved_home += _convert_to_home(award["cash_equivalent_minor"],
                                             award["cash_equivalent_currency"],
                                             eq_home_rate, home_currency)
        total_saved_home -= _convert_to_home(award["cash_copay_minor"],
                                             award["cash_copay_currency"],
                                             cp_home_rate, home_currency)
except FxError:
    # Frankfurter unreachable AND no cache. Hide the line entirely.
    total_saved_home = None
    logger.info("FX unavailable; saved-by-points rollup hidden for trip %s", trip.id)
```

Template variables added: `expenses`, `total_paid_home`, `total_expected_home`, `by_category`, `total_saved_home` (None when FX failed), `home_currency`. Template renders the "Saved by points" line only when `total_saved_home is not None and total_saved_home > 0`.

**Per-segment N+1 note.** With N award segments and a cold cache, the loop triggers up to 2 distinct `get_rate` calls (one per distinct currency in the award), but each cache miss costs at most one Frankfurter HTTP per `base`. The cache de-dupes within a render. Worst case for a typical trip: 1 Frankfurter call (home_currency base) covers all conversions. We do NOT batch across segments because `get_rate` already serves from cache after the first miss.

### 6.3 Trip-detail expense section template

A new section between the Documents tab link and the segments list, in `templates/trips/detail.html`:

```
┌─────────────────────────────────────────────────────────────┐
│ Expenses                                                    │
│ Spent so far: $1,247  ·  Expected: $2,890                   │
│ ✈ Saved by points: ~$1,120                                  │
│                                                              │
│ [+ Add expense]   [Categories: Food $312, Transit $98, ...] │
│                                                              │
│ ─────────────────────────────────────────────────────────── │
│ 2026-06-04  Food     €38.00 ($40.65) ✅   Le Petit Bistro · Paris │
│ 2026-06-03  Transit  €12.50 ($13.38) ✅                     │
│ 2026-06-01  Lodging  €820 ($877.40) ⏳                      │
│   ⚠ Deposit $200 forfeit after 2026-05-25 (3 days)          │
│ ...                                                          │
└─────────────────────────────────────────────────────────────┘
```

Each row: date · category badge · native amount (`€38.00`) · home equivalent (`$40.65`) · status icon · provider/notes preview. Pending rows with `cancellation_deadline` within the next 30 days render the `⚠` warning. The `📎` icon next to the amount opens the linked document if any.

### 6.4 Expense add/edit form template

`src/trip_tracker/templates/expenses/form.html`:

```
Amount *  [_____.__]  Currency * [EUR ▼]
Date *    [2026-06-04]
Category *[Food ▼]              Status [Paid ▼]
Notes     [_______________________________]
Receipt   [  Choose existing document ▼  ] (optional)
Linked segment [ — none — ▼ ] (optional, restricts to this trip's segments)

⏵ Has cancellation policy / deposit       <-- collapsed by default (Alpine.js x-show)
   Deposit amount    [_____.__] (in same currency)
   Cancellation deadline [____-__-__]
   Cancellation fee  [_____.__]

[Save expense]   [Cancel]
```

Cancellation/deposit triple is hidden behind a `<details>` element (or Alpine `x-collapse`). After save, the form server-renders a flash with the saved native + home equiv: *"Saved €38.00 ($40.65 USD)."*

### 6.5 Award fields on flight + lodging forms

`templates/segments/flight_form.html` and `lodging_form.html` gain a new collapsed section "Booked with miles or points":

```
⏵ Booked with miles or points              <-- collapsed by default
   Program       [_____________________] (autocomplete via <datalist>)
   Points spent  [_______]
   Cash co-pay   [_____.__] [USD ▼]
   Cash equivalent (optional)  [_____.__] [USD ▼]
                 ↳ helper text: "What this would have cost in cash. Used for 'saved by points' totals."
```

Datalist suggestions: `Chase Ultimate Rewards`, `Amex Membership Rewards`, `Capital One Venture`, `Citi ThankYou`, `Bilt Rewards`, `United MileagePlus`, `Delta SkyMiles`, `American AAdvantage`, `Alaska Mileage Plan`, `Marriott Bonvoy`, `Hyatt World of Hyatt`, `Hilton Honors`, `IHG One Rewards`. Plus autocomplete from prior `program` strings on this user's segments via a small endpoint that returns recent distinct values.

Form-validator parses these into `AwardDetails`; if validation passes, write to `segment.details["award"]`. If `points_spent` is blank, no `details["award"]` key is created.

### 6.6 Award badge on segment row (`templates/segments/_row.html`)

When `segment.details.get("award")` is truthy:

```
✈ AF007 JFK → CDG · 75k Chase UR + $5.60 — saved ~$1,120
```

Badge format: `{points_spent | k_format} {program_short} {+ $cash_copay if > 0} {— saved ~${equiv - copay} if eq present}`.

`k_format` renders 75000 as `75k`, 1500 as `1.5k`, etc. `program_short` strips common prefixes ("Chase Ultimate Rewards" → "Chase UR", "Amex Membership Rewards" → "Amex MR", "United MileagePlus" → "United"). When `cash_equivalent_minor` is missing, the badge omits the "saved" suffix.

### 6.7 Settings page (`/settings`) gains `home_currency`

A new section:

```
Home currency: [USD ▼]   [Save]
   ↳ Used for trip-total rollups. Changing this only affects new
     expenses; existing rows keep their original frozen FX.
```

Common currencies (USD, EUR, GBP, CAD, AUD, JPY, CHF, CNY) at the top; full list below. Saving is a single `POST /settings/home_currency` route handler that updates `user.home_currency` and redirects with a flash. Mirrors Phase 6's ICS regenerate POST shape.

---

## 7. Threat model

| Risk | Mitigation |
|---|---|
| FX rate manipulated by a malicious request | The route handler always calls `get_rate` server-side; the form does NOT accept a user-provided rate. The user sees the computed rate AFTER save. |
| Expense rows leaked across users | All routes `require_user`-gated; ownership = owner OR trip-traveler. Expenses for trips the user isn't on never appear. |
| Cancellation-deadline timing leak | `cancellation_deadline` is a `date` (not `datetime`). The "deposits at risk" warning fires at the user's local-clock midnight on the deadline date — close enough for a personal log. |
| `cash_equivalent_minor` self-reported | UI badge says "saved ~$1,120" with a tilde to signal estimation. We don't validate. |
| Frankfurter availability outage | 24h Redis cache covers most failures; cold-cache + 5xx → explicit 503 with retry message. No silent wrong rates. |
| Float drift in FX math | `Decimal` arithmetic throughout; `int(round(...))` only at the final amount_home_minor. `numeric(20, 10)` storage. |
| Currency-minor edge cases (JPY/KRW/BHD) | `CURRENCY_MINOR` lookup with sensible default (2); test fixtures cover all three classes (0, 2, 3 decimals). |
| Home-currency change confusion | Settings page warning explicitly says "only affects new expenses." Future v0.8.x may add a "re-FX historical" admin button. |
| Concurrent home-currency change mid-entry | Form GET captures current `user.home_currency` and writes it as a hidden field; POST verifies it still matches the user's current setting. On mismatch, render the form with a flash: "Your home currency changed in another tab — review and resubmit." Re-render preserves the user's `currency` selection as-is; we don't auto-rewrite. Lost-update window collapses to a single round trip. |
| Trip-traveler removed but still owns expense rows | Owner permission is independent of trip-traveler membership: a removed traveler still owns their rows and can edit/delete them via owner-only auth (the rows show in their personal exports). The trip view, however, only renders rows for users currently in the traveler list — orphan rows are hidden from non-owners. |
| Settings home-currency POST CSRF | Existing FastAPI session middleware + same-origin POST + `require_user`. No new attack surface. |

---

## 8. Done definition

- [ ] `expenses` table + migration round-trips clean.
- [ ] `users.home_currency` column added; default `USD`.
- [ ] `Expense` ORM with cascade-on-trip-delete, SET-NULL on segment + document delete; `after_delete` listener cleanup if needed.
- [ ] `expenses/categories.py` exposes the 8-value `Category` enum.
- [ ] `expenses/currencies.py` exposes `CURRENCY_MINOR` lookup with sensible defaults.
- [ ] `expenses/fx.py`: `fetch_rates`, `get_cached_rates`, `set_cached_rates`, `get_rate(base, target, redis)`. Cold-cache `get_rate` performs a Frankfurter HTTP call within 1s.
- [ ] `freeze_fx(amount_minor, currency, home_currency, redis)` helper returns `(fx_rate, amount_home_minor)` using `Decimal` math. Tests cover JPY (0 decimals), USD (2), BHD (3), and same-currency (returns rate=1).
- [ ] Manual expense routes: create, edit, delete; auth + traveler scope. Edit-path correctly recomputes `amount_home_minor` per §4.4 rule (incl. amount-only-changed case).
- [ ] Award-program autocomplete endpoint `GET /segments/award-programs.json` returning recent distinct values from this user's `details.award.program`.
- [ ] Award badge formatter helpers (`k_format`, `program_short`) with unit tests covering 75000→"75k", 1500→"1.5k", "Chase Ultimate Rewards"→"Chase UR", "Amex Membership Rewards"→"Amex MR", etc.
- [ ] "Clear award metadata" checkbox on flight + lodging edit forms + handler that deletes the `"award"` JSONB sub-key while preserving siblings.
- [ ] Concurrent home-currency-change guard: hidden `home_currency_at_load` field on expense forms; POST mismatch re-renders with flash.
- [ ] Trip-detail render swallows `FxError` from the saved-by-points rollup; the rest of the page still renders.
- [ ] Per-row `📎` icon links to attached document when `expense.document_id` is set.
- [ ] Trip detail page expense section: paid total, expected total, by-category breakdown, saved-by-points rollup (await live FX in render — not stored), per-row display with status icon, cancellation deadline warning when ≤30 days away.
- [ ] Award fields (`AwardDetails` Pydantic model) on flight + lodging forms; on POST, `details.award` populated; on render, segment row shows the badge.
- [ ] Settings page exposes `home_currency` dropdown with save flow + warning copy.
- [ ] Frankfurter cache miss + 5xx → 503 + "Try again in a few minutes" message; expense NOT saved.
- [ ] README "Expenses (Phase 8)" section: quick-start, FX-frozen-at-entry rationale, deferred items list (auto-extract → v0.8.1, splits → never, hotel-loyalty award nights → v0.8.x).
- [ ] Smoke test: enter EUR expense; verify USD home equivalent matches frankfurter.dev; change home currency to JPY; enter new expense; verify rollup mixed correctly (old EUR row keeps USD frozen; new JPY row shown in JPY).
- [ ] 85% project-wide coverage holds. ruff + mypy + bandit + djlint + pre-commit all clean.
- [ ] Signed `v0.8.0` tag pushed; release-verification scheduled agent confirms.

---

## 9. Risks & mitigations

| # | Risk | Mitigation |
|---|---|---|
| 1 | Frankfurter availability | 24h Redis cache; explicit 503 on cold-cache + 5xx; documented in README. |
| 2 | Currency-minor edge cases | `CURRENCY_MINOR` table with sensible defaults; test fixtures cover 0/2/3 decimals + same-currency. |
| 3 | Float drift on FX math | `Decimal` arithmetic throughout; `int(round(..., ROUND_HALF_UP))` only at final integer cast. |
| 4 | Home currency change UX trap | Settings page warning + frozen-at-entry contract. |
| 5 | Cancellation-deadline timezone | `date` (not `datetime`) — local-clock midnight is close enough for a personal log. |
| 6 | `cash_equivalent` user-reported | UI badge marks with `~`; we don't validate cents-per-point math. |
| 7 | Auto-extract scope creep into v0.8.0 | v0.8.0 strictly manual; no parser-pack changes; no `/inbox` bucket changes. |
| 8 | Award form doesn't fire on lodging when user only ticks flight | Both forms (`flight_form.html` + `lodging_form.html`) get the section independently; no shared-template ceremony. |
| 9 | Trip-detail render doing live FX for "saved by points" | Live FX only for the cosmetic rollup; not stored. Cache hits cover the common case. If FX is unavailable, the rollup omits the saved-by-points line; trip detail still renders. |

---

## 10. Sequencing — 16 tasks

| # | Task | Model | Notes |
|---|---|---|---|
| 1 | Schema + Alembic migration + ORM + cascade listener | sonnet | Multi-FK cascade; mirror Phase 5 documents pattern |
| 2 | `users.home_currency` column + migration + ORM update | haiku | One-column add |
| 3 | `expenses/categories.py` (Category enum) + `currencies.py` (CURRENCY_MINOR + minor_digits) | haiku | Pure data |
| 4 | Frankfurter HTTP client + Redis cache + `get_rate` (no-symbols, never-invert direction) | sonnet | Direction is subtle; cache shape and Decimal-from-string parsing matter |
| 5 | `freeze_fx` helper + edit-path recompute helper + Decimal math tests (JPY/USD/BHD/same-currency) | haiku | Pure function, table-driven tests |
| 6 | Expense CRUD routes (create/edit/delete; auth; FX freeze on create; recompute on edit per §4.4) | sonnet | Edit recompute rule is the gotcha |
| 7 | Expense form template + cancellation/deposit collapsible + home-currency-at-load hidden field | sonnet | djlint-heavy |
| 8 | Trip detail page expense section (rollups + FxError swallow + template) | sonnet | Touches existing route + template |
| 9 | Pydantic v2 `AwardDetails` model + form-validator integration into segment routes | haiku | Schema-only |
| 10 | Award fields on flight + lodging forms (template + POST writes `details.award` + clear-award checkbox handler) | sonnet | Two forms; clear-award path |
| 11 | Award badge on segment row + `k_format` / `program_short` helpers (with unit tests) + "saved by points" inline rollup | haiku | Display-only |
| 12 | Award-program autocomplete endpoint (`/segments/award-programs.json`) + datalist wiring | haiku | One route + JS hook |
| 13 | Settings page `home_currency` dropdown + POST handler | haiku | Mirror Phase 6 ICS regenerate flow |
| 14 | README "Expenses (Phase 8)" section + deferred-items list | inline | Docs only |
| 15 | Verification gate + Playwright smoke (EUR + JPY entry, home-currency change, FxError fallback rendering) | inline | Same as Phase 6/7 ship |
| 16 | Tag v0.8.0 + push + schedule release-verification agent | inline | Standard ship |

---

## 11. Future phases (Phase 8.x)

- **Phase 8.1 — Auto-extract from receipt emails.** New `expense` output type from `parse_raw_email`, vendor packs (Lyft, Uber, Stripe), Haiku LLM fallback for unmatched receipts, third bucket in `/inbox` for expense-candidates review.
- **Phase 8.2 — CSV import from credit-card statements.** Upload Chase/Amex CSV; review queue assigns each row to a trip + category. Captures the long tail.
- **Phase 8.3 — Hotel-loyalty award nights on lodging segments + nightly breakdown.** "5 nights at Hyatt Andaz, 4 award + 1 cash."
- **Phase 8.4 — Per-segment cost rollup.** "How much did this trip cost me" per segment (flight cash + flight points-equivalent + hotel + ground transport).
- **Phase 8.5 — Expense splitting between travelers.** Master spec explicit non-goal but worth revisiting if the project ever supports household travel.
- **Phase 8.6 — Multi-currency receipts.** Hotel folio in EUR with USD card surcharge (single expense, two amounts).
- **Phase 8.7 — Re-FX historical expenses admin tool.** For users who change home currency and want all rows recomputed once.
