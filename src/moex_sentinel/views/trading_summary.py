"""Read-only HTTP view for persisted trading analytics."""

from functools import partial

from fastapi import APIRouter, Request

from moex_sentinel.services.sync_execution import run_sync
from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.trading_summary import TradingSummarySchema

router = APIRouter(tags=["trading"])


@router.get("/trading/summary", response_model=TradingSummarySchema)
async def trading_summary(request: Request) -> TradingSummarySchema:
    result = await run_sync(partial(_usecases(request).view_trading_summary.execute))
    return TradingSummarySchema.from_domain(result)
