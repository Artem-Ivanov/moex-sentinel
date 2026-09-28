"""Admit Analytics frames and retain transport availability transition state."""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

import httpx

from sentinel_contracts.analytics import (
    AnalyticsInstrument,
    AnalyticsSnapshot,
    AnalyticsSnapshotRequest,
    MarketIndicators,
)
from sentinel_contracts.broker_execution import OrderBookSnapshot
from sentinel_contracts.streaming_market import MarketBatchSnapshot
from trading_automaton.services.order_book_validation import OrderBookValidationService

LOGGER = logging.getLogger(__name__)


class AnalyticsSnapshotPort(Protocol):
    async def snapshot(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot: ...


class AnalyticsMetricsCache:
    """Metrics from the accepted frame, never calculated or refreshed by Worker."""

    def __init__(self) -> None:
        self._values: dict[str, MarketIndicators] = {}

    def replace(self, values: dict[str, MarketIndicators]) -> None:
        self._values = dict(values)

    async def get(self, instrument_id: str) -> MarketIndicators | None:
        return self._values.get(instrument_id)


class AnalyticsFrameService:
    """Fetch and validate market frames without scheduling iterations or closing the client."""

    def __init__(
        self, analytics: AnalyticsSnapshotPort, *, now: Callable[[], datetime], order_books: OrderBookValidationService
    ) -> None:
        self._analytics = analytics
        self._order_books = order_books
        self._now = now
        self._analytics_unavailable_since: datetime | None = None

    async def fetch(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot | None:
        """Return a validated frame or None, logging only availability state transitions."""
        try:
            result = await self._analytics.snapshot(request)
            self._validate_response(result, request)
            if self._analytics_unavailable_since is not None:
                LOGGER.info(
                    "Analytics transport recovered; market freshness is checked separately",
                    extra={
                        "reason_code": "ANALYTICS_TRANSPORT_RECOVERED",
                        "data": {
                            "unavailable_ms": max(
                                0, (self._now() - self._analytics_unavailable_since).total_seconds() * 1000
                            )
                        },
                    },
                )
                self._analytics_unavailable_since = None
            return result
        except (httpx.HTTPError, ValueError):
            if self._analytics_unavailable_since is None:
                self._analytics_unavailable_since = self._now()
                LOGGER.warning(
                    "Analytics unavailable; execution recovery continues",
                    extra={"reason_code": "ANALYTICS_UNAVAILABLE"},
                )
            return None

    @staticmethod
    def _validate_response(result: AnalyticsSnapshot, request: AnalyticsSnapshotRequest) -> None:
        """Reject a response whose calculation profile or instrument set differs from the request."""
        if result.profile_id != request.profile_id:
            raise ValueError("Analytics calculation profile mismatch")
        if {item.instrument_id for item in result.instruments} != set(request.instrument_ids):
            raise ValueError("Analytics instruments mismatch")

    def fresh_instruments(self, frame: AnalyticsSnapshot | None) -> dict[str, AnalyticsInstrument]:
        """Admit tradeable instruments using the current clock and the original frame TTL."""
        if frame is None:
            return {}
        now = self._now()
        ttl = timedelta(milliseconds=min(frame.ttl_ms, 2000))
        if not timedelta() <= now - frame.captured_at <= ttl:
            return {}
        result = {}
        for item in frame.instruments:
            book = item.market.order_book
            if item.freshness != "FRESH" or not item.available or book is None or not book.is_consistent:
                continue
            if not item.market.is_tradeable(now, ttl):
                continue
            valid = self._order_books.validate(
                OrderBookSnapshot(book.bids, book.asks, book.captured_at), now=now, max_age=ttl
            ).valid
            if valid:
                result[item.instrument_id] = item
        return result

    def market_snapshot(
        self, frame: AnalyticsSnapshot | None, instruments: dict[str, AnalyticsInstrument]
    ) -> MarketBatchSnapshot:
        """Build an immutable market view without extending source or order-book expiry."""
        # Worker guards evaluate the admitted market at the current wall clock.
        # The source timestamp remains the expiry anchor, never refreshed by receipt.
        at = self._now()
        snapshot = MarketBatchSnapshot.immutable(
            frame.snapshot_id if frame is not None else "analytics-unavailable",
            at,
            {key: item.market for key, item in instruments.items()},
        )
        if frame is not None and instruments:
            ttl = timedelta(milliseconds=min(frame.ttl_ms, 2000))
            deadline = min(
                frame.captured_at + ttl,
                *(item.market.order_book.captured_at + ttl for item in instruments.values() if item.market.order_book),
            )
            snapshot = snapshot.model_copy(update={"expires_at": deadline})
        return snapshot
