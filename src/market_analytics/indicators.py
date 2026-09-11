"""Pure two-hour minute-candle indicators used by trading strategies."""

from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from market_analytics.volatility_strategy import (
    VolatilityStrategyService,
)
from sentinel_contracts.analytics import AdaptiveThresholds, MarketIndicators
from sentinel_contracts.streaming_market import StreamCandle


class MarketIndicatorsService:
    def __init__(self, volatility: VolatilityStrategyService | None = None) -> None:
        self._volatility = volatility or VolatilityStrategyService()

    def calculate(self, candles: Sequence[StreamCandle], fallback: AdaptiveThresholds) -> MarketIndicators:
        by_time: dict[datetime, StreamCandle] = {}
        conflicts: set[datetime] = set()
        for item in candles:
            if not item.is_complete:
                continue
            previous = by_time.get(item.started_at)
            if previous is not None and previous.model_dump(exclude={"captured_at"}) != item.model_dump(
                exclude={"captured_at"}
            ):
                conflicts.add(item.started_at)
            by_time[item.started_at] = item
        completed = [by_time[start] for start in sorted(by_time) if start not in conflicts][-120:]
        thresholds = self._volatility.calculate(completed, fallback)
        closes = [item.close for item in completed]
        mean_5 = sum(closes[-5:], Decimal()) / 5 if self._continuous(completed, 5) else None
        mean_20 = sum(closes[-20:], Decimal()) / 20 if self._continuous(completed, 20) else None
        change_10 = None
        if self._continuous(completed, 10) and closes[-10] > 0:
            change_10 = (closes[-1] / closes[-10] - 1) * 100
        range_low = min((item.low for item in completed), default=None)
        range_high = max((item.high for item in completed), default=None)
        return MarketIndicators(
            thresholds.averaging_step_percent,
            thresholds.minimum_net_profit_percent,
            thresholds.source,
            mean_5,
            mean_20,
            change_10,
            completed[-1].started_at if completed else None,
            completed[-1].open if completed else None,
            completed[-1].close if completed else None,
            range_low,
            range_high,
        )

    @staticmethod
    def _continuous(candles: Sequence[StreamCandle], size: int) -> bool:
        return len(candles) >= size and all(
            current.started_at - previous.started_at == timedelta(minutes=1)
            for previous, current in pairwise(candles[-size:])
        )
