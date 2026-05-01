"""Open-Meteo HTTP client. Free, keyless. Spec §6.1."""

from __future__ import annotations

from datetime import date

import httpx
from pydantic import BaseModel

_BASE_URL = "https://api.open-meteo.com/v1/forecast"
_TIMEOUT_SEC = 10.0


class DailyForecast(BaseModel):
    date: date
    temp_max_c: float
    temp_min_c: float
    weather_code: int  # WMO code; mapped to emoji + label client-side
    precip_prob: int  # 0-100


class Forecast(BaseModel):
    lat: float
    lon: float
    timezone: str
    days: list[DailyForecast]


async def fetch_forecast(lat: float, lon: float) -> Forecast:
    """Fetch a 7-day daily forecast from Open-Meteo. ~200 ms typical latency."""
    params: dict[str, str | float | int] = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max",
        "timezone": "auto",
        "forecast_days": 7,
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT_SEC) as client:
        r = await client.get(_BASE_URL, params=params)
        r.raise_for_status()
        body = r.json()

    daily = body["daily"]
    days = [
        DailyForecast(
            date=date.fromisoformat(d),
            temp_max_c=tmax,
            temp_min_c=tmin,
            weather_code=int(wc),
            precip_prob=int(pp or 0),
        )
        for d, tmax, tmin, wc, pp in zip(
            daily["time"],
            daily["temperature_2m_max"],
            daily["temperature_2m_min"],
            daily["weather_code"],
            daily["precipitation_probability_max"],
            strict=True,
        )
    ]
    return Forecast(
        lat=float(body["latitude"]),
        lon=float(body["longitude"]),
        timezone=str(body["timezone"]),
        days=days,
    )
