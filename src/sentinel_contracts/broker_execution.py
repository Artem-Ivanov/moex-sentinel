"""Broker execution port values shared by Core and the trading worker."""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderBookLevel(PositionalModel):
    model_config = ConfigDict(frozen=True)
    price: Decimal
    quantity_lots: int


class OrderBookSnapshot(PositionalModel):
    model_config = ConfigDict(frozen=True)
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    captured_at: datetime

    @property
    def best_bid(self) -> OrderBookLevel:
        return self.bids[0]

    @property
    def best_ask(self) -> OrderBookLevel:
        return self.asks[0]


class LimitOrderEstimate(PositionalModel):
    model_config = ConfigDict(frozen=True)
    total_amount: Decimal
    estimated_commission: Decimal
    currency: str


class BrokerOrderState(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_order_id: str
    idempotency_key: str
    status: str
    requested_lots: int
    executed_lots: int
    requested_amount: Decimal
    executed_amount: Decimal
    estimated_commission: Decimal
    executed_commission: Decimal
    currency: str
    executed_price: Decimal = Decimal()
    executed_at: datetime | None = None


class BrokerConnection(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    adapter_code: str
    target: str
    token: str
    is_test: bool


class BrokerPosition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    quantity_lots: Decimal
    average_price: Decimal
    current_price: Decimal
    currency: str


class BrokerTradingStatus(PositionalModel):
    model_config = ConfigDict(frozen=True)
    status: str
    limit_order_available: bool
    market_order_available: bool
    api_trade_available: bool
