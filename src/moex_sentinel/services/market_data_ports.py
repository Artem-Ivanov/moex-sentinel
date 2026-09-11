"""Read-only broker market-data boundary."""

from datetime import datetime
from typing import Protocol

from moex_sentinel.domain.market_data import (
    CandleInterval,
    HistoricCandle,
    LastPrice,
    MarketInstrument,
)


class MarketDataPort(Protocol):
    async def list_instruments(self) -> tuple[MarketInstrument, ...]: ...

    async def search_instruments(self, query: str) -> tuple[MarketInstrument, ...]: ...

    async def get_instrument(self, instrument_id: str) -> MarketInstrument: ...

    async def get_last_prices(self, instrument_ids: tuple[str, ...]) -> tuple[LastPrice, ...]: ...

    async def get_candles(
        self,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]: ...
