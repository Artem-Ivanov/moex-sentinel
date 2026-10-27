"""Read a market snapshot without exposing source failures to the transport."""

from typing import Protocol

from moex_sentinel.usecases.errors import UseCaseError
from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceSnapshot


class MarketSnapshotPort(Protocol):
    """Market snapshot capability independent of gateway lifecycle ownership."""

    async def snapshot(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot:
        """Return the requested source snapshot or raise a source availability error."""
        ...


class GetMarketSnapshotUsecase:
    """Translate expected source failures while allowing unexpected errors to propagate."""

    def __init__(self, gateway: MarketSnapshotPort) -> None:
        self._gateway = gateway

    async def execute(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot:
        """Return a market snapshot or a stable missing/unavailable source error."""
        try:
            return await self._gateway.snapshot(request)
        except LookupError as error:
            raise UseCaseError("INTERNAL_MARKET_SOURCE_NOT_FOUND", "Market source not found.") from error
        except ValueError as error:
            raise UseCaseError("INTERNAL_MARKET_SOURCE_UNAVAILABLE", "Market source unavailable.") from error
