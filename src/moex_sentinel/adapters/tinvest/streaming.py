"""T-Invest market-data stream adapter for one persistent SDK manager."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from t_tech.invest.grpc.schemas import (
    CandleInstrument,
    InfoInstrument,
    LastPriceInstrument,
    OrderBookInstrument,
    SubscriptionInterval,
)

from moex_sentinel.adapters.tinvest.converters import enum_name, quotation_to_decimal
from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import (
    StreamCandle,
    StreamLastPrice,
    StreamOrderBook,
    StreamTradingStatus,
)

type MarketStreamEvent = (StreamOrderBook | StreamLastPrice | StreamTradingStatus | StreamCandle)


class SubscriptionManager(Protocol):
    def subscribe(self, instruments: list[Any]) -> object: ...

    def unsubscribe(self, instruments: list[Any]) -> object: ...


class CandleSubscriptionManager(SubscriptionManager, Protocol):
    def waiting_close(self, enabled: bool = True) -> SubscriptionManager: ...


class MarketDataStreamManager(Protocol):
    order_book: SubscriptionManager
    last_price: SubscriptionManager
    candles: CandleSubscriptionManager
    info: SubscriptionManager

    def __aiter__(self) -> AsyncIterator[Any]: ...

    def stop(self) -> None: ...


class TInvestStreamingAdapter:
    def __init__(self, manager: MarketDataStreamManager) -> None:
        self._manager = manager
        self._instrument_ids: set[str] = set()

    async def replace_subscriptions(self, instrument_ids: set[str]) -> None:
        added = sorted(instrument_ids - self._instrument_ids)
        removed = sorted(self._instrument_ids - instrument_ids)
        if added:
            self._change(added, subscribe=True)
        if removed:
            self._change(removed, subscribe=False)
        self._instrument_ids = instrument_ids.copy()

    async def events(self) -> AsyncIterator[MarketStreamEvent]:
        async for response in self._manager:
            event = _event(response)
            if event is not None:
                yield event

    async def close(self) -> None:
        self._manager.stop()

    def _change(self, instrument_ids: list[str], *, subscribe: bool) -> None:
        action = "subscribe" if subscribe else "unsubscribe"
        getattr(self._manager.order_book, action)(
            [OrderBookInstrument(instrument_id=item, depth=20) for item in instrument_ids]
        )
        getattr(self._manager.last_price, action)([LastPriceInstrument(instrument_id=item) for item in instrument_ids])
        getattr(self._manager.info, action)([InfoInstrument(instrument_id=item) for item in instrument_ids])
        candles = self._manager.candles.waiting_close(True)
        getattr(candles, action)(
            [
                CandleInstrument(
                    instrument_id=item,
                    interval=SubscriptionInterval.SUBSCRIPTION_INTERVAL_ONE_MINUTE,
                )
                for item in instrument_ids
            ]
        )


def _event(response: Any) -> MarketStreamEvent | None:
    if orderbook := getattr(response, "orderbook", None):
        return StreamOrderBook(
            instrument_id=orderbook.instrument_uid,
            bids=tuple(OrderBookLevel(quotation_to_decimal(item.price), item.quantity) for item in orderbook.bids),
            asks=tuple(OrderBookLevel(quotation_to_decimal(item.price), item.quantity) for item in orderbook.asks),
            captured_at=orderbook.time,
            is_consistent=bool(orderbook.is_consistent),
        )
    if last_price := getattr(response, "last_price", None):
        return StreamLastPrice(
            last_price.instrument_uid,
            quotation_to_decimal(last_price.price),
            last_price.time,
        )
    if status := getattr(response, "trading_status", None):
        return StreamTradingStatus(
            instrument_id=status.instrument_uid,
            status=enum_name(status.trading_status),
            limit_order_available=bool(status.limit_order_available_flag),
            api_trade_available=True,
            captured_at=status.time,
        )
    if candle := getattr(response, "candle", None):
        return StreamCandle(
            instrument_id=candle.instrument_uid,
            open=quotation_to_decimal(candle.open),
            high=quotation_to_decimal(candle.high),
            low=quotation_to_decimal(candle.low),
            close=quotation_to_decimal(candle.close),
            volume=candle.volume,
            started_at=candle.time,
            is_complete=True,
            captured_at=candle.last_trade_ts or candle.time,
        )
    return None
