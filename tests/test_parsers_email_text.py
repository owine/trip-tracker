"""Tests for parsers._email_text.extract_text."""

from __future__ import annotations

from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from trip_tracker.parsers._email_text import extract_text


def test_extract_plain_text() -> None:
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("plain body")
    assert "plain body" in extract_text(msg)


def test_extract_multipart_text_plain() -> None:
    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText("html body", "html"))
    msg.attach(MIMEText("plain body extracted", "plain"))
    out = extract_text(msg)
    assert "plain body extracted" in out


def test_extract_no_payload_fallback() -> None:
    msg = EmailMessage()
    msg["Subject"] = "x"
    # Don't set content — get_payload returns ''
    assert extract_text(msg) == ""
