"""Single-transaction merge of source trip into target trip.

Builds the merge_audit JSONB so undo (C2) can be lossless. See spec §3.3.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.document import Document
from trip_tracker.models.expense import Expense
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler

_AUDIT_SCHEMA_VERSION = 1


async def merge_trip_into(
    db: AsyncSession,
    source: Trip,
    target: Trip,
) -> dict[str, Any]:
    """Reassign FKs source→target, populate merge_audit, soft-delete source.

    Returns the audit payload (caller may flash to UI). Does NOT commit;
    the caller is responsible for ``await db.commit()``.
    """
    # 1. Capture moved IDs for the audit (BEFORE the FK reassignment)
    moved_segment_ids = list(
        (await db.execute(select(Segment.id).where(Segment.trip_id == source.id))).scalars().all()
    )
    moved_expense_ids = list(
        (await db.execute(select(Expense.id).where(Expense.trip_id == source.id))).scalars().all()
    )
    moved_document_ids = list(
        (await db.execute(select(Document.id).where(Document.trip_id == source.id))).scalars().all()
    )

    # 2. trip_traveler diff: user_ids on source NOT already on target
    source_users = set(
        (await db.execute(select(TripTraveler.user_id).where(TripTraveler.trip_id == source.id)))
        .scalars()
        .all()
    )
    target_users = set(
        (await db.execute(select(TripTraveler.user_id).where(TripTraveler.trip_id == target.id)))
        .scalars()
        .all()
    )
    added_traveler_user_ids = sorted(source_users - target_users, key=str)

    # 3. Reassign FKs (Segments / Expenses / Documents)
    await db.execute(update(Segment).where(Segment.trip_id == source.id).values(trip_id=target.id))
    await db.execute(update(Expense).where(Expense.trip_id == source.id).values(trip_id=target.id))
    await db.execute(
        update(Document).where(Document.trip_id == source.id).values(trip_id=target.id)
    )

    # 4. Trip travelers: insert added rows on target, then delete all source rows.
    if added_traveler_user_ids:
        added_rows = (
            (
                await db.execute(
                    select(TripTraveler).where(
                        TripTraveler.trip_id == source.id,
                        TripTraveler.user_id.in_(added_traveler_user_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for r in added_rows:
            db.add(TripTraveler(trip_id=target.id, user_id=r.user_id, role=r.role))
    await db.execute(delete(TripTraveler).where(TripTraveler.trip_id == source.id))

    # 5. Widen target dates
    target.start_date = min(target.start_date, source.start_date)
    target.end_date = max(target.end_date, source.end_date)
    target.updated_at = datetime.now(UTC)

    # 6. Soft-delete source + populate audit
    audit: dict[str, Any] = {
        "source_segment_ids": [str(i) for i in moved_segment_ids],
        "source_expense_ids": [str(i) for i in moved_expense_ids],
        "source_document_ids": [str(i) for i in moved_document_ids],
        "added_traveler_user_ids": [str(u) for u in added_traveler_user_ids],
        "source_start_date": source.start_date.isoformat(),
        "source_end_date": source.end_date.isoformat(),
        "schema_version": _AUDIT_SCHEMA_VERSION,
    }
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC)
    source.merge_audit = audit

    await db.flush()  # ensure all changes are visible to subsequent reads
    return audit
