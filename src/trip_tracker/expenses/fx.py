"""Frankfurter FX client + Redis cache. Spec §5.

Convention: always fetch under `base` without `symbols`, cache the full
~30-currency table per (base, date). Never invert — `get_rate(EUR, USD)`
fetches with base=EUR. Decimal precision is preserved end-to-end by
parsing the Frankfurter JSON with `parse_float=Decimal` and storing
cached values as strings in Redis.
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from decimal import Decimal
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)

_FRANKFURTER_URL = "https://api.frankfurter.dev/v1/latest"
_TTL_SEC = 86400  # 24h
_TIMEOUT = 10.0


class FxError(RuntimeError):
    """Raised when an FX rate is unavailable (HTTP failure with no cache,
    or a target currency not present in the response)."""


class _RedisLike(Protocol):
    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: str | bytes, ex: int | None = None) -> object: ...


def _today() -> str:
    return _dt.date.today().isoformat()


def _key(base: str) -> str:
    return f"fx:{base}:{_today()}"


async def fetch_rates(base: str) -> dict[str, Decimal]:
    """Single Frankfurter HTTP call. Returns ALL rates under `base` as Decimal.
    Raises FxError on 5xx / network failure / parse failure."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(_FRANKFURTER_URL, params={"base": base})
            resp.raise_for_status()
            payload = json.loads(resp.text, parse_float=Decimal)
    except (httpx.HTTPError, ValueError) as exc:
        raise FxError(f"Frankfurter fetch failed: {exc}") from exc
    rates = payload.get("rates")
    if not isinstance(rates, dict):
        raise FxError("Frankfurter response missing 'rates' object")
    return {k: Decimal(str(v)) for k, v in rates.items()}


async def get_cached_rates(base: str, redis: _RedisLike) -> dict[str, Decimal] | None:
    raw = await redis.get(_key(base))
    if raw is None:
        return None
    decoded = raw.decode() if isinstance(raw, bytes) else raw
    parsed = json.loads(decoded)
    return {k: Decimal(v) for k, v in parsed.items()}


async def set_cached_rates(base: str, rates: dict[str, Decimal], redis: _RedisLike) -> None:
    serializable = {k: str(v) for k, v in rates.items()}
    await redis.set(_key(base), json.dumps(serializable), ex=_TTL_SEC)


async def get_rate(base: str, target: str, redis: _RedisLike) -> Decimal:
    """Return `1 base = X target` as Decimal. Cache hit → 0ms; cache miss →
    Frankfurter call + cache. Raises FxError if Frankfurter is unreachable
    AND no cache is available, or if `target` isn't supported."""
    if base == target:
        return Decimal(1)
    cached = await get_cached_rates(base, redis)
    if cached is None:
        cached = await fetch_rates(base)
        await set_cached_rates(base, cached, redis)
    if target not in cached:
        raise FxError(f"target currency {target!r} not available under base {base!r}")
    return cached[target]
