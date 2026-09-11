"""Public schemas for SDK-neutral market data."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from moex_sentinel.domain.market_data import HistoricCandle, InstrumentMarketSnapshot
from sentinel_contracts.base import PositionalModel


class StrictSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")


class LastPriceSchema(StrictSchema):
    price: Decimal
    captured_at: datetime


class InstrumentSnapshotSchema(StrictSchema):
    instrument_id: str
    figi: str
    ticker: str
    name: str
    class_code: str
    instrument_type: str
    currency: str | None
    lot: int
    api_trade_available: bool
    last_price: LastPriceSchema | None

    @classmethod
    def from_domain(cls, value: InstrumentMarketSnapshot) -> "InstrumentSnapshotSchema":
        instrument = value.instrument
        price = value.last_price
        return cls(
            instrument_id=instrument.instrument_id,
            figi=instrument.figi,
            ticker=instrument.ticker,
            name=instrument.name,
            class_code=instrument.class_code,
            instrument_type=instrument.instrument_type,
            currency=instrument.currency,
            lot=instrument.lot,
            api_trade_available=instrument.api_trade_available,
            last_price=(None if price is None else LastPriceSchema(price=price.price, captured_at=price.captured_at)),
        )


class InstrumentSearchResponseSchema(StrictSchema):
    items: list[InstrumentSnapshotSchema]


class HistoricCandleSchema(StrictSchema):
    instrument_id: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    started_at: datetime
    is_complete: bool


class HistoricCandlesResponseSchema(StrictSchema):
    items: list[HistoricCandleSchema]

    @classmethod
    def from_domain(cls, values: tuple[HistoricCandle, ...]) -> "HistoricCandlesResponseSchema":
        return cls(items=[HistoricCandleSchema.model_validate(item, from_attributes=True) for item in values])
