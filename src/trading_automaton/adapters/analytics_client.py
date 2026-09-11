"""Typed market-only HTTP client; no execution or credential data crosses this port."""

import httpx

from sentinel_contracts.analytics import AnalyticsSnapshot, AnalyticsSnapshotRequest


class AnalyticsClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def snapshot(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot:
        response = await self._http.post("/internal/v1/analytics/snapshots", json=request.model_dump(mode="json"))
        response.raise_for_status()
        return AnalyticsSnapshot.model_validate_json(response.content)

    async def close(self) -> None:
        await self._http.aclose()
