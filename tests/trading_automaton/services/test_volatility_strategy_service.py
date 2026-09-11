import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from moex_sentinel.domain.market_data import HistoricCandle
from trading_automaton.services.volatility_strategy import (
    AdaptiveThresholds,
    VolatilityStrategyService,
    VolatilityThresholdCache,
)

NOW = datetime(2026, 8, 6, 12, tzinfo=UTC)


def candles(closes: list[str], *, complete: bool = True) -> tuple[HistoricCandle, ...]:
    return tuple(
        HistoricCandle(
            instrument_id="instrument-1",
            open=Decimal(close),
            high=Decimal(close),
            low=Decimal(close),
            close=Decimal(close),
            volume=1,
            started_at=NOW + timedelta(minutes=index),
            is_complete=complete,
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


def test_cache_loads_once_inside_sixty_second_ttl() -> None:
    clock = [NOW]
    calls = 0

    async def loader() -> AdaptiveThresholds:
        nonlocal calls
        calls += 1
        return AdaptiveThresholds(Decimal("0.1"), Decimal("0.05"), "ADAPTIVE")

    cache = VolatilityThresholdCache(now=lambda: clock[0])
    first = asyncio.run(cache.get_or_load("broker-1", "instrument-1", loader))
    clock[0] += timedelta(seconds=59)
    second = asyncio.run(cache.get_or_load("broker-1", "instrument-1", loader))

    assert first == second
    assert calls == 1


def test_cache_reloads_after_sixty_seconds() -> None:
    clock = [NOW]
    calls = 0

    async def loader() -> AdaptiveThresholds:
        nonlocal calls
        calls += 1
        return AdaptiveThresholds(Decimal("0.1"), Decimal("0.05"), "ADAPTIVE")

    cache = VolatilityThresholdCache(now=lambda: clock[0])
    asyncio.run(cache.get_or_load("broker-1", "instrument-1", loader))
    clock[0] += timedelta(seconds=60)
    asyncio.run(cache.get_or_load("broker-1", "instrument-1", loader))

    assert calls == 2
