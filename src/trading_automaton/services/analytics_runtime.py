"""Consume market-only Analytics batches while keeping execution recovery alive."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

import httpx

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.analytics import (
    AdaptiveThresholds,
    AnalyticsInstrument,
    AnalyticsSnapshot,
    AnalyticsSnapshotRequest,
    MarketIndicators,
)
from sentinel_contracts.broker_execution import OrderBookSnapshot
from sentinel_contracts.streaming_market import MarketBatchSnapshot
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.domain.errors import DurableDecisionPersistenceError
from trading_automaton.services.order_book_validation import OrderBookValidationService

LOGGER = logging.getLogger(__name__)


class AnalyticsPort(Protocol):
    async def snapshot(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot: ...

    async def close(self) -> None: ...


class TickPort(Protocol):
    async def run_tick(self, commands: tuple[AutomationCommand, ...], snapshot: MarketBatchSnapshot) -> object: ...


class PreparationPort(Protocol):
    async def prepare(self, commands: tuple[AutomationCommand, ...], snapshot: MarketBatchSnapshot) -> None: ...


class PersistenceFailurePort(Protocol):
    async def hold(self, commands: tuple[AutomationCommand, ...]) -> None: ...


class AnalyticsMetricsCache:
    """Metrics from the accepted frame, never calculated or refreshed by Worker."""

    def __init__(self) -> None:
        self._values: dict[str, MarketIndicators] = {}

    def replace(self, values: dict[str, MarketIndicators]) -> None:
        self._values = dict(values)

    async def get(self, instrument_id: str) -> MarketIndicators | None:
        return self._values.get(instrument_id)


class AnalyticsBrokerRuntime:
    def __init__(
        self,
        analytics: AnalyticsPort,
        tick: TickPort,
        *,
        preparation: PreparationPort,
        metrics: AnalyticsMetricsCache,
        source_id: str,
        fallback: AdaptiveThresholds,
        now: Callable[[], datetime],
        tick_seconds: float = 1.0,
        retry_limit: int = 5,
        persistence_failure: PersistenceFailurePort | None = None,
    ) -> None:
        self._analytics = analytics
        self._tick = tick
        self._preparation = preparation
        self._metrics = metrics
        self._source_id = source_id
        self._fallback = fallback
        self._now = now
        self._tick_seconds = tick_seconds
        if retry_limit < 0:
            raise ValueError("retry_limit must be nonnegative")
        self._retry_limit = retry_limit
        self._analytics_unavailable_since: datetime | None = None
        self._persistence_failure = persistence_failure
        self._closed = asyncio.Event()
        self._commands: tuple[AutomationCommand, ...] = ()
        self._last_seen: dict[str, tuple[str, str]] = {}
        self._run_lock = asyncio.Lock()

    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None:
        self._commands = commands
        active_ids = {str(item.automation_id) for item in commands}
        self._last_seen = {key: value for key, value in self._last_seen.items() if key in active_ids}

    async def run(self) -> None:
        failures = 0
        while not self._closed.is_set():
            delay = self._tick_seconds
            try:
                await self.run_once()
            except TInvestAdapterError as error:
                if not error.retryable or failures >= self._retry_limit:
                    LOGGER.error(  # noqa: TRY400 - keep broker exception text and metadata out of logs
                        "Broker preparation paused; runtime restart required",
                        extra={
                            "reason_code": "BROKER_PREPARATION_BLOCKED",
                            "data": {"retries": failures, "retryable": error.retryable},
                        },
                    )
                    await self._closed.wait()
                    return
                failures += 1
                delay = 2 * failures - 1
                LOGGER.warning(
                    "Broker preparation retry scheduled",
                    extra={
                        "reason_code": "BROKER_PREPARATION_RETRY",
                        "data": {"retry_attempt": failures, "retry_delay_seconds": delay},
                    },
                )
            else:
                failures = 0
            with suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), timeout=delay)

    async def close(self) -> None:
        self._closed.set()
        async with self._run_lock:
            await self._analytics.close()

    async def run_once(self) -> None:
        async with self._run_lock:
            commands = self._commands
            if not commands or self._closed.is_set():
                return
            request = AnalyticsSnapshotRequest(
                source_id=UUID(self._source_id),
                instrument_ids=tuple(sorted({item.external_instrument_id for item in commands})),
                fallback=self._fallback,
            )
            frame = await self._fetch(request)
            fresh = self._fresh_instruments(frame)
            self._metrics.replace({key: value.metrics for key, value in fresh.items()})
            preliminary = self._market_snapshot(frame, fresh)
            # This includes portfolio reconciliation and runs even during Analytics outage.
            await self._preparation.prepare(commands, preliminary)
            if frame is None or self._closed.is_set():
                return
            fresh = self._fresh_instruments(frame)
            identity = (frame.snapshot_id, frame.profile_id)
            current = {item.automation_id: item for item in self._commands}
            selected = tuple(
                item
                for item in commands
                if item.state is AutomationState.IN_WORK
                and item.external_instrument_id in fresh
                and current.get(item.automation_id) == item
                and self._last_seen.get(str(item.automation_id)) != identity
            )
            if not selected:
                return
            snapshot = self._market_snapshot(frame, fresh)
            try:
                await self._tick.run_tick(selected, snapshot)
            except DurableDecisionPersistenceError:
                if self._persistence_failure is not None:
                    await self._persistence_failure.hold(selected)
                raise
            for item in selected:
                self._last_seen[str(item.automation_id)] = identity

    async def _fetch(self, request: AnalyticsSnapshotRequest) -> AnalyticsSnapshot | None:
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
        if result.profile_id != request.profile_id:
            raise ValueError("Analytics calculation profile mismatch")
        if {item.instrument_id for item in result.instruments} != set(request.instrument_ids):
            raise ValueError("Analytics instruments mismatch")

    def _fresh_instruments(self, frame: AnalyticsSnapshot | None) -> dict[str, AnalyticsInstrument]:
        if frame is None:
            return {}
        now = self._now()
        ttl = timedelta(milliseconds=min(frame.ttl_ms, 2000))
        if not timedelta() <= now - frame.captured_at <= ttl:
            return {}
        validator = OrderBookValidationService()
        result = {}
        for item in frame.instruments:
            book = item.market.order_book
            if item.freshness != "FRESH" or not item.available or book is None or not book.is_consistent:
                continue
            if not item.market.is_tradeable(now, ttl):
                continue
            valid = validator.validate(
                OrderBookSnapshot(book.bids, book.asks, book.captured_at), now=now, max_age=ttl
            ).valid
            if valid:
                result[item.instrument_id] = item
        return result

    def _market_snapshot(
        self, frame: AnalyticsSnapshot | None, instruments: dict[str, AnalyticsInstrument]
    ) -> MarketBatchSnapshot:
        at = frame.captured_at if frame is not None else self._now()
        snapshot = MarketBatchSnapshot.immutable(
            frame.snapshot_id if frame is not None else "analytics-unavailable",
            at,
            {key: item.market for key, item in instruments.items()},
        )
        if frame is not None and instruments:
            ttl = timedelta(milliseconds=min(frame.ttl_ms, 2000))
            deadline = min(
                at + ttl,
                *(item.market.order_book.captured_at + ttl for item in instruments.values() if item.market.order_book),
            )
            snapshot = snapshot.model_copy(update={"expires_at": deadline})
        return snapshot
