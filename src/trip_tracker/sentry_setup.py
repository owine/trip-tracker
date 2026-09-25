"""Error reporting to GlitchTip over the Sentry protocol. Errors only.

`init_sentry` is called from both entry points (`app.create_app`, and
`worker.startup`). With no `SENTRY_DSN` it returns without touching the SDK,
so the process behaves exactly as it does without this module.

Privacy: forwarded emails are the payload of this app, so nothing from one may
leave in an event. Request bodies and frame locals are switched off at init;
`scrub_event` / `scrub_breadcrumb` catch what the integrations still attach.
Parse-failure events carry the RawEmail id and a hash of the Message-ID,
which is enough to find the row and replay it.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

import sentry_sdk
from sentry_sdk.integrations.anthropic import AnthropicIntegration

from trip_tracker import build_info
from trip_tracker.config import WorkerSettings

if TYPE_CHECKING:
    from sentry_sdk.types import Breadcrumb, BreadcrumbHint, Event, Hint

_FILTERED = "[Filtered]"

# Keys dropped from `extra` wherever they appear. They name email content or
# the sender; our own log calls use them (e.g. webhook.py logs from_address).
_SENSITIVE_KEYS = frozenset(
    {
        "body",
        "mime",
        "mime_blob",
        "raw",
        "html",
        "text",
        "subject",
        "from",
        "from_address",
        "sender",
        "to",
        "to_address",
        "email",
        "confirmation_number",
    }
)

# SQLAlchemy appends bound parameters to exception messages, which can be a
# confirmation number or a whole MIME blob. Keep the SQL, drop the values.
_SQLA_PARAMS_RE = re.compile(r"\[parameters: .*?\](?=\n\(Background|\Z)", re.DOTALL)
_ICS_TOKEN_RE = re.compile(r"/ics/[^/?#]+\.ics")

# Set on an exception once reported. saq logs a job's exception again from its
# parent task, and DedupeIntegration can't see across that boundary (its state
# is a ContextVar, and the job ran in a child task with a copied context).
_REPORTED_ATTR = "__trip_tracker_reported__"

_STRUCTLOG_RESERVED = frozenset({"event", "level", "timestamp", "exc_info", "stack_info"})


def message_id_hash(message_id: str | None) -> str:
    """Opaque, stable handle for an email: sha256 of its Message-ID, 16 hex."""
    if not message_id:
        return "none"
    return hashlib.sha256(message_id.encode()).hexdigest()[:16]


def init_sentry(settings: WorkerSettings, *, component: str) -> bool:
    """Initialise the SDK if a DSN is configured. Returns whether it did."""
    dsn = settings.sentry_dsn.get_secret_value().strip() if settings.sentry_dsn else ""
    if not dsn:
        return False

    sentry_sdk.init(
        dsn=dsn,
        release=build_info.GIT_SHA if build_info.GIT_SHA != "unknown" else None,
        environment=settings.sentry_environment,
        # Errors only: GlitchTip's performance support is limited.
        traces_sample_rate=0,
        send_default_pii=False,
        include_local_variables=False,
        # Ingest endpoints receive whole emails as the request body.
        max_request_body_size="never",
        auto_session_tracking=False,  # GlitchTip does not support sessions
        # Captures every Anthropic client error, including ones dispatch_parse
        # already handles by falling back; the parse-failure event covers it.
        disabled_integrations=[AnthropicIntegration()],
        before_send=scrub_event,
        before_breadcrumb=scrub_breadcrumb,
    )
    sentry_sdk.get_global_scope().set_tag("component", component)
    return True


def capture_once(exc: BaseException) -> None:
    """Report `exc` from the current scope; later captures of it are dropped."""
    sentry_sdk.capture_exception(exc)
    with contextlib.suppress(AttributeError):
        setattr(exc, _REPORTED_ATTR, True)


def scrub_event(event: Event, hint: Hint) -> Event | None:
    """`before_send`: drop re-reports, strip email content, sender, URL secrets."""
    exc_info = hint.get("exc_info")
    if exc_info and getattr(exc_info[1], _REPORTED_ATTR, False):
        return None

    request = event.get("request")
    if request:
        for key in ("data", "cookies", "query_string"):
            request.pop(key, None)
        url = request.get("url")
        if isinstance(url, str):
            request["url"] = _ICS_TOKEN_RE.sub(f"/ics/{_FILTERED}.ics", url)

    for exc in (event.get("exception") or {}).get("values", []):
        if isinstance(exc.get("value"), str):
            exc["value"] = _SQLA_PARAMS_RE.sub(f"[parameters: {_FILTERED}]", exc["value"])
        for frame in (exc.get("stacktrace") or {}).get("frames", []):
            frame.pop("vars", None)

    extra = event.get("extra")
    if extra:
        event["extra"] = {k: v for k, v in extra.items() if k.lower() not in _SENSITIVE_KEYS}
    return event


def scrub_breadcrumb(crumb: Breadcrumb, hint: BreadcrumbHint) -> Breadcrumb | None:
    """`before_breadcrumb`: log breadcrumbs keep the format string, not its args.

    Args are where exception text and addresses end up ("vendor %s raised: %s",
    uvicorn's access line with the ICS token in the path).
    """
    record = hint.get("log_record")
    if isinstance(record, logging.LogRecord):
        crumb["message"] = str(record.msg)
        crumb["data"] = {}
    return crumb


def structlog_processor(
    _logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """structlog processor: error/critical → event, warning/info → breadcrumb.

    Returns `event_dict` untouched so the rendered stdout line is unchanged.
    Must run before `format_exc_info`, which consumes `exc_info`.
    """
    if not sentry_sdk.get_client().is_active():
        return event_dict

    level = str(event_dict.get("level", method_name))
    message = str(event_dict.get("event", ""))
    if level in ("error", "critical"):
        with sentry_sdk.new_scope() as scope:
            for key, value in event_dict.items():
                if key not in _STRUCTLOG_RESERVED:
                    scope.set_extra(key, value)
            exc_info = _resolve_exc_info(event_dict.get("exc_info"))
            if exc_info is not None:
                scope.set_extra("log_event", message)
                sentry_sdk.capture_exception(exc_info)
            else:
                sentry_sdk.capture_message(message, level=level)  # type: ignore[arg-type]
    elif level in ("warning", "info"):
        # Event name only: kwargs can carry addresses (see webhook.py).
        sentry_sdk.add_breadcrumb(category="structlog", message=message, level=level)
    return event_dict


def _resolve_exc_info(exc_info: Any) -> Any:
    if exc_info is True:
        exc_info = sys.exc_info()
    if isinstance(exc_info, BaseException):
        return exc_info
    if isinstance(exc_info, tuple) and exc_info[0] is not None:
        return exc_info
    return None
