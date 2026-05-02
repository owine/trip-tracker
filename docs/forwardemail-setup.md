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
whose `local_part` matches the local-part of your FE alias address. The
worker lowercases before joining, so case doesn't matter — `me@trips.example.com`
matches a `local_part = "me"` row.

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
