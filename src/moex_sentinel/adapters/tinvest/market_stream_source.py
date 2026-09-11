"""Persistent Core-owned SDK connection exposing market data only."""

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta
from typing import Any

from t_tech.invest import AsyncClient
from t_tech.invest.schemas import CandleInterval

from moex_sentinel.adapters.tinvest.converters import quotation_to_decimal
from moex_sentinel.adapters.tinvest.streaming import MarketStreamEvent, TInvestStreamingAdapter
from moex_sentinel.services.broker_factory import TINVEST_SANDBOX_TARGET
from sentinel_contracts.streaming_market import StreamCandle


class TInvestMarketStreamSource:
    def __init__(
        self,
        token: str,
        target: str,
        *,
        client_factory: Callable[..., Any] = AsyncClient,
    ) -> None:
        if target != TINVEST_SANDBOX_TARGET:
            raise ValueError("Only the configured Sandbox market target is allowed.")
        self._token = token
        self._target = target
        self._client_factory = client_factory
        self._client: Any = None
        self._services: Any = None
        self._stream: TInvestStreamingAdapter | None = None

    async def start(self) -> None:
        if self._services is not None:
            return
        self._client = self._client_factory(self._token, target=self._target)
        self._services = await self._client.__aenter__()
        self._stream = TInvestStreamingAdapter(self._services.create_market_data_stream())

    async def close(self) -> None:
        try:
            if self._stream is not None:
                await self._stream.close()
        finally:
            self._stream = None
            self._services = None
            if self._client is not None:
                client, self._client = self._client, None
                await client.__aexit__(None, None, None)

    async def replace_subscriptions(self, instrument_ids: set[str]) -> None:
        if self._stream is None:
            raise RuntimeError("Market source is not started.")
        await self._stream.replace_subscriptions(instrument_ids)

    async def events(self) -> AsyncIterator[MarketStreamEvent]:
        if self._stream is None:
            raise RuntimeError("Market source is not started.")
        async for event in self._stream.events():
            yield event

    async def get_candles(self, instrument_id: str, start: datetime, end: datetime) -> tuple[StreamCandle, ...]:
        if self._services is None:
            raise RuntimeError("Market source is not started.")
        response = await self._services.market_data.get_candles(
            instrument_id=instrument_id,
            from_=start,
            to=end,
            interval=CandleInterval.CANDLE_INTERVAL_1_MIN,
        )
        return tuple(
            StreamCandle(
                instrument_id=instrument_id,
                open=quotation_to_decimal(item.open),
                high=quotation_to_decimal(item.high),
                low=quotation_to_decimal(item.low),
                close=quotation_to_decimal(item.close),
                volume=item.volume,
                started_at=item.time,
                is_complete=item.is_complete,
                # A history fetch is not a new observation of the candle.
                captured_at=item.time + timedelta(minutes=1),
            )
            for item in response.candles
            if item.is_complete
        )
