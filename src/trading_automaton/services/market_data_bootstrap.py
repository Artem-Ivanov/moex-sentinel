"""Initial candle bootstrap and rolling completed-candle state."""

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from moex_sentinel.domain.market_data import CandleInterval, HistoricCandle
from sentinel_contracts.streaming_market import StreamCandle


class HistoricMarketDataPort(Protocol):
    async def get_candles(
        self,
        instrument_id: str,
        start: datetime,
        end: datetime,
        interval: CandleInterval,
    ) -> tuple[HistoricCandle, ...]: ...


class MarketDataBootstrapService:
    def __init__(
        self,
        market_data: HistoricMarketDataPort,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._market_data = market_data
        self._now = now
        self._lock = asyncio.Lock()
        self._loaded: set[str] = set()
        self._completed: dict[str, tuple[HistoricCandle, ...]] = {}

    async def bootstrap(self, instrument_id: str) -> None:
        async with self._lock:
            if instrument_id in self._loaded:
                return
            self._loaded.add(instrument_id)
        end = self._now()
        try:
            candles = await self._market_data.get_candles(
                instrument_id,
                end - timedelta(hours=2),
                end,
                CandleInterval.MIN_1,
            )
        except Exception:
            async with self._lock:
                self._loaded.discard(instrument_id)
            raise
        completed = tuple(item for item in candles if item.is_complete)[-120:]
        async with self._lock:
            self._completed[instrument_id] = completed

    async def apply_stream_candle(self, candle: StreamCandle) -> None:
        if not candle.is_complete:
            return
        historic = HistoricCandle(
            candle.instrument_id,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            candle.started_at,
            True,
        )
        async with self._lock:
            current = self._completed.get(candle.instrument_id, ())
            by_time = {item.started_at: item for item in current}
            by_time[historic.started_at] = historic
            self._completed[candle.instrument_id] = tuple(by_time[key] for key in sorted(by_time))[-120:]

    async def completed(self, instrument_id: str) -> tuple[HistoricCandle, ...]:
        async with self._lock:
            return self._completed.get(instrument_id, ())
