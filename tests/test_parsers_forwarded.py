"""effective_from(): unwrap the inner From: header from forwarded emails.

Real-world flow: user manually forwards a vendor email (Air France, Trainline,
etc.) to their FE alias. The outer `From:` becomes the user, the inner
forwarded content carries the vendor's address. Without unwrapping, vendor
matchers see the user's address and miss every pack — emails land in
`/inbox` as `no_segments` even when the vendor IS supported.
"""

from __future__ import annotations

from email.message import EmailMessage

from trip_tracker.parsers.forwarded import effective_from


def _msg(from_addr: str, body: str, subject: str = "Fwd: test") -> EmailMessage:
    """Build a minimal EmailMessage with given outer From + plaintext body."""
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def test_non_forwarded_returns_outer_from() -> None:
    """Direct delivery — outer From: passes through unchanged."""
    msg = _msg(
        "noreply@airfrance.com",
        "Your booking is confirmed.",
        subject="Booking confirmation",
    )
    assert effective_from(msg) == "noreply@airfrance.com"


def test_apple_mail_forward() -> None:
    """Apple Mail's `Begin forwarded message:` marker."""
    body = """\
Hey check this out

Begin forwarded message:

From: Air France <noreply@airfrance.com>
Subject: Your booking AF1234
Date: November 4, 2025 at 10:30:00 AM PST
To: Oliver <oliver@personal.com>

Booking details follow...
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "noreply@airfrance.com"


def test_gmail_forward() -> None:
    """Gmail's dashed `Forwarded message` marker."""
    body = """\
FYI

---------- Forwarded message ---------
From: SNCF <noreply@sncf.com>
Date: Mon, Nov 4, 2025 at 10:30 AM
Subject: Your TGV ticket
To: <oliver@personal.com>

Your TGV from Paris...
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "noreply@sncf.com"


def test_outlook_forward() -> None:
    """Outlook's `Original Message` marker."""
    body = """\
See below.

-----Original Message-----
From: "Trainline" <noreply@thetrainline.com>
Sent: Monday, November 4, 2025 10:30 AM
To: Oliver Wine
Subject: Your booking confirmation

Your TGV Inoui booking is confirmed.
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "noreply@thetrainline.com"


def test_forward_with_bare_email_no_brackets() -> None:
    """Inner From: as bare email (no display name, no brackets)."""
    body = """\
Begin forwarded message:

From: noreply@airfrance.com
Subject: Booking
Date: Nov 4, 2025

Body...
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "noreply@airfrance.com"


def test_forward_marker_present_but_no_inner_from_falls_back() -> None:
    """Defensive: forward marker found but no parseable inner From: header
    (mangled forward, malformed body, etc.) — fall back to outer From:."""
    body = """\
---------- Forwarded message ---------
This is a malformed forward without proper headers.
Just some prose.
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "user@personal.com"


def test_quoted_display_name_is_stripped() -> None:
    """Inner From: with quoted display name still extracts bare email."""
    body = """\
Begin forwarded message:

From: "Air France Customer Care" <customer-care@airfrance.com>
Subject: Confirmation

Body...
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "customer-care@airfrance.com"


def test_case_insensitive_marker() -> None:
    """Marker detection is case-insensitive."""
    body = """\
BEGIN FORWARDED MESSAGE:

From: noreply@airfrance.com
Subject: Test

Body...
"""
    msg = _msg("user@personal.com", body)
    assert effective_from(msg) == "noreply@airfrance.com"
