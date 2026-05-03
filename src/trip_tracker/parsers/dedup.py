"""Dedup gate: find existing Segment matching a SegmentDraft.

Match rules in priority order:
  1. Strong:  (provider_normalized, confirmation_number) — both non-null.
  2. Medium (transit):  type + start_at±N min + IATA pair.
  3. Medium (lodging):  type='lodging' + date(start_at) + hotel name CI.
  4. No match below medium. Fuzzy provider matching deliberately excluded.

Match candidates are scoped to owner_user_id and exclude cancelled segments.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment
from trip_tracker.parsers.base import SegmentDraft

_MEDIUM_TIME_WINDOW_MIN = 30  # tunable; make configurable via Settings if churned

_TRANSIT_TYPES = frozenset({"flight", "train", "transfer"})


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


async def _medium_transit_match(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    draft: SegmentDraft,
) -> Segment | None:
    if draft.type not in _TRANSIT_TYPES:
        return None
    start_iata = (draft.start_location or {}).get("iata")
    end_iata = (draft.end_location or {}).get("iata")
    if not (start_iata and end_iata):
        return None
    window = timedelta(minutes=_MEDIUM_TIME_WINDOW_MIN)
    stmt = (
        select(Segment)
        .where(
            Segment.owner_user_id == owner_user_id,
            Segment.status != "cancelled",
            Segment.type == draft.type,
            Segment.start_at.between(draft.start_at - window, draft.start_at + window),
            Segment.start_location["iata"].astext == start_iata,
            Segment.end_location["iata"].astext == end_iata,
        )
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _medium_lodging_match(
    db: AsyncSession,
    owner_user_id: uuid.UUID,
    draft: SegmentDraft,
) -> Segment | None:
    if draft.type != "lodging":
        return None
    name = (draft.start_location or {}).get("name")
    if not name:
        return None
    stmt = (
        select(Segment)
        .where(
            Segment.owner_user_id == owner_user_id,
            Segment.status != "cancelled",
            Segment.type == "lodging",
            func.date(Segment.start_at) == draft.start_at.date(),
            func.lower(Segment.start_location["name"].astext) == name.strip().lower(),
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
    if hit := await _medium_transit_match(db, owner_user_id, draft):
        return hit
    if hit := await _medium_lodging_match(db, owner_user_id, draft):
        return hit
    return None
