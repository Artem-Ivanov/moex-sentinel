"""Compute one immutable market-only analytics batch."""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Literal

from market_analytics.indicators import MarketIndicatorsService
from sentinel_contracts.analytics import (
    AnalyticsInstrument,
    AnalyticsSnapshot,
    AnalyticsSnapshotRequest,
    MarketSourceInstrument,
    MarketSourceSnapshot,
)
from sentinel_contracts.market_quality import valid_market_structure
from sentinel_contracts.streaming_market import InstrumentMarketState, StreamCandle


class AnalyticsService:
    """Calculate market analytics from supplied data without fetching external state."""

    def __init__(self, indicators: MarketIndicatorsService, *, now: Callable[[], datetime]) -> None:
        self._now = now
        self._indicators = indicators

    def calculate(self, request: AnalyticsSnapshotRequest, source: MarketSourceSnapshot) -> AnalyticsSnapshot:
        """Calculate one immutable batch, rejecting unexpected instruments or invalid arithmetic."""
        now = self._now()
        instruments = {item.instrument_id: item for item in source.instruments}
        if set(instruments) - set(request.instrument_ids):
            raise ValueError("Unexpected market instruments")
        output = []
        for instrument_id in request.instrument_ids:
            item = instruments.get(instrument_id) or MarketSourceInstrument(
                instrument_id=instrument_id,
                market=InstrumentMarketState(instrument_id),
                candles=(),
                available=False,
            )
            freshness = self._freshness(item, source, now)
            metrics = self._indicators.calculate(
                self._current_candles(item, source.captured_at) if freshness != "UNAVAILABLE" else (), request.fallback
            )
            output.append(
                AnalyticsInstrument(
                    **item.model_dump(),
                    metrics=metrics,
                    freshness=freshness,
                )
            )
        return AnalyticsSnapshot(
            snapshot_id=source.snapshot_id,
            captured_at=source.captured_at,
            ttl_ms=min(source.ttl_ms, 2000),
            instruments=tuple(output),
            profile_id=request.profile_id,
        )

    @staticmethod
    def _current_candles(item: MarketSourceInstrument, now: datetime) -> tuple[StreamCandle, ...]:
        completed = tuple(
            candle
            for candle in item.candles
            if candle.is_complete and candle.captured_at <= now and candle.started_at + timedelta(minutes=1) <= now
        )
        # A stale history removes the buy window and uses the supplied fallback;
        # quote freshness still permits position protection and profit taking.
        if completed and now - max(candle.started_at for candle in completed) <= timedelta(minutes=3):
            return completed
        return ()

    @staticmethod
    def _freshness(
        item: MarketSourceInstrument, source: MarketSourceSnapshot, now: datetime
    ) -> Literal["FRESH", "STALE", "UNAVAILABLE"]:
        book = item.market.order_book
        status = item.market.trading_status
        if not item.available or book is None or status is None or not book.is_consistent:
            return "UNAVAILABLE"
        if not valid_market_structure(book, item.candles):
            return "UNAVAILABLE"
        ttl = timedelta(milliseconds=min(source.ttl_ms, 2000))
        if not timedelta() <= now - source.captured_at <= ttl or not timedelta() <= now - book.captured_at <= ttl:
            return "STALE"
        if status.captured_at > now:
            return "STALE"
        return "FRESH"
