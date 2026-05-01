"""Open-Meteo HTTP client: parses the daily-forecast response into a Pydantic model."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx

from trip_tracker.weather.client import Forecast, fetch_forecast


@pytest.mark.asyncio
async def test_fetch_forecast_parses_response() -> None:
    fake = {
        "latitude": 48.8566,
        "longitude": 2.3522,
        "timezone": "Europe/Paris",
        "daily": {
            "time": ["2026-06-01", "2026-06-02"],
            "temperature_2m_max": [22.4, 24.1],
            "temperature_2m_min": [13.5, 14.8],
            "weather_code": [1, 3],
            "precipitation_probability_max": [10, 60],
        },
    }
    with respx.mock(base_url="https://api.open-meteo.com") as router:
        router.get("/v1/forecast").respond(json=fake)
        f = await fetch_forecast(48.8566, 2.3522)
    assert isinstance(f, Forecast)
    assert f.lat == pytest.approx(48.8566)
    assert f.lon == pytest.approx(2.3522)
    assert f.timezone == "Europe/Paris"
    assert len(f.days) == 2
    assert f.days[0].date == date(2026, 6, 1)
    assert f.days[0].temp_max_c == 22.4
    assert f.days[1].precip_prob == 60


@pytest.mark.asyncio
async def test_fetch_forecast_raises_on_5xx() -> None:
    with respx.mock(base_url="https://api.open-meteo.com") as router:
        router.get("/v1/forecast").respond(503)
        with pytest.raises(httpx.HTTPError):
            await fetch_forecast(0, 0)
