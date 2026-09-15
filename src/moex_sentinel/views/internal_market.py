"""Market-only internal contract; broker settings never cross this boundary."""

from typing import cast

from fastapi import APIRouter, Request

from moex_sentinel.usecases.market_snapshot import GetMarketSnapshotUsecase
from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceSnapshot

router = APIRouter(prefix="/internal/v1/market", tags=["internal-market"])


@router.post("/snapshots", response_model=MarketSourceSnapshot)
async def snapshot(body: MarketSnapshotRequest, request: Request) -> MarketSourceSnapshot:
    """Return a market snapshot through the application boundary."""
    usecase = cast(GetMarketSnapshotUsecase, request.app.state.market_snapshot_usecase)
    return await usecase.execute(body)
