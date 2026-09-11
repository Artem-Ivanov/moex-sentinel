"""Adaptive thresholds derived from completed minute candles."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import TypeVar, cast

from market_analytics.volatility_strategy import VolatilityStrategyService
from sentinel_contracts.analytics import AdaptiveThresholds

_CachedValue = TypeVar("_CachedValue")


class VolatilityThresholdCache:
    def __init__(
        self,
        *,
        now: Callable[[], datetime],
        ttl: timedelta = timedelta(seconds=60),
    ) -> None:
        self._now = now
        self._ttl = ttl
        self._values: dict[tuple[str, str], tuple[object, datetime]] = {}

    async def get_or_load(
        self,
        broker_id: str,
        instrument_id: str,
        loader: Callable[[], Awaitable[_CachedValue]],
    ) -> _CachedValue:
        key = (broker_id, instrument_id)
        loaded = self._values.get(key)
        now = self._now()
        if loaded is not None and now - loaded[1] < self._ttl:
            return cast(_CachedValue, loaded[0])
        value = await loader()
        self._values[key] = (value, now)
        return value


__all__ = ["AdaptiveThresholds", "VolatilityStrategyService", "VolatilityThresholdCache"]
