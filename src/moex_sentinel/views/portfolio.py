"""Thin read-only HTTP views for broker portfolio data."""

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.portfolio import (
    AccountsResponseSchema,
    OperationsPageResponseSchema,
    OperationsResponseSchema,
    PositionsResponseSchema,
)
from sentinel_contracts.time import floor_utc_millisecond

router = APIRouter(tags=["portfolio"])


@router.get("/brokers/{broker_id}/accounts", response_model=AccountsResponseSchema)
async def broker_accounts(broker_id: str, request: Request) -> AccountsResponseSchema:
    result = await _usecases(request).view_broker_accounts.execute(broker_id)
    return AccountsResponseSchema.from_domain(result)


@router.get("/portfolio/summary", response_model=AccountsResponseSchema)
async def portfolio_summary(request: Request) -> AccountsResponseSchema:
    result = await _usecases(request).view_portfolio_summary.execute()
    return AccountsResponseSchema.from_domain(result)


@router.get("/positions", response_model=PositionsResponseSchema)
async def open_positions(request: Request) -> PositionsResponseSchema:
    result = await _usecases(request).view_external_positions.execute()
    return PositionsResponseSchema.from_domain(result)


@router.get("/operations", response_model=OperationsResponseSchema)
async def recent_operations(
    request: Request,
    limit: int = Query(default=20, ge=1, le=500),
) -> OperationsResponseSchema:
    result = await _usecases(request).view_recent_operations.execute(limit)
    return OperationsResponseSchema.from_domain(result)


@router.get("/operations/scoped", response_model=OperationsPageResponseSchema)
async def scoped_operations(
    request: Request,
    broker_id: Annotated[str, Query(min_length=1)],
    account_id: Annotated[str, Query(min_length=1)],
    from_at: datetime,
    to_at: datetime,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(min_length=1, max_length=1024)] = None,
) -> OperationsPageResponseSchema:
    try:
        from_utc = floor_utc_millisecond(from_at)
        to_utc = floor_utc_millisecond(to_at)
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Даты операций должны содержать часовой пояс.") from error
    if from_utc >= to_utc or to_at.astimezone(UTC) > datetime.now(UTC):
        raise HTTPException(status_code=422, detail="Проверьте временной интервал операций.")

    result = await _usecases(request).view_recent_operations.execute_scoped(
        broker_id,
        account_id,
        limit=limit,
        cursor=cursor,
        from_at=from_utc,
        to_at=to_utc,
    )
    return OperationsPageResponseSchema.from_domain(result)
