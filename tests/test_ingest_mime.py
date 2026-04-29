"""MIME parsing helpers."""

from __future__ import annotations

import hashlib
from pathlib import Path

from trip_tracker.ingest.mime import ParsedEmail, parse_mime

FIXTURE = Path(__file__).parent / "fixtures" / "webhooks" / "sample.eml"


def _body() -> bytes:
    return FIXTURE.read_bytes()


def test_parse_basic() -> None:
    parsed = parse_mime(_body())
    assert isinstance(parsed, ParsedEmail)
    assert parsed.message_id == "<abc123-confirm@delta.com>"
    assert parsed.to_address == "oliver@trips.example.com"
    assert (
        parsed.from_address.endswith("confirmations@delta.com>")
        or parsed.from_address == "confirmations@delta.com"
    )
    assert parsed.subject == "Your Trip Confirmation - DL44"
    assert "Delta" in parsed.headers["From"]


def test_synthetic_message_id_when_missing() -> None:
    body = _body().replace(b"Message-ID: <abc123-confirm@delta.com>\r\n", b"")
    parsed = parse_mime(body)
    expected_hex = hashlib.sha256(body).hexdigest()
    assert parsed.message_id == f"<sha256:{expected_hex}@trip-tracker.local>"


def test_long_subject_handled() -> None:
    body = _body()
    parsed = parse_mime(body)
    assert parsed.subject is not None
    # Real test: very long subject doesn't crash
    long = b"Subject: " + (b"x" * 2000) + b"\r\n"
    body2 = body.replace(b"Subject: Your Trip Confirmation - DL44\r\n", long)
    p2 = parse_mime(body2)
    assert p2.subject is not None
    assert len(p2.subject) >= 500
