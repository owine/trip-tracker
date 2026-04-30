"""Filename → segment.id auto-link heuristic. Spec §7."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import date
from typing import TYPE_CHECKING, cast

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

_DATE_DASH = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DATE_COMPACT = re.compile(r"(\d{4})(\d{2})(\d{2})")


def match_attachment_to_segment(
    filename: str,
    segments: Sequence[object],
) -> uuid.UUID | None:
    """Return the matching segment.id or None.

    Three rules, first match wins:
      1. confirmation_number (exact, case-insensitive, as whole word)
      2. flight_number / train_number (same shape, from Segment.details)
      3. unique start_at::date (YYYY-MM-DD or YYYYMMDD in filename)

    Ambiguous date matches (≥2 segments on the same day) → None.

    `segments` is typed as Sequence[object] to keep the function purely
    structural — callers pass real Segment ORM rows or dataclass test fakes.
    """
    # Rule 1: confirmation number
    for s in segments:
        conf = getattr(s, "confirmation_number", None)
        if conf:
            # Match as whole alphanumeric token (not surrounded by letters/digits)
            pattern = rf"(?<![a-zA-Z0-9]){re.escape(conf)}(?![a-zA-Z0-9])"
            if re.search(pattern, filename, re.IGNORECASE):
                return cast(uuid.UUID, getattr(s, "id"))  # noqa: B009

    # Rule 2: vehicle number (flight_number or train_number from details)
    for s in segments:
        details = getattr(s, "details", None) or {}
        for key in ("flight_number", "train_number"):
            vnum = details.get(key)
            if vnum:
                pattern = rf"(?<![a-zA-Z0-9]){re.escape(str(vnum))}(?![a-zA-Z0-9])"
                if re.search(pattern, filename, re.IGNORECASE):
                    return cast(uuid.UUID, getattr(s, "id"))  # noqa: B009

    # Rule 3: unique date
    target: date | None = None
    m = _DATE_DASH.search(filename)
    if m is None:
        m = _DATE_COMPACT.search(filename)
    if m is not None:
        target = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if target is None:
        return None

    same_day = [
        s
        for s in segments
        if getattr(s, "start_at", None) and getattr(s, "start_at").date() == target  # noqa: B009
    ]
    if len(same_day) == 1:
        return cast(uuid.UUID, getattr(same_day[0], "id"))  # noqa: B009
    return None


async def autolink_pending_for_email(
    db: AsyncSession,
    *,
    raw_email_id: uuid.UUID,
) -> None:
    """For each Document with raw_email_id=:rid AND segment_id IS NULL,
    look up that email's segments and run match_attachment_to_segment.
    Update the doc with segment_id + trip_id when matched.

    Manual links (set via /documents/{id}/link) are preserved by the
    `segment_id IS NULL` filter — the heuristic skips already-linked docs.
    """
    # Local imports to avoid circulars at module-import time.
    from trip_tracker.models.document import Document
    from trip_tracker.models.segment import Segment

    docs = (
        (
            await db.execute(
                select(Document).where(
                    Document.raw_email_id == raw_email_id,
                    Document.segment_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not docs:
        return

    segs = (
        (await db.execute(select(Segment).where(Segment.raw_email_id == raw_email_id)))
        .scalars()
        .all()
    )
    if not segs:
        return

    for doc in docs:
        match_id = match_attachment_to_segment(doc.filename, segs)
        if match_id is None:
            continue
        seg = next(s for s in segs if s.id == match_id)
        doc.segment_id = match_id
        doc.trip_id = seg.trip_id
    await db.commit()
