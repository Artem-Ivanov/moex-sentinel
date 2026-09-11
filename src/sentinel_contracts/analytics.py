"""Strict, market-only contracts shared by Core, Analytics and Worker."""

from datetime import UTC
from decimal import Decimal
from hashlib import sha256
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from sentinel_contracts.base import PositionalModel
from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    StreamCandle,
    StreamLastPrice,
    StreamOrderBook,
    StreamTradingStatus,
)

UtcDatetime = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
InstrumentId = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^\S+$")]


class AnalyticsContract(PositionalModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class AdaptiveThresholds(AnalyticsContract):
    averaging_step_percent: Decimal = Field(gt=0)
    minimum_net_profit_percent: Decimal = Field(gt=0)
    source: str = Field(min_length=1, max_length=100)


class MarketIndicators(AnalyticsContract):
    averaging_step_percent: Decimal
    minimum_net_profit_percent: Decimal
    source: str
    mean_5: Decimal | None
    mean_20: Decimal | None
    change_10_percent: Decimal | None
    last_candle_at: UtcDatetime | None
    last_candle_open: Decimal | None = None
    last_candle_close: Decimal | None = None
    range_low: Decimal | None = None
    range_high: Decimal | None = None

    @property
    def buy_window_confirmed(self) -> bool:
        values = (self.mean_5, self.mean_20, self.range_low, self.range_high)
        return (
            self.last_candle_at is not None
            and all(value is not None and value.is_finite() and value > 0 for value in values)
            and self.change_10_percent is not None
            and self.change_10_percent.is_finite()
            and self.range_low is not None
            and self.range_high is not None
            and self.range_high >= self.range_low
        )

    @property
    def candle_direction(self) -> str:
        if self.last_candle_open is None or self.last_candle_close is None:
            return "FLAT"
        if self.last_candle_close > self.last_candle_open:
            return "GREEN"
        if self.last_candle_close < self.last_candle_open:
            return "RED"
        return "FLAT"

    @property
    def downtrend(self) -> bool:
        return (
            self.mean_5 is not None
            and self.mean_20 is not None
            and self.change_10_percent is not None
            and self.mean_5 < self.mean_20
            and self.change_10_percent <= -self.averaging_step_percent
        )


class MarketSnapshotRequest(AnalyticsContract):
    source_id: UUID
    instrument_ids: tuple[InstrumentId, ...] = Field(min_length=1, max_length=100)

    @field_validator("instrument_ids")
    @classmethod
    def unique_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("Instrument IDs must be unique")
        return value


class AnalyticsSnapshotRequest(MarketSnapshotRequest):
    fallback: AdaptiveThresholds

    @property
    def profile_id(self) -> str:
        values = (
            str(self.fallback.averaging_step_percent.normalize()),
            str(self.fallback.minimum_net_profit_percent.normalize()),
            self.fallback.source,
        )
        return sha256(repr(values).encode()).hexdigest()


class _StrictOrderBookLevel(OrderBookLevel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)


class _StrictOrderBook(StreamOrderBook):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    captured_at: UtcDatetime
    bids: tuple[_StrictOrderBookLevel, ...]
    asks: tuple[_StrictOrderBookLevel, ...]


class _StrictLastPrice(StreamLastPrice):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    captured_at: UtcDatetime


class _StrictTradingStatus(StreamTradingStatus):
    model_config = ConfigDict(frozen=True, extra="forbid")
    captured_at: UtcDatetime


class _StrictCandle(StreamCandle):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    captured_at: UtcDatetime
    started_at: UtcDatetime


class _StrictMarketState(InstrumentMarketState):
    model_config = ConfigDict(frozen=True, extra="forbid")
    order_book: _StrictOrderBook | None = None
    last_price: _StrictLastPrice | None = None
    trading_status: _StrictTradingStatus | None = None
    candle: _StrictCandle | None = None


class MarketSourceInstrument(AnalyticsContract):
    instrument_id: InstrumentId
    market: InstrumentMarketState
    candles: tuple[StreamCandle, ...] = Field(max_length=120)
    available: bool

    @field_validator("market", mode="before")
    @classmethod
    def strict_market(cls, value: object) -> InstrumentMarketState:
        return _StrictMarketState.model_validate(value.model_dump() if isinstance(value, BaseModel) else value)

    @field_validator("candles", mode="before")
    @classmethod
    def strict_candles(cls, values: object) -> object:
        if not isinstance(values, (tuple, list)):
            return values
        return tuple(
            _StrictCandle.model_validate(value.model_dump() if isinstance(value, BaseModel) else value)
            for value in values
        )

    @model_validator(mode="after")
    def matching_instruments(self) -> "MarketSourceInstrument":
        values = (
            self.market,
            self.market.order_book,
            self.market.last_price,
            self.market.trading_status,
            self.market.candle,
            *self.candles,
        )
        if any(value is not None and value.instrument_id != self.instrument_id for value in values):
            raise ValueError("Market values must refer to the enclosing instrument")
        return self


class MarketSourceSnapshot(AnalyticsContract):
    schema_version: Literal[1] = 1
    snapshot_id: str = Field(min_length=1, max_length=200)
    captured_at: UtcDatetime
    ttl_ms: int = Field(default=2000, gt=0, le=60000, strict=True)
    instruments: tuple[MarketSourceInstrument, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def unique_instruments(self) -> "MarketSourceSnapshot":
        ids = [item.instrument_id for item in self.instruments]
        if len(set(ids)) != len(ids):
            raise ValueError("Snapshot instruments must be unique")
        return self


class AnalyticsInstrument(MarketSourceInstrument):
    metrics: MarketIndicators
    freshness: Literal["FRESH", "STALE", "UNAVAILABLE"]


class AnalyticsSnapshot(MarketSourceSnapshot):
    instruments: tuple[AnalyticsInstrument, ...] = Field(max_length=100)
    profile_id: str = Field(min_length=1, max_length=64)
