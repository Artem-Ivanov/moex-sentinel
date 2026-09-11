"""HTTP adapter for the Core market-only gateway."""

from typing import Protocol

import httpx

from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceSnapshot


class MarketSourcePort(Protocol):
    async def snapshot(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot: ...


class HttpMarketSource:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def snapshot(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot:
        response = await self._client.post("/internal/v1/market/snapshots", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return MarketSourceSnapshot.model_validate_json(response.content)
