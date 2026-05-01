"""saq task: refresh_weather pulls forecast and writes to Redis cache."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_refresh_weather_fetches_and_caches() -> None:
    # Import the task BEFORE patching so the worker module is fully loaded
    # and `fetch_forecast` is bound in the worker namespace at patch time.
    from trip_tracker.weather.client import DailyForecast, Forecast
    from trip_tracker.worker import refresh_weather

    fake_forecast = Forecast(
        lat=48.86,
        lon=2.35,
        timezone="Europe/Paris",
        days=[
            DailyForecast(
                date=date(2026, 6, 1),
                temp_max_c=20.0,
                temp_min_c=10.0,
                weather_code=1,
                precip_prob=0,
            )
        ],
    )
    fake_redis = MagicMock()
    fake_redis.set = AsyncMock()

    with patch("trip_tracker.worker.fetch_forecast", AsyncMock(return_value=fake_forecast)):
        ctx = {"redis": fake_redis}
        await refresh_weather(ctx, lat=48.86, lon=2.35)

    fake_redis.set.assert_awaited_once()
    key = fake_redis.set.call_args.args[0]
    assert key == "weather:48.86:2.35"
