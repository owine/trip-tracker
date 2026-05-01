"""Redis cache for forecasts: round-trip + TTL key shape."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from trip_tracker.weather.cache import get_cached, set_cached
from trip_tracker.weather.client import DailyForecast, Forecast


def _f() -> Forecast:
    return Forecast(
        lat=48.86,
        lon=2.35,
        timezone="Europe/Paris",
        days=[
            DailyForecast(
                date=date(2026, 6, 1),
                temp_max_c=22.4,
                temp_min_c=13.5,
                weather_code=1,
                precip_prob=10,
            ),
        ],
    )


@pytest.mark.asyncio
async def test_set_then_get_round_trip() -> None:
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    fake_redis.set = AsyncMock()
    f = _f()
    await set_cached(f, fake_redis)
    fake_redis.set.assert_awaited_once()
    args, kwargs = fake_redis.set.call_args
    # Key shape: weather:<lat:.2f>:<lon:.2f>
    assert args[0] == "weather:48.86:2.35"
    # TTL set via `ex=` kwarg, 3600 seconds
    assert kwargs.get("ex") == 3600


@pytest.mark.asyncio
async def test_get_cached_returns_parsed_forecast() -> None:
    f = _f()
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=f.model_dump_json().encode("utf-8"))
    result = await get_cached(48.86, 2.35, fake_redis)
    assert result is not None
    assert result.lat == pytest.approx(48.86)
    assert result.days[0].temp_max_c == 22.4


@pytest.mark.asyncio
async def test_get_cached_returns_none_on_miss() -> None:
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    assert await get_cached(0, 0, fake_redis) is None


@pytest.mark.asyncio
async def test_set_cached_uses_request_coords_when_provided() -> None:
    """Open-Meteo's response.lat/lon are nearest-station coords; we must
    cache under the REQUESTED coords so subsequent get_cached() hits."""
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()
    # Forecast comes back with response coords (Open-Meteo snapped to station)
    forecast_with_snapped_coords = Forecast(
        lat=40.643,  # response — snapped slightly
        lon=-73.796,  # response — snapped slightly
        timezone="America/New_York",
        days=[
            DailyForecast(
                date=date(2026, 6, 1),
                temp_max_c=18.0,
                temp_min_c=10.0,
                weather_code=1,
                precip_prob=0,
            )
        ],
    )
    # Cache under the ORIGINAL request coords (40.64, -73.78)
    await set_cached(
        forecast_with_snapped_coords,
        fake_redis,
        request_lat=40.64,
        request_lon=-73.78,
    )
    args, _ = fake_redis.set.call_args
    assert args[0] == "weather:40.64:-73.78"  # NOT weather:40.64:-73.80


@pytest.mark.asyncio
async def test_cache_key_rounds_to_two_decimals() -> None:
    """48.85657 → '48.86'; -73.7842 → '-73.78'."""
    fake_redis = MagicMock()
    fake_redis.get = AsyncMock(return_value=None)
    await get_cached(48.85657, -73.7842, fake_redis)
    fake_redis.get.assert_awaited_once_with("weather:48.86:-73.78")
