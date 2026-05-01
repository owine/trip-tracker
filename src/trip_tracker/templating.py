"""Shared Jinja env extensions. Call register_globals(templates) from every
route module's templates instance so filters/globals are available everywhere."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates

from trip_tracker.expenses.awards import k_format, program_short
from trip_tracker.expenses.categories import CATEGORY_LABELS, Category
from trip_tracker.expenses.currencies import minor_digits


def register_globals(templates: Jinja2Templates) -> None:
    """Register common filters and globals on a Jinja2 environment."""
    templates.env.filters["k_format"] = k_format
    templates.env.filters["program_short"] = program_short
    templates.env.globals["minor_digits"] = minor_digits
    templates.env.globals["Category"] = Category
    templates.env.globals["category_labels"] = CATEGORY_LABELS
