"""Forwarded-email unwrap helper.

When a user manually forwards a vendor email through their mail client (e.g.
Apple Mail's "Forward", Gmail's "Forward", Outlook's "Forward"), the outer
`From:` header becomes the user, not the vendor. The inner forwarded content
is inserted in the body, prefixed with a client-specific marker.

`effective_from()` detects those markers, locates the inner `From:` header
inside the forwarded section, and returns just the email address (with any
display name + angle brackets stripped) for vendor-pattern matching. For
non-forwarded emails it returns the outer `From:` header unchanged.

Why this matters: vendor sender_patterns are typically anchored on `$`
(end-of-string), so they need a bare email — `noreply@airfrance.com`, not
`Air France <noreply@airfrance.com>`. Extracting via `email.utils.parseaddr`
gives us the bare email regardless of the inner format.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from email.utils import parseaddr

from trip_tracker.parsers._email_text import extract_text

# Common forward-section markers across mail clients:
#   - Apple Mail: "Begin forwarded message:"
#   - Gmail:      "---------- Forwarded message ---------"
#   - Outlook:    "-----Original Message-----"
_FORWARD_MARKERS = re.compile(
    r"(?:Begin forwarded message:?"
    r"|-{2,}\s*Forwarded message\s*-{2,}"
    r"|-{2,}\s*Original Message\s*-{2,})",
    re.IGNORECASE,
)

# `From:` header inside the forwarded section. Allows:
#   - leading whitespace (some clients indent forwarded headers)
#   - leading `>` quoting prefix(es) — Apple Mail prefixes every line of the
#     forwarded body with `> ` when the user types text above the forward,
#     producing lines like `> From: vendor@example.com`. Multiple `>>` levels
#     occur when forwarding an already-forwarded message.
_INNER_FROM = re.compile(
    r"^[\s>]*From:\s*(.+?)\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def effective_from(msg: EmailMessage) -> str:
    """Return the address best representing the original sender.

    For forwarded emails: the inner `From:` header inside the forwarded body,
    parsed via `email.utils.parseaddr` so display-name+bracket forms collapse
    to a bare email. e.g. "Air France <noreply@airfrance.com>" → "noreply@airfrance.com".

    For non-forwarded emails: the outer `From:` header unchanged. (Direct
    deliveries — the worker sees the original vendor's `From:` and existing
    vendor patterns still work.)

    Edge case: if a forward marker is present but the inner `From:` can't be
    parsed (mangled forward, attachment-only forward, etc.), falls back to
    the outer `From:` rather than returning empty. Defensive — better to try
    the outer match than to skip vendor selection entirely.
    """
    body = extract_text(msg)
    fwd_match = _FORWARD_MARKERS.search(body)
    if fwd_match is None:
        return msg.get("From", "")

    after = body[fwd_match.end() :]
    inner_match = _INNER_FROM.search(after)
    if inner_match is None:
        return msg.get("From", "")

    inner_from_raw = inner_match.group(1)
    _name, addr = parseaddr(inner_from_raw)
    return addr if addr else inner_from_raw
