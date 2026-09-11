"""Thin read-only HTTP views for broker portfolio data."""

from fastapi import APIRouter, Query, Request

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.portfolio import (
    AccountsResponseSchema,
    OperationsResponseSchema,
    PositionsResponseSchema,
)

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
