import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from moex_sentinel.adapters.tinvest.errors import TInvestAdapterError
from sentinel_contracts.broker_execution import OrderBookLevel
from sentinel_contracts.streaming_market import StreamCandle, StreamOrderBook
from sentinel_contracts.trading import AutomationState
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.domain.errors import DurableDecisionPersistenceError
from trading_automaton.services.broker_runtime import BrokerRuntimeService
from trading_automaton.services.hot_market_data import HotMarketDataCacheService

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command(index: int, state: AutomationState = AutomationState.IN_WORK) -> AutomationCommand:
    return baseline_command(automation=f"a{index}", broker="b1", instrument=f"i{index}", state=state)


class Stream:
    def __init__(self) -> None:
        self.subscriptions = []
        self.closed = False
        self.release = asyncio.Event()

    async def replace_subscriptions(self, instrument_ids):
        self.subscriptions.append(instrument_ids)

    async def events(self):
        yield StreamOrderBook(
            "i1",
            (OrderBookLevel(Decimal("100"), 10),),
            (OrderBookLevel(Decimal("100.01"), 10),),
            NOW,
            True,
        )
        await self.release.wait()

    async def close(self):
        self.closed = True
        self.release.set()


def book(price: str = "100") -> StreamOrderBook:
    value = Decimal(price)
    return StreamOrderBook(
        "i1",
        (OrderBookLevel(value, 10),),
        (OrderBookLevel(value + Decimal("0.01"), 10),),
        NOW,
        True,
    )


class OrderBookStream(Stream):
    def __init__(self) -> None:
        super().__init__()
        self.events_queue: asyncio.Queue[StreamOrderBook | None] = asyncio.Queue()

    async def events(self):
        while True:
            event = await self.events_queue.get()
            if event is None:
                return
            yield event

    async def close(self):
        self.closed = True
        await self.events_queue.put(None)


class Tick:
    def __init__(self) -> None:
        self.calls = []
        self.called = asyncio.Event()

    async def run_tick(self, commands, snapshot):
        self.calls.append((commands, snapshot))
        self.called.set()


class PersistenceFailureHandler:
    def __init__(self) -> None:
        self.commands = []

    async def hold(self, commands):
        self.commands.append(commands)


class Hydration:
    def __init__(self, order) -> None:
        self.order = order

    async def hydrate(self, commands):
        self.order.append("hydrate")


class Preparation:
    def __init__(self, order) -> None:
        self.order = order

    async def prepare(self, commands, snapshot):
        self.order.append(("prepare", snapshot.snapshot_id))


class CandleSink:
    def __init__(self) -> None:
        self.items = []
        self.called = asyncio.Event()

    async def apply_stream_candle(self, candle):
        self.items.append(candle)
        self.called.set()


def test_runtime_updates_subscriptions_and_ticks_one_snapshot_for_all_commands() -> None:
    async def scenario():
        stream = Stream()
        tick = Tick()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1), command(2)))
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(tick.called.wait(), timeout=1)
        await runtime.close()
        await task
        return stream, tick

    stream, tick = asyncio.run(scenario())

    assert stream.subscriptions == [{"i1", "i2"}]
    assert {item.external_instrument_id for item in tick.calls[0][0]} == {"i1"}
    assert set(tick.calls[0][1].instruments) == {"i1"}
    assert stream.closed


def test_runtime_trades_once_for_one_new_order_book_and_does_not_repeat_cached_snapshot() -> None:
    async def scenario():
        stream = OrderBookStream()
        tick = Tick()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        task = asyncio.create_task(runtime.run())
        await stream.events_queue.put(book())
        await asyncio.wait_for(tick.called.wait(), timeout=1)
        await asyncio.sleep(0.05)
        await runtime.close()
        await task
        return tick

    tick = asyncio.run(scenario())

    assert len(tick.calls) == 1


def test_runtime_coalesces_order_books_received_while_decision_is_running() -> None:
    class BlockingTick(Tick):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def run_tick(self, commands, snapshot):
            await super().run_tick(commands, snapshot)
            if len(self.calls) == 1:
                await self.release.wait()

    async def scenario():
        stream = OrderBookStream()
        tick = BlockingTick()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        task = asyncio.create_task(runtime.run())
        await stream.events_queue.put(book("100"))
        await asyncio.wait_for(tick.called.wait(), timeout=1)
        await stream.events_queue.put(book("101"))
        await stream.events_queue.put(book("102"))
        tick.release.set()
        for _ in range(100):
            if len(tick.calls) == 2:
                break
            await asyncio.sleep(0.01)
        await runtime.close()
        await task
        return tick

    tick = asyncio.run(scenario())

    assert len(tick.calls) == 2
    assert tick.calls[1][1].instruments["i1"].order_book.best_bid.price == Decimal("102")


def test_runtime_hydrates_state_before_snapshot_tick() -> None:
    async def scenario():
        order = []
        stream = Stream()

        class OrderedTick(Tick):
            async def run_tick(self, commands, snapshot):
                order.append("tick")
                await super().run_tick(commands, snapshot)

        tick = OrderedTick()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            hydration=Hydration(order),
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(tick.called.wait(), timeout=1)
        await runtime.close()
        await task
        return order

    order = asyncio.run(scenario())

    assert order[:2] == ["hydrate", "tick"]


def test_runtime_prepares_from_preliminary_snapshot_before_final_tick() -> None:
    async def scenario():
        order = []
        stream = Stream()

        class OrderedTick(Tick):
            async def run_tick(self, commands, snapshot):
                order.append(("tick", snapshot.snapshot_id))
                await super().run_tick(commands, snapshot)

        tick = OrderedTick()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            preparation=Preparation(order),
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(tick.called.wait(), timeout=1)
        await runtime.close()
        await task
        return order

    order = asyncio.run(scenario())

    assert order[0][0] == "prepare"
    assert order[1][0] == "tick"
    assert order[0][1] != order[1][1]


def test_retryable_preparation_failure_keeps_runtime_closed_to_ticks_until_retry_succeeds() -> None:
    class RetryablePreparation:
        def __init__(self) -> None:
            self.calls = 0
            self.recovered = asyncio.Event()

        async def prepare(self, commands, snapshot):
            self.calls += 1
            if self.calls == 1:
                raise TInvestAdapterError("BROKER_RATE_LIMITED", "Rate limited.", retryable=True)
            self.recovered.set()

    async def scenario():
        stream = Stream()
        tick = Tick()
        preparation = RetryablePreparation()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            preparation=preparation,
            now=lambda: NOW,
            tick_seconds=0.01,
            retry_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        task = asyncio.create_task(runtime.run())
        await asyncio.wait_for(preparation.recovered.wait(), timeout=1)
        await asyncio.wait_for(tick.called.wait(), timeout=1)
        running_after_recovery = not task.done()
        await runtime.close()
        await task
        return preparation.calls, len(tick.calls), running_after_recovery

    assert asyncio.run(scenario()) == (2, 1, True)


def test_non_retryable_preparation_failure_stops_broker_runtime() -> None:
    class NonRetryablePreparation:
        async def prepare(self, commands, snapshot):
            raise TInvestAdapterError("BROKER_AUTH_FAILED", "Authentication failed.", retryable=False)

    async def scenario():
        runtime = BrokerRuntimeService(
            Stream(),
            HotMarketDataCacheService(),
            Tick(),
            preparation=NonRetryablePreparation(),
            now=lambda: NOW,
            tick_seconds=0.01,
            retry_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        with pytest.raises(ExceptionGroup) as captured:
            await runtime.run()
        await runtime.close()
        return captured.value

    error = asyncio.run(scenario())

    assert any(isinstance(item, TInvestAdapterError) for item in error.exceptions)


def test_runtime_prepares_hold_command_without_sending_it_to_trading_tick() -> None:
    async def scenario():
        order = []
        stream = Stream()
        tick = Tick()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            preparation=Preparation(order),
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1, AutomationState.HOLD),))
        task = asyncio.create_task(runtime.run())
        await asyncio.sleep(0.03)
        await runtime.close()
        await task
        return order, tick

    order, tick = asyncio.run(scenario())

    assert order
    assert tick.calls == []


def test_runtime_forwards_completed_stream_candles_to_indicator_window() -> None:
    class CandleStream(Stream):
        async def events(self):
            yield StreamCandle(
                "i1",
                Decimal("100"),
                Decimal("101"),
                Decimal("99"),
                Decimal("100.5"),
                10,
                NOW,
                True,
                NOW,
            )
            await self.release.wait()

    async def scenario():
        stream = CandleStream()
        sink = CandleSink()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            Tick(),
            candle_sink=sink,
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        task = asyncio.create_task(runtime.run())
        await sink.called.wait()
        await runtime.close()
        await task
        return sink

    sink = asyncio.run(scenario())

    assert len(sink.items) == 1
    assert sink.items[0].close == Decimal("100.5")


def test_durable_persistence_failure_holds_commands_and_stops_broker_runtime() -> None:
    class FailingTick(Tick):
        async def run_tick(self, commands, snapshot):
            self.called.set()
            raise DurableDecisionPersistenceError("durable decision batch was not stored")

    async def scenario():
        stream = Stream()
        tick = FailingTick()
        failure = PersistenceFailureHandler()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            persistence_failure=failure,
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1),))
        with pytest.raises(ExceptionGroup) as captured:
            await runtime.run()
        await runtime.close()
        return failure, captured.value

    failure, error = asyncio.run(scenario())

    assert [[item.automation_id for item in commands] for commands in failure.commands] == [[command(1).automation_id]]
    assert any(isinstance(item, DurableDecisionPersistenceError) for item in error.exceptions)
