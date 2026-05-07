"""Trips routes: list and detail. Trip identity is managed by TripIt (Phase 13)."""

from __future__ import annotations

import logging
import uuid
import zoneinfo
from datetime import date, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response  # Response kept for trip_detail return type
from fastapi.templating import Jinja2Templates
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import (
    get_settings,
    require_traveler,
    require_user,
)
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.models.trip import Trip
from trip_tracker.models.user import User
from trip_tracker.templating import register_globals

router = APIRouter(prefix="/trips", tags=["trips"])
logger = logging.getLogger(__name__)


async def _redis(settings: Settings = Depends(get_settings)) -> AsyncRedis:  # noqa: B008
    return AsyncRedis.from_url(str(settings.redis_url))


_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


def _localize_dt(dt: datetime, tz: str, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render a UTC-stored datetime in the given IANA timezone."""
    return dt.astimezone(zoneinfo.ZoneInfo(tz)).strftime(fmt)


templates.env.filters["localize_dt"] = _localize_dt


@router.get("", response_class=HTMLResponse)
async def list_trips(
    request: Request,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    today = date.today()
    is_past = case((Trip.end_date < today, 1), else_=0)
    stmt = select(Trip).order_by(
        is_past.asc(),
        case((Trip.end_date >= today, Trip.start_date), else_=None).asc(),
        case((Trip.end_date < today, Trip.start_date), else_=None).desc(),
    )
    trips = (await db.execute(stmt)).scalars().all()
    return templates.TemplateResponse(request, "trips/list.html", {"trips": trips, "user": user})


@router.get("/{trip_id}", response_class=HTMLResponse, response_model=None)
async def trip_detail(
    request: Request,
    trip: Trip = Depends(require_traveler),  # noqa: B008
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    redis: AsyncRedis = Depends(_redis),  # noqa: B008
) -> HTMLResponse | Response:
    from collections import defaultdict
    from datetime import date as _date_cls

    from trip_tracker.expenses.categories import CATEGORY_LABELS, Category
    from trip_tracker.expenses.currencies import minor_digits
    from trip_tracker.expenses.freeze import recompute_home_minor
    from trip_tracker.expenses.fx import FxError, get_rate
    from trip_tracker.models.document import Document
    from trip_tracker.models.expense import Expense
    from trip_tracker.models.segment import Segment
    from trip_tracker.trips.consolidation import (
        ConsolidationTarget,
        consolidation_candidates,
    )

    segments = (
        (
            await db.execute(
                select(Segment).where(Segment.trip_id == trip.id).order_by(Segment.start_at)
            )
        )
        .scalars()
        .all()
    )

    # Documents already linked to a segment, grouped by segment_id for inline display.
    docs = (
        (
            await db.execute(
                select(Document).where(
                    Document.trip_id == trip.id, Document.segment_id.is_not(None)
                )
            )
        )
        .scalars()
        .all()
    )
    documents_by_segment: dict[uuid.UUID, list[Document]] = defaultdict(list)
    for d in docs:
        # SQL `is_not(None)` filter above guarantees segment_id is set; the
        # assert narrows Optional[UUID] for mypy at the dict-build step.
        assert d.segment_id is not None  # nosec B101
        documents_by_segment[d.segment_id].append(d)

    expenses = (
        (
            await db.execute(
                select(Expense)
                .where(Expense.trip_id == trip.id)
                .order_by(Expense.incurred_on.desc(), Expense.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    home_currency = user.home_currency
    total_paid_home = sum(e.amount_home_minor for e in expenses if e.status == "paid")
    total_expected_home = sum(e.amount_home_minor for e in expenses)
    by_category: dict[str, int] = defaultdict(int)
    for e in expenses:
        if e.status == "paid":
            by_category[e.category] += e.amount_home_minor

    # Consolidation candidates — suggest merging with nearby trips.
    _target = ConsolidationTarget.from_trip(trip, list(segments))
    candidates = await consolidation_candidates(db, user, _target)

    # Saved-by-points rollup with FxError swallow.
    total_saved_home: int | None = 0
    try:
        for s in segments:
            award = (s.details or {}).get("award")
            if not award or award.get("cash_equivalent_minor") is None:
                continue
            eq_rate = await get_rate(
                award["cash_equivalent_currency"],
                home_currency,
                redis,  # type: ignore[arg-type]
            )
            cp_rate = await get_rate(
                award["cash_copay_currency"],
                home_currency,
                redis,  # type: ignore[arg-type]
            )
            eq_home = recompute_home_minor(
                award["cash_equivalent_minor"],
                award["cash_equivalent_currency"],
                home_currency,
                eq_rate,
            )
            cp_home = recompute_home_minor(
                award["cash_copay_minor"],
                award["cash_copay_currency"],
                home_currency,
                cp_rate,
            )
            total_saved_home += eq_home - cp_home  # type: ignore[operator]
    except FxError:
        logger.info("FX unavailable; saved-by-points rollup hidden for trip %s", trip.id)
        total_saved_home = None

    return templates.TemplateResponse(
        request,
        "trips/detail.html",
        {
            "trip": trip,
            "segments": segments,
            "documents_by_segment": dict(documents_by_segment),
            "user": user,
            "expenses": expenses,
            "total_paid_home": total_paid_home,
            "total_expected_home": total_expected_home,
            "by_category": dict(by_category),
            "total_saved_home": total_saved_home,
            "home_currency": home_currency,
            "category_labels": CATEGORY_LABELS,
            "Category": Category,
            "minor_digits": minor_digits,
            "today": _date_cls.today(),
            "consolidation_candidates": candidates,
        },
    )
