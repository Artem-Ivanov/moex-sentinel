from datetime import UTC, datetime, timedelta
from decimal import Decimal

from moex_sentinel.domain.market_data import HistoricCandle
from trading_automaton.services.market_indicators import MarketIndicatorsService
from trading_automaton.services.volatility_strategy import AdaptiveThresholds

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def candle(open_price: str, close_price: str, minute: int, *, complete: bool = True):
    return HistoricCandle(
        "instrument-1",
        Decimal(open_price),
        max(Decimal(open_price), Decimal(close_price)),
        min(Decimal(open_price), Decimal(close_price)),
        Decimal(close_price),
        100,
        NOW + timedelta(minutes=minute),
        complete,
    )


def calculate(items):
    return MarketIndicatorsService().calculate(items, AdaptiveThresholds(Decimal("0.5"), Decimal("0.5"), "FALLBACK"))


def test_direction_uses_last_completed_candle() -> None:
    result = calculate(
        (
            candle("100", "99", 0),
            candle("99", "101", 1),
            candle("101", "90", 2, complete=False),
        )
    )

    assert result.last_candle_open == Decimal("99")
    assert result.last_candle_close == Decimal("101")
    assert result.candle_direction == "GREEN"


def test_direction_distinguishes_red_and_flat_candles() -> None:
    assert calculate((candle("100", "99", 0),)).candle_direction == "RED"
    assert calculate((candle("100", "100", 0),)).candle_direction == "FLAT"


def test_range_uses_only_last_120_completed_candles() -> None:
    excluded = HistoricCandle(
        "instrument-1",
        Decimal("100"),
        Decimal("500"),
        Decimal("0.1"),
        Decimal("100"),
        100,
        NOW,
        True,
    )
    included = [candle("100", "100", minute) for minute in range(1, 121)]
    included[-1] = HistoricCandle(
        "instrument-1",
        Decimal("100"),
        Decimal("130"),
        Decimal("70"),
        Decimal("100"),
        100,
        NOW + timedelta(minutes=120),
        True,
    )
    incomplete = HistoricCandle(
        "instrument-1",
        Decimal("100"),
        Decimal("900"),
        Decimal("0.01"),
        Decimal("100"),
        100,
        NOW + timedelta(minutes=121),
        False,
    )

    result = calculate((excluded, *included, incomplete))

    assert result.range_low == Decimal("70")
    assert result.range_high == Decimal("130")


def test_empty_candles_have_no_range() -> None:
    result = calculate(())

    assert result.range_low is None
    assert result.range_high is None


def test_shuffled_completed_candles_produce_chronological_indicators() -> None:
    items = [candle("100", str(100 + minute), minute) for minute in range(20)]
    result = calculate(tuple(reversed(items)))
    assert result.mean_5 == Decimal("117")
    assert result.mean_20 == Decimal("109.5")
    assert result.last_candle_at == NOW + timedelta(minutes=19)


def test_duplicate_candles_cannot_manufacture_confirmation_window() -> None:
    result = calculate((candle("100", "100", 0),) * 20)
    assert result.mean_20 is None


def test_gap_in_latest_twenty_minutes_does_not_confirm_window() -> None:
    items = [candle("100", "100", minute) for minute in range(21) if minute != 10]
    result = calculate(items)
    assert result.mean_20 is None


def test_duplicate_conflicting_candles_do_not_depend_on_input_order() -> None:
    items = [candle("100", "100", minute) for minute in range(20)]
    items.append(candle("100", "101", 19))
    result = calculate(items)
    assert result == calculate(tuple(reversed(items)))
    assert result.mean_20 is None
