"""Transport-independent market source contract for analytics."""

from typing import Protocol

from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceSnapshot


class MarketSourceUnavailableError(Exception):
    """The source could not provide a valid market snapshot."""


class MarketSourcePort(Protocol):
    """Read market data without exposing a transport-specific failure contract."""

    async def snapshot(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot:
        """Return source data or raise MarketSourceUnavailableError when unavailable."""
        ...
