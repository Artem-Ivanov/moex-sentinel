"""Lifecycle of one broker stream and its one-second market snapshot ticks."""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from datetime import datetime
from typing import Protocol

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.streaming_market import (
    MarketBatchSnapshot,
    StreamCandle,
    StreamLastPrice,
    StreamOrderBook,
    StreamTradingStatus,
)
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from trading_automaton.adapters.tinvest_streaming import MarketStreamEvent
from trading_automaton.domain.errors import DurableDecisionPersistenceError
from trading_automaton.services.hot_market_data import HotMarketDataCacheService

LOGGER = logging.getLogger(__name__)


class BrokerStreamPort(Protocol):
    async def replace_subscriptions(self, instrument_ids: set[str]) -> None: ...

    def events(self) -> AsyncIterator[MarketStreamEvent]: ...

    async def close(self) -> None: ...


class BrokerTickPort(Protocol):
    async def run_tick(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
    ) -> object: ...


class PositionHydrationPort(Protocol):
    async def hydrate(self, commands: tuple[AutomationCommand, ...]) -> None: ...


class BrokerPreparationPort(Protocol):
    async def prepare(
        self,
        commands: tuple[AutomationCommand, ...],
        snapshot: MarketBatchSnapshot,
    ) -> None: ...


class StreamCandleSinkPort(Protocol):
    async def apply_stream_candle(self, candle: StreamCandle) -> None: ...


class PersistenceFailurePort(Protocol):
    async def hold(self, commands: tuple[AutomationCommand, ...]) -> None: ...


class BrokerRuntimeService:
    def __init__(
        self,
        stream: BrokerStreamPort,
        cache: HotMarketDataCacheService,
        tick: BrokerTickPort,
        *,
        hydration: PositionHydrationPort | None = None,
        preparation: BrokerPreparationPort | None = None,
        candle_sink: StreamCandleSinkPort | None = None,
        persistence_failure: PersistenceFailurePort | None = None,
        now: Callable[[], datetime],
        tick_seconds: float = 1.0,
        retry_seconds: float = 5.0,
    ) -> None:
        self._stream = stream
        self._cache = cache
        self._tick = tick
        self._hydration = hydration
        self._preparation = preparation
        self._candle_sink = candle_sink
        self._persistence_failure = persistence_failure
        self._now = now
        self._tick_seconds = tick_seconds
        self._retry_seconds = retry_seconds
        self._commands: tuple[AutomationCommand, ...] = ()
        self._commands_lock = asyncio.Lock()
        self._changed_lock = asyncio.Lock()
        self._changed_instruments: set[str] = set()
        self._market_event = asyncio.Event()
        self._prepared = asyncio.Event()
        self._closed = asyncio.Event()

    async def replace_commands(self, commands: tuple[AutomationCommand, ...]) -> None:
        async with self._commands_lock:
            self._commands = commands
            self._prepared.clear()
        await self._stream.replace_subscriptions({item.external_instrument_id for item in commands})

    async def run(self) -> None:
        async with asyncio.TaskGroup() as group:
            group.create_task(self._ingest())
            group.create_task(self._market_ticks())
            group.create_task(self._control_ticks())

    async def close(self) -> None:
        self._closed.set()
        self._market_event.set()
        self._prepared.set()
        await self._stream.close()

    async def _ingest(self) -> None:
        async for event in self._stream.events():
            if isinstance(event, StreamOrderBook):
                await self._cache.update_order_book(event)
                async with self._changed_lock:
                    self._changed_instruments.add(event.instrument_id)
                self._market_event.set()
            elif isinstance(event, StreamLastPrice):
                await self._cache.update_last_price(event)
            elif isinstance(event, StreamTradingStatus):
                await self._cache.update_trading_status(event)
            elif isinstance(event, StreamCandle):
                await self._cache.update_candle(event)
                if self._candle_sink is not None:
                    await self._candle_sink.apply_stream_candle(event)
            if self._closed.is_set():
                return

    async def _market_ticks(self) -> None:
        while not self._closed.is_set():
            await self._market_event.wait()
            await self._prepared.wait()
            if self._closed.is_set():
                return
            async with self._changed_lock:
                changed = self._changed_instruments
                self._changed_instruments = set()
                self._market_event.clear()
            async with self._commands_lock:
                commands = tuple(
                    item
                    for item in self._commands
                    if item.state is AutomationState.IN_WORK and item.external_instrument_id in changed
                )
            if not commands:
                continue
            snapshot = await self._cache.snapshot(
                tuple(item.external_instrument_id for item in commands),
                created_at=self._now(),
            )
            try:
                await self._tick.run_tick(commands, snapshot)
            except DurableDecisionPersistenceError:
                if self._persistence_failure is not None:
                    await self._persistence_failure.hold(commands)
                raise

    async def _control_ticks(self) -> None:
        while not self._closed.is_set():
            self._prepared.clear()
            wait_seconds = self._tick_seconds
            async with self._commands_lock:
                commands = self._commands
            if commands:
                try:
                    if self._preparation is not None:
                        preliminary = await self._cache.snapshot(
                            tuple(item.external_instrument_id for item in commands),
                            created_at=self._now(),
                        )
                        await self._preparation.prepare(commands, preliminary)
                    elif self._hydration is not None:
                        await self._hydration.hydrate(commands)
                except TInvestAdapterError as error:
                    if not error.retryable:
                        raise
                    wait_seconds = self._retry_seconds
                    LOGGER.warning(
                        "Broker preparation retry scheduled",
                        extra={"broker_error_code": error.code},
                    )
                else:
                    self._prepared.set()
            else:
                self._prepared.set()
            with suppress(TimeoutError):
                await asyncio.wait_for(self._closed.wait(), timeout=wait_seconds)
