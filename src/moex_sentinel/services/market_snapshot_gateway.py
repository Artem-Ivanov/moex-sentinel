"""Core-owned persistent market sources exposed through a credential-free contract."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID, uuid4, uuid5

from moex_sentinel.services.market_recovery import MarketRecoveryPolicy, MarketSourceOperations
from sentinel_contracts.analytics import MarketSnapshotRequest, MarketSourceInstrument, MarketSourceSnapshot
from sentinel_contracts.market_quality import valid_market_structure
from sentinel_contracts.streaming_market import (
    InstrumentMarketState,
    StreamCandle,
    StreamLastPrice,
    StreamOrderBook,
    StreamTradingStatus,
)
from sentinel_contracts.time import utc_now_ms

type MarketEvent = StreamCandle | StreamLastPrice | StreamOrderBook | StreamTradingStatus
LOGGER = logging.getLogger(__name__)
PERMANENT_STATUSES = {
    "UNAUTHENTICATED",
    "PERMISSION_DENIED",
    "INVALID_ARGUMENT",
    "FAILED_PRECONDITION",
    "UNIMPLEMENTED",
}


class _QuietMarket(TimeoutError):
    """No fresh books observed; this does not identify a provider fault."""


def _failure_data(error: BaseException) -> dict[str, list[str]]:
    """Expose diagnostic codes without exception text, metadata or credentials."""
    allowed_statuses = {
        "CANCELLED",
        "UNKNOWN",
        "INVALID_ARGUMENT",
        "DEADLINE_EXCEEDED",
        "NOT_FOUND",
        "ALREADY_EXISTS",
        "PERMISSION_DENIED",
        "RESOURCE_EXHAUSTED",
        "FAILED_PRECONDITION",
        "ABORTED",
        "OUT_OF_RANGE",
        "UNIMPLEMENTED",
        "INTERNAL",
        "UNAVAILABLE",
        "DATA_LOSS",
        "UNAUTHENTICATED",
    }
    pending = [error]
    exception_types: set[str] = set()
    statuses: set[str] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
            continue
        exception_types.add(type(current).__name__)
        # Diagnostics must not interrupt recovery if an SDK accessor fails.
        with suppress(Exception):
            code = getattr(current, "code", None)
            if callable(code):
                code = code()
            status = getattr(code, "name", None)
            if isinstance(status, str) and status in allowed_statuses:
                statuses.add(status)
    return {"exception_types": sorted(exception_types), "grpc_statuses": sorted(statuses)}


class MarketSourcePort(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def replace_subscriptions(self, instrument_ids: set[str]) -> None: ...
    def events(self) -> AsyncIterator[MarketEvent]: ...
    async def get_candles(self, instrument_id: str, start: datetime, end: datetime) -> tuple[StreamCandle, ...]: ...


class _SourceRuntime:
    def __init__(
        self, source: MarketSourcePort, now: Callable[[], datetime], recovery: MarketRecoveryPolicy, refresh: float
    ) -> None:
        self.source = source
        self.now = now
        self.recovery = recovery
        self.refresh = refresh
        self.instruments: set[str] = set()
        self.states: dict[str, InstrumentMarketState] = {}
        self.candles: dict[str, dict[datetime, StreamCandle]] = {}
        self.loaded: set[str] = set()
        self.changed = asyncio.Event()
        self.connected = False
        self.generation = uuid4()
        self.captured_at = now()
        self.failures = 0
        self.detected_at: float | None = None
        self.last_fresh_book = asyncio.get_running_loop().time()
        self.history_blocked: set[str] = set()
        self.history_failures: dict[str, int] = {}
        self.history_retry_due: dict[str, datetime] = {}
        self.closing = False
        self.cleaning = False
        self.operations = MarketSourceOperations()
        self.task = asyncio.create_task(self.run())

    def touch(self) -> None:
        self.generation = uuid4()
        self.captured_at = self.now()

    def add(self, instruments: tuple[str, ...]) -> None:
        if added := set(instruments) - self.instruments:
            self.instruments.update(added)
            self.changed.set()
            self.touch()

    async def run(self) -> None:
        while True:
            permanent = False
            try:
                await self.operations.run(self.source.start(), self.recovery.operation_timeout_seconds)
                self.connected = True
                self.last_fresh_book = asyncio.get_running_loop().time()
                self.changed.set()
                async with asyncio.TaskGroup() as tasks:
                    tasks.create_task(self.ingest())
                    tasks.create_task(self.refresh_history())
                    tasks.create_task(self.watch_quiet_market())
            except asyncio.CancelledError:
                raise
            except Exception as error:
                data = _failure_data(error)
                permanent = bool(PERMANENT_STATUSES.intersection(data["grpc_statuses"]))
                if self.detected_at is None:
                    self.detected_at = asyncio.get_running_loop().time()
                LOGGER.warning(
                    (
                        "Market source blocked until configuration changes"
                        if permanent
                        else "Market source disconnected; reconnect scheduled"
                    ),
                    extra={"data": data},
                )
            finally:
                self.cleaning = True
                self.connected = False
                self.loaded.clear()
                self.states.clear()
                self.touch()
                # A cancellation-resistant operation may retain the SDK client.
                # Never close or replace that client while the call is alive.
                await self.operations.drain()
                try:
                    await self.operations.run(self.source.close(), self.recovery.close_timeout_seconds)
                except Exception as error:
                    LOGGER.warning(
                        "Market source cleanup failed; market remains unavailable", extra={"data": _failure_data(error)}
                    )
                await self.operations.drain()
                self.cleaning = False
            if permanent or self.closing:
                return
            self.failures += 1
            if self.failures > self.recovery.retry_limit:
                LOGGER.warning(
                    "Market source retries exhausted",
                    extra={"data": {"code": "RETRY_EXHAUSTED", "retries": self.recovery.retry_limit}},
                )
                return
            delay = self.recovery.delay(self.failures)
            LOGGER.info(
                "Market source retry",
                extra={"data": {"code": "RETRY_SCHEDULED", "attempt": self.failures, "delay_seconds": delay}},
            )
            await self.recovery.sleep(delay)

    async def watch_quiet_market(self) -> None:
        while True:
            await asyncio.sleep(self.recovery.quiet_seconds)
            active = any(
                (state := self.states.get(item)) is None
                or state.trading_status is None
                or state.trading_status.limit_order_available
                for item in self.instruments
            )
            if active and asyncio.get_running_loop().time() - self.last_fresh_book >= self.recovery.quiet_seconds:
                LOGGER.info("Market source quiet", extra={"data": {"code": "DATA_QUIET"}})
                raise _QuietMarket

    def observe_progress(self, instrument_id: str) -> None:
        market = self.states.get(instrument_id)
        if market is None or market.order_book is None:
            return
        age = (self.now() - market.order_book.captured_at).total_seconds()
        if not 0 <= age <= 2 or not valid_market_structure(market.order_book, ()):
            return
        self.last_fresh_book = asyncio.get_running_loop().time()
        if instrument_id not in self.loaded or market.trading_status is None:
            return
        if (
            not market.trading_status.limit_order_available
            or not market.trading_status.api_trade_available
            or market.trading_status.captured_at > self.now()
            or not valid_market_structure(market.order_book, self.candles.get(instrument_id, {}).values())
        ):
            return
        self.failures = 0
        if self.detected_at is not None:
            LOGGER.info(
                "Market source recovered",
                extra={
                    "data": {
                        "code": "DATA_RECOVERED",
                        "elapsed_seconds": asyncio.get_running_loop().time() - self.detected_at,
                    }
                },
            )
            self.detected_at = None

    async def ingest(self) -> None:
        async for event in self.source.events():
            current = self.states.get(event.instrument_id, InstrumentMarketState(event.instrument_id))
            if isinstance(event, StreamCandle):
                field = "candle"
                self.merge_candles(event.instrument_id, (event,))
            elif isinstance(event, StreamOrderBook):
                field = "order_book"
            elif isinstance(event, StreamLastPrice):
                field = "last_price"
            else:
                field = "trading_status"
            previous = getattr(current, field)
            # Delayed stream delivery cannot replace a newer source observation.
            if previous is not None and event.captured_at < previous.captured_at:
                continue
            updated = current.model_copy(update={field: event})
            if updated != current:
                self.states[event.instrument_id] = updated
                self.touch()
                self.observe_progress(event.instrument_id)
        raise ConnectionError("Market stream ended")

    def merge_candles(self, instrument_id: str, incoming: tuple[StreamCandle, ...]) -> None:
        values = self.candles.setdefault(instrument_id, {})
        previous = dict(values)
        for candle in incoming:
            if not candle.is_complete:
                continue
            existing = values.get(candle.started_at)
            if existing is None or candle.captured_at >= existing.captured_at:
                values[candle.started_at] = candle
        self.candles[instrument_id] = {at: values[at] for at in sorted(values)[-120:]}
        if previous != self.candles[instrument_id]:
            self.touch()

    async def refresh_history(self) -> None:
        subscribed: set[str] = set()
        due = dict(self.history_retry_due)
        while True:
            self.changed.clear()
            desired = self.instruments.copy()
            if desired != subscribed:
                await self.operations.run(
                    self.source.replace_subscriptions(desired), self.recovery.operation_timeout_seconds
                )
                subscribed = desired
            now = self.now()
            pending = [item for item in sorted(desired - self.history_blocked) if item not in due or now >= due[item]]
            # One owner per source provides single-flight bootstrap and backfill.
            for instrument_id in pending:
                end = self.now()
                try:
                    history = await self.operations.run(
                        self.source.get_candles(instrument_id, end - timedelta(hours=2), end),
                        self.recovery.operation_timeout_seconds,
                    )
                except Exception as error:
                    # Do not overlap a retry with a call that ignored cancellation.
                    if self.operations.pending:
                        _, unfinished = await asyncio.wait(
                            tuple(self.operations.pending), timeout=self.recovery.close_timeout_seconds
                        )
                        if unfinished:
                            raise
                    # An initial failure stays unavailable. Once bootstrapped,
                    # stale history only removes buy metrics in Analytics;
                    # fresh quotes must remain usable for position protection.
                    data = _failure_data(error)
                    count = self.history_failures[instrument_id] = self.history_failures.get(instrument_id, 0) + 1
                    if PERMANENT_STATUSES.intersection(data["grpc_statuses"]) or count > self.recovery.retry_limit:
                        self.history_blocked.add(instrument_id)
                        due.pop(instrument_id, None)
                        self.history_retry_due.pop(instrument_id, None)
                        LOGGER.warning(
                            "Market history recovery stopped",
                            extra={"data": {"code": "HISTORY_BLOCKED", "retries": count - 1}},
                        )
                    else:
                        due[instrument_id] = self.now() + timedelta(seconds=self.recovery.delay(count))
                        self.history_retry_due[instrument_id] = due[instrument_id]
                    if count == 1:
                        LOGGER.warning("Market history unavailable", extra={"data": data})
                    continue
                self.history_failures.pop(instrument_id, None)
                self.history_retry_due.pop(instrument_id, None)
                self.merge_candles(instrument_id, history)
                if instrument_id not in self.loaded:
                    self.loaded.add(instrument_id)
                    self.touch()
                self.observe_progress(instrument_id)
                due[instrument_id] = end + timedelta(seconds=self.refresh)
            wait_seconds = min(
                (max(0.001, (deadline - self.now()).total_seconds()) for deadline in due.values()),
                default=self.refresh,
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(self.changed.wait(), timeout=wait_seconds)

    def snapshot(self, instruments: tuple[str, ...]) -> MarketSourceSnapshot:
        values = []
        for instrument_id in instruments:
            market = self.states.get(instrument_id, InstrumentMarketState(instrument_id))
            available = (
                self.connected
                and instrument_id in self.loaded
                and market.order_book is not None
                and market.trading_status is not None
            )
            values.append(
                MarketSourceInstrument(
                    instrument_id=instrument_id,
                    market=market,
                    candles=tuple(self.candles.get(instrument_id, {}).values()),
                    available=available,
                )
            )
        return MarketSourceSnapshot(
            snapshot_id=str(uuid5(self.generation, "|".join(instruments))),
            captured_at=self.captured_at,
            ttl_ms=2000,
            instruments=tuple(values),
        )

    async def close(self) -> None:
        if not self.closing:
            self.closing = True
            if not self.cleaning:
                self.task.cancel()
        done, _ = await asyncio.wait((self.task,), timeout=self.recovery.close_timeout_seconds * 2)
        if not done:
            raise TimeoutError("Previous market source still owns pending operations")
        with suppress(asyncio.CancelledError):
            await self.task


class MarketSnapshotGateway:
    """Lazily start one source per opaque ID; reads never await broker market I/O."""

    def __init__(
        self,
        source_factory: Callable[[object], MarketSourcePort],
        *,
        configuration_resolver: Callable[[UUID], object] | None = None,
        now: Callable[[], datetime] = utc_now_ms,
        retry_seconds: float = 1.0,
        retry_limit: int = 5,
        refresh_seconds: float = 60.0,
        recovery: MarketRecoveryPolicy | None = None,
    ) -> None:
        self._source_factory = source_factory
        self._configuration_resolver = configuration_resolver
        self._configurations: dict[UUID, object] = {}
        self._now = now
        self._recovery = recovery or MarketRecoveryPolicy(retry_seconds=retry_seconds, retry_limit=retry_limit)
        self._refresh = refresh_seconds
        self._sources: dict[UUID, _SourceRuntime] = {}
        self._lock = asyncio.Lock()

    async def snapshot(self, request: MarketSnapshotRequest) -> MarketSourceSnapshot:
        async with self._lock:
            runtime = self._sources.get(request.source_id)
            try:
                configuration = (
                    request.source_id
                    if self._configuration_resolver is None
                    else await asyncio.to_thread(self._configuration_resolver, request.source_id)
                )
            except Exception:
                if runtime is not None:
                    await runtime.close()
                self._configurations.pop(request.source_id, None)
                self._sources.pop(request.source_id, None)
                raise
            if runtime is not None and self._configurations[request.source_id] != configuration:
                await runtime.close()
                self._sources.pop(request.source_id)
                self._configurations.pop(request.source_id)
                runtime = None
            if runtime is None:
                source = await asyncio.to_thread(self._source_factory, configuration)
                runtime = _SourceRuntime(source, self._now, self._recovery, self._refresh)
                self._sources[request.source_id] = runtime
                self._configurations[request.source_id] = configuration
            runtime.add(request.instrument_ids)
            return runtime.snapshot(request.instrument_ids)

    async def close(self) -> None:
        async with self._lock:
            await asyncio.gather(*(runtime.close() for runtime in self._sources.values()))
            self._sources.clear()
            self._configurations.clear()
