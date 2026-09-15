"""HTTP adapter for the Core market-only gateway."""

import httpx

from market_analytics.ports import MarketSourceUnavailableError
from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceSnapshot


class HttpMarketSource:
    """Read Core market snapshots through an externally owned HTTP client."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def snapshot(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot:
        """Translate HTTP failures and invalid source payloads into the source contract error."""
        try:
            response = await self._client.post("/internal/v1/market/snapshots", json=request.model_dump(mode="json"))
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise MarketSourceUnavailableError("Market source request failed.") from error
        try:
            return MarketSourceSnapshot.model_validate_json(response.content)
        except ValueError as error:
            raise MarketSourceUnavailableError("Invalid market source snapshot.") from error
