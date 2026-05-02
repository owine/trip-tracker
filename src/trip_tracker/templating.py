"""Shared Jinja env extensions. Call register_globals(templates) from every
route module's templates instance so filters/globals are available everywhere."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from trip_tracker.expenses.awards import k_format, program_short
from trip_tracker.expenses.categories import CATEGORY_LABELS, Category
from trip_tracker.expenses.currencies import minor_digits


def format_money(minor: int, code: str) -> str:
    """Format an ISO-4217 minor-unit amount as a major-unit string with the
    correct decimal precision for the currency. JPY/KRW have 0 decimals,
    BHD/KWD have 3, everything else 2."""
    digits = minor_digits(code)
    return f"{minor / (10**digits):.{digits}f}"


def register_globals(templates: Jinja2Templates) -> None:
    """Register common filters and globals on a Jinja2 environment."""
    templates.env.filters["k_format"] = k_format
    templates.env.filters["program_short"] = program_short
    templates.env.globals["minor_digits"] = minor_digits
    templates.env.globals["money"] = format_money
    templates.env.globals["Category"] = Category
    templates.env.globals["category_labels"] = CATEGORY_LABELS
