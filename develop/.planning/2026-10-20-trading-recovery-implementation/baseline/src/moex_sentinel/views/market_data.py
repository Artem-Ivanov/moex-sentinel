"""Thin HTTP views for broker-scoped market data."""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, Request

from moex_sentinel.domain.market_data import CandleInterval
from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.market_data import (
    HistoricCandlesResponseSchema,
    InstrumentSearchResponseSchema,
    InstrumentSnapshotSchema,
)

router = APIRouter(tags=["market-data"])


@router.get(
    "/brokers/{broker_id}/market/instruments",
    response_model=InstrumentSearchResponseSchema,
)
async def search_instruments(
    broker_id: str,
    request: Request,
    query: str = "",
    limit: int = 20,
) -> InstrumentSearchResponseSchema:
    result = await _usecases(request).search_market_instruments.execute(broker_id, query, limit)
    return InstrumentSearchResponseSchema(items=[InstrumentSnapshotSchema.from_domain(item) for item in result])


@router.get(
    "/brokers/{broker_id}/market/instruments/{instrument_id}/candles",
    response_model=HistoricCandlesResponseSchema,
)
async def historic_candles(
    broker_id: str,
    instrument_id: str,
    request: Request,
    from_: Annotated[datetime, Query(alias="from")],
    to: datetime,
    interval: CandleInterval,
) -> HistoricCandlesResponseSchema:
    result = await _usecases(request).view_historic_candles.execute(broker_id, instrument_id, from_, to, interval)
    return HistoricCandlesResponseSchema.from_domain(result)


@router.get(
    "/brokers/{broker_id}/market/instruments/{instrument_id}",
    response_model=InstrumentSnapshotSchema,
)
async def market_instrument(broker_id: str, instrument_id: str, request: Request) -> InstrumentSnapshotSchema:
    result = await _usecases(request).view_market_instrument.execute(broker_id, instrument_id)
    return InstrumentSnapshotSchema.from_domain(result)
