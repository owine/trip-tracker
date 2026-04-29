"""MIME parsing for ingested emails. Spec §5 step 6."""

from __future__ import annotations

import email
import email.parser
import email.policy
import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    message_id: str
    to_address: str
    from_address: str
    subject: str | None
    headers: dict[str, str]
    body: bytes


def _str(v: object) -> str:
    """Coerce header value to a plain string with whitespace stripped."""
    return str(v).strip()


def parse_mime(body: bytes) -> ParsedEmail:
    """Parse raw MIME bytes; synthesize a Message-ID if missing.

    The synthetic Message-ID format is ``<sha256:<64 hex>@trip-tracker.local>``
    and is included verbatim (with angle brackets) in the returned struct so
    the caller can store it as-is.
    """
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(body)

    raw_msg_id = msg.get("Message-ID") or msg.get("Message-Id")
    if raw_msg_id:
        message_id = _str(raw_msg_id)
    else:
        digest = hashlib.sha256(body).hexdigest()
        message_id = f"<sha256:{digest}@trip-tracker.local>"

    headers: dict[str, str] = {}
    for key, value in msg.items():
        # Last-write-wins for duplicated headers; spec allows either.
        headers[key] = _str(value)

    return ParsedEmail(
        message_id=message_id,
        to_address=_str(msg.get("To") or ""),
        from_address=_str(msg.get("From") or ""),
        subject=_str(msg.get("Subject")) if msg.get("Subject") else None,
        headers=headers,
        body=body,
    )
