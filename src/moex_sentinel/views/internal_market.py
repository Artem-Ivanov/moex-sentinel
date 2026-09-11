"""Market-only internal contract; broker settings never cross this boundary."""

from fastapi import APIRouter, HTTPException, Request

from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceSnapshot

router = APIRouter(prefix="/internal/v1/market", tags=["internal-market"])


@router.post("/snapshots", response_model=MarketSourceSnapshot)
async def snapshot(body: MarketSnapshotRequest, request: Request) -> MarketSourceSnapshot:
    try:
        return await request.app.state.market_snapshot_gateway.snapshot(body)
    except LookupError:
        raise HTTPException(status_code=404, detail="Market source not found.") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="Market source unavailable.") from None
