"""Finite application operation for fetching and calculating an analytics batch."""

from market_analytics.ports import MarketSourcePort, MarketSourceUnavailableError
from market_analytics.service import AnalyticsService
from sentinel_contracts.analytics import AnalyticsSnapshot, AnalyticsSnapshotRequest, MarketSnapshotRequest


class AnalyticsUnavailableError(Exception):
    """An analytics batch could not be produced from the market source."""


class CalculateAnalyticsSnapshotUsecase:
    """Fetch one market generation and calculate its analytics without owning source resources."""

    def __init__(self, source: MarketSourcePort, calculator: AnalyticsService) -> None:
        self._source = source
        self._calculator = calculator

    async def execute(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot:
        """Return one calculated batch; translate declared source and calculation failures."""
        try:
            source = await self._source.snapshot(MarketSnapshotRequest(request.source_id, request.instrument_ids))
        except MarketSourceUnavailableError as error:
            raise AnalyticsUnavailableError("MARKET_SOURCE_UNAVAILABLE") from error
        try:
            return self._calculator.calculate(request, source)
        except (ValueError, ArithmeticError) as error:
            raise AnalyticsUnavailableError("MARKET_SOURCE_UNAVAILABLE") from error
