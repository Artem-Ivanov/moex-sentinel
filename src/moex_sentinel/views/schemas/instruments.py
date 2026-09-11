"""Public schemas for the broker instrument catalog."""

from datetime import datetime
from decimal import Decimal

from pydantic import ConfigDict

from moex_sentinel.domain.instrument_catalog import (
    CatalogInstrument,
    CatalogInstrumentPrice,
    CatalogListView,
    CatalogReconciliationResult,
    InstrumentDetailsView,
)
from moex_sentinel.views.schemas.market_data import LastPriceSchema
from sentinel_contracts.base import PositionalModel


class StrictSchema(PositionalModel):
    model_config = ConfigDict(extra="forbid")


class CatalogInstrumentSchema(StrictSchema):
    id: str
    broker_id: str
    instrument_id: str
    figi: str
    ticker: str
    name: str
    class_code: str
    instrument_type: str
    category: str
    currency: str | None
    lot: int
    min_price_increment: Decimal
    api_trade_available: bool
    is_active: bool
    is_selected: bool
    first_seen_at: datetime
    last_seen_at: datetime

    @classmethod
    def from_domain(cls, item: CatalogInstrument) -> "CatalogInstrumentSchema":
        return cls.model_validate(item, from_attributes=True)


class CatalogInstrumentPriceSchema(CatalogInstrumentSchema):
    unit_price: Decimal | None
    lot_price: Decimal | None
    price_captured_at: datetime | None

    @classmethod
    def from_priced_domain(cls, item: CatalogInstrumentPrice) -> "CatalogInstrumentPriceSchema":
        return cls(
            **CatalogInstrumentSchema.from_domain(item.instrument).model_dump(),
            unit_price=item.unit_price,
            lot_price=item.lot_price,
            price_captured_at=item.price_captured_at,
        )


class CatalogCategorySchema(StrictSchema):
    code: str
    label: str
    count: int


class CatalogSyncStateSchema(StrictSchema):
    broker_id: str
    status: str
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    safe_error: str | None


class CatalogListResponseSchema(StrictSchema):
    broker_id: str
    items: list[CatalogInstrumentPriceSchema]
    categories: list[CatalogCategorySchema]
    sync_state: CatalogSyncStateSchema
    currencies: list[str]

    @classmethod
    def from_domain(cls, view: CatalogListView) -> "CatalogListResponseSchema":
        return cls(
            broker_id=view.broker_id,
            items=[CatalogInstrumentPriceSchema.from_priced_domain(item) for item in view.items],
            categories=[CatalogCategorySchema.model_validate(item, from_attributes=True) for item in view.categories],
            sync_state=CatalogSyncStateSchema.model_validate(view.sync_state, from_attributes=True),
            currencies=list(view.currencies),
        )


class ReconciliationResultSchema(StrictSchema):
    broker_id: str
    added: int
    updated: int
    deactivated: int
    synchronized_at: datetime

    @classmethod
    def from_domain(cls, value: CatalogReconciliationResult) -> "ReconciliationResultSchema":
        return cls.model_validate(value, from_attributes=True)


class InstrumentDetailsSchema(StrictSchema):
    broker_name: str
    instrument: CatalogInstrumentSchema
    last_price: LastPriceSchema | None
    lot_price: Decimal | None
    sync_state: CatalogSyncStateSchema

    @classmethod
    def from_domain(cls, value: InstrumentDetailsView) -> "InstrumentDetailsSchema":
        return cls(
            broker_name=value.broker_name,
            instrument=CatalogInstrumentSchema.from_domain(value.instrument),
            last_price=(
                None
                if value.last_price is None
                else LastPriceSchema(
                    price=value.last_price.price,
                    captured_at=value.last_price.captured_at,
                )
            ),
            lot_price=value.lot_price,
            sync_state=CatalogSyncStateSchema.model_validate(value.sync_state, from_attributes=True),
        )


class InstrumentSelectionSchema(StrictSchema):
    selected: bool
