"""RFC 5545 iCalendar serializer for the Phase 6 subscribable feed.

Hand-rolled, not via icalendar lib, to avoid pulling pytz into runtime
dependencies. Spec §6.

Times always emit in UTC basic format (YYYYMMDDTHHMMSSZ); calendar
clients localize on display. Lines fold at 75 OCTETS (not characters)
per RFC 5545 §3.1; multi-byte UTF-8 sequences are not split mid-byte.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

from trip_tracker.models.segment import Segment
from trip_tracker.models.user import User

_FOLD_OCTETS = 75
_PRODID = "-//trip-tracker//EN"


def render_calendar(
    *,
    user: User,
    segments: Sequence[Segment],
    base_url: str,
) -> str:
    """Render a full VCALENDAR including one VEVENT per segment.

    Returns CRLF-line-terminated text suitable for serving as
    text/calendar; charset=utf-8.
    """
    host = urlparse(base_url).netloc or "localhost"
    name = f"Trip-Tracker · {user.display_name}"
    lines: list[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{_PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"NAME:{_escape(name)}",
        f"X-WR-CALNAME:{_escape(name)}",
    ]
    now = datetime.now(UTC)
    for s in segments:
        lines.extend(_render_segment_vevent(s, host=host, base_url=base_url, now=now))
    lines.append("END:VCALENDAR")
    folded = [_fold(line) for line in lines]
    return "\r\n".join(folded) + "\r\n"


def _render_segment_vevent(
    s: Segment,
    *,
    host: str,
    base_url: str,
    now: datetime,
) -> Iterable[str]:
    summary, location = _summary_and_location(s)
    description = _description(s)
    end_at = s.end_at or s.start_at + timedelta(hours=1)
    yield "BEGIN:VEVENT"
    yield f"UID:{s.id}@{host}"
    yield f"DTSTAMP:{_dt(now)}"
    yield f"DTSTART:{_dt(s.start_at)}"
    yield f"DTEND:{_dt(end_at)}"
    yield f"SUMMARY:{_escape(summary)}"
    if location:
        yield f"LOCATION:{_escape(location)}"
    if description:
        yield f"DESCRIPTION:{_escape(description)}"
    yield f"URL:{base_url}/trips/{s.trip_id}#segment-{s.id}"
    if s.type == "flight":
        flight_num = (s.details or {}).get("flight_number") or "your flight"
        yield "BEGIN:VALARM"
        yield "ACTION:DISPLAY"
        yield f"DESCRIPTION:{_escape(f'✈ Leave for the airport — {flight_num}')}"
        yield "TRIGGER:-PT3H"
        yield "END:VALARM"
    yield "END:VEVENT"


def _summary_and_location(s: Segment) -> tuple[str, str | None]:
    """Per-type SUMMARY/LOCATION dispatch. See spec §6.3."""
    start_loc = s.start_location or {}
    end_loc = s.end_location or {}
    details = s.details or {}
    provider = s.provider or ""

    if s.type == "flight":
        flight_number = details.get("flight_number")
        s_iata = start_loc.get("iata")
        e_iata = end_loc.get("iata")
        if flight_number and s_iata and e_iata:
            summary = f"✈ {flight_number} {s_iata} → {e_iata}"
        elif s_iata and e_iata:
            summary = f"✈ {s_iata} → {e_iata}"
        elif provider:
            summary = f"✈ {provider}"
        else:
            summary = "✈ flight"
        return summary, end_loc.get("city")

    if s.type == "lodging":
        summary = f"🏨 {provider}" if provider else "🏨 lodging"
        location = end_loc.get("address") or end_loc.get("city")
        return summary, location

    if s.type == "car":
        city = start_loc.get("city")
        if provider and city:
            summary = f"🚗 {provider} {city}"
        elif provider:
            summary = f"🚗 {provider}"
        elif city:
            summary = f"🚗 {city}"
        else:
            summary = "🚗 car"
        return summary, start_loc.get("city")

    if s.type == "train":
        train_number = details.get("train_number")
        s_iata = start_loc.get("iata")
        e_iata = end_loc.get("iata")
        if train_number and s_iata and e_iata:
            summary = f"🚆 {train_number} {s_iata} → {e_iata}"
        elif s_iata and e_iata:
            summary = f"🚆 {s_iata} → {e_iata}"
        elif provider:
            summary = f"🚆 {provider}"
        else:
            summary = "🚆 train"
        return summary, end_loc.get("city")

    if s.type == "transfer":
        summary = f"🚐 {provider}" if provider else "🚐 transfer"
        return summary, end_loc.get("city")

    if s.type == "activity":
        summary = f"🎟 {provider}" if provider else "🎟 activity"
        return summary, start_loc.get("city")

    return f"📍 {s.type} · {provider}".rstrip(" ·"), None


def _description(s: Segment) -> str:
    parts: list[str] = []
    if s.confirmation_number:
        parts.append(f"Conf: {s.confirmation_number}")
    if s.provider:
        parts.append(f"Provider: {s.provider}")
    if s.parse_source and s.parse_source.startswith("llm"):
        parts.append("✨ AI-suggested (review in app)")
    return " · ".join(parts)


def _dt(d: datetime) -> str:
    """Format a datetime as RFC 5545 UTC basic form: YYYYMMDDTHHMMSSZ."""
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    d_utc = d.astimezone(UTC)
    return d_utc.strftime("%Y%m%dT%H%M%SZ")


def _escape(s: str) -> str:
    """Escape a TEXT-typed value per RFC 5545 §3.3.11.

    Order matters: backslash first so we don't double-escape backslashes
    we just inserted.
    """
    s = s.replace("\\", "\\\\")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    # Collapse all line endings into a literal `\n`. Don't leave bare CR.
    return s.replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")


def _fold(line: str) -> str:
    """Fold a logical line at 75 octets per RFC 5545 §3.1.

    Continuation lines start with ``\\r\\n `` (CRLF + single space). Counts
    are in OCTETS, not characters: a multi-byte UTF-8 sequence must not
    be split. We fold on the largest UTF-8 prefix that fits.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= _FOLD_OCTETS:
        return line
    chunks: list[str] = []
    remaining = encoded
    first = True
    while remaining:
        budget = _FOLD_OCTETS if first else _FOLD_OCTETS - 1
        if len(remaining) <= budget:
            chunks.append(remaining.decode("utf-8"))
            break
        # Don't slice mid-codepoint: shrink budget until decode succeeds.
        # `remaining` always starts at a valid UTF-8 boundary, so this loop
        # terminates well before cut=0.
        cut = budget
        while cut > 0:
            try:
                head = remaining[:cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        else:
            # Unreachable for valid UTF-8 input; raise rather than infinite-loop
            # if some upstream caller hands us pre-truncated bytes.
            raise ValueError("cannot fold: invalid UTF-8 prefix in line")
        chunks.append(head)
        remaining = remaining[cut:]
        first = False
    return "\r\n ".join(chunks)
