"""Document upload helpers: sha256, magic-byte check, size cap exception."""

from __future__ import annotations

import hashlib

PDF_MAGIC = b"%PDF"


class SizeLimitExceeded(Exception):
    """Raised when a streaming upload exceeds MAX_UPLOAD_BYTES."""

    def __init__(self, *, limit: int, observed: int) -> None:
        super().__init__(f"upload exceeded {limit} bytes (saw {observed})")
        self.limit = limit
        self.observed = observed


def sha256_hex(content: bytes) -> str:
    """Lowercase 64-char hex sha256 of content. Used for storage_key + dedup."""
    return hashlib.sha256(content).hexdigest()


def is_pdf(content: bytes) -> bool:
    """First-4-bytes magic check. Authoritative — Content-Type is advisory."""
    return content[: len(PDF_MAGIC)] == PDF_MAGIC
