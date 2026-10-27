from datetime import UTC, datetime, timedelta
from decimal import Decimal

from market_analytics.volatility_strategy import VolatilityStrategyService
from sentinel_contracts.analytics import AdaptiveThresholds
from sentinel_contracts.streaming_market import StreamCandle

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def candles(closes: list[str], *, complete: bool = True) -> tuple[StreamCandle, ...]:
    return tuple(
        StreamCandle(
            instrument_id="instrument-1",
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=1,
            started_at=NOW + timedelta(minutes=index),
            is_complete=complete,
            captured_at=NOW,
        )
        for index, close in enumerate(closes)
    )


def fallback() -> AdaptiveThresholds:
    return AdaptiveThresholds(Decimal("0.5"), Decimal("0.5"), "FALLBACK")


def test_thresholds_apply_minimum_floors() -> None:
    values = ["100", "100.02"] * 8

    result = VolatilityStrategyService().calculate(candles(values), fallback())

    assert result == AdaptiveThresholds(Decimal("0.10"), Decimal("0.05"), "ADAPTIVE")


def test_thresholds_use_median_absolute_returns() -> None:
    values = [Decimal("100")]
    for _ in range(15):
        values.append(values[-1] * Decimal("1.0008"))

    result = VolatilityStrategyService().calculate(candles([str(value) for value in values]), fallback())

    assert result.averaging_step_percent == Decimal("0.1600")
    assert result.minimum_net_profit_percent == Decimal("0.0800")
    assert result.source == "ADAPTIVE"


def test_insufficient_or_invalid_candles_use_fallback() -> None:
    service = VolatilityStrategyService()

    assert service.calculate(candles(["100"] * 14), fallback()) == fallback()
    assert service.calculate(candles(["0"] * 16), fallback()) == fallback()
    assert service.calculate(candles(["100"] * 16, complete=False), fallback()) == fallback()
