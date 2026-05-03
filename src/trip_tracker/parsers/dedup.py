"""Dedup gate: find existing Segment matching a SegmentDraft.

Match rules in priority order (only #1 implemented in Task A2;
#2/#3 land in Task A3):
  1. Strong:  (provider_normalized, confirmation_number) — both non-null.
  2. Medium (transit):  type + start_at±N min + IATA pair.
  3. Medium (lodging):  type='lodging' + date(start_at) + hotel name CI.
  4. No match below medium. Fuzzy provider matching deliberately excluded.

Match candidates are scoped to owner_user_id and exclude cancelled segments.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.parsers.base import SegmentDraft


def _normalize_provider(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip().lower()
    return cleaned or None


async def _strong_match(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    draft: SegmentDraft,
) -> Segment | None:
    if not draft.confirmation_number:
        return None
    provider_norm = _normalize_provider(draft.provider)
    if not provider_norm:
        return None
    stmt = (
        select(Segment)
        .where(
            Segment.owner_user_id == owner_user_id,
            Segment.status != "cancelled",
            Segment.confirmation_number == draft.confirmation_number,
            func.lower(func.trim(Segment.provider)) == provider_norm,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def find_existing_segment(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    draft: SegmentDraft,
) -> Segment | None:
    """Return the first existing Segment matching draft, or None."""
    if hit := await _strong_match(db, owner_user_id, draft):
        return hit
    # Medium match in Task A3.
    return None
