"""Redis-backed forecast cache. Keyed by (lat, lon) rounded to 2 decimals.

Cache values are JSON-serialized Forecast models, TTL 3600s. Keys are
shared across users (the Paris forecast is the same regardless of who
asks). Spec §6.2.
"""

from __future__ import annotations

from typing import Protocol

from trip_tracker.weather.client import Forecast

_TTL_SEC = 3600


class _RedisLike(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> object: ...


def _key(lat: float, lon: float) -> str:
    return f"weather:{lat:.2f}:{lon:.2f}"


async def get_cached(lat: float, lon: float, redis: _RedisLike) -> Forecast | None:
    raw = await redis.get(_key(lat, lon))
    if raw is None:
        return None
    return Forecast.model_validate_json(raw)


async def set_cached(forecast: Forecast, redis: _RedisLike) -> None:
    payload = forecast.model_dump_json()
    await redis.set(_key(forecast.lat, forecast.lon), payload, ex=_TTL_SEC)
