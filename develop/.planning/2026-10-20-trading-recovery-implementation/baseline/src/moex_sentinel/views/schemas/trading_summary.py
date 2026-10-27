"""Public schemas for persisted trading analytics."""

from datetime import datetime
from decimal import Decimal

from pydantic import Field

from moex_sentinel.domain.trading_summary import (
    CurrencyTradingSummary,
    TradingPnlPeriod,
    TradingSummaryView,
)
from moex_sentinel.views.schemas.portfolio import ReadErrorSchema, StrictSchema


class TradingPnlPeriodSchema(StrictSchema):
    value: Decimal | None
    from_: datetime | None = Field(serialization_alias="from")
    to: datetime | None
    complete: bool

    @classmethod
    def from_domain(cls, value: TradingPnlPeriod) -> "TradingPnlPeriodSchema":
        return cls(value.value, value.from_at, value.to_at, value.complete)


class CurrencyTradingSummarySchema(StrictSchema):
    currency: str
    portfolio_value: Decimal
    free_cash: Decimal
    pnl_24h: TradingPnlPeriodSchema
    pnl_7d: TradingPnlPeriodSchema
    pnl_30d: TradingPnlPeriodSchema

    @classmethod
    def from_domain(cls, value: CurrencyTradingSummary) -> "CurrencyTradingSummarySchema":
        return cls(
            value.currency,
            value.portfolio_value,
            value.free_cash,
            TradingPnlPeriodSchema.from_domain(value.pnl_24h),
            TradingPnlPeriodSchema.from_domain(value.pnl_7d),
            TradingPnlPeriodSchema.from_domain(value.pnl_30d),
        )


class TradingSummarySchema(StrictSchema):
    captured_at: datetime | None
    currencies: tuple[CurrencyTradingSummarySchema, ...]
    errors: tuple[ReadErrorSchema, ...]

    @classmethod
    def from_domain(cls, value: TradingSummaryView) -> "TradingSummarySchema":
        return cls(
            value.captured_at,
            tuple(CurrencyTradingSummarySchema.from_domain(item) for item in value.currencies),
            tuple(ReadErrorSchema.model_validate(item, from_attributes=True) for item in value.errors),
        )
