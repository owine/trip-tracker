"""Document helpers: sha256, magic-byte, size cap."""

from __future__ import annotations

import hashlib

import pytest

from trip_tracker.documents.helpers import (
    PDF_MAGIC,
    SizeLimitExceeded,
    is_pdf,
    sha256_hex,
)


def test_sha256_hex_matches_hashlib() -> None:
    body = b"hello world"
    assert sha256_hex(body) == hashlib.sha256(body).hexdigest()


def test_sha256_hex_for_empty_bytes() -> None:
    assert sha256_hex(b"") == hashlib.sha256(b"").hexdigest()


def test_is_pdf_accepts_pdf_magic() -> None:
    assert is_pdf(PDF_MAGIC + b"-1.4\n%hello") is True


def test_is_pdf_accepts_exact_4_bytes() -> None:
    assert is_pdf(b"%PDF") is True


def test_is_pdf_rejects_other_content() -> None:
    assert is_pdf(b"PNG\r\n") is False
    assert is_pdf(b"") is False
    assert is_pdf(b"%PD") is False  # too short
    assert is_pdf(b"\x89PNG") is False
    assert is_pdf(b"PDF%") is False  # right bytes, wrong order


def test_size_limit_exceeded_carries_attrs() -> None:
    exc = SizeLimitExceeded(limit=100, observed=150)
    assert exc.limit == 100
    assert exc.observed == 150
    assert "100" in str(exc)
    assert "150" in str(exc)


def test_size_limit_exceeded_is_exception() -> None:
    with pytest.raises(SizeLimitExceeded) as exc_info:
        raise SizeLimitExceeded(limit=10, observed=15)
    assert exc_info.value.limit == 10
