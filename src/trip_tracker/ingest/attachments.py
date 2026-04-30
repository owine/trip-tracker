"""Extract non-inline attachments from a raw MIME body."""

from __future__ import annotations

import email
import email.parser
import email.policy
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Attachment:
    filename: str
    content_type: str
    payload: bytes


def extract_attachments(body: bytes) -> list[Attachment]:
    """Return all non-inline attachments. Inline + multipart wrappers are skipped."""
    msg = email.parser.BytesParser(policy=email.policy.default).parsebytes(body)
    out: list[Attachment] = []
    for part in msg.iter_attachments():
        try:
            payload = part.get_content()
        except (KeyError, AttributeError):
            continue
        if not isinstance(payload, bytes):
            # Text attachments come back as str — skip; we only care about PDFs.
            continue
        out.append(
            Attachment(
                filename=part.get_filename() or "attachment",
                content_type=part.get_content_type() or "application/octet-stream",
                payload=payload,
            )
        )
    return out
