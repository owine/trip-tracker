"""Expense CRUD routes. Spec §6.1."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from redis.asyncio import Redis as AsyncRedis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trip_tracker.auth.deps import get_settings, require_user
from trip_tracker.config import Settings
from trip_tracker.db import get_session
from trip_tracker.expenses.categories import CATEGORY_LABELS
from trip_tracker.expenses.freeze import freeze_fx, recompute_home_minor
from trip_tracker.expenses.fx import FxError
from trip_tracker.models.expense import Expense
from trip_tracker.models.trip_traveler import TripTraveler
from trip_tracker.models.user import User
from trip_tracker.schemas.expense_forms import ExpenseForm
from trip_tracker.templating import register_globals

router = APIRouter(tags=["expenses"])
logger = logging.getLogger(__name__)

_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"
templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
register_globals(templates)


async def _redis(settings: Settings = Depends(get_settings)) -> AsyncRedis:  # noqa: B008
    return AsyncRedis.from_url(str(settings.redis_url))


async def _user_can_access_trip(db: AsyncSession, user: User, trip_id: uuid.UUID) -> bool:
    res = await db.execute(
        select(TripTraveler.user_id).where(
            TripTraveler.trip_id == trip_id, TripTraveler.user_id == user.id
        )
    )
    return res.scalar_one_or_none() is not None


@router.get("/trips/{trip_id}/expenses/new", response_class=HTMLResponse)
async def new_expense_form(
    request: Request,
    trip_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    return _render_form(request, user, trip_id, values={}, errors={})


@router.post("/trips/{trip_id}/expenses")
async def create_expense(
    request: Request,
    trip_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    redis: AsyncRedis = Depends(_redis),  # noqa: B008
) -> Response:
    if not await _user_can_access_trip(db, user, trip_id):
        raise HTTPException(404)
    form_data = dict(await request.form())
    try:
        form = ExpenseForm(**form_data)
    except ValidationError as exc:
        return _render_form(request, user, trip_id, form_data, errors=_pydantic_errors(exc))

    if form.home_currency_at_load != user.home_currency:
        return _render_form(
            request,
            user,
            trip_id,
            form_data,
            errors={"_form": "Your home currency changed in another tab — review and resubmit."},
        )

    try:
        fx_rate, home_minor = await freeze_fx(
            form.amount_minor,
            form.currency,
            user.home_currency,
            redis,  # type: ignore[arg-type]
        )
    except FxError as exc:
        logger.warning("FX unavailable on create: %s", exc)
        return _render_form(
            request,
            user,
            trip_id,
            form_data,
            errors={"_form": "Currency rates unavailable. Try again in a few minutes."},
        )

    exp = Expense(
        trip_id=trip_id,
        owner_user_id=user.id,
        amount_minor=form.amount_minor,
        currency=form.currency,
        fx_rate=fx_rate,
        amount_home_minor=home_minor,
        home_currency=user.home_currency,
        category=form.category.value,
        notes=form.notes,
        incurred_on=form.incurred_on,
        status=form.status,
        segment_id=uuid.UUID(form.segment_id) if form.segment_id else None,
        document_id=uuid.UUID(form.document_id) if form.document_id else None,
        deposit_minor=form.deposit_minor,
        cancellation_deadline=form.cancellation_deadline,
        cancellation_fee_minor=form.cancellation_fee_minor,
    )
    db.add(exp)
    await db.commit()
    if "session" in request.scope:
        request.session["flash"] = {
            "kind": "expense_saved",
            "amount_minor": form.amount_minor,
            "currency": form.currency,
            "home_minor": home_minor,
            "home_currency": user.home_currency,
        }
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.get("/expenses/{expense_id}/edit", response_class=HTMLResponse)
async def edit_expense_form(
    request: Request,
    expense_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> HTMLResponse:
    exp = await db.get(Expense, expense_id)
    if exp is None or not (
        exp.owner_user_id == user.id or await _user_can_access_trip(db, user, exp.trip_id)
    ):
        raise HTTPException(404)
    return _render_form(
        request,
        user,
        exp.trip_id,
        values=_expense_to_form_values(exp),
        errors={},
        edit_id=expense_id,
    )


@router.post("/expenses/{expense_id}")
async def update_expense(
    request: Request,
    expense_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
    redis: AsyncRedis = Depends(_redis),  # noqa: B008
) -> Response:
    exp = await db.get(Expense, expense_id)
    if exp is None or not (
        exp.owner_user_id == user.id or await _user_can_access_trip(db, user, exp.trip_id)
    ):
        raise HTTPException(404)

    form_data = dict(await request.form())
    try:
        form = ExpenseForm(**form_data)
    except ValidationError as exc:
        return _render_form(
            request,
            user,
            exp.trip_id,
            form_data,
            errors=_pydantic_errors(exc),
            edit_id=expense_id,
        )

    if form.home_currency_at_load != user.home_currency:
        return _render_form(
            request,
            user,
            exp.trip_id,
            form_data,
            errors={"_form": "Your home currency changed in another tab — review and resubmit."},
            edit_id=expense_id,
        )

    # === Edit-path recompute rule (Spec §4.4) ===
    currency_changed = form.currency != exp.currency
    amount_changed = form.amount_minor != exp.amount_minor

    if currency_changed:
        try:
            fx_rate, home_minor = await freeze_fx(
                form.amount_minor,
                form.currency,
                user.home_currency,
                redis,  # type: ignore[arg-type]
            )
        except FxError:
            return _render_form(
                request,
                user,
                exp.trip_id,
                form_data,
                errors={"_form": "Currency rates unavailable. Try again in a few minutes."},
                edit_id=expense_id,
            )
        exp.fx_rate = fx_rate
        exp.amount_home_minor = home_minor
        exp.home_currency = user.home_currency
    elif amount_changed:
        exp.amount_home_minor = recompute_home_minor(
            form.amount_minor, exp.currency, exp.home_currency, exp.fx_rate
        )
    # else: neither changed → leave fx_rate / amount_home_minor untouched

    exp.amount_minor = form.amount_minor
    exp.currency = form.currency
    exp.category = form.category.value
    exp.notes = form.notes
    exp.incurred_on = form.incurred_on
    exp.status = form.status
    exp.segment_id = uuid.UUID(form.segment_id) if form.segment_id else None
    exp.document_id = uuid.UUID(form.document_id) if form.document_id else None
    exp.deposit_minor = form.deposit_minor
    exp.cancellation_deadline = form.cancellation_deadline
    exp.cancellation_fee_minor = form.cancellation_fee_minor

    await db.commit()
    return RedirectResponse(f"/trips/{exp.trip_id}", status_code=303)


@router.post("/expenses/{expense_id}/delete")
async def delete_expense(
    expense_id: uuid.UUID,
    user: User = Depends(require_user),  # noqa: B008
    db: AsyncSession = Depends(get_session),  # noqa: B008
) -> Response:
    exp = await db.get(Expense, expense_id)
    if exp is None or not (
        exp.owner_user_id == user.id or await _user_can_access_trip(db, user, exp.trip_id)
    ):
        raise HTTPException(404)
    trip_id = exp.trip_id
    await db.delete(exp)
    await db.commit()
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


def _render_form(
    request: Request,
    user: User,
    trip_id: uuid.UUID,
    values: dict[str, Any],
    *,
    errors: dict[str, str],
    edit_id: uuid.UUID | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "expenses/form.html",
        {
            "user": user,
            "trip_id": trip_id,
            "values": values,
            "errors": errors,
            "edit_id": edit_id,
            "category_labels": CATEGORY_LABELS,
            "home_currency": user.home_currency,
        },
    )


def _pydantic_errors(exc: ValidationError) -> dict[str, str]:
    return {".".join(str(p) for p in e["loc"]): e["msg"] for e in exc.errors()}


def _expense_to_form_values(e: Expense) -> dict[str, Any]:
    return {
        "amount_minor": e.amount_minor,
        "currency": e.currency,
        "category": e.category,
        "notes": e.notes or "",
        "incurred_on": e.incurred_on.isoformat(),
        "status": e.status,
        "segment_id": str(e.segment_id) if e.segment_id else "",
        "document_id": str(e.document_id) if e.document_id else "",
        "deposit_minor": e.deposit_minor or "",
        "cancellation_deadline": e.cancellation_deadline.isoformat()
        if e.cancellation_deadline
        else "",
        "cancellation_fee_minor": e.cancellation_fee_minor or "",
    }
