"""Pure adaptive thresholds derived from completed minute candles."""

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from statistics import median

from sentinel_contracts.analytics import AdaptiveThresholds
from sentinel_contracts.streaming_market import StreamCandle


class VolatilityStrategyService:
    MINIMUM_CANDLES = 15
    MINIMUM_AVERAGING_PERCENT = Decimal("0.10")
    MINIMUM_NET_PROFIT_PERCENT = Decimal("0.05")

    def calculate(
        self,
        candles: Sequence[StreamCandle],
        fallback: AdaptiveThresholds,
    ) -> AdaptiveThresholds:
        """Use median returns from at least 15 positive completed closes, otherwise keep the fallback."""
        closes = [item.close for item in candles if item.is_complete]
        if len(closes) < self.MINIMUM_CANDLES or any(value <= 0 for value in closes):
            return fallback
        returns = [abs((current / previous - Decimal("1")) * Decimal("100")) for previous, current in pairwise(closes)]
        median_return = median(returns)
        return AdaptiveThresholds(
            averaging_step_percent=max(self.MINIMUM_AVERAGING_PERCENT, median_return * Decimal("2")),
            minimum_net_profit_percent=max(self.MINIMUM_NET_PROFIT_PERCENT, median_return),
            source="ADAPTIVE",
        )
