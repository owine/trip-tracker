"""freeze_fx + recompute_home_minor: pure money math. Spec §4.4."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

from trip_tracker.expenses.currencies import minor_digits
from trip_tracker.expenses.fx import get_rate


class _RedisLike(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> object: ...


def recompute_home_minor(amount_minor: int, native: str, home: str, fx_rate: Decimal) -> int:
    """Recompute the home-currency minor-unit equivalent for a known fx_rate.

    Used on the edit path when amount_minor changes but currency does not — we
    keep the original frozen fx_rate and only recompute the home equivalent.
    """
    home_d = minor_digits(home)
    native_d = minor_digits(native)
    factor = Decimal(10) ** (home_d - native_d)
    raw = Decimal(amount_minor) * fx_rate * factor
    return int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


async def freeze_fx(
    amount_minor: int, native: str, home: str, redis: _RedisLike
) -> tuple[Decimal, int]:
    """Return (fx_rate, amount_home_minor) frozen at entry time. Spec §4.4.

    base == target → returns (Decimal(1), amount_minor) — no FX call.
    """
    if native == home:
        return Decimal(1), amount_minor
    fx_rate = await get_rate(native, home, redis)
    home_minor = recompute_home_minor(amount_minor, native, home, fx_rate)
    return fx_rate, home_minor
