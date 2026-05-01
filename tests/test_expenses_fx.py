"""Frankfurter FX client + Redis cache + get_rate."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from trip_tracker.expenses.fx import (
    FxError,
    fetch_rates,
    get_cached_rates,
    get_rate,
    set_cached_rates,
)


class FakeRedis:
    """Minimal in-memory Redis stand-in covering get/set with ex=...

    Hand-rolled to avoid the `fakeredis` dep — Phase 8's FX surface area is
    small (get/set with TTL).
    """

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> None:
        self.store[key] = value.encode() if isinstance(value, str) else value


@pytest.mark.asyncio
async def test_get_rate_same_currency_returns_one() -> None:
    fake = FakeRedis()
    rate = await get_rate("USD", "USD", fake)  # type: ignore[arg-type]
    assert rate == Decimal(1)
    assert fake.store == {}  # no I/O


@pytest.mark.asyncio
async def test_set_get_cached_rates_roundtrip() -> None:
    fake = FakeRedis()
    rates = {"EUR": Decimal("0.93"), "JPY": Decimal("156.4")}
    await set_cached_rates("USD", rates, fake)  # type: ignore[arg-type]
    got = await get_cached_rates("USD", fake)  # type: ignore[arg-type]
    assert got == rates
    assert isinstance(got["EUR"], Decimal)


@pytest.mark.asyncio
async def test_get_rate_cache_miss_calls_fetch() -> None:
    fake = FakeRedis()
    payload = {"EUR": Decimal("0.93"), "GBP": Decimal("0.79")}
    with patch("trip_tracker.expenses.fx.fetch_rates", AsyncMock(return_value=payload)) as m:
        rate = await get_rate("USD", "EUR", fake)  # type: ignore[arg-type]
    m.assert_awaited_once_with("USD")
    assert rate == Decimal("0.93")
    # Cache populated for next call
    assert any(k.startswith("fx:USD:") for k in fake.store)


@pytest.mark.asyncio
async def test_get_rate_cache_hit_no_fetch() -> None:
    fake = FakeRedis()
    await set_cached_rates("USD", {"EUR": Decimal("0.93")}, fake)  # type: ignore[arg-type]
    with patch("trip_tracker.expenses.fx.fetch_rates", AsyncMock()) as m:
        rate = await get_rate("USD", "EUR", fake)  # type: ignore[arg-type]
    m.assert_not_awaited()
    assert rate == Decimal("0.93")


@pytest.mark.asyncio
async def test_get_rate_missing_target_raises() -> None:
    fake = FakeRedis()
    with (
        patch(
            "trip_tracker.expenses.fx.fetch_rates", AsyncMock(return_value={"EUR": Decimal("0.93")})
        ),
        pytest.raises(FxError),
    ):
        await get_rate("USD", "ZZZ", fake)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fetch_rates_parses_decimal_from_string() -> None:
    """Rates parsed via parse_float=Decimal so we never round-trip via float."""
    body = json.dumps(
        {"base": "USD", "date": "2026-05-01", "rates": {"EUR": 0.9300000123, "JPY": 156.4}}
    )

    class FakeResp:
        status_code = 200
        text = body

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def get(self, url: str, params: dict | None = None) -> FakeResp:
            return FakeResp()

    with patch("trip_tracker.expenses.fx.httpx.AsyncClient", return_value=FakeClient()):
        rates = await fetch_rates("USD")
    assert isinstance(rates["EUR"], Decimal)
    # Critical: precision preserved
    assert str(rates["EUR"]) == "0.9300000123"


@pytest.mark.asyncio
async def test_fetch_rates_5xx_raises_fxerror() -> None:
    import httpx as _httpx

    class FakeResp:
        def raise_for_status(self) -> None:
            raise _httpx.HTTPStatusError(
                "503", request=_httpx.Request("GET", "http://x"), response=_httpx.Response(503)
            )

    class FakeClient:
        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *a: object) -> None:
            return None

        async def get(self, url: str, params: dict | None = None) -> FakeResp:
            return FakeResp()

    with (
        patch("trip_tracker.expenses.fx.httpx.AsyncClient", return_value=FakeClient()),
        pytest.raises(FxError),
    ):
        await fetch_rates("USD")
