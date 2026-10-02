"""Broker execution port values shared by Core and the trading worker."""

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import ConfigDict, model_validator

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.tinvest import BrokerAccessMode, BrokerEnvironment


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


class BrokerScope(PositionalModel):
    """Immutable broker identity without credentials or execution capability."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    broker_id: str
    environment: BrokerEnvironment
    account_id: str


class BrokerConnection(PositionalModel):
    model_config = ConfigDict(frozen=True)
    broker_id: str
    adapter_code: str
    target: str
    token: str
    is_test: bool
    environment: BrokerEnvironment | None = None
    access_mode: BrokerAccessMode = "READ_ONLY"
    account_id: str = ""

    @model_validator(mode="after")
    def validate_environment(self):
        expected = "TEST" if self.is_test else "PROD"
        if self.environment is not None and self.environment != expected:
            raise ValueError("Broker environment and is_test disagree.")
        if expected == "PROD" and not self.account_id.strip():
            raise ValueError("PROD requires a selected account.")
        object.__setattr__(self, "environment", expected)
        return self


class BrokerPosition(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    quantity_lots: Decimal
    average_price: Decimal
    current_price: Decimal
    currency: str
    blocked: bool = False


class BrokerTradingStatus(PositionalModel):
    model_config = ConfigDict(frozen=True)
    status: str
    limit_order_available: bool
    market_order_available: bool
    api_trade_available: bool
