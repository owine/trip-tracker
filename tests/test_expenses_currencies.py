"""ISO 4217 minor-unit digit lookup."""

from __future__ import annotations

from trip_tracker.expenses.currencies import CURRENCY_MINOR, minor_digits


def test_zero_decimal_currencies() -> None:
    for code in ("JPY", "KRW", "VND", "CLP", "ISK"):
        assert minor_digits(code) == 0


def test_three_decimal_currencies() -> None:
    for code in ("BHD", "JOD", "KWD", "OMR", "TND"):
        assert minor_digits(code) == 3


def test_default_two_decimals() -> None:
    assert minor_digits("USD") == 2
    assert minor_digits("EUR") == 2
    assert minor_digits("XYZ") == 2  # unknown defaults to 2


def test_currency_minor_table_shape() -> None:
    """The exported lookup is a dict[str, int]."""
    assert isinstance(CURRENCY_MINOR, dict)
    for k, v in CURRENCY_MINOR.items():
        assert isinstance(k, str)
        assert len(k) == 3
        assert isinstance(v, int)
        assert v in (0, 3)
