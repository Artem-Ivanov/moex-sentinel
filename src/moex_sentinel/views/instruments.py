"""Thin HTTP Views for the broker instrument catalog."""

from decimal import Decimal

from fastapi import APIRouter, Request

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.instruments import (
    CatalogInstrumentSchema,
    CatalogListResponseSchema,
    InstrumentDetailsSchema,
    InstrumentSelectionSchema,
    ReconciliationResultSchema,
)

router = APIRouter(tags=["instruments"])


@router.post(
    "/brokers/{broker_id}/instruments/synchronize",
    response_model=ReconciliationResultSchema,
)
async def synchronize(broker_id: str, request: Request) -> ReconciliationResultSchema:
    result = await _usecases(request).synchronize_broker_instruments.execute(broker_id)
    return ReconciliationResultSchema.from_domain(result)


@router.get("/brokers/{broker_id}/instruments", response_model=CatalogListResponseSchema)
async def instruments(
    broker_id: str,
    request: Request,
    category: str | None = None,
    include_inactive: bool = False,
    selected_only: bool = False,
    lot_price_from: Decimal | None = None,
    lot_price_to: Decimal | None = None,
    currency: str = "RUB",
) -> CatalogListResponseSchema:
    result = await _usecases(request).view_broker_instruments.execute(
        broker_id,
        category,
        include_inactive,
        selected_only,
        lot_price_from,
        lot_price_to,
        currency,
    )
    return CatalogListResponseSchema.from_domain(result)


@router.put(
    "/brokers/{broker_id}/instruments/{instrument_id}/selection",
    response_model=CatalogInstrumentSchema,
)
async def set_selection(
    broker_id: str,
    instrument_id: str,
    payload: InstrumentSelectionSchema,
    request: Request,
) -> CatalogInstrumentSchema:
    result = _usecases(request).set_instrument_selection.execute(broker_id, instrument_id, payload.selected)
    return CatalogInstrumentSchema.from_domain(result)


@router.get(
    "/brokers/{broker_id}/instruments/{instrument_id}",
    response_model=InstrumentDetailsSchema,
)
async def instrument_details(broker_id: str, instrument_id: str, request: Request) -> InstrumentDetailsSchema:
    result = await _usecases(request).view_instrument_details.execute(broker_id, instrument_id)
    return InstrumentDetailsSchema.from_domain(result)
