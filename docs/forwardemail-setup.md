# ForwardEmail.net ingest setup

trip-tracker accepts inbound email two ways:

| Endpoint | Body | Auth | Use case |
|---|---|---|---|
| `POST /api/ingest/email` | raw RFC-822 MIME | HMAC-SHA256 + timestamp + nonce | Programmatic / direct integrations |
| `POST /api/ingest/forwardemail` | ForwardEmail JSON envelope | `?token=<shared-secret>` | ForwardEmail.net webhook deliveries |

This doc covers the second path. The adapter unwraps FE's JSON, extracts the
embedded raw MIME from `payload.raw`, and feeds it through the same
persistence + worker pipeline as the direct endpoint. No HMAC because FE only
signs payloads on paid plans, and the inner MIME is unsigned regardless;
the `?token=` is the trust boundary.

## Prerequisites

- A domain pointed at ForwardEmail's MX records (`mx1.forwardemail.net`,
  `mx2.forwardemail.net`).
- An alias configured in the FE dashboard (paid plan) or via DNS TXT record
  (free plan) — see ForwardEmail's docs.
- trip-tracker deployed at a public HTTPS URL.

## Setup

### 1. Generate the relay token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Set the result as `FORWARDEMAIL_RELAY_TOKEN` in your deployment environment
(matching the `Settings` field of the same name). Restart the app.

### 2. Create the matching forwarding alias

In trip-tracker, visit `/admin/aliases` and create a `forwarding_aliases` row
whose `local_part` matches the local-part of **whatever address you'll be
forwarding emails to**. That doesn't have to be on a `trips.*` subdomain —
it can be your existing personal-domain alias, your work alias, anything
ForwardEmail is configured to forward.

**The domain is irrelevant — trip-tracker only matches on the local part.**
If your FE alias is `oliver@yourpersonaldomain.com`, create a row with
`local_part = "oliver"` and you're set. Forward emails to that address from
anywhere; trip-tracker reads the MIME's `To:` header, splits on `@`, lowercases
the local part, and looks up `forwarding_aliases WHERE local_part = 'oliver'`.

(The Phase 2 README's `oliver@trips.<your-domain>` example is one valid
setup but not the only one. The `trips.` subdomain is convention, not a
requirement.)

### 3. Point FE at the adapter URL

Configure the FE webhook URL to:

```
https://trips.example.com/api/ingest/forwardemail?token=<TOKEN>&attachments=false
```

- `?token=<TOKEN>` — the value from step 1. FE preserves the URL exactly
  from the DNS TXT record / dashboard alias config, so the secret rides
  through every delivery.
- `?attachments=false` — bandwidth optimization. FE's webhook JSON includes
  attachments **twice** by default: once as base64-encoded multipart sections
  inside the `raw` MIME blob, and again as pre-decoded buffers in a separate
  `attachments[]` array. trip-tracker's worker extracts attachments from the
  `raw` blob (Phase 5 path), so the `attachments[]` array is redundant —
  setting `attachments=false` removes the duplicate copy and roughly halves
  the webhook payload size on emails with large PDFs (boarding passes, hotel
  folios). **No data loss** — attachments still flow through end-to-end via
  the raw MIME. Recommended to keep this on; helps stay inside FE's 5-second
  endpoint timeout when the worker is cold.

### 4. Smoke test

1. From your phone or desktop mail client, forward an existing flight or
   hotel confirmation to the FE alias.
2. Watch logs:
   ```
   docker compose logs -f trip-tracker-app trip-tracker-worker
   ```
3. Expect this sequence:
   - `ingest_forwardemail status=202` line within seconds (adapter received
     and persisted the row).
   - `parse_raw_email` worker log a few seconds later (worker picked up the
     row and ran the per-vendor parser + Haiku fallback).
   - A new row in `/inbox` for review/confirmation.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| **401 from `/api/ingest/forwardemail`** | Token mismatch | Confirm the URL in FE's dashboard exactly matches `FORWARDEMAIL_RELAY_TOKEN` (no trailing whitespace, no URL-encoding artifacts). |
| **400 `missing or empty 'raw' field`** | FE's `?raw=false` querystring filter is on | Remove `?raw=false`. The adapter requires the raw MIME. |
| **202 but nothing appears in `/inbox`** | Alias mismatch | The forwarded MIME's `To:` local-part doesn't match any `forwarding_aliases.local_part` row. The worker marks the email `no_segments`. Check `/admin/raw-emails`. |
| **Same email forwarded twice — second is silently ignored** | Working as designed | Dedup is on `Message-ID`. Only the first delivery creates a segment. |
| **5-second timeout on FE side** | Webhook payload too large (typically a big PDF attachment doubled by FE's `attachments[]` array) | Add `?attachments=false` to the URL if not already. The parse worker runs out-of-band, so the adapter response itself returns 202 quickly once the row is inserted. |

## Rotating the token

1. Generate a new value: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
2. Update `FORWARDEMAIL_RELAY_TOKEN` in your deployment env. Redeploy.
3. Update the FE webhook URL to the new token.

There is no key-rotation grace window. If you do it out of order, deliveries
401 until both sides match. Order matters: rotate the env var first
(briefly: deliveries fail closed), then update FE.

## Why this is a separate route from `/api/ingest/email`

ForwardEmail's webhook contract delivers JSON (`{raw, headers, attachments[],
session, ...}`) and signs payloads only on paid plans. trip-tracker's
existing direct-ingest route expects raw MIME bytes plus an HMAC + timestamp
+ nonce triple. Rather than degrade the direct route's contract, the
adapter is a sibling route that translates FE's JSON into the same internal
persistence helper (`_persist_raw_email`). Both routes converge into the
same worker pipeline, alias resolution, and `/inbox` UI — only the
authentication boundary is different.

## Glossary

**Local part** — Per RFC 5321/5322, everything before the `@` in an email
address. In `oliver@trips.example.com`, the local part is `oliver`.
trip-tracker uses this as the routing key.

**Domain** — Everything after the `@`. trip-tracker's worker **ignores the
domain entirely** when resolving an email to a user; only the local part is
matched against `forwarding_aliases.local_part`. So `oliver@anything.com` and
`oliver@somewhere-else.org` both route to the same `oliver` user.

**Alias resolution** — The worker's process for figuring out who a forwarded
email belongs to:

```
1. parse_mime(mime_bytes)         → ParsedEmail with .to_address
2. local_part = to_address.split("@", 1)[0].lower()
3. SELECT user_id FROM forwarding_aliases WHERE local_part = :local_part
4. If found → assign as raw_email's owner; if not → mark no_segments
```

**ForwardEmail (FE)** — `forwardemail.net`, the email-forwarding service this
adapter is built for. Free tier supports DNS-TXT-based aliases that forward
to webhooks; paid tier adds a dashboard and HMAC-signed payloads. trip-tracker
works with both tiers (the adapter only needs the URL to be private).

**Webhook adapter** — The `/api/ingest/forwardemail` route added by this
phase. Translates FE's JSON envelope into raw MIME bytes for the existing
worker pipeline. NOT the same as `/api/ingest/email` (which expects raw MIME
plus an HMAC triple — see the "Why this is a separate route" section above).

**Raw email** — In trip-tracker's database (`raw_emails` table), the full
RFC-822 MIME bytes of an inbound email, plus parsed headers (`to_address`,
`from_address`, `subject`, `message_id`). Both ingest routes write into this
table; the parser worker reads from it.

**Message-ID dedup** — Every email has a unique `Message-ID:` header. The
INSERT into `raw_emails` is `ON CONFLICT (message_id) DO NOTHING`, so the
same email forwarded twice produces only one row + one parse attempt.

**Inbox bucket** — `/inbox` shows three buckets per Phase 3 spec: `review`
(needs your confirmation), `no_segments` (parser couldn't extract anything),
and `duplicates` (Message-ID matched an existing trip). Forwarded emails
that successfully attribute to your user but couldn't be parsed land in
`no_segments`; ones that parsed but with low confidence land in `review`.
