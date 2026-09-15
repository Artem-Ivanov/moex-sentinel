import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from sentinel_contracts.broker_execution import BrokerPosition, OrderBookLevel
from sentinel_contracts.streaming_market import StreamOrderBook, StreamTradingStatus
from sentinel_contracts.trading import DecisionKind
from sentinel_contracts.trading_facts import AutomationCommand
from tests.trading_automaton.command_factory import command as baseline_command
from trading_automaton.config import StrategySettings
from trading_automaton.services.account_commission_profile import CommissionSchedule
from trading_automaton.services.broker_runtime import BrokerRuntimeService
from trading_automaton.services.decision import TradeDecision, TradeDecisionService
from trading_automaton.services.decision_context import DecisionContextService
from trading_automaton.services.decision_materialization import DecisionMaterializerService
from trading_automaton.services.hot_market_data import HotMarketDataCacheService
from trading_automaton.services.market_indicators import MarketIndicators
from trading_automaton.services.order_book_validation import OrderBookValidationService
from trading_automaton.services.position_batch_scheduler import PositionBatchSchedulerService
from trading_automaton.services.streaming_batch_tick import StreamingBatchTickService
from trading_automaton.services.streaming_position_decision import (
    HydratedPositionState,
    StreamingPositionDecisionService,
)
from trading_automaton.storage.repository import IntentHistory, TradingCycleState

NOW = datetime(2026, 8, 7, 12, tzinfo=UTC)


def command(index: int) -> AutomationCommand:
    return baseline_command(automation=f"automation-{index}")


def hydrated(automation_id: str) -> HydratedPositionState:
    return HydratedPositionState(
        BrokerPosition("instrument", Decimal("1"), Decimal("100"), Decimal("100"), "RUB"),
        IntentHistory(Decimal("100"), 0, Decimal(), 1, Decimal()),
        (),
        TradingCycleState(automation_id, Decimal("99"), None, True, None, NOW),
        MarketIndicators(Decimal("0.5"), Decimal("0.5"), "TEST", None, None, None, NOW),
    )


class Stream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[object | None] = asyncio.Queue()

    async def replace_subscriptions(self, instrument_ids: set[str]) -> None:
        pass

    async def events(self):
        while (event := await self.queue.get()) is not None:
            yield event

    async def close(self) -> None:
        await self.queue.put(None)


class States:
    def __init__(self) -> None:
        self.values = {
            str(command(index).automation_id): hydrated(str(command(index).automation_id)) for index in (1, 2)
        }

    async def get(self, automation_id: str):
        return self.values[automation_id]

    async def update(self, automation_id: str, value: HydratedPositionState) -> None:
        self.values[automation_id] = value


class Schedules:
    def schedule(self, key, *, snapshot_at):
        assert snapshot_at == NOW
        return CommissionSchedule(Decimal("0.001"), Decimal("0.001"))


class SpyStrategy:
    code = "SPY"
    version = "1"

    def __init__(self) -> None:
        self.calls_by_automation: dict[str, int] = {}

    def decide(self, context):
        automation_id = context.cycle.automation_id
        self.calls_by_automation[automation_id] = self.calls_by_automation.get(automation_id, 0) + 1
        return TradeDecision(DecisionKind.BUY_MORE, 1, Decimal("100.1"), "BUY")


class Batch:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.items = ()
        self.requests = ()
        self.called = asyncio.Event()

    async def run_batch(self, items, requests, *, snapshot_at):
        self.events.append("persist")
        self.items, self.requests = items, requests
        self.events.extend("sdk_dispatch" for _ in requests)
        self.called.set()
        return "persisted"


class Preparation:
    def __init__(self) -> None:
        self.calls = 0
        self.prepared = asyncio.Event()

    async def prepare(self, commands, snapshot) -> None:
        self.calls += 1
        self.prepared.set()


def test_one_order_book_event_evaluates_each_position_once_before_sdk_dispatch() -> None:
    async def scenario():
        stream = Stream()
        states = States()
        strategy = SpyStrategy()
        batch = Batch()
        tick = StreamingBatchTickService(
            PositionBatchSchedulerService(
                StreamingPositionDecisionService(
                    Schedules(),
                    decisions=TradeDecisionService(strategy),
                    contexts=DecisionContextService(StrategySettings()),
                )
            ),
            states,
            batch,
            now=lambda: NOW,
            materializer=DecisionMaterializerService(settings=StrategySettings(), id_factory=lambda: "id"),
            order_books=OrderBookValidationService(),
        )
        preparation = Preparation()
        runtime = BrokerRuntimeService(
            stream,
            HotMarketDataCacheService(),
            tick,
            preparation=preparation,
            now=lambda: NOW,
            tick_seconds=0.01,
        )
        await runtime.replace_commands((command(1), command(2)))
        task = asyncio.create_task(runtime.run())
        await preparation.prepared.wait()
        assert strategy.calls_by_automation == {}
        await stream.queue.put(StreamTradingStatus("instrument", "NORMAL", True, True, NOW))
        await stream.queue.put(
            StreamOrderBook(
                "instrument",
                (OrderBookLevel(Decimal("100"), 1),),
                (OrderBookLevel(Decimal("100.1"), 1),),
                NOW,
                True,
            )
        )
        await asyncio.wait_for(batch.called.wait(), timeout=1)
        await runtime.close()
        await task
        return strategy, batch

    strategy, batch = asyncio.run(scenario())

    assert strategy.calls_by_automation == {
        str(command(1).automation_id): 1,
        str(command(2).automation_id): 1,
    }
    assert batch.events.index("persist") < batch.events.index("sdk_dispatch")
    assert batch.items[0].estimated_commission == Decimal("1.0010")
    assert batch.requests[0].required_cash == Decimal("1002.0010")
