# Observability

The app and the worker log to stdout and, when a DSN is configured, report errors to a Sentry-protocol endpoint (a self-hosted GlitchTip, via the official `sentry-sdk`). Error reporting is optional: with no DSN the SDK is never initialised and both processes behave exactly as they do without it.

## Environment variables

All optional. Set them on **both** the app and the worker container.

| Var | Purpose | Default |
|---|---|---|
| `SENTRY_DSN` | GlitchTip project DSN. Init is skipped entirely when this is unset or empty. | unset |
| `SENTRY_ENVIRONMENT` | `environment` on every event, e.g. `production` or `staging`. | `production` |
| `LOG_LEVEL` | Stdlib and structlog level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). | `INFO` |
| `LOG_FORMAT` | App stdout format: `json` or `console`. | `json` |

`release` is not an env var. It is the deployed git SHA, which CI bakes into the image as `TRIP_TRACKER_GIT_SHA` (Dockerfile `ARG GIT_SHA`). Local builds without that arg send no release.

The SDK is initialised for errors only: `traces_sample_rate=0`, no profiling, `send_default_pii=False`, and `auto_session_tracking=False` because GlitchTip does not support sessions. Sentry Logs (`enable_logs`) stays off. Stdout is still where logs live.

## What becomes an event

| Source | Becomes | Notes |
|---|---|---|
| Unhandled exception in a request | event | FastAPI/Starlette integration |
| Stdlib `logger.error` / `logger.exception` | event | `LoggingIntegration`, default `event_level=ERROR` |
| structlog `log.error` / `log.exception` / `log.critical` | event | `sentry_setup.structlog_processor`, which sits in the structlog chain before `format_exc_info` |
| Stdlib `logger.info` / `logger.warning` | breadcrumb | Format string only (see Redaction) |
| structlog `log.info` / `log.warning` | breadcrumb | Event name only, no key/values |
| Exception in `parse_raw_email` | one event per attempt | Tagged with the parse context below. saq logs the same exception again from its parent task, and that duplicate is dropped. |
| Email parsed to zero segments (`no_segments`) | one event | `parse_failed: no segments`: the "trip never appears" case |

Deliberately **not** events:

- A Redis blip while enqueueing document extraction (`_enqueue_doc_extracts`). It is a warning and becomes a breadcrumb; the document stays `pending` and is retried later.
- A strategy failing inside `dispatch_parse` (JSON-LD, a vendor pack, or the LLM) when a later strategy recovers. These are warnings. If every strategy fails, the email ends up `no_segments` and that raises the single parse-failure event.
- Anthropic client errors. The auto-enabled `AnthropicIntegration` is disabled, because it would report errors that `dispatch_parse` already handles by falling back.

### Parse-failure context

Every event raised while parsing an email carries these tags, which are enough to find the email and replay it:

| Tag | Value |
|---|---|
| `raw_email_id` | `RawEmail.id` (UUID). Replay with `python -m trip_tracker parse_pending` after resetting the row to `pending`. |
| `message_id_sha256` | First 16 hex characters of sha256(`Message-ID`). To match a message in your mailbox, hash its header: `python -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:16])' '<id@host>'` |
| `parser` | `ParseResult.source` of the best attempt, e.g. `none`, `json-ld`, a vendor pack name, or `llm:haiku-4-5`. Set once dispatch has run. |
| `budget_skipped` | `true` when the LLM fallback was skipped because the daily budget was spent. Only on `no_segments` events. |
| `component` | `app` or `worker` (on every event). |

`no_segments` events are fingerprinted `["parse-failed", <parser>]`, so GlitchTip groups them into one issue per parser.

## Redaction

Forwarded emails are this app's payload, so no email body, sender address, subject, or confirmation number is sent. Enforced in `src/trip_tracker/sentry_setup.py`:

- **Request bodies** are never attached (`max_request_body_size="never"`). The ingest endpoints receive whole emails. `before_send` also drops `request.data`, `request.cookies`, and `request.query_string` (the ForwardEmail relay token lives in `?token=`).
- **ICS feed tokens** in the request URL become `/ics/[Filtered].ics`.
- **Frame locals** are off (`include_local_variables=False`), and `before_send` strips any `vars` that still appear.
- **SQLAlchemy exception messages** keep the `[SQL: ...]` and lose the `[parameters: ...]` block, which can hold a confirmation number or a MIME blob.
- **`extra` keys** naming email content or addresses (`body`, `mime_blob`, `subject`, `from_address`, `to_address`, `confirmation_number`, and similar) are dropped. The list is `_SENSITIVE_KEYS`.
- **Breadcrumbs** from stdlib logging keep the format string and lose its arguments, e.g. `vendor %s raised: %s`. Arguments are where exception text, addresses, and the ICS token in uvicorn's access line end up. structlog breadcrumbs carry only the event name.

Stdout is **not** scrubbed; the JSON log lines are unchanged. When you add a log call that names new email-derived data, add its key to `_SENSITIVE_KEYS`.

Free-text exception messages are the remaining gap. An exception whose own message quotes email content (for example, a vendor parser raising `ValueError(f"bad date {text}")` that escapes the task) is sent as-is. Keep email content out of exception messages.

## Setting up GlitchTip

1. Run GlitchTip in your homelab Docker Compose (`glitchtip/glitchtip` plus Postgres and Valkey/Redis).
2. Create a project (platform: Python) and copy its DSN.
3. Set `SENTRY_DSN` to that DSN on both the app and the worker. In `docker-piwine`, that's `TRIPS_SENTRY_DSN` in the stack's `.env`, wired into `trips/compose.yaml`.
4. Optionally set `SENTRY_ENVIRONMENT`.
5. Recreate both containers. To check it works, reset one `RawEmail` to `pending` and forward an email that can't parse (a marketing email works). A `parse_failed` issue should appear.

## Reading logs

```bash
docker logs trips-app | jq .          # app: structlog JSON
docker logs trips-worker --since 1h   # worker: saq's stdlib logging format
```
