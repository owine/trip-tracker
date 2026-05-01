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


async def set_cached(
    forecast: Forecast,
    redis: _RedisLike,
    *,
    request_lat: float | None = None,
    request_lon: float | None = None,
) -> None:
    """Persist a forecast under either the request coords or the response's.

    Open-Meteo snaps the response `latitude`/`longitude` to the nearest
    weather station, which can differ from the requested coords by ~0.02°.
    Callers fetching for a known (lat, lon) should pass `request_lat` /
    `request_lon` so the cache key matches what `get_cached` will look up
    next time. Otherwise, fall back to the forecast's own coords.
    """
    key_lat = request_lat if request_lat is not None else forecast.lat
    key_lon = request_lon if request_lon is not None else forecast.lon
    payload = forecast.model_dump_json()
    await redis.set(_key(key_lat, key_lon), payload, ex=_TTL_SEC)
