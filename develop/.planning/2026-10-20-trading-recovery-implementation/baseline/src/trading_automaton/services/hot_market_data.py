"""Atomic in-process cache of the latest streaming market data."""

import asyncio
from datetime import datetime
from uuid import uuid4

from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    MarketBatchSnapshot,
    StreamCandle,
    StreamLastPrice,
    StreamOrderBook,
    StreamTradingStatus,
)


class HotMarketDataCacheService:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._states: dict[str, InstrumentMarketState] = {}

    async def update_order_book(self, value: StreamOrderBook) -> None:
        await self._update(value.instrument_id, order_book=value)

    async def update_last_price(self, value: StreamLastPrice) -> None:
        await self._update(value.instrument_id, last_price=value)

    async def update_trading_status(self, value: StreamTradingStatus) -> None:
        await self._update(value.instrument_id, trading_status=value)

    async def update_candle(self, value: StreamCandle) -> None:
        await self._update(value.instrument_id, candle=value)

    async def snapshot(
        self,
        instrument_ids: tuple[str, ...],
        *,
        created_at: datetime,
    ) -> MarketBatchSnapshot:
        async with self._lock:
            states = {
                instrument_id: self._states.get(instrument_id, InstrumentMarketState(instrument_id))
                for instrument_id in instrument_ids
            }
        return MarketBatchSnapshot.immutable(str(uuid4()), created_at, states)

    async def _update(self, instrument_id: str, **changes: object) -> None:
        async with self._lock:
            current = self._states.get(instrument_id, InstrumentMarketState(instrument_id))
            self._states[instrument_id] = current.model_copy(update=changes)
