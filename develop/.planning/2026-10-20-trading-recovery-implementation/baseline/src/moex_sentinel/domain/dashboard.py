"""Read models displayed by dashboard pages."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class PositionRecordNotFoundError(LookupError):
    """An open position could not be found."""


class AnalyticsPoint(PositionalModel):
    model_config = ConfigDict(frozen=True)
    captured_at: datetime
    value: Decimal


class AnalyticsView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    balance: Decimal | None
    balance_change: Decimal | None
    realized_pnl: Decimal | None
    unrealized_pnl: Decimal | None
    history: tuple[AnalyticsPoint, ...]


class VolatileInstrumentView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    broker_id: str
    broker_name: str
    instrument_id: str
    ticker: str
    source: str


class OpenPositionView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    broker_id: str
    broker_name: str
    instrument_id: str
    ticker: str
    quantity_lots: int
    average_open_price: Decimal
    net_pnl: Decimal | None


class PositionOperationView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    id: str
    side: str
    quantity_lots: int
    price: Decimal
    commission: Decimal
    occurred_at: datetime


class PositionDetailsView(PositionalModel):
    model_config = ConfigDict(frozen=True)
    position: OpenPositionView
    order_book: tuple[object, ...]
    operations: tuple[PositionOperationView, ...]
