import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from moex_sentinel.domain.market_data import HistoricCandle
from sentinel_contracts.streaming_market import StreamCandle
from trading_automaton.services.market_data_bootstrap import MarketDataBootstrapService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def historic(minute: int, *, complete: bool = True) -> HistoricCandle:
    return HistoricCandle(
        "i1",
        Decimal("100"),
        Decimal("101"),
        Decimal("99"),
        Decimal("100"),
        10,
        NOW + timedelta(minutes=minute),
        complete,
    )


class MarketData:
    def __init__(self, candles) -> None:
        self.candles = candles
        self.calls = []

    async def get_candles(self, instrument_id, start, end, interval):
        self.calls.append((instrument_id, start, end, interval))
        return self.candles


def test_bootstrap_loads_once_and_keeps_last_120_completed_candles() -> None:
    async def scenario():
        market = MarketData((*tuple(historic(i) for i in range(121)), historic(122, complete=False)))
        service = MarketDataBootstrapService(market, now=lambda: NOW)
        await service.bootstrap("i1")
        await service.bootstrap("i1")
        return market, await service.completed("i1")

    market, candles = asyncio.run(scenario())

    assert len(market.calls) == 1
    assert len(candles) == 120
    assert candles[0].started_at == NOW + timedelta(minutes=1)


def test_completed_stream_candle_advances_rolling_window() -> None:
    async def scenario():
        service = MarketDataBootstrapService(MarketData((historic(0),)), now=lambda: NOW)
        await service.bootstrap("i1")
        await service.apply_stream_candle(
            StreamCandle(
                "i1",
                Decimal("100"),
                Decimal("102"),
                Decimal("99"),
                Decimal("101"),
                20,
                NOW + timedelta(minutes=1),
                True,
                NOW + timedelta(minutes=2),
            )
        )
        return await service.completed("i1")

    candles = asyncio.run(scenario())

    assert [item.started_at for item in candles] == [NOW, NOW + timedelta(minutes=1)]
