"""SDK-neutral market-data records."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import ConfigDict

from sentinel_contracts.base import PositionalModel


class CandleInterval(StrEnum):
    MIN_1 = "1_MIN"
    MIN_5 = "5_MIN"
    MIN_15 = "15_MIN"
    HOUR = "HOUR"
    DAY = "DAY"


class MarketInstrument(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    figi: str
    ticker: str
    name: str
    class_code: str
    instrument_type: str
    currency: str | None
    lot: int
    min_price_increment: Decimal
    api_trade_available: bool


class LastPrice(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    price: Decimal
    captured_at: datetime


class HistoricCandle(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    started_at: datetime
    is_complete: bool


class InstrumentMarketSnapshot(PositionalModel):
    model_config = ConfigDict(frozen=True)
    instrument: MarketInstrument
    last_price: LastPrice | None
