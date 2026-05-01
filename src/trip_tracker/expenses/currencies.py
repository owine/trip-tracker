"""ISO 4217 minor-unit digit lookup. Spec §4.3."""

from __future__ import annotations

CURRENCY_MINOR: dict[str, int] = {
    "JPY": 0,
    "KRW": 0,
    "VND": 0,
    "CLP": 0,
    "ISK": 0,
    "BHD": 3,
    "JOD": 3,
    "KWD": 3,
    "OMR": 3,
    "TND": 3,
}


def minor_digits(code: str) -> int:
    """Return the number of fractional digits for an ISO 4217 currency.
    Defaults to 2 for any code not in the lookup table."""
    return CURRENCY_MINOR.get(code, 2)
