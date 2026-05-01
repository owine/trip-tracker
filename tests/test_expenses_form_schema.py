"""ExpenseForm Pydantic schema — empty-string coercion regression tests.

Browsers submit unfilled optional fields as `name=` (empty string), not as
omitted fields. Pydantic's int/date validators reject "", so the schema
pre-coerces those to None. Caught in v0.8.0 smoke when the new-expense form
re-rendered silently instead of redirecting on submit with empty optional fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from trip_tracker.schemas.expense_forms import ExpenseForm


def _base_payload() -> dict:
    return {
        "amount_minor": 3800,
        "currency": "EUR",
        "category": "food",
        "incurred_on": "2026-06-01",
        "status": "paid",
        "home_currency_at_load": "USD",
    }


def test_empty_strings_in_optional_int_and_date_fields_coerce_to_none() -> None:
    """Browsers send `deposit_minor=&cancellation_deadline=&cancellation_fee_minor=`
    when the cancellation-policy section is unfilled. Schema must accept this."""
    form = ExpenseForm.model_validate(
        {
            **_base_payload(),
            "notes": "",
            "deposit_minor": "",
            "cancellation_deadline": "",
            "cancellation_fee_minor": "",
            "segment_id": "",
            "document_id": "",
        }
    )
    assert form.notes is None
    assert form.deposit_minor is None
    assert form.cancellation_deadline is None
    assert form.cancellation_fee_minor is None
    assert form.segment_id is None
    assert form.document_id is None


def test_real_values_still_validate() -> None:
    form = ExpenseForm.model_validate(
        {
            **_base_payload(),
            "deposit_minor": "1000",
            "cancellation_deadline": "2026-05-25",
            "cancellation_fee_minor": "500",
        }
    )
    assert form.deposit_minor == 1000
    assert form.cancellation_fee_minor == 500


def test_invalid_int_still_rejected() -> None:
    """Coercion only handles empty string, not arbitrary garbage."""
    with pytest.raises(ValidationError):
        ExpenseForm.model_validate({**_base_payload(), "deposit_minor": "not-a-number"})
