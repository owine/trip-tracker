"""freeze_fx + recompute_home_minor: pure money math. Spec §4.4."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from trip_tracker.expenses.freeze import freeze_fx, recompute_home_minor


@pytest.mark.parametrize(
    ("amount_minor", "native", "home", "rate", "expected_home_minor"),
    [
        # USD→USD same currency: rate=1, no FX call.
        (3800, "USD", "USD", Decimal(1), 3800),
        # EUR (2 decimals) → USD (2): 38.00 * 1.07 = 40.66 → 4066
        (3800, "EUR", "USD", Decimal("1.07"), 4066),
        # JPY (0 decimals) → USD (2): 5000 yen * 0.0064 = $32.00
        # 5000 * 0.0064 * 10**(2-0) = 5000 * 0.0064 * 100 = 3200
        (5000, "JPY", "USD", Decimal("0.0064"), 3200),
        # USD (2) → JPY (0): $38.00 * 156.4 = ¥5943.20 → 5943 (HALF_UP)
        # 3800 * 156.4 * 10**(0-2) = 3800 * 156.4 / 100 = 5943.2 → 5943
        (3800, "USD", "JPY", Decimal("156.4"), 5943),
        # BHD (3) → USD (2): 1.234 BHD * 2.65 ≈ 3.27 USD
        # 1234 * 2.65 * 10**(2-3) = 1234 * 2.65 / 10 = 326.99 → 327
        (1234, "BHD", "USD", Decimal("2.65"), 327),
    ],
)
@pytest.mark.asyncio
async def test_freeze_fx_table(
    amount_minor: int, native: str, home: str, rate: Decimal, expected_home_minor: int
) -> None:
    fake_redis = AsyncMock()
    with patch("trip_tracker.expenses.freeze.get_rate", AsyncMock(return_value=rate)):
        fx_rate, home_minor = await freeze_fx(amount_minor, native, home, fake_redis)
    if native == home:
        assert fx_rate == Decimal(1)
    else:
        assert fx_rate == rate
    assert home_minor == expected_home_minor


@pytest.mark.asyncio
async def test_freeze_fx_same_currency_no_io() -> None:
    """Same currency must NOT call get_rate at all."""
    fake_redis = AsyncMock()
    with patch("trip_tracker.expenses.freeze.get_rate", AsyncMock()) as mocked:
        fx_rate, home_minor = await freeze_fx(3800, "USD", "USD", fake_redis)
    mocked.assert_not_awaited()
    assert fx_rate == Decimal(1)
    assert home_minor == 3800


def test_recompute_home_minor_pure() -> None:
    """recompute_home_minor is sync, takes a known fx_rate (edit-path)."""
    # 38.00 EUR @ 1.07 → 40.66 USD
    assert recompute_home_minor(3800, "EUR", "USD", Decimal("1.07")) == 4066
    # 5000 JPY @ 0.0064 → 32.00 USD
    assert recompute_home_minor(5000, "JPY", "USD", Decimal("0.0064")) == 3200


def test_recompute_home_minor_round_half_up() -> None:
    # 100 minor * 0.005 = 0.5 → 1 with HALF_UP
    assert recompute_home_minor(100, "USD", "USD", Decimal("0.005")) == 1
