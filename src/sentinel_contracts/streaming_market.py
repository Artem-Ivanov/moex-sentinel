"""SDK-neutral immutable contracts for streaming market snapshots."""

from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

from pydantic import ConfigDict, model_validator

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import OrderBookLevel


class StreamOrderBook(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    captured_at: datetime
    is_consistent: bool

    @property
    def best_bid(self) -> OrderBookLevel:
        return self.bids[0]

    @property
    def best_ask(self) -> OrderBookLevel:
        return self.asks[0]


class StreamLastPrice(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    price: Decimal
    captured_at: datetime


class StreamTradingStatus(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    status: str
    limit_order_available: bool
    captured_at: datetime

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_api_availability(cls, value: object) -> object:
        if isinstance(value, dict) and "api_trade_available" in value:
            value = {key: item for key, item in value.items() if key != "api_trade_available"}
        return value


class UnaryApiTradingStatus(PositionalModel):
    model_config = ConfigDict(frozen=True)
    api_trade_available: bool
    observed_at: datetime
    expires_at: datetime

    def is_fresh(self, now: datetime) -> bool:
        return self.observed_at <= now < self.expires_at


class StreamCandle(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    started_at: datetime
    is_complete: bool
    captured_at: datetime


class InstrumentMarketState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    order_book: StreamOrderBook | None = None
    last_price: StreamLastPrice | None = None
    trading_status: StreamTradingStatus | None = None
    api_trading_status: UnaryApiTradingStatus | None = None
    candle: StreamCandle | None = None

    def is_tradeable(self, now: datetime, max_age: timedelta) -> bool:
        book = self.order_book
        status = self.trading_status
        return bool(
            book is not None
            and book.bids
            and book.asks
            and book.is_consistent
            and now - book.captured_at <= max_age
            and status is not None
            and status.limit_order_available
            and self.has_fresh_api_trade_availability(now)
        )

    def has_fresh_api_trade_availability(self, now: datetime) -> bool:
        status = self.api_trading_status
        return status is not None and status.api_trade_available and status.is_fresh(now)


class MarketBatchSnapshot(PositionalModel):
    model_config = ConfigDict(frozen=True)
    snapshot_id: str
    created_at: datetime
    instruments: Mapping[str, InstrumentMarketState]
    expires_at: datetime | None = None

    @classmethod
    def immutable(
        cls,
        snapshot_id: str,
        created_at: datetime,
        instruments: dict[str, InstrumentMarketState],
    ) -> "MarketBatchSnapshot":
        return cls(
            snapshot_id=snapshot_id,
            created_at=created_at,
            instruments=MappingProxyType(instruments.copy()),
        )

    def model_post_init(self, __context: object) -> None:  # noqa: ARG002
        object.__setattr__(self, "instruments", MappingProxyType(dict(self.instruments)))
