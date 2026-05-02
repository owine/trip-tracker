"""Seed a trip + flight segment for dev smoke. Idempotent.

Skips the segment-creation form (which is huge) so the smoke can focus on
Phase 8 (expenses) behavior. After running, the dev user has:
- 1 trip "Paris May 2026" 2026-06-01 → 2026-06-02
- 1 flight segment JFK → CDG (DL44 / Delta) on 2026-06-01

Usage:
    uv run python scripts/_dev_seed_trip.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from trip_tracker.config import Settings
from trip_tracker.models.segment import Segment
from trip_tracker.models.trip import Trip
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User


async def _seed() -> None:
    settings = Settings()
    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        user = (
            await db.execute(select(User).where(User.email == "dev@local"))
        ).scalar_one_or_none()
        if user is None:
            raise SystemExit("dev user not found — run scripts/_dev_session_cookie.py first")

        existing_trip = (
            await db.execute(
                select(Trip).where(Trip.created_by == user.id, Trip.title == "Paris May 2026")
            )
        ).scalar_one_or_none()
        if existing_trip is not None:
            print(f"trip already exists: id={existing_trip.id}")
            return

        trip = Trip(
            title="Paris May 2026",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 2),
            primary_destination="Paris, France",
            created_by=user.id,
        )
        db.add(trip)
        await db.flush()

        traveler = TripTraveler(trip_id=trip.id, user_id=user.id, role="owner")
        db.add(traveler)

        segment = Segment(
            trip_id=trip.id,
            owner_user_id=user.id,
            type="flight",
            status="confirmed",
            provider="Delta",
            confirmation_number="ABC123",
            start_at=datetime(2026, 6, 1, 13, 0, tzinfo=UTC),
            start_tz="America/New_York",
            end_at=datetime(2026, 6, 2, 2, 0, tzinfo=UTC),
            end_tz="Europe/Paris",
            start_location={"name": "New York", "iata": "JFK"},
            end_location={"name": "Paris", "iata": "CDG"},
            details={"flight_number": "DL44", "seat": "12A"},
            parse_source="manual",
            parse_confidence=1.0,
        )
        db.add(segment)
        await db.commit()

        print(f"seeded trip id={trip.id} segment id={segment.id}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(_seed())
