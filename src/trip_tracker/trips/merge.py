"""Single-transaction merge of source trip into target trip.

Builds the merge_audit JSONB so undo (C2) can be lossless. See spec §3.3.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from datetime import date as _date
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
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

    # 4. Trip travelers: INSERT...SELECT...ON CONFLICT DO NOTHING per spec §3.3.
    # The Python diff above (`added_traveler_user_ids`) drives the audit; the
    # ON CONFLICT clause is the race-safety net for concurrent merges to the
    # same target — without it, two near-simultaneous merges would hit a
    # composite-PK IntegrityError on overlapping users.
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
        if added_rows:
            await db.execute(
                pg_insert(TripTraveler)
                .values(
                    [
                        {"trip_id": target.id, "user_id": r.user_id, "role": r.role}
                        for r in added_rows
                    ]
                )
                .on_conflict_do_nothing(index_elements=["trip_id", "user_id"])
            )
    await db.execute(delete(TripTraveler).where(TripTraveler.trip_id == source.id))

    # 5. Widen target dates (capture pre-merge dates for audit-driven undo)
    target_start_pre = target.start_date
    target_end_pre = target.end_date
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
        "target_start_date_pre_merge": target_start_pre.isoformat(),
        "target_end_date_pre_merge": target_end_pre.isoformat(),
        "schema_version": _AUDIT_SCHEMA_VERSION,
    }
    source.merged_into_id = target.id
    source.merged_at = datetime.now(UTC)
    source.merge_audit = audit

    await db.flush()  # ensure all changes are visible to subsequent reads
    return audit


async def undo_merge_trip(
    db: AsyncSession,
    source: Trip,
    target: Trip,
) -> None:
    """Reverse a previous merge using source.merge_audit. Idempotent.

    Caller is responsible for ``await db.commit()``. Caller MUST verify:
    - source.merged_into_id == target.id (the merge actually happened)
    - now() - source.merged_at <= 7 days
    - target.merged_into_id IS NULL (target itself isn't merged)
    """
    audit = source.merge_audit or {}
    source_segment_ids = [uuid.UUID(s) for s in audit.get("source_segment_ids", [])]
    source_expense_ids = [uuid.UUID(s) for s in audit.get("source_expense_ids", [])]
    source_document_ids = [uuid.UUID(s) for s in audit.get("source_document_ids", [])]
    added_traveler_user_ids = [uuid.UUID(s) for s in audit.get("added_traveler_user_ids", [])]
    target_start_pre = _date.fromisoformat(audit["target_start_date_pre_merge"])
    target_end_pre = _date.fromisoformat(audit["target_end_date_pre_merge"])

    # 1. Reverse FK reassignment. Idempotent: rows already moved back are
    #    skipped by the ``AND trip_id = target.id`` guard.
    if source_segment_ids:
        await db.execute(
            update(Segment)
            .where(Segment.id.in_(source_segment_ids), Segment.trip_id == target.id)
            .values(trip_id=source.id)
        )
    if source_expense_ids:
        await db.execute(
            update(Expense)
            .where(Expense.id.in_(source_expense_ids), Expense.trip_id == target.id)
            .values(trip_id=source.id)
        )
    if source_document_ids:
        await db.execute(
            update(Document)
            .where(Document.id.in_(source_document_ids), Document.trip_id == target.id)
            .values(trip_id=source.id)
        )

    # 2. Remove target rows added by the merge, capturing their roles for source restore.
    restored_rows: list[tuple[uuid.UUID, str]] = []
    if added_traveler_user_ids:
        target_added_rows = (
            (
                await db.execute(
                    select(TripTraveler).where(
                        TripTraveler.trip_id == target.id,
                        TripTraveler.user_id.in_(added_traveler_user_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        restored_rows = [(r.user_id, r.role) for r in target_added_rows]
        await db.execute(
            delete(TripTraveler).where(
                TripTraveler.trip_id == target.id,
                TripTraveler.user_id.in_(added_traveler_user_ids),
            )
        )

    # 3. Restore source's trip_traveler rows. The creator is always present
    #    (set by Trip creation). Re-add rows we just removed from target,
    #    plus the creator if not already covered.
    creator_id = source.created_by
    creator_in_added = any(uid == creator_id for uid, _ in restored_rows)
    if not creator_in_added:
        # Creator was either in S∩T (still on target, untouched) or genuinely
        # missing — defensively re-add with role="owner" (Trip-creation default).
        await db.execute(
            pg_insert(TripTraveler)
            .values({"trip_id": source.id, "user_id": creator_id, "role": "owner"})
            .on_conflict_do_nothing(index_elements=["trip_id", "user_id"])
        )
    if restored_rows:
        await db.execute(
            pg_insert(TripTraveler)
            .values(
                [
                    {"trip_id": source.id, "user_id": uid, "role": role}
                    for uid, role in restored_rows
                ]
            )
            .on_conflict_do_nothing(index_elements=["trip_id", "user_id"])
        )

    # 4. Restore target's pre-merge dates (audit-driven, lossless).
    target.start_date = target_start_pre
    target.end_date = target_end_pre
    target.updated_at = datetime.now(UTC)

    # 5. Lift source soft-delete.
    source.merged_into_id = None
    source.merged_at = None
    source.merge_audit = None

    await db.flush()
