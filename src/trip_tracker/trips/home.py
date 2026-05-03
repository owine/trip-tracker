"""Auto-infer the user's 'home' from segment endpoint frequency.

Used by the consolidation candidate scorer to decide if a trip is 'open'
(no return-to-home segment yet) and whether a new segment is a closing leg.

No persisted column — recomputed per query. Cheap given the partial index.
"""

from __future__ import annotations

import uuid
from collections import Counter

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.models.segment import Segment

_LAST_N = 20
_DOMINANCE_FLOOR = 0.30  # 30 % — see spec §3.2


async def infer_home(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Top endpoint city across the user's last N confirmed segments,
    if its share ≥ 30 % of total endpoint observations. Else None.
    """
    stmt = (
        select(Segment.start_location, Segment.end_location)
        .where(
            Segment.owner_user_id == user_id,
            Segment.status == "confirmed",
        )
        .order_by(Segment.start_at.desc())
        .limit(_LAST_N)
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return None

    counter: Counter[str] = Counter()
    total = 0
    for start_loc, end_loc in rows:
        for loc in (start_loc, end_loc):
            city = (loc or {}).get("city")
            if city:
                counter[city] += 1
                total += 1

    if total == 0:
        return None
    top_city, top_count = counter.most_common(1)[0]
    if (top_count / total) >= _DOMINANCE_FLOOR:
        return top_city
    return None
