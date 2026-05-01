import pytest
from pydantic import ValidationError

from trip_tracker.expenses.awards import AwardDetails, k_format, program_short


@pytest.mark.parametrize(
    ("inp", "exp"),
    [
        (75000, "75k"),
        (1500, "1.5k"),
        (1000, "1k"),
        (100, "100"),
        (999, "999"),
        (12345, "12.3k"),
    ],
)
def test_k_format(inp: int, exp: str) -> None:
    assert k_format(inp) == exp


def test_program_short_known() -> None:
    assert program_short("Chase Ultimate Rewards") == "Chase UR"
    assert program_short("Amex Membership Rewards") == "Amex MR"


def test_program_short_unknown_passthrough() -> None:
    assert program_short("Random Loyalty Program") == "Random Loyalty Program"


def test_award_details_valid() -> None:
    a = AwardDetails(
        program="Chase Ultimate Rewards",
        points_spent=75000,
        cash_copay_minor=560,
        cash_copay_currency="usd",
    )
    assert a.cash_copay_currency == "USD"  # upper validator


def test_award_details_zero_points_rejected() -> None:
    with pytest.raises(ValidationError):
        AwardDetails(program="X", points_spent=0, cash_copay_minor=0, cash_copay_currency="USD")


def test_award_details_negative_copay_rejected() -> None:
    with pytest.raises(ValidationError):
        AwardDetails(program="X", points_spent=1, cash_copay_minor=-1, cash_copay_currency="USD")


def test_award_details_equivalent_pair_both_required() -> None:
    """If cash_equivalent_minor is set, cash_equivalent_currency must be too."""
    with pytest.raises(ValidationError):
        AwardDetails(
            program="X",
            points_spent=1,
            cash_copay_minor=0,
            cash_copay_currency="USD",
            cash_equivalent_minor=100,
            cash_equivalent_currency=None,
        )


def test_award_details_equivalent_pair_both_none_ok() -> None:
    """Both None is fine — optional feature."""
    a = AwardDetails(
        program="X",
        points_spent=1,
        cash_copay_minor=0,
        cash_copay_currency="USD",
        cash_equivalent_minor=None,
        cash_equivalent_currency=None,
    )
    assert a.cash_equivalent_minor is None
    assert a.cash_equivalent_currency is None
