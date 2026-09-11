"""Read-only HTTP view for persisted trading analytics."""

from fastapi import APIRouter, Request

from moex_sentinel.views.dependencies import usecases as _usecases
from moex_sentinel.views.schemas.trading_summary import TradingSummarySchema

router = APIRouter(tags=["trading"])


@router.get("/trading/summary", response_model=TradingSummarySchema)
def trading_summary(request: Request) -> TradingSummarySchema:
    result = _usecases(request).view_trading_summary.execute()
    return TradingSummarySchema.from_domain(result)
