"""Shared text/plain body extraction helper for vendor parsers.

Phase 2 Task 12 noted: 'The `_extract_text` helper is repeated; if a 3rd
parser duplicates it, refactor to parsers/_email_text.py then.' We have 11
duplicates now — consolidating.
"""

from __future__ import annotations

from email.message import EmailMessage


def extract_text(msg: EmailMessage) -> str:
    """Pull text/plain body from an email, handling multipart + charset."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if isinstance(payload, bytes):
                    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    payload = msg.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload.decode(msg.get_content_charset() or "utf-8", errors="replace")
    return str(msg.get_payload() or "")
